"""作业执行声明（Claim）与资源栅栏令牌的持久化规则。"""

from __future__ import annotations

import sqlite3
from typing import Any
from uuid import uuid4

from unilabos.workflow.json_codec import decode_json_bytes, encode_json
from unilabos.workflow.store import StoreConflict, utc_now

_ACTIVE_CLAIM_STATES = frozenset({"reserved", "running", "uncertain"})


def get_execution_claim(
    connection: sqlite3.Connection,
    *,
    job_uuid: str,
) -> dict[str, Any] | None:
    """读取作业当前尝试的稳定 Claim 与每个资源的栅栏令牌。

    参数：``connection`` 是工作流读事务；``job_uuid`` 是稳定作业身份。返回：
    不存在时为 ``None``，否则包含 Claim 状态和按锁键排序的 fences。异常：持久
    资源集合、权威 Fence 快照或活动自有 Lease 损坏时抛 ``StoreConflict``，
    关闭式阻止派发；已经物理结算的终态 Job 仍从不可变快照稳定读取。
    """

    row = connection.execute(
        """
        SELECT * FROM execution_claim
        WHERE workflow_node_job_uuid = ?
        ORDER BY attempt DESC LIMIT 1
        """,
        (job_uuid,),
    ).fetchone()
    if row is None:
        return None
    raw_resource_keys = decode_json_bytes(str(row["resource_keys"]).encode("utf-8"))
    if not isinstance(raw_resource_keys, list):
        raise StoreConflict(f"Claim 资源集合损坏：{job_uuid}")
    resource_keys: list[str] = []
    for raw_key in raw_resource_keys:
        if (
            not isinstance(raw_key, str)
            or not raw_key.strip()
            or raw_key.strip() in resource_keys
        ):
            raise StoreConflict(f"Claim 资源集合损坏：{job_uuid}")
        resource_keys.append(raw_key.strip())
    fences = connection.execute(
        """
        SELECT lock_key, fencing_token, workflow_node_job_uuid, state
        FROM execution_lock_lease
        WHERE claim_uuid = ? AND deleted_at IS NULL
        ORDER BY lock_key ASC
        """,
        (row["claim_uuid"],),
    ).fetchall()
    lease_fences: dict[str, int] = {}
    active_owned_keys: set[str] = set()
    for fence in fences:
        lock_key = str(fence["lock_key"])
        if lock_key in lease_fences:
            raise StoreConflict(f"Claim 本地 Lease 重复：{job_uuid}/{lock_key}")
        lease_fences[lock_key] = int(fence["fencing_token"])
        if (
            str(fence["workflow_node_job_uuid"]) == job_uuid
            and str(fence["state"]) in _ACTIVE_CLAIM_STATES
        ):
            active_owned_keys.add(lock_key)
    job = connection.execute(
        "SELECT control_data, dispatch_effect_uuid, status "
        "FROM workflow_node_job WHERE uuid=? AND deleted_at IS NULL",
        (job_uuid,),
    ).fetchone()
    control_data = (
        decode_json_bytes(str(job["control_data"]).encode("utf-8"))
        if job is not None
        else {}
    )
    raw_dispatch_fences = (
        control_data.get("dispatch_fences")
        if isinstance(control_data, dict)
        else None
    )
    authoritative_permit = bool(
        job is not None and str(job["dispatch_effect_uuid"] or "").strip()
    )
    if authoritative_permit and raw_dispatch_fences is None:
        raise StoreConflict(f"作业缺少权威 Fence 快照：{job_uuid}")
    claim_fences = lease_fences
    if raw_dispatch_fences is not None:
        if not isinstance(raw_dispatch_fences, list):
            raise StoreConflict(f"作业权威 Fence 快照损坏：{job_uuid}")
        authoritative: dict[str, int] = {}
        for raw_fence in raw_dispatch_fences:
            if not isinstance(raw_fence, dict) or set(raw_fence) != {
                "lock_key",
                "fencing_token",
            }:
                raise StoreConflict(f"作业权威 Fence 快照损坏：{job_uuid}")
            lock_key = str(raw_fence["lock_key"] or "").strip()
            token = raw_fence["fencing_token"]
            if (
                not lock_key
                or isinstance(token, bool)
                or not isinstance(token, int)
                or token <= 0
                or lock_key in authoritative
            ):
                raise StoreConflict(f"作业权威 Fence 快照损坏：{job_uuid}")
            authoritative[lock_key] = token
        if set(authoritative) != set(resource_keys):
            raise StoreConflict(f"作业权威 Fence 与 Claim 资源集合不一致：{job_uuid}")
        if any(authoritative.get(key) != token for key, token in lease_fences.items()):
            raise StoreConflict(f"作业权威 Fence 与本地 Lease 不一致：{job_uuid}")
        raw_preheld_keys = control_data.get("dispatch_preheld_lock_keys", [])
        if not isinstance(raw_preheld_keys, list):
            raise StoreConflict(f"作业权威 preheld 资源快照损坏：{job_uuid}")
        preheld_keys: set[str] = set()
        for raw_key in raw_preheld_keys:
            if (
                not isinstance(raw_key, str)
                or not raw_key.strip()
                or raw_key.strip() in preheld_keys
            ):
                raise StoreConflict(f"作业权威 preheld 资源快照损坏：{job_uuid}")
            preheld_keys.add(raw_key.strip())
        if not preheld_keys <= set(resource_keys):
            raise StoreConflict(f"作业权威 preheld 资源不在 Claim 中：{job_uuid}")
        job_status = str(job["status"]) if job is not None else ""
        claim_state = str(row["state"])
        settled_job_holds_interval = (
            job_status in {"succeeded", "failed", "skipped", "canceled", "timeout"}
            and claim_state != "uncertain"
        )
        if claim_state in _ACTIVE_CLAIM_STATES and not settled_job_holds_interval:
            missing_owned = set(resource_keys) - preheld_keys - active_owned_keys
            if missing_owned:
                raise StoreConflict(
                    "活动 Claim 缺少自有新增资源 Lease："
                    + "、".join(sorted(missing_owned))
                )
        claim_fences = authoritative
    return {
        "claim_uuid": row["claim_uuid"],
        "workflow_task_uuid": row["workflow_task_uuid"],
        "workflow_node_job_uuid": row["workflow_node_job_uuid"],
        "attempt": int(row["attempt"]),
        "resource_keys": resource_keys,
        "state": row["state"],
        "acquired_at": row["acquired_at"],
        "released_at": row["released_at"],
        "fences": [
            {
                "lock_key": lock_key,
                "fencing_token": token,
            }
            for lock_key, token in sorted(claim_fences.items())
        ],
    }


def require_dispatchable_execution_claim(
    connection: sqlite3.Connection,
    *,
    job_uuid: str,
) -> dict[str, Any]:
    """在物理派发前证明 Workflow Claim 与全部 Lease 仍可使用。

    参数：工作流事务和待派发 Job。返回：已经过 Claim、Lease 与 Fence 完整性
    复验的凭据。异常：Claim 不是 reserved、当前 Job Lease 不完整，或 preheld
    资源不再由声明的同 Task 前驱活动持有时抛 ``StoreConflict``。
    """

    claim = get_execution_claim(connection, job_uuid=job_uuid)
    if claim is None:
        raise StoreConflict(f"物理派发前作业缺少 Claim：{job_uuid}")
    if str(claim["state"]) != "reserved" or claim["released_at"] is not None:
        raise StoreConflict(f"物理派发前作业 Claim 不是活动预留：{job_uuid}")
    job = connection.execute(
        "SELECT control_data FROM workflow_node_job "
        "WHERE uuid=? AND deleted_at IS NULL",
        (job_uuid,),
    ).fetchone()
    if job is None:
        raise StoreConflict(f"物理派发前作业不存在：{job_uuid}")
    control_data = decode_json_bytes(
        str(job["control_data"] or "{}").encode("utf-8")
    )
    if not isinstance(control_data, dict):
        raise StoreConflict(f"物理派发前作业 control_data 已损坏：{job_uuid}")
    raw_preheld_keys = control_data.get("dispatch_preheld_lock_keys", [])
    raw_preheld_jobs = control_data.get("dispatch_preheld_job_uuids", [])
    if not isinstance(raw_preheld_keys, list) or not isinstance(
        raw_preheld_jobs, list
    ):
        raise StoreConflict(f"物理派发前作业 preheld 快照已损坏：{job_uuid}")
    if any(not isinstance(value, str) for value in raw_preheld_jobs):
        raise StoreConflict(f"物理派发前作业 preheld 快照已损坏：{job_uuid}")
    preheld_keys = {
        str(value).strip() for value in raw_preheld_keys if str(value).strip()
    }
    preheld_jobs = {
        str(value).strip() for value in raw_preheld_jobs if str(value).strip()
    }
    if (
        len(preheld_keys) != len(raw_preheld_keys)
        or len(preheld_jobs) != len(raw_preheld_jobs)
        or job_uuid in preheld_jobs
        or (preheld_keys and not preheld_jobs)
    ):
        raise StoreConflict(f"物理派发前作业 preheld 快照已损坏：{job_uuid}")
    resource_keys = set(claim["resource_keys"])
    authoritative = {
        str(fence["lock_key"]): int(fence["fencing_token"])
        for fence in claim["fences"]
    }
    if not preheld_keys <= resource_keys or set(authoritative) != resource_keys:
        raise StoreConflict(f"物理派发前作业资源快照不完整：{job_uuid}")

    current_rows = connection.execute(
        "SELECT lock_key,fencing_token,workflow_node_job_uuid,state,released_at,"
        "meta_data "
        "FROM execution_lock_lease WHERE claim_uuid=? AND deleted_at IS NULL "
        "ORDER BY lock_key",
        (claim["claim_uuid"],),
    ).fetchall()
    current_by_key: dict[str, sqlite3.Row] = {}
    invalid_current: list[str] = []
    for lease in current_rows:
        lock_key = str(lease["lock_key"])
        allowed_states = (
            {"reserved", "running"} if lock_key in preheld_keys else {"reserved"}
        )
        valid_handoff = True
        if lock_key in preheld_keys:
            try:
                metadata = decode_json_bytes(
                    str(lease["meta_data"] or "{}").encode("utf-8")
                )
            except (TypeError, ValueError, UnicodeError):
                valid_handoff = False
            else:
                valid_handoff = bool(
                    isinstance(metadata, dict)
                    and str(metadata.get("acquired_by_job_uuid") or "")
                    == job_uuid
                    and str(metadata.get("handoff_from_job_uuid") or "")
                    in preheld_jobs
                )
        if (
            lock_key in current_by_key
            or lock_key not in resource_keys
            or str(lease["workflow_node_job_uuid"]) != job_uuid
            or str(lease["state"]) not in allowed_states
            or lease["released_at"] is not None
            or int(lease["fencing_token"])
            != authoritative.get(lock_key)
            or not valid_handoff
        ):
            invalid_current.append(lock_key)
        current_by_key[lock_key] = lease
    missing_keys = resource_keys - set(current_by_key)
    if invalid_current or not missing_keys <= preheld_keys:
        raise StoreConflict(
            "物理派发前 Workflow Lease 缺失、失活或 Fence 不一致："
            + "、".join(sorted(set(invalid_current) | missing_keys))
        )
    if not missing_keys:
        return claim
    if not preheld_jobs:
        raise StoreConflict(f"物理派发前 preheld 资源缺少前驱 Job：{job_uuid}")

    placeholders = ",".join("?" for _ in missing_keys)
    source_rows = connection.execute(
        f"""
        SELECT lease.lock_key, lease.fencing_token,
               lease.workflow_node_job_uuid, lease.meta_data,
               lease.state AS lease_state,
               owner_claim.state AS owner_claim_state,
               owner_claim.workflow_task_uuid AS owner_task_uuid,
               owner_claim.workflow_node_job_uuid AS owner_job_uuid,
               owner_claim.resource_keys AS owner_resource_keys,
               owner_claim.released_at AS owner_released_at
        FROM execution_lock_lease AS lease
        JOIN execution_claim AS owner_claim
          ON owner_claim.claim_uuid = lease.claim_uuid
        WHERE lease.workflow_task_uuid=?
          AND lease.lock_key IN ({placeholders})
          AND lease.deleted_at IS NULL
          AND lease.released_at IS NULL
          AND lease.state IN ('reserved','running')
        ORDER BY lease.lock_key
        """,
        (str(claim["workflow_task_uuid"]), *sorted(missing_keys)),
    ).fetchall()
    source_by_key: dict[str, sqlite3.Row] = {}
    invalid_preheld: set[str] = set()
    for source in source_rows:
        lock_key = str(source["lock_key"])
        if lock_key in source_by_key:
            invalid_preheld.add(lock_key)
        source_by_key[lock_key] = source
    for lock_key in sorted(missing_keys):
        source = source_by_key.get(lock_key)
        if source is None:
            invalid_preheld.add(lock_key)
            continue
        try:
            metadata = decode_json_bytes(
                str(source["meta_data"] or "{}").encode("utf-8")
            )
            owner_keys = decode_json_bytes(
                str(source["owner_resource_keys"]).encode("utf-8")
            )
        except (TypeError, ValueError, UnicodeError):
            invalid_preheld.add(lock_key)
            continue
        acquired_by = (
            str(metadata.get("acquired_by_job_uuid") or "")
            if isinstance(metadata, dict)
            else ""
        )
        if (
            str(source["workflow_node_job_uuid"]) not in preheld_jobs
            or acquired_by not in preheld_jobs
            or str(source["owner_job_uuid"]) not in preheld_jobs
            or str(source["owner_task_uuid"])
            != str(claim["workflow_task_uuid"])
            or str(source["owner_claim_state"]) != str(source["lease_state"])
            or source["owner_released_at"] is not None
            or not isinstance(owner_keys, list)
            or lock_key not in owner_keys
            or int(source["fencing_token"]) != authoritative[lock_key]
        ):
            invalid_preheld.add(lock_key)
    if invalid_preheld:
        raise StoreConflict(
            "物理派发前 preheld 资源缺少匹配的活动前驱 Lease："
            + "、".join(sorted(invalid_preheld))
        )
    return claim


def ensure_execution_claim(
    connection: sqlite3.Connection,
    *,
    task_uuid: str,
    job_uuid: str,
    resource_keys: tuple[str, ...],
    acquired_at: str | None = None,
    claim_uuid: str | None = None,
) -> sqlite3.Row:
    """为一个 Job 尝试幂等创建稳定 Claim，且禁止资源集合漂移。

    参数：工作流写事务、任务/作业身份、完整资源键集合、可选取得时间和库存
    权威已签发的 ``claim_uuid``。返回：已存在或新建的审计 Claim 行。异常：作业
    不存在、同一次尝试的身份/资源集合变化或已释放 Claim 被重用时抛
    ``StoreConflict``；SQLite 写入错误原样传播。
    """

    job = connection.execute(
        """
        SELECT attempt FROM workflow_node_job
        WHERE uuid = ? AND workflow_task_uuid = ? AND deleted_at IS NULL
        """,
        (job_uuid, task_uuid),
    ).fetchone()
    if job is None:
        raise StoreConflict(f"Claim 所属作业不存在：{job_uuid}")
    attempt = int(job["attempt"])
    resource_json = encode_json(list(resource_keys), sort_keys=True).decode("utf-8")
    existing = connection.execute(
        """
        SELECT * FROM execution_claim
        WHERE workflow_node_job_uuid = ? AND attempt = ?
        """,
        (job_uuid, attempt),
    ).fetchone()
    if existing is not None:
        if claim_uuid is not None and str(existing["claim_uuid"]) != claim_uuid:
            raise StoreConflict(f"Claim 身份与库存 Permit 不一致：{job_uuid}")
        if str(existing["resource_keys"]) != resource_json:
            raise StoreConflict(f"Claim 完整资源集合发生变化：{job_uuid}")
        if existing["state"] == "released":
            raise StoreConflict(f"已释放 Claim 不能重新派发：{job_uuid}")
        return existing
    now = acquired_at or utc_now()
    resolved_claim_uuid = claim_uuid or str(uuid4())
    connection.execute(
        """
        INSERT INTO execution_claim(
            claim_uuid, create_time, update_time, workflow_task_uuid,
            workflow_node_job_uuid, attempt, resource_keys, state,
            acquired_at, released_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'reserved', ?, NULL)
        """,
        (
            resolved_claim_uuid,
            now,
            now,
            task_uuid,
            job_uuid,
            attempt,
            resource_json,
            now,
        ),
    )
    row = connection.execute(
        "SELECT * FROM execution_claim WHERE claim_uuid = ?",
        (resolved_claim_uuid,),
    ).fetchone()
    assert row is not None
    return row


def required_active_claim(
    connection: sqlite3.Connection,
    *,
    job_uuid: str,
) -> sqlite3.Row:
    """读取作业未释放 Claim，缺失时关闭式失败。

    参数：工作流读事务和作业身份。返回：最新的 reserved、running 或 uncertain
    Claim 行。异常：缺失说明锁占用事实损坏，抛 ``StoreConflict``。
    """

    row = connection.execute(
        """
        SELECT * FROM execution_claim
        WHERE workflow_node_job_uuid = ?
          AND state IN ('reserved', 'running', 'uncertain')
        ORDER BY attempt DESC LIMIT 1
        """,
        (job_uuid,),
    ).fetchone()
    if row is None:
        raise StoreConflict(f"作业执行租约缺少 Claim：{job_uuid}")
    return row


def list_active_execution_claim_uuids(
    connection: sqlite3.Connection,
) -> tuple[str, ...]:
    """列出工作流库仍承担安全意义的全部 Claim 身份。

    参数：工作流读事务。返回：按取得时间与 UUID 稳定排序的 reserved、running、
    uncertain Claim UUID。异常：SQLite 读取错误原样传播。
    """

    rows = connection.execute(
        """
        SELECT claim_uuid FROM execution_claim
        WHERE state IN ('reserved', 'running', 'uncertain')
        ORDER BY acquired_at, claim_uuid
        """
    ).fetchall()
    return tuple(str(row["claim_uuid"]) for row in rows)


def next_fencing_token(
    connection: sqlite3.Connection,
    *,
    lock_key: str,
    now: str,
) -> int:
    """在当前事务内为单个资源分配严格递增的栅栏令牌。

    参数：工作流写事务、规范资源锁键和统一分配时间。返回：该资源新分配的正
    整数令牌。异常：SQLite 写入错误原样传播；调用方必须让本操作与租约插入
    位于同一事务中，避免令牌已发布但租约未落库。
    """

    connection.execute(
        """
        INSERT INTO execution_fence_counter(lock_key, last_fencing_token, update_time)
        VALUES (?, 1, ?)
        ON CONFLICT(lock_key) DO UPDATE SET
            last_fencing_token = last_fencing_token + 1,
            update_time = excluded.update_time
        """,
        (lock_key, now),
    )
    row = connection.execute(
        """
        SELECT last_fencing_token FROM execution_fence_counter
        WHERE lock_key = ?
        """,
        (lock_key,),
    ).fetchone()
    assert row is not None
    return int(row["last_fencing_token"])


__all__ = [
    "ensure_execution_claim",
    "get_execution_claim",
    "list_active_execution_claim_uuids",
    "next_fencing_token",
    "require_dispatchable_execution_claim",
    "required_active_claim",
]
