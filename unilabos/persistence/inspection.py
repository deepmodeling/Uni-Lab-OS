"""业务及按需初始化共用的结构查询，同时接受遗留 SQLite 迁移连接。"""

from __future__ import annotations

import sqlite3

from unilabos.persistence.database import Database


def table_exists(connection: Database | sqlite3.Connection, name: str) -> bool:
    if isinstance(connection, Database):
        return connection.table_exists(name)
    if type(connection).__module__.startswith("psycopg"):
        return (
            connection.execute(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema=current_schema() AND table_type='BASE TABLE' "
                "AND table_name=%s",
                (name,),
            ).fetchone()
            is not None
        )
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def index_exists(connection: Database | sqlite3.Connection, name: str) -> bool:
    if isinstance(connection, Database):
        return connection.index_exists(name)
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def table_names(connection: Database | sqlite3.Connection) -> set[str]:
    if isinstance(connection, Database):
        return connection.table_names()
    return {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }


def query_many(connection, queries):
    """同一连接上的批量只读查询，兼容原 SQLite 迁移和测试连接。"""
    if isinstance(connection, Database):
        return connection.query_many(queries)
    return [
        connection.execute(sql, parameters).fetchall() for sql, parameters in queries
    ]
