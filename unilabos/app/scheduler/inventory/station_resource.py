"""工站调度读取设备、库位（Site）与转运事实的窄库存接缝。"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from unilabos.app.scheduler.inventory.dispatch_admission import (
    DispatchAdmissionDecision,
    DispatchAdmissionRequest,
    DispatchFence,
    TemporaryDispatchCondition,
    acquire_dispatch_permit,
    acquire_dispatch_permit_candidates,
    check_reserved_pick_site,
    release_preheld_dispatch_claims,
    release_task_dispatch_permits,
    release_unprojected_dispatch_permits,
    retain_dispatch_permit_resources,
    transition_dispatch_permit,
    validate_active_dispatch_permit,
)
from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.workflow.resource_lock_key import device_lock_key, material_lock_key, site_lock_key


class StationResourceError(ValueError):
    """库存权威无法证明调度所需工站资源事实。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        resources: Sequence[Mapping[str, str]] = (),
    ) -> None:
        """保存稳定诊断码和中文原因。

        参数：``code`` 是调度可判断的稳定错误码；``message`` 是供日志和界面
        展示的中文原因；``resources`` 是库存权威已经确认的实际阻塞设备、物料
        或库位。返回：无。异常：构造过程不访问外部状态。
        """

        super().__init__(message)
        self.code = code
        self.message = message
        self.resources = tuple(dict(resource) for resource in resources)


@dataclass(frozen=True, slots=True)
class TargetSiteRequest:
    """一次目标库位（Site）选择所需的完整库存条件。"""

    owner_material_uuid: str
    site_uuid: str = ""
    site_name: str = ""
    equivalent_site_uuids: tuple[str, ...] = ()
    occupant_material_uuid: str = ""
    unavailable_site_uuids: tuple[str, ...] = ()
    require_available: bool = True


@dataclass(frozen=True, slots=True)
class StationSiteTarget:
    """库存权威已确认可接收物料的目标库位（Site）。"""

    uuid: str
    name: str
    owner_material_uuid: str


@dataclass(frozen=True, slots=True)
class TransferResourceRequest:
    """机械臂转运完整资源事实的解析请求。"""

    resource_material_uuid: str
    target_site_uuid: str
    target_owner_material_uuid: str
    executor_material_uuid: str = ""
    gripper_site_role: str = ""
    require_device_owners: bool = False
    allow_held_material: bool = False


@dataclass(frozen=True, slots=True)
class TransferResourceFacts:
    """库存权威已证明的转运来源、设备祖先和夹爪库位事实。"""

    source_site_uuid: str
    source_site_name: str
    source_owner_material_uuid: str
    source_device_material_uuid: str
    target_device_material_uuid: str
    gripper_site_uuid: str


@dataclass(frozen=True, slots=True)
class OperateInPlaceFacts:
    """库存已证明物料当前位于实际执行设备内的精确库位事实。"""

    material_uuid: str
    site_uuid: str
    site_owner_material_uuid: str
    device_material_uuid: str


@dataclass(frozen=True, slots=True)
class MaterialTransferCommand:
    """成功作业携带完整 Permit 向库存权威提交的幂等物料移动命令。"""

    material_uuid: str
    target_owner_material_uuid: str
    target_site_uuid: str = ""
    target_site_name: str = ""
    actor: str = ""
    causation_id: str = ""
    effect_uuid: str = ""
    claim_uuid: str = ""
    job_uuid: str = ""
    attempt: int = 0
    parameter_hash: str = ""
    expected_change_set: Mapping[str, Any] | None = None
    fences: tuple[DispatchFence, ...] = ()


@dataclass(frozen=True, slots=True)
class AliquotReceipt:
    """执行器确认写入一个目标容器的实际分装数量。"""

    target_material_uuid: str
    actual_quantity: float
    quantity_unit: str


@dataclass(frozen=True, slots=True)
class MaterialAliquotCommand:
    """携完整 Permit 和全目标回执的成功分装物理结算。"""

    source_material_uuid: str
    receipts: tuple[AliquotReceipt, ...]
    effect_uuid: str
    claim_uuid: str
    job_uuid: str
    attempt: int
    parameter_hash: str
    expected_change_set: Mapping[str, Any]
    fences: tuple[DispatchFence, ...]


class StationResourceInventory(Protocol):
    """向调度器隐藏库存表、SQL 和资源树遍历的深模块接口。"""

    def error_handling_inventory(self) -> dict[str, Any]: ...

    def correct_inventory(self, command_uuid: str, request: dict[str, Any]) -> dict[str, Any]: ...

    def release_error_occupancies(self, occupancies: Sequence[Mapping[str, Any]]) -> None: ...

    def check_pick_destination(self, owner_uuid: str, site_uuid: str, material_uuid: str) -> None:
        """只读核对拆分取放的最终库位，派发时仍在库存事务中复验。"""
        ...

    def resolve_reservation_site(self, resource_id: str, site_name: str) -> StationSiteTarget:
        """按部署物料身份和库位名称解析预留的库位。"""
        ...

    def plan_transfer_resources(
        self, *, owner_uuids: Sequence[str], executor_uuid: str, gripper_role: str,
    ) -> tuple[str, ...]:
        """读取部署的设备祖先和夹爪身份，不在计划阶段判定运行时占用。"""

    def is_device_material(
        self,
        material_uuid: str,
        *,
        resource_template_uuid: str,
    ) -> bool:
        """判断具体物料是否为指定模板的设备实例。

        参数：``material_uuid`` 是候选设备物料身份；``resource_template_uuid``
        是冻结执行计划要求的模板身份。返回：两者匹配且候选类型为设备时为真。
        异常：库存读取失败原样传播，不得把读取失败降级成不匹配。
        """

    def describe_wait_resources(
        self,
        resources: Sequence[Mapping[str, str]],
    ) -> tuple[dict[str, str], ...]:
        """用库存当前名称补齐等待资源，不改变其稳定身份。"""

    def resolve_named_site_group(
        self,
        *,
        owner_material_uuid: str,
        group_key: str,
    ) -> Mapping[str, Any]:
        """按父物料和命名组键返回有序库位 UUID，不看占用。

        参数：``owner_material_uuid`` 是运行时已经选出的父物料；``group_key`` 是
        作者写下的库位组。返回：库存权威的有序 ``site_uuids`` 与指纹。异常：组不
        存在或父物料非法时抛 ``StationResourceError``。
        """

    def resolve_target_site(self, request: TargetSiteRequest) -> StationSiteTarget:
        """按稳定选择器解析当前可接收物料的目标库位（Site）。

        参数：``request`` 包含父资源、显式库位或等价组及当前不可用集合。返回：
        已验证归属、模板和占用条件的规范库位。异常：事实不足或条件冲突时抛
        ``StationResourceError``；底层读取故障原样传播。
        """

    def resolve_transfer_resources(
        self,
        request: TransferResourceRequest,
    ) -> TransferResourceFacts:
        """解析机械臂转运需要同时占用的全部库存资源事实。

        参数：``request`` 标识被搬物料、目标库位、目标设备和可选机械臂。返回：
        来源库位、来源/目标设备和夹爪库位的规范资源集合。异常：任一资源无法从
        库存事实证明时抛 ``StationResourceError``；底层读取故障原样传播。
        """

    def plan_operate_in_place_resources(
        self,
        *,
        material_uuid: str,
        device_material_uuid: str,
        planned_location: Mapping[str, str] | None = None,
        planned_parent: Callable[[str], str | None] | None = None,
    ) -> tuple[str, ...]:
        """只读投影原位操作的物料、确切库位与设备身份。"""

    def resolve_operate_in_place(
        self,
        *,
        material_uuid: str,
        device_material_uuid: str,
    ) -> OperateInPlaceFacts:
        """证明物料实际占用的库位属于本轮真实执行设备。"""

    def settle_material_transfer(
        self,
        command: MaterialTransferCommand,
    ) -> Mapping[str, Any]:
        """以稳定因果身份幂等结算一次成功物料转移。

        参数：``command`` 包含物料、目标父资源、目标库位与因果身份。返回：库存
        权威持久化后的移动结果。异常：目标身份不完整、结算冲突或数据库故障时
        原样传播，调用方不得自行补写库位占用。
        """

    def settle_material_aliquot(
        self, command: MaterialAliquotCommand
    ) -> Mapping[str, Any]:
        """以完整 Permit 原子结算来源扣减与全部目标内容写入。"""

    def acquire_dispatch_permit(
        self,
        request: DispatchAdmissionRequest,
    ) -> DispatchAdmissionDecision:
        """在库存事务内复验条件并取得完整 Claim/Fence。

        参数：``request`` 是稳定派发效果、最终参数、预期变化与全资源集合。返回：
        成功 Permit 或正常竞争等待结果。异常：运行条件改变时抛稳定
        ``StationResourceError``；合同和数据库故障原样传播。
        """

    def acquire_dispatch_permit_candidates(
        self,
        requests: Sequence[DispatchAdmissionRequest],
    ) -> DispatchAdmissionDecision:
        """在同一库存事务内从有序完整候选中取得首个可用 Claim。"""

    def transition_dispatch_permit(
        self,
        claim_uuid: str,
        *,
        target_state: str,
    ) -> None:
        """推进库存 Claim 的派发、运行、不确定或释放状态。

        参数：``claim_uuid`` 是 Permit 身份；``target_state`` 是目标生命周期状态。
        返回：无。异常：非法转换或数据库故障原样传播。
        """

    def validate_active_dispatch_permit(
        self,
        *,
        effect_uuid: str,
        claim_uuid: str,
        task_uuid: str,
        job_uuid: str,
        attempt: int,
        parameter_hash: str,
        expected_change_set: Mapping[str, Any],
        resource_keys: Sequence[str],
        fences: Mapping[str, int],
    ) -> None:
        """在物理派发前复验库存 Permit 仍是同一活动预留。"""

    def validate_native_operate_custody(
        self,
        *,
        condition: Mapping[str, str] | Sequence[Mapping[str, str]],
        permit: Mapping[str, Any],
        task_uuid: str,
        job_uuid: str,
        attempt: int,
    ) -> None:
        """投影前原子证明原位操作的来源保管和库存凭据。"""

    def release_unprojected_dispatch_permits(
        self,
        *,
        known_claim_uuids: Sequence[str],
    ) -> tuple[str, ...]:
        """释放未在工作流库留下 Claim 投影的 prepared Permit。

        参数：``known_claim_uuids`` 是工作流权威当前仍保存的全部活动 Claim。
        返回：本次释放的库存 Claim UUID。异常：身份或数据库错误原样传播。
        """

    def release_task_dispatch_permits(self, *, task_uuid: str) -> tuple[str, ...]:
        """整组释放已由工作流权威确认的异常终态 Task Permit。"""

    def retain_dispatch_permit_resources(
        self,
        claim_uuid: str,
        *,
        keep_lock_keys: Sequence[str],
        proved_result: bool = False,
    ) -> None:
        """只释放连续交接后不再需要的临时 Claim 资源。

        ``proved_result`` 为真时，明确结果把 uncertain 保留资源收敛回 running。
        """

    def release_preheld_dispatch_claims(
        self,
        *,
        task_uuid: str,
        job_uuids: Sequence[str],
        lock_keys: Sequence[str],
    ) -> tuple[str, ...]:
        """在后继派发已投影后收敛前一 Job 的物理 Claim。"""


class SqliteStationResourceInventory:
    """基于本地库存 SQLite 的工站资源读取与结算适配器。"""

    def check_pick_destination(self, owner_uuid: str, site_uuid: str, material_uuid: str) -> None:
        # 库位占用是当前来源的权威事实；只有同一物料可以取出后归还原位。
        with self._store.read_connection() as connection:
            try:
                check_reserved_pick_site(connection, owner_uuid, site_uuid, material_uuid)
            except TemporaryDispatchCondition as exc:
                raise StationResourceError(exc.code, exc.message, resources=exc.resources) from exc

    def resolve_reservation_site(self, resource_id: str, site_name: str) -> StationSiteTarget:
        """绑定稳定库位身份；占用状态由执行时的库存准入检查。"""
        from .resource_reference import build_inventory_resource_reference_resolver

        owner = build_inventory_resource_reference_resolver(self._store)(resource_id)
        if owner is None:
            raise StationResourceError("site_owner_missing", "库位预留的部署物料不存在")
        rows = self._site_candidates(
            owner_material_uuid=owner["uuid"], site_name=site_name, site_uuid="",
            equivalent_site_uuids=(),
        )
        if len(rows) != 1:
            raise StationResourceError("site_not_found", "库位预留未能定位唯一库位")
        row = rows[0]
        # 地址翻译后库位在载架上：锁和预留按库位实际所属的物料记。
        return StationSiteTarget(str(row["uuid"]), str(row["name"]), str((row["material_uuid"] if "material_uuid" in row.keys() else None) or owner["uuid"]))

    def plan_transfer_resources(
        self, *, owner_uuids: Sequence[str], executor_uuid: str, gripper_role: str,
    ) -> tuple[str, ...]:
        """冻结两端仓库的设备祖先与执行器夹爪，运行可用性仍由 Gate 7 裁决。"""
        keys = {
            device_lock_key(device_uuid)
            for owner in owner_uuids
            if (device_uuid := self._owning_device_uuid(owner))
        }
        if gripper_role:
            rows = self._store.query_all(
                "SELECT uuid, meta_data FROM site WHERE material_uuid = ? "
                "AND deleted_at IS NULL", (executor_uuid,),
            )
            matches = [row for row in rows if _site_role(row.get("meta_data")) == gripper_role]
            if len(matches) != 1:
                raise StationResourceError("gripper_site_role_invalid", "夹爪角色必须唯一")
            keys.add(site_lock_key(executor_uuid, str(matches[0]["uuid"])))
        return tuple(sorted(keys))

    def __init__(
        self,
        store: InventoryStore,
        *,
        settle_material_transfer: Callable[[MaterialTransferCommand], dict[str, Any]],
        correct_inventory: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
        settle_material_aliquot: Callable[[MaterialAliquotCommand], dict[str, Any]]
        | None = None,
    ) -> None:
        """绑定库存存储和既有物料移动写入口。

        参数：``store`` 是本地库存权威的 SQLite 适配器；
        ``settle_material_transfer`` 是只接受完整 Permit 的内部结算入口。返回：
        无。异常：构造不访问数据库；具体查询或结算错误原样传播。
        """

        self._store = store
        self._correct_inventory = correct_inventory
        self._settle_material_transfer = settle_material_transfer
        self._settle_material_aliquot = settle_material_aliquot

    def error_handling_inventory(self) -> dict[str, Any]:
        """返回人工登记所需的真实物料、库位及版本。"""
        return {"instances": self._store.query_all("SELECT i.*,m.name FROM material_instance i JOIN material m ON m.uuid=i.edge_uuid WHERE m.deleted_at IS NULL"),
                "sites": self._store.query_all("SELECT uuid,material_uuid,name,occupied_material_uuid,update_time FROM site WHERE deleted_at IS NULL")}

    def correct_inventory(self, command_uuid: str, request: dict[str, Any]) -> dict[str, Any]:
        if self._correct_inventory is None:
            raise StationResourceError("inventory_correction_unavailable", "库存人工登记入口未装配")
        return self._correct_inventory(command_uuid, request)

    def release_error_occupancies(self, occupancies: Sequence[Mapping[str, Any]]) -> None:
        """按已冻结的 Claim、键和 Fence 释放指定占用；不推断物理停止。"""
        from unilabos.workflow.store import utc_now
        with self._store.transaction() as conn:
            now = utc_now()
            for item in occupancies:
                if item.get("occupancy_kind") == "device_tenancy":
                    continue
                row = conn.execute("SELECT * FROM station_execution_lock_lease WHERE claim_uuid=? AND lock_key=?", (item["claim_uuid"], item["lock_key"])).fetchone()
                if row is None or int(row["fencing_token"]) != int(item["fencing_token"]):
                    raise StationResourceError("occupancy_version_conflict", "库存占用身份已变化")
                # 同一任务词法共享范围共用 Fence；工作流投影只显示一个逻辑占用，
                # 库存的每个参与 Claim 都要释放该同代键，不能遗留隐形阻塞者。
                shared = conn.execute("""SELECT l.claim_uuid FROM station_execution_lock_lease l
                    JOIN station_execution_claim c ON c.claim_uuid=l.claim_uuid
                    WHERE c.task_uuid=(SELECT task_uuid FROM station_execution_claim WHERE claim_uuid=?) AND l.lock_key=? AND l.fencing_token=?""",
                    (item["claim_uuid"],item["lock_key"],item["fencing_token"])).fetchall()
                for holder in shared:
                    conn.execute("UPDATE station_execution_lock_lease SET state='released',released_at=?,update_time=? WHERE claim_uuid=? AND lock_key=?", (now,now,holder["claim_uuid"],item["lock_key"]))
                    remaining = conn.execute("SELECT 1 FROM station_execution_lock_lease WHERE claim_uuid=? AND state!='released' LIMIT 1", (holder["claim_uuid"],)).fetchone()
                    if remaining is None:
                        conn.execute("UPDATE station_execution_claim SET state='released',released_at=?,update_time=? WHERE claim_uuid=?", (now,now,holder["claim_uuid"]))

    def describe_wait_resources(
        self,
        resources: Sequence[Mapping[str, str]],
    ) -> tuple[dict[str, str], ...]:
        """补齐等待资源的人类可读名称。

        参数：``resources`` 是调度器已经确认的设备、物料或库位稳定身份。返回：
        保持输入顺序和原字段的资源描述；库存中仍存在的物料与库位分别补充
        ``material_name``、``site_name``。异常：SQLite 读取错误原样传播，名称
        缺失时保留稳定 UUID，绝不伪造展示值。
        """

        described: list[dict[str, str]] = []
        for raw_resource in resources:
            resource = dict(raw_resource)
            scope = str(resource.get("scope") or "").strip()
            device_id = str(resource.get("device_id") or "").strip()
            if scope == "device" and device_id and not str(
                resource.get("device_name") or ""
            ).strip():
                device = self._store.query_one(
                    "SELECT name FROM material "
                    "WHERE uuid=? AND deleted_at IS NULL",
                    (device_id,),
                )
                if device is not None and str(device.get("name") or "").strip():
                    resource["device_name"] = str(device["name"]).strip()
            material_uuid = str(resource.get("material_uuid") or "").strip()
            if material_uuid and not str(resource.get("material_name") or "").strip():
                material = self._store.query_one(
                    "SELECT name FROM material "
                    "WHERE uuid=? AND deleted_at IS NULL",
                    (material_uuid,),
                )
                if material is not None and str(material.get("name") or "").strip():
                    resource["material_name"] = str(material["name"]).strip()
            site_uuid = str(resource.get("site_uuid") or "").strip()
            if site_uuid and not str(resource.get("site_name") or "").strip():
                site = self._store.query_one(
                    "SELECT name FROM site WHERE uuid=? AND deleted_at IS NULL",
                    (site_uuid,),
                )
                if site is not None and str(site.get("name") or "").strip():
                    resource["site_name"] = str(site["name"]).strip()
            described.append(resource)
        return tuple(described)

    def is_device_material(
        self,
        material_uuid: str,
        *,
        resource_template_uuid: str,
    ) -> bool:
        """判断具体物料是否为指定模板的设备实例。

        参数：``material_uuid`` 是设备候选物料身份；
        ``resource_template_uuid`` 是冻结执行计划要求的设备模板身份。返回：仅当
        活跃物料同时匹配模板且类型为 ``device`` 时为真。异常：SQLite 读取错误
        原样传播，调用方不得把读取失败解释成匹配。
        """

        material = self._store.query_one(
            "SELECT resource_template_uuid, type FROM material "
            "WHERE uuid = ? AND deleted_at IS NULL",
            (material_uuid,),
        )
        return bool(
            material is not None
            and str(material.get("resource_template_uuid") or "")
            == resource_template_uuid
            and str(material.get("type") or "") == "device"
        )

    def resolve_named_site_group(
        self,
        *,
        owner_material_uuid: str,
        group_key: str,
    ) -> Mapping[str, Any]:
        """按父物料和命名组键返回有序库位 UUID，不看占用。

        参数：``owner_material_uuid`` 是运行时已经选出的父物料；``group_key`` 是
        作者写下的库位组。返回：库存权威的有序 ``site_uuids`` 与指纹。异常：组不
        存在或父物料非法时抛 ``StationResourceError``。
        """

        from unilabos.app.scheduler.inventory.site_selection import (
            InventorySiteSelectionError,
            build_inventory_site_selection_resolver,
        )

        try:
            return build_inventory_site_selection_resolver(self._store)(
                {
                    "version": 1,
                    "owner_material_uuid": owner_material_uuid,
                    "group_key": group_key,
                    "strategy": "sort_order",
                }
            )
        except InventorySiteSelectionError as error:
            raise StationResourceError("site_group_unresolved", str(error)) from error

    def site_uuid_for_address(self, owner_material_uuid: str, name: str) -> str | None:
        """``name`` 是指向 ``owner_material_uuid``（载架）上某个库位的上层物料地址时，返回该库位 UUID。

        工作流参数里的库位名保留作者写的地址（台面位号、料架层号），所属父物料已按地址表换成载架；
        核对“声明库位 = 实际库位”时按这个反查认可地址写法。不是地址或不唯一时返回 ``None``。
        """

        from unilabos.app.scheduler.inventory.site_address import addresses_on_owner

        address = addresses_on_owner(self._store, owner_material_uuid, name)
        return str(address.site_uuid) if address is not None else None

    def resolve_target_site(self, request: TargetSiteRequest) -> StationSiteTarget:
        """按稳定身份、显式等价组和当前占用解析目标库位（Site）。

        参数：``request`` 同时携带库位拥有者、单一选择器或等价组、待放物料和
        本轮已不可用库位。返回：按 ``sort_order`` 选出的规范库位身份与名称。
        异常：选择器、归属、占用或模板约束无法满足时抛
        ``StationResourceError``；SQLite 错误原样传播并关闭式阻止派发。
        """

        owner = str(request.owner_material_uuid or "").strip()
        requested_uuid = str(request.site_uuid or "").strip()
        requested_name = str(request.site_name or "").strip()
        requested_group = tuple(
            str(item or "").strip() for item in request.equivalent_site_uuids
        )
        occupant = str(request.occupant_material_uuid or "").strip()
        unavailable = {
            str(item or "").strip() for item in request.unavailable_site_uuids
        }
        if not owner:
            raise StationResourceError(
                "site_owner_missing",
                "目标库位缺少所属父物料 mount_resource.uuid",
            )
        if not requested_uuid and not requested_name and not requested_group:
            raise StationResourceError(
                "site_selector_missing",
                "目标库位必须提供 site_uuid、site 名称或显式等价库位组",
            )
        if requested_group and (requested_uuid or requested_name):
            raise StationResourceError(
                "site_selector_conflict",
                "显式库位与等价库位组不能同时提供",
            )
        if occupant and owner == occupant:
            raise StationResourceError(
                "site_self_mount",
                "待转移物料不能挂载到自身库位",
            )

        candidate_rows = self._site_candidates(
            owner_material_uuid=owner,
            site_uuid=requested_uuid,
            site_name=requested_name,
            equivalent_site_uuids=requested_group,
        )
        if not candidate_rows and requested_name and not requested_uuid and not requested_group:
            # 名字是父物料的库位地址（台面位号、料架层号）：物理库位在载架上，所属父物料换成载架。
            from unilabos.app.scheduler.inventory.site_address import resolve_site_address

            address = resolve_site_address(self._store, owner, requested_name)
            if address is None:
                # 所属父物料已经是载架、库位名还是地址：找指向这个载架的同名地址。
                from unilabos.app.scheduler.inventory.site_address import addresses_on_owner

                address = addresses_on_owner(self._store, owner, requested_name)
            if address is not None:
                owner = str(address.owner_material_uuid)
                candidate_rows = self._site_candidates(
                    owner_material_uuid=owner,
                    site_uuid=str(address.site_uuid),
                    site_name="",
                    equivalent_site_uuids=(),
                )
        if not candidate_rows:
            selector = (
                "等价库位组"
                if requested_group
                else (
                    f"UUID {requested_uuid}"
                    if requested_uuid
                    else f"名称 {requested_name}"
                )
            )
            raise StationResourceError(
                "site_not_found",
                f"目标父物料 {owner} 下找不到库位（{selector}）",
            )
        for candidate in candidate_rows:
            if str(candidate["material_uuid"]) != owner:
                raise StationResourceError(
                    "site_owner_mismatch",
                    f"库位 {candidate['uuid']} 不属于目标父物料 {owner}",
                )

        first_candidate = candidate_rows[0]
        canonical_name = str(first_candidate["name"])
        if (
            requested_uuid
            and requested_name
            and canonical_name.casefold() != requested_name.casefold()
        ):
            raise StationResourceError(
                "site_identity_mismatch",
                f"site_uuid {first_candidate['uuid']} 对应名称 {canonical_name}，"
                f"与入参 {requested_name} 不一致",
            )

        occupant_template_uuid = self._material_template_uuid(occupant)
        active_ingress_sites = (
            self._active_ingress_site_uuids(
                tuple(str(candidate["uuid"]) for candidate in candidate_rows)
            )
            if request.require_available
            else set()
        )
        selected: dict[str, Any] | None = None
        for candidate in candidate_rows:
            candidate_uuid = str(candidate["uuid"])
            if request.require_available and (
                candidate_uuid in unavailable or candidate_uuid in active_ingress_sites
            ):
                continue
            occupied_by = str(candidate.get("occupied_material_uuid") or "").strip()
            if request.require_available and occupied_by and occupied_by != occupant:
                continue
            allowed_templates = _allowed_template_uuids(
                candidate.get("allowed_resource_template_uuids"),
                site_name=str(candidate["name"]),
            )
            if allowed_templates and occupant_template_uuid not in allowed_templates:
                continue
            selected = candidate
            break
        if selected is None:
            self._raise_unavailable_site(
                candidates=candidate_rows,
                requested_group=requested_group,
                unavailable_site_uuids=unavailable,
                active_ingress_site_uuids=active_ingress_sites,
                occupant_material_uuid=occupant,
            )
        assert selected is not None
        return StationSiteTarget(
            uuid=str(selected["uuid"]),
            name=str(selected["name"]),
            owner_material_uuid=owner,
        )

    def resolve_transfer_resources(
        self,
        request: TransferResourceRequest,
    ) -> TransferResourceFacts:
        """解析转运来源、两端设备祖先和空夹爪库位。

        参数：``request`` 提供待搬物料、已选目标库位、机械臂身份与夹爪角色。
        返回：调度器构造完整作业执行占用（JobExecutionClaim）所需的稳定库存
        事实。异常：来源、父链、设备祖先或夹爪条件无法证明时抛
        ``StationResourceError``，绝不返回部分资源集合。
        """

        source = self._store.query_one(
            "SELECT uuid, name, material_uuid FROM site "
            "WHERE occupied_material_uuid = ? AND deleted_at IS NULL "
            "ORDER BY sort_order ASC, create_time ASC, uuid ASC LIMIT 1",
            (request.resource_material_uuid,),
        )
        if source is None:
            raise StationResourceError(
                "transfer_source_site_missing",
                "被搬运物料没有可证明的来源库位",
                resources=(
                    {
                        "scope": "material",
                        "material_uuid": request.resource_material_uuid,
                    },
                ),
            )
        source_site_uuid = str(source["uuid"])
        source_owner_uuid = str(source["material_uuid"])
        source_device_uuid = self._owning_device_uuid(source_owner_uuid)
        target_device_uuid = self._owning_device_uuid(request.target_owner_material_uuid)
        if request.require_device_owners and (not source_device_uuid or not target_device_uuid):
            raise StationResourceError(
                "transfer_device_owner_missing",
                "机械臂转运的来源库位和目标库位必须能追溯到设备",
            )
        gripper_site_uuid = ""
        gripper_role = str(request.gripper_site_role or "").strip()
        executor_uuid = str(request.executor_material_uuid or "").strip()
        if gripper_role:
            if not executor_uuid:
                raise StationResourceError(
                    "transfer_executor_missing",
                    "机械臂转运缺少实际执行器物料 UUID",
                )
            gripper_site_uuid = self._empty_role_site_uuid(
                owner_material_uuid=executor_uuid,
                role=gripper_role,
                allowed_material_uuid=(
                    request.resource_material_uuid
                    if request.allow_held_material and source_owner_uuid == executor_uuid
                    else ""
                ),
            )
        return TransferResourceFacts(
            source_site_uuid=source_site_uuid,
            source_site_name=str(source["name"]),
            source_owner_material_uuid=source_owner_uuid,
            source_device_material_uuid=source_device_uuid,
            target_device_material_uuid=target_device_uuid,
            gripper_site_uuid=gripper_site_uuid,
        )

    def plan_operate_in_place_resources(
        self,
        *,
        material_uuid: str,
        device_material_uuid: str,
        planned_location: Mapping[str, str] | None = None,
        planned_parent: Callable[[str], str | None] | None = None,
    ) -> tuple[str, ...]:
        """用已证明的因果位置冻结原位资源；没有先前搬运时保留真实占位。"""
        if planned_location is not None:
            target = self.resolve_target_site(
                TargetSiteRequest(
                    occupant_material_uuid=material_uuid,
                    require_available=False,
                    owner_material_uuid=planned_location.get("owner_material_uuid", ""),
                    site_uuid=planned_location.get("site_uuid", ""),
                    site_name=planned_location.get("site_name", ""),
                )
            )
            owner, site = target.owner_material_uuid, target.uuid
        else:
            row = self._store.query_one(
                "SELECT uuid,material_uuid FROM site WHERE occupied_material_uuid=? AND deleted_at IS NULL",
                (material_uuid,),
            )
            if row is None:
                raise StationResourceError("operate_in_place_site_missing", "原位操作物料缺少已证明的库位")
            owner, site = str(row["material_uuid"]), str(row["uuid"])
        device = self._owning_device_uuid(owner, planned_parent=planned_parent)
        if not device or device != device_material_uuid:
            raise StationResourceError("operate_in_place_device_mismatch", "原位操作规划库位不属于实际执行设备")
        return tuple(sorted({material_lock_key(material_uuid), site_lock_key(owner, site), device_lock_key(device)}))

    def resolve_operate_in_place(
        self,
        *,
        material_uuid: str,
        device_material_uuid: str,
    ) -> OperateInPlaceFacts:
        """沿库位拥有者父链证明物料仍在本轮实际执行设备中。"""

        material = str(material_uuid or "").strip()
        device = str(device_material_uuid or "").strip()
        if not material or not device:
            raise StationResourceError(
                "operate_in_place_identity_missing",
                "原位操作缺少物料或实际执行设备身份",
            )
        row = self._store.query_one(
            "SELECT uuid,material_uuid FROM site WHERE occupied_material_uuid=? "
            "AND deleted_at IS NULL",
            (material,),
        )
        if row is None:
            raise StationResourceError(
                "operate_in_place_site_missing",
                "待操作物料当前未占用任何设备库位",
                resources=({"scope": "material", "material_uuid": material},),
            )
        owner = str(row["material_uuid"])
        actual_device = self._owning_device_uuid(owner)
        if actual_device != device:
            raise StationResourceError(
                "operate_in_place_device_mismatch",
                "待操作物料不在本轮实际执行设备内",
                resources=(
                    {
                        "scope": "material_site",
                        "material_uuid": owner,
                        "site_uuid": str(row["uuid"]),
                    },
                ),
            )
        return OperateInPlaceFacts(
            material_uuid=material,
            site_uuid=str(row["uuid"]),
            site_owner_material_uuid=owner,
            device_material_uuid=device,
        )

    def settle_material_transfer(
        self,
        command: MaterialTransferCommand,
    ) -> Mapping[str, Any]:
        """把完整 PhysicalSettlement 命令交给库存单事务写模型。

        参数：``command`` 提供物料、目标父级、目标库位和稳定作业因果身份。
        返回：库存写模型提交后的物料快照。异常：目标库位不再可用时抛
        ``StationResourceError``；库存版本或物料冲突由写模型原样传播。
        """

        return self._settle_material_transfer(command)

    def settle_material_aliquot(
        self, command: MaterialAliquotCommand
    ) -> Mapping[str, Any]:
        """把完整分装 PhysicalSettlement 交给库存单事务写模型。"""

        if self._settle_material_aliquot is None:
            raise StationResourceError(
                "aliquot_settlement_unavailable", "库存权威未装配分装结算入口"
            )
        return self._settle_material_aliquot(command)

    def acquire_dispatch_permit(
        self,
        request: DispatchAdmissionRequest,
    ) -> DispatchAdmissionDecision:
        """在库存单写事务内完成条件复验与完整资源 Claim/Fence。

        参数：``request`` 是调度器冻结的派发效果和全资源请求。返回：成功 Permit
        或资源竞争等待结果。异常：可变化条件转为 ``StationResourceError``；合同
        冲突与 SQLite 故障原样传播，整个事务自动回滚。
        """

        try:
            with self._store.transaction() as connection:
                return acquire_dispatch_permit(connection, request)
        except TemporaryDispatchCondition as error:
            raise StationResourceError(
                error.code,
                error.message,
                resources=error.resources,
            ) from error

    def acquire_dispatch_permit_candidates(
        self,
        requests: Sequence[DispatchAdmissionRequest],
    ) -> DispatchAdmissionDecision:
        """在一个 ``BEGIN IMMEDIATE`` 事务内完成候选回退和整组 Claim。

        参数：``requests`` 是同一作业按冻结组顺序形成的完整派发候选。返回：
        首个成功 Permit 及其索引，或稳定等待。异常：所有候选物理条件不满足时
        转为 ``StationResourceError``；合同/数据库故障原样传播且整体回滚。
        """

        try:
            with self._store.transaction() as connection:
                return acquire_dispatch_permit_candidates(connection, requests)
        except TemporaryDispatchCondition as error:
            raise StationResourceError(
                error.code,
                error.message,
                resources=error.resources,
            ) from error

    def transition_dispatch_permit(
        self,
        claim_uuid: str,
        *,
        target_state: str,
    ) -> None:
        """在库存事务内幂等推进 Claim 与全部 Lease。

        参数：``claim_uuid`` 是库存签发身份；``target_state`` 是 reserved、
        running、uncertain 或 released。返回：无。异常：非法转换与 SQLite 故障
        原样传播，调用方不得只修改工作流库投影。
        """

        with self._store.transaction() as connection:
            transition_dispatch_permit(
                connection,
                claim_uuid=claim_uuid,
                target_state=target_state,
            )

    def validate_active_dispatch_permit(
        self,
        *,
        effect_uuid: str,
        claim_uuid: str,
        task_uuid: str,
        job_uuid: str,
        attempt: int,
        parameter_hash: str,
        expected_change_set: Mapping[str, Any],
        resource_keys: Sequence[str],
        fences: Mapping[str, int],
    ) -> None:
        """在库存事务内关闭式复验物理派发 Permit。"""

        with self._store.transaction() as connection:
            validate_active_dispatch_permit(
                connection,
                effect_uuid=effect_uuid,
                claim_uuid=claim_uuid,
                task_uuid=task_uuid,
                job_uuid=job_uuid,
                attempt=attempt,
                parameter_hash=parameter_hash,
                expected_change_set=expected_change_set,
                resource_keys=resource_keys,
                fences=fences,
            )

    def validate_native_operate_custody(
        self,
        *,
        condition: Mapping[str, str] | Sequence[Mapping[str, str]],
        permit: Mapping[str, Any],
        task_uuid: str,
        job_uuid: str,
        attempt: int,
    ) -> None:
        """投影前在同一库存事务证明来源保管、实际位置及完整活跃凭据。"""
        from unilabos.app.scheduler.inventory.custody import material_subtree
        from unilabos.app.scheduler.inventory.dispatch_admission import (
            DispatchAdmissionConflict,
            OperateInPlaceCondition,
            _validate_live_dispatch_permit,
            _validate_operate_in_place_facts,
        )

        with self._store.transaction() as connection:
            _validate_live_dispatch_permit(
                connection,
                task_uuid=task_uuid,
                job_uuid=job_uuid,
                attempt=attempt,
                effect_uuid=permit["effect_uuid"],
                claim_uuid=permit["claim_uuid"],
                parameter_hash=permit["parameter_hash"],
                expected_change_set=permit["expected_change_set"],
                resource_keys=tuple(permit["fencing_tokens"]),
                fences=permit["fencing_tokens"],
                prepared=True,
            )
            raw_conditions = [condition] if isinstance(condition, Mapping) else condition
            if not isinstance(raw_conditions, (list, tuple)) or not raw_conditions:
                raise DispatchAdmissionConflict("原位操作保管条件集合无效")
            try:
                all_facts = tuple(OperateInPlaceCondition(**item) for item in raw_conditions)
            except (TypeError, ValueError) as error:
                raise DispatchAdmissionConflict("原位操作保管条件字段无效") from error
            rows = connection.execute(
                "SELECT b.material_uuid,b.custody_policy,r.amounts_json FROM inventory_material_source_binding b "
                "JOIN inventory_reservation r ON r.workflow_id=b.workflow_id AND r.node_id=b.node_id "
                "AND r.attempt=b.attempt WHERE b.workflow_id=? AND b.status='active' AND r.status='active'",
                (task_uuid,),
            ).fetchall()
            owned = set()
            for row in rows:
                original = (
                    json.loads(row["amounts_json"]).get("custody_material_uuids", [])
                    if row["custody_policy"] == "task_exclusive"
                    else []
                )
                if not isinstance(original, list) or any(not isinstance(value, str) or not value for value in original):
                    raise DispatchAdmissionConflict("原任务保管范围损坏，必须核实")
                owned.update(material_subtree(connection, row["material_uuid"]))
                owned.update(original)
            if any(facts.material_uuid not in owned for facts in all_facts):
                raise DispatchAdmissionConflict("原位操作物料缺少本 Task 的活动来源保管")
            _validate_operate_in_place_facts(
                connection,
                all_facts,
                set(permit["fencing_tokens"]),
                permit["expected_change_set"],
            )

    def release_unprojected_dispatch_permits(
        self,
        *,
        known_claim_uuids: Sequence[str],
    ) -> tuple[str, ...]:
        """在库存事务内回收未被工作流库投影的 prepared Permit。

        参数：工作流库仍保存的活动 Claim 身份。返回：本次释放身份。异常：身份
        或 SQLite 错误原样传播，已越过 prepared 的 Claim 不受影响。
        """

        with self._store.transaction() as connection:
            return release_unprojected_dispatch_permits(
                connection,
                known_claim_uuids=known_claim_uuids,
            )

    def release_task_dispatch_permits(self, *, task_uuid: str) -> tuple[str, ...]:
        """在单个库存事务内整组释放终态 Task 的活动 Permit。"""

        with self._store.transaction() as connection:
            return release_task_dispatch_permits(
                connection,
                task_uuid=task_uuid,
            )

    def retain_dispatch_permit_resources(
        self,
        claim_uuid: str,
        *,
        keep_lock_keys: Sequence[str],
        proved_result: bool = False,
    ) -> None:
        """在库存事务中保留连续区间资源并释放其余资源。"""

        with self._store.transaction() as connection:
            retain_dispatch_permit_resources(
                connection,
                claim_uuid=claim_uuid,
                keep_lock_keys=keep_lock_keys,
                proved_result=proved_result,
            )

    def release_preheld_dispatch_claims(
        self,
        *,
        task_uuid: str,
        job_uuids: Sequence[str],
        lock_keys: Sequence[str],
    ) -> tuple[str, ...]:
        """在库存事务中收敛前一 Job 的 Claim。"""

        with self._store.transaction() as connection:
            return release_preheld_dispatch_claims(
                connection,
                task_uuid=task_uuid,
                job_uuids=job_uuids,
                lock_keys=lock_keys,
            )

    def _site_candidates(
        self,
        *,
        owner_material_uuid: str,
        site_uuid: str,
        site_name: str,
        equivalent_site_uuids: Sequence[str],
    ) -> list[dict[str, Any]]:
        """读取单一选择器或等价组对应的规范库位行。

        参数：父物料、精确 UUID、兼容名称和等价组已经过互斥校验。返回：按
        ``sort_order`` 排序的候选行。异常：UUID 非法、重复或引用缺失时抛
        ``StationResourceError``；SQLite 错误原样传播。
        """

        if equivalent_site_uuids:
            try:
                canonical_group = tuple(
                    str(uuid.UUID(item)) for item in equivalent_site_uuids
                )
            except (AttributeError, TypeError, ValueError) as error:
                raise StationResourceError(
                    "invalid_site_group",
                    "等价库位组包含非法 UUID",
                ) from error
            if len(set(canonical_group)) != len(canonical_group):
                raise StationResourceError(
                    "invalid_site_group",
                    "等价库位组包含重复 UUID",
                )
            placeholders = ",".join("?" for _ in canonical_group)
            rows = self._store.query_all(
                "SELECT uuid, material_uuid, name, sort_order, "
                "occupied_material_uuid, allowed_resource_template_uuids "
                f"FROM site WHERE uuid IN ({placeholders}) "
                "AND deleted_at IS NULL "
                "ORDER BY sort_order ASC, create_time ASC, uuid ASC",
                canonical_group,
            )
            found = {str(row["uuid"]) for row in rows}
            missing = sorted(set(canonical_group) - found)
            if missing:
                raise StationResourceError(
                    "site_not_found",
                    "等价库位组引用不存在的库位：" + ",".join(missing),
                )
            return rows
        if site_uuid:
            try:
                canonical_uuid = str(uuid.UUID(site_uuid))
            except (AttributeError, TypeError, ValueError) as error:
                raise StationResourceError(
                    "invalid_site_uuid",
                    "site_uuid 不是合法 UUID",
                ) from error
            row = self._store.query_one(
                "SELECT uuid, material_uuid, name, occupied_material_uuid, "
                "allowed_resource_template_uuids "
                "FROM site WHERE uuid=? AND deleted_at IS NULL",
                (canonical_uuid,),
            )
            return [row] if row is not None else []
        rows = self._store.query_all(
            "SELECT uuid, material_uuid, name, occupied_material_uuid, "
            "allowed_resource_template_uuids "
            "FROM site WHERE material_uuid=? AND LOWER(name)=LOWER(?) "
            "AND deleted_at IS NULL",
            (owner_material_uuid, site_name),
        )
        if not rows and site_name:
            # 库位名是作者写的上层物料地址（厂家位号）：父物料就是地址的拥有者（台面、堆栈）时按它的地址表翻译；
            # 父物料已换成载架时按“哪个上层物料的同名地址指向这个载架”反查。
            from unilabos.app.scheduler.inventory.site_address import addresses_on_owner, resolve_site_address

            address = resolve_site_address(self._store, owner_material_uuid, site_name)
            if address is None:
                address = addresses_on_owner(self._store, owner_material_uuid, site_name)
            if address is not None:
                row = self._store.query_one(
                    "SELECT uuid, material_uuid, name, occupied_material_uuid, "
                    "allowed_resource_template_uuids "
                    "FROM site WHERE uuid=? AND deleted_at IS NULL",
                    (str(address.site_uuid),),
                )
                rows = [row] if row is not None else []
        if len(rows) > 1:
            raise StationResourceError(
                "site_name_ambiguous",
                f"父物料 {owner_material_uuid} 下库位名称 {site_name} 不唯一",
            )
        return rows

    def _material_template_uuid(self, material_uuid: str) -> str:
        """读取具体物料的资源模板 UUID。

        参数：``material_uuid`` 是准备放入目标库位的物料身份。返回：活跃物料的
        模板 UUID；空身份或不存在时返回空字符串，由库位模板约束失败关闭。
        异常：SQLite 读取错误原样传播。
        """

        if not material_uuid:
            return ""
        row = self._store.query_one(
            "SELECT resource_template_uuid FROM material "
            "WHERE uuid=? AND deleted_at IS NULL",
            (material_uuid,),
        )
        return str((row or {}).get("resource_template_uuid") or "")

    def _active_ingress_site_uuids(
        self,
        site_uuids: Sequence[str],
    ) -> set[str]:
        """批量读取仍由 AGV 入口预留占用的候选库位。

        参数：``site_uuids`` 是本轮目标选择已经验证存在的候选 UUID。返回：活动
        入口预留绑定的库位集合；空候选返回空集合。异常：SQLite 读取错误原样
        传播，调度器必须失败关闭，不能让普通节点抢占运输目的地。
        """

        if not site_uuids:
            return set()
        placeholders = ",".join("?" for _ in site_uuids)
        rows = self._store.query_all(
            "SELECT site_uuid FROM station_ingress_reservation "
            f"WHERE site_active=1 AND site_uuid IN ({placeholders})",
            tuple(site_uuids),
        )
        return {str(row["site_uuid"]) for row in rows}

    def _owning_device_uuid(
        self,
        material_uuid: str,
        *,
        planned_parent: Callable[[str], str | None] | None = None,
    ) -> str:
        """沿物料父链查找最近设备祖先。

        参数：``material_uuid`` 是库位直接拥有者。返回：最近设备物料 UUID；没有
        设备祖先时返回空字符串。异常：父链循环时抛 ``StationResourceError``，
        防止损坏资源树绕过设备互斥。
        """

        current = str(material_uuid or "").strip()
        visited: set[str] = set()
        while current:
            if current in visited:
                raise StationResourceError(
                    "material_parent_cycle",
                    "库存物料父链存在循环",
                )
            visited.add(current)
            row = self._store.query_one(
                "SELECT type, parent_uuid FROM material "
                "WHERE uuid = ? AND deleted_at IS NULL",
                (current,),
            )
            if row is None:
                return ""
            if str(row.get("type") or "") == "device":
                return current
            # 规划投影只覆盖已证明的父关系；运行期不传解析器，始终读取真实库存。
            projected = planned_parent(current) if planned_parent is not None else None
            current = str(projected if projected is not None else row.get("parent_uuid") or "").strip()
        return ""

    def _empty_role_site_uuid(
        self,
        *,
        owner_material_uuid: str,
        role: str,
        allowed_material_uuid: str = "",
    ) -> str:
        """选择执行器下唯一且空闲的具名角色库位。

        参数：``owner_material_uuid`` 是机械臂物料 UUID；``role`` 是动作资源合同
        声明的夹爪库位角色。返回：匹配库位的稳定 UUID。异常：角色重复、缺失、
        元数据损坏或位置有物料时抛 ``StationResourceError``。
        """

        rows = self._store.query_all(
            "SELECT uuid, meta_data, occupied_material_uuid FROM site "
            "WHERE material_uuid = ? AND deleted_at IS NULL "
            "ORDER BY sort_order ASC, create_time ASC, uuid ASC",
            (owner_material_uuid,),
        )
        matches = [row for row in rows if _site_role(row.get("meta_data")) == role]
        if len(matches) != 1:
            raise StationResourceError(
                "gripper_site_role_invalid",
                f"机械臂夹爪库位角色 {role} 必须且只能配置一个，当前为 {len(matches)} 个",
            )
        occupied = str(matches[0].get("occupied_material_uuid") or "").strip()
        if occupied and occupied != allowed_material_uuid:
            raise StationResourceError(
                "gripper_site_occupied",
                f"机械臂夹爪库位已有物料 {occupied}，不能开始新的转运",
                resources=(
                    {
                        "scope": "material_site",
                        "material_uuid": owner_material_uuid,
                        "site_uuid": str(matches[0]["uuid"]),
                    },
                ),
            )
        return str(matches[0]["uuid"])

    @staticmethod
    def _raise_unavailable_site(
        *,
        candidates: Sequence[Mapping[str, Any]],
        requested_group: Sequence[str],
        unavailable_site_uuids: set[str],
        active_ingress_site_uuids: set[str],
        occupant_material_uuid: str,
    ) -> None:
        """把候选不可用原因收敛为稳定调度错误。

        参数：实际候选、是否为等价组、本轮已申领集合和待放物料身份用于判定
        最具体原因。返回：无。异常：始终抛 ``StationResourceError``，并携带
        库存权威确认的实际候选库位身份。
        """

        candidate = candidates[0]
        resources = tuple(
            {
                "scope": "material_site",
                "material_uuid": str(item["material_uuid"]),
                "site_uuid": str(item["uuid"]),
            }
            for item in candidates
        )
        if requested_group:
            raise StationResourceError(
                "site_group_unavailable",
                "等价库位组当前没有可接收目标物料的位置",
                resources=resources,
            )
        site_uuid = str(candidate["uuid"])
        site_name = str(candidate["name"])
        if site_uuid in active_ingress_site_uuids:
            raise StationResourceError(
                "site_ingress_reserved",
                f"目标库位 {site_name} 已为运输中的入口载体预留",
                resources=resources,
            )
        if site_uuid in unavailable_site_uuids:
            raise StationResourceError(
                "site_claimed",
                f"目标库位 {site_name} 已由其他作业申领",
                resources=resources,
            )
        occupied_by = str(candidate.get("occupied_material_uuid") or "").strip()
        if occupied_by and occupied_by != occupant_material_uuid:
            raise StationResourceError(
                "site_occupied",
                f"目标库位 {site_name} 已被物料 {occupied_by} 占用",
                resources=resources,
            )
        raise StationResourceError(
            "site_template_not_allowed",
            f"物料 {occupant_material_uuid} 的模板不允许放入目标库位 {site_name}",
            resources=resources,
        )


def _allowed_template_uuids(raw_value: Any, *, site_name: str) -> set[str]:
    """解码库位允许物料模板集合。

    参数：``raw_value`` 是 SQLite JSON 文本或已解码序列；``site_name`` 用于诊断。
    返回：去空白后的模板 UUID 集合。异常：值不是合法 JSON 数组时抛
    ``StationResourceError``，禁止把损坏约束解释为无限制。
    """

    try:
        decoded = (
            raw_value
            if isinstance(raw_value, (list, tuple))
            else json.loads(str(raw_value or "[]"))
        )
    except (TypeError, ValueError) as error:
        raise StationResourceError(
            "invalid_site_template_constraint",
            f"目标库位 {site_name} 的允许物料类型配置无效",
        ) from error
    if not isinstance(decoded, (list, tuple)):
        raise StationResourceError(
            "invalid_site_template_constraint",
            f"目标库位 {site_name} 的允许物料类型配置无效",
        )
    return {str(item) for item in decoded if str(item).strip()}


def _site_role(raw_metadata: Any) -> str:
    """读取库位部署元数据中的规范资源角色。

    参数：``raw_metadata`` 是 SQLite JSON 文本或已解码对象。返回：
    ``meta_data.unilab.resource_role`` 文本，缺省为空。异常：JSON 损坏时抛
    ``StationResourceError``，不把损坏元数据解释为无角色。
    """

    try:
        metadata = (
            raw_metadata
            if isinstance(raw_metadata, dict)
            else json.loads(str(raw_metadata or "{}"))
        )
    except (TypeError, ValueError) as error:
        raise StationResourceError(
            "invalid_site_metadata",
            "库位 meta_data 不是合法 JSON",
        ) from error
    unilab = metadata.get("unilab") if isinstance(metadata, dict) else None
    if not isinstance(unilab, dict):
        return ""
    return str(unilab.get("resource_role") or "").strip()


__all__ = [
    "AliquotReceipt",
    "MaterialAliquotCommand",
    "MaterialTransferCommand",
    "SqliteStationResourceInventory",
    "StationResourceError",
    "StationResourceInventory",
    "StationSiteTarget",
    "TargetSiteRequest",
    "TransferResourceFacts",
    "TransferResourceRequest",
]
