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
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Iterator, List, Optional

from unilabos.persistence import open_database
from unilabos.persistence.database import prepare_database_path


@dataclass(frozen=True)
class StoredEvent:
    event_uuid: str
    event_type: str
    payload: Dict[str, Any]
    created_at: str
    traceparent: str = ""
    tracestate: str = ""
    trace_id: str = ""


@dataclass(frozen=True)
class StoredJob:
    job_uuid: str
    task_uuid: str
    node_uuid: str
    command_uuid: str
    job_access_token: str
    status: str
    feedback_sequence: int
    claim_uuid: str = ""
    execution_source: str = "workflow"
    attempt: int = 1
    fences: tuple[tuple[str, int], ...] = ()
    traceparent: str = ""
    tracestate: str = ""
    trace_id: str = ""


@dataclass(frozen=True)
class StoredOutcome:
    job_uuid: str
    outcome: str
    return_info: Dict[str, Any]
    error_info: List[Dict[str, Any]]
    unknown_command_ids: List[str]


class EdgeControlStore:
    """线程安全的 SQLite Command/Outbox 存储。"""

    SCHEMA_VERSION = 5

    def __init__(self, path: str) -> None:
        """打开并迁移边缘控制投递存储（Edge Control Delivery Store）。

        ``path`` 是当前 Edge 实例的 SQLite 文件路径；构造函数返回初始化后的
        ``EdgeControlStore``。数据库版本高于当前实现时抛出 ``RuntimeError``，
        迁移或初始化
        失败时原样抛出 SQLite 异常，禁止以未知 Schema 继续写入。
        """

        self.path = prepare_database_path(path, create_parent=True)
        self._lock = threading.RLock()
        self.db = open_database(self.path, "edge", private=True)
        self._connection = self.db
        self._connection.row_factory = sqlite3.Row
        self.db.initialize(self._initialize_sqlite)
        with self._lock:
            # Pong only answers a ping from the current WebSocket session. Older
            # versions persisted it as a durable business event, which allowed a
            # stale pong to be replayed into a new session and closed by the
            # scheduler as a policy violation.
            self._connection.execute(
                "DELETE FROM edge_event_outbox WHERE type = 'pong'"
            )
            self._connection.execute(
                """
                INSERT OR IGNORE INTO edge_control_meta(key, value)
                SELECT 'last_ack_command_sequence',
                       CAST(COALESCE(MAX(sequence), 0) AS TEXT)
                FROM edge_command WHERE status = 'completed'
                """
            )
            acknowledged_events = self._connection.execute(
                """
                SELECT event_uuid, type, payload_json
                FROM edge_event_outbox WHERE acked_at IS NOT NULL
                """
            ).fetchall()
            for acknowledged_event in acknowledged_events:
                self._retire_event_locked(acknowledged_event)
            self._connection.commit()

    def _initialize_sqlite(self) -> None:
        with self._lock:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=FULL")
            schema_version = int(
                self._connection.execute("PRAGMA user_version").fetchone()[0]
            )
            if schema_version > self.SCHEMA_VERSION:
                self._connection.close()
                raise RuntimeError(
                    f"edge_control schema {schema_version} is newer than supported "
                    f"version {self.SCHEMA_VERSION}"
                )
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
                    trace_id TEXT NOT NULL DEFAULT '',
                    traceparent TEXT NOT NULL DEFAULT '',
                    tracestate TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    received_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_edge_command_sequence
                    ON edge_command(sequence);
                CREATE INDEX IF NOT EXISTS idx_edge_command_status_sequence
                    ON edge_command(status, sequence);
                CREATE TABLE IF NOT EXISTS edge_event_outbox (
                    event_uuid TEXT PRIMARY KEY,
                    type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    trace_id TEXT NOT NULL DEFAULT '',
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
                    claim_uuid TEXT NOT NULL,
                    attempt INTEGER NOT NULL CHECK (attempt > 0),
                    fences_json TEXT NOT NULL,
                    job_access_token TEXT NOT NULL,
                    status TEXT NOT NULL,
                    feedback_sequence INTEGER NOT NULL DEFAULT 0,
                    trace_id TEXT NOT NULL DEFAULT '',
                    traceparent TEXT NOT NULL DEFAULT '',
                    tracestate TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL,
                    outcome TEXT,
                    return_info_json TEXT,
                    error_info_json TEXT,
                    unknown_command_ids_json TEXT,
                    outcome_updated_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_edge_job_status_updated
                    ON edge_job_runtime(status, updated_at);
                CREATE TABLE IF NOT EXISTS edge_resource_fence (
                    lock_key TEXT PRIMARY KEY,
                    last_fencing_token INTEGER NOT NULL,
                    claim_uuid TEXT NOT NULL,
                    job_uuid TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                """
            )
            if schema_version == 0:
                # 版本 0 是引入迁移所有者前的状态库。只在这条显式迁移中补列，
                # 后续版本不再通过启动期猜测 Schema。
                self._migrate_legacy_column(
                    "edge_event_outbox", "traceparent", "TEXT NOT NULL DEFAULT ''"
                )
                self._migrate_legacy_column(
                    "edge_event_outbox", "tracestate", "TEXT NOT NULL DEFAULT ''"
                )
                self._migrate_legacy_column(
                    "edge_job_runtime", "traceparent", "TEXT NOT NULL DEFAULT ''"
                )
                self._migrate_legacy_column(
                    "edge_job_runtime", "tracestate", "TEXT NOT NULL DEFAULT ''"
                )
                pending_exists = self._connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='edge_job_outcome_pending'"
                ).fetchone()
                if pending_exists is not None:
                    self._migrate_legacy_column(
                        "edge_job_outcome_pending",
                        "unknown_command_ids_json",
                        "TEXT NOT NULL DEFAULT '[]'",
                    )
            if schema_version < 2:
                self._migrate_legacy_column(
                    "edge_job_runtime", "claim_uuid", "TEXT NOT NULL DEFAULT ''"
                )
                self._migrate_legacy_column(
                    "edge_job_runtime", "attempt", "INTEGER NOT NULL DEFAULT 1"
                )
                self._migrate_legacy_column(
                    "edge_job_runtime", "fences_json", "TEXT NOT NULL DEFAULT '[]'"
                )
            if schema_version < 3:
                # 本地 trace_id 在 OTel 关闭时是跨进程关联同一操作的唯一身份，
                # 必须随既有命令、事件和作业镜像持久化，重启后才能继续重放。
                for table in ("edge_command", "edge_event_outbox", "edge_job_runtime"):
                    self._migrate_legacy_column(
                        table, "trace_id", "TEXT NOT NULL DEFAULT ''"
                    )
            self._migrate_legacy_column("edge_job_runtime", "execution_source", "TEXT NOT NULL DEFAULT 'workflow'")
            if schema_version < 4:
                self._migrate_legacy_column(
                    "edge_resource_fence", "task_uuid", "TEXT NOT NULL DEFAULT ''"
                )
                # 已退役作业无法证明 Task 归属时保留空值，旧版本相等请求仍拒绝。
                self._connection.execute(
                    "UPDATE edge_resource_fence SET task_uuid=COALESCE("
                    "(SELECT task_uuid FROM edge_job_runtime j "
                    "WHERE j.job_uuid=edge_resource_fence.job_uuid), '')"
                )
            self._fold_pending_outcome()
            # 结果列是折叠之后才补上的。索引不能写进上面的建表脚本，
            # 否则旧运行表还没有 outcome_updated_at，启动会直接失败。
            self._connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_edge_outcome_updated
                    ON edge_job_runtime(outcome_updated_at)
                    WHERE outcome IS NOT NULL
                """
            )
            # Pong only answers a ping from the current WebSocket session. Older
            # versions persisted it as a durable business event, which allowed a
            # stale pong to be replayed into a new session and closed by the
            # scheduler as a policy violation.
            self._connection.execute(
                "DELETE FROM edge_event_outbox WHERE type = 'pong'"
            )
            self._connection.execute(
                """
                INSERT OR IGNORE INTO edge_control_meta(key, value)
                SELECT 'last_ack_command_sequence',
                       CAST(COALESCE(MAX(sequence), 0) AS TEXT)
                FROM edge_command WHERE status = 'completed'
                """
            )
            acknowledged_events = self._connection.execute(
                """
                SELECT event_uuid, type, payload_json
                FROM edge_event_outbox WHERE acked_at IS NOT NULL
                """
            ).fetchall()
            for acknowledged_event in acknowledged_events:
                self._retire_event_locked(acknowledged_event)
            self._connection.execute(f"PRAGMA user_version = {self.SCHEMA_VERSION}")
            self._connection.commit()

    def _fold_pending_outcome(self) -> None:
        """把待上报结果并进作业运行行。上报完成后清空列，不删运行行。"""

        for column, definition in (
            ("outcome", "TEXT"),
            ("return_info_json", "TEXT"),
            ("error_info_json", "TEXT"),
            ("unknown_command_ids_json", "TEXT"),
            ("outcome_updated_at", "REAL"),
        ):
            self._migrate_legacy_column("edge_job_runtime", column, definition)
        exists = self._connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='edge_job_outcome_pending'"
        ).fetchone()
        if exists is None:
            return
        self._connection.execute(
            """
            UPDATE edge_job_runtime SET
                outcome = pending.outcome,
                return_info_json = pending.return_info_json,
                error_info_json = pending.error_info_json,
                unknown_command_ids_json = pending.unknown_command_ids_json,
                outcome_updated_at = pending.updated_at
            FROM edge_job_outcome_pending AS pending
            WHERE pending.job_uuid = edge_job_runtime.job_uuid
            """
        )
        self._connection.execute("DROP TABLE edge_job_outcome_pending")

    def _migrate_legacy_column(
        self, table: str, column: str, definition: str
    ) -> None:
        """把迁移前数据库缺失的列补入显式版本 0→1 迁移。

        ``table``、``column`` 和 ``definition`` 只由本模块固定调用点提供；返回
        为空。SQLite 拒绝迁移时异常上抛，使边缘执行镜像关闭失败。
        """

        columns = {
            str(row["name"])
            for row in self._connection.execute(f"PRAGMA table_info({table})")
        }
        if column not in columns:
            self._connection.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
            )

    @contextmanager
    def _immediate_transaction(self) -> Iterator[None]:
        """提供异常必回滚的 SQLite 立即事务边界。

        参数为空；yield 后由调用方执行同一连接上的状态转换，正常返回时提交，
        任意异常时回滚并原样抛出。
        """

        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._connection.rollback()
            raise
        else:
            self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def reset_transient_state(self) -> None:
        """清空可恢复协议工作但保留 Edge 实例身份。

        参数和返回值为空。Managed Local 仅在 Edge 进程停止后通过显式
        ``local.reset-state`` 调用；普通重启不得调用，否则会破坏投递重放。
        """

        with self._lock:
            with self._immediate_transaction():
                self._clear_transient_state_locked()

    def adopt_authority_edge_uuid(self, edge_uuid: str) -> bool:
        """把恢复状态绑定到 Backend 分配的 Edge 权威身份。

        ``edge_uuid`` 是 Backend 当前认可的 Edge UUID；身份首次出现时只记录，
        与既有值不同时原子清空旧命令、ACK 游标、作业和发件箱，防止跨权威
        投递重放（DeliveryReplay）。返回是否因身份变化执行了清理；非法 UUID
        抛出 ``ValueError``，数据库失败时事务回滚并原样抛出。
        """

        normalized = str(uuid.UUID(edge_uuid))
        with self._lock:
            with self._immediate_transaction():
                row = self._connection.execute(
                    "SELECT value FROM edge_control_meta WHERE key = 'authority_edge_uuid'"
                ).fetchone()
                previous = str(row["value"]) if row is not None else ""
                changed = bool(previous and previous != normalized)
                if changed:
                    self._clear_transient_state_locked()
                self._connection.execute(
                    """
                    INSERT INTO edge_control_meta(key, value)
                    VALUES ('authority_edge_uuid', ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (normalized,),
                )
                return changed

    def _clear_transient_state_locked(self) -> None:
        """清空当前权威下可重建的协议状态并复位投递高水位。

        参数和返回值为空。调用方必须持有进程锁和写事务；Edge 实例身份等
        非瞬态 meta 保留，命令、作业、结果和事件发件箱全部清空。
        """

        self._connection.execute("DELETE FROM edge_job_runtime")
        self._connection.execute("DELETE FROM edge_event_outbox")
        self._connection.execute("DELETE FROM edge_command")
        self._connection.execute("DELETE FROM edge_resource_fence")
        self._connection.execute(
            """
            INSERT INTO edge_control_meta(key, value)
            VALUES ('last_ack_command_sequence', '0')
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """
        )

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
        with self._lock, self._immediate_transaction():
            existing = self._connection.execute(
                "SELECT sequence,type,payload_json FROM edge_command WHERE command_uuid=?",
                (command_uuid,),
            ).fetchone()
            if existing is not None:
                if (int(existing["sequence"]) != int(envelope.get("sequence") or 0)
                        or existing["type"] != str(envelope.get("type") or "")
                        or json.loads(existing["payload_json"]) != payload):
                    raise ValueError("重复命令身份的内容发生变化")
                return False
            cursor = self._connection.execute(
                """
                INSERT OR IGNORE INTO edge_command(
                    command_uuid, sequence, type, payload_json, trace_id,
                    traceparent, tracestate, status, received_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'received', ?)
                """,
                (
                    command_uuid,
                    int(envelope.get("sequence") or 0),
                    str(envelope.get("type") or ""),
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    str(envelope.get("trace_id") or ""),
                    str(envelope.get("traceparent") or ""),
                    str(envelope.get("tracestate") or ""),
                    time.time(),
                ),
            )
            return cursor.rowcount == 1

    def command_status(self, command_uuid: str) -> str:
        with self._lock:
            row = self._connection.execute(
                "SELECT status FROM edge_command WHERE command_uuid = ?",
                (command_uuid,),
            ).fetchone()
        return str(row["status"]) if row is not None else ""

    def mark_command_completed(self, command_uuid: str) -> None:
        """完成命令并原子推进已确认命令序号高水位。

        ``command_uuid`` 是 Backend 下行命令稳定身份；返回为空。命令不存在时
        保持幂等，高水位不变化。
        """

        with self._lock:
            with self._immediate_transaction():
                self._connection.execute(
                    "UPDATE edge_command SET status = 'completed' WHERE command_uuid = ?",
                    (command_uuid,),
                )
                self._advance_command_highwater_locked(command_uuid)

    def complete_unknown_resolution(
        self,
        command_uuid: str,
        resolution_payload: Dict[str, Any],
        trace_context: Optional[Dict[str, str]] = None,
        remaining_unknown_command_ids: Optional[List[str]] = None,
    ) -> None:
        """原子持久化 UNKNOWN 处置回执、通用 ACK 和命令完成状态。

        ``command_uuid`` 是对账恢复（Reconciliation）命令身份，
        ``resolution_payload`` 是设备已审计落账后的物理证据摘要，
        ``trace_context`` 是可选 W3C 追踪上下文；
        ``remaining_unknown_command_ids`` 是处置后驱动仍报告的结构化物理不确定
        身份，``None`` 表示旧调用方无法证明集合。返回为空。任意序列化或
        SQLite 异常都会回滚，禁止只完成部分回执或凭展示文案推断结算。
        """

        trace_context = trace_context or {}
        created_at = _utc_now()
        # 最终物理结算必须绑定到确切的处置事件，不能让同作业
        # 早先一条处置的 ACK 误删尚未被 Backend 确认的最后一条。
        resolution_event_uuid = str(uuid.uuid4())
        events = (
            (
                resolution_event_uuid,
                "job.unknown_resolution_committed",
                resolution_payload,
            ),
            (str(uuid.uuid4()), "command.ack", {"command_uuid": command_uuid}),
        )
        with self._lock:
            with self._immediate_transaction():
                for event_uuid, event_type, payload in events:
                    self._connection.execute(
                        """
                        INSERT INTO edge_event_outbox(
                            event_uuid, type, payload_json, created_at,
                            trace_id, traceparent, tracestate
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            event_uuid,
                            event_type,
                            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                            created_at,
                            str(trace_context.get("trace_id") or ""),
                            str(trace_context.get("traceparent") or ""),
                            str(trace_context.get("tracestate") or ""),
                        ),
                    )
                self._connection.execute(
                    "UPDATE edge_command SET status = 'completed' WHERE command_uuid = ?",
                    (command_uuid,),
                )
                self._advance_command_highwater_locked(command_uuid)
                job_uuid = str(resolution_payload.get("job_uuid") or "")
                if job_uuid and remaining_unknown_command_ids == []:
                    # 结构化剩余集合明确为空，表示本次是该工作流节点作业
                    # （WorkflowNodeJob）的最后一条 UNKNOWN 处置；该事实既可来自
                    # 结果上报，也可来自取消超时后的对账恢复。仍须等待
                    # Backend ACK 后才能真正退役根命令和作业镜像。
                    self._connection.execute(
                        """
                        UPDATE edge_job_runtime
                        SET status = ?, job_access_token = '', updated_at = ?
                        WHERE job_uuid = ?
                          AND status IN (
                              'outcome_committed_unknown',
                              'outcome_pending',
                              'cancel_requested',
                              'running',
                              'dispatching'
                          )
                        """,
                        (
                            f"outcome_resolution_pending_final:{resolution_event_uuid}",
                            time.time(),
                            job_uuid,
                        ),
                    )

    def _advance_command_highwater_locked(self, command_uuid: str) -> None:
        """按已完成命令推进持久投递重放高水位。

        ``command_uuid`` 定位本事务刚完成的命令；返回为空。调用方必须持有锁
        和写事务，缺失命令不会降低既有高水位。
        """

        command = self._connection.execute(
            "SELECT sequence FROM edge_command WHERE command_uuid = ? AND status = 'completed'",
            (command_uuid,),
        ).fetchone()
        if command is None:
            return
        stored = self._connection.execute(
            "SELECT value FROM edge_control_meta WHERE key = 'last_ack_command_sequence'"
        ).fetchone()
        current = int(stored["value"]) if stored is not None else 0
        sequence = max(current, int(command["sequence"]))
        self._connection.execute(
            """
            INSERT INTO edge_control_meta(key, value)
            VALUES ('last_ack_command_sequence', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (str(sequence),),
        )

    def last_ack_command_sequence(self) -> int:
        """返回可向 Backend 证明已完成的连续命令高水位。

        参数为空；返回持久序号。该值独立于命令明细，因此安全退役明细后仍能
        阻止旧命令投递重放。
        """

        with self._lock:
            row = self._connection.execute(
                """
                SELECT value FROM edge_control_meta
                WHERE key = 'last_ack_command_sequence'
                """
            ).fetchone()
        return int(row["value"]) if row is not None else 0

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
                    trace_id, traceparent, tracestate
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_uuid,
                    event_type,
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    created_at,
                    str(trace_context.get("trace_id") or ""),
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
                       trace_id, traceparent, tracestate
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
                trace_id=str(row["trace_id"]),
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

    def event_for_ack(self, event_uuid: str) -> Optional[StoredEvent]:
        """读取即将被 Backend ACK 的事件，供物理结算后清理设备账本。

        ``event_uuid`` 是发件箱事件身份；返回尚未退役的事件快照，缺失时返回
        ``None``。该方法不修改数据，调用方完成设备侧结算清理后再确认事件。
        """

        with self._lock:
            row = self._connection.execute(
                """
                SELECT event_uuid, type, payload_json, created_at,
                       trace_id, traceparent, tracestate
                FROM edge_event_outbox WHERE event_uuid = ?
                """,
                (event_uuid,),
            ).fetchone()
        if row is None:
            return None
        return StoredEvent(
            event_uuid=str(row["event_uuid"]),
            event_type=str(row["type"]),
            payload=json.loads(str(row["payload_json"])),
            created_at=str(row["created_at"]),
            traceparent=str(row["traceparent"]),
            tracestate=str(row["tracestate"]),
            trace_id=str(row["trace_id"]),
        )

    def acknowledge_event(self, event_uuid: str) -> None:
        """接收 Backend 事件 ACK 并原子退役已证明可恢复的本地明细。

        ``event_uuid`` 是 Edge 发件箱事件身份；返回为空。重复 ACK 幂等。命令
        ACK 会退役对应已完成命令；结果 ACK 只退役已经完成物理结算的终态
        工作流节点作业（WorkflowNodeJob）。
        """

        with self._lock:
            with self._immediate_transaction():
                event = self._connection.execute(
                    """
                    SELECT event_uuid, type, payload_json
                    FROM edge_event_outbox WHERE event_uuid = ?
                    """,
                    (event_uuid,),
                ).fetchone()
                if event is not None:
                    self._retire_event_locked(event)

    def _retire_event_locked(self, event: sqlite3.Row) -> None:
        """按已确认事件类型退役发件箱及其边缘执行镜像明细。

        ``event`` 提供事件身份、类型和 JSON 载荷；返回为空。调用方必须持有锁
        和事务，非法旧载荷只删除已 ACK 事件而不猜测关联身份。
        """

        try:
            payload = json.loads(str(event["payload_json"]))
        except (TypeError, ValueError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        event_type = str(event["type"])
        if event_type == "command.ack":
            command_uuid = str(payload.get("command_uuid") or "")
            if command_uuid:
                self._connection.execute(
                    """
                    DELETE FROM edge_command
                    WHERE command_uuid = ? AND status = 'completed'
                      AND sequence <= CAST((
                          SELECT value FROM edge_control_meta
                          WHERE key = 'last_ack_command_sequence'
                      ) AS INTEGER)
                    """,
                    (command_uuid,),
                )
        elif event_type == "job.outcome_committed":
            job_uuid = str(payload.get("job_uuid") or "")
            if job_uuid:
                self._connection.execute(
                    """
                    DELETE FROM edge_job_runtime
                    WHERE job_uuid = ?
                      AND status IN ('outcome_committed', 'outcome_retired')
                      AND outcome IS NULL
                    """,
                    (job_uuid,),
                )
        elif event_type == "job.unknown_resolution_committed":
            job_uuid = str(payload.get("job_uuid") or "")
            if job_uuid:
                self._connection.execute(
                    """
                    DELETE FROM edge_job_runtime
                    WHERE job_uuid = ?
                      AND status = ?
                      AND outcome IS NULL
                    """,
                    (
                        job_uuid,
                        "outcome_resolution_pending_final:"
                        f"{str(event['event_uuid'])}",
                    ),
                )
        self._connection.execute(
            "DELETE FROM edge_event_outbox WHERE event_uuid = ?",
            (str(event["event_uuid"]),),
        )

    def save_job_start(
        self,
        payload: Dict[str, Any],
        command_uuid: str,
        trace_context: Optional[Dict[str, str]] = None,
    ) -> bool:
        """原子保存 Job 镜像并拒绝任何过期资源 Fence。

        参数：``payload`` 必须含 Task/Job/Node、Claim、attempt、fences 和访问令牌；
        ``command_uuid`` 是 WebSocket 命令身份；``trace_context`` 是可选追踪上下文。
        返回首次插入时为真，完全相同重放为假。异常：身份变化、Fence 回退或非法
        载荷抛 ``ValueError``，不会留下部分资源栅栏或作业镜像。
        """

        required = (
            "job_uuid",
            "task_uuid",
            "node_uuid",
            "claim_uuid",
            "job_access_token",
        )
        execution_source = payload.get("execution_source", "workflow")
        if execution_source not in {"workflow", "error_manual"}:
            raise ValueError("invalid execution_source")
        manual = execution_source == "error_manual"
        if any(not str(payload.get(field) or "").strip() for field in required if not (manual and field == "claim_uuid")):
            raise ValueError("job.start identity, Claim and token are required")
        attempt = payload.get("attempt")
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            raise ValueError("job.start attempt must be a positive integer")
        fences = _normalize_fences(payload.get("fences"))
        if manual and (payload.get("claim_uuid") is not None or fences):
            raise ValueError("手动执行不得携带 Claim/Fence")
        trace_context = trace_context or {}
        trace_id = str(trace_context.get("trace_id") or "")
        traceparent = str(trace_context.get("traceparent") or "")
        tracestate = str(trace_context.get("tracestate") or "")
        values = (
            str(uuid.UUID(str(payload["job_uuid"]))),
            str(uuid.UUID(str(payload["task_uuid"]))),
            str(uuid.UUID(str(payload["node_uuid"]))),
            str(uuid.UUID(command_uuid)),
            "" if manual else str(uuid.UUID(str(payload["claim_uuid"]))),
            attempt,
            json.dumps(fences, separators=(",", ":")),
            str(payload["job_access_token"]),
            trace_id,
            traceparent,
            tracestate,
            time.time(),
        )
        with self._lock, self._immediate_transaction():
            if not trace_id and not traceparent and not tracestate:
                command = self._connection.execute(
                    """
                    SELECT trace_id, traceparent, tracestate FROM edge_command
                    WHERE command_uuid = ?
                    """,
                    (str(uuid.UUID(command_uuid)),),
                ).fetchone()
                if command is not None:
                    trace_id = str(command["trace_id"])
                    traceparent = str(command["traceparent"])
                    tracestate = str(command["tracestate"])
                    values = values[:-4] + (
                        trace_id,
                        traceparent,
                        tracestate,
                        values[-1],
                    )
            existing = self._connection.execute(
                "SELECT * FROM edge_job_runtime WHERE job_uuid = ?",
                (values[0],),
            ).fetchone()
            if existing is not None:
                actual = (
                    existing["task_uuid"],
                    existing["node_uuid"],
                    existing["command_uuid"],
                    existing["claim_uuid"],
                    int(existing["attempt"]),
                    existing["fences_json"],
                    existing["job_access_token"],
                )
                expected = (
                    values[1],
                    values[2],
                    values[3],
                    values[4],
                    values[5],
                    values[6],
                    values[7],
                )
                if actual != expected or existing["execution_source"] != execution_source:
                    raise ValueError("duplicate job.start identity or Claim changed")
                return False
            for fence in fences:
                current = self._connection.execute(
                    "SELECT * FROM edge_resource_fence WHERE lock_key = ?",
                    (fence["lock_key"],),
                ).fetchone()
                token = int(fence["fencing_token"])
                # 库存权威为同一 Task 的共享区间成员签发不同 Claim、同一
                # Fence。只有同一 Task 可沿用当前资源版本；跨 Task 或回退拒绝。
                if current is not None and (
                    token < int(current["last_fencing_token"])
                    or (
                        token == int(current["last_fencing_token"])
                        and current["task_uuid"] != values[1]
                    )
                ):
                    raise ValueError(
                        "stale resource Fence: "
                        f"{fence['lock_key']}/{token}"
                    )
                self._connection.execute(
                    """
                    INSERT INTO edge_resource_fence(
                        lock_key, last_fencing_token, claim_uuid, job_uuid, updated_at, task_uuid
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(lock_key) DO UPDATE SET
                        last_fencing_token = excluded.last_fencing_token,
                        claim_uuid = excluded.claim_uuid,
                        job_uuid = excluded.job_uuid,
                        updated_at = excluded.updated_at,
                        task_uuid = excluded.task_uuid
                    """,
                    (
                        fence["lock_key"],
                        token,
                        values[4],
                        values[0],
                        time.time(),
                        values[1],
                    ),
                )
            cursor = self._connection.execute(
                """
                INSERT OR IGNORE INTO edge_job_runtime(
                    job_uuid, task_uuid, node_uuid, command_uuid,
                    claim_uuid, attempt, fences_json, job_access_token,
                    status, trace_id, traceparent, tracestate, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'received', ?, ?, ?, ?)
                """,
                values,
            )
            self._connection.execute("UPDATE edge_job_runtime SET execution_source=? WHERE job_uuid=?", (execution_source, values[0]))
            if cursor.rowcount == 0 and (trace_id or traceparent or tracestate):
                self._connection.execute(
                    """
                    UPDATE edge_job_runtime
                    SET trace_id = CASE
                            WHEN trace_id = '' THEN ? ELSE trace_id END,
                        traceparent = CASE
                            WHEN traceparent = '' THEN ? ELSE traceparent END,
                        tracestate = CASE
                            WHEN tracestate = '' THEN ? ELSE tracestate END
                    WHERE job_uuid = ?
                    """,
                    (trace_id, traceparent, tracestate, values[0]),
                )
            return cursor.rowcount == 1

    def get_job(self, job_uuid: str) -> Optional[StoredJob]:
        """读取一个动作执行镜像作业。

        参数：``job_uuid`` 是工站节点作业身份。返回对应 ``StoredJob``，不存在时
        返回 ``None``；SQLite 查询异常原样传播，不修改任何协议事实。
        """

        with self._lock:
            row = self._connection.execute(
                """
                SELECT job_uuid, task_uuid, node_uuid, command_uuid,
                       claim_uuid, attempt, fences_json, execution_source,
                       job_access_token, status, feedback_sequence,
                       trace_id, traceparent, tracestate
                FROM edge_job_runtime WHERE job_uuid = ?
                """,
                (job_uuid,),
            ).fetchone()
        return _stored_job(row) if row is not None else None

    def list_jobs(self, statuses: Iterable[str]) -> List[StoredJob]:
        """按动作镜像状态读取作业。

        参数：``statuses`` 是允许状态集合。返回按更新时间排序的作业；空集合返回
        空列表。异常：SQLite 查询错误原样传播，不推断工作流业务状态。
        """

        status_list = list(statuses)
        if not status_list:
            return []
        placeholders = ",".join("?" for _ in status_list)
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT job_uuid, task_uuid, node_uuid, command_uuid,
                       claim_uuid, attempt, fences_json, execution_source,
                       job_access_token, status, feedback_sequence,
                       trace_id, traceparent, tracestate
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

    def abort_restarted_job(self, job_uuid: str) -> None:
        """退役工站权威已终结的旧进程镜像，不发布新的结果或对账请求。"""
        with self._lock, self._immediate_transaction():
            self._connection.execute(
                "UPDATE edge_job_runtime SET status='outcome_retired',updated_at=? WHERE job_uuid=?",
                (time.time(), job_uuid),
            )
            self._connection.execute(
                "UPDATE edge_job_runtime SET outcome=NULL, return_info_json=NULL, "
                "error_info_json=NULL, unknown_command_ids_json=NULL, "
                "outcome_updated_at=NULL WHERE job_uuid=?",
                (job_uuid,),
            )
            # 已发生的事件仍留在 outbox，由新会话照常 ACK；它们不能重开终态任务。

    def preserve_restarted_job(self, job_uuid: str, *, not_started: bool) -> None:
        """保留原回执；仅有持久未启动证明时重新开放同一命令的取参。"""
        with self._lock, self._immediate_transaction():
            self._connection.execute(
                "UPDATE edge_job_runtime SET status=CASE "
                "WHEN status='cancel_requested' THEN 'cancel_requested' "
                "WHEN ? THEN 'received' ELSE 'running' END,updated_at=? "
                "WHERE job_uuid=? "
                "AND status IN ('received','fetch_retry','dispatching','running','cancel_requested') "
                "AND outcome IS NULL",
                (bool(not_started), time.time(), job_uuid),
            )
            self._connection.execute(
                "INSERT INTO edge_control_meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (f"recovery_policy:{job_uuid}", "reconcile"),
            )

    def defer_not_started_job(self, job_uuid: str) -> None:
        """只撤回本次尚未开始的 dispatching，不覆盖并发取消或真实终态。"""
        with self._lock:
            self._connection.execute("UPDATE edge_job_runtime SET status='received',updated_at=? WHERE job_uuid=? AND status='dispatching'", (time.time(),job_uuid))
            self._connection.commit()

    def next_feedback_sequence(self, job_uuid: str) -> int:
        """原子分配作业反馈序号。

        ``job_uuid`` 是 Edge 作业镜像身份；返回下一个正整数序号。作业不存在时
        抛出 ``KeyError``，任意数据库异常都会回滚。
        """

        with self._lock:
            with self._immediate_transaction():
                row = self._connection.execute(
                    "SELECT feedback_sequence FROM edge_job_runtime WHERE job_uuid = ?",
                    (job_uuid,),
                ).fetchone()
                if row is None:
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
                return sequence

    def save_pending_outcome(
        self,
        job_uuid: str,
        outcome: str,
        return_info: Dict[str, Any],
        error_info: List[Dict[str, Any]],
        unknown_command_ids: Optional[List[str]] = None,
    ) -> bool:
        """原子保存待提交终态，重复设备回调以第一次终态为准。

        ``job_uuid`` 是边缘执行镜像（EdgeExecutionMirror）中的作业身份；
        ``outcome``、``return_info`` 和 ``error_info`` 是待提交结果；
        ``unknown_command_ids`` 是尚未物理结算的设备命令身份。首次保存返回
        ``True``，重复回调或作业镜像已退役时返回 ``False``；
        序列化或数据库失败时完整回滚。已进入结果提交/物理结算的
        作业拒绝迟到回调，禁止重建失效 Token 待办。
        """

        with self._lock:
            with self._immediate_transaction():
                runtime = self._connection.execute(
                    "SELECT status FROM edge_job_runtime WHERE job_uuid = ?",
                    (job_uuid,),
                ).fetchone()
                if runtime is None:
                    return False
                runtime_status = str(runtime["status"])
                if runtime_status in {
                    "outcome_pending",
                    "outcome_committed",
                    "outcome_committed_unknown",
                    "outcome_retired",
                } or runtime_status.startswith("outcome_resolution_pending_final:"):
                    return False
                cursor = self._connection.execute(
                    """
                    UPDATE edge_job_runtime SET
                        outcome=?, return_info_json=?, error_info_json=?,
                        unknown_command_ids_json=?, outcome_updated_at=?
                    WHERE job_uuid=? AND outcome IS NULL
                    """,
                    (
                        outcome,
                        json.dumps(return_info, ensure_ascii=False, separators=(",", ":")),
                        json.dumps(error_info, ensure_ascii=False, separators=(",", ":")),
                        json.dumps(unknown_command_ids or [], ensure_ascii=False, separators=(",", ":")),
                        time.time(),
                        job_uuid,
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
                return cursor.rowcount == 1

    def get_pending_outcome(self, job_uuid: str) -> Optional[StoredOutcome]:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT job_uuid, outcome, return_info_json, error_info_json,
                       unknown_command_ids_json
                FROM edge_job_runtime WHERE job_uuid = ? AND outcome IS NOT NULL
                """,
                (job_uuid,),
            ).fetchone()
        return _stored_outcome(row) if row is not None else None

    def list_pending_outcomes(self) -> List[StoredOutcome]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT job_uuid, outcome, return_info_json, error_info_json,
                       unknown_command_ids_json
                FROM edge_job_runtime WHERE outcome IS NOT NULL
                ORDER BY outcome_updated_at
                """
            ).fetchall()
        return [_stored_outcome(row) for row in rows]

    def complete_pending_outcome(
        self,
        job_uuid: str,
        event_payload: Dict[str, Any],
        trace_context: Optional[Dict[str, str]] = None,
    ) -> str:
        """原子清除 HTTP 待办并创建最终 WebSocket 通知。

        ``job_uuid`` 定位已被 Backend 持久化的结果，``event_payload`` 是严格
        ``job.outcome_committed`` 线协议（wire）载荷，
        ``trace_context`` 是可选追踪上下文。
        返回新事件 UUID；待办不存在时返回空字符串。携带物理不确定命令的作业
        保留独立本地状态，直到对账恢复（Reconciliation）完成物理结算。
        UNKNOWN JSON 不是列表时抛出 ``ValueError``；序列化或 SQLite
        异常原样抛出且整个状态转换回滚。
        """

        event_uuid = str(uuid.uuid4())
        created_at = _utc_now()
        trace_context = trace_context or {}
        with self._lock:
            with self._immediate_transaction():
                pending = self._connection.execute(
                    """
                    SELECT unknown_command_ids_json
                    FROM edge_job_runtime WHERE job_uuid = ? AND outcome IS NOT NULL
                    """,
                    (job_uuid,),
                ).fetchone()
                if pending is None:
                    return ""
                # 该集合是工作流任务业务终态之外的物理不确定事实；非空时
                # Backend 结果 ACK 不能被解释成物理结算（PhysicalSettlement）。
                pending_unknown_command_ids = json.loads(
                    str(pending["unknown_command_ids_json"])
                )
                if not isinstance(pending_unknown_command_ids, list):
                    raise ValueError("pending outcome unknown command IDs must be a list")
                runtime_status = (
                    "outcome_committed_unknown"
                    if pending_unknown_command_ids
                    else "outcome_committed"
                )
                trace_id = str(trace_context.get("trace_id") or "")
                traceparent = str(trace_context.get("traceparent") or "")
                tracestate = str(trace_context.get("tracestate") or "")
                if not trace_id and not traceparent and not tracestate:
                    runtime = self._connection.execute(
                        """
                        SELECT trace_id, traceparent, tracestate FROM edge_job_runtime
                        WHERE job_uuid = ?
                        """,
                        (job_uuid,),
                    ).fetchone()
                    if runtime is not None:
                        trace_id = str(runtime["trace_id"])
                        traceparent = str(runtime["traceparent"])
                        tracestate = str(runtime["tracestate"])
                self._connection.execute(
                    """
                    INSERT INTO edge_event_outbox(
                        event_uuid, type, payload_json, created_at,
                        trace_id, traceparent, tracestate
                    ) VALUES (?, 'job.outcome_committed', ?, ?, ?, ?, ?)
                    """,
                    (
                        event_uuid,
                        json.dumps(
                            event_payload, ensure_ascii=False, separators=(",", ":")
                        ),
                        created_at,
                        trace_id,
                        traceparent,
                        tracestate,
                    ),
                )
                self._connection.execute(
                    """
                    UPDATE edge_job_runtime
                    SET status = CASE
                            WHEN status LIKE 'outcome_resolution_pending_final:%'
                            THEN status ELSE ?
                        END,
                        job_access_token = '', updated_at = ?
                    WHERE job_uuid = ?
                    """,
                    (runtime_status, time.time(), job_uuid),
                )
                self._connection.execute(
                    "UPDATE edge_job_runtime SET outcome=NULL, return_info_json=NULL, "
                    "error_info_json=NULL, unknown_command_ids_json=NULL, "
                    "outcome_updated_at=NULL WHERE job_uuid=?",
                    (job_uuid,),
                )
        return event_uuid

    def retire_pending_outcome(self, job_uuid: str) -> bool:
        """退役 Backend 已明确拒绝的结果，避免永久重放失效作业 Token。

        ``job_uuid`` 是被 Backend 权威终结的作业身份；实际删除待办时返回
        ``True``，不存在时返回 ``False``。待办与不再承担恢复职责的作业镜像
        在同一事务删除；若最终 UNKNOWN 事件仍等待 ACK，则保留其精确事件标记，
        由 ACK 完成设备根命令退役后再删除。失败时完整回滚。
        """

        with self._lock:
            with self._immediate_transaction():
                pending = self._connection.execute(
                    "SELECT 1 FROM edge_job_runtime WHERE job_uuid = ? AND outcome IS NOT NULL",
                    (job_uuid,),
                ).fetchone()
                if pending is None:
                    return False
                self._connection.execute(
                    "UPDATE edge_job_runtime SET outcome=NULL, return_info_json=NULL, "
                    "error_info_json=NULL, unknown_command_ids_json=NULL, "
                    "outcome_updated_at=NULL WHERE job_uuid = ?",
                    (job_uuid,),
                )
                runtime = self._connection.execute(
                    "SELECT status FROM edge_job_runtime WHERE job_uuid = ?",
                    (job_uuid,),
                ).fetchone()
                keep_for_final_ack = False
                if runtime is not None:
                    status = str(runtime["status"])
                    prefix = "outcome_resolution_pending_final:"
                    if status.startswith(prefix):
                        final_event_uuid = status.removeprefix(prefix)
                        keep_for_final_ack = self._connection.execute(
                            """
                            SELECT 1 FROM edge_event_outbox
                            WHERE event_uuid = ?
                              AND type = 'job.unknown_resolution_committed'
                            """,
                            (final_event_uuid,),
                        ).fetchone() is not None
                if not keep_for_final_ack:
                    self._connection.execute(
                        "DELETE FROM edge_job_runtime WHERE job_uuid = ?",
                        (job_uuid,),
                    )
                return True


def _stored_job(row: sqlite3.Row) -> StoredJob:
    """把 Edge 作业镜像行恢复为含 Claim/Fence 的不可变对象。"""

    return StoredJob(
        job_uuid=str(row["job_uuid"]),
        task_uuid=str(row["task_uuid"]),
        node_uuid=str(row["node_uuid"]),
        command_uuid=str(row["command_uuid"]),
        claim_uuid=str(row["claim_uuid"]),
        execution_source=str(row["execution_source"]),
        attempt=int(row["attempt"]),
        fences=tuple(
            (str(item["lock_key"]), int(item["fencing_token"]))
            for item in json.loads(str(row["fences_json"]))
        ),
        job_access_token=str(row["job_access_token"]),
        status=str(row["status"]),
        feedback_sequence=int(row["feedback_sequence"]),
        traceparent=str(row["traceparent"]),
        tracestate=str(row["tracestate"]),
        trace_id=str(row["trace_id"]),
    )


def _normalize_fences(value: Any) -> list[dict[str, Any]]:
    """规范 Edge ``job.start`` 的资源栅栏列表并拒绝重复锁键。"""

    if not isinstance(value, list):
        raise ValueError("job.start fences must be a list")
    normalized: dict[str, int] = {}
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("job.start fence must be an object")
        lock_key = str(item.get("lock_key") or "").strip()
        token = item.get("fencing_token")
        if (
            not lock_key
            or isinstance(token, bool)
            or not isinstance(token, int)
            or token < 1
            or lock_key in normalized
        ):
            raise ValueError("job.start fence identity or token is invalid")
        normalized[lock_key] = token
    return [
        {"lock_key": lock_key, "fencing_token": normalized[lock_key]}
        for lock_key in sorted(normalized)
    ]


def _stored_outcome(row: sqlite3.Row) -> StoredOutcome:
    return StoredOutcome(
        job_uuid=str(row["job_uuid"]),
        outcome=str(row["outcome"]),
        return_info=json.loads(str(row["return_info_json"])),
        error_info=json.loads(str(row["error_info_json"])),
        unknown_command_ids=json.loads(str(row["unknown_command_ids_json"])),
    )


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + ".000000Z"
