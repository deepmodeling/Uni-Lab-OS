"""开发调试路由：四库 SQLite 的只读浏览面（Swagger 即调试页面）。

不参与业务写路径：每次请求用 ``mode=ro`` 短连接直接打开数据库文件，
不共享 writer 连接、不抢写锁；BLOB 列只回长度占位，避免大对象出流。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, FastAPI, HTTPException, Query

from unilabos.server.api.envelope import envelope_router
from unilabos.server.database import ServerDatabasePaths

_INTERNAL_TABLE_PREFIX = "sqlite_"


def _readonly_connect(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)


def _database_path(paths: ServerDatabasePaths, database: str) -> Path:
    mapping = paths.as_mapping()
    if database not in mapping:
        known = ", ".join(sorted(mapping))
        raise HTTPException(
            status_code=404,
            detail=f"unknown database {database!r}; expected one of: {known}",
        )
    path = mapping[database]
    if not path.is_file():
        raise HTTPException(
            status_code=404,
            detail=f"database file for {database!r} does not exist yet: {path}",
        )
    return path


def _list_tables(connection: sqlite3.Connection) -> list[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
    ).fetchall()
    return [name for (name,) in rows if not name.startswith(_INTERNAL_TABLE_PREFIX)]


def _json_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return f"<blob {len(value)} bytes>"
    return value


def create_debug_router(paths: ServerDatabasePaths) -> APIRouter:
    """创建只读 db 调试 router；配合 ``/api/docs`` 直接在浏览器里查库。"""

    router = envelope_router(prefix="/api/v1/debug", tags=["debug"])

    @router.get("/databases")
    async def list_databases():
        """列出四库的文件状态与每张表的行数。"""

        databases = []
        for key, path in sorted(paths.as_mapping().items()):
            entry: dict[str, Any] = {
                "database": key,
                "path": str(path),
                "exists": path.is_file(),
            }
            if entry["exists"]:
                entry["size_bytes"] = path.stat().st_size
                with _readonly_connect(path) as connection:
                    entry["tables"] = [
                        {
                            "name": table,
                            "rows": connection.execute(
                                f'SELECT COUNT(*) FROM "{table}"'
                            ).fetchone()[0],
                        }
                        for table in _list_tables(connection)
                    ]
            databases.append(entry)
        return {"root": str(paths.root), "databases": databases}

    @router.get("/databases/{database}/tables/{table}")
    async def browse_table(
        database: str,
        table: str,
        limit: int = Query(default=50, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        order: Optional[str] = Query(
            default=None,
            description="排序列名（默认按 rowid 倒序，即最新写入在前）",
        ),
        descending: bool = Query(default=True),
    ):
        """浏览单表：列定义 + 总行数 + 一页行数据（默认最新在前）。"""

        path = _database_path(paths, database)
        with _readonly_connect(path) as connection:
            tables = _list_tables(connection)
            if table not in tables:
                raise HTTPException(
                    status_code=404,
                    detail=f"table {table!r} does not exist in {database!r}",
                )
            columns = [
                {"name": name, "type": column_type, "pk": bool(pk)}
                for (_, name, column_type, _, _, pk) in connection.execute(
                    f'PRAGMA table_info("{table}")'
                ).fetchall()
            ]
            column_names = {column["name"] for column in columns}
            if order is not None and order not in column_names:
                raise HTTPException(
                    status_code=422,
                    detail=f"unknown order column {order!r} for table {table!r}",
                )
            total_rows = connection.execute(
                f'SELECT COUNT(*) FROM "{table}"'
            ).fetchone()[0]
            direction = "DESC" if descending else "ASC"
            order_sql = f'"{order}"' if order is not None else "rowid"
            try:
                cursor = connection.execute(
                    f'SELECT * FROM "{table}" ORDER BY {order_sql} {direction} '
                    "LIMIT ? OFFSET ?",
                    (limit, offset),
                )
            except sqlite3.OperationalError:
                # WITHOUT ROWID 表没有 rowid，可用主键序回退。
                cursor = connection.execute(
                    f'SELECT * FROM "{table}" LIMIT ? OFFSET ?',
                    (limit, offset),
                )
            names = [description[0] for description in cursor.description]
            rows = [
                {name: _json_value(value) for name, value in zip(names, row)}
                for row in cursor.fetchall()
            ]
        return {
            "database": database,
            "table": table,
            "columns": columns,
            "total_rows": total_rows,
            "limit": limit,
            "offset": offset,
            "rows": rows,
        }

    return router


def install_debug_api(app: FastAPI, paths: ServerDatabasePaths) -> None:
    app.include_router(create_debug_router(paths))


__all__ = ["create_debug_router", "install_debug_api"]
