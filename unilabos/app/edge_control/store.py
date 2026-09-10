"""Edge 控制协议的本地持久状态。

Command 在确认前必须先落盘，业务 Event 在收到后端 ``event.ack`` 前保留在
Outbox。该存储只保存协议恢复所需的最小数据，不承担工作流调度职责。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


@dataclass(frozen=True)
class StoredEvent:
    event_uuid: str
    event_type: str
    payload: Dict[str, Any]
    created_at: str
    traceparent: str = ""
    tracestate: str = ""


@dataclass(frozen=True)
class StoredJob:
    job_uuid: str
    task_uuid: str
    node_uuid: str
    command_uuid: str
    job_access_token: str
    status: str
    feedback_sequence: int
    traceparent: str = ""
    tracestate: str = ""


@dataclass(frozen=True)
class StoredOutcome:
    job_uuid: str
    outcome: str
    return_info: Dict[str, Any]
    error_info: List[Dict[str, Any]]


class EdgeControlStore:
    """线程安全的 SQLite Command/Outbox 存储。"""

    def __init__(self, path: str) -> None:
        expanded = Path(path).expanduser().resolve()
        expanded.parent.mkdir(parents=True, exist_ok=True)
        self.path = str(expanded)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS edge_control_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS edge_command (
                    command_uuid TEXT PRIMARY KEY,
                    sequence INTEGER NOT NULL,
                    type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    traceparent TEXT NOT NULL DEFAULT '',
                    tracestate TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    received_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_edge_command_sequence
                    ON edge_command(sequence);
                CREATE TABLE IF NOT EXISTS edge_event_outbox (
                    event_uuid TEXT PRIMARY KEY,
                    type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    traceparent TEXT NOT NULL DEFAULT '',
                    tracestate TEXT NOT NULL DEFAULT '',
                    last_sent_at REAL,
                    acked_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_edge_event_pending
                    ON edge_event_outbox(acked_at, last_sent_at);
                CREATE TABLE IF NOT EXISTS edge_job_runtime (
                    job_uuid TEXT PRIMARY KEY,
                    task_uuid TEXT NOT NULL,
                    node_uuid TEXT NOT NULL,
                    command_uuid TEXT NOT NULL,
                    job_access_token TEXT NOT NULL,
                    status TEXT NOT NULL,
                    feedback_sequence INTEGER NOT NULL DEFAULT 0,
                    traceparent TEXT NOT NULL DEFAULT '',
                    tracestate TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS edge_job_outcome_pending (
                    job_uuid TEXT PRIMARY KEY,
                    outcome TEXT NOT NULL,
                    return_info_json TEXT NOT NULL,
                    error_info_json TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                """
            )
            # 已存在的状态库原地补列，避免 Edge 升级后丢失未完成任务。
            self._ensure_column(
                "edge_event_outbox", "traceparent", "TEXT NOT NULL DEFAULT ''"
            )
            self._ensure_column(
                "edge_event_outbox", "tracestate", "TEXT NOT NULL DEFAULT ''"
            )
            self._ensure_column(
                "edge_job_runtime", "traceparent", "TEXT NOT NULL DEFAULT ''"
            )
            self._ensure_column(
                "edge_job_runtime", "tracestate", "TEXT NOT NULL DEFAULT ''"
            )
            # Pong only answers a ping from the current WebSocket session. Older
            # versions persisted it as a durable business event, which allowed a
            # stale pong to be replayed into a new session and closed by the
            # scheduler as a policy violation.
            self._connection.execute(
                "DELETE FROM edge_event_outbox WHERE type = 'pong'"
            )
            self._connection.commit()

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        columns = {
            str(row["name"])
            for row in self._connection.execute(f"PRAGMA table_info({table})")
        }
        if column not in columns:
            self._connection.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
            )

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def get_or_create_instance_uuid(self, configured: str = "") -> str:
        configured = configured.strip()
        if configured:
            parsed = str(uuid.UUID(configured))
            self.set_meta("instance_uuid", parsed)
            return parsed
        existing = self.get_meta("instance_uuid")
        if existing:
            return str(uuid.UUID(existing))
        generated = str(uuid.uuid4())
        self.set_meta("instance_uuid", generated)
        return generated

    def get_meta(self, key: str, fallback: str = "") -> str:
        with self._lock:
            row = self._connection.execute(
                "SELECT value FROM edge_control_meta WHERE key = ?", (key,)
            ).fetchone()
        return str(row["value"]) if row is not None else fallback

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO edge_control_meta(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )
            self._connection.commit()

    def record_command(self, envelope: Dict[str, Any]) -> bool:
        command_uuid = str(uuid.UUID(str(envelope["message_uuid"])))
        payload = envelope.get("payload")
        if not isinstance(payload, dict):
            raise ValueError("command payload must be an object")
        with self._lock:
            cursor = self._connection.execute(
                """
                INSERT OR IGNORE INTO edge_command(
                    command_uuid, sequence, type, payload_json, traceparent,
                    tracestate, status, received_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'received', ?)
                """,
                (
                    command_uuid,
                    int(envelope.get("sequence") or 0),
                    str(envelope.get("type") or ""),
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    str(envelope.get("traceparent") or ""),
                    str(envelope.get("tracestate") or ""),
                    time.time(),
                ),
            )
            self._connection.commit()
            return cursor.rowcount == 1

    def command_status(self, command_uuid: str) -> str:
        with self._lock:
            row = self._connection.execute(
                "SELECT status FROM edge_command WHERE command_uuid = ?",
                (command_uuid,),
            ).fetchone()
        return str(row["status"]) if row is not None else ""

    def mark_command_completed(self, command_uuid: str) -> None:
        with self._lock:
            self._connection.execute(
                "UPDATE edge_command SET status = 'completed' WHERE command_uuid = ?",
                (command_uuid,),
            )
            self._connection.commit()

    def last_ack_command_sequence(self) -> int:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT COALESCE(MAX(sequence), 0) AS sequence
                FROM edge_command WHERE status = 'completed'
                """
            ).fetchone()
        return int(row["sequence"])

    def enqueue_event(
        self,
        event_type: str,
        payload: Dict[str, Any],
        trace_context: Optional[Dict[str, str]] = None,
    ) -> str:
        event_uuid = str(uuid.uuid4())
        created_at = _utc_now()
        trace_context = trace_context or {}
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO edge_event_outbox(
                    event_uuid, type, payload_json, created_at,
                    traceparent, tracestate
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    event_uuid,
                    event_type,
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    created_at,
                    str(trace_context.get("traceparent") or ""),
                    str(trace_context.get("tracestate") or ""),
                ),
            )
            self._connection.commit()
        return event_uuid

    def pending_events(self, retry_before: float, limit: int = 100) -> List[StoredEvent]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT event_uuid, type, payload_json, created_at,
                       traceparent, tracestate
                FROM edge_event_outbox
                WHERE acked_at IS NULL
                  AND (last_sent_at IS NULL OR last_sent_at <= ?)
                ORDER BY rowid
                LIMIT ?
                """,
                (retry_before, limit),
            ).fetchall()
        return [
            StoredEvent(
                event_uuid=str(row["event_uuid"]),
                event_type=str(row["type"]),
                payload=json.loads(str(row["payload_json"])),
                created_at=str(row["created_at"]),
                traceparent=str(row["traceparent"]),
                tracestate=str(row["tracestate"]),
            )
            for row in rows
        ]

    def mark_event_sent(self, event_uuid: str) -> None:
        with self._lock:
            self._connection.execute(
                "UPDATE edge_event_outbox SET last_sent_at = ? WHERE event_uuid = ?",
                (time.time(), event_uuid),
            )
            self._connection.commit()

    def acknowledge_event(self, event_uuid: str) -> None:
        with self._lock:
            self._connection.execute(
                "UPDATE edge_event_outbox SET acked_at = ? WHERE event_uuid = ?",
                (time.time(), event_uuid),
            )
            self._connection.commit()

    def save_job_start(
        self,
        payload: Dict[str, Any],
        command_uuid: str,
        trace_context: Optional[Dict[str, str]] = None,
    ) -> bool:
        required = ("job_uuid", "task_uuid", "node_uuid", "job_access_token")
        if any(not str(payload.get(field) or "").strip() for field in required):
            raise ValueError("job.start identity and token are required")
        trace_context = trace_context or {}
        traceparent = str(trace_context.get("traceparent") or "")
        tracestate = str(trace_context.get("tracestate") or "")
        values = (
            str(uuid.UUID(str(payload["job_uuid"]))),
            str(uuid.UUID(str(payload["task_uuid"]))),
            str(uuid.UUID(str(payload["node_uuid"]))),
            str(uuid.UUID(command_uuid)),
            str(payload["job_access_token"]),
            traceparent,
            tracestate,
            time.time(),
        )
        with self._lock:
            if not traceparent and not tracestate:
                command = self._connection.execute(
                    """
                    SELECT traceparent, tracestate FROM edge_command
                    WHERE command_uuid = ?
                    """,
                    (str(uuid.UUID(command_uuid)),),
                ).fetchone()
                if command is not None:
                    traceparent = str(command["traceparent"])
                    tracestate = str(command["tracestate"])
                    values = values[:-3] + (traceparent, tracestate, values[-1])
            cursor = self._connection.execute(
                """
                INSERT OR IGNORE INTO edge_job_runtime(
                    job_uuid, task_uuid, node_uuid, command_uuid,
                    job_access_token, status, traceparent, tracestate, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'received', ?, ?, ?)
                """,
                values,
            )
            if cursor.rowcount == 0 and (traceparent or tracestate):
                self._connection.execute(
                    """
                    UPDATE edge_job_runtime
                    SET traceparent = CASE
                            WHEN traceparent = '' THEN ? ELSE traceparent END,
                        tracestate = CASE
                            WHEN tracestate = '' THEN ? ELSE tracestate END
                    WHERE job_uuid = ?
                    """,
                    (traceparent, tracestate, values[0]),
                )
            self._connection.commit()
            return cursor.rowcount == 1

    def get_job(self, job_uuid: str) -> Optional[StoredJob]:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT job_uuid, task_uuid, node_uuid, command_uuid,
                       job_access_token, status, feedback_sequence,
                       traceparent, tracestate
                FROM edge_job_runtime WHERE job_uuid = ?
                """,
                (job_uuid,),
            ).fetchone()
        return _stored_job(row) if row is not None else None

    def list_jobs(self, statuses: Iterable[str]) -> List[StoredJob]:
        status_list = list(statuses)
        if not status_list:
            return []
        placeholders = ",".join("?" for _ in status_list)
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT job_uuid, task_uuid, node_uuid, command_uuid,
                       job_access_token, status, feedback_sequence,
                       traceparent, tracestate
                FROM edge_job_runtime WHERE status IN ({placeholders})
                ORDER BY updated_at
                """,
                status_list,
            ).fetchall()
        return [_stored_job(row) for row in rows]

    def set_job_status(self, job_uuid: str, status: str) -> None:
        with self._lock:
            self._connection.execute(
                "UPDATE edge_job_runtime SET status = ?, updated_at = ? WHERE job_uuid = ?",
                (status, time.time(), job_uuid),
            )
            self._connection.commit()

    def next_feedback_sequence(self, job_uuid: str) -> int:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            row = self._connection.execute(
                "SELECT feedback_sequence FROM edge_job_runtime WHERE job_uuid = ?",
                (job_uuid,),
            ).fetchone()
            if row is None:
                self._connection.rollback()
                raise KeyError(job_uuid)
            sequence = int(row["feedback_sequence"]) + 1
            self._connection.execute(
                """
                UPDATE edge_job_runtime
                SET feedback_sequence = ?, updated_at = ?
                WHERE job_uuid = ?
                """,
                (sequence, time.time(), job_uuid),
            )
            self._connection.commit()
            return sequence

    def save_pending_outcome(
        self,
        job_uuid: str,
        outcome: str,
        return_info: Dict[str, Any],
        error_info: List[Dict[str, Any]],
    ) -> bool:
        """原子保存待提交终态，重复设备回调以第一次终态为准。"""

        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            cursor = self._connection.execute(
                """
                INSERT OR IGNORE INTO edge_job_outcome_pending(
                    job_uuid, outcome, return_info_json, error_info_json, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    job_uuid,
                    outcome,
                    json.dumps(return_info, ensure_ascii=False, separators=(",", ":")),
                    json.dumps(error_info, ensure_ascii=False, separators=(",", ":")),
                    time.time(),
                ),
            )
            if cursor.rowcount == 1:
                self._connection.execute(
                    """
                    UPDATE edge_job_runtime
                    SET status = 'outcome_pending', updated_at = ?
                    WHERE job_uuid = ?
                    """,
                    (time.time(), job_uuid),
                )
            self._connection.commit()
            return cursor.rowcount == 1

    def get_pending_outcome(self, job_uuid: str) -> Optional[StoredOutcome]:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT job_uuid, outcome, return_info_json, error_info_json
                FROM edge_job_outcome_pending WHERE job_uuid = ?
                """,
                (job_uuid,),
            ).fetchone()
        return _stored_outcome(row) if row is not None else None

    def list_pending_outcomes(self) -> List[StoredOutcome]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT job_uuid, outcome, return_info_json, error_info_json
                FROM edge_job_outcome_pending ORDER BY updated_at
                """
            ).fetchall()
        return [_stored_outcome(row) for row in rows]

    def complete_pending_outcome(
        self,
        job_uuid: str,
        event_payload: Dict[str, Any],
        trace_context: Optional[Dict[str, str]] = None,
    ) -> str:
        """原子清除 HTTP 待办并创建最终 WebSocket 通知。"""

        event_uuid = str(uuid.uuid4())
        created_at = _utc_now()
        trace_context = trace_context or {}
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            pending = self._connection.execute(
                "SELECT 1 FROM edge_job_outcome_pending WHERE job_uuid = ?",
                (job_uuid,),
            ).fetchone()
            if pending is None:
                self._connection.rollback()
                return ""
            traceparent = str(trace_context.get("traceparent") or "")
            tracestate = str(trace_context.get("tracestate") or "")
            if not traceparent and not tracestate:
                runtime = self._connection.execute(
                    """
                    SELECT traceparent, tracestate FROM edge_job_runtime
                    WHERE job_uuid = ?
                    """,
                    (job_uuid,),
                ).fetchone()
                if runtime is not None:
                    traceparent = str(runtime["traceparent"])
                    tracestate = str(runtime["tracestate"])
            self._connection.execute(
                """
                INSERT INTO edge_event_outbox(
                    event_uuid, type, payload_json, created_at,
                    traceparent, tracestate
                ) VALUES (?, 'job.outcome_committed', ?, ?, ?, ?)
                """,
                (
                    event_uuid,
                    json.dumps(
                        event_payload, ensure_ascii=False, separators=(",", ":")
                    ),
                    created_at,
                    traceparent,
                    tracestate,
                ),
            )
            self._connection.execute(
                """
                UPDATE edge_job_runtime
                SET status = 'outcome_committed', updated_at = ?
                WHERE job_uuid = ?
                """,
                (time.time(), job_uuid),
            )
            self._connection.execute(
                "DELETE FROM edge_job_outcome_pending WHERE job_uuid = ?",
                (job_uuid,),
            )
            self._connection.commit()
        return event_uuid

def _stored_job(row: sqlite3.Row) -> StoredJob:
    return StoredJob(
        job_uuid=str(row["job_uuid"]),
        task_uuid=str(row["task_uuid"]),
        node_uuid=str(row["node_uuid"]),
        command_uuid=str(row["command_uuid"]),
        job_access_token=str(row["job_access_token"]),
        status=str(row["status"]),
        feedback_sequence=int(row["feedback_sequence"]),
        traceparent=str(row["traceparent"]),
        tracestate=str(row["tracestate"]),
    )


def _stored_outcome(row: sqlite3.Row) -> StoredOutcome:
    return StoredOutcome(
        job_uuid=str(row["job_uuid"]),
        outcome=str(row["outcome"]),
        return_info=json.loads(str(row["return_info_json"])),
        error_info=json.loads(str(row["error_info_json"])),
    )


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + ".000000Z"
