"""基于既有 Task/Job 状态实现本地持久调度容量。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from unilabos.workflow.json_codec import decode_json_bytes, encode_json
from unilabos.workflow.store import StoreConflict, StoreNotFound, utc_now

_ACTIVE_TASK_STATUSES = ("running", "canceling")
_IN_FLIGHT_JOB_STATUSES = (
    "dispatched",
    "running",
    "intervention_required",
    "cancel_requested",
)


@dataclass(frozen=True, slots=True)
class CapacityDecision:
    """一次持久容量判定结果。"""

    available: bool
    reason: str | None = None
    activated: bool = False


def activate_workflow_task(
    connection: sqlite3.Connection,
    *,
    task_uuid: str,
    max_active_tasks: int,
    max_tasks_per_workflow: int,
) -> CapacityDecision:
    """在当前写事务中领取 Task 全局和工作流级容量。

    SQLite ``BEGIN IMMEDIATE`` 已串行化本地写事务，所以直接以现有 Task 状态
    计数即可得到与 Backend 槽位相同的业务语义，无需复制容量表。终态转换会
    自然释放容量；重启后仍可从持久状态重建。
    """

    decision = check_workflow_task_capacity(
        connection,
        task_uuid=task_uuid,
        max_active_tasks=max_active_tasks,
        max_tasks_per_workflow=max_tasks_per_workflow,
    )
    if not decision.available:
        return decision

    task = connection.execute(
        "SELECT status FROM workflow_task WHERE uuid = ? AND deleted_at IS NULL",
        (task_uuid,),
    ).fetchone()
    if task is None:
        raise StoreNotFound(f"工作流任务不存在：{task_uuid}")
    if str(task["status"]) in _ACTIVE_TASK_STATUSES:
        return CapacityDecision(available=True)
    if task["status"] != "pending":
        raise StoreConflict(f"工作流任务不能领取运行容量：{task_uuid}")

    activated_at = utc_now()
    changed = connection.execute(
        """
        UPDATE workflow_task
        SET status = 'running', started_at = COALESCE(started_at, ?),
            wait_reason = '{}', update_time = ?
        WHERE uuid = ? AND status = 'pending' AND deleted_at IS NULL
        """,
        (activated_at, activated_at, task_uuid),
    ).rowcount
    if changed != 1:
        raise StoreConflict(f"工作流任务容量领取发生并发变化：{task_uuid}")
    return CapacityDecision(available=True, activated=True)


def check_workflow_task_capacity(
    connection: sqlite3.Connection,
    *,
    task_uuid: str,
    max_active_tasks: int,
    max_tasks_per_workflow: int,
) -> CapacityDecision:
    """只检查任务容量，不改变任务的运行状态。

    派发流程需要先完成设备托管和执行锁准入，再把父 Task 从 ``pending``
    激活为 ``running``。这个只读（除记录等待原因外）门禁让调用方可以在
    资源尝试前拒绝容量不足，同时避免资源已拿到后才发现父任务无法激活。
    """

    _require_positive(max_active_tasks, field="max_active_tasks")
    _require_positive(max_tasks_per_workflow, field="max_tasks_per_workflow")
    task = connection.execute(
        """
        SELECT uuid, workflow_uuid, status, wait_reason
        FROM workflow_task
        WHERE uuid = ? AND deleted_at IS NULL
        """,
        (task_uuid,),
    ).fetchone()
    if task is None:
        raise StoreNotFound(f"工作流任务不存在：{task_uuid}")
    if str(task["status"]) in _ACTIVE_TASK_STATUSES:
        return CapacityDecision(available=True)
    if task["status"] != "pending":
        raise StoreConflict(f"工作流任务不能领取运行容量：{task_uuid}")

    global_active = _count(
        connection,
        "SELECT COUNT(*) FROM workflow_task "
        "WHERE deleted_at IS NULL AND status IN (?, ?)",
        _ACTIVE_TASK_STATUSES,
    )
    if global_active >= max_active_tasks:
        _record_task_wait(
            connection,
            task_uuid=task_uuid,
            code="global_task_capacity",
            message="全局运行任务容量已满，任务继续排队",
        )
        return CapacityDecision(False, "global_task_capacity")

    workflow_active = _count(
        connection,
        """
        SELECT COUNT(*) FROM workflow_task
        WHERE deleted_at IS NULL AND workflow_uuid = ?
          AND status IN (?, ?)
        """,
        (str(task["workflow_uuid"]), *_ACTIVE_TASK_STATUSES),
    )
    if workflow_active >= max_tasks_per_workflow:
        _record_task_wait(
            connection,
            task_uuid=task_uuid,
            code="workflow_task_capacity",
            message="同一工作流的运行任务容量已满，任务继续排队",
        )
        return CapacityDecision(False, "workflow_task_capacity")
    return CapacityDecision(available=True)


def admit_job_dispatch(
    connection: sqlite3.Connection,
    *,
    task_uuid: str,
    job_uuid: str,
    max_in_flight_jobs: int,
) -> CapacityDecision:
    """在当前写事务中判断一个 Job 能否进入持久派发边界。"""

    _require_positive(max_in_flight_jobs, field="max_in_flight_jobs")
    job = connection.execute(
        """
        SELECT workflow_task_uuid, status, wait_reason
        FROM workflow_node_job
        WHERE uuid = ? AND deleted_at IS NULL
        """,
        (job_uuid,),
    ).fetchone()
    if job is None:
        raise StoreNotFound(f"工作流节点作业不存在：{job_uuid}")
    if str(job["workflow_task_uuid"]) != task_uuid:
        raise StoreConflict(f"作业不属于指定任务：{job_uuid}/{task_uuid}")
    if str(job["status"]) in _IN_FLIGHT_JOB_STATUSES:
        return CapacityDecision(available=True)
    if job["status"] != "pending":
        raise StoreConflict(f"工作流节点作业不能领取派发容量：{job_uuid}")

    in_flight = _count(
        connection,
        "SELECT COUNT(*) FROM workflow_node_job "
        "WHERE deleted_at IS NULL AND status IN (?, ?, ?, ?)",
        _IN_FLIGHT_JOB_STATUSES,
    )
    if in_flight < max_in_flight_jobs:
        return CapacityDecision(available=True)

    waited_at = utc_now()
    reason = {
        "code": "job_dispatch_capacity",
        "message": "全局在途作业容量已满，作业继续排队",
        "waiting_since": _waiting_since(job["wait_reason"], fallback=waited_at),
    }
    encoded = encode_json(reason, sort_keys=True).decode("utf-8")
    connection.execute(
        "UPDATE workflow_node_job SET wait_reason = ?, update_time = ? WHERE uuid = ?",
        (encoded, waited_at, job_uuid),
    )
    connection.execute(
        "UPDATE workflow_task SET wait_reason = ?, update_time = ? WHERE uuid = ?",
        (encoded, waited_at, task_uuid),
    )
    return CapacityDecision(False, "job_dispatch_capacity")


def _count(
    connection: sqlite3.Connection,
    query: str,
    parameters: tuple[Any, ...],
) -> int:
    row = connection.execute(query, parameters).fetchone()
    return int(row[0])


def _record_task_wait(
    connection: sqlite3.Connection,
    *,
    task_uuid: str,
    code: str,
    message: str,
) -> None:
    row = connection.execute(
        "SELECT wait_reason FROM workflow_task WHERE uuid = ?",
        (task_uuid,),
    ).fetchone()
    waited_at = utc_now()
    reason = {
        "code": code,
        "message": message,
        "waiting_since": _waiting_since(
            row["wait_reason"] if row is not None else None,
            fallback=waited_at,
        ),
    }
    connection.execute(
        "UPDATE workflow_task SET wait_reason = ?, update_time = ? WHERE uuid = ?",
        (encode_json(reason, sort_keys=True).decode("utf-8"), waited_at, task_uuid),
    )


def _waiting_since(value: Any, *, fallback: str) -> str:
    try:
        decoded = decode_json_bytes(str(value or "{}").encode("utf-8"))
    except (TypeError, ValueError, UnicodeError):
        return fallback
    if not isinstance(decoded, dict):
        return fallback
    existing = str(decoded.get("waiting_since") or "").strip()
    return existing or fallback


def _require_positive(value: int, *, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field} 必须是正整数")


__all__ = [
    "CapacityDecision",
    "activate_workflow_task",
    "admit_job_dispatch",
    "check_workflow_task_capacity",
]
