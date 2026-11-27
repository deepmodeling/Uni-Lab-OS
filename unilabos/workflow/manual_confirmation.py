"""人工确认包装设备动作的持久事实。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from unilabos.persistence.inspection import table_exists
from unilabos.workflow.event_writer import append_frontend_event
from unilabos.workflow.store import StoreConflict, StoreNotFound, WorkflowStore

DEFAULT_MANUAL_CONFIRMATION_TIMEOUT_SECONDS = 3600
MIN_MANUAL_CONFIRMATION_TIMEOUT_SECONDS = 1
MAX_MANUAL_CONFIRMATION_TIMEOUT_SECONDS = 86400

_COLUMNS = {
    "workflow_node_job_uuid",
    "workflow_task_uuid",
    "status",
    "opened_at",
    "deadline_at",
    "decided_at",
    "resolution_reason",
}


def normalize_manual_confirmation_config(value: Any) -> dict[str, int]:
    """校验独立于设备参数的人工确认配置。"""

    if value is None:
        value = {}
    if not isinstance(value, Mapping) or set(value) - {"timeout_seconds"}:
        raise StoreConflict("manual_confirmation 只允许 timeout_seconds")
    timeout = value.get(
        "timeout_seconds",
        DEFAULT_MANUAL_CONFIRMATION_TIMEOUT_SECONDS,
    )
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, int)
        or not MIN_MANUAL_CONFIRMATION_TIMEOUT_SECONDS
        <= timeout
        <= MAX_MANUAL_CONFIRMATION_TIMEOUT_SECONDS
    ):
        raise StoreConflict("manual_confirmation.timeout_seconds 必须在 1..86400")
    return {"timeout_seconds": timeout}


def ensure_manual_confirmation_schema(connection: sqlite3.Connection) -> None:
    """创建当前表；旧人工确认表无需兼容，检测到后直接替换。"""

    if table_exists(connection, "workflow_manual_confirmation"):
        columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(workflow_manual_confirmation)"
            ).fetchall()
        }
        if columns != _COLUMNS:
            connection.execute("DROP TABLE workflow_manual_confirmation")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS workflow_manual_confirmation (
            workflow_node_job_uuid TEXT PRIMARY KEY,
            workflow_task_uuid TEXT NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN ('pending', 'approved', 'rejected', 'timed_out', 'canceled')
            ),
            opened_at TEXT NOT NULL,
            deadline_at TEXT NOT NULL,
            decided_at TEXT,
            resolution_reason TEXT,
            FOREIGN KEY(workflow_task_uuid)
                REFERENCES workflow_task(uuid) ON DELETE RESTRICT,
            FOREIGN KEY(workflow_node_job_uuid)
                REFERENCES workflow_node_job(uuid) ON DELETE RESTRICT
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_manual_confirmation_task
        ON workflow_manual_confirmation(workflow_task_uuid, opened_at DESC,
                                        workflow_node_job_uuid DESC)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_manual_confirmation_pending_deadline
        ON workflow_manual_confirmation(deadline_at, workflow_node_job_uuid)
        WHERE status = 'pending'
        """
    )


def open_manual_confirmation(
    connection: sqlite3.Connection,
    *,
    job_row: sqlite3.Row,
    config: Mapping[str, Any] | None,
    opened_at: str,
) -> dict[str, Any]:
    """在完整资源准入事务内幂等开启一次人工确认。"""

    normalized = normalize_manual_confirmation_config(config)
    job_uuid = str(job_row["uuid"])
    task_uuid = str(job_row["workflow_task_uuid"])
    existing = connection.execute(
        "SELECT * FROM workflow_manual_confirmation "
        "WHERE workflow_node_job_uuid = ?",
        (job_uuid,),
    ).fetchone()
    if existing is not None:
        return _row(existing)
    opened = _parse_time(opened_at)
    deadline_at = _format_time(
        opened + timedelta(seconds=normalized["timeout_seconds"])
    )
    connection.execute(
        """
        INSERT INTO workflow_manual_confirmation(
            workflow_node_job_uuid, workflow_task_uuid, status,
            opened_at, deadline_at, decided_at, resolution_reason
        ) VALUES (?, ?, 'pending', ?, ?, NULL, NULL)
        """,
        (job_uuid, task_uuid, _format_time(opened), deadline_at),
    )
    append_frontend_event(
        connection,
        event="manual_confirmation.required",
        data={"task_uuid": task_uuid, "job_uuid": job_uuid},
        now=_format_time(opened),
    )
    row = connection.execute(
        "SELECT * FROM workflow_manual_confirmation "
        "WHERE workflow_node_job_uuid = ?",
        (job_uuid,),
    ).fetchone()
    assert row is not None
    return _row(row)


def close_pending_manual_confirmation(
    connection: sqlite3.Connection,
    *,
    job_uuid: str,
    status: str,
    decided_at: str,
    resolution_reason: str | None = None,
) -> bool:
    """随 Task Cancel/重启原子关闭尚未决定的人工确认并发出一次事件。"""

    if status not in {"timed_out", "canceled"}:
        raise StoreConflict("人工确认只能按 timed_out 或 canceled 自动关闭")
    current = connection.execute(
        "SELECT workflow_task_uuid FROM workflow_manual_confirmation "
        "WHERE workflow_node_job_uuid = ? AND status = 'pending'",
        (job_uuid,),
    ).fetchone()
    if current is None:
        return False
    changed = connection.execute(
        """
        UPDATE workflow_manual_confirmation
        SET status = ?, decided_at = ?, resolution_reason = ?
        WHERE workflow_node_job_uuid = ? AND status = 'pending'
        """,
        (status, decided_at, resolution_reason, job_uuid),
    ).rowcount
    if changed != 1:
        return False
    append_frontend_event(
        connection,
        event="manual_confirmation.resolved",
        data={
            "task_uuid": str(current["workflow_task_uuid"]),
            "job_uuid": job_uuid,
        },
        now=decided_at,
    )
    return True


class ManualConfirmationStore:
    """按 Job UUID 查询人工确认；写入由 TaskRuntimeProjection 统一完成。"""

    def __init__(self, store: WorkflowStore) -> None:
        self._store = store
        with store.transaction() as connection:
            ensure_manual_confirmation_schema(connection)

    def get(self, job_uuid: str) -> dict[str, Any]:
        return self.get_by_job(job_uuid)

    def get_by_job(self, job_uuid: str) -> dict[str, Any]:
        with self._store.read() as connection:
            row = connection.execute(
                "SELECT * FROM workflow_manual_confirmation "
                "WHERE workflow_node_job_uuid = ?",
                (job_uuid,),
            ).fetchone()
        if row is None:
            raise StoreNotFound(f"manual confirmation for job {job_uuid} not found")
        return _row(row)

    def list_by_task(
        self,
        task_uuid: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        """读取一个 Task 的确认记录，可复用调用方的事务快照。"""

        if connection is not None:
            return self._list_by_task_connection(connection, task_uuid)
        with self._store.read() as read_connection:
            return self._list_by_task_connection(read_connection, task_uuid)

    @staticmethod
    def _list_by_task_connection(
        connection: sqlite3.Connection,
        task_uuid: str,
    ) -> list[dict[str, Any]]:
        task = connection.execute(
            "SELECT 1 FROM workflow_task WHERE uuid = ? AND deleted_at IS NULL",
            (task_uuid,),
        ).fetchone()
        if task is None:
            raise StoreNotFound(f"workflow task {task_uuid} not found")
        rows = connection.execute(
            """
            SELECT * FROM workflow_manual_confirmation
            WHERE workflow_task_uuid = ?
            ORDER BY opened_at DESC, workflow_node_job_uuid DESC
            """,
            (task_uuid,),
        ).fetchall()
        return [_row(row) for row in rows]

    def list_by_tasks(
        self,
        task_uuids: Iterable[str],
    ) -> dict[str, dict[str, dict[str, Any]]]:
        """一次查询返回 Task→Job→Confirmation，避免展示列表 N+1。"""

        identities = tuple(dict.fromkeys(str(value) for value in task_uuids))
        if not identities:
            return {}
        placeholders = ",".join("?" for _ in identities)
        with self._store.read() as connection:
            rows = connection.execute(
                "SELECT * FROM workflow_manual_confirmation "
                f"WHERE workflow_task_uuid IN ({placeholders})",
                identities,
            ).fetchall()
        result: dict[str, dict[str, dict[str, Any]]] = {}
        for row in rows:
            task_uuid = str(row["workflow_task_uuid"])
            job_uuid = str(row["workflow_node_job_uuid"])
            result.setdefault(task_uuid, {})[job_uuid] = _row(row)
        return result

    def next_pending_deadline(self) -> dict[str, Any] | None:
        with self._store.read() as connection:
            row = connection.execute(
                """
                SELECT * FROM workflow_manual_confirmation
                WHERE status = 'pending'
                ORDER BY deadline_at ASC, workflow_node_job_uuid ASC
                LIMIT 1
                """
            ).fetchone()
        return _row(row) if row is not None else None

    def list_due(self, now: str) -> list[dict[str, Any]]:
        with self._store.read() as connection:
            rows = connection.execute(
                """
                SELECT * FROM workflow_manual_confirmation
                WHERE status = 'pending' AND deadline_at <= ?
                ORDER BY deadline_at ASC, workflow_node_job_uuid ASC
                """,
                (now,),
            ).fetchall()
        return [_row(row) for row in rows]


def _row(row: sqlite3.Row) -> dict[str, Any]:
    """兼容内部查询调用，把 SQLite 行委托给统一人工确认投影器。

    参数：``row`` 是人工确认表的一行。返回：与公开查询接口一致的人工确认事实；
    异常：缺失列时由统一投影器原样抛出，禁止静默补齐状态。
    """

    return manual_confirmation_projection(row)


def manual_confirmation_projection(row: sqlite3.Row) -> dict[str, Any]:
    """把人工确认表行转换为公开的任务作业投影。

    参数：``row`` 是 ``workflow_manual_confirmation`` 的 SQLite 行。返回：包含
    稳定作业/任务身份、状态、截止时间和可用人工操作的字典；非空决定字段也会
    被保留。异常：调用方传入缺少规范列的行时由 SQLite 行访问原样抛出，避免
    用不完整事实生成确认状态。
    """

    result: dict[str, Any] = {
        "workflow_node_job_uuid": str(row["workflow_node_job_uuid"]),
        "workflow_task_uuid": str(row["workflow_task_uuid"]),
        "status": str(row["status"]),
        "opened_at": str(row["opened_at"]),
        "deadline_at": str(row["deadline_at"]),
        "actions": (
            ["approve", "reject"] if str(row["status"]) == "pending" else []
        ),
    }
    for field in ("decided_at", "resolution_reason"):
        if row[field] is not None:
            result[field] = str(row[field])
    return result


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise StoreConflict("人工确认时间缺少时区")
    return parsed.astimezone(timezone.utc)


def _format_time(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = [
    "DEFAULT_MANUAL_CONFIRMATION_TIMEOUT_SECONDS",
    "MAX_MANUAL_CONFIRMATION_TIMEOUT_SECONDS",
    "MIN_MANUAL_CONFIRMATION_TIMEOUT_SECONDS",
    "ManualConfirmationStore",
    "close_pending_manual_confirmation",
    "ensure_manual_confirmation_schema",
    "normalize_manual_confirmation_config",
    "manual_confirmation_projection",
    "open_manual_confirmation",
]
