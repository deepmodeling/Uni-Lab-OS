"""Loopback implementation of the production-shaped durable Edge protocol.

The Workspace Backend owns this adapter.  It persists dispatch intent and
results, while an independently restartable Edge Runtime consumes the same
HTTP/WebSocket contract used by the production Backend.  No device driver or
ROS object is imported here.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import math
import secrets
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from functools import partial
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse
from fastapi.websockets import WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field, field_validator

from unilabos.app.edge_control.local_edge_session import (
    LocalEdgeSessionStore,
    project_local_edge_readiness,
)
from unilabos.app.scheduler.dispatch import (
    CancelDispatchState,
    CommittedJobOutcome,
    DispatchPayload,
)
from unilabos.persistence import open_database
from unilabos.persistence.database import prepare_database_path
from unilabos.utils.tracing import inject_trace_context, normalize_trace_context

logger = logging.getLogger(__name__)

_PROTOCOL_VERSION = 1
_COMMAND_RETRY_SECONDS = 0.5
_REPLACED_SOCKET_CLOSE_TIMEOUT_SECONDS = 0.5


class _MaterialAliquotReceipt(BaseModel):
    """Edge 动作提交的单个分装目标实收量。"""

    model_config = ConfigDict(extra="forbid")

    target_material_uuid: uuid.UUID
    actual_quantity: float = Field(gt=0, strict=True)
    quantity_unit: str = Field(min_length=1)

    @field_validator("actual_quantity", mode="before")
    @classmethod
    def _reject_boolean_quantity(cls, value: Any) -> Any:
        """拒绝 Python 中可被当作数值的布尔值。"""

        if isinstance(value, bool):
            raise ValueError("actual_quantity 不能是布尔值")
        return value

    @field_validator("actual_quantity")
    @classmethod
    def _require_finite_quantity(cls, value: float) -> float:
        """拒绝无法写入库存事实的非有限实收量。"""

        if not math.isfinite(value):
            raise ValueError("actual_quantity 必须是有限数")
        return value

    @field_validator("quantity_unit")
    @classmethod
    def _normalize_quantity_unit(cls, value: str) -> str:
        """去除单位首尾空白并拒绝空单位。"""

        normalized = value.strip()
        if not normalized:
            raise ValueError("quantity_unit 不能为空")
        return normalized


class _DeviceStatusUpdate(BaseModel):
    """Edge Runtime 提交的设备实时可派发状态增量。"""

    model_config = ConfigDict(extra="forbid")

    online: bool
    dispatch_block_reason: str = ""
    unknown_command_ids: list[str] = Field(default_factory=list)
    status: dict[str, Any] = Field(default_factory=dict)

    @field_validator("unknown_command_ids")
    @classmethod
    def _normalize_unknown_command_ids(cls, value: list[str]) -> list[str]:
        """规范未知命令身份并拒绝空值或重复值。"""

        normalized = [item.strip() for item in value]
        if any(not item for item in normalized) or len(set(normalized)) != len(
            normalized
        ):
            raise ValueError("unknown_command_ids 包含空值或重复值")
        return sorted(normalized)


class LocalEdgeOutcomeConflict(ValueError):
    """表示不可变 Edge 结果与首次提交冲突。"""


class LocalEdgeAuthorityStore:
    """SQLite facts for one Local Backend's Edge sessions and jobs."""

    def __init__(self, path: str | Path) -> None:
        """打开工站调度进程独占的动作协议权威账本。

        参数：``path`` 是本地 SQLite 路径。返回无。异常：目录、连接或迁移失败
        原样传播；初始化启用 WAL 与同步提交，确保命令先落盘再通知动作进程，并
        清除不可能跨 Backend 进程存活的旧 WebSocket 连接事实。
        """

        self.path = prepare_database_path(path, create_parent=True)
        self._lock = threading.RLock()
        self.db = open_database(self.path, "authority")
        self._connection = self.db
        self._connection.row_factory = sqlite3.Row
        self.db.initialize(self._initialize_sqlite)
        self._sessions = LocalEdgeSessionStore(self._connection, self._lock)

    def _initialize_sqlite(self) -> None:
        with self._lock:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS local_edge_meta (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    execution_process_uuid TEXT,
                    execution_restart_pending TEXT,
                    sequence INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS local_edge_command (
                    command_uuid TEXT PRIMARY KEY,
                    sequence INTEGER NOT NULL UNIQUE,
                    type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    traceparent TEXT NOT NULL DEFAULT '',
                    tracestate TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    last_sent_at REAL,
                    created_at REAL NOT NULL,
                    acked_at REAL
                );
                CREATE TABLE IF NOT EXISTS local_edge_job (
                    job_uuid TEXT PRIMARY KEY,
                    task_uuid TEXT NOT NULL,
                    node_uuid TEXT NOT NULL,
                    command_uuid TEXT NOT NULL UNIQUE,
                    claim_uuid TEXT,
                    attempt INTEGER,
                    fences_json TEXT NOT NULL DEFAULT '[]',
                    local_device_id TEXT NOT NULL,
                    action_name TEXT NOT NULL,
                    action_type TEXT NOT NULL,
                    param_json TEXT NOT NULL,
                    token_hash TEXT NOT NULL,
                    device_action_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    feedback_sequence INTEGER NOT NULL DEFAULT 0,
                    outcome_json TEXT,
                    unknown_command_ids_json TEXT NOT NULL DEFAULT '[]',
                    projected_at REAL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS local_edge_job_feedback (
                    job_uuid TEXT NOT NULL,
                    sequence INTEGER NOT NULL CHECK (sequence > 0),
                    payload_json TEXT NOT NULL,
                    projected_at REAL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (job_uuid, sequence),
                    FOREIGN KEY (job_uuid) REFERENCES local_edge_job(job_uuid)
                );
                CREATE INDEX IF NOT EXISTS ix_local_edge_feedback_projection
                    ON local_edge_job_feedback(projected_at, job_uuid, sequence);
                """
            )
            self._collapse_local_edge_meta()
            job_columns = {
                str(row["name"])
                for row in self._connection.execute(
                    "PRAGMA table_info(local_edge_job)"
                ).fetchall()
            }
            for name, declaration in (
                ("execution_source", "TEXT NOT NULL DEFAULT 'workflow'"),
                ("claim_uuid", "TEXT"),
                ("attempt", "INTEGER"),
                ("fences_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("resolution_outcome_json", "TEXT"),
            ):
                if name not in job_columns:
                    self._connection.execute(
                        f"ALTER TABLE local_edge_job ADD COLUMN {name} {declaration}"
                    )
            command_columns = {
                str(row["name"])
                for row in self._connection.execute(
                    "PRAGMA table_info(local_edge_command)"
                ).fetchall()
            }
            for name in ("traceparent", "tracestate"):
                if name not in command_columns:
                    self._connection.execute(
                        "ALTER TABLE local_edge_command "
                        f"ADD COLUMN {name} TEXT NOT NULL DEFAULT ''"
                    )
            self._connection.commit()

    def _collapse_local_edge_meta(self) -> None:
        """把键值元数据收成一行。缺重启标记时保持 NULL。"""

        columns = {
            str(row["name"])
            for row in self._connection.execute(
                "PRAGMA table_info(local_edge_meta)"
            ).fetchall()
        }
        if "key" not in columns:
            return
        rows = {
            str(row["key"]): str(row["value"])
            for row in self._connection.execute(
                "SELECT key, value FROM local_edge_meta"
            ).fetchall()
        }
        sequence = int(rows["sequence"]) if "sequence" in rows else 0
        self._connection.execute(
            """
            CREATE TABLE local_edge_meta_row (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                execution_process_uuid TEXT,
                execution_restart_pending TEXT,
                sequence INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        self._connection.execute(
            """
            INSERT INTO local_edge_meta_row(
                id, execution_process_uuid, execution_restart_pending, sequence
            ) VALUES (1, ?, ?, ?)
            """,
            (
                rows.get("execution_process_uuid"),
                rows.get("execution_restart_pending"),
                sequence,
            ),
        )
        self._connection.execute("DROP TABLE local_edge_meta")
        self._connection.execute(
            "ALTER TABLE local_edge_meta_row RENAME TO local_edge_meta"
        )

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def register_session(self, payload: dict[str, Any]) -> dict[str, Any]:
        """通过兼容门面持久化一份尚未连接的 Edge 注册。

        参数：``payload`` 包含 Edge 身份、实例 UUID 与设备能力。返回稳定
        ``edge_uuid`` 和新 ``session_uuid``。异常：载荷非法时抛 ``ValueError``，
        SQLite 错误原样传播；具体会话语义由 ``LocalEdgeSessionStore`` 独占。
        """

        return self._sessions.register_session(payload)

    def set_session_connected(self, session_uuid: str, connected: bool) -> None:
        """原子切换唯一当前 Edge 会话的连接状态。

        参数：``session_uuid`` 是已注册会话，``connected`` 表示 WebSocket 当前
        是否已完成 hello。返回无。异常：会话不存在时抛 ``ValueError``；数据库
        错误回滚。连接新会话时先关闭所有旧连接事实，避免 Backend 非正常重启
        留下的陈旧 ``connected=1`` 使实时就绪探针误报。
        """

        self._sessions.set_session_connected(session_uuid, connected)

    def update_device_status(
        self,
        session_uuid: str,
        local_device_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        """通过会话深模块更新一个设备的实时可派发事实。"""

        return self._sessions.update_device_status(
            session_uuid,
            local_device_id,
            payload,
        )

    def pending_device_property_reports(self) -> list[dict[str, Any]]:
        return self._sessions.pending_property_reports()

    def acknowledge_device_property_report(self, report_id: int) -> None:
        self._sessions.acknowledge_property_report(report_id)

    def set_device_property_history_enabled(self, enabled: bool) -> None:
        self._sessions.set_property_history_enabled(enabled)

    def disconnect_session(self, session_uuid: str) -> list[str]:
        """关闭一个 WebSocket 会话并仅在 Edge 真离线时锁定在途作业。

        参数：``session_uuid`` 是进入 ``finally`` 的会话身份。返回：仅当该会话
        原本确为 active 且 Authority 已无在线接管会话时转为 ``unknown`` 的 Job
        UUID；否则为空列表。异常：会话不存在时抛 ``ValueError``，SQLite 错误
        原样传播；会话关闭、全局代际检查与作业转换共享一个写事务。
        """

        edge_offline, affected = self._sessions.disconnect_session(
            session_uuid,
            self._mark_disconnected_jobs_unknown_locked,
        )
        return list(affected or ()) if edge_offline else []

    def reconcile_hello(self, payload: dict[str, Any]) -> tuple[bool, tuple[str, ...]]:
        """为身份匹配的已注册会话提交 hello 对账事实。

        参数：``payload`` 必须包含注册返回的 Session/Edge UUID、动作进程 UUID、
        ACK 游标与运行作业。返回：是否发生进程重启及受影响 Job UUID。异常：
        身份、游标、运行作业或注册绑定非法时整个事务回滚；任何对账写都发生在
        Session 存在且 Edge 身份匹配的验证之后。
        """

        (
            session_uuid,
            edge_uuid,
            process_uuid,
            last_ack,
            running_jobs,
        ) = _normalize_edge_hello(payload)
        return self._sessions.reconcile_registered_session(
            session_uuid,
            edge_uuid,
            partial(
                self._reconcile_hello_locked,
                process_uuid=process_uuid,
                last_ack=last_ack,
                running_jobs=running_jobs,
            ),
        )

    @contextmanager
    def activate_session_and_reconcile(
        self,
        payload: dict[str, Any],
    ) -> Iterator[tuple[str, bool, tuple[str, ...]]]:
        """原子验证、对账并激活一份尚未连接的 hello 注册。

        参数：``payload`` 是完整 hello 载荷。返回：上下文中产出规范 Session
        UUID、进程是否重启及受影响 Job UUID；调用方须在上下文内同步切换内存
        generation，严禁跨 ``await``。异常：未知、伪造或已 active 的 Session，
        非法对账载荷、内存切换及 SQLite 提交失败均回滚整个激活事务。
        """

        (
            session_uuid,
            edge_uuid,
            process_uuid,
            last_ack,
            running_jobs,
        ) = _normalize_edge_hello(payload)
        with self._sessions.activation_transaction(
            session_uuid,
            edge_uuid,
            partial(
                self._reconcile_hello_locked,
                process_uuid=process_uuid,
                last_ack=last_ack,
                running_jobs=running_jobs,
            ),
        ) as (process_restarted, affected):
            yield session_uuid, process_restarted, affected

    def _reconcile_hello_locked(
        self,
        connection: sqlite3.Connection,
        *,
        process_uuid: str,
        last_ack: int,
        running_jobs: list[dict[str, Any]],
    ) -> tuple[bool, tuple[str, ...]]:
        """在已验证 Session 的调用方事务内写入 hello 对账事实。

        参数：``connection`` 已进入写事务；其余参数是规范化的进程身份、ACK
        游标与运行 Job 声明。返回：是否发生进程重启及受影响 Job UUID。异常：
        运行 Job 身份未知或 SQLite 写入失败时原样传播；本函数不提交或回滚。
        """

        previous = connection.execute(
            "SELECT execution_process_uuid, execution_restart_pending "
            "FROM local_edge_meta WHERE id=1"
        ).fetchone()
        previous_process_uuid = (
            str(previous["execution_process_uuid"])
            if previous is not None and previous["execution_process_uuid"]
            else ""
        )
        changed_process = bool(
            previous_process_uuid and previous_process_uuid != process_uuid
        )
        pending_restart = (
            previous is not None and previous["execution_restart_pending"] is not None
        )
        process_restarted = changed_process or pending_restart
        connection.execute(
            """
            INSERT INTO local_edge_meta(
                id, execution_process_uuid, execution_restart_pending
            ) VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                execution_process_uuid=excluded.execution_process_uuid,
                execution_restart_pending=CASE
                    WHEN ? THEN excluded.execution_process_uuid
                    ELSE local_edge_meta.execution_restart_pending
                END
            """,
            (process_uuid, process_uuid if changed_process else None, changed_process),
        )
        connection.execute(
            """
            UPDATE local_edge_command
            SET status = 'acked', acked_at = COALESCE(acked_at, ?)
            WHERE sequence <= ?
            """,
            (time.time(), last_ack),
        )
        connection.execute(
            """
            UPDATE local_edge_job
            SET status = 'dispatched', updated_at = ?
            WHERE status = 'pending' AND command_uuid IN (
                SELECT command_uuid FROM local_edge_command
                WHERE sequence <= ? AND status = 'acked'
            )
            """,
            (time.time(), last_ack),
        )
        affected: tuple[str, ...] = ()
        if process_restarted:
            rows = connection.execute(
                """
                SELECT job_uuid FROM local_edge_job
                WHERE (
                    status IN (
                        'dispatched', 'running', 'cancel_requested', 'unknown'
                    )
                    OR (? AND status='pending'))
                  AND outcome_json IS NULL
                ORDER BY created_at, job_uuid
                """,
                (bool(getattr(self, "time_scheduling_enabled", False)),),
            ).fetchall()
            affected = tuple(str(row["job_uuid"]) for row in rows)
            for job_uuid in affected:
                connection.execute(
                    """
                    UPDATE local_edge_job
                    SET status = 'unknown', unknown_command_ids_json = ?, updated_at = ?
                    WHERE job_uuid = ?
                    """,
                    (
                        json.dumps([f"workflow-node-job:{job_uuid}"]),
                        time.time(),
                        job_uuid,
                    ),
                )
        else:
            for reported in running_jobs:
                job_uuid = str(uuid.UUID(_required_text(reported, "job_uuid")))
                command_uuid = str(
                    uuid.UUID(_required_text(reported, "command_uuid"))
                )
                changed = connection.execute(
                    """
                    UPDATE local_edge_job
                    SET status = CASE
                            WHEN status = 'cancel_requested'
                                OR ? = 'cancel_requested'
                                THEN 'cancel_requested'
                            ELSE 'running'
                        END, unknown_command_ids_json = '[]',
                        updated_at = ?
                    WHERE job_uuid = ? AND command_uuid = ?
                      AND outcome_json IS NULL
                    """,
                    (
                        str(reported.get("state") or "running"),
                        time.time(),
                        job_uuid,
                        command_uuid,
                    ),
                ).rowcount
                if changed != 1:
                    raise ValueError("running Edge job identity is unknown")
        return process_restarted, affected

    def complete_execution_restart(self) -> None:
        """调度侧全部终结成功后确认重启通知，失败或掉电时保留重试标记。"""
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE local_edge_meta SET execution_restart_pending=NULL WHERE id=1"
            )

    def mark_disconnected_jobs_unknown(self) -> list[str]:
        """显式锁定可能已跨越物理动作边界的所有在途作业。

        参数：无。返回：转为 ``unknown`` 的 Job UUID。异常：SQLite 错误导致
        整个事务回滚并原样传播。WebSocket 断线应调用 ``disconnect_session``，
        由会话代际检查决定是否执行本操作。
        """

        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                job_uuids = self._mark_disconnected_jobs_unknown_locked(
                    self._connection
                )
                self._connection.commit()
                return job_uuids
            except BaseException:
                self._connection.rollback()
                raise

    def _mark_disconnected_jobs_unknown_locked(
        self,
        connection: sqlite3.Connection,
    ) -> list[str]:
        """在调用方持有的写事务中固化物理动作结果不明事实。

        参数：``connection`` 是已进入 ``BEGIN IMMEDIATE`` 的 Authority 连接。
        返回：从 ``dispatched``、``running`` 或 ``cancel_requested`` 转为
        ``unknown`` 的 Job UUID。
        异常：SQLite 错误原样传播；本函数不提交也不回滚事务。
        """

        rows = connection.execute(
            """
            SELECT job_uuid FROM local_edge_job
            WHERE status IN ('dispatched', 'running', 'cancel_requested')
              AND outcome_json IS NULL
            """
        ).fetchall()
        job_uuids = [str(row["job_uuid"]) for row in rows]
        for job_uuid in job_uuids:
            connection.execute(
                """
                UPDATE local_edge_job
                SET status = 'unknown', unknown_command_ids_json = ?, updated_at = ?
                WHERE job_uuid = ?
                """,
                (
                    json.dumps([f"workflow-node-job:{job_uuid}"]),
                    time.time(),
                    job_uuid,
                ),
            )
        return job_uuids

    def dispatch(self, payload: DispatchPayload) -> dict[str, Any]:
        """持久化 Scheduler 已提交的 Command、Claim、Fence 与节点实参。

        参数：``payload`` 是物理派发前已通过九道门禁的工作流节点作业载荷。
        返回：本地 Edge 作业投影。异常：身份、资源栅栏或重复载荷冲突时抛
        ``ValueError``，事务完整回滚且不会发送 WebSocket 通知。
        """

        job_uuid = str(uuid.UUID(_required_text(payload, "job_id")))
        task_uuid = str(uuid.UUID(_required_text(payload, "task_id")))
        node_uuid = str(uuid.UUID(_required_text(payload, "node_id")))
        local_device_id = _required_text(payload, "device_id")
        action_name = _required_text(payload, "action")
        action_type = str(payload.get("action_type") or "")
        param = payload.get("action_args") or {}
        if not isinstance(param, dict):
            raise ValueError("action_args must be an object")
        device_action_key = f"/devices/{local_device_id}/{action_name}"
        command_uuid = str(uuid.UUID(_required_text(payload, "command_uuid")))
        execution_source = payload.get("execution_source", "workflow")
        if execution_source not in {"workflow", "error_manual"}:
            raise ValueError("invalid execution_source")
        manual = execution_source == "error_manual"
        claim_uuid = None if manual else str(uuid.UUID(_required_text(payload, "claim_uuid")))
        attempt = payload.get("attempt")
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            raise ValueError("attempt must be a positive integer")
        fences = _validate_fences(payload.get("fences"))
        if manual and (payload.get("claim_uuid") is not None or fences):
            raise ValueError("手动执行不得携带虚构 Claim/Fence")
        trace_context = _current_trace_context()
        now = time.time()
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            existing = self._connection.execute(
                "SELECT * FROM local_edge_job WHERE job_uuid = ?", (job_uuid,)
            ).fetchone()
            if existing is not None:
                self._connection.rollback()
                if (
                    existing["task_uuid"] != task_uuid
                    or existing["node_uuid"] != node_uuid
                    or existing["local_device_id"] != local_device_id
                    or existing["action_name"] != action_name
                    or existing["action_type"] != action_type
                    or json.loads(str(existing["param_json"])) != param
                    or existing["command_uuid"] != command_uuid
                    or existing["claim_uuid"] != claim_uuid
                    or existing["execution_source"] != execution_source
                    or int(existing["attempt"] or 0) != attempt
                    or json.loads(str(existing["fences_json"])) != fences
                ):
                    raise ValueError("duplicate local Edge job identity changed")
                return _job_projection(existing)
            blocked = self._connection.execute(
                """
                SELECT job_uuid FROM local_edge_job
                WHERE local_device_id = ? AND status = 'unknown'
                ORDER BY created_at LIMIT 1
                """,
                (local_device_id,),
            ).fetchone()
            if blocked is not None and not manual:
                self._connection.rollback()
                raise RuntimeError(
                    "device is locked by unresolved UNKNOWN job: "
                    f"{blocked['job_uuid']}"
                )
            sequence = self._next_sequence_locked()
            job_token = secrets.token_urlsafe(32)
            command_payload = {
                "job_uuid": job_uuid,
                "task_uuid": task_uuid,
                "node_uuid": node_uuid,
                "job_access_token": job_token,
                "executor_kind": "device_action",
                "execution_source": execution_source,
                "claim_uuid": claim_uuid,
                "attempt": attempt,
                "fences": fences,
            }
            try:
                self._connection.execute(
                    """
                    INSERT INTO local_edge_job(
                        job_uuid, task_uuid, node_uuid, command_uuid,
                        claim_uuid, attempt, fences_json,
                        local_device_id, action_name, action_type, param_json,
                        token_hash, device_action_key, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                    """,
                    (
                        job_uuid,
                        task_uuid,
                        node_uuid,
                        command_uuid,
                        claim_uuid,
                        attempt,
                        json.dumps(fences, separators=(",", ":")),
                        local_device_id,
                        action_name,
                        action_type,
                        json.dumps(param, ensure_ascii=False, separators=(",", ":")),
                        _token_hash(job_token),
                        device_action_key,
                        now,
                        now,
                    ),
                )
                self._connection.execute("UPDATE local_edge_job SET execution_source=? WHERE job_uuid=?", (execution_source, job_uuid))
                self._connection.execute(
                    """
                    INSERT INTO local_edge_command(
                        command_uuid, sequence, type, payload_json,
                        traceparent, tracestate, status, created_at
                    ) VALUES (?, ?, 'job.start', ?, ?, ?, 'pending', ?)
                    """,
                    (
                        command_uuid,
                        sequence,
                        json.dumps(
                            command_payload,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        trace_context.get("traceparent", ""),
                        trace_context.get("tracestate", ""),
                        now,
                    ),
                )
                self._connection.execute(
                    """
                    INSERT INTO local_edge_meta(id, sequence) VALUES (1, ?)
                    ON CONFLICT(id) DO UPDATE SET sequence = excluded.sequence
                    """,
                    (str(sequence),),
                )
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise
            row = self._connection.execute(
                "SELECT * FROM local_edge_job WHERE job_uuid = ?", (job_uuid,)
            ).fetchone()
        assert row is not None
        return _job_projection(row)

    def pending_commands(self) -> list[dict[str, Any]]:
        retry_before = time.time() - _COMMAND_RETRY_SECONDS
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT command_uuid, sequence, type, payload_json,
                       traceparent, tracestate
                FROM local_edge_command
                WHERE status NOT IN ('acked', 'retired')
                  AND (last_sent_at IS NULL OR last_sent_at <= ?)
                ORDER BY sequence
                """,
                (retry_before,),
            ).fetchall()
        commands: list[dict[str, Any]] = []
        for row in rows:
            command = {
                "protocol_version": _PROTOCOL_VERSION,
                "message_uuid": str(row["command_uuid"]),
                "sequence": int(row["sequence"]),
                "type": str(row["type"]),
                "sent_at": _utc_now(),
                "payload": json.loads(str(row["payload_json"])),
            }
            for key in ("traceparent", "tracestate"):
                if row[key]:
                    command[key] = str(row[key])
            commands.append(command)
        return commands

    def enqueue_error_decision(
        self,
        decision_id: str,
        *,
        job_id: str,
        device_id: str,
        decision: Mapping[str, Any],
    ) -> bool:
        """把已选择的异常处理决定持久化为下行 Edge 命令。"""

        command_uuid = str(uuid.UUID(decision_id))
        normalized_job = str(uuid.UUID(job_id))
        if not device_id:
            return False
        payload = {
            **dict(decision),
            "decision_id": command_uuid,
            "job_id": normalized_job,
            "device_id": str(device_id),
        }
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            existing = self._connection.execute(
                "SELECT type, payload_json FROM local_edge_command WHERE command_uuid = ?",
                (command_uuid,),
            ).fetchone()
            if existing is not None:
                return (
                    str(existing["type"]) == "job.error_decision"
                    and str(existing["payload_json"]) == encoded
                )
            job = self._connection.execute(
                "SELECT local_device_id FROM local_edge_job WHERE job_uuid = ?",
                (normalized_job,),
            ).fetchone()
            if job is None or str(job["local_device_id"]) != str(device_id):
                return False
            sequence = self._next_sequence_locked()
            now = time.time()
            self._connection.execute(
                """
                INSERT INTO local_edge_command(
                    command_uuid, sequence, type, payload_json,
                    traceparent, tracestate, status, created_at
                ) VALUES (?, ?, 'job.error_decision', ?, '', '', 'pending', ?)
                """,
                (command_uuid, sequence, encoded, now),
            )
            self._connection.execute(
                """
                INSERT INTO local_edge_meta(id, sequence) VALUES (1, ?)
                ON CONFLICT(id) DO UPDATE SET sequence = excluded.sequence
                """,
                (str(sequence),),
            )
            self._connection.commit()
        return True

    def enqueue_cancel(self, job_uuid: str) -> dict[str, Any] | None:
        """持久化一个幂等的 ``job.cancel`` 下行命令。

        这里只确认取消请求已写入本地控制面，不声称设备已经停止；设备收到
        命令后的取消回执仍由 Edge Runtime 通过结果接口提交。
        """

        normalized_job = str(uuid.UUID(job_uuid))
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            row = self._connection.execute(
                "SELECT status FROM local_edge_job WHERE job_uuid = ?",
                (normalized_job,),
            ).fetchone()
            if row is None:
                self._connection.rollback()
                return None
            if str(row["status"]) not in {
                "pending",
                "dispatched",
                "running",
                "unknown",
                "cancel_requested",
            }:
                self._connection.rollback()
                return None
            pending = self._connection.execute(
                """
                SELECT command_uuid, sequence, payload_json
                FROM local_edge_command
                WHERE type = 'job.cancel' AND status NOT IN ('acked', 'retired')
                ORDER BY sequence ASC
                """
            ).fetchall()
            for command in pending:
                payload = json.loads(str(command["payload_json"]))
                if payload.get("job_uuid") == normalized_job:
                    self._connection.rollback()
                    return {
                        "command_uuid": str(command["command_uuid"]),
                        "sequence": int(command["sequence"]),
                        "created": False,
                    }
            sequence = self._next_sequence_locked()
            command_uuid = str(uuid.uuid4())
            trace_context = _current_trace_context()
            payload = {"job_uuid": normalized_job}
            now = time.time()
            self._connection.execute(
                """
                INSERT INTO local_edge_command(
                    command_uuid, sequence, type, payload_json,
                    traceparent, tracestate, status, created_at
                ) VALUES (?, ?, 'job.cancel', ?, ?, ?, 'pending', ?)
                """,
                (
                    command_uuid,
                    sequence,
                    json.dumps(payload, separators=(",", ":")),
                    trace_context.get("traceparent", ""),
                    trace_context.get("tracestate", ""),
                    now,
                ),
            )
            self._connection.execute(
                """
                INSERT INTO local_edge_meta(id, sequence) VALUES (1, ?)
                ON CONFLICT(id) DO UPDATE SET sequence = excluded.sequence
                """,
                (str(sequence),),
            )
            self._connection.commit()
        return {"command_uuid": command_uuid, "sequence": sequence, "created": True}

    def mark_command_sent(self, command_uuid: str) -> None:
        with self._lock:
            self._connection.execute(
                "UPDATE local_edge_command SET last_sent_at = ? WHERE command_uuid = ?",
                (time.time(), command_uuid),
            )
            self._connection.commit()

    def acknowledge_command(self, command_uuid: str) -> None:
        normalized = str(uuid.UUID(command_uuid))
        with self._lock:
            self._connection.execute(
                """
                UPDATE local_edge_command
                SET status = 'acked', acked_at = COALESCE(acked_at, ?)
                WHERE command_uuid = ?
                """,
                (time.time(), normalized),
            )
            self._connection.execute(
                """
                UPDATE local_edge_job SET status = CASE
                    WHEN status = 'pending' THEN 'dispatched' ELSE status END,
                    updated_at = ? WHERE command_uuid = ?
                """,
                (time.time(), normalized),
            )
            self._connection.commit()

    def fetch_job(
        self,
        job_uuid: str,
        *,
        command_uuid: str,
        job_token: str,
    ) -> dict[str, Any]:
        """按命令 UUID 和一次性作业令牌读取实际派发载荷。

        参数：三个身份共同定位同一 Job 尝试。返回含 Claim/Fence 和实际参数的
        载荷。异常：作业不存在抛 ``KeyError``，凭据不匹配抛 ``PermissionError``。
        """

        normalized_job = str(uuid.UUID(job_uuid))
        normalized_command = str(uuid.UUID(command_uuid))
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM local_edge_job WHERE job_uuid = ?", (normalized_job,)
            ).fetchone()
        if row is None:
            raise KeyError(normalized_job)
        if row["command_uuid"] != normalized_command or not hmac.compare_digest(
            str(row["token_hash"]), _token_hash(job_token)
        ):
            raise PermissionError("job credential rejected")
        return {
            "job_uuid": normalized_job,
            "task_uuid": str(row["task_uuid"]),
            "node_uuid": str(row["node_uuid"]),
            "command_uuid": normalized_command,
            "claim_uuid": row["claim_uuid"],
            "execution_source": row["execution_source"],
            "attempt": int(row["attempt"]),
            "fences": json.loads(str(row["fences_json"])),
            "local_device_id": str(row["local_device_id"]),
            "action_name": str(row["action_name"]),
            "action_type": str(row["action_type"]),
            "param": json.loads(str(row["param_json"])),
        }

    def requeue_not_started(self, job_uuid: str) -> None:
        """调用方持有工站开始屏障；只重置无结果命令的投递状态。"""
        with self._lock, self._connection:
            self._connection.execute("UPDATE local_edge_command SET status='pending', last_sent_at=NULL, acked_at=NULL WHERE command_uuid IN (SELECT command_uuid FROM local_edge_job WHERE job_uuid=? AND outcome_json IS NULL)", (job_uuid,))
            self._connection.execute("UPDATE local_edge_job SET status='pending',unknown_command_ids_json='[]',updated_at=? WHERE job_uuid=? AND outcome_json IS NULL", (time.time(), job_uuid))

    def mark_job_started(self, job_uuid: str) -> None:
        with self._lock:
            self._connection.execute(
                """
                UPDATE local_edge_job SET status = CASE
                    WHEN status IN ('pending', 'dispatched') THEN 'running'
                    ELSE status END, updated_at = ? WHERE job_uuid = ?
                """,
                (time.time(), str(uuid.UUID(job_uuid))),
            )
            self._connection.commit()

    def commit_feedback(
        self,
        job_uuid: str,
        *,
        command_uuid: str,
        job_token: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """校验执行尝试身份并幂等保存一条 Edge 反馈事实。"""

        row = self._authorized_job(job_uuid, command_uuid, job_token)
        _validate_job_attempt_identity(row, job_uuid, command_uuid, payload)
        sequence = payload.get("sequence")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
            raise ValueError("feedback sequence is invalid")
        normalized_job = str(uuid.UUID(job_uuid))
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        now = time.time()
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                existing = self._connection.execute(
                    """
                    SELECT payload_json, projected_at
                    FROM local_edge_job_feedback
                    WHERE job_uuid = ? AND sequence = ?
                    """,
                    (normalized_job, sequence),
                ).fetchone()
                if existing is not None and str(existing["payload_json"]) != encoded:
                    raise ValueError(
                        "feedback sequence conflicts with its first committed payload"
                    )
                created = existing is None
                if created:
                    self._connection.execute(
                        """
                        INSERT INTO local_edge_job_feedback(
                            job_uuid, sequence, payload_json, created_at
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (normalized_job, sequence, encoded, now),
                    )
                through = max(int(row["feedback_sequence"]), sequence)
                self._connection.execute(
                    """
                    UPDATE local_edge_job SET feedback_sequence = ?, status = CASE
                        WHEN status IN ('pending', 'dispatched') THEN 'running'
                        ELSE status END, updated_at = ? WHERE job_uuid = ?
                    """,
                    (through, now, normalized_job),
                )
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise
        return {
            "through_sequence": through,
            "created": created,
            "projected": existing is not None and existing["projected_at"] is not None,
        }

    def pending_feedback_projections(self, *, limit: int = 1000) -> list[dict[str, Any]]:
        """读取 Edge 已提交、工作流库尚未确认的反馈事实。"""

        with self._lock:
            rows = self._connection.execute(
                """
                SELECT job_uuid, sequence, payload_json
                FROM local_edge_job_feedback
                WHERE projected_at IS NULL
                ORDER BY created_at, job_uuid, sequence
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        return [
            {
                "job_uuid": str(row["job_uuid"]),
                "sequence": int(row["sequence"]),
                "payload": json.loads(str(row["payload_json"])),
            }
            for row in rows
        ]

    def mark_feedback_projected(self, job_uuid: str, sequence: int) -> None:
        """仅在工作流库已幂等提交后标记反馈投影完成。"""

        with self._lock:
            self._connection.execute(
                """
                UPDATE local_edge_job_feedback
                SET projected_at = COALESCE(projected_at, ?)
                WHERE job_uuid = ? AND sequence = ?
                """,
                (time.time(), str(uuid.UUID(job_uuid)), int(sequence)),
            )
            self._connection.commit()

    def save_outcome(
        self,
        job_uuid: str,
        *,
        command_uuid: str,
        job_token: str,
        idempotency_key: str,
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        """幂等保存 Edge 作业不可变结果。

        参数：``job_uuid``、``command_uuid`` 与 ``job_token`` 共同授权同一个
        工作流节点作业尝试（WorkflowNodeJobAttempt）；``idempotency_key`` 是
        Backend HTTP 契约要求的稳定提交身份；``payload`` 保存结果终态与证据。
        返回：规范结果及是否首次创建。异常：身份、终态或重复内容冲突时拒绝，
        不覆盖首次已提交结果。
        """

        row = self._authorized_job(job_uuid, command_uuid, job_token)
        try:
            _validate_job_attempt_identity(row, job_uuid, command_uuid, payload)
        except ValueError as error:
            raise LocalEdgeOutcomeConflict(str(error)) from error
        normalized_idempotency_key = str(idempotency_key or "").strip()
        if not normalized_idempotency_key:
            raise ValueError("Idempotency-Key is required")
        task_uuid = str(uuid.UUID(_required_text(payload, "task_uuid")))
        node_uuid = str(uuid.UUID(_required_text(payload, "node_uuid")))
        if task_uuid != str(row["task_uuid"]) or node_uuid != str(row["node_uuid"]):
            raise LocalEdgeOutcomeConflict("job task or node identity does not match")
        outcome = str(payload.get("outcome") or "")
        if outcome not in {"succeeded", "failed", "canceled", "timeout"}:
            raise ValueError("outcome is invalid")
        return_info = payload.get("return_info")
        error_info = payload.get("error_info")
        if return_info is None:
            return_info = {}
        if error_info is None:
            error_info = []
        if not isinstance(return_info, dict):
            raise ValueError("return_info must be an object")
        if not isinstance(error_info, list):
            raise ValueError("error_info must be an array")
        raw_unknown_ids = payload.get("unknown_command_ids")
        if raw_unknown_ids is None:
            raw_unknown_ids = []
        unknown_ids = _normalize_unknown_command_ids(job_uuid, raw_unknown_ids)
        raw_consumptions = payload.get("inventory_consumptions")
        if raw_consumptions is None:
            raw_consumptions = []
        if not isinstance(raw_consumptions, list):
            raise ValueError("inventory_consumptions must be an array")
        inventory_consumptions: list[dict[str, Any]] = []
        seen_inventory: set[tuple[str, str]] = set()
        for index, consumption in enumerate(raw_consumptions):
            if not isinstance(consumption, dict):
                raise ValueError(
                    f"inventory_consumptions[{index}] must be an object"
                )
            inventory_type = str(
                consumption.get("inventory_type") or ""
            ).strip().lower()
            if inventory_type not in {"reagent", "current_substance"}:
                raise ValueError(
                    f"inventory_consumptions[{index}].inventory_type is invalid"
                )
            inventory_uuid = str(
                uuid.UUID(_required_text(consumption, "inventory_uuid"))
            )
            actual_quantity = consumption.get("actual_quantity")
            if (
                isinstance(actual_quantity, bool)
                or not isinstance(actual_quantity, (int, float))
                or not math.isfinite(float(actual_quantity))
                or float(actual_quantity) < 0
            ):
                raise ValueError(
                    f"inventory_consumptions[{index}].actual_quantity is invalid"
                )
            quantity_unit = _required_text(consumption, "quantity_unit")
            identity = (inventory_type, inventory_uuid)
            if identity in seen_inventory:
                raise ValueError(
                    "inventory_consumptions contains a duplicate inventory"
                )
            seen_inventory.add(identity)
            inventory_consumptions.append(
                {
                    "inventory_type": inventory_type,
                    "inventory_uuid": inventory_uuid,
                    "actual_quantity": float(actual_quantity),
                    "quantity_unit": quantity_unit,
                }
            )
        inventory_consumptions.sort(
            key=lambda item: (item["inventory_type"], item["inventory_uuid"])
        )
        raw_aliquot_receipts = payload.get("material_aliquot_receipts", [])
        if not isinstance(raw_aliquot_receipts, list):
            raise ValueError("material_aliquot_receipts must be an array")
        material_aliquot_receipts: list[dict[str, Any]] = []
        seen_targets: set[str] = set()
        for index, receipt in enumerate(raw_aliquot_receipts):
            if not isinstance(receipt, dict):
                raise ValueError(
                    f"material_aliquot_receipts[{index}] must be an object"
                )
            try:
                validated_receipt = _MaterialAliquotReceipt.model_validate(receipt)
            except ValueError as exc:
                raise ValueError(
                    f"material_aliquot_receipts[{index}] is invalid"
                ) from exc
            target_uuid = str(validated_receipt.target_material_uuid)
            if target_uuid in seen_targets:
                raise ValueError("material_aliquot_receipts contains a duplicate target")
            seen_targets.add(target_uuid)
            material_aliquot_receipts.append(
                {
                    "target_material_uuid": target_uuid,
                    "actual_quantity": validated_receipt.actual_quantity,
                    "quantity_unit": validated_receipt.quantity_unit,
                }
            )
        material_aliquot_receipts.sort(
            key=lambda item: item["target_material_uuid"]
        )
        normalized = {
            "outcome": outcome,
            "return_info": return_info,
            "error_info": error_info,
            "unknown_command_ids": unknown_ids,
            "inventory_consumptions": inventory_consumptions,
            "material_aliquot_receipts": material_aliquot_receipts,
        }
        existing = row["outcome_json"]
        if existing is None:
            committed_at = _utc_now()
            stored_outcome = {
                **normalized,
                "_idempotency_key": normalized_idempotency_key,
                "_result_uuid": str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"unilab:local-edge-job-result:{job_uuid}",
                    )
                ),
                "_create_time": committed_at,
                "_update_time": committed_at,
                "_committed_at": committed_at,
                "_edge_command_uuid": str(uuid.UUID(command_uuid)),
            }
        else:
            try:
                stored_outcome = json.loads(str(existing))
            except (TypeError, ValueError) as error:
                raise RuntimeError("stored job outcome is corrupted") from error
            expected = {
                **normalized,
                "_idempotency_key": normalized_idempotency_key,
            }
            observed = {
                key: stored_outcome.get(key)
                for key in expected
            }
            if observed != expected:
                raise LocalEdgeOutcomeConflict(
                    "job outcome conflicts with its first committed result"
                )
        encoded = json.dumps(
            stored_outcome,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        is_new = existing is None
        status = "unknown" if unknown_ids else "outcome_pending"
        with self._lock:
            self._connection.execute(
                """
                UPDATE local_edge_job
                SET outcome_json = COALESCE(outcome_json, ?),
                    unknown_command_ids_json = ?, status = CASE
                        WHEN projected_at IS NOT NULL THEN status ELSE ? END,
                    updated_at = ?
                WHERE job_uuid = ?
                """,
                (
                    encoded,
                    json.dumps(unknown_ids, separators=(",", ":")),
                    status,
                    time.time(),
                    str(uuid.UUID(job_uuid)),
                ),
            )
            self._connection.commit()
        return {
            **normalized,
            "_result": _job_result_projection(
                job_uuid=job_uuid,
                stored_outcome=stored_outcome,
            ),
        }, is_new

    def mark_outcome_projected(self, job_uuid: str) -> None:
        with self._lock:
            self._connection.execute(
                """
                UPDATE local_edge_job SET status = CASE
                        WHEN unknown_command_ids_json = '[]' THEN 'completed'
                        ELSE 'unknown'
                    END,
                    projected_at = COALESCE(projected_at, ?), updated_at = ?
                WHERE job_uuid = ?
                """,
                (time.time(), time.time(), str(uuid.UUID(job_uuid))),
            )
            self._connection.commit()

    def is_outcome_projected(self, job_uuid: str) -> bool:
        with self._lock:
            row = self._connection.execute(
                "SELECT projected_at FROM local_edge_job WHERE job_uuid = ?",
                (str(uuid.UUID(job_uuid)),),
            ).fetchone()
        return row is not None and row["projected_at"] is not None

    def pending_outcome_projections(self, *, limit: int = 1000) -> list[dict[str, Any]]:
        """读取尚未投影的 Edge 不可变结果，包含待人工收敛的 UNKNOWN 证据。"""

        with self._lock:
            rows = self._connection.execute(
                """
                SELECT job_uuid, COALESCE(resolution_outcome_json,outcome_json) AS outcome_json
                FROM local_edge_job
                WHERE (resolution_outcome_json IS NOT NULL OR outcome_json IS NOT NULL)
                  AND projected_at IS NULL
                ORDER BY updated_at, job_uuid
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        return [
            {
                "job_uuid": str(row["job_uuid"]),
                "outcome": {
                    key: value
                    for key, value in json.loads(str(row["outcome_json"])).items()
                    if not key.startswith("_")
                },
            }
            for row in rows
        ]

    def create_unknown_resolution(
        self, job_uuid: str, *, reason: str
    ) -> dict[str, Any]:
        normalized_job = str(uuid.UUID(job_uuid))
        if not reason.strip():
            raise ValueError("reason is required")
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            row = self._connection.execute(
                "SELECT * FROM local_edge_job WHERE job_uuid = ?", (normalized_job,)
            ).fetchone()
            if row is None:
                self._connection.rollback()
                raise KeyError(normalized_job)
            unknown_ids = json.loads(str(row["unknown_command_ids_json"]))
            if row["status"] == "failed":
                # Runtime 重启终结软件任务，不能代替物理结算。仅接受当前在线
                # 设备报告且明确属于本 Job 的命令；处置不改变已失败的任务终态。
                registration = self.latest_registration() or {}
                devices = registration.get("devices", []) if registration.get("connected") else []
                device = next((device for device in devices
                               if device.get("local_id") == row["local_device_id"]), {})
                root = f"workflow-node-job:{normalized_job}"
                try:
                    unknown_ids = _normalize_unknown_command_ids(normalized_job, [
                        value for value in device.get("unknown_command_ids", [])
                        if isinstance(value, str) and (value == root or value.startswith(root + ":"))
                    ])
                except (TypeError, ValueError):
                    self._connection.rollback()
                    raise ValueError("job has no unresolved UNKNOWN command") from None
            if row["status"] not in {"unknown", "failed"} or not unknown_ids:
                self._connection.rollback()
                raise ValueError("job has no unresolved UNKNOWN command")
            pending_rows = self._connection.execute(
                """
                SELECT command_uuid, sequence, payload_json
                FROM local_edge_command
                WHERE type = 'job.resolve_unknown' AND status = 'pending'
                ORDER BY sequence ASC
                """
            ).fetchall()
            for pending in pending_rows:
                existing_payload = json.loads(str(pending["payload_json"]))
                if existing_payload.get("job_uuid") != normalized_job:
                    continue
                if existing_payload.get("reason") != reason.strip():
                    self._connection.rollback()
                    raise ValueError("another UNKNOWN resolution is already pending")
                self._connection.rollback()
                return {
                    "command_uuid": str(pending["command_uuid"]),
                    "sequence": int(pending["sequence"]),
                    "created": False,
                }
            sequence = self._next_sequence_locked()
            command_uuid = str(uuid.uuid4())
            trace_context = _current_trace_context()
            payload = {
                "job_uuid": normalized_job,
                "local_device_id": str(row["local_device_id"]),
                "device_command_id": str(unknown_ids[0]),
                "resolution": "canceled",
                "reason": reason.strip(),
            }
            try:
                self._connection.execute(
                    """
                    INSERT INTO local_edge_command(
                        command_uuid, sequence, type, payload_json,
                        traceparent, tracestate, status, created_at
                    ) VALUES (?, ?, 'job.resolve_unknown', ?, ?, ?, 'pending', ?)
                    """,
                    (
                        command_uuid,
                        sequence,
                        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                        trace_context.get("traceparent", ""),
                        trace_context.get("tracestate", ""),
                        time.time(),
                    ),
                )
                self._connection.execute(
                    """
                    INSERT INTO local_edge_meta(id, sequence) VALUES (1, ?)
                    ON CONFLICT(id) DO UPDATE SET sequence = excluded.sequence
                    """,
                    (str(sequence),),
                )
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise
        return {"command_uuid": command_uuid, "sequence": sequence, "created": True}

    def resolve_unknown_committed(self, job_uuid: str) -> bool:
        normalized = str(uuid.UUID(job_uuid))
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT status,resolution_outcome_json FROM local_edge_job WHERE job_uuid = ?", (normalized,)
            ).fetchone()
            if row is not None and row["resolution_outcome_json"] is not None:
                # 前次已保存物理核实结果、投影中断：重投同一证据继续完成投影。
                return True
            if row is None or row["status"] != "unknown":
                return False
            resolution = json.dumps({
                "outcome": "canceled", "return_info": {}, "error_info": [],
                "unknown_command_ids": [], "inventory_consumptions": [],
                "material_aliquot_receipts": [],
            }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            self._connection.execute(
                """
                UPDATE local_edge_job SET status = 'outcome_pending',
                    unknown_command_ids_json = '[]', resolution_outcome_json=?,
                    projected_at = NULL, updated_at = ?
                WHERE job_uuid = ?
                """,
                (resolution, time.time(), normalized),
            )
        return True

    def fail_restarted_jobs(self, job_uuids: Sequence[str]) -> list[str]:
        """把工作流已失败的重启作业从 unknown 收成 failed，并释放忙碌键。

        参数：``job_uuids`` 是工作流侧已经失败并释放锁的作业身份。返回：本次
        实际从占用态收口的作业 UUID。异常：SQLite 错误回滚后原样传播。已有明确
        终态或账本中不存在的作业保持不变。
        """

        normalized = [str(uuid.UUID(str(job_uuid))) for job_uuid in job_uuids]
        if not normalized:
            return []
        now = time.time()
        failed: list[str] = []
        with self._lock:
            try:
                for job_uuid in normalized:
                    # 下行命令的作业身份键并不统一：job.start / job.cancel /
                    # job.resolve_unknown 用 ``job_uuid``，而 job.error_decision
                    # 用 ``job_id``。只匹配前者会把人工决定命令永久留在
                    # pending，Edge 每次重连都要重新收到一条已经没有物理意义
                    # 的命令。作业既然被强制收口，两种键都必须退役。
                    self._connection.execute(
                        "UPDATE local_edge_command SET status='retired' "
                        "WHERE status='pending' AND ? IN ("
                        "  json_extract(payload_json,'$.job_uuid'),"
                        "  json_extract(payload_json,'$.job_id')"
                        ")",
                        (job_uuid,),
                    )
                    row = self._connection.execute(
                        """
                        SELECT status, outcome_json FROM local_edge_job
                        WHERE job_uuid = ?
                        """,
                        (job_uuid,),
                    ).fetchone()
                    if row is None or str(row["status"]) not in {
                        "pending",
                        "dispatched",
                        "running",
                        "cancel_requested",
                        "unknown",
                        "outcome_pending",
                    }:
                        continue
                    outcome = row["outcome_json"]
                    if outcome is None:
                        outcome = json.dumps(
                            {
                                "outcome": "failed",
                                "return_info": {},
                                "error_info": [
                                    {
                                        "code": "execution_process_restarted",
                                        "message": (
                                            "设备执行进程重启，无法继续推进原工作流任务"
                                        ),
                                    }
                                ],
                                "unknown_command_ids": [],
                                "inventory_consumptions": [],
                                "material_aliquot_receipts": [],
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                    changed = self._connection.execute(
                        """
                        UPDATE local_edge_job
                        SET status = 'failed',
                            unknown_command_ids_json = '[]',
                            outcome_json = ?,
                            projected_at = COALESCE(projected_at, ?),
                            updated_at = ?
                        WHERE job_uuid = ?
                          AND status IN (
                              'pending', 'dispatched', 'running',
                              'cancel_requested',
                              'unknown', 'outcome_pending'
                          )
                        """,
                        (outcome, now, now, job_uuid),
                    ).rowcount
                    if changed == 1:
                        failed.append(job_uuid)
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise
        return failed

    def busy_device_action_keys(self) -> set[str]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT device_action_key FROM local_edge_job
                WHERE status IN (
                    'pending', 'dispatched', 'running', 'cancel_requested', 'unknown'
                )
                """
            ).fetchall()
        return {str(row["device_action_key"]) for row in rows}

    def job(self, job_uuid: str) -> dict[str, Any]:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM local_edge_job WHERE job_uuid = ?",
                (str(uuid.UUID(job_uuid)),),
            ).fetchone()
        if row is None:
            raise KeyError(job_uuid)
        return _job_projection(row)

    def online_devices(self) -> dict[str, dict[str, Any]]:
        """通过兼容门面读取唯一在线会话的设备投影。

        参数：无。返回：无在线会话时为空字典，否则按本地设备身份索引在线事实。
        异常：持久 JSON 或数据库损坏时原样传播；投影语义由会话深模块独占。
        """

        return self._sessions.online_devices()

    def latest_registration(self) -> dict[str, Any] | None:
        """返回最近一次 Edge 注册的脱离副本，不把 SQLite 行泄漏给调用方。

        参数：无。返回：尚未注册时为 ``None``，否则包含 Edge/实例身份、连接
        状态、时间戳和设备声明。异常：持久的设备声明不是对象数组时按空数组
        失败关闭，数据库错误原样传播。
        """

        return self._sessions.latest_registration()

    def _authorized_job(
        self, job_uuid: str, command_uuid: str, job_token: str
    ) -> sqlite3.Row:
        normalized = str(uuid.UUID(job_uuid))
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM local_edge_job WHERE job_uuid = ?", (normalized,)
            ).fetchone()
        if row is None:
            raise KeyError(normalized)
        if row["command_uuid"] != str(uuid.UUID(command_uuid)) or not hmac.compare_digest(
            str(row["token_hash"]), _token_hash(job_token)
        ):
            raise PermissionError("job credential rejected")
        return row

    def _next_sequence_locked(self) -> int:
        row = self._connection.execute(
            "SELECT sequence FROM local_edge_meta WHERE id = 1"
        ).fetchone()
        return int(row["sequence"]) + 1 if row is not None else 1


def _committed_outcome(payload: dict[str, Any]) -> CommittedJobOutcome:
    """把已校验的 Edge 结果恢复为保真投影对象。

    参数：``payload`` 是本地 Edge 发件箱中已提交的公共结果字段。返回：保留
    Backend 终态、返回值、错误证据、未知命令和实际库存消耗的不可变结果。
    异常：持久数据形状损坏时抛出 ``ValueError``，并保持该结果待投影，避免用
    默认值掩盖数据损坏。
    """

    return_info = payload.get("return_info")
    error_info = payload.get("error_info")
    unknown_command_ids = payload.get("unknown_command_ids")
    inventory_consumptions = payload.get("inventory_consumptions", [])
    material_aliquot_receipts = payload.get("material_aliquot_receipts", [])
    if not isinstance(return_info, dict):
        raise ValueError("committed return_info must be an object")
    if not isinstance(error_info, list):
        raise ValueError("committed error_info must be an array")
    if not isinstance(unknown_command_ids, list) or any(
        not isinstance(value, str) for value in unknown_command_ids
    ):
        raise ValueError("committed unknown_command_ids must be a string array")
    if not isinstance(inventory_consumptions, list) or any(
        not isinstance(value, dict) for value in inventory_consumptions
    ):
        raise ValueError("committed inventory_consumptions must be an object array")
    if not isinstance(material_aliquot_receipts, list) or any(
        not isinstance(value, dict) for value in material_aliquot_receipts
    ):
        raise ValueError("committed material_aliquot_receipts must be an object array")
    return CommittedJobOutcome(
        outcome=str(payload.get("outcome") or ""),
        return_info=dict(return_info),
        error_info=list(error_info),
        unknown_command_ids=list(unknown_command_ids),
        inventory_consumptions=[dict(value) for value in inventory_consumptions],
        material_aliquot_receipts=[
            dict(value) for value in material_aliquot_receipts
        ],
    )


def _legacy_outcome(payload: dict[str, Any]) -> tuple[bool, Any, str]:
    """把保真结果降级成旧四参数完成回调。

    参数：``payload`` 是已提交结果。返回：旧调度器需要的成功标记、返回值与
    终态类型；失败、取消和超时不再全部伪装成 ``normal``。异常：结果字段损坏
    时由 :func:`_committed_outcome` 抛出，调用方不会提前确认投影。
    """

    committed = _committed_outcome(payload)
    result = committed.return_info.get("return_value", committed.return_info)
    return (
        committed.outcome == "succeeded",
        result,
        "normal" if committed.outcome == "succeeded" else committed.outcome,
    )


class LocalEdgeControlAuthority:
    """Dispatcher plus loopback protocol authority owned by Local Backend."""

    def __init__(self, store: LocalEdgeAuthorityStore, *, api_key: str) -> None:
        """绑定本地 Edge 持久事实与协议密钥。

        参数：``store`` 是命令、反馈与结果的唯一写权威；``api_key`` 用于本地 Edge
        传输鉴别。返回无。异常：密钥为空时抛 ``ValueError``，不创建监听器状态。
        """

        if not api_key:
            raise ValueError(
                "本地 Edge 执行服务未配置工作区通信令牌；请通过 Workspace Host 启动，"
                "或设置 UNILABOS_EDGECONTROLCONFIG_API_KEY"
            )
        self.store = store
        self.api_key = api_key
        self.device_state = None
        self._device_property_projection_lock = threading.Lock()
        self._device_property_projection_stop = threading.Event()
        self._device_property_projection_wakeup = threading.Event()
        self._device_property_projection_thread: threading.Thread | None = None
        self._listeners: list[Callable[[str, bool, Any, str], None]] = []
        self._outcome_listeners: list[
            Callable[[str, CommittedJobOutcome], None]
        ] = []
        self._feedback_listeners: list[Callable[[str, dict[str, Any]], None]] = []
        self._execution_process_restarted_listeners: list[
            Callable[[tuple[str, ...]], None]
        ] = []
        self._execution_connection_lost_listeners: list[
            Callable[[tuple[str, ...]], None]
        ] = []
        self._error_decision_required_listeners: list[
            Callable[[dict[str, Any]], None]
        ] = []
        self._error_decision_reports: dict[str, dict[str, Any]] = {}
        self._resolved_error_decisions: set[str] = set()
        self._error_decision_lock = threading.Lock()
        self.execution_control: Any = None
        self._execution_started: Callable[[str], None] | None = None
        self.deferred_start = True

    def bind_execution_control(self, control: Any, on_started: Callable[[str], None]) -> None:
        self.execution_control = control
        self.store.time_scheduling_enabled = bool(
            getattr(control, "time_scheduling_enabled", False)
        )
        self._execution_started = on_started

    def start(self) -> None:
        """启动后端拥有的遥测重试消费者，不依赖 Edge 持续在线。"""
        if self._device_property_projection_thread is not None:
            return
        self._device_property_projection_thread = threading.Thread(
            target=self._run_device_property_projection,
            name="device-property-projector", daemon=True,
        )
        self._device_property_projection_thread.start()

    def _run_device_property_projection(self) -> None:
        while not self._device_property_projection_stop.is_set():
            if self.retry_device_property_projection():
                # 每页持锁，页间让出；积压连续消费到空，失败等待后再试。
                continue
            self._device_property_projection_wakeup.wait(5.0)
            self._device_property_projection_wakeup.clear()

    def project_device_properties(self) -> int:
        """按顺序投影一页属性；后台消费者持续翻页，失败保留源事实。"""
        if self.device_state is None:
            return 0
        from unilabos.app.scheduler.device_state import report_device_properties

        projected = 0
        with self._device_property_projection_lock:
            for report in self.store.pending_device_property_reports():
                if self._device_property_projection_stop.is_set():
                    break
                report_device_properties(
                    self.device_state, report["device_id"], report["properties"], now_ms=report["recorded_at"],
                )
                self.store.acknowledge_device_property_report(report["id"])
                projected += 1
        return projected

    def retry_device_property_projection(self) -> int:
        """遥测投影失败保留待办，不阻断会话接受或作业结果恢复。"""
        try:
            return self.project_device_properties()
        except Exception:
            logger.warning("设备属性历史投影失败，保留已接受记录等待重试", exc_info=True)
            return 0

    def configure_device_state(self, store: Any) -> None:
        """装配历史存储；显式关闭时停止积压属性投影。"""
        with self._device_property_projection_lock:
            self.device_state = store
            self.store.set_device_property_history_enabled(store is not None)
        self.retry_device_property_projection()
        self._device_property_projection_wakeup.set()

    def stop(self) -> None:
        self._device_property_projection_stop.set()
        self._device_property_projection_wakeup.set()
        if self._device_property_projection_thread is not None:
            self._device_property_projection_thread.join()
        self.store.close()

    def dispatch(self, payload: DispatchPayload) -> None:
        if payload.get("execution_source") == "error_manual" and (
            self.execution_control is None or not self.execution_control.is_manual(str(payload.get("job_id", "")))
        ):
            raise ValueError("手动执行来源未被工站控制器授权")
        self.store.dispatch(payload)

    def cancel(
        self,
        job_id: str,
        on_accepted: Callable[[bool], None],
    ) -> CancelDispatchState:
        """把取消请求写入 Edge 控制面并等待设备侧终态。

        ``REQUESTED`` 仅表示本地权威已持久化 ``job.cancel``；不会删除 Job、
        释放 Claim，也不会假设物理动作已经停止。后续由 Edge Runtime 提交
        ``canceled`` 或 ``failed`` 结果，调度器再做终态清理。
        """

        try:
            command = self.store.enqueue_cancel(job_id)
        except (TypeError, ValueError, KeyError):
            return CancelDispatchState.UNAVAILABLE
        if command is None:
            return CancelDispatchState.UNAVAILABLE
        # 命令已落盘并可由 WebSocket 重试投递；这是本地异步取消的受理事实，
        # 不是设备停止确认。
        try:
            on_accepted(True)
        except Exception:
            logger.exception("取消受理回调失败：%s", job_id)
            return CancelDispatchState.UNAVAILABLE
        return CancelDispatchState.REQUESTED

    def restore_dispatch(self, payload: DispatchPayload) -> None:
        """工站开始权威证明未启动后，恢复同一出站命令，绝不新建尝试。"""
        if self.execution_control is None:
            raise ValueError("恢复命令缺少工站控制权威")
        with self.execution_control.lock:
            if self.execution_control.execution_start_state(str(payload["job_id"])) != "NOT_STARTED":
                return
            self.dispatch(payload)
            self.store.requeue_not_started(str(payload["job_id"]))

    def add_error_decision_required_listener(
        self, listener: Callable[[dict[str, Any]], None]
    ) -> None:
        if listener not in self._error_decision_required_listeners:
            self._error_decision_required_listeners.append(listener)

    def remove_error_decision_required_listener(
        self, listener: Callable[[dict[str, Any]], None]
    ) -> None:
        self._error_decision_required_listeners = [
            current for current in self._error_decision_required_listeners
            if current != listener
        ]

    def publish_job_error_decision_required(self, report: dict[str, Any]) -> bool:
        """接收 Edge 动作异常，并先交给工作流权威持久化。"""

        decision_id = str(report.get("decision_id") or "")
        job_id = str(report.get("job_id") or "")
        device_id = str(report.get("device_id") or "")
        try:
            uuid.UUID(decision_id)
            uuid.UUID(job_id)
        except (TypeError, ValueError):
            return False
        if not device_id:
            return False
        try:
            for listener in tuple(self._error_decision_required_listeners):
                listener(dict(report))
        except Exception:
            logger.exception("[LocalEdgeControl] failed to persist error decision %s", decision_id)
            return False
        with self._error_decision_lock:
            self._error_decision_reports[decision_id] = dict(report)
            # 同一 decision_id 重新上报视为新的待决事件，清掉旧的已决标记。
            self._resolved_error_decisions.discard(decision_id)
        return True

    def list_error_decisions(self) -> list[dict[str, Any]]:
        """当前等待人工决策的动作异常（供 REST / 前端展示）。

        只返回尚未被 ``resolve_error_decision`` 成功投递的报告，
        并隐藏下划线开头的内部字段。
        返回列表；没有待决异常时为空列表，不抛异常。
        """

        with self._error_decision_lock:
            pending = [
                report
                for decision_id, report in self._error_decision_reports.items()
                if decision_id not in self._resolved_error_decisions
            ]
        return [
            {
                key: value
                for key, value in report.items()
                if not str(key).startswith("_")
            }
            for report in pending
        ]

    def resolve_error_decision(self, decision_id: str, decision: dict[str, Any]) -> bool:
        """把已持久选择的方案作为 Edge 命令投递给原始动作。"""

        identity = str(decision_id)
        with self._error_decision_lock:
            report = self._error_decision_reports.get(identity)
        job_id = str(
            (report or {}).get("job_id") or decision.get("job_id") or ""
        )
        device_id = str(
            (report or {}).get("device_id") or decision.get("device_id") or ""
        )
        delivered = self.store.enqueue_error_decision(
            identity,
            job_id=job_id,
            device_id=device_id,
            decision=decision,
        )
        if delivered:
            # 只标记不删除：enqueue 是幂等的，保留报告才能让重复投递继续命中
            # 原始 job_id/device_id，而不是退化成空串触发 UUID 解析失败。
            with self._error_decision_lock:
                self._resolved_error_decisions.add(identity)
        return delivered

    def add_job_finished_listener(
        self, listener: Callable[[str, bool, Any, str], None]
    ) -> None:
        self._listeners.append(listener)

    def add_job_outcome_listener(
        self,
        listener: Callable[[str, CommittedJobOutcome], None],
    ) -> None:
        """注册不可变作业结果的保真投影监听器。

        参数：``listener`` 接收作业 UUID 和完整 Edge 结果证据。返回无。异常：
        监听器异常在提交或重放时向上传播，使结果保持待投影而不是被误确认。
        """

        self._outcome_listeners.append(listener)

    def add_job_feedback_listener(
        self,
        listener: Callable[[str, dict[str, Any]], None],
    ) -> None:
        """注册已持久提交反馈的投影监听器。"""

        self._feedback_listeners.append(listener)

    def add_execution_process_restarted_listener(
        self,
        listener: Callable[[tuple[str, ...]], None],
    ) -> None:
        """注册动作执行进程重启监听器。

        参数：``listener`` 接收已经越过派发边界且终态未知的作业 UUID。返回无。
        异常：监听器异常向上传播，使同进程的工站调度权威显式暴露投影失败；
        本地动作账本已在通知前保留不确定事实与资源占用。
        """

        self._execution_process_restarted_listeners.append(listener)

    def remove_execution_process_restarted_listener(
        self,
        listener: Callable[[tuple[str, ...]], None],
    ) -> None:
        """幂等移除动作执行进程重启监听器。"""

        self._execution_process_restarted_listeners = [
            current
            for current in self._execution_process_restarted_listeners
            if current != listener
        ]

    def add_execution_connection_lost_listener(
        self, listener: Callable[[tuple[str, ...]], None]
    ) -> None:
        self._execution_connection_lost_listeners.append(listener)

    def notify_execution_connection_lost(self, job_uuids: tuple[str, ...]) -> None:
        """连接断开只报告断线；进程身份变化才报告重启。"""
        for listener in tuple(self._execution_connection_lost_listeners):
            listener(job_uuids)

    def notify_execution_process_restarted(
        self,
        job_uuids: list[str] | tuple[str, ...],
    ) -> None:
        """把动作进程重启事实同步送入工站调度生命周期。

        参数：``job_uuids`` 是本地账本刚标为不确定的在途作业。返回无；空集合
        仍传播进程重启事实，使工作流权威能够终结账本尚未记录的等待任务。异常：
        监听器异常原样传播，禁止静默遗失整条 DAG 的失败事实。普通网络断线不会
        调用本方法。
        """

        affected = tuple(dict.fromkeys(str(job_uuid) for job_uuid in job_uuids))
        for listener in tuple(self._execution_process_restarted_listeners):
            listener(affected)
        if self._execution_process_restarted_listeners:
            self.store.complete_execution_restart()

    def commit_feedback(
        self,
        job_uuid: str,
        *,
        command_uuid: str,
        job_token: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """先提交 Edge 本地事实，再幂等通知工作流投影。"""

        samples = payload.get("samples")
        if samples is None:
            samples = [payload]
        if not isinstance(samples, list) or not samples or any(
            not isinstance(sample, dict) for sample in samples
        ):
            raise ValueError("feedback samples are invalid")
        through = 0
        created = 0
        for sample in samples:
            result = self.store.commit_feedback(
                job_uuid,
                command_uuid=command_uuid,
                job_token=job_token,
                payload=sample,
            )
            through = max(through, int(result["through_sequence"]))
            if not bool(result.get("projected")):
                listeners = tuple(self._feedback_listeners)
                for listener in listeners:
                    listener(job_uuid, dict(sample))
                if listeners:
                    self.store.mark_feedback_projected(
                        job_uuid,
                        int(sample["sequence"]),
                    )
            created += int(bool(result.get("created")))
        return {"through_sequence": through, "created": created}

    def busy_device_action_keys(self) -> set[str]:
        return self.store.busy_device_action_keys()

    def fail_restarted_jobs(self, job_uuids: Sequence[str]) -> list[str]:
        """工作流已失败后，把 Edge 账本中的重启占用作业收成 failed。"""

        return self.store.fail_restarted_jobs(job_uuids)

    def commit_outcome(
        self,
        job_uuid: str,
        *,
        command_uuid: str,
        job_token: str,
        idempotency_key: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """提交 Edge 结果并在有持久消费者时确认投影。

        参数：作业、命令、令牌和 ``idempotency_key`` 共同标识同一次 HTTP
        结果提交；``payload`` 是 Backend-shaped outcome。返回：结果身份、状态及
        首次创建标记。异常：授权、幂等或投影失败原样传播，未成功投影的结果会
        保留并在重启后进行投递重放（DeliveryReplay）。
        """

        outcome, is_new = self.store.save_outcome(
            job_uuid,
            command_uuid=command_uuid,
            job_token=job_token,
            idempotency_key=idempotency_key,
            payload=payload,
        )
        if not self.store.is_outcome_projected(job_uuid):
            exact_targets = tuple(self._outcome_listeners)
            legacy_targets = (
                tuple(self._listeners)
                if not exact_targets and not outcome["unknown_command_ids"]
                else ()
            )
            if exact_targets:
                committed = _committed_outcome(outcome)
                for listener in exact_targets:
                    listener(job_uuid, committed)
            elif legacy_targets:
                success, result, suc_type = _legacy_outcome(outcome)
                for listener in legacy_targets:
                    listener(job_uuid, success, result, suc_type)
            if exact_targets or legacy_targets:
                self.store.mark_outcome_projected(job_uuid)
        status = (
            "unknown"
            if outcome["unknown_command_ids"]
            else (
                "completed"
                if self.store.is_outcome_projected(job_uuid)
                else "outcome_pending"
            )
        )
        return {
            "result": dict(outcome["_result"]),
            "status": status,
            "created": is_new,
        }

    def resolve_unknown_committed(self, job_uuid: str) -> None:
        """以 Edge 明确取消证据结算此前结果不明的作业。

        参数：``job_uuid`` 是原作业身份。返回无。异常：投影监听器故障原样传播，
        且不会提前确认结果；没有监听器时保留待投影事实。
        """

        if not self.store.resolve_unknown_committed(job_uuid):
            return
        if not self.store.is_outcome_projected(job_uuid):
            exact_targets = tuple(self._outcome_listeners)
            legacy_targets = tuple(self._listeners) if not exact_targets else ()
            if exact_targets:
                committed = CommittedJobOutcome(
                    outcome="canceled",
                    return_info={},
                    error_info=[],
                    unknown_command_ids=[],
                    inventory_consumptions=[],
                    material_aliquot_receipts=[],
                )
                for listener in exact_targets:
                    listener(job_uuid, committed)
            else:
                for listener in legacy_targets:
                    listener(job_uuid, False, None, "canceled")
            if exact_targets or legacy_targets:
                self.store.mark_outcome_projected(job_uuid)

    def replay_pending_projections(
        self,
        *,
        feedback_listener: Callable[[str, dict[str, Any]], None] | None = None,
        outcome_listener: Callable[[str, CommittedJobOutcome], None] | None = None,
        finished_listener: Callable[[str, bool, Any, str], None] | None = None,
    ) -> dict[str, int]:
        """重放 Edge 已提交但工作流库尚未确认的证据。

        反馈与结果只在目标投影回调成功后标记完成；任一回调
        失败都保留未确认事实，下次启动继续交付。可选的显式回调
        供启动恢复使用，避免绕经已丢失内存 DAG 的旧调度器路由。
        """

        self.retry_device_property_projection()
        feedback_targets = (
            (feedback_listener,)
            if feedback_listener is not None
            else tuple(self._feedback_listeners)
        )
        finished_targets = (
            (finished_listener,)
            if finished_listener is not None
            else tuple(self._listeners)
        )
        outcome_targets = (
            (outcome_listener,)
            if outcome_listener is not None
            else tuple(self._outcome_listeners)
        )
        if outcome_targets:
            finished_targets = ()
        projected_feedback = 0
        for pending in self.store.pending_feedback_projections():
            if not feedback_targets:
                break
            for listener in feedback_targets:
                listener(pending["job_uuid"], dict(pending["payload"]))
            self.store.mark_feedback_projected(
                pending["job_uuid"],
                int(pending["sequence"]),
            )
            projected_feedback += 1

        projected_outcomes = 0
        for pending in self.store.pending_outcome_projections():
            if not outcome_targets and not finished_targets:
                break
            outcome = pending["outcome"]
            if outcome_targets:
                committed = _committed_outcome(outcome)
                for listener in outcome_targets:
                    listener(pending["job_uuid"], committed)
            else:
                succeeded, result, suc_type = _legacy_outcome(outcome)
                for listener in finished_targets:
                    listener(pending["job_uuid"], succeeded, result, suc_type)
            self.store.mark_outcome_projected(pending["job_uuid"])
            projected_outcomes += 1
        return {
            "feedback": projected_feedback,
            "outcomes": projected_outcomes,
        }


def create_local_edge_control_router(
    authority: LocalEdgeControlAuthority,
) -> APIRouter:
    """创建与 Backend 同形的本地 Edge HTTP/WebSocket 适配器。

    参数：``authority`` 提供唯一命令、反馈和不可变结果权威。返回可挂载的
    ``APIRouter``。异常：请求身份、载荷或幂等冲突分别映射为稳定 HTTP 状态；
    构造本身不启动线程也不修改持久事实。
    """

    router = APIRouter(prefix="/api/v1/edge", tags=["local-edge-control"])
    active_session_lock = asyncio.Lock()
    active_generation = 0
    active_session_uuid = ""
    active_websocket: WebSocket | None = None
    active_handler_task: asyncio.Task[Any] | None = None

    def authorize(value: str | None) -> None:
        expected = f"Bearer {authority.api_key}"
        if value is None or not hmac.compare_digest(value, expected):
            raise HTTPException(
                status_code=401,
                detail=(
                    "Edge 请求未通过身份校验：Authorization 头缺失，或 Bearer 令牌与"
                    "当前工作区不一致；请由 Workspace Host 启动 Edge Runtime，"
                    "或使用当前工作区令牌"
                ),
            )

    @router.get("/readiness")
    def readiness() -> JSONResponse:
        """实时读取动作进程注册事实并返回 Kubernetes 就绪合同。

        参数：无。返回：仅含状态、Edge/实例身份、连接状态和设备数量的非敏感
        摘要；注册完整且当前已连接时为 HTTP 200，否则为 HTTP 503。异常：持久
        存储读取错误原样传播；响应不包含令牌、设备清单或动作参数。
        """

        ready, summary = project_local_edge_readiness(
            authority.store.latest_registration()
        )
        return JSONResponse(status_code=200 if ready else 503, content=summary)

    @router.post("/sessions")
    def register_session(
        payload: dict[str, Any],
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        authorize(authorization)
        try:
            result = authority.store.register_session(payload)
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return _envelope(result)

    @router.put("/sessions/{session_uuid}/devices/{local_device_id}/status")
    def update_device_status(
        session_uuid: str,
        local_device_id: str,
        payload: _DeviceStatusUpdate,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        """提交当前会话中一个设备的在线、健康和属性事实。"""

        authorize(authorization)
        try:
            result = authority.store.update_device_status(
                session_uuid,
                local_device_id,
                payload.model_dump(),
            )
            authority.retry_device_property_projection()
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return _envelope(result)

    @router.post("/error-decisions")
    def report_error_decision_required(
        payload: dict[str, Any],
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        """接收 Edge 动作异常，并建立等待人工处理的工作流干预。"""

        authorize(authorization)
        if not authority.publish_job_error_decision_required(payload):
            raise HTTPException(status_code=409, detail="错误决策报告未被工作流权威接受")
        return _envelope({"accepted": True})

    @router.get("/jobs/{job_uuid}")
    def fetch_job(
        job_uuid: str,
        task_uuid: str,
        node_uuid: str,
        x_command_uuid: str = Header(alias="X-Command-UUID"),
        x_job_token: str = Header(alias="X-Job-Token"),
    ) -> dict[str, Any]:
        try:
            result = authority.store.fetch_job(
                job_uuid,
                command_uuid=x_command_uuid,
                job_token=x_job_token,
            )
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Job not found") from error
        except (PermissionError, ValueError) as error:
            raise HTTPException(status_code=401, detail=str(error)) from error
        if result["task_uuid"] != task_uuid or result["node_uuid"] != node_uuid:
            raise HTTPException(status_code=409, detail="Job identity changed")
        result["station_controlled"] = authority.execution_control is not None
        if authority.execution_control is not None:
            result["execution_start_state"] = authority.execution_control.execution_start_state(job_uuid)
            result["recovery_policy"] = (
                "reconcile"
                if getattr(authority.execution_control, "time_scheduling_enabled", False)
                else "legacy_abort"
            )
        return _envelope(result)

    @router.post("/jobs/{job_uuid}/begin", summary="确认设备动作开始执行")
    def begin_execution(job_uuid: str, x_command_uuid: str = Header(alias="X-Command-UUID"),
                        x_job_token: str = Header(alias="X-Job-Token")) -> dict[str, Any]:
        try:
            authority.store.fetch_job(job_uuid, command_uuid=x_command_uuid, job_token=x_job_token)
            if authority.execution_control is None:
                raise HTTPException(503, "工站执行控制尚未恢复")
            result = authority.execution_control.begin_execution(job_uuid, x_command_uuid)
            if result == "START":
                authority.store.mark_job_started(job_uuid)
                if authority._execution_started is not None:
                    authority._execution_started(job_uuid)
        except KeyError as error:
            raise HTTPException(404, "Job not found") from error
        except (PermissionError, ValueError) as error:
            raise HTTPException(409, str(error)) from error
        return _envelope({"start": result})

    @router.post("/jobs/{job_uuid}/feedback")
    def commit_feedback(
        job_uuid: str,
        payload: dict[str, Any],
        x_command_uuid: str = Header(alias="X-Command-UUID"),
        x_job_token: str = Header(alias="X-Job-Token"),
    ) -> dict[str, Any]:
        try:
            result = authority.commit_feedback(
                job_uuid,
                command_uuid=x_command_uuid,
                job_token=x_job_token,
                payload=payload,
            )
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Job not found") from error
        except (PermissionError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return _envelope(result)

    @router.put("/jobs/{job_uuid}/outcome")
    def commit_outcome(
        job_uuid: str,
        payload: dict[str, Any],
        idempotency_key: str = Header(alias="Idempotency-Key"),
        x_command_uuid: str = Header(alias="X-Command-UUID"),
        x_job_token: str = Header(alias="X-Job-Token"),
    ) -> JSONResponse:
        """提交或重放完整作业结果。

        参数：路径 Job、结果载荷及三个请求头共同约束身份与幂等。返回首次 201、
        同结果重放 200，并携带完整 ``WorkflowNodeJobResult``。异常：不存在为
        404、身份无效为 401、首次结果冲突为 409、其他非法载荷为 400。
        """

        try:
            result = authority.commit_outcome(
                job_uuid,
                command_uuid=x_command_uuid,
                job_token=x_job_token,
                idempotency_key=idempotency_key,
                payload=payload,
            )
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Job not found") from error
        except PermissionError as error:
            raise HTTPException(status_code=401, detail=str(error)) from error
        except LocalEdgeOutcomeConflict as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except (RuntimeError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        created = bool(result.pop("created", False))
        return JSONResponse(
            status_code=201 if created else 200,
            content=_envelope(dict(result["result"])),
        )

    @router.post("/jobs/{job_uuid}/resolve-unknown")
    def resolve_unknown(
        job_uuid: str,
        request: Request,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        authorize(request.headers.get("Authorization"))
        try:
            result = authority.store.create_unknown_resolution(
                job_uuid, reason=str(payload.get("reason") or "")
            )
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Job not found") from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return _envelope(result)

    @router.websocket("/ws")
    async def edge_websocket(websocket: WebSocket) -> None:
        """维护动作执行进程会话并投递持久命令通知。

        参数：``websocket`` 是同工站动作进程连接。返回无。异常：鉴权、握手或
        会话代际冲突通过关闭连接失败关闭；普通断线仅在没有新会话接管时固化
        不确定事实，载荷与持久存储错误原样传播；只有 hello 中进程身份变化才
        通知工站调度器失败相关 DAG。
        """

        nonlocal active_generation, active_handler_task
        nonlocal active_session_uuid, active_websocket

        authorization = websocket.headers.get("Authorization")
        if authorization is None or not hmac.compare_digest(
            authorization, f"Bearer {authority.api_key}"
        ):
            await websocket.close(
                code=4401,
                reason="Edge 请求未通过身份校验：请使用当前工作区的 Bearer 令牌",
            )
            return
        await websocket.accept()
        session_uuid = ""
        session_generation = 0
        activated = False
        try:
            hello = json.loads(await asyncio.wait_for(websocket.receive_text(), 10))
            if hello.get("type") != "hello" or not isinstance(
                hello.get("payload"), dict
            ):
                await websocket.close(code=4400)
                return
            handler_task = asyncio.current_task()
            if handler_task is None:
                raise RuntimeError("Edge WebSocket handler task is unavailable")
            async with active_session_lock:
                previous_generation = active_generation
                previous_session_uuid = active_session_uuid
                previous_websocket = active_websocket
                previous_handler_task = active_handler_task
                try:
                    with authority.store.activate_session_and_reconcile(
                        hello["payload"]
                    ) as (
                        activated_session_uuid,
                        process_restarted,
                        affected_job_uuids,
                    ):
                        session_uuid = activated_session_uuid
                        replaced_websocket = active_websocket
                        replaced_handler_task = active_handler_task
                        active_generation += 1
                        session_generation = active_generation
                        active_session_uuid = session_uuid
                        active_websocket = websocket
                        active_handler_task = handler_task
                    activated = True
                except BaseException:
                    active_generation = previous_generation
                    active_session_uuid = previous_session_uuid
                    active_websocket = previous_websocket
                    active_handler_task = previous_handler_task
                    session_generation = 0
                    raise
            if (
                replaced_handler_task is not None
                and replaced_handler_task is not handler_task
            ):
                replaced_handler_task.cancel()
            if replaced_websocket is not None and replaced_websocket is not websocket:
                try:
                    await asyncio.wait_for(
                        replaced_websocket.close(code=4409),
                        timeout=_REPLACED_SOCKET_CLOSE_TIMEOUT_SECONDS,
                    )
                except (RuntimeError, WebSocketDisconnect, TimeoutError):
                    pass
            if process_restarted:
                authority.notify_execution_process_restarted(affected_job_uuids)
            while True:
                async with active_session_lock:
                    current_generation = (
                        active_generation == session_generation
                        and active_session_uuid == session_uuid
                    )
                if not current_generation:
                    break
                stale_generation = False
                for command in authority.store.pending_commands():
                    if command["type"] == "job.start" and authority.execution_control is not None:
                        if not authority.execution_control.allows_delivery(str(command["payload"].get("job_uuid") or "")):
                            continue
                    async with active_session_lock:
                        current_generation = (
                            active_generation == session_generation
                            and active_session_uuid == session_uuid
                        )
                    if not current_generation:
                        stale_generation = True
                        break
                    await websocket.send_text(json.dumps(command, ensure_ascii=False))
                    authority.store.mark_command_sent(str(command["message_uuid"]))
                if stale_generation:
                    break
                try:
                    encoded = await asyncio.wait_for(
                        websocket.receive_text(), timeout=0.1
                    )
                except TimeoutError:
                    continue
                event = json.loads(encoded)
                async with active_session_lock:
                    current_generation = (
                        active_generation == session_generation
                        and active_session_uuid == session_uuid
                    )
                if not current_generation:
                    break
                await _handle_edge_event(authority, websocket, event)
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        finally:
            if activated:
                owns_active_generation = False
                async with active_session_lock:
                    if (
                        active_generation == session_generation
                        and active_session_uuid == session_uuid
                    ):
                        active_session_uuid = ""
                        active_websocket = None
                        active_handler_task = None
                        owns_active_generation = True
                if owns_active_generation:
                    try:
                        affected = authority.store.disconnect_session(session_uuid)
                        if affected and authority.execution_control is not None:
                            authority.notify_execution_connection_lost(affected)
                    except ValueError:
                        pass

    return router


async def _handle_edge_event(
    authority: LocalEdgeControlAuthority,
    websocket: WebSocket,
    event: dict[str, Any],
) -> None:
    event_uuid = str(uuid.UUID(_required_text(event, "message_uuid")))
    event_type = _required_text(event, "type")
    payload = event.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("event payload must be an object")
    if event_type == "command.ack":
        authority.store.acknowledge_command(_required_text(payload, "command_uuid"))
    elif event_type == "job.started":
        job_uuid = _required_text(payload, "job_uuid")
        # 新协议只有持久 begin 才能宣布开始；排队或 ROS 接受不是运行事实。
        active = authority.store.job(job_uuid)["status"] not in {
            "failed",
            "canceled",
            "succeeded",
        }
        if active and (
            authority.execution_control is None
            or authority.execution_control.execution_start_state(job_uuid)
            == "START_COMMITTED"
        ):
            authority.store.mark_job_started(job_uuid)
            if authority._execution_started is not None:
                authority._execution_started(job_uuid)
    elif event_type == "job.unknown_resolution_committed":
        authority.resolve_unknown_committed(_required_text(payload, "job_uuid"))
    elif event_type not in {
        "job.feedback_committed",
        "job.outcome_committed",
    }:
        raise ValueError(f"unsupported Edge event {event_type!r}")
    await websocket.send_text(
        json.dumps(
            {
                "protocol_version": _PROTOCOL_VERSION,
                "message_uuid": str(uuid.uuid4()),
                "sequence": 0,
                "type": "event.ack",
                "sent_at": _utc_now(),
                "payload": {"event_uuid": event_uuid},
            },
            ensure_ascii=False,
        )
    )


def _required_text(payload: dict[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    return value.strip()


def _normalize_edge_hello(
    payload: dict[str, Any],
) -> tuple[str, str, str, int, list[dict[str, Any]]]:
    """在任何持久写入前规范并完整校验 Edge hello。

    参数：``payload`` 必须包含注册 Session/Edge UUID、进程 UUID、非负 ACK 游标
    及对象数组形式的运行 Job。返回三个规范 UUID、游标与运行 Job 脱离列表。
    异常：字段缺失、UUID/游标或数组形状非法时抛 ``ValueError``，且不访问数据库。
    """

    session_uuid = str(uuid.UUID(_required_text(payload, "session_uuid")))
    edge_uuid = str(uuid.UUID(_required_text(payload, "edge_uuid")))
    process_uuid = str(uuid.UUID(_required_text(payload, "process_uuid")))
    last_ack = payload.get("last_ack_command_sequence", 0)
    if isinstance(last_ack, bool) or not isinstance(last_ack, int) or last_ack < 0:
        raise ValueError("last_ack_command_sequence is invalid")
    individual_ack = payload.get("individual_command_ack", False)
    if not isinstance(individual_ack, bool):
        raise ValueError("individual_command_ack is invalid")
    # 工站暂停允许手动命令越过普通命令，最大序号不再证明前缀均已收到。
    # 新执行端逐条可靠重放 command.ack，兼容旧执行端的连续投递游标。
    if individual_ack:
        last_ack = 0
    raw_running_jobs = payload.get("running_jobs")
    running_jobs = [] if raw_running_jobs is None else raw_running_jobs
    if not isinstance(running_jobs, list) or any(
        not isinstance(job, dict) for job in running_jobs
    ):
        raise ValueError("running_jobs must be a list of objects")
    return session_uuid, edge_uuid, process_uuid, last_ack, [
        dict(job) for job in running_jobs
    ]


def _normalize_unknown_command_ids(job_uuid: str, values: Any) -> list[str]:
    """规范结果不明命令身份并验证至少一个身份属于当前 Job。

    参数：``job_uuid`` 是当前工作流节点作业；``values`` 是 Edge 上报的命令身份
    数组。返回：去重排序后的规范命令身份。异常：数组形状、命令格式、长度或归属
    不合法时抛 ``ValueError``；空数组表示结果明确，不产生物理不确定事实。
    """

    if not isinstance(values, list):
        raise ValueError("unknown_command_ids must be an array")
    normalized_job = str(uuid.UUID(job_uuid))
    normalized: set[str] = set()
    belongs_to_job = False
    for value in values:
        if not isinstance(value, str):
            raise ValueError("unknown_command_ids must contain strings")
        command_id = value.strip()
        if not command_id.startswith("workflow-node-job:") or len(command_id) > 512:
            raise ValueError("unknown_command_ids contains an invalid command identity")
        identity = command_id.removeprefix("workflow-node-job:")
        job_identity, separator, child_identity = identity.partition(":")
        try:
            command_job_uuid = str(uuid.UUID(job_identity))
        except ValueError as error:
            raise ValueError(
                "unknown_command_ids contains an invalid command UUID"
            ) from error
        if job_identity != command_job_uuid:
            raise ValueError("unknown_command_ids command UUID is not canonical")
        if separator and (
            not child_identity
            or len(child_identity) > 128
            or any(
                not (
                    character.isascii()
                    and (character.isalnum() or character in "-_.")
                )
                for character in child_identity
            )
        ):
            raise ValueError("unknown_command_ids contains an invalid child identity")
        belongs_to_job = belongs_to_job or command_job_uuid == normalized_job
        normalized.add(command_id)
    ordered = sorted(normalized)
    if ordered and not belongs_to_job:
        raise ValueError("unknown_command_ids does not contain this Job command")
    if len("unresolved_unknown_command:" + ",".join(ordered)) > 1024:
        raise ValueError("unknown_command_ids is too long")
    return ordered


def _job_result_projection(
    *,
    job_uuid: str,
    stored_outcome: dict[str, Any],
) -> dict[str, Any]:
    """把本地不可变结果投影为 Backend ``WorkflowNodeJobResult`` 响应。

    参数：``job_uuid`` 是父作业身份；``stored_outcome`` 是首次提交后持久化的结果
    与内部身份。返回：稳定的公共结果 DTO。异常：持久字段损坏时抛 ``RuntimeError``，
    禁止以新身份覆盖首次结果；重复 HTTP 提交返回完全相同的结果身份和时间。
    """

    try:
        result_uuid = str(uuid.UUID(str(stored_outcome["_result_uuid"])))
        command_uuid = str(uuid.UUID(str(stored_outcome["_edge_command_uuid"])))
        committed_at = str(stored_outcome["_committed_at"])
        create_time = str(stored_outcome["_create_time"])
        update_time = str(stored_outcome["_update_time"])
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError("stored job outcome identity is corrupted") from error
    return {
        "uuid": result_uuid,
        "create_time": create_time,
        "update_time": update_time,
        "meta_data": {
            "inventory_consumptions": [
                dict(item) for item in stored_outcome["inventory_consumptions"]
            ],
            "material_aliquot_receipts": [
                dict(item)
                for item in stored_outcome.get("material_aliquot_receipts", [])
            ],
            "unknown_command_ids": list(stored_outcome["unknown_command_ids"]),
        },
        "workflow_node_job_uuid": str(uuid.UUID(job_uuid)),
        "edge_command_uuid": command_uuid,
        "idempotency_key": str(stored_outcome["_idempotency_key"]),
        "outcome": str(stored_outcome["outcome"]),
        "return_info": dict(stored_outcome["return_info"]),
        "error_info": list(stored_outcome["error_info"]),
        "committed_at": committed_at,
    }


def _token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _current_trace_context() -> dict[str, str]:
    """捕获当前调度 span 的持久 W3C carrier；追踪关闭时为空。"""

    carrier: dict[str, Any] = {}
    inject_trace_context(carrier)
    return normalize_trace_context(carrier)


def _job_projection(row: sqlite3.Row) -> dict[str, Any]:
    """把本地双进程作业行投影为不含访问令牌的稳定摘要。"""

    return {
        "job_uuid": str(row["job_uuid"]),
        "task_uuid": str(row["task_uuid"]),
        "node_uuid": str(row["node_uuid"]),
        "command_uuid": str(row["command_uuid"]),
        "claim_uuid": row["claim_uuid"],
            "execution_source": row["execution_source"],
        "attempt": int(row["attempt"]),
        "fences": json.loads(str(row["fences_json"])),
        "local_device_id": str(row["local_device_id"]),
        "action_name": str(row["action_name"]),
        "device_action_key": str(row["device_action_key"]),
        "status": str(row["status"]),
    }


def _validate_fences(value: Any) -> list[dict[str, Any]]:
    """规范并校验一个 Claim 携带的资源 Fence 列表。

    参数：``value`` 必须是锁键唯一的对象列表。返回按锁键排序的副本。异常：
    锁键为空、令牌非正整数或重复键内容冲突时抛 ``ValueError``。
    """

    if not isinstance(value, list):
        raise ValueError("fences must be a list")
    normalized: dict[str, int] = {}
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("fence must be an object")
        lock_key = str(item.get("lock_key") or "").strip()
        token = item.get("fencing_token")
        if (
            not lock_key
            or isinstance(token, bool)
            or not isinstance(token, int)
            or token < 1
        ):
            raise ValueError("fence lock_key or fencing_token is invalid")
        if lock_key in normalized and normalized[lock_key] != token:
            raise ValueError("duplicate fence lock_key changed token")
        normalized[lock_key] = token
    return [
        {"lock_key": lock_key, "fencing_token": normalized[lock_key]}
        for lock_key in sorted(normalized)
    ]


def _validate_job_attempt_identity(
    row: sqlite3.Row,
    job_uuid: str,
    command_uuid: str,
    payload: dict[str, Any],
) -> None:
    """证明 HTTP 事实与本地持久化的 Job/Claim/Fence 尝试完全一致。"""

    expected = {
        "job_uuid": str(uuid.UUID(job_uuid)),
        "task_uuid": str(row["task_uuid"]),
        "node_uuid": str(row["node_uuid"]),
        "command_uuid": str(uuid.UUID(command_uuid)),
        "claim_uuid": row["claim_uuid"],
            "execution_source": row["execution_source"],
    }
    for field, value in expected.items():
        if field == "execution_source":
            if payload.get(field, "workflow") != value:
                raise ValueError("execution_source does not match persisted job")
            continue
        if field == "claim_uuid" and row["execution_source"] == "error_manual":
            if payload.get(field) is not None:
                raise ValueError("manual execution must not carry a claim")
            continue
        try:
            actual = str(uuid.UUID(_required_text(payload, field)))
        except (TypeError, ValueError) as error:
            raise ValueError(f"{field} is invalid") from error
        if actual != value:
            raise ValueError(f"{field} does not match the persisted job attempt")
    attempt = payload.get("attempt")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise ValueError("attempt must be a positive integer")
    if attempt != int(row["attempt"]):
        raise ValueError("attempt does not match the persisted job attempt")
    if _validate_fences(payload.get("fences")) != json.loads(
        str(row["fences_json"])
    ):
        raise ValueError("fences do not match the persisted job attempt")


def _envelope(data: dict[str, Any]) -> dict[str, Any]:
    return {"code": 0, "data": data}


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + ".000000Z"


__all__ = [
    "LocalEdgeAuthorityStore",
    "LocalEdgeControlAuthority",
    "create_local_edge_control_router",
]
