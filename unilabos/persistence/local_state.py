"""设备本地 JSON 状态存储；持久事务、文件身份及一次性迁移集中由 OS 实现。"""

import json
import re
import sqlite3
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from unilabos.runtime.state import LegacyState, LocalState

_META = "unilab_local_state_meta"
_RECORDS = "unilab_local_state_records"
_SEQUENCES = "unilab_local_state_sequences"


def _encode(value: Any) -> str:
    def validate(item: Any) -> None:
        if isinstance(item, dict):
            if any(not isinstance(k, str) for k in item):
                raise ValueError("状态 JSON 的键必须为字符串")
            for child in item.values():
                validate(child)
        elif isinstance(item, list):
            for child in item:
                validate(child)
        elif item is not None and type(item) not in (str, int, float, bool):
            raise ValueError("状态必须为 JSON 值")
    validate(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


class _LegacyReader:
    def __init__(self, connection: sqlite3.Connection, tables: frozenset[str]) -> None:
        self._connection = connection
        self.tables = tables

    def rows(self, table: str, columns: tuple[str, ...]) -> list[dict[str, Any]]:
        if table not in self.tables or not columns:
            raise ValueError("旧状态表不存在或未指定字段")
        if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", x) for x in (table, *columns)):
            raise ValueError("旧状态表或字段名称无效")
        available = {row[1] for row in self._connection.execute(f'PRAGMA table_info("{table}")')}
        if not set(columns) <= available:
            raise ValueError("旧状态表字段不完整")
        names = ",".join(f'"{name}"' for name in columns)
        rows = self._connection.execute(f'SELECT {names} FROM "{table}"').fetchall()
        return [dict(zip(columns, row, strict=True)) for row in rows]


class SqliteLocalState:
    def __init__(self, path: str | Path, *, namespace: str, create: bool = True,
                 readonly: bool = False,
                 initialize: Callable[[LocalState, LegacyState], None] | None = None) -> None:
        if not isinstance(namespace, str) or not namespace.strip():
            raise ValueError("本地状态必须指定稳定命名空间")
        self._path = Path(path).expanduser().absolute()
        self._namespace = namespace
        self._readonly = readonly
        self._closed = False
        self._local = threading.local()
        self._lock = threading.RLock()
        if create and not readonly:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        mode = "ro" if readonly else "rwc" if create else "rw"
        connection = self._connect(mode)
        try:
            if not readonly:
                connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("BEGIN" if readonly else "BEGIN IMMEDIATE")
            tables = frozenset(row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"))
            if _META in tables:
                row = connection.execute(f"SELECT namespace,version,identity FROM {_META}").fetchall()
                if len(row) != 1 or row[0][:2] != (namespace, 1):
                    raise ValueError("本地状态命名空间或版本不匹配")
                self._identity = row[0][2]
                if not {_RECORDS, _SEQUENCES} <= tables:
                    raise ValueError("本地状态文件不完整")
            else:
                if readonly:
                    raise ValueError("本地状态尚未迁移，请先由所有者打开")
                if tables and (initialize is None or tables & {_RECORDS, _SEQUENCES}):
                    raise ValueError("已有状态不能作为空库初始化")
                self._identity = str(uuid.uuid4())
                connection.execute(f"CREATE TABLE {_META} (namespace TEXT,version INTEGER,identity TEXT)")
                connection.execute(f"CREATE TABLE {_RECORDS} (collection TEXT,key TEXT,value TEXT NOT NULL,"
                                   "PRIMARY KEY(collection,key))")
                connection.execute(f"CREATE TABLE {_SEQUENCES} (collection TEXT PRIMARY KEY,value INTEGER NOT NULL)")
                connection.execute(f"INSERT INTO {_META} VALUES (?,1,?)", (namespace, self._identity))
                if initialize is not None:
                    self._local.connection = connection
                    try:
                        initialize(self, _LegacyReader(connection, tables))
                    finally:
                        self._local.connection = None
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _connect(self, mode: str) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path.as_uri() + "?mode=" + mode,
                                     uri=True, timeout=10, isolation_level=None)
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    @contextmanager
    def transaction(self) -> Iterator["SqliteLocalState"]:
        with self._lock:
            if self._closed:
                raise RuntimeError("本地状态已关闭")
            existing = getattr(self._local, "connection", None)
            if existing is not None:
                savepoint = "state_" + uuid.uuid4().hex
                existing.execute(f"SAVEPOINT {savepoint}")
                try:
                    yield self
                    existing.execute(f"RELEASE SAVEPOINT {savepoint}")
                except BaseException:
                    existing.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                    existing.execute(f"RELEASE SAVEPOINT {savepoint}")
                    raise
                return
            connection = self._connect("ro" if self._readonly else "rw")
            try:
                connection.execute("BEGIN" if self._readonly else "BEGIN IMMEDIATE")
                row = connection.execute(f"SELECT namespace,version,identity FROM {_META}").fetchall()
                if row != [(self._namespace, 1, self._identity)]:
                    raise ValueError("本地状态文件已替换或身份不符")
                self._local.connection = connection
                yield self
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            finally:
                self._local.connection = None
                connection.close()

    @staticmethod
    def _validate_names(collection: str, key: str = "_") -> None:
        if any(not isinstance(v, str) or not v for v in (collection, key)):
            raise ValueError("状态集合和键不能为空")

    def get(self, collection: str, key: str, default: Any = None) -> Any:
        self._validate_names(collection, key)
        with self.transaction():
            row = self._local.connection.execute(
                f"SELECT value FROM {_RECORDS} WHERE collection=? AND key=?", (collection, key)).fetchone()
            return json.loads(row[0]) if row else default

    def items(self, collection: str) -> dict[str, Any]:
        self._validate_names(collection)
        with self.transaction():
            return {key: json.loads(value) for key, value in self._local.connection.execute(
                f"SELECT key,value FROM {_RECORDS} WHERE collection=? ORDER BY key", (collection,))}

    def put(self, collection: str, key: str, value: Any) -> None:
        self._validate_names(collection, key)
        encoded = _encode(value)
        if self._readonly:
            raise PermissionError("本地状态仅允许查询")
        with self.transaction():
            self._local.connection.execute(f"INSERT INTO {_RECORDS} VALUES (?,?,?) "
                "ON CONFLICT(collection,key) DO UPDATE SET value=excluded.value", (collection, key, encoded))

    def delete(self, collection: str, key: str) -> None:
        self._validate_names(collection, key)
        if self._readonly:
            raise PermissionError("本地状态仅允许查询")
        with self.transaction():
            self._local.connection.execute(f"DELETE FROM {_RECORDS} WHERE collection=? AND key=?", (collection, key))

    def append(self, collection: str, value: Any) -> int:
        self._validate_names(collection)
        if self._readonly:
            raise PermissionError("本地状态仅允许查询")
        with self.transaction():
            row = self._local.connection.execute(
                f"SELECT value FROM {_SEQUENCES} WHERE collection=?", (collection,)).fetchone()
            sequence = (row[0] if row else 0) + 1
            missing = object()
            if self.get(collection, str(sequence), missing) is not missing:
                raise ValueError("追加序列与已有记录冲突")
            self.put(collection, str(sequence), value)
            self._local.connection.execute(f"INSERT INTO {_SEQUENCES} VALUES (?,?) "
                "ON CONFLICT(collection) DO UPDATE SET value=excluded.value", (collection, sequence))
            return sequence

    def close(self) -> None:
        with self._lock:
            if getattr(self._local, "connection", None) is not None:
                raise RuntimeError("不能在事务中关闭本地状态")
            self._closed = True
