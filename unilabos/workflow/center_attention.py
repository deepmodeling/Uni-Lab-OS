"""当前仍需中心展示或人工处理的工作流事项。"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from unilabos.persistence.inspection import table_exists


def _json_object(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _manual_confirmation_item(row: sqlite3.Row) -> dict[str, Any]:
    """Project the existing manual-confirmation public fields unchanged."""

    return {
        "workflow_node_job_uuid": str(row["workflow_node_job_uuid"]),
        "workflow_task_uuid": str(row["workflow_task_uuid"]),
        "status": str(row["status"]),
        "opened_at": str(row["opened_at"]),
        "deadline_at": str(row["deadline_at"]),
        "actions": ["approve", "reject"] if row["status"] == "pending" else [],
    }


def _intervention_item(row: sqlite3.Row) -> dict[str, Any]:
    """投影一条尚未解决的原生干预事实，不增加中心专有别名。"""

    decision_kind = str(row["decision_kind"] or "device_intervention")
    item: dict[str, Any] = {
        "decision_kind": decision_kind,
        "workflow_task_uuid": str(row["workflow_task_uuid"]),
        "workflow_node_job_uuid": str(row["workflow_node_job_uuid"]),
        "revision": int(row["revision"]),
        "status": str(row["status"]),
        "options": _json_object(row["options"], []),
        "meta_data": _json_object(row["meta_data"], {}),
        "opened_at": str(row["opened_at"]),
    }
    if decision_kind == "device_intervention":
        item.update({
            "uuid": str(row["uuid"]),
            "delivery_status": str(row["delivery_status"]),
        })
    else:
        item.update({
            "decision_id": str(row["decision_id"]),
            "stage": row["stage"],
            "error_phase": row["error_phase"],
            "observed_error_epoch": int(row["observed_error_epoch"]),
        })
    return item


def _center_task_ids(connection: sqlite3.Connection, candidates: set[str]) -> set[str]:
    """只认可持久化收件绑定，不从连接、任务元数据或名称推断来源。"""
    if not candidates or not table_exists(connection, "center_invocation_inbox"):
        return set()
    result: set[str] = set()
    ordered = sorted(candidates)
    # 只查询当前事项涉及的任务，分批避免数据库参数数量限制。
    for start in range(0, len(ordered), 250):
        batch = ordered[start:start + 250]
        placeholders = ",".join("?" for _ in batch)
        rows = connection.execute(
            f"SELECT local_task_uuid FROM center_invocation_inbox WHERE local_task_uuid IN ({placeholders})",
            batch,
        ).fetchall()
        result.update(str(row["local_task_uuid"]) for row in rows)
    return result


def current_snapshot(connection: sqlite3.Connection) -> dict[str, Any]:
    """在同一读事务中生成完整当前快照。

    只包含待确认人工确认、open 的设备干预和原生生命周期中尚未解决的节点错误。
    仅上报已有中心调用绑定的任务；工站本地任务、不确定结果、普通终态失败、
    已解决异常和历史记录均不进入快照。
    """

    manual: list[dict[str, Any]] = []
    if table_exists(connection, "workflow_manual_confirmation"):
        rows = connection.execute(
            """
            SELECT m.workflow_node_job_uuid, m.workflow_task_uuid, m.status,
                   m.opened_at, m.deadline_at, m.resolution_reason,
                   j.workflow_node_uuid, j.status AS job_status
            FROM workflow_manual_confirmation AS m
            JOIN workflow_node_job AS j
              ON j.uuid = m.workflow_node_job_uuid
            JOIN workflow_task AS t
              ON t.uuid = m.workflow_task_uuid
            WHERE m.status = 'pending'
              AND j.deleted_at IS NULL
              AND t.deleted_at IS NULL
            ORDER BY m.opened_at, m.workflow_node_job_uuid
            """
        ).fetchall()
        for row in rows:
            manual.append(_manual_confirmation_item(row))

    exceptions: list[dict[str, Any]] = []
    if table_exists(connection, "workflow_intervention"):
        rows = connection.execute(
            """
            SELECT i.uuid, i.workflow_node_job_uuid, i.workflow_task_uuid,
                   i.decision_id, i.revision, i.status, i.options, i.opened_at,
                   i.meta_data, i.delivery_status, i.decision_kind,
                   i.stage, i.error_phase, i.observed_error_epoch
            FROM workflow_intervention AS i
            JOIN workflow_task AS t
              ON t.uuid = i.workflow_task_uuid
            WHERE ((i.decision_kind = 'device_intervention' AND i.status = 'open')
                   OR (i.decision_kind = 'edge_node_error'
                       AND i.status IN ('open', 'selected')
                       AND i.stage IN ('DECISION_REQUIRED', 'MANUAL_HANDLING', 'APPLYING')))
              AND i.deleted_at IS NULL
              AND t.deleted_at IS NULL
            ORDER BY i.opened_at, i.uuid
            """
        ).fetchall()
        for row in rows:
            exceptions.append(_intervention_item(row))

    human_interactions = []
    if table_exists(connection, "workflow_human_interaction"):
        from unilabos.workflow.human_commands import pending_human_interactions
        human_interactions = pending_human_interactions(connection)
    candidates = {item["workflow_task_uuid"] for item in manual + exceptions}
    candidates.update(item["task_uuid"] for item in human_interactions)
    center_tasks = _center_task_ids(connection, candidates)
    manual = [item for item in manual if item["workflow_task_uuid"] in center_tasks]
    exceptions = [item for item in exceptions if item["workflow_task_uuid"] in center_tasks]
    human_interactions = [item for item in human_interactions if item["task_uuid"] in center_tasks]
    items = {"manual_confirmations": manual, "exceptions": exceptions}
    # 无原生交互时保留旧快照结构，新数组仅作为兼容扩展。
    if human_interactions:
        items["human_interactions"] = human_interactions
    return {
        "manual_confirmation_count": len(manual) + len(human_interactions),
        "exception_count": len(exceptions),
        "items": items,
    }


def current_state(connection: sqlite3.Connection) -> tuple[Any, ...]:
    """按持久中心任务绑定读取原生事项小状态，不加载选项、表单或诊断正文。"""
    if not table_exists(connection, "center_invocation_inbox"):
        return ((), (), ())
    queries = (
        ("workflow_manual_confirmation", """SELECT m.workflow_node_job_uuid,m.status,m.opened_at,m.deadline_at
            FROM workflow_manual_confirmation m
            JOIN workflow_node_job j ON j.uuid=m.workflow_node_job_uuid
            JOIN workflow_task t ON t.uuid=m.workflow_task_uuid
            WHERE m.status='pending' AND j.deleted_at IS NULL AND t.deleted_at IS NULL
            AND EXISTS (SELECT 1 FROM center_invocation_inbox c WHERE c.local_task_uuid=m.workflow_task_uuid)
            ORDER BY m.workflow_node_job_uuid"""),
        ("workflow_intervention", """SELECT i.uuid,i.revision,i.status,i.delivery_status,
                i.stage,i.error_phase,i.observed_error_epoch
            FROM workflow_intervention i JOIN workflow_task t ON t.uuid=i.workflow_task_uuid
            WHERE ((i.decision_kind='device_intervention' AND i.status='open')
                OR (i.decision_kind='edge_node_error' AND i.status IN ('open','selected')
                    AND i.stage IN ('DECISION_REQUIRED','MANUAL_HANDLING','APPLYING')))
            AND i.deleted_at IS NULL AND t.deleted_at IS NULL
            AND EXISTS (SELECT 1 FROM center_invocation_inbox c WHERE c.local_task_uuid=i.workflow_task_uuid)
            ORDER BY i.uuid"""),
        ("workflow_human_interaction", """SELECT h.uuid,h.status,h.opened_at,h.deadline_at
            FROM workflow_human_interaction h
            JOIN workflow_node_job j ON j.uuid=h.job_uuid AND j.workflow_task_uuid=h.task_uuid
            JOIN workflow_task t ON t.uuid=h.task_uuid
            WHERE h.status='pending' AND j.deleted_at IS NULL AND t.deleted_at IS NULL
            AND t.status NOT IN ('succeeded','failed','canceled','canceling','timeout','execution_unknown')
            AND j.status NOT IN ('succeeded','failed','canceled','timeout')
            AND EXISTS (SELECT 1 FROM center_invocation_inbox c WHERE c.local_task_uuid=h.task_uuid)
            ORDER BY h.uuid"""),
    )
    return tuple(tuple(tuple(row) for row in connection.execute(query).fetchall())
                 if table_exists(connection, table) else () for table, query in queries)


__all__ = ["current_snapshot", "current_state"]
