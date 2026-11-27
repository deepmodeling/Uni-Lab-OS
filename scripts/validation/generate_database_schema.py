"""从当前 SQLite Store 初始化导出 PostgreSQL 使用的结构快照。"""

from __future__ import annotations

from unilabos.config.config import BasicConfig

_SCOPES = ("inventory", "workflow", "history", "device", "edge", "authority")
_KINDS = ("index", "table", "trigger", "view")


def generate() -> dict:
    """在临时 SQLite 上执行各 Store 的现有初始化，并导出结构快照。

    返回值与 ``unilabos/persistence/schema.json`` 同形。不创建新表，不连接
    PostgreSQL，也不改调用方的数据库类型配置。
    """

    previous = BasicConfig.database_type
    BasicConfig.database_type = "sqlite"
    try:
        return {scope: _objects(scope) for scope in _SCOPES}
    finally:
        BasicConfig.database_type = previous


def _objects(scope: str) -> list[dict]:
    store = _open(scope)
    try:
        connection = store.db
        grouped: dict[str, list] = {kind: [] for kind in _KINDS}
        rows = connection.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        for kind, name, statement in rows:
            grouped.setdefault(kind, []).append((name, statement))
        objects = []
        for kind in _KINDS:
            for name, statement in sorted(grouped.get(kind, [])):
                item = {"type": kind, "name": name, "sql": statement}
                if kind == "table":
                    item["columns"] = _columns(connection, name)
                    item["foreign_keys"] = _foreign_keys(connection, name)
                objects.append(item)
        return objects
    finally:
        store.close()


def _columns(connection, table: str) -> list[dict]:
    return [
        {
            "cid": row[0],
            "name": row[1],
            "type": row[2],
            "notnull": row[3],
            "default": row[4],
            "pk": row[5],
        }
        for row in connection.execute(f"PRAGMA table_info({table})")
    ]


def _foreign_keys(connection, table: str) -> list[dict]:
    return [
        {
            "id": row[0],
            "seq": row[1],
            "table": row[2],
            "from": row[3],
            "to": row[4],
            "on_update": row[5],
            "on_delete": row[6],
            "match": row[7],
        }
        for row in connection.execute(f"PRAGMA foreign_key_list({table})")
    ]


def _open(scope: str):
    if scope == "inventory":
        from unilabos.app.scheduler.inventory.store import InventoryStore

        return InventoryStore(":memory:")
    if scope == "workflow":
        from unilabos.workflow.store import WorkflowStore

        # schema.json 是完整兼容 catalog；PostgreSQL 运行事实 Store 会在
        # initialize(..., persist_workflow_definitions=False) 时筛掉定义对象。
        return WorkflowStore(":memory:", persist_workflow_definitions=True)
    if scope == "history":
        from unilabos.app.scheduler.history import WorkflowHistoryStore

        return WorkflowHistoryStore(":memory:")
    if scope == "device":
        from unilabos.app.scheduler.device_state import DeviceStateStore

        return DeviceStateStore(":memory:")
    if scope == "edge":
        from unilabos.app.edge_control.store import EdgeControlStore

        return EdgeControlStore(":memory:")
    from unilabos.app.edge_control.local_authority import LocalEdgeAuthorityStore

    return LocalEdgeAuthorityStore(":memory:")
