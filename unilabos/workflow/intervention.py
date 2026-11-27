"""工作流人工干预（Intervention）的持久事实与修订选择。"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import uuid4

from unilabos.workflow.event_writer import append_frontend_event
from unilabos.workflow.json_codec import decode_json_bytes, encode_json
from unilabos.workflow.store import StoreConflict, StoreNotFound, WorkflowStore, utc_now

_STATUSES = {"open", "selected", "superseded"}


def _json(value: Any) -> str:
    return encode_json(value, sort_keys=True).decode("utf-8")


def _load(value: str) -> Any:
    return decode_json_bytes(value.encode("utf-8"))


def ensure_intervention_schema(connection: sqlite3.Connection) -> None:
    """幂等创建干预事实表与查询索引。"""

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS workflow_intervention (
            uuid TEXT PRIMARY KEY,
            create_time TEXT NOT NULL,
            update_time TEXT NOT NULL,
            deleted_at TEXT,
            description TEXT,
            meta_data TEXT NOT NULL DEFAULT '{}',
            workflow_task_uuid TEXT NOT NULL,
            workflow_node_job_uuid TEXT NOT NULL,
            decision_id TEXT NOT NULL UNIQUE,
            revision INTEGER NOT NULL CHECK (revision >= 1),
            status TEXT NOT NULL CHECK (status IN ('open', 'selected', 'superseded')),
            options TEXT NOT NULL,
            resume_control_status TEXT NOT NULL,
            selected_option_id TEXT,
            selected_option TEXT NOT NULL DEFAULT '{}',
            decision_idempotency_key TEXT,
            delivery_status TEXT NOT NULL DEFAULT 'none'
                CHECK (delivery_status IN ('none', 'pending', 'accepted', 'unknown')),
            opened_at TEXT NOT NULL,
            decided_at TEXT,
            delivered_at TEXT,
            FOREIGN KEY(workflow_task_uuid)
                REFERENCES workflow_task(uuid) ON DELETE RESTRICT,
            FOREIGN KEY(workflow_node_job_uuid)
                REFERENCES workflow_node_job(uuid) ON DELETE RESTRICT,
            UNIQUE(workflow_node_job_uuid, revision)
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_workflow_intervention_status
        ON workflow_intervention(status, opened_at, uuid)
        WHERE deleted_at IS NULL
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_workflow_intervention_job_revision
        ON workflow_intervention(workflow_node_job_uuid, revision DESC)
        WHERE deleted_at IS NULL
        """
    )


def _normalize_options(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise StoreConflict("工作流干预 options 必须是对象数组")
    if not 1 <= len(value) <= 100:
        raise StoreConflict("工作流干预 options 必须包含 1-100 个选项")
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, Mapping):
            raise StoreConflict("工作流干预 option 必须是对象")
        option = dict(raw)
        option_id = str(option.get("id") or option.get("action") or "").strip()
        if not option_id or len(option_id) > 255 or option_id in seen:
            raise StoreConflict("工作流干预 option.id 缺失、重复或过长")
        option["id"] = option_id
        seen.add(option_id)
        normalized.append(option)
    return normalized


class WorkflowInterventionStore:
    """工作流干预的单一 SQLite 事务边界。"""

    def __init__(self, store: WorkflowStore) -> None:
        self._store = store
        with store.transaction() as connection:
            ensure_intervention_schema(connection)

    def open_from_report(self, report: Mapping[str, Any]) -> dict[str, Any]:
        """把设备动作异常报告转换为可查询的工作流干预事实。"""

        task_uuid = str(report.get("task_id") or "").strip()
        job_uuid = str(report.get("job_id") or "").strip()
        decision_id = str(report.get("decision_id") or "").strip()
        if not task_uuid or not job_uuid or not decision_id:
            raise StoreConflict("干预报告缺少 task_id、job_id 或 decision_id")
        options = _normalize_options(report.get("options"))
        now = utc_now()
        with self._store.transaction() as connection:
            job = connection.execute(
                """
                SELECT workflow_task_uuid, status FROM workflow_node_job
                WHERE uuid = ? AND deleted_at IS NULL
                """,
                (job_uuid,),
            ).fetchone()
            if job is None or str(job["workflow_task_uuid"]) != task_uuid:
                raise StoreNotFound(f"workflow node job {job_uuid} not found")
            if str(job["status"]) not in {"dispatched", "running"}:
                raise StoreConflict("只有正在执行的作业可以请求人工干预")
            existing = connection.execute(
                """
                SELECT * FROM workflow_intervention
                WHERE decision_id = ? AND deleted_at IS NULL
                """,
                (decision_id,),
            ).fetchone()
            if existing is not None:
                if _load(existing["options"]) != options:
                    raise StoreConflict("同一干预报告携带了不同选项")
                return _row(existing)
            task = connection.execute(
                """
                SELECT control_status, dispatch_gate, execution_mode FROM workflow_task
                WHERE uuid = ? AND deleted_at IS NULL
                """,
                (task_uuid,),
            ).fetchone()
            if task is None:
                raise StoreNotFound(f"workflow task {task_uuid} not found")
            latest = connection.execute(
                """
                SELECT * FROM workflow_intervention
                WHERE workflow_node_job_uuid = ? AND deleted_at IS NULL
                ORDER BY revision DESC LIMIT 1
                """,
                (job_uuid,),
            ).fetchone()
            revision = 1 if latest is None else int(latest["revision"]) + 1
            if task["control_status"] == "waiting_intervention" and latest is not None:
                resume_status = str(latest["resume_control_status"])
            else:
                resume_status = str(task["dispatch_gate"] or task["control_status"] or "active")
                if resume_status not in {"active", "paused"}:
                    resume_status = (
                        "paused"
                        if task["execution_mode"] in {"step", "switching_to_step"}
                        else "active"
                    )
            if resume_status not in {"active", "paused"}:
                raise StoreConflict("任务当前不能进入人工干预等待状态")
            if latest is not None and str(latest["status"]) in {"open", "selected"}:
                connection.execute(
                    """
                    UPDATE workflow_intervention
                    SET status = 'superseded', update_time = ?
                    WHERE uuid = ? AND status IN ('open', 'selected')
                    """,
                    (now, latest["uuid"]),
                )
            intervention_uuid = str(uuid4())
            connection.execute(
                """
                INSERT INTO workflow_intervention(
                    uuid, create_time, update_time, deleted_at, description,
                    meta_data, workflow_task_uuid, workflow_node_job_uuid,
                    decision_id, revision, status, options,
                    resume_control_status, selected_option_id,
                    selected_option, decision_idempotency_key,
                    delivery_status, opened_at, decided_at, delivered_at
                ) VALUES (?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?, 'open', ?, ?,
                          NULL, '{}', NULL, 'none', ?, NULL, NULL)
                """,
                (
                    intervention_uuid,
                    now,
                    now,
                    _json({
                        "decision_timeout_seconds": report.get("decision_timeout_seconds", 300),
                        "default_on_decision_timeout": report.get("default_on_decision_timeout", "abort"),
                        "job_id": job_uuid,
                        "device_id": str(report.get("device_id") or ""),
                        "action_name": str(report.get("action_name") or ""),
                        "exception_type": str(report.get("exception_type") or ""),
                        "error_message": str(report.get("error_message") or ""),
                    }),
                    task_uuid,
                    job_uuid,
                    decision_id,
                    revision,
                    _json(options),
                    resume_status,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE workflow_task
                SET control_status = 'waiting_intervention', update_time = ?
                WHERE uuid = ?
                """,
                (now, task_uuid),
            )
            append_frontend_event(
                connection,
                event="workflow.runtime.changed",
                data={"workflow_task_uuid": task_uuid},
                now=now,
            )
            row = connection.execute(
                "SELECT * FROM workflow_intervention WHERE uuid = ?",
                (intervention_uuid,),
            ).fetchone()
            assert row is not None
            return _row(row)

    def list(self, *, status: str, limit: int) -> list[dict[str, Any]]:
        normalized = status.strip().lower() or "open"
        if normalized not in _STATUSES or not 1 <= limit <= 500:
            raise StoreConflict("干预查询状态或 limit 非法")
        with self._store.read() as connection:
            rows = connection.execute(
                """
                SELECT * FROM workflow_intervention
                WHERE status = ? AND deleted_at IS NULL AND decision_kind='device_intervention'
                ORDER BY opened_at ASC, uuid ASC LIMIT ?
                """,
                (normalized, limit),
            ).fetchall()
        return [_row(row) for row in rows]

    def get(self, intervention_uuid: str) -> dict[str, Any]:
        with self._store.read() as connection:
            row = connection.execute(
                """
                SELECT * FROM workflow_intervention
                WHERE uuid = ? AND deleted_at IS NULL
                """,
                (intervention_uuid,),
            ).fetchone()
        if row is None:
            raise StoreNotFound(f"workflow intervention {intervention_uuid} not found")
        return _row(row)

    def select(
        self,
        intervention_uuid: str,
        *,
        revision: int,
        option_id: str,
        idempotency_key: str,
        result: Any = None,
    ) -> tuple[dict[str, Any], bool]:
        """按修订与幂等键选择并持久冻结设备处理方案。

        参数：干预身份、修订、选项、幂等键和可选人工结果。返回：当前干预与是否
        首次选择。异常：修订/选项过期或同一幂等请求携带不同结果时冲突；实际投递
        载荷写入既有 ``meta_data`` JSON，服务重建后无需新表即可安全重投。
        """

        normalized_option = option_id.strip()
        normalized_key = idempotency_key.strip()
        if revision < 1 or not normalized_option or not normalized_key:
            raise StoreConflict("干预 revision、option_id 和幂等键不能为空")
        now = utc_now()
        with self._store.transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM workflow_intervention
                WHERE uuid = ? AND deleted_at IS NULL AND decision_kind='device_intervention'
                """,
                (intervention_uuid,),
            ).fetchone()
            if row is None:
                raise StoreNotFound(
                    f"workflow intervention {intervention_uuid} not found"
                )
            if int(row["revision"]) != revision:
                raise StoreConflict("干预修订已经过期")
            options = _load(row["options"])
            selected = next(
                (item for item in options if item.get("id") == normalized_option),
                None,
            )
            if selected is None:
                raise StoreConflict("选择的方案不是设备提供的候选项")
            delivery_payload: dict[str, Any] = {
                "option": selected,
                "action": str(selected.get("action") or normalized_option),
            }
            if result is not None:
                delivery_payload["result"] = result
            elif "result" in selected:
                delivery_payload["result"] = selected["result"]
            if row["status"] == "selected":
                if (
                    row["selected_option_id"] == normalized_option
                    and row["decision_idempotency_key"] == normalized_key
                ):
                    existing_meta = _load(row["meta_data"])
                    if (
                        isinstance(existing_meta, Mapping)
                        and existing_meta.get("delivery_payload") is not None
                        and existing_meta.get("delivery_payload") != delivery_payload
                    ):
                        raise StoreConflict("同一干预幂等请求携带了不同结果")
                    return _row(row), False
                raise StoreConflict("干预已经选择了另一处理方案")
            if row["status"] != "open":
                raise StoreConflict("干预已经不再开放")
            connection.execute(
                """
                UPDATE workflow_intervention
                SET status = 'selected', selected_option_id = ?,
                    selected_option = ?, decision_idempotency_key = ?,
                    meta_data = ?, delivery_status = 'pending',
                    decided_at = ?, update_time = ?
                WHERE uuid = ? AND status = 'open'
                """,
                (
                    normalized_option,
                    _json(selected),
                    normalized_key,
                    _json({
                        **dict(_load(row["meta_data"])),
                        "delivery_payload": delivery_payload,
                    }),
                    now,
                    now,
                    intervention_uuid,
                ),
            )
            decided = connection.execute(
                "SELECT * FROM workflow_intervention WHERE uuid = ?",
                (intervention_uuid,),
            ).fetchone()
            assert decided is not None
            append_frontend_event(
                connection,
                event="workflow.runtime.changed",
                data={"workflow_task_uuid": str(row["workflow_task_uuid"])},
                now=now,
            )
            return _row(decided), True

    def list_replayable_selected(self, *, limit: int = 500) -> list[dict[str, Any]]:
        """读取尚未明确投递成功的已选干预。

        参数：``limit`` 限制单次恢复规模。返回：按决定时间排序的 pending/unknown
        记录；已接受决定不会重投。异常：非法上限抛 ``StoreConflict``。
        """

        if not 1 <= limit <= 500:
            raise StoreConflict("干预恢复 limit 非法")
        with self._store.read() as connection:
            rows = connection.execute(
                """
                SELECT * FROM workflow_intervention
                WHERE status='selected' AND decision_kind='device_intervention'
                  AND delivery_status IN ('pending','unknown')
                  AND deleted_at IS NULL
                ORDER BY decided_at ASC,uuid ASC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [_row(row) for row in rows]

    def mark_delivery(self, intervention_uuid: str, *, accepted: bool) -> dict[str, Any]:
        """记录决定是否成功交给仍在等待的设备动作。"""

        now = utc_now()
        with self._store.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM workflow_intervention WHERE uuid = ?",
                (intervention_uuid,),
            ).fetchone()
            if row is None:
                raise StoreNotFound(
                    f"workflow intervention {intervention_uuid} not found"
                )
            connection.execute(
                """
                UPDATE workflow_intervention
                SET delivery_status = ?, delivered_at = ?, update_time = ?
                WHERE uuid = ? AND status = 'selected'
                """,
                (
                    "accepted" if accepted else "unknown",
                    now if accepted else None,
                    now,
                    intervention_uuid,
                ),
            )
            if accepted:
                connection.execute(
                    """
                    UPDATE workflow_task
                    SET control_status = ?, dispatch_gate = ?, update_time = ?
                    WHERE uuid = ? AND control_status = 'waiting_intervention'
                    """,
                    (
                        row["resume_control_status"],
                        row["resume_control_status"],
                        now,
                        row["workflow_task_uuid"],
                    ),
                )
            updated = connection.execute(
                "SELECT * FROM workflow_intervention WHERE uuid = ?",
                (intervention_uuid,),
            ).fetchone()
            assert updated is not None
            return _row(updated)


def settle_intervention_for_job(
    connection: sqlite3.Connection,
    *,
    job_uuid: str,
    now: str,
) -> None:
    """作业终态时关闭仍开放或等待投递的干预。"""

    connection.execute(
        """
        UPDATE workflow_intervention
        SET status = 'superseded', update_time = ?
        WHERE workflow_node_job_uuid = ? AND status IN ('open', 'selected')
            AND decision_kind='device_intervention'
        """,
        (now, job_uuid),
    )


def _row(row: sqlite3.Row) -> dict[str, Any]:
    result = {
        "uuid": row["uuid"],
        "create_time": row["create_time"],
        "update_time": row["update_time"],
        "meta_data": _load(row["meta_data"]),
        "workflow_task_uuid": row["workflow_task_uuid"],
        "workflow_node_job_uuid": row["workflow_node_job_uuid"],
        # Local 模式用设备侧 decision_id 承担 Backend EdgeCommandUUID 的稳定
        # 投递身份；名称保持公共合同，内部无需复制 Edge 命令表。
        "edge_command_uuid": row["decision_id"],
        "revision": int(row["revision"]),
        "status": row["status"],
        "options": _load(row["options"]),
        "resume_control_status": row["resume_control_status"],
        "selected_option": _load(row["selected_option"]),
        "delivery_status": row["delivery_status"],
        "opened_at": row["opened_at"],
    }
    for field in (
        "description",
        "selected_option_id",
        "decided_at",
        "delivered_at",
    ):
        if row[field] is not None:
            result[field] = row[field]
    return result


__all__ = [
    "WorkflowInterventionStore",
    "ensure_intervention_schema",
    "settle_intervention_for_job",
]
