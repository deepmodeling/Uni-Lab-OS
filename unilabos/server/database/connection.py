"""同库多线程共享连接：语句执行、取数与写事务使用同一把锁。"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from typing import Any


class MaterializedCursor:
    """锁内读取的游标快照；不会把仍在执行的 SQLite statement 带到锁外。"""

    def __init__(self, cursor: sqlite3.Cursor):
        self.description = cursor.description
        self._rows = cursor.fetchall() if self.description is not None else []
        self._index = 0
        self.rowcount = cursor.rowcount
        self.lastrowid = cursor.lastrowid
        self.arraysize = cursor.arraysize
        cursor.close()

    def fetchone(self) -> Any:
        if self._index >= len(self._rows):
            return None
        row = self._rows[self._index]
        self._index += 1
        return row

    def fetchmany(self, size: int | None = None) -> list[Any]:
        count = self.arraysize if size is None else size
        if count < 0:
            raise ValueError("fetchmany size must be non-negative")
        rows = self._rows[self._index:self._index + count]
        self._index += len(rows)
        return rows

    def fetchall(self) -> list[Any]:
        rows = self._rows[self._index:]
        self._index = len(self._rows)
        return rows

    def __iter__(self) -> Iterator[Any]:
        while self._index < len(self._rows):
            yield self.fetchone()


class SerializedConnection:
    """复用物料库的连接保护，供四库及同库共存域统一使用。

    check_same_thread=False 只是允许跨线程调用，不会替调用方保证
    execute → fetch 的原子性或事务隔离。每次 execute 在锁内取完结果，
    写事务必须持有同一个 write_lock 覆盖 BEGIN 到 COMMIT/ROLLBACK。
    查询范围仍由各业务 API 的分页、列选择和 LIMIT 控制。
    """

    def __init__(self, connection: sqlite3.Connection, lock: threading.RLock):
        self._connection = connection
        self.write_lock = lock

    def execute(self, sql: str, params: Any = ()) -> MaterializedCursor:
        with self.write_lock:
            return MaterializedCursor(self._connection.execute(sql, params))

    def executemany(self, sql: str, params: Any) -> MaterializedCursor:
        with self.write_lock:
            return MaterializedCursor(self._connection.executemany(sql, params))

    def commit(self) -> None:
        with self.write_lock:
            self._connection.commit()

    def rollback(self) -> None:
        with self.write_lock:
            self._connection.rollback()

    def close(self) -> None:
        with self.write_lock:
            self._connection.close()

    @property
    def in_transaction(self) -> bool:
        with self.write_lock:
            return self._connection.in_transaction

    @property
    def row_factory(self) -> Any:
        with self.write_lock:
            return self._connection.row_factory

    @row_factory.setter
    def row_factory(self, value: Any) -> None:
        with self.write_lock:
            self._connection.row_factory = value

    def __enter__(self) -> SerializedConnection:
        self.write_lock.acquire()
        return self

    def __exit__(self, *exc_info: Any) -> Any:
        try:
            return self._connection.__exit__(*exc_info)
        finally:
            self.write_lock.release()
