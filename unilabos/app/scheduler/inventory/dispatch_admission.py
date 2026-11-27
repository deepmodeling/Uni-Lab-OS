"""库存权威中的原子作业派发准入、Claim 与 Fence。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from unilabos.app.scheduler.resource_lock import (
    canonical_resource_lock_scope,
    conflicting_resource_lock_keys,
    device_lock_key,
    material_lock_key,
    site_lock_key,
)
from unilabos.workflow.resource_lock_key import parse_canonical_resource_lock_key

_SCOPES = frozenset({"device", "material", "material_site", "resource"})


@dataclass(frozen=True, slots=True)
class DispatchResource:
    """派发前必须全有或全无取得的一个库存资源。"""

    lock_key: str
    scope: str
    material_uuid: str = ""
    site_uuid: str = ""


@dataclass(frozen=True, slots=True)
class TransferDispatchCondition:
    """机械臂转运在取得 Claim 的同一事务内必须成立的物理条件。"""

    material_uuid: str
    source_owner_material_uuid: str
    source_site_uuid: str
    target_owner_material_uuid: str
    target_site_uuid: str
    executor_material_uuid: str
    gripper_site_uuid: str
    allow_held_material: bool = False


@dataclass(frozen=True, slots=True)
class OperateInPlaceCondition:
    """原位操作在 Claim 事务内必须保持的物料、库位与执行设备事实。"""

    material_uuid: str
    site_owner_material_uuid: str
    site_uuid: str
    device_material_uuid: str


@dataclass(frozen=True, slots=True)
class AliquotDispatchCondition:
    """分装派发前必须共同锁定的来源与全部目标容器。"""

    source_material_uuid: str
    target_material_uuids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class InventoryRelocationCondition:
    """已到位物料改挂目标库位时，Claim 事务内必须成立的库存条件。"""

    material_uuid: str
    source_owner_material_uuid: str
    source_site_uuid: str
    target_owner_material_uuid: str
    target_site_uuid: str


@dataclass(frozen=True, slots=True)
class DispatchAdmissionRequest:
    """一次不可变派发效果请求及其完整资源和条件快照。"""

    effect_uuid: str
    task_uuid: str
    job_uuid: str
    attempt: int
    parameter_hash: str
    expected_change_set: Mapping[str, Any]
    resources: tuple[DispatchResource, ...]
    preheld_lock_keys: tuple[str, ...] = ()
    preheld_job_uuids: tuple[str, ...] = ()
    transfer: TransferDispatchCondition | None = None
    operate_in_place: OperateInPlaceCondition | tuple[OperateInPlaceCondition, ...] | None = None
    aliquot: AliquotDispatchCondition | None = None
    inventory_relocation: InventoryRelocationCondition | None = None
    shared_scope_lock_keys: tuple[str, ...] = ()
    reserved_target_site_uuids: tuple[str, ...] = ()
    material_input_receipt: Mapping[str, Any] | None = None
    custody_endpoint_site_uuids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DispatchFence:
    """一个资源 Claim 对应的严格递增栅栏令牌。"""

    lock_key: str
    fencing_token: int


@dataclass(frozen=True, slots=True)
class DispatchPermit:
    """库存权威已经原子签发、可供工作流库投影的派发凭据。"""

    effect_uuid: str
    claim_uuid: str
    task_uuid: str
    job_uuid: str
    attempt: int
    parameter_hash: str
    expected_change_set: Mapping[str, Any]
    fences: tuple[DispatchFence, ...]

    def as_mapping(self) -> dict[str, Any]:
        """返回工作流派发意图可持久化的公共凭据形状。

        参数：无。返回：Claim、效果身份、参数哈希、预期 ChangeSet 和按资源键
        排序的 Fence 副本。异常：无；值已在库存事务签发前完成校验。
        """

        return {
            "effect_uuid": self.effect_uuid,
            "claim_uuid": self.claim_uuid,
            "parameter_hash": self.parameter_hash,
            "expected_change_set": dict(self.expected_change_set),
            "fences": [
                {
                    "lock_key": fence.lock_key,
                    "fencing_token": fence.fencing_token,
                }
                for fence in self.fences
            ],
        }


@dataclass(frozen=True, slots=True)
class DispatchAdmissionDecision:
    """派发准入成功凭据或正常资源竞争的等待原因。"""

    permit: DispatchPermit | None = None
    wait_code: str = ""
    wait_message: str = ""
    blocking_task_uuid: str = ""
    blocking_job_uuid: str = ""
    selected_candidate_index: int = -1

    @property
    def acquired(self) -> bool:
        """返回本次请求是否已经取得完整派发凭据。"""

        return self.permit is not None

    @property
    def effect_uuid(self) -> str:
        """返回成功凭据的效果 UUID；等待时为空。"""

        return self.permit.effect_uuid if self.permit is not None else ""

    @property
    def claim_uuid(self) -> str:
        """返回成功凭据的 Claim UUID；等待时为空。"""

        return self.permit.claim_uuid if self.permit is not None else ""

    @property
    def fences(self) -> tuple[DispatchFence, ...]:
        """返回成功凭据的 Fence；等待时为空元组。"""

        return self.permit.fences if self.permit is not None else ()


class DispatchAdmissionConflict(ValueError):
    """派发请求或持久准入事实损坏，不能降级为资源等待。"""


class InventoryMutationConflict(ValueError):
    """公共库存写操作命中了活动调度 Claim，必须等待或走结算接口。"""

    def __init__(
        self,
        *,
        claim_uuid: str,
        job_uuid: str,
        requested_lock_keys: Sequence[str],
    ) -> None:
        """保存阻塞 Claim、Job 和本次物理写涉及的规范资源键。"""

        self.claim_uuid = claim_uuid
        self.job_uuid = job_uuid
        self.requested_lock_keys = tuple(sorted(set(requested_lock_keys)))
        super().__init__(f"库存资源正由作业 {job_uuid} 的活动 Claim {claim_uuid} 持有")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS station_execution_claim (
    claim_uuid TEXT PRIMARY KEY,
    effect_uuid TEXT NOT NULL UNIQUE,
    task_uuid TEXT NOT NULL,
    job_uuid TEXT NOT NULL,
    attempt INTEGER NOT NULL CHECK (attempt > 0),
    parameter_hash TEXT NOT NULL CHECK (length(trim(parameter_hash)) > 0),
    expected_change_set TEXT NOT NULL,
    resource_keys TEXT NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN ('prepared', 'reserved', 'running', 'released', 'uncertain')
    ),
    acquired_at TEXT NOT NULL,
    committed_at TEXT,
    released_at TEXT,
    update_time TEXT NOT NULL,
    UNIQUE(job_uuid, attempt)
);
CREATE INDEX IF NOT EXISTS ix_station_execution_claim_task_state
ON station_execution_claim(task_uuid, state, acquired_at, claim_uuid);

CREATE TABLE IF NOT EXISTS station_execution_fence_counter (
    lock_key TEXT PRIMARY KEY,
    last_fencing_token INTEGER NOT NULL CHECK (last_fencing_token > 0),
    update_time TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS station_execution_lock_lease (
    claim_uuid TEXT NOT NULL,
    lock_key TEXT NOT NULL,
    scope TEXT NOT NULL CHECK (
        scope IN ('device', 'material', 'material_site', 'resource')
    ),
    material_uuid TEXT,
    site_uuid TEXT,
    fencing_token INTEGER NOT NULL CHECK (fencing_token > 0),
    state TEXT NOT NULL CHECK (
        state IN ('prepared', 'reserved', 'running', 'released', 'uncertain')
    ),
    acquired_at TEXT NOT NULL,
    released_at TEXT,
    update_time TEXT NOT NULL,
    PRIMARY KEY(claim_uuid, lock_key),
    FOREIGN KEY(claim_uuid) REFERENCES station_execution_claim(claim_uuid)
);
CREATE INDEX IF NOT EXISTS ix_station_execution_lock_lease_active
ON station_execution_lock_lease(state, lock_key, acquired_at, claim_uuid);
"""


def migrate_dispatch_admission_schema(connection: sqlite3.Connection) -> None:
    """幂等创建库存派发 Claim、Lease 与 Fence 表。

    参数：``connection`` 是库存初始化持有的 SQLite 连接。返回：无。异常：DDL
    或约束错误原样传播，由库存初始化整体回滚，禁止缺表降级运行。
    """

    connection.executescript(_SCHEMA)
    from .transport_allocation import migrate as migrate_transport_allocations

    migrate_transport_allocations(connection)
    _ensure_dispatch_resource_scope(connection)


def _ensure_dispatch_resource_scope(connection: sqlite3.Connection) -> None:
    """原地放宽旧库存 Lease 的 scope CHECK，不复制 Claim 或 Fence 事实。"""

    table = "station_execution_lock_lease"
    old = "scope IN ('device', 'material', 'material_site')"
    new = "scope IN ('device', 'material', 'material_site', 'resource')"
    row = connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    sql = str(row[0] or "") if row is not None else ""
    if new not in sql or old in sql:
        if sql.count(old) != 1 or new in sql:
            raise sqlite3.OperationalError(
                "station_execution_lock_lease 定义无法安全增加通用资源 scope"
            )
        schema_version = int(connection.execute("PRAGMA schema_version").fetchone()[0])
        connection.execute("PRAGMA writable_schema = ON")
        try:
            cursor = connection.execute(
                "UPDATE sqlite_schema SET sql=replace(sql, ?, ?) "
                "WHERE type='table' AND name=? AND instr(sql, ?) > 0",
                (old, new, table, old),
            )
            if cursor.rowcount != 1:
                raise sqlite3.OperationalError("库存通用资源 scope 迁移未命中")
            connection.execute(f"PRAGMA schema_version = {schema_version + 1}")
        finally:
            connection.execute("PRAGMA writable_schema = OFF")
    refreshed = connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    if refreshed is None or new not in str(refreshed[0] or ""):
        raise sqlite3.OperationalError("库存通用资源 scope 迁移未生效")
    integrity = connection.execute("PRAGMA integrity_check").fetchone()
    if integrity is None or str(integrity[0]).lower() != "ok":
        raise sqlite3.IntegrityError("库存执行锁 scope 迁移后完整性检查失败")
    if connection.execute("PRAGMA foreign_key_check").fetchall():
        raise sqlite3.IntegrityError("库存执行锁 scope 迁移后外键检查失败")


def assert_resource_keys_unclaimed(
    connection: sqlite3.Connection,
    *,
    lock_keys: Sequence[str],
    ignore_ingress_reservation_uuid: str = "",
    ignore_handoff_uuid: str = "",
    allow_ingress_device_sharing: bool = False,
    material_input_task_uuid: str = "",
    material_output_invocation_uuid: str = "",
) -> None:
    """拒绝与活动库存 Claim 冲突的一组精确规范资源键。

    ``material_output_invocation_uuid`` 仅供已经核实原产物完整导出证据的交接预检排除原保护。
    参数：``connection`` 是调用方即将写入的同一库存事务；``lock_keys`` 已按
    实际读写集合构造，可同时包含设备、物料、库位和通用资源键。返回：无。
    ``ignore_ingress_reservation_uuid`` 仅供入口自身结算时排除自己的持续占用；
    ``allow_ingress_device_sharing`` 允许同一入口架的多个逻辑库位预留共享架体
    设备键，但普通 Job 和库存写仍与该键互斥。异常：键语法损坏时抛
    ``DispatchAdmissionConflict``；命中活动 Lease 或入口预留时抛
    ``InventoryMutationConflict``，调用方必须整体回滚或跳过当前候选。
    """

    requested_keys: set[str] = set()
    for lock_key in lock_keys:
        if (
            not isinstance(lock_key, str)
            or canonical_resource_lock_scope(lock_key) is None
        ):
            raise DispatchAdmissionConflict("库存写操作包含非规范资源键")
        requested_keys.add(lock_key)
    if not requested_keys:
        return

    from .transport_allocation import blocker as transport_blocker

    allocation = transport_blocker(connection, tuple(requested_keys))
    if allocation is not None:
        raise InventoryMutationConflict(claim_uuid=allocation, job_uuid="",
                                        requested_lock_keys=tuple(requested_keys))

    from unilabos.app.scheduler.inventory.material_inputs import material_input_blocker

    blocker = material_input_blocker(connection, tuple(requested_keys), task_uuid=material_input_task_uuid)
    if blocker is not None:
        raise InventoryMutationConflict(
            claim_uuid=str(blocker["invocation_uuid"]), job_uuid="",
            requested_lock_keys=tuple(requested_keys),
        )

    from unilabos.app.scheduler.inventory.outbound import outbound_blocker
    outbound = outbound_blocker(connection, requested_keys, ignore_invocation_uuid=material_output_invocation_uuid)
    if outbound is not None:
        raise InventoryMutationConflict(claim_uuid=str(outbound["workflow_id"]), job_uuid="",
                                       requested_lock_keys=tuple(requested_keys))

    from unilabos.app.scheduler.inventory.custody import custody_blocker_for_keys
    custody = custody_blocker_for_keys(connection, tuple(requested_keys), task_uuid=material_input_task_uuid)
    if custody is not None:
        raise InventoryMutationConflict(claim_uuid=str(custody["binding_id"]), job_uuid="",
                                       requested_lock_keys=tuple(requested_keys))

    active = connection.execute(
        """
        SELECT lease.lock_key,lease.claim_uuid,claim.job_uuid
        FROM station_execution_lock_lease AS lease
        JOIN station_execution_claim AS claim USING(claim_uuid)
        WHERE lease.state IN ('prepared','reserved','running','uncertain')
        ORDER BY lease.acquired_at,lease.claim_uuid,lease.lock_key
        """
    ).fetchall()
    for row in active:
        if conflicting_resource_lock_keys(
            requested_keys,
            {str(row["lock_key"])},
        ):
            raise InventoryMutationConflict(
                claim_uuid=str(row["claim_uuid"]),
                job_uuid=str(row["job_uuid"]),
                requested_lock_keys=tuple(requested_keys),
            )
    for row in connection.execute(
        "SELECT handoff_uuid,lock_key FROM station_handoff_resource WHERE active=1 AND handoff_uuid<>?",
        (ignore_handoff_uuid,),
    ).fetchall():
        if conflicting_resource_lock_keys(requested_keys, {str(row["lock_key"])}):
            raise InventoryMutationConflict(
                claim_uuid=str(row["handoff_uuid"]), job_uuid="",
                requested_lock_keys=tuple(requested_keys),
            )
    from unilabos.app.scheduler.inventory.handoff_pickup import source_location_holds
    for row in source_location_holds(connection,ignore_handoff_uuid=ignore_handoff_uuid):
        if conflicting_resource_lock_keys(requested_keys,{row["lock_key"]}):
            raise InventoryMutationConflict(claim_uuid=row["handoff_uuid"],job_uuid="",requested_lock_keys=tuple(requested_keys))
    ingress_rows = connection.execute(
        "SELECT reservation_uuid,lock_key "
        "FROM station_ingress_reservation_resource "
        "WHERE active=1 AND reservation_uuid<>? "
        "ORDER BY reservation_uuid,lock_key",
        (str(ignore_ingress_reservation_uuid or ""),),
    ).fetchall()
    for row in ingress_rows:
        held_key = str(row["lock_key"])
        if (
            allow_ingress_device_sharing
            and canonical_resource_lock_scope(held_key) == "device"
            and held_key in requested_keys
        ):
            continue
        if conflicting_resource_lock_keys(
            requested_keys,
            {held_key},
        ):
            raise InventoryMutationConflict(
                claim_uuid=str(row["reservation_uuid"]),
                job_uuid="",
                requested_lock_keys=tuple(requested_keys),
            )


def assert_inventory_mutation_unclaimed(
    connection: sqlite3.Connection,
    *,
    material_uuids: Sequence[str] = (),
    site_uuids: Sequence[str] = (),
) -> None:
    """在公共库存写事务内拒绝任何与活动 Claim 冲突的物理事实修改。

    参数：``connection`` 是即将执行修改的同一库存事务；物料和库位身份是本次
    写操作可能影响的完整集合。补齐物料当前占用库位；库位拥有者仅用于生成
    精确库位键，不升级为整物料或设备请求。整体结构修改由调用者显式提供
    整物料身份。返回：无。异常：命中 prepared/reserved/running/uncertain Lease 时抛
    ``InventoryMutationConflict``，事务必须整体回滚。
    """

    materials = {str(item or "").strip() for item in material_uuids}
    materials.discard("")
    sites = {str(item or "").strip() for item in site_uuids}
    sites.discard("")

    if materials:
        placeholders = ",".join("?" for _ in materials)
        occupied = connection.execute(
            "SELECT uuid,material_uuid FROM site "
            f"WHERE occupied_material_uuid IN ({placeholders}) "
            "AND deleted_at IS NULL",
            tuple(sorted(materials)),
        ).fetchall()
        for row in occupied:
            sites.add(str(row["uuid"]))
    if sites:
        placeholders = ",".join("?" for _ in sites)
        owners = connection.execute(
            "SELECT uuid,material_uuid FROM site "
            f"WHERE uuid IN ({placeholders}) AND deleted_at IS NULL",
            tuple(sorted(sites)),
        ).fetchall()
        site_owner = {str(row["uuid"]): str(row["material_uuid"]) for row in owners}
    else:
        site_owner = {}

    requested_keys = {
        key
        for material_uuid in materials
        for key in (material_lock_key(material_uuid), device_lock_key(material_uuid))
    }
    requested_keys.update(
        site_lock_key(owner_uuid, site_uuid)
        for site_uuid, owner_uuid in site_owner.items()
    )
    assert_resource_keys_unclaimed(connection, lock_keys=tuple(requested_keys))


def validate_physical_settlement_credentials(
    connection: sqlite3.Connection,
    *,
    effect_uuid: str,
    claim_uuid: str,
    job_uuid: str,
    attempt: int,
    parameter_hash: str,
    expected_change_set: Mapping[str, Any],
    fences: Mapping[str, int],
    allow_released_replay: bool = False,
    evidence_only: bool = False,
) -> str:
    """验证 Scheduler PhysicalSettlement 携带的是当前完整 Permit。

    参数：库存写事务和派发时冻结的七类凭据。返回：无。异常：Claim 不存在、
    生命周期不允许结算、任一身份/哈希/ChangeSet/Fence 缺失或漂移时抛
    ``DispatchAdmissionConflict``。验证与后续物理写必须共享同一事务。
    """

    claim = connection.execute(
        "SELECT * FROM station_execution_claim WHERE claim_uuid=?",
        (str(claim_uuid or "").strip(),),
    ).fetchone()
    if claim is None:
        raise DispatchAdmissionConflict("PhysicalSettlement Claim 不存在")
    claim_state = str(claim["state"])
    active_states = {"reserved", "running", "uncertain"}
    allowed_states = active_states | ({"released"} if allow_released_replay else set())
    if claim_state not in allowed_states:
        raise DispatchAdmissionConflict(
            f"PhysicalSettlement Claim 状态不允许结算：{claim['state']}"
        )
    expected_identity = (
        str(effect_uuid or "").strip(),
        str(job_uuid or "").strip(),
        int(attempt),
        str(parameter_hash or "").strip(),
        _canonical_json(expected_change_set),
    )
    persisted_identity = (
        str(claim["effect_uuid"]),
        str(claim["job_uuid"]),
        int(claim["attempt"]),
        str(claim["parameter_hash"]),
        str(claim["expected_change_set"]),
    )
    if expected_identity != persisted_identity:
        raise DispatchAdmissionConflict(
            "PhysicalSettlement 派发身份、参数哈希或 ChangeSet 与 Claim 不一致"
        )

    leases = connection.execute(
        "SELECT lock_key,fencing_token,state FROM station_execution_lock_lease "
        "WHERE claim_uuid=? ORDER BY lock_key",
        (claim_uuid,),
    ).fetchall()
    persisted_fences = {
        str(row["lock_key"]): int(row["fencing_token"]) for row in leases
    }
    normalized_fences = {
        str(lock_key): int(token) for lock_key, token in fences.items()
    }
    allowed_lease_states = {"released"} if claim_state == "released" else active_states
    if normalized_fences != persisted_fences or (not evidence_only and any(
        str(row["state"]) not in allowed_lease_states for row in leases
    )):
        raise DispatchAdmissionConflict(
            "PhysicalSettlement Fence 缺失、过期或状态不一致"
        )
    return claim_state


def validate_active_dispatch_permit(
    connection: sqlite3.Connection,
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
    """物理下发只接受已提交的 RESERVED 凭据。"""
    _validate_live_dispatch_permit(
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


def _validate_live_dispatch_permit(
    connection: sqlite3.Connection,
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
    prepared: bool = False,
) -> None:
    """在物理派发前严格复验库存 Claim 仍是同一活动预留。

    参数：库存读写事务和工作流已冻结的 Permit 身份。返回：无。异常：Claim
    缺失、已释放、尚未提交或任一稳定身份发生漂移时抛
    ``DispatchAdmissionConflict``，调用方不得越过物理派发边界。
    """

    claim = connection.execute(
        "SELECT * FROM station_execution_claim WHERE claim_uuid=?",
        (str(claim_uuid or "").strip(),),
    ).fetchone()
    if claim is None:
        raise DispatchAdmissionConflict("物理派发前库存 Claim 不存在")
    if (
        str(claim["state"]) not in ({"prepared", "reserved"} if prepared else {"reserved"})
        or (claim["state"] == "reserved" and claim["committed_at"] is None)
        or claim["released_at"] is not None
    ):
        raise DispatchAdmissionConflict("物理派发前库存 Claim 不是活动预留")
    if not isinstance(expected_change_set, Mapping):
        raise DispatchAdmissionConflict("物理派发前 expected_change_set 已损坏")
    expected_identity = (
        str(effect_uuid or "").strip(),
        str(task_uuid or "").strip(),
        str(job_uuid or "").strip(),
        int(attempt),
        str(parameter_hash or "").strip(),
        _canonical_json(expected_change_set),
    )
    persisted_identity = (
        str(claim["effect_uuid"]),
        str(claim["task_uuid"]),
        str(claim["job_uuid"]),
        int(claim["attempt"]),
        str(claim["parameter_hash"]),
        str(claim["expected_change_set"]),
    )
    if expected_identity != persisted_identity:
        raise DispatchAdmissionConflict("物理派发前库存 Permit 身份或内容不一致")
    normalized_resource_keys: list[str] = []
    for raw_key in resource_keys:
        if not isinstance(raw_key, str):
            raise DispatchAdmissionConflict("物理派发前资源集合已损坏")
        lock_key = raw_key.strip()
        if not lock_key or lock_key != raw_key or lock_key in normalized_resource_keys:
            raise DispatchAdmissionConflict("物理派发前资源集合已损坏")
        normalized_resource_keys.append(lock_key)
    expected_resource_keys = tuple(sorted(normalized_resource_keys))
    try:
        raw_persisted_keys = json.loads(str(claim["resource_keys"]))
    except (TypeError, ValueError) as error:
        raise DispatchAdmissionConflict("物理派发前库存 Claim 资源集合已损坏") from error
    if not isinstance(raw_persisted_keys, list):
        raise DispatchAdmissionConflict("物理派发前库存 Claim 资源集合已损坏")
    persisted_resource_keys: list[str] = []
    for raw_key in raw_persisted_keys:
        if not isinstance(raw_key, str):
            raise DispatchAdmissionConflict("物理派发前库存 Claim 资源集合已损坏")
        lock_key = raw_key.strip()
        if (
            not lock_key
            or lock_key != raw_key
            or lock_key in persisted_resource_keys
        ):
            raise DispatchAdmissionConflict("物理派发前库存 Claim 资源集合已损坏")
        persisted_resource_keys.append(lock_key)
    persisted_resource_keys_tuple = tuple(sorted(persisted_resource_keys))
    if persisted_resource_keys_tuple != expected_resource_keys:
        raise DispatchAdmissionConflict("物理派发前库存 Claim 资源集合不一致")
    normalized_fences: dict[str, int] = {}
    for raw_key, raw_token in fences.items():
        lock_key = str(raw_key or "").strip()
        if (
            not lock_key
            or lock_key in normalized_fences
            or isinstance(raw_token, bool)
            or not isinstance(raw_token, int)
            or raw_token <= 0
        ):
            raise DispatchAdmissionConflict("物理派发前 Fence 快照已损坏")
        normalized_fences[lock_key] = raw_token
    if set(normalized_fences) != set(expected_resource_keys):
        raise DispatchAdmissionConflict("物理派发前 Fence 快照资源集合不完整")
    leases = connection.execute(
        "SELECT lock_key,fencing_token,state,released_at "
        "FROM station_execution_lock_lease "
        "WHERE claim_uuid=? ORDER BY lock_key",
        (claim_uuid,),
    ).fetchall()
    lease_resource_keys = tuple(str(lease["lock_key"]) for lease in leases)
    if lease_resource_keys != expected_resource_keys or any(
        str(lease["state"]) != str(claim["state"]) or lease["released_at"] is not None
        for lease in leases
    ):
        raise DispatchAdmissionConflict("物理派发前库存 Lease 缺失或不是活动预留")
    persisted_fences = {
        str(lease["lock_key"]): int(lease["fencing_token"]) for lease in leases
    }
    if persisted_fences != normalized_fences:
        raise DispatchAdmissionConflict("物理派发前库存 Fence 与工作流快照不一致")


def acquire_dispatch_permit(
    connection: sqlite3.Connection,
    request: DispatchAdmissionRequest,
) -> DispatchAdmissionDecision:
    """在库存写事务中复验条件并全有或全无取得资源 Claim。

    参数：``connection`` 已由 ``InventoryStore`` 以 ``BEGIN IMMEDIATE`` 串行化；
    ``request`` 包含稳定效果身份、最终参数哈希、预期变化、完整资源和可选转运
    条件。返回：成功时携带 ``DispatchPermit``；正常竞争时携带阻塞身份。异常：
    请求漂移、资源身份损坏或部署事实不完整时抛 ``DispatchAdmissionConflict``；
    条件暂时不满足由调用适配器转换为稳定库存条件错误。
    """

    normalized = _normalize_request(request)
    from .transport_allocation import blocker as transport_blocker

    allocation = transport_blocker(connection, tuple(r.lock_key for r in normalized.resources),
                                   task_uuid=normalized.task_uuid)
    if allocation is not None:
        return DispatchAdmissionDecision(wait_code="transport_vehicle_reserved",
                                         wait_message="车辆仍由搬运预占或结算屏障持有")
    task_material_conflict = _active_task_material_claim_conflict(
        connection,
        normalized,
    )
    if task_material_conflict is not None:
        return task_material_conflict
    existing = connection.execute(
        "SELECT * FROM station_execution_claim WHERE job_uuid=? AND attempt=?",
        (normalized.job_uuid, normalized.attempt),
    ).fetchone()
    if existing is not None:
        if str(existing["state"]) == "released" and existing["committed_at"] is None:
            return _reprepare_released_permit(
                connection,
                existing=existing,
                request=normalized,
            )
        return DispatchAdmissionDecision(permit=_replay_permit(connection, existing, normalized))

    active = connection.execute("""
        SELECT lease.*, claim.task_uuid, claim.job_uuid
        FROM station_execution_lock_lease lease
        JOIN station_execution_claim claim USING(claim_uuid)
        WHERE lease.state IN ('prepared', 'reserved', 'running', 'uncertain')
        ORDER BY lease.acquired_at, lease.claim_uuid, lease.lock_key
        """).fetchall()
    requested_keys = {resource.lock_key for resource in normalized.resources}
    conflict = _active_resource_conflict(active, normalized)
    if conflict is not None:
        return conflict
    ingress_conflict = _active_ingress_resource_conflict(connection, normalized)
    if ingress_conflict is not None:
        return ingress_conflict
    _validate_dispatch_conditions(connection, normalized)

    now = _utc_now()
    claim_uuid = str(uuid4())
    resource_keys_json = _canonical_json(sorted(requested_keys))
    expected_change_json = _canonical_json(normalized.expected_change_set)
    connection.execute(
        """
        INSERT INTO station_execution_claim(
            claim_uuid,effect_uuid,task_uuid,job_uuid,attempt,parameter_hash,
            expected_change_set,resource_keys,state,acquired_at,committed_at,
            released_at,update_time
        ) VALUES (?,?,?,?,?,?,?,?,'prepared',?,NULL,NULL,?)
        """,
        (
            claim_uuid,
            normalized.effect_uuid,
            normalized.task_uuid,
            normalized.job_uuid,
            normalized.attempt,
            normalized.parameter_hash,
            expected_change_json,
            resource_keys_json,
            now,
            now,
        ),
    )
    fences: list[DispatchFence] = []
    for resource in normalized.resources:
        token = _admission_fencing_token(connection, resource.lock_key, active, normalized, now=now)
        connection.execute(
            """
            INSERT INTO station_execution_lock_lease(
                claim_uuid,lock_key,scope,material_uuid,site_uuid,fencing_token,
                state,acquired_at,released_at,update_time
            ) VALUES (?,?,?,?,?,?,'prepared',?,NULL,?)
            """,
            (
                claim_uuid,
                resource.lock_key,
                resource.scope,
                resource.material_uuid or None,
                resource.site_uuid or None,
                token,
                now,
                now,
            ),
        )
        fences.append(DispatchFence(resource.lock_key, token))
    return DispatchAdmissionDecision(
        permit=DispatchPermit(
            effect_uuid=normalized.effect_uuid,
            claim_uuid=claim_uuid,
            task_uuid=normalized.task_uuid,
            job_uuid=normalized.job_uuid,
            attempt=normalized.attempt,
            parameter_hash=normalized.parameter_hash,
            expected_change_set=dict(normalized.expected_change_set),
            fences=tuple(fences),
        )
    )


def release_preheld_dispatch_claims(
    connection: sqlite3.Connection,
    *,
    task_uuid: str,
    job_uuids: Sequence[str],
    lock_keys: Sequence[str],
) -> tuple[str, ...]:
    """在后继派发意图已投影后收敛前一 Job 的物理 Claim。"""

    jobs = {str(value or "").strip() for value in job_uuids if str(value or "").strip()}
    keys = {str(value or "").strip() for value in lock_keys if str(value or "").strip()}
    if not jobs or not keys:
        return ()
    rows = connection.execute(
        "SELECT claim_uuid, job_uuid FROM station_execution_claim "
        "WHERE task_uuid=? AND job_uuid IN (%s) "
        "AND state IN ('prepared','reserved','running')" % ",".join("?" for _ in jobs),
        (task_uuid, *sorted(jobs)),
    ).fetchall()
    released: list[str] = []
    for row in rows:
        claim_uuid = str(row["claim_uuid"])
        active = {
            str(item["lock_key"])
            for item in connection.execute(
                "SELECT lock_key FROM station_execution_lock_lease "
                "WHERE claim_uuid=? AND state IN ('prepared','reserved','running')",
                (claim_uuid,),
            ).fetchall()
        }
        remaining = active - keys
        if remaining:
            retain_dispatch_permit_resources(
                connection, claim_uuid=claim_uuid, keep_lock_keys=tuple(remaining)
            )
        else:
            transition_dispatch_permit(connection, claim_uuid=claim_uuid, target_state="released")
            released.append(claim_uuid)
    return tuple(sorted(released))


def release_task_dispatch_permits(
    connection: sqlite3.Connection,
    *,
    task_uuid: str,
) -> tuple[str, ...]:
    """在操作员完成物理确认后整组释放一个终态 Task 的库存 Permit。

    参数：``connection`` 是库存权威写事务；``task_uuid`` 已由工作流权威证明为
    异常终态。返回本次仍处于活动生命周期的 Claim UUID，按身份稳定排序。
    异常：空 Task 身份或非法 Claim 状态抛 ``DispatchAdmissionConflict``，整个
    库存事务回滚。释放是单向且幂等的，便于跨库清理失败后安全重放。
    """

    normalized_task_uuid = str(task_uuid or "").strip()
    if not normalized_task_uuid:
        raise DispatchAdmissionConflict("人工释放缺少工作流任务身份")
    rows = connection.execute(
        """
        SELECT claim_uuid FROM station_execution_claim
        WHERE task_uuid=?
          AND state IN ('prepared','reserved','running','uncertain')
        ORDER BY claim_uuid
        """,
        (normalized_task_uuid,),
    ).fetchall()
    claim_uuids = tuple(str(row["claim_uuid"]) for row in rows)
    for claim_uuid in claim_uuids:
        transition_dispatch_permit(
            connection,
            claim_uuid=claim_uuid,
            target_state="released",
        )
    return claim_uuids


def acquire_dispatch_permit_candidates(
    connection: sqlite3.Connection,
    requests: Sequence[DispatchAdmissionRequest],
) -> DispatchAdmissionDecision:
    """在同一库存事务内按稳定顺序尝试一组完整派发候选。

    参数：``connection`` 已由单写事务串行化；``requests`` 的每项都是同一 Task/
    Job/attempt 对不同目标库位形成的完整资源闭集。返回：首个成功候选的 Permit
    与索引；全部竞争时返回首个稳定等待原因。异常：请求身份不一致或任一候选
    合同损坏时关闭失败；可变化物理条件会尝试后续候选，全部不满足才上抛首项。
    """

    if not isinstance(requests, Sequence) or isinstance(requests, (str, bytes)):
        raise DispatchAdmissionConflict("派发候选必须是非空请求数组")
    candidates = tuple(requests)
    if not candidates:
        raise DispatchAdmissionConflict("派发候选必须是非空请求数组")
    normalized = tuple(_normalize_request(request) for request in candidates)
    identity = {
        (request.task_uuid, request.job_uuid, request.attempt) for request in normalized
    }
    if len(identity) != 1:
        raise DispatchAdmissionConflict("派发候选必须属于同一 Task、Job 和 attempt")
    task_uuid, job_uuid, attempt = next(iter(identity))
    existing = connection.execute(
        "SELECT * FROM station_execution_claim WHERE job_uuid=? AND attempt=?",
        (job_uuid, attempt),
    ).fetchone()
    if existing is not None:
        for index, request in enumerate(normalized):
            try:
                _assert_replay_matches(existing, request)
            except DispatchAdmissionConflict:
                continue
            decision = acquire_dispatch_permit(connection, request)
            return DispatchAdmissionDecision(
                permit=decision.permit,
                wait_code=decision.wait_code,
                wait_message=decision.wait_message,
                blocking_task_uuid=decision.blocking_task_uuid,
                blocking_job_uuid=decision.blocking_job_uuid,
                selected_candidate_index=(index if decision.acquired else -1),
            )
        raise DispatchAdmissionConflict("同一作业尝试的既有 Claim 不匹配任何稳定候选")
    first_wait: DispatchAdmissionDecision | None = None
    first_temporary: TemporaryDispatchCondition | None = None
    for index, request in enumerate(normalized):
        try:
            decision = acquire_dispatch_permit(connection, request)
        except TemporaryDispatchCondition as error:
            if first_temporary is None:
                first_temporary = error
            continue
        if decision.acquired:
            return DispatchAdmissionDecision(
                permit=decision.permit,
                selected_candidate_index=index,
            )
        if first_wait is None:
            first_wait = decision
    if first_wait is not None:
        return first_wait
    if first_temporary is not None:
        raise first_temporary
    raise DispatchAdmissionConflict("派发候选没有形成准入结果")


def transition_dispatch_permit(
    connection: sqlite3.Connection,
    *,
    claim_uuid: str,
    target_state: str,
) -> None:
    """幂等推进库存 Claim 与全部 Lease 的物理生命周期。

    参数：``connection`` 是库存写事务；``claim_uuid`` 是已签发凭据；
    ``target_state`` 只接受 reserved、running、uncertain、released。返回：无。
    迟到的 running 通知不覆盖已释放或待核对状态；其他逆向转换、Claim 缺失
    或非法状态抛 ``DispatchAdmissionConflict``。
    """

    allowed = {
        "reserved": {"prepared", "reserved"},
        "running": {"reserved", "running"},
        "uncertain": {"prepared", "reserved", "running", "uncertain"},
        "released": {
            "prepared",
            "reserved",
            "running",
            "uncertain",
            "released",
        },
    }
    if target_state not in allowed:
        raise DispatchAdmissionConflict(f"非法库存 Claim 目标状态：{target_state}")
    row = connection.execute(
        "SELECT state FROM station_execution_claim WHERE claim_uuid=?",
        (claim_uuid,),
    ).fetchone()
    if row is None:
        raise DispatchAdmissionConflict(f"库存 Claim 不存在：{claim_uuid}")
    current = str(row["state"])
    if target_state == "running" and current in {"released", "uncertain"}:
        # HTTP 终态回执可能先于 WebSocket started；本事务中保留终态，
        # 不能依赖桥接层事前查询 Job 状态来避免这个并发窗口。
        return
    if current == "released" and target_state == "uncertain":
        return  # 人工释放是独立事实，迟到结果不能复活占用。
    if current not in allowed[target_state]:
        raise DispatchAdmissionConflict(
            f"库存 Claim 不能从 {current} 进入 {target_state}：{claim_uuid}"
        )
    if current == target_state:
        return
    now = _utc_now()
    released_at = now if target_state == "released" else None
    committed_at = now if target_state == "reserved" else None
    connection.execute(
        """
        UPDATE station_execution_claim
        SET state=?, committed_at=COALESCE(committed_at, ?),
            released_at=?, update_time=?
        WHERE claim_uuid=?
        """,
        (target_state, committed_at, released_at, now, claim_uuid),
    )
    connection.execute(
        """
        UPDATE station_execution_lock_lease
        SET state=?, released_at=?, update_time=?
        WHERE claim_uuid=? AND state!='released'
        """,
        (target_state, released_at, now, claim_uuid),
    )


def retain_dispatch_permit_resources(
    connection: sqlite3.Connection,
    *,
    claim_uuid: str,
    keep_lock_keys: Sequence[str],
    proved_result: bool = False,
) -> None:
    """在连续区间交接或异常冻结时只释放当前 Claim 的临时资源。

    ``proved_result`` 表示调用方已提交该 Job 的明确结果：此前因 UNKNOWN 标记为
    uncertain 的 Claim 与保留 Lease 收敛回 running，后继才能按连续区间继承。
    未证明结果时保持 uncertain，继承判定仍会拒绝。
    """

    keep = {
        str(value or "").strip() for value in keep_lock_keys if str(value or "").strip()
    }
    row = connection.execute(
        "SELECT state FROM station_execution_claim WHERE claim_uuid=?",
        (claim_uuid,),
    ).fetchone()
    if row is None:
        raise DispatchAdmissionConflict(f"库存 Claim 不存在：{claim_uuid}")
    state = str(row["state"])
    if state not in {"prepared", "reserved", "running", "uncertain"}:
        raise DispatchAdmissionConflict(
            f"库存 Claim 不能进行连续资源保留：{claim_uuid}"
        )
    active_rows = connection.execute(
        "SELECT lock_key FROM station_execution_lock_lease "
        "WHERE claim_uuid=? AND state IN ('prepared','reserved','running','uncertain')",
        (claim_uuid,),
    ).fetchall()
    active_keys = {str(item["lock_key"]) for item in active_rows}
    if not keep <= active_keys:
        raise DispatchAdmissionConflict("连续区间保留资源不属于当前库存 Claim")
    if not keep:
        transition_dispatch_permit(
            connection,
            claim_uuid=claim_uuid,
            target_state="released",
        )
        return
    now = _utc_now()
    release_keys = tuple(sorted(active_keys - keep))
    placeholders = ",".join("?" for _ in release_keys)
    if release_keys:
        connection.execute(
            "UPDATE station_execution_lock_lease "
            "SET state='released', released_at=?, update_time=? "
            f"WHERE claim_uuid=? AND lock_key IN ({placeholders}) "
            "AND state IN ('prepared','reserved','running','uncertain')",
            (now, now, claim_uuid, *release_keys),
        )
    settled_state = "running" if proved_result and state == "uncertain" else state
    if settled_state != state:
        keep_placeholders = ",".join("?" for _ in keep)
        connection.execute(
            "UPDATE station_execution_lock_lease SET state=?, update_time=? "
            f"WHERE claim_uuid=? AND lock_key IN ({keep_placeholders}) "
            "AND state='uncertain'",
            (settled_state, now, claim_uuid, *sorted(keep)),
        )
    connection.execute(
        "UPDATE station_execution_claim SET resource_keys=?, state=?, update_time=? "
        "WHERE claim_uuid=?",
        (_canonical_json(sorted(keep)), settled_state, now, claim_uuid),
    )


def release_unprojected_dispatch_permits(
    connection: sqlite3.Connection,
    *,
    known_claim_uuids: Sequence[str],
) -> tuple[str, ...]:
    """释放未跨库投影的 prepared Permit，收敛准入崩溃窗口。

    参数：库存事务和工作流库仍可识别的 Claim UUID 集合。返回：本次按 UUID
    排序释放的库存 Claim。异常：身份非法或数据库错误原样传播。reserved、
    running、uncertain Claim 已可能越过物理边界，永远不会由此恢复入口释放。
    """

    known: set[str] = set()
    for value in known_claim_uuids:
        try:
            known.add(str(UUID(str(value))))
        except (AttributeError, TypeError, ValueError) as error:
            raise DispatchAdmissionConflict("工作流 Claim 身份非法") from error
    rows = connection.execute(
        "SELECT claim_uuid FROM station_execution_claim "
        "WHERE state='prepared' ORDER BY acquired_at, claim_uuid"
    ).fetchall()
    released = tuple(
        str(row["claim_uuid"]) for row in rows if str(row["claim_uuid"]) not in known
    )
    for claim_uuid in released:
        transition_dispatch_permit(
            connection,
            claim_uuid=claim_uuid,
            target_state="released",
        )
    return released


def _normalize_request(request: DispatchAdmissionRequest) -> DispatchAdmissionRequest:
    """校验并稳定排序派发请求，阻止同键资源定义漂移。

    参数：``request`` 是调度器构造的候选请求。返回：字段规范且资源按锁键排序的
    新对象。异常：身份、哈希、预期变化或资源不合法时抛
    ``DispatchAdmissionConflict``。
    """

    for field, value in (
        ("effect_uuid", request.effect_uuid),
        ("task_uuid", request.task_uuid),
        ("job_uuid", request.job_uuid),
    ):
        try:
            UUID(str(value))
        except (AttributeError, TypeError, ValueError) as error:
            raise DispatchAdmissionConflict(f"{field} 不是合法 UUID") from error
    if request.attempt <= 0 or not str(request.parameter_hash or "").strip():
        raise DispatchAdmissionConflict("派发 attempt 或 parameter_hash 非法")
    if not isinstance(request.expected_change_set, Mapping):
        raise DispatchAdmissionConflict("expected_change_set 必须是对象")
    resources: dict[str, DispatchResource] = {}
    for item in request.resources:
        if not isinstance(item, DispatchResource):
            raise DispatchAdmissionConflict("派发资源必须使用 DispatchResource")
        lock_key = str(item.lock_key or "").strip()
        scope = str(item.scope or "").strip()
        if not lock_key or scope not in _SCOPES:
            raise DispatchAdmissionConflict("派发资源缺少合法 lock_key 或 scope")
        normalized = DispatchResource(
            lock_key=lock_key,
            scope=scope,
            material_uuid=str(item.material_uuid or "").strip(),
            site_uuid=str(item.site_uuid or "").strip(),
        )
        key_identity = parse_canonical_resource_lock_key(lock_key)
        if key_identity is None or key_identity.scope != scope:
            raise DispatchAdmissionConflict(
                "派发资源 scope 与规范 lock_key 不匹配"
            )
        if (
            normalized.material_uuid != (key_identity.material_uuid or "")
            or normalized.site_uuid != (key_identity.site_uuid or "")
        ):
            raise DispatchAdmissionConflict(
                "派发资源身份与 canonical lock_key 不一致"
            )
        previous = resources.get(lock_key)
        if previous is not None and previous != normalized:
            raise DispatchAdmissionConflict(f"同一派发资源定义冲突：{lock_key}")
        resources[lock_key] = normalized
    preheld_lock_keys = tuple(
        sorted(
            {
                str(value or "").strip()
                for value in request.preheld_lock_keys
                if str(value or "").strip()
            }
        )
    )
    if not set(preheld_lock_keys) <= set(resources):
        raise DispatchAdmissionConflict("连续区间预持有资源不在完整派发资源集合中")
    if not set(request.shared_scope_lock_keys) <= set(preheld_lock_keys):
        raise DispatchAdmissionConflict("共同作用域只能复用已声明的预持有资源")
    preheld_job_uuids = tuple(
        sorted(
            {
                str(value or "").strip()
                for value in request.preheld_job_uuids
                if str(value or "").strip()
            }
        )
    )
    for preheld_job_uuid in preheld_job_uuids:
        try:
            UUID(preheld_job_uuid)
        except (AttributeError, TypeError, ValueError) as error:
            raise DispatchAdmissionConflict("连续区间前一 Job 身份非法") from error
    if str(request.job_uuid) in preheld_job_uuids:
        raise DispatchAdmissionConflict("连续区间前一 Job 不能是当前 Job")
    endpoint_sites = tuple(sorted(set(request.custody_endpoint_site_uuids)))
    if not set(endpoint_sites) <= {r.site_uuid for r in resources.values() if r.scope == "material_site"}:
        raise DispatchAdmissionConflict("搬运保管范围的目标库位必须属于本次完整执行资源集")
    return DispatchAdmissionRequest(
        effect_uuid=str(request.effect_uuid),
        task_uuid=str(request.task_uuid),
        job_uuid=str(request.job_uuid),
        attempt=request.attempt,
        parameter_hash=str(request.parameter_hash).strip(),
        expected_change_set=dict(request.expected_change_set),
        resources=tuple(resources[key] for key in sorted(resources)),
        preheld_lock_keys=preheld_lock_keys,
        preheld_job_uuids=preheld_job_uuids,
        reserved_target_site_uuids=tuple(sorted(set(request.reserved_target_site_uuids))),
        shared_scope_lock_keys=tuple(sorted(set(request.shared_scope_lock_keys))),
        transfer=request.transfer,
        operate_in_place=request.operate_in_place,
        aliquot=request.aliquot,
        inventory_relocation=request.inventory_relocation,
        material_input_receipt=dict(request.material_input_receipt) if request.material_input_receipt is not None else None,
        custody_endpoint_site_uuids=endpoint_sites,
    )


def _validate_resource_facts(
    connection: sqlite3.Connection,
    resources: Sequence[DispatchResource],
) -> None:
    """证明请求引用的设备、物料与库位均存在且归属一致。

    参数：库存事务和规范资源集合。返回：无。异常：引用缺失或类型/归属不一致
    时抛 ``DispatchAdmissionConflict``，不得签发部分 Claim。
    """

    for resource in resources:
        if resource.scope == "device":
            row = connection.execute(
                "SELECT type FROM material WHERE uuid=? AND deleted_at IS NULL",
                (resource.material_uuid,),
            ).fetchone()
            if row is None or str(row["type"]) != "device":
                raise DispatchAdmissionConflict(
                    f"设备资源不存在或类型错误：{resource.material_uuid}"
                )
        elif resource.scope == "material":
            row = connection.execute(
                "SELECT 1 FROM material WHERE uuid=? AND deleted_at IS NULL",
                (resource.material_uuid,),
            ).fetchone()
            if row is None:
                raise DispatchAdmissionConflict(
                    f"物料资源不存在：{resource.material_uuid}"
                )
        elif resource.scope == "material_site":
            row = connection.execute(
                "SELECT material_uuid FROM site WHERE uuid=? AND deleted_at IS NULL",
                (resource.site_uuid,),
            ).fetchone()
            if row is None or str(row["material_uuid"]) != resource.material_uuid:
                raise DispatchAdmissionConflict(
                    f"库位不存在或归属不一致：{resource.site_uuid}"
                )
        elif resource.scope == "resource":
            # 通用命名互斥的身份由严格 canonical key 自证，不冒充 Inventory
            # Material/Site；Claim、Lease 与 Fence 仍在同一库存事务中签发。
            continue
        else:  # pragma: no cover - _normalize_request 已关闭式拒绝未知 scope。
            raise DispatchAdmissionConflict(f"派发资源 scope 不合法：{resource.scope}")


def _validate_transfer_conditions(
    connection: sqlite3.Connection,
    request: DispatchAdmissionRequest,
) -> None:
    """在 Claim 写入前复验来源、目标、机械臂夹爪和预期变化。

    参数：库存事务和含转运条件的规范请求。返回：无。异常：条件已被并发改变时
    抛内部 ``_TemporaryCondition``；合同/资源集合不完整时抛
    ``DispatchAdmissionConflict``。
    """

    condition = request.transfer
    assert condition is not None
    source = connection.execute(
        "SELECT material_uuid,occupied_material_uuid FROM site "
        "WHERE uuid=? AND deleted_at IS NULL",
        (condition.source_site_uuid,),
    ).fetchone()
    if (
        source is None
        or str(source["material_uuid"]) != condition.source_owner_material_uuid
        or str(source["occupied_material_uuid"] or "") != condition.material_uuid
    ):
        raise TemporaryDispatchCondition(
            "transfer_source_site_missing",
            "待搬物料已经不在准入时确认的来源库位",
            resources=({"scope": "material", "material_uuid": condition.material_uuid},),
        )
    target = connection.execute(
        "SELECT material_uuid,occupied_material_uuid FROM site "
        "WHERE uuid=? AND deleted_at IS NULL",
        (condition.target_site_uuid,),
    ).fetchone()
    if target is None or str(target["material_uuid"]) != condition.target_owner_material_uuid:
        raise DispatchAdmissionConflict("目标库位不存在或归属已经改变")
    if str(target["occupied_material_uuid"] or ""):
        raise TemporaryDispatchCondition(
            "site_occupied",
            "目标库位当前已有物料",
            resources=(
                {
                    "scope": "material_site",
                    "material_uuid": condition.target_owner_material_uuid,
                    "site_uuid": condition.target_site_uuid,
                },
            ),
        )
    ingress = connection.execute(
        "SELECT 1 FROM station_ingress_reservation WHERE site_uuid=? AND site_active=1 LIMIT 1",
        (condition.target_site_uuid,),
    ).fetchone()
    if ingress is not None:
        raise TemporaryDispatchCondition(
            "site_ingress_reserved",
            "目标库位已为运输中的入口载体预留",
            resources=(
                {
                    "scope": "material_site",
                    "material_uuid": condition.target_owner_material_uuid,
                    "site_uuid": condition.target_site_uuid,
                },
            ),
        )
    gripper = connection.execute(
        "SELECT material_uuid,occupied_material_uuid FROM site "
        "WHERE uuid=? AND deleted_at IS NULL",
        (condition.gripper_site_uuid,),
    ).fetchone()
    if gripper is None or str(gripper["material_uuid"]) != condition.executor_material_uuid:
        raise DispatchAdmissionConflict("机械臂夹爪库位不存在或归属已经改变")
    occupied = str(gripper["occupied_material_uuid"] or "")
    if occupied and not (
        condition.allow_held_material
        and occupied == condition.material_uuid
        and condition.source_site_uuid == condition.gripper_site_uuid
    ):
        raise TemporaryDispatchCondition(
            "gripper_site_occupied",
            "机械臂夹爪库位当前已有物料",
            resources=(
                {
                    "scope": "material_site",
                    "material_uuid": condition.executor_material_uuid,
                    "site_uuid": condition.gripper_site_uuid,
                },
            ),
        )

    required_keys = {
        device_lock_key(condition.executor_material_uuid),
        material_lock_key(condition.material_uuid),
        site_lock_key(
            condition.source_owner_material_uuid,
            condition.source_site_uuid,
        ),
        site_lock_key(
            condition.target_owner_material_uuid,
            condition.target_site_uuid,
        ),
        site_lock_key(
            condition.executor_material_uuid,
            condition.gripper_site_uuid,
        ),
    }
    for endpoint_owner_uuid in (
        condition.source_owner_material_uuid,
        condition.target_owner_material_uuid,
    ):
        device_owner_uuid = _owning_device_uuid(
            connection,
            endpoint_owner_uuid,
        )
        if device_owner_uuid:
            required_keys.add(device_lock_key(device_owner_uuid))
    actual_keys = {resource.lock_key for resource in request.resources}
    missing = sorted(required_keys - actual_keys)
    if missing:
        raise DispatchAdmissionConflict("机械臂转运 Claim 缺少完整资源：" + ",".join(missing))
    expected = request.expected_change_set
    required_change = {
        "kind": "material_transfer",
        "material_uuid": condition.material_uuid,
        "source_site_uuid": condition.source_site_uuid,
        "target_site_uuid": condition.target_site_uuid,
    }
    if any(expected.get(key) != value for key, value in required_change.items()):
        raise DispatchAdmissionConflict("转运预期 ChangeSet 与条件快照不一致")


def _validate_inventory_relocation_conditions(
    connection: sqlite3.Connection,
    request: DispatchAdmissionRequest,
) -> None:
    """在 Claim 写入前复验已到位物料的来源库位和目标库位。

    参数：库存事务和含库存改挂条件的规范请求。返回：无。异常：来源已变化时抛
    内部 ``_TemporaryCondition``；与机械臂转运同时声明，或目标库位不存在时抛
    ``DispatchAdmissionConflict``。不查询夹爪库位。
    """

    condition = request.inventory_relocation
    assert condition is not None
    if request.transfer is not None or request.operate_in_place is not None or request.aliquot is not None:
        raise DispatchAdmissionConflict("库存改挂不能与机械臂转运、原位操作或分装同时声明")
    source = connection.execute(
        "SELECT material_uuid,occupied_material_uuid FROM site "
        "WHERE uuid=? AND deleted_at IS NULL",
        (condition.source_site_uuid,),
    ).fetchone()
    if (
        source is None
        or str(source["material_uuid"]) != condition.source_owner_material_uuid
        or str(source["occupied_material_uuid"] or "") != condition.material_uuid
    ):
        raise TemporaryDispatchCondition(
            "transfer_source_site_missing",
            "待改挂物料已经不在确认的来源库位",
            resources=({"scope": "material", "material_uuid": condition.material_uuid},),
        )
    target = connection.execute(
        "SELECT material_uuid,occupied_material_uuid FROM site "
        "WHERE uuid=? AND deleted_at IS NULL",
        (condition.target_site_uuid,),
    ).fetchone()
    if target is None or str(target["material_uuid"]) != condition.target_owner_material_uuid:
        raise DispatchAdmissionConflict("目标库位不存在或归属已经改变")
    if str(target["occupied_material_uuid"] or ""):
        raise TemporaryDispatchCondition(
            "site_occupied",
            "目标库位当前已有物料",
            resources=(
                {
                    "scope": "material_site",
                    "material_uuid": condition.target_owner_material_uuid,
                    "site_uuid": condition.target_site_uuid,
                },
            ),
        )
    ingress = connection.execute(
        "SELECT 1 FROM station_ingress_reservation WHERE site_uuid=? AND site_active=1 LIMIT 1",
        (condition.target_site_uuid,),
    ).fetchone()
    if ingress is not None:
        raise TemporaryDispatchCondition(
            "site_ingress_reserved",
            "目标库位已为运输中的入口载体预留",
            resources=(
                {
                    "scope": "material_site",
                    "material_uuid": condition.target_owner_material_uuid,
                    "site_uuid": condition.target_site_uuid,
                },
            ),
        )
    required_keys = {
        material_lock_key(condition.material_uuid),
        site_lock_key(
            condition.source_owner_material_uuid,
            condition.source_site_uuid,
        ),
        site_lock_key(
            condition.target_owner_material_uuid,
            condition.target_site_uuid,
        ),
    }
    for endpoint_owner_uuid in (
        condition.source_owner_material_uuid,
        condition.target_owner_material_uuid,
    ):
        device_owner_uuid = _owning_device_uuid(connection, endpoint_owner_uuid)
        if device_owner_uuid:
            required_keys.add(device_lock_key(device_owner_uuid))
    actual_keys = {resource.lock_key for resource in request.resources}
    missing = sorted(required_keys - actual_keys)
    if missing:
        raise DispatchAdmissionConflict("库存改挂 Claim 缺少完整资源：" + ",".join(missing))
    expected = request.expected_change_set
    required_change = {
        "kind": "material_transfer",
        "material_uuid": condition.material_uuid,
        "source_site_uuid": condition.source_site_uuid,
        "target_site_uuid": condition.target_site_uuid,
    }
    if any(expected.get(key) != value for key, value in required_change.items()):
        raise DispatchAdmissionConflict("库存改挂预期 ChangeSet 与条件快照不一致")


def _validate_operate_in_place_conditions(
    connection: sqlite3.Connection,
    request: DispatchAdmissionRequest,
) -> None:
    """在 Claim 写入前复验原位物料、精确库位及实际执行设备祖先。"""

    _validate_operate_in_place_facts(
        connection,
        _operate_conditions(request.operate_in_place),
        {resource.lock_key for resource in request.resources},
        request.expected_change_set,
    )


def _validate_operate_in_place_facts(
    connection: sqlite3.Connection,
    conditions: tuple[OperateInPlaceCondition, ...],
    actual_keys: set[str],
    expected_change_set: Mapping[str, Any],
) -> None:
    """初次准入与派发前复验共用同一组库存位置与资源锁检查。"""
    if len({item.material_uuid for item in conditions}) != len(conditions):
        raise DispatchAdmissionConflict("原位操作物料身份重复")
    if len({item.device_material_uuid for item in conditions}) != 1:
        raise DispatchAdmissionConflict("原位操作物料不属于同一实际执行设备")
    for condition in conditions:
        site = connection.execute(
            "SELECT material_uuid,occupied_material_uuid FROM site WHERE uuid=? AND deleted_at IS NULL",
            (condition.site_uuid,),
        ).fetchone()
        if (
            site is None
            or str(site["material_uuid"]) != condition.site_owner_material_uuid
            or str(site["occupied_material_uuid"] or "") != condition.material_uuid
        ):
            raise TemporaryDispatchCondition(
                "operate_in_place_site_changed",
                "原位操作物料已经离开准入时确认的库位",
                resources=({"scope": "material", "material_uuid": condition.material_uuid},),
            )
        if _owning_device_uuid(connection, condition.site_owner_material_uuid) != condition.device_material_uuid:
            raise DispatchAdmissionConflict("原位操作库位不属于实际执行设备")
        required_keys = {
            device_lock_key(condition.device_material_uuid),
            material_lock_key(condition.material_uuid),
            site_lock_key(condition.site_owner_material_uuid, condition.site_uuid),
        }
        missing = sorted(required_keys - actual_keys)
        if missing:
            raise DispatchAdmissionConflict("原位操作 Claim 缺少完整资源：" + ",".join(missing))
    if expected_change_set.get("kind") != "no_inventory_change":
        raise DispatchAdmissionConflict("原位操作必须声明 no_inventory_change")


def _operate_conditions(
    value: OperateInPlaceCondition | tuple[OperateInPlaceCondition, ...] | None,
) -> tuple[OperateInPlaceCondition, ...]:
    """兼容旧的单项条件，拒绝空集合或损坏的多物料条件。"""
    conditions = (value,) if isinstance(value, OperateInPlaceCondition) else value
    if (
        not isinstance(conditions, tuple)
        or not conditions
        or any(not isinstance(item, OperateInPlaceCondition) for item in conditions)
    ):
        raise DispatchAdmissionConflict("原位操作条件集合无效")
    return conditions


def _validate_aliquot_conditions(
    connection: sqlite3.Connection,
    request: DispatchAdmissionRequest,
) -> None:
    """原子证明分装来源与完整目标闭集存在、互异并被同一 Claim 覆盖。"""

    condition = request.aliquot
    assert condition is not None
    source = str(condition.source_material_uuid or "").strip()
    targets = tuple(
        sorted({str(value or "").strip() for value in condition.target_material_uuids})
    )
    if not source or not targets or "" in targets or source in targets:
        raise DispatchAdmissionConflict("分装来源或目标集合非法")
    if len(targets) != len(condition.target_material_uuids):
        raise DispatchAdmissionConflict("分装目标集合包含重复项")
    existing = connection.execute(
        "SELECT uuid FROM material WHERE uuid IN ("
        + ",".join("?" for _ in (source, *targets))
        + ") AND deleted_at IS NULL",
        (source, *targets),
    ).fetchall()
    if {str(row["uuid"]) for row in existing} != {source, *targets}:
        raise DispatchAdmissionConflict("分装来源或目标容器不存在")
    required_keys = {material_lock_key(value) for value in (source, *targets)}
    actual_keys = {resource.lock_key for resource in request.resources}
    if missing := sorted(required_keys - actual_keys):
        raise DispatchAdmissionConflict("分装 Claim 缺少完整资源：" + ",".join(missing))
    expected = request.expected_change_set
    if (
        expected.get("kind") != "material_content_aliquot"
        or expected.get("source_material_uuid") != source
        or tuple(sorted(expected.get("target_material_uuids") or ())) != targets
    ):
        raise DispatchAdmissionConflict("分装预期 ChangeSet 与目标闭集不一致")


def _owning_device_uuid(
    connection: sqlite3.Connection,
    material_uuid: str,
) -> str:
    """在同一准入事务内沿物料父链解析最近的真实设备祖先。"""

    current = str(material_uuid or "").strip()
    visited: set[str] = set()
    while current:
        if current in visited:
            raise DispatchAdmissionConflict("库存物料父链存在循环")
        visited.add(current)
        row = connection.execute(
            "SELECT type,parent_uuid FROM material WHERE uuid=? AND deleted_at IS NULL",
            (current,),
        ).fetchone()
        if row is None:
            return ""
        if str(row["type"] or "") == "device":
            return current
        current = str(row["parent_uuid"] or "").strip()
    return ""


class TemporaryDispatchCondition(ValueError):
    """库存原子门禁观察到可由其他任务改变的运行条件。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        resources: Sequence[Mapping[str, str]] = (),
    ) -> None:
        """保存稳定等待码和中文原因。

        参数：``code`` 是调度等待原因；``message`` 是展示文本；``resources`` 是
        同一库存事务确认的实际阻塞资源。返回：无。异常：无；该内部异常由工站
        库存适配器转换为统一 ``StationResourceError``。
        """

        super().__init__(message)
        self.code = code
        self.message = message
        self.resources = tuple(dict(resource) for resource in resources)


def _inherits_lease(lease: sqlite3.Row, request: DispatchAdmissionRequest) -> bool:
    """仅明确声明的同任务前驱可交接，结果不明的租约绝不被继承绕过。"""
    return (
        str(lease["state"]) in {"prepared", "reserved", "running"}
        and str(lease["task_uuid"]) == request.task_uuid
        and str(lease["lock_key"]) in request.preheld_lock_keys
        and str(lease["job_uuid"]) in request.preheld_job_uuids
    )


def _active_resource_conflict(
    active: Sequence[sqlite3.Row],
    request: DispatchAdmissionRequest,
) -> DispatchAdmissionDecision | None:
    """首次准入和重准备使用相同的资源冲突与继承判断。

    ``preheld`` 是连续区间已经保持物理占用的证明，不是忽略普通冲突的提示。
    因此前驱 Lease 缺失、已释放或状态不明都属于持久事实断裂，必须在签发任何
    新 Claim/Fence 前关闭失败；共同作用域仍可由多条匹配 Lease 共同证明。
    """
    if request.preheld_lock_keys and not request.preheld_job_uuids:
        raise DispatchAdmissionConflict("连续区间预持有资源缺少前一 Job 身份")
    inherited_keys = {
        str(lease["lock_key"])
        for lease in active
        if _inherits_lease(lease, request)
    }
    missing_preheld_keys = sorted(
        set(request.preheld_lock_keys) - inherited_keys
    )
    if missing_preheld_keys:
        raise DispatchAdmissionConflict(
            "连续区间预持有资源缺少活动前驱租约："
            + "、".join(missing_preheld_keys)
        )
    requested = {resource.lock_key for resource in request.resources}
    for lease in active:
        if conflicting_resource_lock_keys(
            requested, {str(lease["lock_key"])}
        ) and not _inherits_lease(lease, request):
            return DispatchAdmissionDecision(
                wait_code="resource_claimed",
                wait_message=f"资源 {lease['lock_key']} 已由其他作业申领",
                blocking_task_uuid=str(lease["task_uuid"]),
                blocking_job_uuid=str(lease["job_uuid"]),
            )
    return None


def _active_ingress_resource_conflict(
    connection: sqlite3.Connection,
    request: DispatchAdmissionRequest,
) -> DispatchAdmissionDecision | None:
    """阻止普通 Job 抢占入口运输从预留到终态持续持有的资源。"""

    requested = {resource.lock_key for resource in request.resources}
    for row in connection.execute("SELECT lock_key FROM station_handoff_resource WHERE active=1").fetchall():
        if conflicting_resource_lock_keys(requested, {str(row["lock_key"])}):
            return DispatchAdmissionDecision(
                wait_code="station_handoff_protected",
                wait_message="物料正处于跨站交接冻结或 incoming 隔离",
            )
    from unilabos.app.scheduler.inventory.handoff_pickup import source_location_holds
    if any(conflicting_resource_lock_keys(requested,{row["lock_key"]}) for row in source_location_holds(connection)):
        return DispatchAdmissionDecision(wait_code="station_handoff_protected",wait_message="来源库位仍由原交接保护，等待可信取货或物理结算")
    rows = connection.execute(
        "SELECT reservation_uuid,lock_key "
        "FROM station_ingress_reservation_resource WHERE active=1 "
        "ORDER BY reservation_uuid,lock_key"
    ).fetchall()
    for row in rows:
        lock_key = str(row["lock_key"])
        if conflicting_resource_lock_keys(requested, {lock_key}):
            return DispatchAdmissionDecision(
                wait_code="station_ingress_reserved",
                wait_message=f"资源 {lock_key} 已由入口运输预留",
            )
    return None


def _active_task_material_claim_conflict(
    connection: sqlite3.Connection,
    request: DispatchAdmissionRequest,
) -> DispatchAdmissionDecision | None:
    """阻止动作越过其他 Task 的活动任务级物料独占。

    Inventory 的 ``inventory_material_source_binding`` 是来源准入已经提交的
    TaskMaterialClaim 镜像。动作是否自行声明 MaterialSource 与这里无关：只要
    完整派发资源描述引用了该物料，其他 Task 就必须等待；同 Task 的多个动作
    仍交给 JobActiveUse 的普通 Lease 逐节点互斥。
    """

    from unilabos.app.scheduler.inventory.material_inputs import material_input_blocker

    if request.material_input_receipt is not None:
        from unilabos.app.scheduler.inventory.material_inputs import MaterialInputError, StationMaterialInputAuthority

        receipt = request.material_input_receipt
        row = connection.execute("SELECT context_json FROM station_material_input WHERE invocation_uuid=?",
                                 (receipt.get("station_invocation_uuid", ""),)).fetchone()
        try:
            if row is None or receipt.get("local_task_uuid") != request.task_uuid:
                raise MaterialInputError("原输入占用缺失或任务身份不匹配")
            StationMaterialInputAuthority._verify(connection, json.loads(row["context_json"])["package"], dict(receipt))
        except (MaterialInputError, ValueError, KeyError, TypeError):
            return DispatchAdmissionDecision(wait_code="material_input_missing",
                                             wait_message="原物料输入占用不能核实，禁止重新创建执行权限")
    from unilabos.app.scheduler.inventory.outbound import outbound_blocker
    outbound = outbound_blocker(connection, [resource.lock_key for resource in request.resources])
    if outbound is not None:
        return DispatchAdmissionDecision(wait_code="task_material_claimed", wait_message="产物仍由出站交接责任保护",
                                         blocking_task_uuid=str(outbound["workflow_id"]))
    blocker = material_input_blocker(
        connection, tuple(resource.lock_key for resource in request.resources), task_uuid=request.task_uuid,
    )
    if blocker is not None:
        return DispatchAdmissionDecision(
            wait_code="task_material_claimed", wait_message="物料已为中心调用固定的本地任务保留",
            blocking_task_uuid=str(blocker["local_task_uuid"]),
        )
    from unilabos.app.scheduler.inventory.custody import custody_blocker_for_keys
    blocker = custody_blocker_for_keys(connection, [resource.lock_key for resource in request.resources],
                                       task_uuid=request.task_uuid,
                                       site_owner_uuids=_dispatch_site_owners(connection, request))
    if blocker is None:
        return None
    return DispatchAdmissionDecision(wait_code="task_material_claimed",
        wait_message="实际载体或其内容已由其他任务全程独占", blocking_task_uuid=str(blocker["workflow_id"]))


def _dispatch_site_owners(connection: sqlite3.Connection, request: DispatchAdmissionRequest) -> tuple[str, ...]:
    """从本次物理操作条件识别库位所属载体，执行锁及 Fence 保持完整。

    条件和库位事实在同一签发事务内校验。没有物理操作条件的普通物料键，
    以及本次实际搬动或加工的载体，仍按完整子树检查。
    """
    owners: set[str] = set()
    materials: set[str] = set()
    for resource in request.resources:
        if resource.scope == "material_site" and resource.site_uuid in request.custody_endpoint_site_uuids:
            owners.add(resource.material_uuid)
    for condition in (request.transfer, request.inventory_relocation):
        if condition is not None:
            owners.update((condition.source_owner_material_uuid, condition.target_owner_material_uuid))
            materials.add(condition.material_uuid)
    if request.transfer is not None:
        owners.add(request.transfer.executor_material_uuid)
        # 拆分 pick/place 时，最终目标库位在 pick 阶段就预持有。
        for site_uuid in request.reserved_target_site_uuids:
            row = connection.execute("SELECT material_uuid FROM site WHERE uuid=? AND deleted_at IS NULL",
                                     (site_uuid,)).fetchone()
            if row is not None:
                owners.add(str(row["material_uuid"]))
    if request.operate_in_place is not None:
        for condition in _operate_conditions(request.operate_in_place):
            owners.add(condition.site_owner_material_uuid)
            materials.add(condition.material_uuid)
    return tuple(sorted(owners - materials))


def _admission_fencing_token(
    connection: sqlite3.Connection,
    lock_key: str,
    active: Sequence[sqlite3.Row],
    request: DispatchAdmissionRequest,
    *,
    now: str,
) -> int:
    """共同外层所有权复用 fence，不能使仍在运行的并行命令失效。"""
    if lock_key in request.shared_scope_lock_keys:
        inherited = {
            int(row["fencing_token"])
            for row in active
            if str(row["lock_key"]) == lock_key and _inherits_lease(row, request)
        }
        if len(inherited) > 1:
            raise DispatchAdmissionConflict("共同范围的活动资源 fence 不一致")
        if inherited:
            return inherited.pop()
    return _next_fencing_token(connection, lock_key, now=now)


def _validate_dispatch_conditions(
    connection: sqlite3.Connection,
    request: DispatchAdmissionRequest,
) -> None:
    """每次签发凭据都重查资源及全部物理前置条件。"""
    _validate_resource_facts(connection, request.resources)
    _validate_reserved_targets(connection, request)
    if request.transfer is not None:
        _validate_transfer_conditions(connection, request)
    if request.inventory_relocation is not None:
        _validate_inventory_relocation_conditions(connection, request)
    if request.operate_in_place is not None:
        _validate_operate_in_place_conditions(connection, request)
    if request.aliquot is not None:
        _validate_aliquot_conditions(connection, request)


def _replay_permit(
    connection: sqlite3.Connection,
    existing: sqlite3.Row,
    request: DispatchAdmissionRequest,
) -> DispatchPermit:
    """验证同一次 Job 尝试的重放请求并恢复原 Permit。

    参数：库存事务、既有 Claim 和规范请求。返回：原 Claim/Fence 凭据。异常：
    效果、参数、预期变化、资源集合漂移或 Claim 已释放时抛冲突。
    """

    _assert_replay_matches(existing, request)
    if str(existing["state"]) == "released":
        raise DispatchAdmissionConflict("已释放的库存 Claim 不能重新派发")
    fence_rows = connection.execute(
        "SELECT lock_key,fencing_token FROM station_execution_lock_lease "
        "WHERE claim_uuid=? ORDER BY lock_key",
        (existing["claim_uuid"],),
    ).fetchall()
    return DispatchPermit(
        effect_uuid=request.effect_uuid,
        claim_uuid=str(existing["claim_uuid"]),
        task_uuid=request.task_uuid,
        job_uuid=request.job_uuid,
        attempt=request.attempt,
        parameter_hash=request.parameter_hash,
        expected_change_set=dict(request.expected_change_set),
        fences=tuple(
            DispatchFence(str(row["lock_key"]), int(row["fencing_token"]))
            for row in fence_rows
        ),
    )


def _reprepare_released_permit(
    connection: sqlite3.Connection,
    *,
    existing: sqlite3.Row,
    request: DispatchAdmissionRequest,
) -> DispatchAdmissionDecision:
    """重新准备从未提交到物理边界、但被后续门禁释放的同一 Permit。

    参数：库存事务、released 且 committed_at 为空的原 Claim、同一 Job 尝试请求。
    返回：条件仍满足时复用 Claim/effect，共同范围继承 Fence，其余签发新 Fence；冲突时等待。
    异常：请求漂移、库存条件损坏或 SQLite 错误原样传播。
    """

    _assert_replay_matches(existing, request)
    active = connection.execute(
        """
        SELECT lease.*, claim.task_uuid, claim.job_uuid
        FROM station_execution_lock_lease lease
        JOIN station_execution_claim claim USING(claim_uuid)
        WHERE lease.state IN ('prepared', 'reserved', 'running', 'uncertain')
          AND lease.claim_uuid <> ?
        ORDER BY lease.acquired_at, lease.claim_uuid, lease.lock_key
        """,
        (existing["claim_uuid"],),
    ).fetchall()
    conflict = _active_resource_conflict(active, request)
    if conflict is not None:
        return conflict
    ingress_conflict = _active_ingress_resource_conflict(connection, request)
    if ingress_conflict is not None:
        return ingress_conflict
    task_material_conflict = _active_task_material_claim_conflict(
        connection,
        request,
    )
    if task_material_conflict is not None:
        return task_material_conflict
    _validate_dispatch_conditions(connection, request)
    now = _utc_now()
    claim_uuid = str(existing["claim_uuid"])
    connection.execute(
        """
        UPDATE station_execution_claim
        SET state='prepared', acquired_at=?, released_at=NULL, update_time=?
        WHERE claim_uuid=? AND state='released' AND committed_at IS NULL
        """,
        (now, now, claim_uuid),
    )
    fences: list[DispatchFence] = []
    for resource in request.resources:
        token = _admission_fencing_token(connection, resource.lock_key, active, request, now=now)
        changed = connection.execute(
            """
            UPDATE station_execution_lock_lease
            SET fencing_token=?, state='prepared', acquired_at=?,
                released_at=NULL, update_time=?
            WHERE claim_uuid=? AND lock_key=? AND state='released'
            """,
            (token, now, now, claim_uuid, resource.lock_key),
        ).rowcount
        if changed != 1:
            raise DispatchAdmissionConflict("重准备 Permit 的资源 Lease 不完整")
        fences.append(DispatchFence(resource.lock_key, token))
    return DispatchAdmissionDecision(
        permit=DispatchPermit(
            effect_uuid=request.effect_uuid,
            claim_uuid=claim_uuid,
            task_uuid=request.task_uuid,
            job_uuid=request.job_uuid,
            attempt=request.attempt,
            parameter_hash=request.parameter_hash,
            expected_change_set=dict(request.expected_change_set),
            fences=tuple(fences),
        )
    )


def _assert_replay_matches(
    existing: sqlite3.Row,
    request: DispatchAdmissionRequest,
) -> None:
    """验证同一 Job 尝试的稳定效果、参数和完整资源未漂移。"""

    expected_fields = {
        "effect_uuid": request.effect_uuid,
        "task_uuid": request.task_uuid,
        "parameter_hash": request.parameter_hash,
        "expected_change_set": _canonical_json(request.expected_change_set),
        "resource_keys": _canonical_json(
            sorted(resource.lock_key for resource in request.resources)
        ),
    }
    for field, expected in expected_fields.items():
        if str(existing[field]) != str(expected):
            raise DispatchAdmissionConflict(f"同一作业尝试的派发请求发生漂移：{field}")


def _next_fencing_token(
    connection: sqlite3.Connection,
    lock_key: str,
    *,
    now: str,
) -> int:
    """在库存事务内为一个资源分配严格递增的 Fence。

    参数：库存事务、规范锁键和统一时间。返回：新的正整数令牌。异常：SQLite
    错误原样传播，调用方必须与 Claim 插入处于同一事务。
    """

    connection.execute(
        """
        INSERT INTO station_execution_fence_counter(
            lock_key,last_fencing_token,update_time
        ) VALUES (?,1,?)
        ON CONFLICT(lock_key) DO UPDATE SET
            last_fencing_token=last_fencing_token+1,
            update_time=excluded.update_time
        """,
        (lock_key, now),
    )
    row = connection.execute(
        "SELECT last_fencing_token FROM station_execution_fence_counter "
        "WHERE lock_key=?",
        (lock_key,),
    ).fetchone()
    assert row is not None
    return int(row["last_fencing_token"])


def _canonical_json(value: Any) -> str:
    """把可 JSON 化值编码为稳定 UTF-8 文本。

    参数：``value`` 是预期变化或资源键集合。返回：按键排序且无多余空白的文本。
    异常：不可 JSON 化值抛 ``DispatchAdmissionConflict``。
    """

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as error:
        raise DispatchAdmissionConflict("派发请求包含不可序列化值") from error


def _utc_now() -> str:
    """返回库存准入事实使用的 UTC ISO 时间。"""

    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "AliquotDispatchCondition",
    "DispatchAdmissionConflict",
    "DispatchAdmissionDecision",
    "DispatchAdmissionRequest",
    "DispatchFence",
    "InventoryRelocationCondition",
    "DispatchPermit",
    "DispatchResource",
    "InventoryMutationConflict",
    "OperateInPlaceCondition",
    "TemporaryDispatchCondition",
    "TransferDispatchCondition",
    "acquire_dispatch_permit",
    "acquire_dispatch_permit_candidates",
    "assert_inventory_mutation_unclaimed",
    "assert_resource_keys_unclaimed",
    "migrate_dispatch_admission_schema",
    "release_preheld_dispatch_claims",
    "release_task_dispatch_permits",
    "release_unprojected_dispatch_permits",
    "retain_dispatch_permit_resources",
    "transition_dispatch_permit",
    "validate_active_dispatch_permit",
    "validate_physical_settlement_credentials",
]


def _validate_reserved_targets(
    connection: sqlite3.Connection, request: DispatchAdmissionRequest
) -> None:
    """在取得整组 Claim 的同一事务预留拆分搬运最终 Site，杜绝取料后才等待目标。"""
    for site_uuid in request.reserved_target_site_uuids:
        resource = next(
            (
                r
                for r in request.resources
                if r.scope == "material_site" and r.site_uuid == site_uuid
            ),
            None,
        )
        if resource is None:
            raise DispatchAdmissionConflict("pick 缺少最终目标 Site 的独立预留")
        transfer = request.transfer
        allowed_occupant = (
            transfer.material_uuid if transfer and transfer.source_site_uuid == site_uuid else ""
        )
        check_reserved_pick_site(connection, resource.material_uuid, site_uuid, allowed_occupant)


def check_reserved_pick_site(
    connection: sqlite3.Connection, owner_uuid: str, site_uuid: str, allowed_occupant: str,
) -> None:
    """Check the final place endpoint without acquiring a claim or changing inventory."""
    row = connection.execute(
        "SELECT occupied_material_uuid FROM site WHERE uuid=? AND material_uuid=? AND deleted_at IS NULL",
        (site_uuid, owner_uuid),
    ).fetchone()
    if row is None:
        raise DispatchAdmissionConflict("最终目标 Site 不存在或所属物料不匹配")
    occupant = row["occupied_material_uuid"]
    if occupant and occupant != allowed_occupant:
        raise TemporaryDispatchCondition(
            "site_occupied", f"取料前最终目标 Site {site_uuid} 已被物料 {occupant} 占用",
            resources=({"scope": "material_site", "material_uuid": owner_uuid, "site_uuid": site_uuid},),
        )
    if connection.execute(
        "SELECT 1 FROM station_ingress_reservation WHERE site_uuid=? AND site_active=1",
        (site_uuid,),
    ).fetchone():
        raise TemporaryDispatchCondition("site_ingress_reserved", f"最终目标 Site {site_uuid} 已被入口预留")
