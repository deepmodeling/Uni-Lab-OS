"""工作流人工装入 / 取走 / 补量（``material_io`` 节点）的库存命令。

调度器在人工确认之后提交**一条**类型化库存命令 ``material.io``（``inventory_commands.py``），它在一个
materials.db 事务里完成全部写入：

- ``load``：按资源模板建新物料，放到挂载物料的目标库位上，可选登记试剂；目标库位已有物料时拒绝
  （``site_occupied``），不覆盖。模板名就是注册表资源条目 id（注册表同步的模板）：能实例化时与库存命令
  ``instantiate_material`` 同一条路（``instantiate_registry_resource`` → ``plr_resources_to_create``），建出带 PLR
  ``config`` 与默认子物料（如吸头盒里的吸头）的整棵树，设备台面才能把它反序列化成 PLR 资源；不能实例化时只按模板
  写身份（展开模板库位）。挂载物料属于某台设备时模板必须能实例化，否则计划阶段就拒绝（写库后台面无法同步）；
- ``unload``：``detach`` 解除挂载、物料留在库存里，或 ``soft_delete`` 软删除（``material_with_contents`` 连同子物料
  与内容物）；物料被其他任务的预留、来源绑定或未结算的物理动作占着时拒绝（``active_claim``）；
- ``refill``：给仍在原库位的容器的唯一试剂加量（单位可换算），位置不变、不新建物料。

命令身份由工作流 attempt 决定，``inventory_processed_command`` 保存首次结果（完成或拒绝）：确认后、写库前或写库后
应答前崩溃，重启重放同一命令只会写一次。

``check`` 是同一套写入在保存点里的试运行（随后回滚），确认前用它判断此刻能否执行，与真正写入的规则完全一致；
它抛出的异常与写入时相同，调度器据此区分「等一等」（库位被占、被其他任务占用）和「失败」（合同错误）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping, Optional
from uuid import uuid4

from unilabos.protocol.materials import (
    InventoryMutation,
    MaterialDelete,
    MaterialIdentityWrite,
    MaterialMove,
    MaterialNodeCreate,
    MaterialTreeCreate,
)
from unilabos.protocol.runtime.material_io import MaterialIOParamError, normalize_material_io_param
from unilabos.server.services.materials.chemicals import ChemicalError, normalize_cas
from unilabos.server.services.materials.contents import ContentContract
from unilabos.server.services.materials.core import (
    MaterialConflictError,
    MaterialNotFoundError,
    MaterialsService,
    MaterialValidationError,
)
from unilabos.server.services.materials.quantity import convert_quantity
from unilabos.server.services.materials.store import MaterialsRepository

#: 类型化库存命令的类型名
MATERIAL_IO_COMMAND = "material.io"


def material_io_command_id(job_uuid: str) -> str:
    """命令身份 = 工作流 attempt：同一 attempt 重放只写一次，重试的新 attempt 是新命令。"""

    return f"workflow.material_io:{job_uuid}"


def _load_name_prefix(template_name: str) -> str:
    """模板名最后一段作自动物料名的前缀（V1 ``_load_name_prefix``）。"""

    short = template_name.strip().rsplit(".", 1)[-1].rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    cleaned = "".join(char if char.isalnum() or char in "-_" else "-" for char in short).strip("-")
    return cleaned or "material"


def _registry_draft(template: Any, name: str) -> tuple[Any, str]:
    """按模板名对应的注册表资源类实例化 PLR 草稿（与 ``instantiate_material`` 同一入口）；不能实例化时返回
    ``(None, 原因)``。

    注册表同步的模板以资源条目 id 为名（``registry_definition_to_template``），所以模板名就是
    ``instantiate_registry_resource`` 的资源类名；资源类目录在调度权威里是 Host 上报的生效条目。
    """

    from unilabos.resources.materials import instantiate_registry_resource

    try:
        return instantiate_registry_resource(template.name, name), ""
    except ValueError as exc:
        return None, str(exc)


def _not_deck_template(device_id: str, template: Any, reason: str) -> MaterialValidationError:
    return MaterialValidationError(
        f"挂载物料属于设备 {device_id}，装入的物料要同步到设备台面，必须能按资源模板 {template.name} "
        f"实例化成 PLR 资源；现在不能：{reason}", subject="template")


class MaterialIO:
    """``material_io`` 的身份解析、试运行与一次性写入；与 :class:`MaterialsService` 共用连接和写锁。"""

    def __init__(self, materials: MaterialsService, *, contents: Optional[ContentContract] = None) -> None:
        self.materials = materials
        self.contents = contents or ContentContract(materials, compound_source=None)

    @property
    def connection(self) -> Any:
        return self.materials.connection

    # ── 身份解析 ─────────────────────────────────────────────────

    def plan(self, param: Mapping[str, Any], *, task_uuid: str, job_uuid: str) -> dict[str, Any]:
        """解析完引用后的节点参数 → 冻结的执行计划（挂载物料 / 库位 / 模板 / 物料的 UUID 与锁定集合）。"""

        try:
            values = normalize_material_io_param(param, allow_references=False)
        except MaterialIOParamError as exc:
            raise MaterialValidationError(str(exc)) from None
        if not str(task_uuid or "").strip() or not str(job_uuid or "").strip():
            raise MaterialValidationError("material_io 需要 task_uuid 与 job_uuid")
        mount = self._material(values["mount"], "挂载物料")
        site = self._mount_site(mount, values["site"])
        plan: dict[str, Any] = {
            "mode": values["mode"],
            "task_uuid": str(task_uuid),
            "job_uuid": str(job_uuid),
            "mount_uuid": mount.material.material_uuid,
            "mount_name": mount.material.name,
            "site_uuid": site.site_uuid,
            "site_label": site.label,
        }
        locks = {mount.material.material_uuid}
        if values["mode"] == "load":
            template = self._template(values["resource_template"])
            self._check_deck_template(mount.material.material_uuid, template)
            plan.update(template_uuid=template.template_uuid, template_name=template.name)
            for key in ("material_name", "barcode", "reagent"):
                if key in values:
                    plan[key] = values[key]
        else:
            material = self._material(values["material"], "物料")
            plan.update(material_uuid=material.material.material_uuid,
                        material_display_name=material.material.name)
            locks.add(material.material.material_uuid)
            if values["mode"] == "unload":
                plan.update(disposition=values["disposition"], scope=values["scope"])
            else:
                plan["reagent"] = values["reagent"]
        plan["lock_material_uuids"] = sorted(locks)
        return plan

    def _material(self, reference: Any, label: str) -> Any:
        """``{"uuid": ...}`` 或字符串：先当物料 UUID，查不到再当物料业务 ID（``resource_id``）。"""

        identity = str(reference["uuid"] if isinstance(reference, Mapping) else reference)
        try:
            return self.materials.get_material(identity)
        except MaterialNotFoundError:
            if isinstance(reference, Mapping):
                raise MaterialNotFoundError(f"{label} {identity} 不存在", subject="material") from None
        try:
            return self.materials.get_material_by_resource_id(identity)
        except MaterialNotFoundError:
            raise MaterialNotFoundError(f"{label} {identity} 不存在", subject="material") from None

    @staticmethod
    def _mount_site(mount: Any, selector: str) -> Any:
        """挂载物料上按库位 UUID、名称（不区分大小写）或序号唯一选出一个可见库位。"""

        wanted = selector.strip()
        matches = [site for site in mount.sites if site.site_uuid == wanted]
        if not matches:
            matches = [site for site in mount.sites if site.label.casefold() == wanted.casefold()]
        if not matches:
            matches = [site for site in mount.sites if str(site.site_index) == wanted]
        if not matches:
            raise MaterialNotFoundError(f"挂载物料 {mount.material.name} 上没有库位 {wanted}", subject="site")
        if len(matches) > 1:
            raise MaterialValidationError(f"挂载物料 {mount.material.name} 上的库位 {wanted} 不唯一，请改用库位 UUID")
        if not matches[0].visible:
            raise MaterialValidationError(f"库位 {wanted} 不可见，不能人工装入 / 取走")
        return matches[0]

    def _template(self, identity: str) -> Any:
        try:
            return self.materials.get_template(identity)
        except MaterialNotFoundError:
            record = self.materials.get_template_by_name(identity)
            if record is None:
                raise MaterialNotFoundError(f"资源模板 {identity} 不存在", subject="template") from None
            return self.materials.get_template(record.template_uuid)

    def _check_deck_template(self, mount_uuid: str, template: Any) -> None:
        """计划阶段：挂到设备上的模板必须能实例化成 PLR 资源，不等写库后台面同步失败（只实例化，建树留给写入）。"""

        from unilabos.resources.materials import find_owner_device

        device_id = find_owner_device(mount_uuid, gateway=self.materials)
        if device_id is not None:
            draft, reason = _registry_draft(template, _load_name_prefix(template.name))
            if draft is None:
                raise _not_deck_template(device_id, template, reason)

    def _load_tree(self, mount_uuid: str, template: Any, name: str) -> Optional[MaterialTreeCreate]:
        """装入物料按注册表资源类建出的创建树（``plr_resources_to_create``，带默认子物料）；模板不能实例化时
        返回 None（只写身份）。

        挂载物料属于某台设备（``find_owner_device``）时，只有身份的物料设备反序列化不了、台面同步必然失败：
        直接拒绝并说明原因。普通货架（没有所属设备）照旧只写身份。
        """

        from unilabos.resources.adapters.plr_materials import plr_resources_to_create
        from unilabos.resources.materials import find_owner_device

        draft, reason = _registry_draft(template, name)
        if draft is not None:
            try:
                return plr_resources_to_create([draft])
            except ValueError as exc:
                reason = str(exc)
        device_id = find_owner_device(mount_uuid, gateway=self.materials)
        if device_id is not None:
            raise _not_deck_template(device_id, template, reason)
        return None

    # ── 试运行与写入 ─────────────────────────────────────────────

    def check(self, param: Mapping[str, Any], *, task_uuid: str, job_uuid: str) -> dict[str, Any]:
        """解析身份后在保存点里试写一次再回滚：返回执行计划；此刻不能执行时抛出与写入相同的异常。"""

        plan = self.plan(param, task_uuid=task_uuid, job_uuid=job_uuid)
        with self.materials.write() as conn:
            conn.execute("SAVEPOINT material_io_check")
            try:
                self.apply(plan, command_id=f"material_io.check:{uuid4()}")
            finally:
                conn.execute("ROLLBACK TO material_io_check")
                conn.execute("RELEASE material_io_check")
        return plan

    def apply(self, plan: Mapping[str, Any], *, command_id: str) -> dict[str, Any]:
        """按冻结计划在**调用方的**写事务里完成写入（类型化库存命令已包好事务与幂等）。"""

        mode = plan.get("mode")
        if mode not in {"load", "unload", "refill"}:
            raise MaterialValidationError("material_io 计划缺少有效的 mode")
        with self.materials.write():
            mount = self.materials.get_material(str(plan.get("mount_uuid") or ""))
            site = next((item for item in mount.sites if item.site_uuid == plan.get("site_uuid")), None)
            if site is None:
                raise MaterialNotFoundError(f"挂载物料 {mount.material.name} 上已没有库位 {plan.get('site_label')}",
                                            subject="site")
            if mode == "load":
                return self._load(plan, site, command_id=command_id)
            if mode == "unload":
                return self._unload(plan, site, command_id=command_id)
            return self._refill(plan, site, command_id=command_id)

    def _mutation(self, plan: Mapping[str, Any], command_id: str, step: str, operation: str) -> InventoryMutation:
        return InventoryMutation(
            command_uuid=command_id, effect_key=f"material_io.{step}", operation=operation,
            actor_type="scheduler", job_uuid=str(plan["job_uuid"]),
        )

    def _load(self, plan: Mapping[str, Any], site: Any, *, command_id: str) -> dict[str, Any]:
        template = self.materials.get_template(str(plan["template_uuid"]))
        if site.occupied_material_uuid is not None:
            raise MaterialConflictError(
                f"库位 {site.label} 已有物料 {site.occupied_material_uuid}，不覆盖", conflict="site_occupied")
        record = MaterialsRepository.get_site(self.materials, site.site_uuid)
        self.materials.assert_site_accepts(record, template_uuid=template.template_uuid, template_name=template.name)
        barcode = str(plan.get("barcode") or "")
        if barcode and self.connection.execute(
                "SELECT 1 FROM material WHERE barcode=? AND deleted_at_ms IS NULL", (barcode,)).fetchone():
            raise MaterialConflictError(f"条码 {barcode} 已被其他物料使用", conflict="identity")
        name = str(plan.get("material_name") or "") or self._allocate_name(template.name)
        if self.connection.execute(
                "SELECT 1 FROM material WHERE parent_material_uuid IS NULL AND deleted_at_ms IS NULL "
                "AND LOWER(name)=LOWER(?)", (name,)).fetchone():
            raise MaterialConflictError(f"物料名 {name} 已被另一件根物料使用", conflict="identity")
        from unilabos.server.services.materials.resource_contract import ResourceContract

        material_uuid = str(uuid4())
        meta_data = {"material_io": {"task_uuid": plan["task_uuid"], "job_uuid": plan["job_uuid"]}}
        tree = self._load_tree(str(plan["mount_uuid"]), template, name)
        if tree is None:
            tree = MaterialTreeCreate(nodes=[MaterialNodeCreate(
                client_ref="material",
                material_uuid=material_uuid,
                identity=MaterialIdentityWrite(
                    resource_id=ResourceContract(self.materials)._resource_id(name),
                    name=name,
                    display_name=name,
                    resource_type=template.resource_type,
                    class_name=template.class_name or template.resource_type or "Resource",
                    barcode=barcode,
                    template_name=template.name,
                    meta_data=meta_data,
                ),
            )])
        else:
            self._load_root(tree, material_uuid=material_uuid, name=name, barcode=barcode,
                            template_name=template.name, meta_data=meta_data)
        self.materials.create_tree(self._mutation(plan, command_id, "create", "create_material_tree"), tree)
        self.materials.move_material(
            self._mutation(plan, command_id, "place", "move_material"),
            MaterialMove(material_uuid=material_uuid, destination_site_uuid=site.site_uuid),
        )
        result: dict[str, Any] = {
            "mode": "load",
            "material": {"uuid": material_uuid},
            "material_uuid": material_uuid,
            "name": name,
            "template_uuid": template.template_uuid,
            "mount_uuid": plan["mount_uuid"],
            "site_uuid": site.site_uuid,
            "site_label": site.label,
        }
        if plan.get("reagent"):
            result["reagent"] = self._attach_reagent(material_uuid, dict(plan["reagent"]), command_id=command_id)
        return result

    def _load_root(self, tree: MaterialTreeCreate, *, material_uuid: str, name: str, barcode: str,
                   template_name: str, meta_data: dict[str, Any]) -> None:
        """把注册表资源类建出的创建树的根节点换成装入步骤的身份（原地修改）。

        根节点保留只写身份时的那些字段：物料 UUID、名字 / 展示名、条码、``meta_data.material_io`` 与模板身份
        （注册表模板，而不是 PLR 型号名派生的模板）；``config`` / 类型 / 尺寸与子物料来自 PLR。子物料的
        ``resource_id`` 是根的路径前缀，根换了不重名的 ID 时一起换。
        """

        from unilabos.server.services.materials.resource_contract import ResourceContract

        root = next(node for node in tree.nodes if node.parent_client_ref is None)
        drafted = root.identity.resource_id
        resource_id = ResourceContract(self.materials)._resource_id(drafted)
        if resource_id != drafted:
            for node in tree.nodes:
                if node.identity.resource_id.startswith(f"{drafted}/"):
                    node.identity.resource_id = resource_id + node.identity.resource_id[len(drafted):]
        root.material_uuid = material_uuid
        root.identity = root.identity.model_copy(update={
            "resource_id": resource_id,
            "name": name,
            "display_name": name,
            "barcode": barcode,
            "template_name": template_name,
            "meta_data": {**root.identity.meta_data, **meta_data},
        })

    def _allocate_name(self, template_name: str) -> str:
        """模板短名 + 本地时间到秒；同一秒已占用时再补毫秒和序号（V1 ``allocate_load_material_name``）。"""

        now = datetime.now().astimezone()
        base = f"{_load_name_prefix(template_name)}-{now.strftime('%Y%m%d-%H%M%S')}"

        def taken(candidate: str) -> bool:
            return self.connection.execute(
                "SELECT 1 FROM material WHERE deleted_at_ms IS NULL AND LOWER(name)=LOWER(?) LIMIT 1", (candidate,),
            ).fetchone() is not None

        if not taken(base):
            return base
        stamped = f"{base}-{now.microsecond // 1000:03d}"
        if not taken(stamped):
            return stamped
        for index in range(2, 100):
            if not taken(f"{stamped}-{index}"):
                return f"{stamped}-{index}"
        raise MaterialConflictError("无法分配不重复的物料名，请显式给出 material_name", conflict="identity")

    def _attach_reagent(self, material_uuid: str, reagent: dict[str, Any], *, command_id: str) -> dict[str, Any]:
        """装入时登记试剂：复用已有试剂身份（UUID / CAS / 唯一同名），没有时按 name 新建，再写入数量。"""

        chemical_uuid = reagent.get("reagent_info_uuid")
        if chemical_uuid:
            self.contents.chemicals.get_chemical(chemical_uuid)
        else:
            cas = normalize_cas(reagent["cas"]) if reagent.get("cas") else None
            row = self.connection.execute(
                "SELECT chemical_uuid FROM chemical WHERE cas=? AND deleted_at_ms IS NULL", (cas,),
            ).fetchone() if cas else None
            if row is None and reagent.get("name"):
                matches = [] if cas else self.connection.execute(
                    "SELECT chemical_uuid FROM chemical WHERE LOWER(name)=LOWER(?) AND deleted_at_ms IS NULL",
                    (reagent["name"],),
                ).fetchall()
                if len(matches) == 1:
                    row = matches[0]
                else:
                    values = {"name": reagent["name"], "physical_state": reagent.get("physical_state") or "liquid"}
                    if cas:
                        values["cas"] = cas
                    chemical_uuid = self.contents.chemicals.create_chemical(values)["chemical_uuid"]
            if row is not None:
                chemical_uuid = row[0]
            if not chemical_uuid:
                raise ChemicalError("not_found", f"CAS {cas} 的试剂身份尚未登记；同时给出 name 即可一起登记")
        values = {"material_uuid": material_uuid, "reagent_info_uuid": chemical_uuid,
                  "quantity": reagent["quantity"], "quantity_unit": reagent["quantity_unit"], "source": "material_io"}
        for key in ("physical_state", "concentration_value", "concentration_unit", "density_g_per_ml", "description"):
            if key in reagent:
                values[key] = reagent[key]
        created = self.contents.create_reagent(values, causation_id=command_id)
        return {key: created.get(key) for key in ("uuid", "reagent_info_uuid", "name", "cas", "quantity", "quantity_unit")}

    def _foreign_claims(self, material_uuids: list[str], task_uuid: str) -> list[str]:
        """物料是否被**其他任务**占着：库存预留、内容物数量预留、来源绑定，或任何未结算的物理动作。"""

        marks = ",".join("?" for _ in material_uuids)
        claims: list[str] = []
        for (other,) in self.connection.execute(
            f"SELECT DISTINCT r.task_uuid FROM inventory_reservation r, json_each(r.items_json) item "
            f"WHERE r.status='active' AND r.task_uuid<>? AND json_extract(item.value, '$.material_uuid') IN ({marks})",
            (task_uuid, *material_uuids),
        ):
            claims.append(f"任务 {other} 的库存预留")
        for table, label in (("material_content_reservation", "内容物预留"), ("material_source_binding", "来源绑定")):
            for (other,) in self.connection.execute(
                f"SELECT DISTINCT task_uuid FROM {table} WHERE status='active' AND task_uuid<>? "
                f"AND material_uuid IN ({marks})", (task_uuid, *material_uuids),
            ):
                claims.append(f"任务 {other} 的{label}")
        for (job,) in self.connection.execute(
            f"SELECT DISTINCT job_uuid FROM material_execution_hold WHERE aggregate_type='material' "
            f"AND aggregate_uuid IN ({marks})", tuple(material_uuids),
        ):
            claims.append(f"作业 {job} 未结算的物理动作")
        return claims

    def _occupying(self, plan: Mapping[str, Any], site: Any) -> Any:
        material = self.materials.get_material(str(plan["material_uuid"]))
        if site.occupied_material_uuid != material.material.material_uuid:
            current = self.materials.occupied_site(material.material.material_uuid)
            where = f"库位 {current.label}" if current is not None else "任何库位上"
            raise MaterialConflictError(
                f"物料 {material.material.name} 不在库位 {site.label} 上（现在在{where}）", conflict="state")
        return material

    def _unload(self, plan: Mapping[str, Any], site: Any, *, command_id: str) -> dict[str, Any]:
        material = self._occupying(plan, site)
        material_uuid = material.material.material_uuid
        tree = [record.material_uuid for record in self.materials.tree_materials(material_uuid)]
        if plan["scope"] == "material_only":
            content = self.connection.execute(
                "SELECT 1 FROM material_substance WHERE material_uuid=? LIMIT 1", (material_uuid,)).fetchone()
            if len(tree) > 1 or content is not None or any(item.occupied_material_uuid for item in material.sites):
                raise MaterialConflictError(
                    f"material_only 要求物料 {material.material.name} 没有子物料和内容物；"
                    "连内容物一起处理请用 soft_delete + material_with_contents", conflict="state")
            tree = [material_uuid]
        claims = self._foreign_claims(tree, str(plan["task_uuid"]))
        if claims:
            raise MaterialConflictError(
                f"物料 {material.material.name} 正被占用：{'；'.join(claims)}", conflict="active_claim")
        if plan["disposition"] == "detach":
            if self.connection.execute(
                    "SELECT 1 FROM material WHERE parent_material_uuid IS NULL AND deleted_at_ms IS NULL "
                    "AND LOWER(name)=LOWER(?) AND material_uuid<>?", (material.material.name, material_uuid)).fetchone():
                raise MaterialConflictError(
                    f"取走后物料 {material.material.name} 成为根物料，与已有根物料重名", conflict="identity")
            self.materials.move_material(self._mutation(plan, command_id, "detach", "move_material"),
                                         MaterialMove(material_uuid=material_uuid))
            affected = [material_uuid]
        else:
            deleted = self.materials.delete_material(
                self._mutation(plan, command_id, "delete", "delete_material"),
                MaterialDelete(material_uuid=material_uuid, recursive=plan["scope"] == "material_with_contents"),
            )
            affected = list(deleted.data.deleted_material_uuids)
        return {
            "mode": "unload",
            "material": {"uuid": material_uuid},
            "material_uuid": material_uuid,
            "disposition": plan["disposition"],
            "scope": plan["scope"],
            "affected_material_uuids": affected,
            "mount_uuid": plan["mount_uuid"],
            "site_uuid": site.site_uuid,
            "site_label": site.label,
        }

    def _refill(self, plan: Mapping[str, Any], site: Any, *, command_id: str) -> dict[str, Any]:
        material = self._occupying(plan, site)
        material_uuid = material.material.material_uuid
        content = self.contents._content_of_material(material_uuid, "reagent")
        if content is None:
            raise MaterialNotFoundError(f"物料 {material.material.name} 没有可补量的试剂", subject="reagent")
        substance_uuid = content["substance_uuid"]
        _aggregate, substances = self.contents._substances(material_uuid)
        target = next(item for item in substances if item.substance_uuid == substance_uuid)
        reagent = plan["reagent"]
        converted = convert_quantity(
            float(reagent["quantity"]), str(reagent["quantity_unit"]), target.quantity_unit,
            inventory_type="content", physical_state=target.physical_state,
            concentration_value=content["concentration_value"], density_g_per_ml=content["density_g_per_ml"],
        )
        if converted is None:
            raise MaterialValidationError(
                f"补量单位 {reagent['quantity_unit']} 不能换算到试剂单位 {target.quantity_unit}")
        added = converted[0]
        previous = self.contents._reagent_state(substance_uuid)
        updated = target.model_copy(update={"quantity": target.quantity + added})
        self.contents._put_substances(
            material_uuid, [updated if item.substance_uuid == substance_uuid else item for item in substances],
            job_uuid=str(plan["job_uuid"]),
        )
        self.contents._update_content(substance_uuid, self.materials._now_ms())
        state = self.contents._reagent_state(substance_uuid)
        self.contents._append_history(
            material_uuid=material_uuid, subject_type="reagent", subject_uuid=substance_uuid, event_type="add",
            quantity_delta=added, quantity_unit=target.quantity_unit, revision=state.get("revision"),
            previous=previous, result=state, extension={"source": "material_io"}, operator_type="workflow",
            causation_id=command_id, task_uuid=str(plan["task_uuid"]), job_uuid=str(plan["job_uuid"]),
        )
        return {
            "mode": "refill",
            "material": {"uuid": material_uuid},
            "material_uuid": material_uuid,
            "reagent_uuid": substance_uuid,
            "quantity_added": added,
            "quantity": updated.quantity,
            "quantity_unit": target.quantity_unit,
            "mount_uuid": plan["mount_uuid"],
            "site_uuid": site.site_uuid,
            "site_label": site.label,
        }


__all__ = ["MATERIAL_IO_COMMAND", "MaterialIO", "material_io_command_id"]
