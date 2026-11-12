"""V1 工作流干预（``/workflow-interventions*``）在 dev 决策链上的持久投影。

dev 的设备动作失败先由执行面挂起为"待决策"（``/error-decisions``），调度器把 attempt 标成
``intervention_required``（``mark_job_decision_pending``）。本模块在同一事务里把这份决策报告
持久化为 ``workflow_intervention`` 行（V1 形状：修订、候选方案、选择与投递状态），attempt
恢复运行或进入终态时把开放的干预收为 ``superseded``。

选择方案（``POST /workflow-interventions/{uuid}/decisions``）按修订与 ``Idempotency-Key``
冻结，再经与 ``/error-decisions`` 相同的放行通道投递给持有该决策的执行面。
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Callable, Dict, List, Mapping, Optional
from uuid import uuid4

from unilabos.server.services.runtime.workflow.errors import StoreConflict, StoreNotFound
from unilabos.server.services.runtime.workflow.store import _json, _load, utc_now

logger = logging.getLogger(__name__)

_STATUSES = frozenset({"open", "selected", "superseded"})


def _normalize_options(value: Any) -> List[Dict[str, Any]]:
    """V1 规则：每个方案有唯一 ``id``（缺省取 ``action``），1–100 个。"""

    if not isinstance(value, (list, tuple)) or not 1 <= len(value) <= 100:
        return [{"id": "abort", "action": "abort"}]
    seen: set[str] = set()
    result: List[Dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, Mapping):
            continue
        option = dict(raw)
        option_id = str(option.get("id") or option.get("action") or "").strip()
        if not option_id or len(option_id) > 255 or option_id in seen:
            continue
        option["id"] = option_id
        seen.add(option_id)
        result.append(option)
    return result or [{"id": "abort", "action": "abort"}]


def intervention_row(row: sqlite3.Row) -> Dict[str, Any]:
    meta = _load(row["meta_data"], {})
    result: Dict[str, Any] = {
        "uuid": row["uuid"],
        "create_time": row["create_time"],
        "update_time": row["update_time"],
        "meta_data": {key: value for key, value in meta.items() if key != "delivery_payload"},
        "workflow_task_uuid": row["workflow_task_uuid"],
        "workflow_node_job_uuid": row["workflow_node_job_uuid"],
        # 与 V1 一致：用执行面的 decision_id 充当稳定投递身份
        "edge_command_uuid": row["edge_command_uuid"] or meta.get("decision_id"),
        "revision": int(row["revision"]),
        "status": row["status"],
        "options": _load(row["options"], []),
        "resume_control_status": row["resume_control_status"],
        "selected_option": _load(row["selected_option"], {}),
        "delivery_status": meta.get("delivery_status", "none"),
        "opened_at": row["opened_at"],
    }
    for field in ("description", "selected_option_id", "decided_at"):
        if row[field] is not None:
            result[field] = row[field]
    if meta.get("delivered_at"):
        result["delivered_at"] = meta["delivered_at"]
    return result


def open_for_decision(conn: sqlite3.Connection, job: sqlite3.Row, report: Mapping[str, Any], now: str) -> None:
    """在 ``mark_job_decision_pending`` 的事务里开出（或沿用）该决策对应的干预。"""

    decision_id = str(report.get("decision_id") or "").strip()
    if not decision_id:
        return
    job_uuid = str(job["uuid"])
    current = conn.execute(
        "SELECT * FROM workflow_intervention WHERE workflow_node_job_uuid=? AND deleted_at IS NULL "
        "ORDER BY revision DESC LIMIT 1",
        (job_uuid,),
    ).fetchone()
    if current is not None and _load(current["meta_data"], {}).get("decision_id") == decision_id:
        return
    if current is not None and current["status"] == "open":
        conn.execute("UPDATE workflow_intervention SET status='superseded', update_time=? WHERE uuid=?",
                     (now, current["uuid"]))
    task = conn.execute("SELECT control_status, dispatch_gate, run_mode FROM workflow_task WHERE uuid=?",
                        (job["workflow_task_uuid"],)).fetchone()
    resume = "paused" if task is not None and (
        task["run_mode"] == "step" or task["dispatch_gate"] == "paused" or task["control_status"] == "paused"
    ) else "active"
    conn.execute(
        """
        INSERT INTO workflow_intervention(
            uuid, create_time, update_time, meta_data, workflow_task_uuid, workflow_node_job_uuid,
            edge_agent_uuid, revision, status, options, resume_control_status, opened_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)
        """,
        (
            str(uuid4()), now, now,
            _json({
                "decision_id": decision_id,
                "job_id": job_uuid,
                "device_id": str(report.get("device_id") or ""),
                "action_name": str(report.get("action_name") or report.get("action") or ""),
                "exception_type": str(report.get("exception_type") or ""),
                "error_message": str(report.get("error_message") or ""),
                "decision_timeout_seconds": report.get("decision_timeout_seconds", 300),
                "default_on_decision_timeout": report.get("default_on_decision_timeout", "abort"),
                "delivery_status": "none",
            }),
            job["workflow_task_uuid"], job_uuid, str(job["edge_agent_uuid"] or "local"),
            1 if current is None else int(current["revision"]) + 1,
            _json(_normalize_options(report.get("options"))), resume, now,
        ),
    )


def supersede_for_job(conn: sqlite3.Connection, job_uuid: str, now: str) -> None:
    """attempt 已恢复运行或进入终态：仍开放的干预不再可选。"""

    conn.execute(
        "UPDATE workflow_intervention SET status='superseded', update_time=? "
        "WHERE workflow_node_job_uuid=? AND status='open' AND deleted_at IS NULL",
        (now, job_uuid),
    )


class WorkflowInterventions:
    """V1 干预读写；``deliver(decision_id, payload) -> bool`` 投递到执行面。"""

    def __init__(self, store: Any, deliver: Optional[Callable[[str, Dict[str, Any]], bool]] = None) -> None:
        self._store = store
        self._deliver = deliver

    def list(self, *, status: str = "open", limit: int = 100) -> List[Dict[str, Any]]:
        normalized = (status or "").strip().lower() or "open"
        if normalized not in _STATUSES or isinstance(limit, bool) or not 1 <= int(limit) <= 500:
            raise ValueError("干预查询状态或 limit 非法")
        with self._store.write_lock:
            rows = self._store.connection.execute(
                "SELECT * FROM workflow_intervention WHERE status=? AND deleted_at IS NULL "
                "ORDER BY opened_at ASC, uuid ASC LIMIT ?",
                (normalized, int(limit)),
            ).fetchall()
        return [intervention_row(row) for row in rows]

    def get(self, intervention_uuid: str) -> Dict[str, Any]:
        with self._store.write_lock:
            row = self._store.connection.execute(
                "SELECT * FROM workflow_intervention WHERE uuid=? AND deleted_at IS NULL", (intervention_uuid,),
            ).fetchone()
        if row is None:
            raise StoreNotFound(f"workflow intervention {intervention_uuid} not found")
        return intervention_row(row)

    def select(
        self,
        intervention_uuid: str,
        *,
        revision: int,
        option_id: str,
        idempotency_key: str,
        result: Any = None,
        operator_id: str = "",
    ) -> Dict[str, Any]:
        option_id = str(option_id or "").strip()
        key = str(idempotency_key or "").strip()
        if revision < 1 or not option_id or not key:
            raise StoreConflict("干预 revision、option_id 和幂等键不能为空")
        now = utc_now()
        with self._store.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM workflow_intervention WHERE uuid=? AND deleted_at IS NULL", (intervention_uuid,),
            ).fetchone()
            if row is None:
                raise StoreNotFound(f"workflow intervention {intervention_uuid} not found")
            if int(row["revision"]) != revision:
                raise StoreConflict("干预修订已经过期")
            selected = next((item for item in _load(row["options"], []) if item.get("id") == option_id), None)
            if selected is None:
                raise StoreConflict("选择的方案不是设备提供的候选项")
            payload: Dict[str, Any] = {"option": selected, "action": str(selected.get("action") or option_id)}
            if result is not None:
                payload["result"] = result
            elif "result" in selected:
                payload["result"] = selected["result"]
            meta = _load(row["meta_data"], {})
            if row["status"] == "selected":
                if row["selected_option_id"] == option_id and row["decision_idempotency_key"] == key:
                    if meta.get("delivery_payload") not in (None, payload):
                        raise StoreConflict("同一干预幂等请求携带了不同结果")
                    if meta.get("delivery_status") == "accepted":
                        return {"intervention": intervention_row(row), "command_uuid": row["edge_command_uuid"],
                                "created": False}
                    created = False
                else:
                    raise StoreConflict("干预已经选择了另一处理方案")
            elif row["status"] != "open":
                raise StoreConflict("干预已经不再开放")
            else:
                created = True
                meta.update({"delivery_payload": payload, "operator_id": operator_id or "local-workspace"})
                conn.execute(
                    "UPDATE workflow_intervention SET status='selected', selected_option_id=?, selected_option=?, "
                    "decision_idempotency_key=?, edge_command_uuid=?, decided_at=?, meta_data=?, update_time=? "
                    "WHERE uuid=?",
                    (option_id, _json(selected), key, meta.get("decision_id") or intervention_uuid, now,
                     _json(meta), now, intervention_uuid),
                )
            decision_id = str(meta.get("decision_id") or "")
        accepted = False
        if self._deliver is not None and decision_id:
            try:
                accepted = bool(self._deliver(decision_id, {**payload, "reason": f"workflow intervention {intervention_uuid}",
                                                            "operator_id": operator_id or "local-workspace"}))
            except Exception:  # noqa: BLE001 - 投递失败记为 unknown，可用同一幂等键重试
                logger.exception("干预 %s 投递失败", intervention_uuid)
        with self._store.transaction() as conn:
            row = conn.execute("SELECT * FROM workflow_intervention WHERE uuid=?", (intervention_uuid,)).fetchone()
            meta = _load(row["meta_data"], {})
            meta["delivery_status"] = "accepted" if accepted else "unknown"
            if accepted:
                meta["delivered_at"] = utc_now()
            conn.execute("UPDATE workflow_intervention SET meta_data=?, update_time=? WHERE uuid=?",
                         (_json(meta), utc_now(), intervention_uuid))
            self._store._append_event(conn, event="workflow.task.changed",
                                      data={"workflow_task_uuid": row["workflow_task_uuid"],
                                            "intervention_uuid": intervention_uuid}, now=utc_now())
            row = conn.execute("SELECT * FROM workflow_intervention WHERE uuid=?", (intervention_uuid,)).fetchone()
        if not accepted:
            raise StoreConflict("执行面没有接受这项干预决定（决策已不在等待或执行面离线），可用同一幂等键重试")
        return {"intervention": intervention_row(row), "command_uuid": row["edge_command_uuid"], "created": created}


__all__ = ["WorkflowInterventions", "intervention_row", "open_for_decision", "supersede_for_job"]
