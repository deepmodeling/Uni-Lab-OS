"""位置计划测试的后端无关库存事实快照。"""
from __future__ import annotations


def inventory_rows(store) -> dict[str, list[tuple]]:
    """记录全部业务表数据，用同一断言验证 SQLite/PG 的只读与回滚。"""
    with store.read_connection() as connection:
        return {
            name: sorted(
                (tuple(row) for row in connection.execute(
                    'SELECT * FROM "' + name.replace('"', '""') + '"'
                ).fetchall()),
                key=repr,
            )
            for name in sorted(connection.table_names())
            if not name.startswith("sqlite_")
        }
