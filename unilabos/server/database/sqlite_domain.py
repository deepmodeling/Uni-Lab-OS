"""域服务共享的 SQLite 载体：连接生命周期与唯一写事务入口。

表结构由 ``tables/`` 管理、输入输出类型由 ``protocol/`` 管理，域服务本身
持有连接并提供业务 API。同一个库文件上的多个域（如 ``runtime.db`` 的
data/workflow/registry、``materials.db`` 的 materials/graph）共享连接与
写锁：宿主域打开文件，其余域把宿主实例作为 ``database`` 传入。
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Union

from unilabos.server.database.connection import SerializedConnection
from unilabos.server.database.schema import DatabaseSpec, initialize_database

DomainDatabase = Union[str, Path, sqlite3.Connection, SerializedConnection, "SqliteDomain"]


class SqliteDomain:
    """一个域服务的 SQLite 底座：单连接、单写者、可共存同库域。

    ``database`` 支持三种形态：

    - ``str | Path``：打开（必要时初始化）库文件，独占连接；
    - ``sqlite3.Connection``：借用外部连接（测试/内存库），不负责关闭；
    - ``SqliteDomain``：同库共存域，直接共享宿主的连接与写锁。
    """

    def __init__(self, database: DomainDatabase, spec: DatabaseSpec):
        if isinstance(database, SqliteDomain):
            self.connection = database.connection
            self._write_lock = database._write_lock
            self._owns_connection = False
        elif isinstance(database, SerializedConnection):
            self.connection = database
            self._write_lock = database.write_lock
            self._owns_connection = False
        elif isinstance(database, sqlite3.Connection):
            self._write_lock = threading.RLock()
            self.connection = SerializedConnection(database, self._write_lock)
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA foreign_keys = ON")
            self._owns_connection = False
        else:
            self._write_lock = threading.RLock()
            self.connection = SerializedConnection(
                initialize_database(database, spec), self._write_lock
            )
            self._owns_connection = True

    @property
    def write_lock(self) -> threading.RLock:
        """本库进程内读写共用锁；同库共存域共享同一实例。"""

        return self._write_lock

    def close(self) -> None:
        if self._owns_connection:
            self.connection.close()

    def __enter__(self) -> "SqliteDomain":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @contextmanager
    def write(self) -> Iterator[SerializedConnection]:
        """本库唯一的进程内 writer 事务入口（BEGIN IMMEDIATE）。"""

        with self._write_lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                yield self.connection
            except BaseException:
                self.connection.rollback()
                raise
            else:
                self.connection.commit()


__all__ = ["DomainDatabase", "SqliteDomain"]
