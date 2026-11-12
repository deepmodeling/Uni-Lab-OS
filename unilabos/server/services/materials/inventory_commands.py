"""V1 ``POST /api/v1/inventory/commands``：库存写入的统一命令入口。

两类命令共用这一个入口：

- **dev ``materials.v1`` 变更**（``protocol_version == "materials.v1"``）：原 dev 按操作分路由的
  幂等变更（建树、改数据、移动、转运、快照、模板、批次、预留 …）。按 ``command_uuid`` +
  ``effect_key`` 幂等，结果是 :class:`MutationResult`。``patch_material`` / ``put_position`` /
  ``put_data`` 需要的物料身份放在命令顶层 ``target_uuid``（原路径参数）。
- **V1 类型化命令**（``type`` + ``command_id``）：``reagent.dispense``、``inventory.*``（含 kind=instance 实例入库）、
  ``material.*`` 以及本分支新增的 ``inventory.lot.correct``、``content.*``、``source.*``。
  按 ``command_id`` 幂等，首次结果（完成或拒绝）存在 ``inventory_processed_command``；一条命令的全部写入
  在同一个 materials.db 事务里，拒绝时整体回滚。V1 实例状态映射到 dev 生命周期：warehouse / bench →
  ``active``（上台只是放到父物料或库位上），used → 未删除的 ``retired``，consumed → ``consumed``，
  discarded → 软删除；``expected_version`` 对实例是物料版本、对内容物是 ``content_version``、对批次是批次版本。

响应保持 V1 的裸结果 ``{command_id, status, result | error, error_code, replayed}``（V1 控制台按
``status`` 判断，不经信封）；拒绝时另带 ``code``（V1 业务码）。
"""

from __future__ import annotations

import json
from typing import Any, Callable, Mapping, Optional
from uuid import NAMESPACE_URL, uuid4, uuid5

from unilabos.protocol.base import canonical_hash
from unilabos.protocol.materials import (
    AggregatePrecondition,
    InventoryLotCorrection,
    InventoryLotInbound,
    InventoryMutation,
    InventoryRequirement,
    InventoryReservationCreate,
    InventoryReservationTransition,
    InventoryStocktake,
    InventoryTaskReservationCreate,
    MaterialDataWrite,
    MaterialDelete,
    MaterialDelta,
    MaterialIdentityWrite,
    MaterialInstantiate,
    MaterialMove,
    MaterialNodeCreate,
    MaterialPatch,
    MaterialPosition,
    MaterialSnapshot,
    MaterialSubstance,
    MaterialTransfer,
    MaterialTreeCreate,
    ResourceTemplateWrite,
)
from unilabos.server.api.v1_fields import iso_to_ms
from unilabos.server.services.materials.chemicals import ChemicalError
from unilabos.server.services.materials.contents import ContentContract
from unilabos.server.services.materials.core import (
    MaterialConflictError,
    MaterialNoChangeError,
    MaterialNotFoundError,
    MaterialsService,
    MaterialsServiceError,
    MaterialValidationError,
)
from unilabos.server.services.materials.quantity import quantity_tolerance
from unilabos.server.services.materials.resource_contract import new_mutation

_JOB_NAMESPACE = uuid5(NAMESPACE_URL, "unilabos://v1-inventory-reservations")
CodeOf = Callable[[BaseException], int]


class UnsupportedCommand(MaterialValidationError):
    code = "unsupported_command"


def instantiate_tree(value: MaterialInstantiate) -> MaterialTreeCreate:
    """registry 资源类 → 物料创建树（Host 进程内已加载 registry / PLR）。"""

    from pylabrobot.resources.resource import Resource as ResourcePLR

    from unilabos.resources.adapters.plr_materials import plr_resources_to_create
    from unilabos.resources.graphio import initialize_resource

    draft = initialize_resource({"name": value.name, "class": value.registry_class}, resource_type=ResourcePLR)
    if not isinstance(draft, ResourcePLR):
        raise MaterialValidationError(f"registry 资源类不可实例化: {value.registry_class}")
    tree = plr_resources_to_create([draft])
    if value.barcode:
        # 条码只落在出库产物的根节点（子节点如 tip spot 不带条码）
        for node in tree.nodes:
            if node.parent_client_ref is None:
                node.identity.barcode = value.barcode
                break
    return tree


class InventoryCommands:
    """统一库存命令入口的执行器；返回值即 HTTP 响应体（V1 裸结果）。"""

    def __init__(self, materials: MaterialsService, *, contents: Optional[ContentContract] = None) -> None:
        self.materials = materials
        self.contents = contents or ContentContract(materials, compound_source=None)

    # ── dev materials.v1 变更 ────────────────────────────────────

    def _dev_handlers(self) -> dict[str, tuple[type, Callable[..., Any], bool]]:
        service = self.materials
        return {
            # operation: (载荷模型, 执行函数, 是否需要 target_uuid)
            "put_template": (ResourceTemplateWrite, service.put_template, False),
            "create_template": (ResourceTemplateWrite, service.put_template, False),
            "sync_template": (ResourceTemplateWrite, service.put_template, False),
            "inbound_inventory_lot": (InventoryLotInbound, service.inbound_inventory_lot, False),
            "stocktake_inventory_lot": (InventoryStocktake, service.stocktake_inventory_lot, False),
            "correct_inventory_lot": (InventoryLotCorrection, service.correct_inventory_lot, False),
            "reserve_inventory": (InventoryReservationCreate, service.reserve_inventory, False),
            "reserve_task_inventory": (InventoryTaskReservationCreate, service.reserve_task_inventory, False),
            "consume_inventory_reservation": (InventoryReservationTransition, service.consume_inventory_reservation, False),
            "release_inventory_reservation": (InventoryReservationTransition, service.release_inventory_reservation, False),
            "quarantine_inventory_reservation": (InventoryReservationTransition, service.quarantine_inventory_reservation, False),
            "create_material": (MaterialTreeCreate, service.create_tree, False),
            "create_material_tree": (MaterialTreeCreate, service.create_tree, False),
            "patch_material": (MaterialPatch, service.patch_material, True),
            "update_material": (MaterialPatch, service.patch_material, True),
            "put_position": (MaterialPosition, service.put_position, True),
            "update_position": (MaterialPosition, service.put_position, True),
            "put_data": (MaterialDataWrite, service.put_data, True),
            "update_data": (MaterialDataWrite, service.put_data, True),
            "update_substances": (MaterialDataWrite, service.put_data, True),
            "delete_material": (MaterialDelete, service.delete_material, False),
            "move_material": (MaterialMove, service.move_material, False),
            "transfer_material": (MaterialTransfer, service.transfer_material, False),
            "apply_material_snapshot": (MaterialSnapshot, service.apply_snapshot, False),
            "apply_material_delta": (MaterialDelta, service.apply_delta, False),
        }

    def execute_mutation(self, body: Mapping[str, Any]) -> Any:
        """执行一条 dev 变更；异常原样上抛（由调用方按业务码组装拒绝结果）。"""

        values = dict(body)
        target_uuid = values.pop("target_uuid", None)
        try:
            mutation = InventoryMutation.model_validate(values)
        except ValueError as exc:
            raise MaterialValidationError(f"invalid materials.v1 mutation: {exc}") from None
        operation = mutation.operation
        if operation == "delete_template":
            template_uuid = str(mutation.payload.get("template_uuid") or "")
            if not template_uuid:
                raise MaterialValidationError("delete_template payload requires template_uuid")
            return self.materials.delete_template(mutation, template_uuid)
        if operation == "instantiate_material":
            try:
                value = MaterialInstantiate.model_validate(mutation.payload)
            except ValueError as exc:
                raise MaterialValidationError(str(exc)) from None
            tree = instantiate_tree(value)
            # 实例化展开为建树：effect_key 保持调用方原值维持幂等，载荷重绑为展开后的树
            create = mutation.model_copy(update={
                "operation": "create_material_tree",
                "payload": tree.model_dump(mode="json", exclude_none=False),
            })
            return self.materials.create_tree(create, tree)
        handler = self._dev_handlers().get(operation)
        if handler is None:
            raise UnsupportedCommand(f"unsupported materials.v1 operation: {operation}")
        model, function, needs_target = handler
        try:
            value = model.model_validate(mutation.payload)
        except ValueError as exc:
            raise MaterialValidationError(str(exc)) from None
        if needs_target:
            if not target_uuid:
                raise MaterialValidationError(f"{operation} requires target_uuid")
            return function(mutation, str(target_uuid), value)
        return function(mutation, value)

    def dev_command(self, body: Mapping[str, Any], code_of: CodeOf) -> dict[str, Any]:
        command_id = str(body.get("command_uuid") or "")
        try:
            result = self.execute_mutation(body)
        except (MaterialsServiceError, ChemicalError) as exc:
            return self._rejected(command_id, exc, code_of)
        data = result.model_dump(mode="json", exclude_none=False)
        return {"command_id": command_id, "status": "completed", "result": data, "replayed": bool(data.get("replayed"))}

    # ── V1 类型化命令 ────────────────────────────────────────────

    @staticmethod
    def _error_code(exc: BaseException) -> str:
        code = str(getattr(exc, "code", "") or "inventory_error")
        qualifier = getattr(exc, "subject", None) or getattr(exc, "conflict", None)
        return f"{code}:{qualifier}" if qualifier else code

    def _rejected(self, command_id: str, exc: BaseException, code_of: CodeOf) -> dict[str, Any]:
        return {
            "command_id": command_id,
            "status": "rejected",
            "error": getattr(exc, "message", None) or str(exc) or "command rejected",
            "error_code": self._error_code(exc),
            "code": code_of(exc),
        }

    def _replay(self, row: Any) -> dict[str, Any]:
        if row["status"] == "completed":
            return {"command_id": row["command_id"], "status": "completed",
                    "result": json.loads(row["result_json"] or "{}"), "replayed": True}
        return {"command_id": row["command_id"], "status": "rejected", "error": row["error_message"] or "command rejected",
                "error_code": row["error_code"] or "inventory_error", "code": row["business_code"], "replayed": True}

    def typed_command(self, body: Mapping[str, Any], code_of: CodeOf) -> dict[str, Any]:
        command_id = str(body.get("command_id") or "").strip()
        command_type = str(body.get("type") or "").strip()
        if not command_id or not command_type:
            raise MaterialValidationError("command_id and type are required")
        actor = str(body.get("actor") or "")
        request_hash = canonical_hash({key: value for key, value in body.items() if key != "actor"})
        connection = self.materials.connection
        handler = self._typed_handlers().get(command_type)
        try:
            with self.materials.write():
                existing = connection.execute(
                    "SELECT * FROM inventory_processed_command WHERE command_id=?", (command_id,)
                ).fetchone()
                if existing is not None:
                    if existing["request_hash"] != request_hash:
                        raise MaterialConflictError("command_id was already used with another request", conflict="state")
                    return self._replay(existing)
                if handler is None:
                    raise UnsupportedCommand(f"unsupported inventory command type: {command_type}")
                result = handler(dict(body.get("payload") or {}), command_id=command_id, actor=actor,
                                 expected_version=body.get("expected_version"))
                connection.execute(
                    "INSERT INTO inventory_processed_command(command_id, command_type, request_hash, status, result_json, "
                    "actor, processed_at_ms) VALUES (?,?,?,'completed',?,?,?)",
                    (command_id, command_type, request_hash, json.dumps(result, ensure_ascii=False), actor,
                     self.materials._now_ms()),
                )
        except (MaterialsServiceError, ChemicalError) as exc:
            rejected = self._rejected(command_id, exc, code_of)
            with self.materials.write():
                connection.execute(
                    "INSERT OR IGNORE INTO inventory_processed_command(command_id, command_type, request_hash, status, "
                    "result_json, error_code, error_message, business_code, actor, processed_at_ms) "
                    "VALUES (?,?,?,'rejected','{}',?,?,?,?,?)",
                    (command_id, command_type, request_hash, rejected["error_code"], rejected["error"], rejected["code"],
                     actor, self.materials._now_ms()),
                )
            return rejected
        return {"command_id": command_id, "status": "completed", "result": result, "replayed": False}

    def _typed_handlers(self) -> dict[str, Callable[..., dict[str, Any]]]:
        return {
            "reagent.dispense": self._reagent_dispense,
            "inventory.template.upsert": self._template_upsert,
            "inventory.template.delete": self._template_delete,
            "inventory.inbound": self._inbound,
            "inventory.reserve": self._reserve,
            "inventory.release": lambda payload, **kw: self._transition(payload, "release", **kw),
            "inventory.consume": lambda payload, **kw: self._transition(payload, "consume", **kw),
            "inventory.quarantine": lambda payload, **kw: self._transition(payload, "quarantine", **kw),
            "inventory.reconcile_count": self._reconcile_count,
            "inventory.lot.correct": self._lot_correct,
            "material.move": self._material_move,
            "material.detach": self._material_detach,
            "material.set_parent": self._material_set_parent,
            "material.discard": self._material_discard,
            "material.deploy": self._material_deploy,
            "material.content.set": self._content_set,
            "material.content.clear": self._content_clear,
            "material.consume": self._material_consume,
            "material.mark_used": self._material_mark_used,
            "material.adjust": self._material_adjust,
            "content.reserve": lambda payload, **kw: self.contents.reserve_content(payload),
            "content.release": lambda payload, **kw: self.contents.finish_content_reservation(
                str(payload.get("reservation_uuid") or ""), "released"),
            "content.consume": lambda payload, **kw: self.contents.finish_content_reservation(
                str(payload.get("reservation_uuid") or ""), "consumed", consumed_quantity=payload.get("quantity")),
            "source.bind": lambda payload, **kw: self.contents.bind_material_source(payload),
            "source.release": lambda payload, **kw: {"released": self.contents.release_material_sources(
                str(payload.get("task_uuid") or payload.get("workflow_id") or ""), node_uuid=payload.get("node_uuid"))},
        }

    def _mutation(self, operation: str, actor: str) -> InventoryMutation:
        return new_mutation(operation, actor_type="human", actor_uuid=actor or "inventory.command")

    def _reagent_dispense(self, payload: Mapping[str, Any], *, command_id: str, actor: str, **_: Any) -> dict[str, Any]:
        return self.contents.dispense_reagent(payload, command_id=command_id, actor=actor)

    def _template(self, identity: str) -> Any:
        try:
            return self.materials.get_template(identity)
        except MaterialNotFoundError:
            template = self.materials.get_template_by_name(identity)
            if template is None:
                raise MaterialNotFoundError(f"template not found: {identity}", subject="template") from None
            return self.materials.get_template(template.template_uuid)

    def _template_upsert(self, payload: Mapping[str, Any], *, actor: str, **_: Any) -> dict[str, Any]:
        identity = str(payload.get("template_id") or "").strip()
        if not identity:
            raise MaterialValidationError("template_id is required")
        try:
            current = self._template(identity)
        except MaterialNotFoundError:
            current = None
        spec = dict(payload.get("spec") or {})
        write = ResourceTemplateWrite(
            template_uuid=current.template_uuid if current else None,
            name=current.name if current else identity,
            display_name=str(payload.get("name") or (current.display_name if current else identity)),
            resource_type=current.resource_type if current else str(payload.get("category") or "resource"),
            class_name=current.class_name if current else None,
            category=[str(payload["category"])] if payload.get("category") else (current.category if current else []),
            available_sites=current.available_sites if current else [],
            handles=current.handles if current else [],
            definition={**(current.definition if current else {}), **spec},
        )
        result = self.materials.put_template(self._mutation("put_template", actor), write)
        return result.data.model_dump(mode="json")

    def _template_delete(self, payload: Mapping[str, Any], *, actor: str, **_: Any) -> dict[str, Any]:
        template = self._template(str(payload.get("template_id") or ""))
        self.materials.delete_template(
            new_mutation("delete_template", actor_uuid=actor or None, payload={"template_uuid": template.template_uuid}),
            template.template_uuid,
        )
        return {"template_id": template.template_uuid, "deleted": True}

    _INSTANCE_ONLY = ("barcode", "edge_uuid", "legacy_cloud_id", "cloud_uuid", "parent_uuid", "slot_id")
    _LOT_ONLY = ("quantity", "unit", "batch_no", "expiry")

    def _inbound(self, payload: Mapping[str, Any], *, actor: str, **_: Any) -> dict[str, Any]:
        kind = str(payload.get("kind") or "lot")
        if kind == "instance":
            return self._instance_inbound(payload, actor=actor)
        if kind != "lot":
            raise MaterialValidationError(f"inbound kind must be lot or instance: {kind}")
        if any(payload.get(key) is not None for key in self._INSTANCE_ONLY):
            raise MaterialValidationError("lot inbound contains instance-only fields")
        template = self._template(str(payload.get("template_id") or ""))
        try:
            expiry = iso_to_ms(payload.get("expiry")) if payload.get("expiry") else None
        except ValueError:
            raise MaterialValidationError("expiry must be ISO 8601") from None
        value = InventoryLotInbound(
            lot_uuid=payload.get("lot_id") or None, template_uuid=template.template_uuid,
            batch_no=str(payload.get("batch_no") or ""), unit=str(payload.get("unit") or "count"),
            quantity=float(payload.get("quantity") or 0), expiry_at_ms=expiry,
            location_site_uuid=payload.get("location_site_uuid") or None,
            warehouse_zone_id=str(payload.get("warehouse_zone_id") or ""),
        )
        lot = self.materials.inbound_inventory_lot(self._mutation("inbound_inventory_lot", actor), value).data
        return {**lot.model_dump(mode="json"), "lot_id": lot.lot_uuid, "template_id": lot.template_uuid}

    def _reserve(self, payload: Mapping[str, Any], *, actor: str, **_: Any) -> dict[str, Any]:
        workflow_id = str(payload.get("workflow_id") or "").strip()
        if not workflow_id:
            raise MaterialValidationError("workflow_id is required")
        attempt = int(payload.get("attempt") or 1)
        reservations = []
        for node_id, requirements in dict(payload.get("node_requirements") or {}).items():
            items = []
            for index, raw in enumerate(requirements or []):
                item = dict(raw)
                if item.get("instance_uuid") or item.get("barcode"):
                    material_uuid = item.get("instance_uuid")
                    if not material_uuid:
                        row = self.materials.connection.execute(
                            "SELECT material_uuid FROM material WHERE barcode=? AND deleted_at_ms IS NULL", (item["barcode"],)
                        ).fetchone()
                        if row is None:
                            raise MaterialNotFoundError(f"material not found: barcode {item['barcode']}", subject="material")
                        material_uuid = row[0]
                    items.append(InventoryRequirement(key=f"{node_id}:{index}", kind="material", material_uuid=material_uuid))
                else:
                    template_uuid = self._template(item["template_id"]).template_uuid if item.get("template_id") else None
                    items.append(InventoryRequirement(
                        key=f"{node_id}:{index}", kind="lot", template_uuid=template_uuid,
                        lot_uuid=item.get("lot_id") or None, quantity=float(item.get("quantity") or 0) or None,
                        unit=item.get("unit") or None,
                    ))
            reservations.append(InventoryReservationCreate(
                task_uuid=workflow_id, node_uuid=str(node_id),
                job_uuid=str(uuid5(_JOB_NAMESPACE, f"{workflow_id}:{node_id}:{attempt}")),
                scheduler_revision=attempt, requirements=items,
            ))
        if not reservations:
            raise MaterialValidationError("node_requirements is required")
        result = self.materials.reserve_task_inventory(
            self._mutation("reserve_task_inventory", actor),
            InventoryTaskReservationCreate(task_uuid=workflow_id, scheduler_revision=attempt, reservations=reservations),
        )
        return {"workflow_id": workflow_id, "attempt": attempt, **result.data.model_dump(mode="json")}

    def _transition(self, payload: Mapping[str, Any], action: str, *, actor: str, **_: Any) -> dict[str, Any]:
        workflow_id = str(payload.get("workflow_id") or "").strip()
        if not workflow_id:
            raise MaterialValidationError("workflow_id is required")
        node_id = str(payload.get("node_id") or "")
        active = [
            item for item in self.materials.list_inventory_reservations(task_uuid=workflow_id, status="active")
            if not node_id or item.node_uuid == node_id
        ]
        if not active and action != "release":
            raise MaterialNotFoundError(f"no active reservation for workflow {workflow_id}", subject="reservation")
        method = {"release": self.materials.release_inventory_reservation,
                  "consume": self.materials.consume_inventory_reservation,
                  "quarantine": self.materials.quarantine_inventory_reservation}[action]
        done = []
        for item in active:
            value = InventoryReservationTransition(reservation_uuid=item.reservation_uuid, reason=str(payload.get("reason") or ""))
            done.append(method(self._mutation(f"{action}_inventory_reservation", actor), value).data.model_dump(mode="json"))
        return {"workflow_id": workflow_id, "reservations": done}

    def _reconcile_count(self, payload: Mapping[str, Any], *, actor: str, expected_version: Any = None, **_: Any) -> dict[str, Any]:
        lot = self.materials.get_inventory_lot(str(payload.get("lot_id") or ""))
        value = InventoryStocktake(
            lot_uuid=lot.lot_uuid, expected_version=int(payload.get("expected_version") or expected_version or lot.version),
            measured_count=int(payload.get("measured_count", payload.get("count", 0))),
            reason=str(payload.get("reason") or "reconcile_count"),
        )
        return self.materials.stocktake_inventory_lot(self._mutation("stocktake_inventory_lot", actor), value).data.model_dump(mode="json")

    def _lot_correct(self, payload: Mapping[str, Any], *, actor: str, expected_version: Any = None, **_: Any) -> dict[str, Any]:
        values = dict(payload)
        values.setdefault("lot_uuid", values.pop("lot_id", None))
        if values.get("expected_version") is None and expected_version is not None:
            values["expected_version"] = expected_version
        try:
            value = InventoryLotCorrection.model_validate(values)
        except ValueError as exc:
            raise MaterialValidationError(str(exc)) from None
        return self.materials.correct_inventory_lot(self._mutation("correct_inventory_lot", actor), value).data.model_dump(mode="json")

    def _move(self, move: MaterialMove, actor: str) -> dict[str, Any]:
        return self.materials.move_material(self._mutation("move_material", actor), move).data.model_dump(mode="json")

    def _material_move(self, payload: Mapping[str, Any], *, actor: str, **_: Any) -> dict[str, Any]:
        material_uuid = str(payload.get("edge_uuid") or payload.get("material_uuid") or "")
        site_uuid = payload.get("site_uuid") or None
        if not site_uuid and payload.get("parent_uuid") and payload.get("slot_id"):
            site = self.materials.connection.execute(
                "SELECT site_uuid FROM site WHERE owner_material_uuid=? AND (label=? OR CAST(site_index AS TEXT)=?) "
                "AND deleted_at_ms IS NULL", (payload["parent_uuid"], payload["slot_id"], str(payload["slot_id"])),
            ).fetchone()
            if site is None:
                raise MaterialNotFoundError(f"site not found: {payload['parent_uuid']}/{payload['slot_id']}", subject="site")
            site_uuid = site[0]
        return self._move(MaterialMove(material_uuid=material_uuid, destination_site_uuid=site_uuid,
                                       parent_material_uuid=None if site_uuid else payload.get("parent_uuid") or None), actor)

    def _material_detach(self, payload: Mapping[str, Any], *, actor: str, **_: Any) -> dict[str, Any]:
        return self._move(MaterialMove(material_uuid=str(payload.get("edge_uuid") or payload.get("material_uuid") or "")), actor)

    def _material_set_parent(self, payload: Mapping[str, Any], *, actor: str, **_: Any) -> dict[str, Any]:
        return self._material_move(payload, actor=actor)

    def _material_discard(self, payload: Mapping[str, Any], *, actor: str, **_: Any) -> dict[str, Any]:
        value = MaterialDelete(material_uuid=str(payload.get("edge_uuid") or payload.get("material_uuid") or ""),
                               recursive=bool(payload.get("recursive", True)))
        return self.materials.delete_material(
            new_mutation("delete_material", actor_uuid=actor or None, payload=value.model_dump(mode="json")), value
        ).data.model_dump(mode="json")

    # ── V1 实例命令：上台 / 内容物 / 消耗 / 标为已使用 / 批次调整 / 实例入库 ─────────

    #: dev 生命周期 → V1 实例状态（未删除的 retired 即 V1 的 used：保留身份、库位与内容，只退出自动选料）
    _V1_STATUS = {"retired": "used"}

    def _material_identity(self, payload: Mapping[str, Any]) -> str:
        identity = str(payload.get("edge_uuid") or payload.get("material_uuid") or "").strip()
        if not identity:
            raise MaterialValidationError("edge_uuid is required")
        return identity

    @staticmethod
    def _expect_version(current: int, expected: Any, what: str) -> None:
        """V1 乐观并发：``expected_version`` 给出时必须等于当前版本（禁止 Last-Write-Wins）。"""

        if expected is None:
            return
        try:
            value = int(expected)
        except (TypeError, ValueError):
            raise MaterialValidationError("expected_version must be an integer") from None
        if value != current:
            raise MaterialConflictError(f"{what} version is {current}, expected {value}", conflict="state")

    def _instance_result(self, material_uuid: str, **extra: Any) -> dict[str, Any]:
        aggregate = self.materials.get_material(material_uuid)
        material = aggregate.material
        site = self.materials.occupied_site(material_uuid)
        return {
            "edge_uuid": material.material_uuid,
            "material_uuid": material.material_uuid,
            "template_id": material.template_uuid,
            "lot_id": material.lot_uuid or "",
            "barcode": material.barcode,
            "status": self._V1_STATUS.get(material.lifecycle_status, material.lifecycle_status),
            "lifecycle_status": material.lifecycle_status,
            "parent_uuid": material.parent_material_uuid or "",
            "slot_id": site.label if site is not None else "",
            "site_uuid": site.site_uuid if site is not None else None,
            "legacy_cloud_id": str(material.extra.get("legacy_cloud_id") or ""),
            "version": material.version,
            **extra,
        }

    def _active_claims(self, material_uuid: str, lifecycle_status: str) -> list[str]:
        """物料当前被谁占用：作业库存预留、内容物数量预留、任务独占来源、未结算物理动作。"""

        connection = self.materials.connection
        claims: list[str] = []
        if lifecycle_status in {"reserved", "in_use"}:
            claims.append(f"inventory reservation ({lifecycle_status})")
        if connection.execute(
            "SELECT 1 FROM material_content_reservation WHERE material_uuid=? AND status='active' LIMIT 1",
            (material_uuid,),
        ).fetchone():
            claims.append("active content reservation")
        if connection.execute(
            "SELECT 1 FROM material_source_binding WHERE material_uuid=? AND status='active' "
            "AND custody_policy='task_exclusive' LIMIT 1", (material_uuid,),
        ).fetchone():
            claims.append("task-exclusive source binding")
        hold = connection.execute(
            "SELECT job_uuid FROM material_execution_hold WHERE aggregate_type='material' AND aggregate_uuid=?",
            (material_uuid,),
        ).fetchone()
        if hold is not None:
            claims.append(f"unsettled physical action {hold[0]}")
        return claims

    def _assert_unclaimed(self, material_uuid: str, lifecycle_status: str, action: str) -> None:
        claims = self._active_claims(material_uuid, lifecycle_status)
        if claims:
            raise MaterialConflictError(
                f"material {material_uuid} cannot be {action} while held by: {', '.join(claims)}",
                conflict="active_claim",
            )

    def _site_for_slot(self, parent_uuid: str, slot_id: Any) -> Optional[str]:
        if slot_id in (None, ""):
            return None
        site = self.materials.connection.execute(
            "SELECT site_uuid FROM site WHERE owner_material_uuid=? AND (label=? OR CAST(site_index AS TEXT)=?) "
            "AND deleted_at_ms IS NULL", (parent_uuid, str(slot_id), str(slot_id)),
        ).fetchone()
        if site is None:
            raise MaterialNotFoundError(f"site not found: {parent_uuid}/{slot_id}", subject="site")
        return site[0]

    def _place(self, material_uuid: str, parent_uuid: str, slot_id: Any, actor: str) -> None:
        try:
            self.materials.get_material(parent_uuid)
        except MaterialNotFoundError:
            raise MaterialNotFoundError(f"parent material not found: {parent_uuid}", subject="parent") from None
        site_uuid = self._site_for_slot(parent_uuid, slot_id)
        move = MaterialMove(material_uuid=material_uuid, destination_site_uuid=site_uuid,
                            parent_material_uuid=None if site_uuid else parent_uuid)
        try:
            self._move(move, actor)
        except MaterialNoChangeError:
            pass

    def _material_deploy(self, payload: Mapping[str, Any], *, actor: str, expected_version: Any = None,
                         **_: Any) -> dict[str, Any]:
        """V1 上台：把仓储中的实例放到父物料（可选具名库位）上；已消耗 / 已使用 / 隔离的实例不能上台。"""

        material_uuid = self._material_identity(payload)
        parent_uuid = str(payload.get("parent_uuid") or "").strip()
        if not parent_uuid:
            raise MaterialValidationError("material.deploy requires parent_uuid")
        material = self.materials.get_material(material_uuid).material
        self._expect_version(material.version, expected_version, "material")
        if material.lifecycle_status not in {"active", "reserved"}:
            raise MaterialConflictError(
                f"material {material_uuid} is {material.lifecycle_status}, cannot be deployed", conflict="state")
        self._place(material_uuid, parent_uuid, payload.get("slot_id"), actor)
        return self._instance_result(material_uuid)

    def _set_lifecycle(self, material_uuid: str, status: str, actor: str, reason: str) -> None:
        current = self.materials.get_material(material_uuid).material
        mutation = new_mutation("patch_material", actor_uuid=actor or None)
        mutation = mutation.model_copy(update={"preconditions": [AggregatePrecondition(
            aggregate_type="material", aggregate_uuid=material_uuid, expected_version=current.version)]})
        meta = dict(current.meta_data)
        if reason:
            meta["lifecycle_reason"] = reason
        self.materials.patch_material(mutation, material_uuid, MaterialPatch(lifecycle_status=status, meta_data=meta))

    def _material_consume(self, payload: Mapping[str, Any], *, actor: str, expected_version: Any = None,
                          **_: Any) -> dict[str, Any]:
        """V1 消耗：实例终态 ``consumed``，同时退出父物料与库位（历史留在账本）。"""

        material_uuid = self._material_identity(payload)
        material = self.materials.get_material(material_uuid).material
        self._expect_version(material.version, expected_version, "material")
        if material.lifecycle_status not in {"active", "reserved", "in_use", "retired"}:
            raise MaterialConflictError(
                f"material {material_uuid} is {material.lifecycle_status}, cannot be consumed", conflict="state")
        self._assert_unclaimed(material_uuid, material.lifecycle_status, "consumed")
        if material.parent_material_uuid is not None or self.materials.occupied_site(material_uuid) is not None:
            self._move(MaterialMove(material_uuid=material_uuid), actor)
        self._set_lifecycle(material_uuid, "consumed", actor, str(payload.get("reason") or ""))
        return self._instance_result(material_uuid, previous_status=self._V1_STATUS.get(
            material.lifecycle_status, material.lifecycle_status))

    def _material_mark_used(self, payload: Mapping[str, Any], *, actor: str, expected_version: Any = None,
                            **_: Any) -> dict[str, Any]:
        """V1 标为已使用：保留身份、库位与内容，只退出自动选料；已是 used 时直接返回（幂等）。"""

        material_uuid = self._material_identity(payload)
        material = self.materials.get_material(material_uuid).material
        self._expect_version(material.version, expected_version, "material")
        if material.lifecycle_status == "retired":
            return self._instance_result(material_uuid, previous_status="used")
        if material.lifecycle_status not in {"active", "reserved", "in_use"}:
            raise MaterialConflictError(
                f"material {material_uuid} is {material.lifecycle_status}, cannot be marked used", conflict="state")
        self._assert_unclaimed(material_uuid, material.lifecycle_status, "marked used")
        self._set_lifecycle(material_uuid, "retired", actor, str(payload.get("reason") or ""))
        return self._instance_result(material_uuid, previous_status=material.lifecycle_status)

    def _content_version(self, aggregate: Any, expected: Any) -> None:
        data = aggregate.data
        empty = not data.data and not data.substances
        if expected is not None and str(expected).strip() == "0" and empty:
            return  # V1：从未写过内容物的实例版本为 0
        self._expect_version(data.content_version, expected, "content")

    def _write_content(self, material_uuid: str, data: Mapping[str, Any], substances: list[MaterialSubstance],
                       actor: str) -> int:
        aggregate = self.materials.get_material(material_uuid)
        current = aggregate.data
        value = MaterialDataWrite(
            data=dict(data), substances=substances, sites_initialized=current.sites_initialized,
            unknown_counter=current.unknown_counter, state_status=current.state_status,
            observed_at_ms=current.observed_at_ms,
        )
        try:
            result = self.materials.put_data(new_mutation("put_data", actor_uuid=actor or None), material_uuid, value)
        except MaterialNoChangeError:
            return current.content_version
        return result.data.data.content_version

    def _content_set(self, payload: Mapping[str, Any], *, actor: str, expected_version: Any = None,
                     **_: Any) -> dict[str, Any]:
        """V1 写内容物状态：``state`` 整体成为物料数据；``state.substances`` 给出时替换内容物（数量唯一权威），
        不能低于活动的内容物数量预留。"""

        material_uuid = self._material_identity(payload)
        state = payload.get("state")
        if not isinstance(state, Mapping):
            raise MaterialValidationError("material.content.set requires a state object")
        aggregate = self.materials.get_material(material_uuid)
        self._content_version(aggregate, expected_version)
        data = {key: value for key, value in state.items() if key != "substances"}
        substances = [
            MaterialSubstance.model_validate({**item.model_dump(mode="json"), "chemical": None})
            for item in aggregate.data.substances
        ]
        if "substances" in state:
            raw = state.get("substances")
            if not isinstance(raw, list):
                raise MaterialValidationError("state.substances must be a list")
            try:
                substances = [MaterialSubstance.model_validate(item) for item in raw]
            except ValueError as exc:
                raise MaterialValidationError(f"invalid state.substances: {exc}") from None
            self._assert_reservations_kept(material_uuid, substances)
        version = self._write_content(material_uuid, data, substances, actor)
        return {"instance_uuid": material_uuid, "edge_uuid": material_uuid, "version": version, "state": dict(state)}

    def _assert_reservations_kept(self, material_uuid: str, substances: list[MaterialSubstance]) -> None:
        by_uuid = {item.substance_uuid: item for item in substances if item.substance_uuid}
        rows = self.materials.connection.execute(
            "SELECT DISTINCT substance_uuid FROM material_content_reservation WHERE material_uuid=? AND status='active'",
            (material_uuid,),
        ).fetchall()
        for (substance_uuid,) in rows:
            kept = by_uuid.get(substance_uuid)
            if kept is None:
                raise MaterialConflictError(
                    f"content {substance_uuid} has an active workflow reservation and cannot be removed",
                    conflict="active_claim")
            reserved = self.contents._reserved(substance_uuid, kept.quantity_unit)
            if kept.quantity + quantity_tolerance(kept.quantity, reserved) < reserved:
                raise MaterialConflictError(
                    f"quantity {kept.quantity:g} {kept.quantity_unit} is below the active workflow reservation "
                    f"{reserved:g} {kept.quantity_unit}", conflict="state")

    def _content_clear(self, payload: Mapping[str, Any], *, actor: str, expected_version: Any = None,
                       **_: Any) -> dict[str, Any]:
        """V1 清空内容物：数据与内容物都清空，版本递增；有活动内容物预留时拒绝。"""

        material_uuid = self._material_identity(payload)
        aggregate = self.materials.get_material(material_uuid)
        self._content_version(aggregate, expected_version)
        self._assert_reservations_kept(material_uuid, [])
        version = self._write_content(material_uuid, {}, [], actor)
        return {"instance_uuid": material_uuid, "edge_uuid": material_uuid, "version": version, "state": {}}

    def _material_adjust(self, payload: Mapping[str, Any], *, actor: str, expected_version: Any = None,
                         **_: Any) -> dict[str, Any]:
        """V1 人工盘点调整批次总量：必须带 reason 与 actor；可用量随差额变化，不能低于已预留量。"""

        lot_uuid = str(payload.get("lot_id") or "").strip()
        reason = str(payload.get("reason") or "").strip()
        if not lot_uuid or not reason or not actor:
            raise MaterialValidationError("material.adjust requires lot_id, reason and actor for audit")
        try:
            new_total = float(payload["new_total"])
        except (KeyError, TypeError, ValueError):
            raise MaterialValidationError("material.adjust requires a numeric new_total") from None
        if new_total < 0 or new_total != new_total or new_total in (float("inf"), float("-inf")):
            raise MaterialValidationError("new_total must be a finite non-negative quantity")
        lot = self.materials.get_inventory_lot(lot_uuid)
        self._expect_version(lot.version, expected_version, "lot")
        value = InventoryLotCorrection(lot_uuid=lot.lot_uuid, expected_version=lot.version,
                                       measured_quantity=new_total, reason=reason)
        corrected = self.materials.correct_inventory_lot(self._mutation("correct_inventory_lot", actor), value).data
        return {**corrected.model_dump(mode="json"), "lot_id": corrected.lot_uuid, "template_id": corrected.template_uuid,
                "delta": corrected.quantity_total - lot.quantity_total}

    def _instance_inbound(self, payload: Mapping[str, Any], *, actor: str) -> dict[str, Any]:
        """V1 实例入库：登记一件离散物料（可选放到父物料 / 库位）；``edge_uuid`` 给出时按它幂等，
        云端 ``cloud_uuid`` / ``legacy_cloud_id`` 只记映射，不覆盖本地身份。"""

        if any(payload.get(key) is not None for key in self._LOT_ONLY):
            raise MaterialValidationError("instance inbound contains lot-only fields")
        legacy = str(payload.get("legacy_cloud_id") or "").strip()
        cloud = str(payload.get("cloud_uuid") or "").strip()
        if legacy and cloud and legacy != cloud:
            raise MaterialValidationError("legacy_cloud_id and cloud_uuid conflict")
        legacy = legacy or cloud
        edge_uuid = str(payload.get("edge_uuid") or "").strip()
        if edge_uuid:
            try:
                existing = self.materials.get_material(edge_uuid).material
            except MaterialNotFoundError:
                existing = None
            if existing is not None:
                # 幂等重放：只补云端映射，不改身份与状态
                if legacy and not existing.extra.get("legacy_cloud_id"):
                    self.materials.patch_material(
                        new_mutation("patch_material", actor_uuid=actor or None), edge_uuid,
                        MaterialPatch(extra={**existing.extra, "legacy_cloud_id": legacy}),
                    )
                return self._instance_result(edge_uuid, created=False)
        lot_uuid = str(payload.get("lot_id") or "").strip() or None
        template_identity = str(payload.get("template_id") or "").strip()
        if not template_identity and lot_uuid:
            template_identity = self.materials.get_inventory_lot(lot_uuid).template_uuid
        elif lot_uuid:
            self.materials.get_inventory_lot(lot_uuid)
        if not template_identity:
            raise MaterialValidationError("instance inbound requires template_id or lot_id")
        template = self._template(template_identity)
        barcode = str(payload.get("barcode") or "").strip()
        if barcode and self.materials.connection.execute(
            "SELECT 1 FROM material WHERE barcode=? AND deleted_at_ms IS NULL", (barcode,)
        ).fetchone():
            raise MaterialConflictError(f"barcode {barcode} is already active", conflict="identity")
        from unilabos.server.services.materials.resource_contract import ResourceContract

        material_uuid = edge_uuid or str(uuid4())
        name = barcode or template.display_name or template.name
        tree = MaterialTreeCreate(nodes=[MaterialNodeCreate(
            client_ref="material",
            material_uuid=material_uuid,
            identity=MaterialIdentityWrite(
                resource_id=ResourceContract(self.materials)._resource_id(name), name=name, display_name=name,
                lot_uuid=lot_uuid, resource_type=template.resource_type,
                class_name=template.class_name or template.resource_type or "Resource",
                barcode=barcode, template_name=template.name,
                extra={"legacy_cloud_id": legacy} if legacy else {},
            ),
        )])
        self.materials.create_tree(
            new_mutation("create_material_tree", actor_uuid=actor or None,
                         payload=tree.model_dump(mode="json", exclude_none=False)), tree,
        )
        parent_uuid = str(payload.get("parent_uuid") or "").strip()
        if parent_uuid:
            self._place(material_uuid, parent_uuid, payload.get("slot_id"), actor)
        return self._instance_result(material_uuid, created=True)

    # ── 入口 ──────────────────────────────────────────────────

    def execute(self, body: Mapping[str, Any], code_of: CodeOf) -> dict[str, Any]:
        if body.get("protocol_version") == "materials.v1" or "command_uuid" in body:
            return self.dev_command(body, code_of)
        return self.typed_command(body, code_of)

    def processed(self, *, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.materials.connection.execute(
            "SELECT command_id, command_type, status, error_code, business_code, actor, processed_at_ms "
            "FROM inventory_processed_command ORDER BY processed_at_ms DESC, command_id LIMIT ?", (limit,),
        ).fetchall()
        commands = [{**{key: row[key] for key in row.keys()}, "type": row["command_type"], "family": "v1"} for row in rows]
        effects = self.materials.connection.execute(
            "SELECT command_uuid, effect_key, operation, status, error_code, completed_at_ms, updated_at_ms "
            "FROM inventory_command_effect ORDER BY updated_at_ms DESC, command_uuid LIMIT ?", (limit,),
        ).fetchall()
        commands += [{
            "command_id": row["command_uuid"], "effect_key": row["effect_key"], "type": row["operation"],
            "status": {"applied": "completed"}.get(row["status"], row["status"]), "error_code": row["error_code"],
            "processed_at_ms": row["completed_at_ms"] or row["updated_at_ms"], "family": "materials.v1",
        } for row in effects]
        commands.sort(key=lambda item: item["processed_at_ms"] or 0, reverse=True)
        return commands[:limit]


__all__ = ["InventoryCommands", "UnsupportedCommand", "instantiate_tree"]
