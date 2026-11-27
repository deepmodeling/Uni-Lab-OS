"""以主物料驱动的装载期间设备托管（Device Tenancy）持久模型。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from unilabos.registry.action_resource_contract import operate_in_place_parameters
from unilabos.workflow.resource_lock_key import device_lock_key, material_lock_key, site_lock_key
from unilabos.workflow.store import StoreConflict, utc_now


@dataclass(frozen=True, slots=True)
class DeviceTenancyDecision:
    """一次设备托管预处理的准入结果和阻塞身份。"""

    acquired: bool
    blocking_task_uuid: str | None = None
    blocking_job_uuid: str | None = None


def normalize_device_tenancy_transition(
    value: Mapping[str, Any] | None,
) -> dict[str, str] | None:
    """校验并规范一个作业的主物料设备托管转换。"""

    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise StoreConflict("设备托管转换必须是对象")
    allowed = {
        "mode",
        "material_uuid",
        "acquire_device_lock_key",
        "release_device_lock_key",
    }
    if set(value) - allowed or value.get("mode") != "task_while_loaded":
        raise StoreConflict("设备托管转换模式或字段非法")
    material_uuid = str(value.get("material_uuid") or "").strip()
    acquire_key = str(value.get("acquire_device_lock_key") or "").strip()
    release_key = str(value.get("release_device_lock_key") or "").strip()
    if not material_uuid or not (acquire_key or release_key):
        raise StoreConflict("设备托管转换缺少主物料或设备")
    if acquire_key and not acquire_key.startswith("/devices/"):
        raise StoreConflict("设备托管取得键不是规范设备键")
    if release_key and not release_key.startswith("/devices/"):
        raise StoreConflict("设备托管释放键不是规范设备键")
    if acquire_key and acquire_key == release_key:
        raise StoreConflict("设备托管不能同时取得并释放同一设备")
    return {
        "mode": "task_while_loaded",
        "material_uuid": material_uuid,
        "acquire_device_lock_key": acquire_key,
        "release_device_lock_key": release_key,
    }


def check_device_tenancy(
    connection: sqlite3.Connection,
    *,
    task_uuid: str,
    job_uuid: str,
    transition: Mapping[str, Any] | None,
) -> DeviceTenancyDecision:
    """只读检查设备托管转换能否与现有 Task 托管事实兼容。"""

    normalized = normalize_device_tenancy_transition(transition)
    if normalized is None:
        return DeviceTenancyDecision(acquired=True)
    material_uuid = normalized["material_uuid"]
    release_key = normalized["release_device_lock_key"]
    if release_key:
        source = connection.execute(
            """
            SELECT * FROM task_device_tenancy
            WHERE material_uuid = ? AND device_lock_key = ? AND state = 'active'
            """,
            (material_uuid, release_key),
        ).fetchone()
        if source is None or str(source["workflow_task_uuid"]) != task_uuid:
            raise StoreConflict("作业准备释放的设备并未由本 Task 主物料托管")
    acquire_key = normalized["acquire_device_lock_key"]
    if not acquire_key:
        return DeviceTenancyDecision(acquired=True)
    blocker = connection.execute(
        """
        SELECT workflow_task_uuid, acquired_by_job_uuid
        FROM task_device_tenancy
        WHERE state = 'active'
          AND (device_lock_key = ? OR material_uuid = ?)
          AND workflow_task_uuid <> ?
        ORDER BY acquired_at, uuid LIMIT 1
        """,
        (acquire_key, material_uuid, task_uuid),
    ).fetchone()
    if blocker is None:
        return DeviceTenancyDecision(acquired=True)
    blocker_task_uuid = str(blocker["workflow_task_uuid"])
    wait_edges: dict[str, set[str]] = {}
    for row in connection.execute(
        """
        SELECT waiter.workflow_task_uuid AS waiting_task_uuid,
               tenancy.workflow_task_uuid AS owning_task_uuid
        FROM execution_lock_waiter AS waiter
        JOIN task_device_tenancy AS tenancy
          ON tenancy.device_lock_key = waiter.lock_key
         AND tenancy.state = 'active'
        WHERE waiter.state = 'waiting'
          AND waiter.workflow_task_uuid <> tenancy.workflow_task_uuid
        """
    ).fetchall():
        wait_edges.setdefault(str(row["waiting_task_uuid"]), set()).add(
            str(row["owning_task_uuid"])
        )
    wait_edges.setdefault(task_uuid, set()).add(blocker_task_uuid)
    pending = [blocker_task_uuid]
    visited: set[str] = set()
    while pending:
        current = pending.pop()
        if current == task_uuid:
            raise StoreConflict(
                "检测到 Task 间 H→N 设备托管等待环路，拒绝建立第二条等待边"
            )
        if current in visited:
            continue
        visited.add(current)
        pending.extend(sorted(wait_edges.get(current, ())))
    return DeviceTenancyDecision(
        acquired=False,
        blocking_task_uuid=blocker_task_uuid,
        blocking_job_uuid=str(blocker["acquired_by_job_uuid"]),
    )


def require_active_device_tenancy(
    connection: sqlite3.Connection,
    *,
    task_uuid: str,
    requirement: Mapping[str, Any] | None,
) -> None:
    """证明原位操作的物料与设备正由同一 Task 长期托管。"""

    if requirement is None:
        return
    if not isinstance(requirement, Mapping) or set(requirement) != {
        "material_uuid",
        "device_lock_key",
    }:
        raise StoreConflict("原位操作设备托管要求字段非法")
    material_uuid = str(requirement.get("material_uuid") or "").strip()
    device_lock_key = str(requirement.get("device_lock_key") or "").strip()
    if not material_uuid or not device_lock_key.startswith("/devices/"):
        raise StoreConflict("原位操作设备托管要求缺少物料或规范设备键")
    owned = connection.execute(
        """
        SELECT 1 FROM task_device_tenancy
        WHERE workflow_task_uuid = ? AND material_uuid = ?
          AND device_lock_key = ? AND state = 'active'
        LIMIT 1
        """,
        (task_uuid, material_uuid, device_lock_key),
    ).fetchone()
    if owned is None:
        raise StoreConflict("原位操作物料并未由本 Task 托管在实际执行设备中")


def validate_native_operate_custody(
    *,
    task: Mapping[str, Any],
    job: Mapping[str, Any],
    requirement: Mapping[str, Any] | Sequence[Mapping[str, Any]],
    condition: Mapping[str, str] | Sequence[Mapping[str, str]],
    resolved_param: Mapping[str, Any],
    permit: Mapping[str, Any] | None,
    authority: Any,
    plan_id: str | None,
    interval_ids: Any,
) -> bool:
    """只对未声明长期托管的原位合同采用原生库存保管证明。"""
    plan = task.get("execution_plan")
    if isinstance(plan, str):
        plan = json.loads(plan)
    if not isinstance(plan, Mapping):
        return False
    node = next((item for item in plan.get("nodes", ()) if item.get("uuid") == job["workflow_node_uuid"]), None)
    contract = node.get("action_resource_contract", {}) if node else {}
    operation = contract.get("operate_in_place")
    if not isinstance(operation, Mapping):
        return False
    if "material_param" in operation and (contract.get("version") != 2 or not node.get("inventory_resource_lock_keys")):
        # 旧单物料合同、升级前未冻结库存锁的任务仍须证明长期设备托管。
        return False
    if contract.get("device_tenancy") is not None:
        return False
    if permit is None:
        raise StoreConflict("原生原位操作缺少库存 Permit")
    parameter_hash = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(resolved_param, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
    )
    if permit["parameter_hash"] != parameter_hash:
        raise StoreConflict("原生原位保管证明与最终参数哈希不一致")

    try:
        parameters = operate_in_place_parameters(operation)
    except ValueError as error:
        raise StoreConflict("原生原位保管证明的物料合同损坏") from error
    conditions = [condition] if isinstance(condition, Mapping) else condition
    requirements = [requirement] if isinstance(requirement, Mapping) else requirement
    if (
        not isinstance(conditions, (list, tuple))
        or not isinstance(requirements, (list, tuple))
        or len(conditions) != len(parameters)
        or len(requirements) != len(parameters)
    ):
        raise StoreConflict("原生原位保管证明缺少完整物料集合")
    required_keys = set()
    materials = set()
    devices = set()
    for parameter, fact, required in zip(parameters, conditions, requirements, strict=True):
        if not isinstance(fact, Mapping) or not isinstance(required, Mapping):
            raise StoreConflict("原生原位保管证明字段无效")
        value = resolved_param.get(parameter)
        material = str(value.get("uuid") or "") if isinstance(value, Mapping) else str(value or "")
        device_key = device_lock_key(fact["device_material_uuid"])
        if (
            not material
            or material in materials
            or fact.get("material_uuid") != material
            or required.get("material_uuid") != material
            or required.get("device_lock_key") != device_key
        ):
            raise StoreConflict("原生原位保管证明与冻结物料合同不一致")
        materials.add(material)
        devices.add(device_key)
        required_keys.update(
            {
                material_lock_key(material),
                device_key,
                site_lock_key(fact["site_owner_material_uuid"], fact["site_uuid"]),
            }
        )
    if len(devices) != 1:
        raise StoreConflict("原生原位操作物料不属于同一设备")
    frozen = plan.get("resource_plan", {})
    resources = {item["resource_id"]: item["canonical_key"] for item in frozen.get("resources", ())}
    selected = set(interval_ids or ())
    declared = {item["interval_id"]: item for item in frozen.get("intervals", ())}
    if (
        plan_id != frozen.get("plan_id")
        or not selected
        or not selected <= declared.keys()
        or not required_keys <= set(node.get("inventory_resource_lock_keys", ()))
    ):
        raise StoreConflict("原生原位保管证明缺少冻结库存资源区间")
    covered = {
        resources[declared[identity]["resource_id"]]
        for identity in selected
        if job["workflow_node_uuid"] in declared[identity]["node_uuids"]
    }
    if not required_keys <= covered or not required_keys <= set(permit["fencing_tokens"]):
        raise StoreConflict("原生原位保管证明的资源区间或 Fence 不完整")
    validator = getattr(authority, "validate_native_operate_custody", None)
    if not callable(validator):
        raise StoreConflict("原生原位保管证明缺少库存权威复验能力")
    validator(
        condition=condition,
        permit=permit,
        task_uuid=str(task["uuid"]),
        job_uuid=str(job["uuid"]),
        attempt=int(job["attempt"]),
    )
    return True


def prepare_device_tenancy(
    connection: sqlite3.Connection,
    *,
    task_uuid: str,
    job_uuid: str,
    transition: Mapping[str, Any] | None,
) -> None:
    """在派发事务内持久化托管转换，并在装载开始前取得目标设备。"""

    normalized = normalize_device_tenancy_transition(transition)
    if normalized is None:
        return
    existing = connection.execute(
        "SELECT * FROM job_device_tenancy_transition WHERE workflow_node_job_uuid = ?",
        (job_uuid,),
    ).fetchone()
    expected = (
        task_uuid,
        normalized["material_uuid"],
        normalized["acquire_device_lock_key"] or None,
        normalized["release_device_lock_key"] or None,
    )
    if existing is not None:
        actual = (
            str(existing["workflow_task_uuid"]),
            str(existing["material_uuid"]),
            existing["acquire_device_lock_key"],
            existing["release_device_lock_key"],
        )
        if actual != expected:
            raise StoreConflict("作业设备托管转换发生变化")
        return
    decision = check_device_tenancy(
        connection,
        task_uuid=task_uuid,
        job_uuid=job_uuid,
        transition=normalized,
    )
    if not decision.acquired:
        raise StoreConflict("设备托管在准备阶段发生并发冲突")
    now = utc_now()
    acquire_key = normalized["acquire_device_lock_key"]
    if acquire_key:
        own = connection.execute(
            """
            SELECT * FROM task_device_tenancy
            WHERE state = 'active' AND workflow_task_uuid = ?
              AND material_uuid = ? AND device_lock_key = ?
            """,
            (task_uuid, normalized["material_uuid"], acquire_key),
        ).fetchone()
        if own is None:
            connection.execute(
                """
                INSERT INTO task_device_tenancy(
                    uuid, create_time, update_time, workflow_task_uuid,
                    material_uuid, device_lock_key, acquired_by_job_uuid,
                    released_by_job_uuid, state, acquired_at, released_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 'active', ?, NULL)
                """,
                (
                    str(uuid4()),
                    now,
                    now,
                    task_uuid,
                    normalized["material_uuid"],
                    acquire_key,
                    job_uuid,
                    now,
                ),
            )
    release_tenancy = connection.execute(
        "SELECT uuid FROM task_device_tenancy WHERE workflow_task_uuid=? AND material_uuid=? AND device_lock_key=? AND state='active'",
        (task_uuid, normalized["material_uuid"], normalized["release_device_lock_key"]),
    ).fetchone()
    connection.execute(
        """
        INSERT INTO job_device_tenancy_transition(
            workflow_node_job_uuid, workflow_task_uuid, material_uuid,
            acquire_device_lock_key, release_device_lock_key, status,
            create_time, update_time, settled_at, release_tenancy_uuid
        ) VALUES (?, ?, ?, ?, ?, 'prepared', ?, ?, NULL, ?)
        """,
        (job_uuid, *expected, now, now, release_tenancy["uuid"] if release_tenancy else None),
    )


def settle_device_tenancy(
    connection: sqlite3.Connection,
    *,
    job_uuid: str,
    succeeded: bool,
    not_started: bool = False,
) -> bool:
    """按明确物理结果结算托管转换；失败或未知时保留设备。"""

    transition = connection.execute(
        "SELECT * FROM job_device_tenancy_transition WHERE workflow_node_job_uuid = ?",
        (job_uuid,),
    ).fetchone()
    if transition is None:
        return False
    if str(transition["status"]) in {"settled", "retained", "reverted"}:
        return str(transition["status"]) == "retained"
    now = utc_now()
    if not_started:
        acquire_key = transition["acquire_device_lock_key"]
        if acquire_key is not None:
            connection.execute(
                """
                DELETE FROM task_device_tenancy
                WHERE state = 'active' AND acquired_by_job_uuid = ?
                  AND device_lock_key = ?
                """,
                (job_uuid, acquire_key),
            )
        status = "reverted"
    elif succeeded:
        release_key = transition["release_device_lock_key"]
        if release_key is not None:
            changed = connection.execute(
                """
                UPDATE task_device_tenancy
                SET state = 'released', released_by_job_uuid = ?,
                    released_at = ?, update_time = ?
                WHERE state = 'active' AND uuid = ?
                """,
                (
                    job_uuid,
                    now,
                    now,
                    transition["release_tenancy_uuid"],
                ),
            ).rowcount
            if changed != 1:
                released = connection.execute("SELECT state FROM task_device_tenancy WHERE uuid=?", (transition["release_tenancy_uuid"],)).fetchone()
                if released is None or released["state"] != "released":
                    raise StoreConflict("设备托管释放事实不存在或已变化")
                # 已人工释放的同一托管行只保留结果；不能释放之后新取得的托管。
        status = "settled"
    else:
        status = "retained"
    connection.execute(
        """
        UPDATE job_device_tenancy_transition
        SET status = ?, settled_at = ?, update_time = ?
        WHERE workflow_node_job_uuid = ? AND status = 'prepared'
        """,
        (status, now, now, job_uuid),
    )
    return status == "retained"


def active_task_device_tenancies(
    connection: sqlite3.Connection,
    *,
    task_uuid: str | None = None,
) -> list[dict[str, Any]]:
    """读取全部或指定 Task 的活动设备托管事实。"""

    if task_uuid is None:
        rows = connection.execute(
            "SELECT * FROM task_device_tenancy WHERE state = 'active' "
            "ORDER BY acquired_at, uuid"
        ).fetchall()
    else:
        rows = connection.execute(
            "SELECT * FROM task_device_tenancy "
            "WHERE state = 'active' AND workflow_task_uuid = ? "
            "ORDER BY acquired_at, uuid",
            (task_uuid,),
        ).fetchall()
    return [dict(row) for row in rows]


__all__ = [
    "DeviceTenancyDecision",
    "active_task_device_tenancies",
    "check_device_tenancy",
    "normalize_device_tenancy_transition",
    "prepare_device_tenancy",
    "require_active_device_tenancy",
    "settle_device_tenancy",
]
