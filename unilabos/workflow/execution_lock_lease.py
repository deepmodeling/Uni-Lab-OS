"""本地模式的持久作业执行占用（ExecutionLockLease）。

该模块只管理工作流数据库内的锁租约与等待顺序。库存数量、物料状态和库位
实体仍由库存库负责；两库之间没有被伪装成同一个原子事务。
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from unilabos.app.scheduler.models import priority_weight
from unilabos.app.scheduler.ordering import (
    DEFAULT_AGING_INTERVAL_SECONDS,
    aged_priority,
)
from unilabos.app.scheduler.resource_lock import conflicting_resource_lock_keys
from unilabos.workflow.execution_claim import (
    ensure_execution_claim,
    next_fencing_token,
    required_active_claim,
)
from unilabos.workflow.json_codec import decode_json_bytes, encode_json
from unilabos.workflow.resource_lock_key import (
    parse_canonical_resource_lock_key,
)
from unilabos.workflow.store import StoreConflict, utc_now
from unilabos.workflow.task_material_admission import (
    find_active_foreign_material_claim,
)

_SCOPES = frozenset({"device", "material", "material_site", "resource"})


def _validate_execution_lock_row(row: sqlite3.Row) -> None:
    """关闭式校验一条持久 Lease 的规范键与冗余物理身份。"""

    identity = parse_canonical_resource_lock_key(row["lock_key"])
    scope = str(row["scope"] or "")
    material_uuid = str(row["material_uuid"] or "") or None
    site_uuid = str(row["site_uuid"] or "") or None
    if (
        identity is None
        or identity.scope != scope
        or (
            scope == "resource"
            and (material_uuid is not None or site_uuid is not None)
        )
        or (
            scope != "resource"
            and (
                identity.material_uuid != material_uuid
                or identity.site_uuid != site_uuid
            )
        )
    ):
        raise StoreConflict("活动执行锁租约包含非规范或不一致的资源身份")


def _validated_active_execution_lock_rows(
    connection: sqlite3.Connection,
) -> list[sqlite3.Row]:
    """读取并验证全局活动 Lease，任何损坏事实均阻止继续签发。"""

    rows = connection.execute(
        """
        SELECT * FROM execution_lock_lease
        WHERE deleted_at IS NULL
          AND state IN ('reserved', 'running', 'uncertain')
        ORDER BY acquired_at ASC, uuid ASC
        """
    ).fetchall()
    for row in rows:
        _validate_execution_lock_row(row)
    return rows


def _normalize_interval_ids_by_lock(
    value: Mapping[str, Sequence[str]] | None,
    *,
    requested_keys: set[str],
    interval_ids: set[str],
) -> dict[str, set[str]]:
    """校验连续区间到锁键的精确归属，避免临时锁被错误续持。"""

    if not interval_ids:
        if value:
            raise StoreConflict("无连续区间时不能提供锁键区间映射")
        return {}
    if value is None:
        if len(requested_keys) == 1:
            only_key = next(iter(requested_keys))
            return {only_key: set(interval_ids)}
        raise StoreConflict("多资源连续区间缺少按锁键的区间映射")
    result: dict[str, set[str]] = {}
    for raw_key, raw_values in value.items():
        lock_key = str(raw_key or "").strip()
        if lock_key not in requested_keys:
            raise StoreConflict("连续区间映射包含当前作业未声明的资源")
        if not isinstance(raw_values, Sequence) or isinstance(raw_values, (str, bytes)):
            raise StoreConflict("连续区间映射的区间身份必须是序列")
        ids = {str(item).strip() for item in raw_values if str(item).strip()}
        if not ids or not ids <= interval_ids:
            raise StoreConflict("连续区间映射包含未声明的区间身份")
        result[lock_key] = ids
    if not result:
        raise StoreConflict("连续区间映射至少需要一个连续资源")
    if set().union(*result.values()) != interval_ids:
        raise StoreConflict("连续区间映射未覆盖全部区间身份")
    return result


@dataclass(frozen=True, slots=True)
class ExecutionLockRequest:
    """一个作业需要全有或全无取得的设备、物料或库位执行占用。"""

    lock_key: str
    scope: str
    material_uuid: str | None = None
    site_uuid: str | None = None


@dataclass(frozen=True, slots=True)
class ExecutionLockDecision:
    """持久占用准入结果、Claim 身份、栅栏与阻塞作业身份。"""

    acquired: bool
    blocking_task_uuid: str | None = None
    blocking_job_uuid: str | None = None
    claim_uuid: str | None = None
    fencing_tokens: tuple[tuple[str, int], ...] = ()


def normalize_execution_lock_requests(
    values: Sequence[Mapping[str, Any]] | None,
) -> tuple[ExecutionLockRequest, ...]:
    """校验、去重并稳定排序一组执行锁请求。

    参数：``values`` 是调度器产生的完整占用声明。返回：稳定、不可变的请求
    元组。异常：字段缺失、访问区域键非规范小写 token、UUID 非法或同键定义
    冲突时抛 ``StoreConflict``。该函数只规范业务合同，不写数据库。
    """

    normalized: dict[str, ExecutionLockRequest] = {}
    for value in values or ():
        if not isinstance(value, Mapping):
            raise StoreConflict("执行锁请求必须是对象")
        lock_key = str(value.get("lock_key") or "").strip()
        scope = str(value.get("scope") or "").strip()
        if not lock_key or scope not in _SCOPES:
            raise StoreConflict("执行锁请求缺少合法 lock_key 或 scope")
        material_uuid = str(value.get("material_uuid") or "").strip() or None
        site_uuid = str(value.get("site_uuid") or "").strip() or None
        key_identity = parse_canonical_resource_lock_key(lock_key)
        if key_identity is None or key_identity.scope != scope:
            raise StoreConflict("执行锁 scope 与规范 lock_key 不匹配")
        if scope == "resource":
            if material_uuid is not None or site_uuid is not None:
                raise StoreConflict("通用执行锁不能伪造物料或库位身份")
        else:
            if (
                material_uuid is not None
                and material_uuid != key_identity.material_uuid
            ) or (site_uuid is not None and site_uuid != key_identity.site_uuid):
                raise StoreConflict("执行锁物理身份与 canonical lock_key 不一致")
            material_uuid = key_identity.material_uuid
            site_uuid = key_identity.site_uuid
        request = ExecutionLockRequest(
            lock_key=lock_key,
            scope=scope,
            material_uuid=material_uuid,
            site_uuid=site_uuid,
        )
        previous = normalized.get(lock_key)
        if previous is not None and previous != request:
            raise StoreConflict(f"同一执行锁键的请求定义冲突：{lock_key}")
        normalized[lock_key] = request
    return tuple(normalized[key] for key in sorted(normalized))


def _find_interval_leases(
    connection: sqlite3.Connection,
    *,
    task_uuid: str,
    plan_id: str | None,
    interval_ids: set[str],
) -> list[sqlite3.Row]:
    """读取当前 Task 尚未到释放边界的活动区间租约。"""

    if not plan_id or not interval_ids:
        return []
    rows = connection.execute(
        """
        SELECT * FROM execution_lock_lease
        WHERE workflow_task_uuid=? AND deleted_at IS NULL
          AND state IN ('reserved','running')
        ORDER BY acquired_at ASC, uuid ASC
        """,
        (task_uuid,),
    ).fetchall()
    result: list[sqlite3.Row] = []
    for row in rows:
        metadata = _lease_metadata(row)
        if str(metadata.get("resource_plan_id") or "") != str(plan_id):
            continue
        raw_ids = metadata.get("resource_interval_ids")
        if not isinstance(raw_ids, Sequence) or isinstance(raw_ids, (str, bytes)):
            continue
        if interval_ids.intersection(str(value) for value in raw_ids):
            result.append(row)
    return result


def _validate_preheld_interval_leases(
    connection: sqlite3.Connection,
    *,
    task_uuid: str,
    job_uuid: str,
    preheld_lock_keys: set[str],
    preheld_job_uuids: set[str],
    inherited_by_key: Mapping[str, sqlite3.Row],
    interval_ids_by_lock: Mapping[str, set[str]],
) -> None:
    """证明声明的预持有资源仍由同 Task 的前驱区间 Lease 持有。"""

    if not preheld_lock_keys:
        return
    if not preheld_job_uuids:
        raise StoreConflict("连续区间预持有资源缺少前一 Job 身份")
    missing: list[str] = []
    for lock_key in sorted(preheld_lock_keys):
        row = inherited_by_key.get(lock_key)
        expected_interval_ids = interval_ids_by_lock.get(lock_key, set())
        if row is None or not expected_interval_ids:
            missing.append(lock_key)
            continue
        metadata = _lease_metadata(row)
        raw_interval_ids = metadata.get("resource_interval_ids")
        lease_interval_ids = (
            {str(value).strip() for value in raw_interval_ids if str(value).strip()}
            if isinstance(raw_interval_ids, Sequence)
            and not isinstance(raw_interval_ids, (str, bytes))
            else set()
        )
        acquired_by = _lease_acquired_by(row)
        predecessor_job_uuid = acquired_by
        if acquired_by == job_uuid:
            predecessor_job_uuid = str(metadata.get("handoff_from_job_uuid") or "")
        predecessor = connection.execute(
            "SELECT workflow_task_uuid FROM workflow_node_job WHERE uuid=? "
            "AND deleted_at IS NULL",
            (predecessor_job_uuid,),
        ).fetchone()
        if (
            predecessor_job_uuid not in preheld_job_uuids
            or predecessor is None
            or str(predecessor["workflow_task_uuid"]) != task_uuid
            or not expected_interval_ids.intersection(lease_interval_ids)
        ):
            missing.append(lock_key)
    if missing:
        raise StoreConflict(
            "连续区间预持有资源缺少匹配的活动前驱租约：" + "、".join(missing)
        )


def try_acquire_execution_locks(
    connection: sqlite3.Connection,
    *,
    task_uuid: str,
    job_uuid: str,
    requests: Sequence[Mapping[str, Any]] | None,
    claim_uuid: str | None = None,
    fencing_tokens: Mapping[str, int] | None = None,
    resource_plan_id: str | None = None,
    resource_interval_ids: Sequence[str] = (),
    resource_acquire_set_id: str | None = None,
    resource_interval_ids_by_lock: Mapping[str, Sequence[str]] | None = None,
    preheld_lock_keys: Sequence[str] | None = None,
    preheld_job_uuids: Sequence[str] | None = None,
    authoritative_permit: bool = False,
    aging_interval_seconds: float = DEFAULT_AGING_INTERVAL_SECONDS,
) -> ExecutionLockDecision:
    """在当前写事务中投影一个作业全有或全无的库存执行占用。

    参数：工作流事务、Task/Job 身份、完整锁请求，以及可选库存 Claim UUID 与
    Fence 映射。返回：取得/等待决定。异常：库存 Permit 与锁集合不一致、同次
    尝试漂移或数据库冲突时抛 ``StoreConflict``。未提供 Permit 仅用于隔离干跑
    和遗留投影测试；物理派发必须由组合根传入库存签发凭据。
    """

    normalized = normalize_execution_lock_requests(requests)
    task_material_blocker = find_active_foreign_material_claim(
        connection,
        task_uuid=task_uuid,
        material_uuids=tuple(
            request.material_uuid
            for request in normalized
            if request.material_uuid is not None
        ),
    )
    if task_material_blocker is not None:
        if authoritative_permit:
            raise StoreConflict(
                "库存 Permit 越过其他任务的活动物料独占："
                f"{task_material_blocker['material_uuid']}"
            )
        waiting_since = _ensure_waiters(
            connection,
            task_uuid=task_uuid,
            job_uuid=job_uuid,
            requests=normalized,
        )
        return _record_wait(
            connection,
            task_uuid=task_uuid,
            job_uuid=job_uuid,
            requests=normalized,
            blocking_task_uuid=str(
                task_material_blocker["workflow_task_uuid"]
            ),
            blocking_job_uuid=None,
            waiting_since=waiting_since,
        )
    provided_fences = dict(fencing_tokens or {})
    normalized_keys = {request.lock_key for request in normalized}
    declared_preheld_keys = {
        str(value).strip() for value in (preheld_lock_keys or ()) if str(value).strip()
    }
    declared_preheld_jobs = {
        str(value).strip() for value in (preheld_job_uuids or ()) if str(value).strip()
    }
    if not declared_preheld_keys <= normalized_keys:
        raise StoreConflict("连续区间预持有资源不在当前作业完整锁集合中")
    interval_ids = {str(value).strip() for value in resource_interval_ids if str(value).strip()}
    interval_ids_by_lock = _normalize_interval_ids_by_lock(
        resource_interval_ids_by_lock,
        requested_keys=normalized_keys,
        interval_ids=interval_ids,
    )
    inherited_rows = _find_interval_leases(
        connection,
        task_uuid=task_uuid,
        plan_id=resource_plan_id,
        interval_ids=interval_ids,
    )
    inherited_by_key: dict[str, sqlite3.Row] = {}
    for row in inherited_rows:
        lock_key = str(row["lock_key"])
        if lock_key in inherited_by_key:
            raise StoreConflict(f"连续区间存在重复活动租约：{lock_key}")
        inherited_by_key[lock_key] = row
    inherited_keys = set(inherited_by_key)
    if not inherited_keys <= normalized_keys:
        raise StoreConflict("连续区间继承的资源不在当前作业完整锁集合中")
    _validate_preheld_interval_leases(
        connection,
        task_uuid=task_uuid,
        job_uuid=job_uuid,
        preheld_lock_keys=declared_preheld_keys,
        preheld_job_uuids=declared_preheld_jobs,
        inherited_by_key=inherited_by_key,
        interval_ids_by_lock=interval_ids_by_lock,
    )
    provided_fence_keys = set(provided_fences)
    if claim_uuid is not None and provided_fence_keys not in (
        normalized_keys,
        normalized_keys - inherited_keys,
    ):
        raise StoreConflict("库存 Permit Fence 与完整执行锁集合不一致")
    if any(token <= 0 for token in provided_fences.values()):
        raise StoreConflict("库存 Permit Fence 必须是正整数")
    active_rows = _validated_active_execution_lock_rows(connection)
    if not normalized:
        claim = ensure_execution_claim(
            connection,
            task_uuid=task_uuid,
            job_uuid=job_uuid,
            resource_keys=(),
            claim_uuid=claim_uuid,
        )
        _clear_wait_reason(connection, task_uuid=task_uuid, job_uuid=job_uuid)
        return ExecutionLockDecision(
            acquired=True,
            claim_uuid=str(claim["claim_uuid"]),
        )

    own_keys = {str(row["lock_key"]) for row in active_rows if _lease_acquired_by(row) == job_uuid}
    own_keys |= inherited_keys
    requested_keys = {request.lock_key for request in normalized}
    if own_keys and not inherited_keys:
        if own_keys != requested_keys:
            raise StoreConflict(f"作业持久执行锁集合发生变化：{job_uuid}")
        claim = required_active_claim(connection, job_uuid=job_uuid)
        if claim_uuid is not None and str(claim["claim_uuid"]) != claim_uuid:
            raise StoreConflict(f"持久 Claim 与库存 Permit 不一致：{job_uuid}")
        fencing_tokens = tuple(
            sorted(
                (
                    str(row["lock_key"]),
                    int(row["fencing_token"]),
                )
                for row in active_rows
                if _lease_acquired_by(row) == job_uuid
            )
        )
        _release_waiters(connection, job_uuid=job_uuid)
        _clear_wait_reason(connection, task_uuid=task_uuid, job_uuid=job_uuid)
        return ExecutionLockDecision(
            acquired=True,
            claim_uuid=str(claim["claim_uuid"]),
            fencing_tokens=fencing_tokens,
        )

    enqueued_at = (
        utc_now()
        if authoritative_permit
        else _ensure_waiters(
            connection,
            task_uuid=task_uuid,
            job_uuid=job_uuid,
            requests=normalized,
        )
    )
    blockers = (
        []
        if authoritative_permit
        else [
            row
            for row in active_rows
            if _lease_acquired_by(row) != job_uuid
            and row["workflow_node_job_uuid"] != job_uuid
            and str(row["lock_key"]) not in inherited_keys
            and conflicting_resource_lock_keys(
                requested_keys - inherited_keys,
                {str(row["lock_key"])},
            )
        ]
    )
    device_keys = tuple(request.lock_key for request in normalized if request.scope == "device")
    tenancy_blocker = None
    owned_tenancy_keys: set[str] = set()
    if device_keys:
        placeholders = ",".join("?" for _ in device_keys)
        owned_tenancy_keys = {
            str(row["device_lock_key"])
            for row in connection.execute(
                f"""
                SELECT device_lock_key FROM task_device_tenancy
                WHERE state = 'active'
                  AND device_lock_key IN ({placeholders})
                  AND workflow_task_uuid = ?
                """,
                (*device_keys, task_uuid),
            ).fetchall()
        }
        tenancy_blocker = (
            connection.execute(
                f"""
            SELECT workflow_task_uuid, acquired_by_job_uuid
            FROM task_device_tenancy
            WHERE state = 'active'
              AND device_lock_key IN ({placeholders})
              AND workflow_task_uuid <> ?
            ORDER BY acquired_at, uuid LIMIT 1
            """,
                (*device_keys, task_uuid),
            ).fetchone()
            if not authoritative_permit
            else None
        )
    if blockers:
        blocker = blockers[0]
        return _record_wait(
            connection,
            task_uuid=task_uuid,
            job_uuid=job_uuid,
            requests=normalized,
            blocking_task_uuid=str(blocker["workflow_task_uuid"]),
            blocking_job_uuid=str(blocker["workflow_node_job_uuid"]),
            waiting_since=enqueued_at,
        )
    if tenancy_blocker is not None:
        return _record_wait(
            connection,
            task_uuid=task_uuid,
            job_uuid=job_uuid,
            requests=normalized,
            blocking_task_uuid=str(tenancy_blocker["workflow_task_uuid"]),
            blocking_job_uuid=str(tenancy_blocker["acquired_by_job_uuid"]),
            waiting_since=enqueued_at,
        )

    current_task = connection.execute(
        "SELECT create_time, priority FROM workflow_task WHERE uuid = ?",
        (task_uuid,),
    ).fetchone()
    if current_task is None:
        raise StoreConflict(f"执行锁所属任务不存在：{task_uuid}")
    older = (
        None
        if authoritative_permit
        else _older_conflicting_waiter(
            connection,
            job_uuid=job_uuid,
            current_enqueued_at=enqueued_at,
            current_task_create_time=str(current_task["create_time"]),
            current_task_uuid=task_uuid,
            current_priority=priority_weight(current_task["priority"]),
            requested_keys=requested_keys - owned_tenancy_keys - inherited_keys,
            aging_interval_seconds=aging_interval_seconds,
        )
    )
    if older is not None:
        return _record_wait(
            connection,
            task_uuid=task_uuid,
            job_uuid=job_uuid,
            requests=normalized,
            blocking_task_uuid=older[0],
            blocking_job_uuid=older[1],
            waiting_since=enqueued_at,
        )

    acquired_at = utc_now()
    claim = ensure_execution_claim(
        connection,
        task_uuid=task_uuid,
        job_uuid=job_uuid,
        resource_keys=tuple(sorted(requested_keys)),
        acquired_at=acquired_at,
        claim_uuid=claim_uuid,
    )
    claim_uuid = str(claim["claim_uuid"])
    fencing_tokens: list[tuple[str, int]] = []
    old_claims: set[str] = set()
    for inherited in inherited_rows:
        previous_job = connection.execute(
            "SELECT status FROM workflow_node_job WHERE uuid=?",
            (inherited["workflow_node_job_uuid"],),
        ).fetchone()
        if (
            authoritative_permit
            and previous_job is not None
            and previous_job["status"] not in {"succeeded", "skipped"}
        ):
            # 在途共同祖先仍由原作业镜像持有；库存 Permit 复用同一栅栏，不能把
            # 原作业的锁行转移给兄弟作业而破坏原物理完成凭据。
            continue
        old_claim = str(inherited["claim_uuid"] or "")
        if old_claim and old_claim != claim_uuid:
            old_claims.add(old_claim)
        metadata = _lease_metadata(inherited)
        metadata.update(
            {
                "acquired_by_job_uuid": job_uuid,
                "resource_plan_id": str(resource_plan_id or metadata.get("resource_plan_id") or ""),
                "resource_interval_ids": sorted(
                    interval_ids_by_lock.get(str(inherited["lock_key"]), ())
                ),
                "resource_acquire_set_id": str(
                    resource_acquire_set_id or metadata.get("resource_acquire_set_id") or ""
                ),
                "handoff_from_job_uuid": str(inherited["workflow_node_job_uuid"]),
            }
        )
        if authoritative_permit:
            metadata["authority"] = "inventory_dispatch_permit"
        connection.execute(
            """
            UPDATE execution_lock_lease
            SET workflow_node_job_uuid=?, claim_uuid=?, meta_data=?,
                fencing_token=?, update_time=?
            WHERE uuid=? AND state IN ('reserved','running','uncertain')
            """,
            (
                job_uuid,
                claim_uuid,
                encode_json(metadata, sort_keys=True).decode("utf-8"),
                int(provided_fences.get(str(inherited["lock_key"]), inherited["fencing_token"])),
                acquired_at,
                inherited["uuid"],
            ),
        )
    for request in normalized:
        if request.lock_key in inherited_by_key:
            inherited = inherited_by_key[request.lock_key]
            fencing_tokens.append(
                (
                    request.lock_key,
                    int(provided_fences.get(request.lock_key, inherited["fencing_token"])),
                )
            )
            continue
        fencing_token = provided_fences.get(request.lock_key)
        if fencing_token is None:
            fencing_token = next_fencing_token(
                connection,
                lock_key=request.lock_key,
                now=acquired_at,
            )
        fencing_tokens.append((request.lock_key, fencing_token))
        metadata = {
            "semantic_scope": request.scope,
            "acquired_by_job_uuid": job_uuid,
            "resource_plan_id": str(resource_plan_id or ""),
            "resource_interval_ids": sorted(interval_ids_by_lock.get(request.lock_key, ())),
            "resource_acquire_set_id": str(resource_acquire_set_id or ""),
        }
        if authoritative_permit:
            metadata["authority"] = "inventory_dispatch_permit"
        connection.execute(
            """
            INSERT INTO execution_lock_lease(
                uuid, create_time, update_time, deleted_at, description,
                meta_data, workflow_task_uuid, workflow_node_job_uuid,
                lock_key, scope, material_uuid, site_uuid, state,
                acquired_at, released_at, claim_uuid, fencing_token
            ) VALUES (?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?, ?, ?,
                      'reserved', ?, NULL, ?, ?)
            """,
            (
                str(uuid4()),
                acquired_at,
                acquired_at,
                encode_json(metadata, sort_keys=True).decode("utf-8"),
                task_uuid,
                job_uuid,
                request.lock_key,
                request.scope,
                request.material_uuid,
                request.site_uuid,
                acquired_at,
                claim_uuid,
                fencing_token,
            ),
        )
    for old_claim in old_claims:
        remaining = connection.execute(
            """
            SELECT 1 FROM execution_lock_lease
            WHERE claim_uuid=? AND deleted_at IS NULL
              AND state IN ('reserved','running','uncertain') LIMIT 1
            """,
            (old_claim,),
        ).fetchone()
        if remaining is None:
            connection.execute(
                """
                UPDATE execution_claim
                SET state='released', released_at=?, update_time=?
                WHERE claim_uuid=? AND state IN ('reserved','running','uncertain')
                """,
                (acquired_at, acquired_at, old_claim),
            )
    _release_waiters(connection, job_uuid=job_uuid, released_at=acquired_at)
    _clear_wait_reason(connection, task_uuid=task_uuid, job_uuid=job_uuid)
    return ExecutionLockDecision(
        acquired=True,
        claim_uuid=claim_uuid,
        fencing_tokens=tuple(fencing_tokens),
    )


def mirror_execution_locks_from_permit(
    connection: sqlite3.Connection,
    *,
    task_uuid: str,
    job_uuid: str,
    requests: Sequence[Mapping[str, Any]] | None,
    claim_uuid: str,
    fencing_tokens: Mapping[str, int],
    resource_plan_id: str | None = None,
    resource_interval_ids: Sequence[str] = (),
    resource_acquire_set_id: str | None = None,
    resource_interval_ids_by_lock: Mapping[str, Sequence[str]] | None = None,
    preheld_lock_keys: Sequence[str] | None = None,
    preheld_job_uuids: Sequence[str] | None = None,
) -> ExecutionLockDecision:
    """只镜像库存 Permit，不在工作流库再次进行资源仲裁。"""

    normalized = normalize_execution_lock_requests(requests)
    task_material_blocker = find_active_foreign_material_claim(
        connection,
        task_uuid=task_uuid,
        material_uuids=tuple(
            request.material_uuid
            for request in normalized
            if request.material_uuid is not None
        ),
    )
    if task_material_blocker is not None:
        raise StoreConflict(
            "库存 Permit 越过其他任务的活动物料独占："
            f"{task_material_blocker['material_uuid']}"
        )
    provided_fences = {str(key): int(value) for key, value in fencing_tokens.items()}
    requested_keys = {request.lock_key for request in normalized}
    if resource_interval_ids or preheld_lock_keys:
        return try_acquire_execution_locks(
            connection,
            task_uuid=task_uuid,
            job_uuid=job_uuid,
            requests=requests,
            claim_uuid=claim_uuid,
            fencing_tokens=provided_fences,
            resource_plan_id=resource_plan_id,
            resource_interval_ids=resource_interval_ids,
            resource_acquire_set_id=resource_acquire_set_id,
            resource_interval_ids_by_lock=resource_interval_ids_by_lock,
            preheld_lock_keys=preheld_lock_keys,
            preheld_job_uuids=preheld_job_uuids,
            authoritative_permit=True,
        )
    if not claim_uuid or set(provided_fences) != requested_keys:
        raise StoreConflict("库存 Permit Fence 与完整执行锁集合不一致")
    if any(token <= 0 for token in provided_fences.values()):
        raise StoreConflict("库存 Permit Fence 必须是正整数")
    _validated_active_execution_lock_rows(connection)
    existing = connection.execute(
        "SELECT * FROM execution_lock_lease WHERE workflow_node_job_uuid=? "
        "AND deleted_at IS NULL ORDER BY lock_key",
        (job_uuid,),
    ).fetchall()
    if existing:
        for row in existing:
            _validate_execution_lock_row(row)
        actual = {
            str(row["lock_key"]): (str(row["claim_uuid"]), int(row["fencing_token"]))
            for row in existing
        }
        expected = {
            key: (claim_uuid, token) for key, token in provided_fences.items()
        }
        if actual != expected:
            raise StoreConflict(f"工作流 Permit 镜像发生漂移：{job_uuid}")
        claim = required_active_claim(connection, job_uuid=job_uuid)
        if str(claim["claim_uuid"]) != claim_uuid:
            raise StoreConflict(f"持久 Claim 与库存 Permit 不一致：{job_uuid}")
        _release_waiters(connection, job_uuid=job_uuid)
        _clear_wait_reason(connection, task_uuid=task_uuid, job_uuid=job_uuid)
        return ExecutionLockDecision(
            acquired=True,
            claim_uuid=claim_uuid,
            fencing_tokens=tuple(sorted(provided_fences.items())),
        )
    acquired_at = utc_now()
    ensure_execution_claim(
        connection,
        task_uuid=task_uuid,
        job_uuid=job_uuid,
        resource_keys=tuple(sorted(requested_keys)),
        acquired_at=acquired_at,
        claim_uuid=claim_uuid,
    )
    for request in normalized:
        connection.execute(
            """
            INSERT INTO execution_lock_lease(
                uuid, create_time, update_time, deleted_at, description,
                meta_data, workflow_task_uuid, workflow_node_job_uuid,
                lock_key, scope, material_uuid, site_uuid, state,
                acquired_at, released_at, claim_uuid, fencing_token
            ) VALUES (?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?, ?, ?,
                      'reserved', ?, NULL, ?, ?)
            """,
            (
                str(uuid4()),
                acquired_at,
                acquired_at,
                encode_json(
                    {
                        "semantic_scope": request.scope,
                        "acquired_by_job_uuid": job_uuid,
                        "authority": "inventory_dispatch_permit",
                        "resource_plan_id": str(resource_plan_id or ""),
                        "resource_interval_ids": sorted(
                            _normalize_interval_ids_by_lock(
                                resource_interval_ids_by_lock,
                                requested_keys=requested_keys,
                                interval_ids={
                                    str(value).strip()
                                    for value in resource_interval_ids
                                    if str(value).strip()
                                },
                            ).get(request.lock_key, ())
                        ),
                        "resource_acquire_set_id": str(resource_acquire_set_id or ""),
                    },
                    sort_keys=True,
                ).decode("utf-8"),
                task_uuid,
                job_uuid,
                request.lock_key,
                request.scope,
                request.material_uuid,
                request.site_uuid,
                acquired_at,
                claim_uuid,
                provided_fences[request.lock_key],
            ),
        )
    _release_waiters(connection, job_uuid=job_uuid, released_at=acquired_at)
    _clear_wait_reason(connection, task_uuid=task_uuid, job_uuid=job_uuid)
    return ExecutionLockDecision(
        acquired=True,
        claim_uuid=claim_uuid,
        fencing_tokens=tuple(sorted(provided_fences.items())),
    )


def record_execution_lock_wait(
    connection: sqlite3.Connection,
    *,
    task_uuid: str,
    job_uuid: str,
    requests: Sequence[Mapping[str, Any]] | None,
    blocking_task_uuid: str | None = None,
    blocking_job_uuid: str | None = None,
    wait_resources: Sequence[Mapping[str, str]] = (),
) -> ExecutionLockDecision:
    """在物理执行尚被内存占用阻止时持久化同一组锁的等待顺序与名称。"""

    normalized = normalize_execution_lock_requests(requests)
    if not normalized:
        _clear_wait_reason(connection, task_uuid=task_uuid, job_uuid=job_uuid)
        return ExecutionLockDecision(acquired=True)
    waiting_since = _ensure_waiters(
        connection,
        task_uuid=task_uuid,
        job_uuid=job_uuid,
        requests=normalized,
    )
    return _record_wait(
        connection,
        task_uuid=task_uuid,
        job_uuid=job_uuid,
        requests=normalized,
        blocking_task_uuid=blocking_task_uuid,
        blocking_job_uuid=blocking_job_uuid,
        waiting_since=waiting_since,
        wait_resources=wait_resources,
    )


def mark_execution_locks_running(
    connection: sqlite3.Connection,
    *,
    job_uuid: str,
    now: str,
) -> None:
    """把已被执行适配器接受的作业锁从 reserved 推进为 running。"""

    connection.execute(
        """
        UPDATE execution_lock_lease
        SET state = 'running', update_time = ?
        WHERE workflow_node_job_uuid = ? AND state = 'reserved'
          AND deleted_at IS NULL
        """,
        (now, job_uuid),
    )
    connection.execute(
        """
        UPDATE execution_claim
        SET state = 'running', update_time = ?
        WHERE workflow_node_job_uuid = ? AND state = 'reserved'
        """,
        (now, job_uuid),
    )


def mark_execution_locks_uncertain(
    connection: sqlite3.Connection,
    *,
    job_uuid: str,
    now: str,
) -> None:
    """保留结果不明作业的全部锁，并标记为 uncertain。"""

    connection.execute(
        """
        UPDATE execution_lock_lease
        SET state = 'uncertain', update_time = ?
        WHERE workflow_node_job_uuid = ?
          AND state IN ('reserved', 'running')
          AND deleted_at IS NULL
        """,
        (now, job_uuid),
    )
    connection.execute(
        """
        UPDATE execution_claim
        SET state = 'uncertain', update_time = ?
        WHERE workflow_node_job_uuid = ?
          AND state IN ('reserved', 'running')
        """,
        (now, job_uuid),
    )


def release_execution_locks(
    connection: sqlite3.Connection,
    *,
    job_uuid: str,
    now: str,
    keep_interval_ids: Sequence[str] = (),
) -> None:
    """在明确结果提交后释放作业锁，可保留仍在区间内的资源。"""

    keep = {str(value).strip() for value in keep_interval_ids if str(value).strip()}
    if keep:
        rows = connection.execute(
            """
            SELECT * FROM execution_lock_lease
            WHERE workflow_node_job_uuid = ?
              AND state IN ('reserved', 'running', 'uncertain')
              AND deleted_at IS NULL
            """,
            (job_uuid,),
        ).fetchall()
        release_rows: list[sqlite3.Row] = []
        for row in rows:
            metadata = _lease_metadata(row)
            raw_ids = metadata.get("resource_interval_ids")
            interval_ids = (
                {str(value) for value in raw_ids}
                if isinstance(raw_ids, Sequence) and not isinstance(raw_ids, (str, bytes))
                else set()
            )
            if not interval_ids.intersection(keep):
                release_rows.append(row)
        for row in release_rows:
            connection.execute(
                """
                UPDATE execution_lock_lease
                SET state='released', released_at=?, update_time=?
                WHERE uuid=? AND state IN ('reserved','running','uncertain')
                """,
                (now, now, row["uuid"]),
            )
        # 本函数只在明确结果提交后调用：UNKNOWN 期间标成 uncertain 的续持资源
        # 已被证明仍由本区间占用，收敛回 running，后继才能按连续区间继承。
        release_uuids = {str(row["uuid"]) for row in release_rows}
        for row in rows:
            if str(row["uuid"]) not in release_uuids and row["state"] == "uncertain":
                connection.execute(
                    "UPDATE execution_lock_lease SET state='running', update_time=? "
                    "WHERE uuid=? AND state='uncertain'",
                    (now, row["uuid"]),
                )
        remaining = connection.execute(
            """
            SELECT 1 FROM execution_lock_lease
            WHERE workflow_node_job_uuid=? AND state IN ('reserved','running','uncertain')
              AND deleted_at IS NULL LIMIT 1
            """,
            (job_uuid,),
        ).fetchone()
        if remaining is None:
            connection.execute(
                """
                UPDATE execution_claim
                SET state='released', released_at=?, update_time=?
                WHERE workflow_node_job_uuid=?
                  AND state IN ('reserved','running','uncertain')
                """,
                (now, now, job_uuid),
            )
        else:
            connection.execute(
                "UPDATE execution_claim SET state='running', update_time=? "
                "WHERE workflow_node_job_uuid=? AND state='uncertain'",
                (now, job_uuid),
            )
        return

    connection.execute(
        """
        UPDATE execution_lock_lease
        SET state = 'released', released_at = ?, update_time = ?
        WHERE workflow_node_job_uuid = ?
          AND state IN ('reserved', 'running', 'uncertain')
          AND deleted_at IS NULL
        """,
        (now, now, job_uuid),
    )
    connection.execute(
        """
        UPDATE execution_claim
        SET state = 'released', released_at = ?, update_time = ?
        WHERE workflow_node_job_uuid = ?
          AND state IN ('reserved', 'running', 'uncertain')
        """,
        (now, now, job_uuid),
    )


def release_task_execution_locks(
    connection: sqlite3.Connection,
    *,
    task_uuid: str,
    now: str,
) -> None:
    """在物理清理完成时回收一个任务残留的全部占用与等待事实。

    参数：数据库连接、工作流任务（WorkflowTask）身份和统一结算时间。返回无。
    异常：SQLite 写入错误原样传播。该操作只应在调用方已证明没有在途物理作业
    后执行；它覆盖由后续释放作业持有的访问区域长锁，并保持幂等。
    """

    connection.execute(
        """
        UPDATE execution_lock_lease
        SET state = 'released', released_at = ?, update_time = ?
        WHERE workflow_task_uuid = ?
          AND state IN ('reserved', 'running', 'uncertain')
          AND deleted_at IS NULL
        """,
        (now, now, task_uuid),
    )
    connection.execute(
        """
        UPDATE execution_claim
        SET state = 'released', released_at = ?, update_time = ?
        WHERE workflow_task_uuid = ?
          AND state IN ('reserved', 'running', 'uncertain')
        """,
        (now, now, task_uuid),
    )
    connection.execute(
        """
        UPDATE execution_lock_waiter
        SET state = 'released', released_at = ?, update_time = ?
        WHERE workflow_task_uuid = ? AND state = 'waiting'
          AND deleted_at IS NULL
        """,
        (now, now, task_uuid),
    )


def list_execution_locks(
    connection: sqlite3.Connection,
    *,
    job_uuid: str | None = None,
    task_uuid: str | None = None,
    uuids: list[str] | None = None,
    active_only: bool = False,
) -> list[dict[str, Any]]:
    """按身份过滤执行锁；监控查询在 SQL 中排除已释放的历史行。"""

    query = "SELECT * FROM execution_lock_lease WHERE deleted_at IS NULL"
    parameters: list[str] = []
    if job_uuid is not None:
        query += " AND workflow_node_job_uuid = ?"
        parameters.append(job_uuid)
    if task_uuid is not None:
        query += " AND workflow_task_uuid = ?"
        parameters.append(task_uuid)
    if active_only:
        query += " AND state != 'released'"
    if uuids is not None:
        if not uuids:
            return []
        query += " AND uuid IN (" + ",".join("?" for _ in uuids) + ")"
        parameters.extend(uuids)
    query += " ORDER BY create_time ASC, uuid ASC"
    result: list[dict[str, Any]] = []
    for row in connection.execute(query, tuple(parameters)).fetchall():
        item = dict(row)
        metadata = _lease_metadata(row)
        semantic_scope = str(metadata.get("semantic_scope") or "").strip()
        if semantic_scope in _SCOPES:
            item["scope"] = semantic_scope
        result.append(item)
    return result


def record_execution_lock_operator_action(
    connection: sqlite3.Connection,
    *,
    task_uuid: str,
    job_uuid: str,
    lease_uuid: str,
    claim_uuid: str,
    expected_claim_uuid: str,
    expected_fencing_token: int,
    reason: str,
    physical_settlement_confirmed: bool,
    result: str,
    released_lock_uuids: Sequence[str],
    now: str,
) -> dict[str, Any]:
    """在当前事务内持久化一次执行锁人工处置审计。"""

    if result not in {"released", "already_released"}:
        raise StoreConflict("执行锁人工处置结果非法")
    action_uuid = str(uuid4())
    connection.execute(
        """
        INSERT INTO execution_lock_operator_action(
            uuid, create_time, update_time, workflow_task_uuid,
            workflow_node_job_uuid, lease_uuid, claim_uuid,
            expected_claim_uuid, expected_fencing_token, action, result,
            reason, physical_settlement_confirmed, released_lock_uuids, meta_data
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'force_release', ?, ?, ?, ?, '{}')
        """,
        (
            action_uuid,
            now,
            now,
            task_uuid,
            job_uuid,
            lease_uuid,
            claim_uuid,
            expected_claim_uuid,
            expected_fencing_token,
            result,
            reason,
            1 if physical_settlement_confirmed else 0,
            encode_json(list(released_lock_uuids), sort_keys=True).decode("utf-8"),
        ),
    )
    return {
        "uuid": action_uuid,
        "workflow_task_uuid": task_uuid,
        "workflow_node_job_uuid": job_uuid,
        "lease_uuid": lease_uuid,
        "claim_uuid": claim_uuid,
        "action": "force_release",
        "result": result,
        "reason": reason,
        "physical_settlement_confirmed": physical_settlement_confirmed,
        "released_lock_uuids": list(released_lock_uuids),
        "create_time": now,
    }


def _ensure_waiters(
    connection: sqlite3.Connection,
    *,
    task_uuid: str,
    job_uuid: str,
    requests: tuple[ExecutionLockRequest, ...],
) -> str:
    """为一组未取得的执行锁建立稳定排队事实。

    参数：工作流事务、Task/Job 身份与规范锁请求。返回：该 Job 首次排队时间；
    重放复用相同时间并用唯一约束避免重复 waiter。异常：数据库错误原样传播，
    所有写入随调用方事务提交或回滚。
    """

    existing = connection.execute(
        """
        SELECT MIN(enqueued_at) AS enqueued_at
        FROM execution_lock_waiter
        WHERE workflow_node_job_uuid = ? AND state = 'waiting'
          AND deleted_at IS NULL
        """,
        (job_uuid,),
    ).fetchone()
    enqueued_at = str(existing["enqueued_at"] or utc_now())
    for request in requests:
        metadata = {
            "semantic_scope": request.scope,
        }
        connection.execute(
            """
            INSERT OR IGNORE INTO execution_lock_waiter(
                uuid, create_time, update_time, deleted_at, description,
                meta_data, workflow_task_uuid, workflow_node_job_uuid,
                lock_key, scope, material_uuid, site_uuid, state,
                enqueued_at, released_at
            ) VALUES (?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?, ?, ?,
                      'waiting', ?, NULL)
            """,
            (
                str(uuid4()),
                enqueued_at,
                enqueued_at,
                encode_json(metadata, sort_keys=True).decode("utf-8"),
                task_uuid,
                job_uuid,
                request.lock_key,
                request.scope,
                request.material_uuid,
                request.site_uuid,
                enqueued_at,
            ),
        )
    return enqueued_at


def _older_conflicting_waiter(
    connection: sqlite3.Connection,
    *,
    job_uuid: str,
    current_enqueued_at: str,
    current_task_create_time: str,
    current_task_uuid: str,
    current_priority: float,
    requested_keys: set[str],
    aging_interval_seconds: float,
) -> tuple[str, str] | None:
    """查找有效优先级领先且与尚未托管资源冲突的等待作业。

    参数：``connection`` 是工作流权威事务；``job_uuid`` 是当前作业身份；
    当前等待、任务创建、稳定身份和基础优先级共同构成排序；``requested_keys``
    已排除当前任务长期托管的设备键。返回：领先的阻塞任务和作业 UUID，或无冲突
    时返回 ``None``。异常：时间或数据库事实非法时原样传播。长期托管设备必须让
    所属任务优先完成操作或卸载，不能被外部等待者反向阻塞。
    """

    rows = connection.execute(
        """
        SELECT waiter.workflow_task_uuid, waiter.workflow_node_job_uuid,
               waiter.lock_key, waiter.enqueued_at,
               task.create_time AS task_create_time, task.priority
        FROM execution_lock_waiter AS waiter
        JOIN workflow_node_job AS job
          ON job.uuid = waiter.workflow_node_job_uuid
         AND job.deleted_at IS NULL AND job.status = 'pending'
        JOIN workflow_task AS task
          ON task.uuid = waiter.workflow_task_uuid
         AND task.deleted_at IS NULL
         AND task.status IN ('pending', 'running')
        WHERE waiter.deleted_at IS NULL AND waiter.state = 'waiting'
          AND waiter.workflow_node_job_uuid <> ?
        ORDER BY waiter.enqueued_at ASC, task.create_time ASC,
                 waiter.workflow_task_uuid ASC,
                 waiter.workflow_node_job_uuid ASC, waiter.lock_key ASC
        """,
        (job_uuid,),
    ).fetchall()
    grouped: dict[tuple[str, str, str, str, float], set[str]] = defaultdict(set)
    for row in rows:
        order = (
            str(row["enqueued_at"]),
            str(row["task_create_time"]),
            str(row["workflow_task_uuid"]),
            str(row["workflow_node_job_uuid"]),
            priority_weight(row["priority"]),
        )
        grouped[order].add(str(row["lock_key"]))
    now_seconds = _rfc3339_seconds(utc_now())
    current_rank = _waiter_rank(
        priority=current_priority,
        enqueued_at=current_enqueued_at,
        task_create_time=current_task_create_time,
        task_uuid=current_task_uuid,
        job_uuid=job_uuid,
        now_seconds=now_seconds,
        aging_interval_seconds=aging_interval_seconds,
    )
    candidates: list[tuple[tuple[float, str, str, str, str], str, str]] = []
    for order, lock_keys in grouped.items():
        enqueued_at, task_create_time, task_uuid, waiter_job_uuid, priority = order
        if not conflicting_resource_lock_keys(requested_keys, lock_keys):
            continue
        rank = _waiter_rank(
            priority=priority,
            enqueued_at=enqueued_at,
            task_create_time=task_create_time,
            task_uuid=task_uuid,
            job_uuid=waiter_job_uuid,
            now_seconds=now_seconds,
            aging_interval_seconds=aging_interval_seconds,
        )
        if rank < current_rank:
            candidates.append((rank, task_uuid, waiter_job_uuid))
    if candidates:
        _, blocking_task_uuid, blocking_job_uuid = min(candidates)
        return blocking_task_uuid, blocking_job_uuid
    return None


def _waiter_rank(
    *,
    priority: float,
    enqueued_at: str,
    task_create_time: str,
    task_uuid: str,
    job_uuid: str,
    now_seconds: float,
    aging_interval_seconds: float,
) -> tuple[float, str, str, str, str]:
    """把持久等待事实转换为与内存调度一致的稳定升序键。"""

    effective = aged_priority(
        priority,
        waited_seconds=now_seconds - _rfc3339_seconds(enqueued_at),
        aging_interval_seconds=aging_interval_seconds,
    )
    return (
        -effective,
        enqueued_at,
        task_create_time,
        task_uuid,
        job_uuid,
    )


def _rfc3339_seconds(value: str) -> float:
    """解析带时区 RFC3339 时间并返回 UTC epoch 秒。"""

    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("持久等待时间缺少时区")
    return parsed.astimezone(timezone.utc).timestamp()


def _record_wait(
    connection: sqlite3.Connection,
    *,
    task_uuid: str,
    job_uuid: str,
    requests: tuple[ExecutionLockRequest, ...],
    blocking_task_uuid: str | None,
    blocking_job_uuid: str | None,
    waiting_since: str,
    wait_resources: Sequence[Mapping[str, str]] = (),
) -> ExecutionLockDecision:
    resources = [_wait_resource(request) for request in requests]
    descriptions = {
        key: dict(resource)
        for resource in wait_resources
        if (key := wait_resource_identity(resource)) is not None
    }
    for resource in resources:
        description = descriptions.get(wait_resource_identity(resource))
        if description is None:
            continue
        for field in (
            "local_device_id",
            "device_name",
            "material_name",
            "site_name",
            "wait_code",
            "wait_message",
        ):
            value = str(description.get(field) or "").strip()
            if value:
                resource[field] = value
    reason = {
        "code": "operation_lease",
        "message": "执行资源正在被其他作业使用",
        "scopes": sorted({request.scope for request in requests}),
        "resources": resources,
        "waiting_since": waiting_since,
    }
    if blocking_task_uuid is not None:
        reason["blocking_task_uuid"] = blocking_task_uuid
    if blocking_job_uuid is not None:
        reason["blocking_job_uuid"] = blocking_job_uuid
    reason_json = encode_json(reason, sort_keys=True).decode("utf-8")
    now = utc_now()
    connection.execute(
        "UPDATE workflow_node_job SET wait_reason = ?, update_time = ? WHERE uuid = ?",
        (reason_json, now, job_uuid),
    )
    connection.execute(
        "UPDATE workflow_task SET wait_reason = ?, update_time = ? WHERE uuid = ?",
        (reason_json, now, task_uuid),
    )
    return ExecutionLockDecision(
        acquired=False,
        blocking_task_uuid=blocking_task_uuid,
        blocking_job_uuid=blocking_job_uuid,
    )


def _wait_resource(request: ExecutionLockRequest) -> dict[str, str]:
    """把内部锁键转换为可展示但不依赖锁键语法的等待资源。"""

    resource = {"scope": request.scope}
    if request.scope == "resource":
        resource["lock_key"] = request.lock_key
        return resource
    if request.scope == "device":
        if request.material_uuid is not None:
            resource["device_id"] = request.material_uuid
        elif request.lock_key.startswith("/devices/"):
            resource["device_id"] = request.lock_key.removeprefix(
                "/devices/"
            ).split("/", 1)[0]
        return resource
    if request.material_uuid is not None:
        resource["material_uuid"] = request.material_uuid
    if request.scope == "material_site" and request.site_uuid is not None:
        resource["site_uuid"] = request.site_uuid
    return resource


def wait_resource_identity(
    resource: Mapping[str, Any],
) -> tuple[str, str] | None:
    """返回等待资源用于合并名称的稳定作用域与身份。"""

    scope = str(resource.get("scope") or "").strip()
    identity_field = {
        "device": "device_id",
        "material": "material_uuid",
        "material_site": "site_uuid",
        "resource": "lock_key",
    }.get(scope)
    if identity_field is None:
        return None
    identity = str(resource.get(identity_field) or "").strip()
    return (scope, identity) if identity else None


def wait_resource_from_execution_lock(
    lock: Mapping[str, Any],
) -> dict[str, str] | None:
    """把单个内部执行锁合同转换为公开等待资源身份。"""

    requests = normalize_execution_lock_requests([lock])
    return _wait_resource(requests[0]) if requests else None


def _lease_metadata(row: sqlite3.Row) -> dict[str, Any]:
    """安全读取既有租约元数据；损坏事实按空对象保守处理。"""

    raw = row["meta_data"]
    try:
        decoded = decode_json_bytes(str(raw or "{}").encode("utf-8"))
    except (TypeError, ValueError, UnicodeError):
        return {}
    return dict(decoded) if isinstance(decoded, Mapping) else {}


def _lease_acquired_by(row: sqlite3.Row) -> str:
    """返回真正越过派发边界并取得租约的入口作业身份。"""

    metadata = _lease_metadata(row)
    return str(
        metadata.get("acquired_by_job_uuid") or row["workflow_node_job_uuid"]
    )


def _release_waiters(
    connection: sqlite3.Connection,
    *,
    job_uuid: str,
    released_at: str | None = None,
) -> None:
    settled_at = released_at or utc_now()
    connection.execute(
        """
        UPDATE execution_lock_waiter
        SET state = 'released', released_at = ?, update_time = ?
        WHERE workflow_node_job_uuid = ? AND state = 'waiting'
          AND deleted_at IS NULL
        """,
        (settled_at, settled_at, job_uuid),
    )


def _clear_wait_reason(
    connection: sqlite3.Connection,
    *,
    task_uuid: str,
    job_uuid: str,
) -> None:
    now = utc_now()
    connection.execute(
        "UPDATE workflow_node_job SET wait_reason = '{}', update_time = ? WHERE uuid = ?",
        (now, job_uuid),
    )
    remaining = connection.execute(
        """
        SELECT wait_reason FROM workflow_node_job
        WHERE workflow_task_uuid = ? AND uuid <> ? AND status = 'pending'
          AND deleted_at IS NULL AND wait_reason <> '{}'
        ORDER BY update_time ASC, uuid ASC LIMIT 1
        """,
        (task_uuid, job_uuid),
    ).fetchone()
    task_wait_reason = str(remaining["wait_reason"]) if remaining is not None else "{}"
    connection.execute(
        "UPDATE workflow_task SET wait_reason = ?, update_time = ? WHERE uuid = ?",
        (task_wait_reason, now, task_uuid),
    )


__all__ = [
    "ExecutionLockDecision",
    "ExecutionLockRequest",
    "list_execution_locks",
    "record_execution_lock_operator_action",
    "mark_execution_locks_running",
    "mark_execution_locks_uncertain",
    "normalize_execution_lock_requests",
    "record_execution_lock_wait",
    "release_execution_locks",
    "release_task_execution_locks",
    "try_acquire_execution_locks",
    "mirror_execution_locks_from_permit",
    "wait_resource_from_execution_lock",
    "wait_resource_identity",
]
