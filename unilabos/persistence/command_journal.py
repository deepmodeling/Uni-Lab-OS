"""设备本地命令日志；事务内去重与序列 fencing，先持久化再执行物理动作。"""

import json
import re
import sqlite3
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from unilabos.runtime.commands import (
    CommandConflictError,
    CommandRecord,
    CommandRequest,
    CommandSequenceError,
    CommandState,
    LegacyCommandTables,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS device_command_v1 (
 namespace TEXT NOT NULL, command_id TEXT NOT NULL, request_hash TEXT NOT NULL,
 request_json TEXT NOT NULL, state TEXT NOT NULL, boot_id TEXT NOT NULL,
 message TEXT NOT NULL, output_json TEXT NOT NULL, created_at REAL NOT NULL,
 updated_at REAL NOT NULL, PRIMARY KEY(namespace,command_id));
CREATE TABLE IF NOT EXISTS device_command_epoch_v1 (
 namespace TEXT NOT NULL, source TEXT NOT NULL, source_boot_id TEXT NOT NULL,
 last_sequence INTEGER NOT NULL, active INTEGER NOT NULL,
 PRIMARY KEY(namespace,source,source_boot_id));
CREATE TABLE IF NOT EXISTS device_command_import_v1 (
 commands_table TEXT PRIMARY KEY, epochs_table TEXT NOT NULL, namespace TEXT NOT NULL);
"""


class SqliteCommandJournal:
    def __init__(self, path: str | Path, *, namespace: str,
                 legacy_tables: LegacyCommandTables | None = None) -> None:
        if not namespace or not namespace.strip():
            raise ValueError("命令日志必须指定稳定命名空间")
        self._path = Path(path).expanduser()
        self._namespace = namespace
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(_SCHEMA)
        if legacy_tables is not None:
            self._import_legacy(legacy_tables)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA synchronous=FULL")
            with connection:
                yield connection
        finally:
            connection.close()

    def _import_legacy(self, tables: LegacyCommandTables) -> None:
        for name in (tables.commands, tables.epochs):
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) or name.startswith("device_command_"):
                raise ValueError("旧日志表名无效")
        with self._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            imported = c.execute("SELECT * FROM device_command_import_v1 WHERE commands_table=?",
                                 (tables.commands,)).fetchone()
            if imported is not None:
                if (imported["namespace"], imported["epochs_table"]) != (self._namespace, tables.epochs):
                    raise ValueError("旧命令日志已由其他设备命名空间认领")
                return
            present = {row[0] for row in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables.commands not in present and tables.epochs not in present:
                return
            if not {tables.commands, tables.epochs} <= present:
                raise ValueError("旧命令日志不完整，禁止使用空日志启动")
            if any(c.execute(f"SELECT 1 FROM {table} WHERE namespace=? LIMIT 1",
                             (self._namespace,)).fetchone()
                   for table in ("device_command_v1", "device_command_epoch_v1")):
                raise ValueError("目标命令日志已有数据，禁止覆盖导入")
            c.execute(
                "INSERT INTO device_command_v1 SELECT ?,command_id,request_hash,request_json,state,"
                f'boot_id,message,output_json,created_at,updated_at FROM "{tables.commands}"',
                (self._namespace,),
            )
            c.execute(
                "INSERT INTO device_command_epoch_v1 SELECT ?,source,source_boot_id,last_sequence,active "
                f'FROM "{tables.epochs}"', (self._namespace,),
            )
            c.execute("INSERT INTO device_command_import_v1 VALUES (?,?,?)",
                      (tables.commands, tables.epochs, self._namespace))

    @staticmethod
    def _record(row: sqlite3.Row) -> CommandRecord:
        return CommandRecord(row["request_hash"], json.loads(row["request_json"]),
                             CommandState(row["state"]), row["boot_id"], row["message"],
                             json.loads(row["output_json"]))

    def accept(self, request: CommandRequest, boot_id: str) -> tuple[bool, CommandRecord]:
        if not all((request.command_id, request.source, request.source_boot_id, boot_id)):
            raise ValueError("命令、来源和启动身份不能为空")
        if type(request.monotonic_sequence) is not int or not 1 <= request.monotonic_sequence <= 2**63 - 1:
            raise ValueError("命令序列必须为 1..2^63-1 的整数")
        payload = request.to_dict()
        if payload.get("command_id") != request.command_id:
            raise ValueError("请求载荷与命令身份不一致")
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)
        fingerprint = request.fingerprint()
        with self._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM device_command_v1 WHERE namespace=? AND command_id=?",
                            (self._namespace, request.command_id)).fetchone()
            if row is not None:
                record = self._record(row)
                if record.request_hash != fingerprint:
                    raise CommandConflictError(f"command_id {request.command_id} 已被不同请求占用")
                return False, record
            epoch = c.execute(
                "SELECT last_sequence,active FROM device_command_epoch_v1 "
                "WHERE namespace=? AND source=? AND source_boot_id=?",
                (self._namespace, request.source, request.source_boot_id),
            ).fetchone()
            if epoch is not None:
                if not epoch["active"]:
                    raise CommandSequenceError(f"来源 {request.source} 使用了已退役 source_boot_id")
                if request.monotonic_sequence <= epoch["last_sequence"]:
                    raise CommandSequenceError("monotonic_sequence 未单调递增")
                c.execute("UPDATE device_command_epoch_v1 SET last_sequence=? "
                          "WHERE namespace=? AND source=? AND source_boot_id=?",
                          (request.monotonic_sequence, self._namespace, request.source, request.source_boot_id))
            else:
                c.execute("UPDATE device_command_epoch_v1 SET active=0 WHERE namespace=? AND source=?",
                          (self._namespace, request.source))
                c.execute("INSERT INTO device_command_epoch_v1 VALUES (?,?,?,?,1)",
                          (self._namespace, request.source, request.source_boot_id, request.monotonic_sequence))
            now = time.time()
            c.execute("INSERT INTO device_command_v1 VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (self._namespace, request.command_id, fingerprint, encoded, CommandState.ACCEPTED.value,
                       boot_id, "命令已登记，尚未下发", "{}", now, now))
            row = c.execute("SELECT * FROM device_command_v1 WHERE namespace=? AND command_id=?",
                            (self._namespace, request.command_id)).fetchone()
            assert row is not None
            return True, self._record(row)

    def get(self, command_id: str) -> CommandRecord | None:
        with self._connect() as c:
            row = c.execute("SELECT * FROM device_command_v1 WHERE namespace=? AND command_id=?",
                            (self._namespace, command_id)).fetchone()
            return self._record(row) if row is not None else None

    def update(self, command_id: str, state: CommandState, boot_id: str, message: str,
               output: Mapping[str, Any] | None = None) -> CommandRecord:
        encoded = json.dumps(dict(output or {}), ensure_ascii=False, sort_keys=True, allow_nan=False)
        with self._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            cursor = c.execute("UPDATE device_command_v1 SET state=?,boot_id=?,message=?,output_json=?,updated_at=? "
                               "WHERE namespace=? AND command_id=?",
                               (state.value, boot_id, message, encoded, time.time(), self._namespace, command_id))
            if cursor.rowcount != 1:
                raise KeyError(f"命令不存在: {command_id}")
            row = c.execute("SELECT * FROM device_command_v1 WHERE namespace=? AND command_id=?",
                            (self._namespace, command_id)).fetchone()
            assert row is not None
            return self._record(row)

    def unknown_command_ids(self) -> list[str]:
        with self._connect() as c:
            return [row[0] for row in c.execute(
                "SELECT command_id FROM device_command_v1 WHERE namespace=? AND state=? ORDER BY created_at,command_id",
                (self._namespace, CommandState.UNKNOWN.value),
            )]

    def has_unknown(self) -> bool:
        return bool(self.unknown_command_ids())

    def mark_previous_boot_inflight_unknown(self, current_boot_id: str) -> int:
        with self._connect() as c:
            return c.execute("UPDATE device_command_v1 SET state=?,message=?,updated_at=? "
                             "WHERE namespace=? AND state IN (?,?) AND boot_id<>?",
                             (CommandState.UNKNOWN.value, "驱动已重启，原执行结果必须对账，禁止自动重发",
                              time.time(), self._namespace, CommandState.ACCEPTED.value,
                              CommandState.RUNNING.value, current_boot_id)).rowcount

    def reset(self) -> None:
        with self._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute("DELETE FROM device_command_v1 WHERE namespace=?", (self._namespace,))
            c.execute("DELETE FROM device_command_epoch_v1 WHERE namespace=?", (self._namespace,))
