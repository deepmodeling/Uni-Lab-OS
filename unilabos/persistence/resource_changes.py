"""物料画布的事务内失效日志；通知只包含身份，当前状态仍通过 HTTP 读取。"""

from __future__ import annotations

import json
from contextlib import contextmanager

from unilabos.persistence.database import PostgreSQLDatabase

_SCHEMA = """CREATE TABLE IF NOT EXISTS {table} (
 id INTEGER PRIMARY KEY AUTOINCREMENT, event TEXT NOT NULL, data TEXT NOT NULL,
 create_time TEXT NOT NULL)"""


def change_event_table(scope: str) -> str:
    """固定域对应固定日志表，不接受调用者指定任意 SQL 标识符。"""
    return {"inventory": "material_change_event", "workflow": "resource_usage_change_event"}[scope]


def initialize_resource_changes(connection, scope: str) -> None:
    """安装两种数据库共用的变更捕获，回滚业务事务时通知也一起回滚。"""
    table_name = change_event_table(scope)
    connection.execute(_SCHEMA.format(table=table_name))
    if scope == "inventory":
        specs = [
            ("material", "material.changed", {"material_uuids": ("uuid", "parent_uuid")}, None),
            ("site", "material.changed", {"material_uuids": ("material_uuid", "occupied_material_uuid")}, None),
            ("station_handoff_resource", "material.changed", {"material_uuids": ("material_uuid",)}, None),
            (
                "station_handoff_source_location",
                "material.changed",
                {"material_uuids": ("carrier_material_uuid",)},
                None,
            ),
        ]
    else:
        specs = [
            (
                "execution_lock_lease",
                "resource.usage.changed",
                {"occupancy_uuids": ("uuid",)},
                ("state", "lock_key", "workflow_node_job_uuid", "deleted_at"),
            ),
            (
                "task_device_tenancy",
                "resource.usage.changed",
                {"occupancy_uuids": ("uuid",)},
                ("state", "device_lock_key", "acquired_by_job_uuid"),
            ),
            (
                "workflow_task_material_claim",
                "resource.usage.changed",
                {"binding_uuids": ("uuid",)},
                ("status", "material_uuid", "workflow_node_job_uuid", "deleted_at"),
            ),
            (
                "workflow_node_job",
                "resource.usage.changed",
                {"job_uuids": ("uuid",), "task_uuids": ("workflow_task_uuid",)},
                ("status", "control_data", "expected_change_set", "deleted_at"),
            ),
            (
                "workflow_task",
                "resource.usage.changed",
                {"task_uuids": ("uuid",)},
                ("status", "cleanup_status", "deleted_at"),
            ),
        ]
    for table, event, fields, watched in specs:
        if not connection.table_exists(table):
            continue
        for operation in ("INSERT", "UPDATE", "DELETE"):
            refs = ("OLD", "NEW") if operation == "UPDATE" else ("OLD" if operation == "DELETE" else "NEW",)
            pairs = []
            for key, columns in fields.items():
                values = ",".join(f"{ref}.{column}" for ref in refs for column in columns)
                pairs.append(f"'{key}',json_array({values})")
            when = ""
            if operation == "UPDATE" and watched:
                when = " WHEN " + " OR ".join(f"OLD.{col} IS NOT NEW.{col}" for col in watched)
            sql = (
                f"CREATE TRIGGER IF NOT EXISTS ui_change_{scope}_{table}_{operation.lower()} "
                f"AFTER {operation} ON {table}{when} BEGIN "
                f"INSERT INTO {table_name}(event,data,create_time) VALUES "
                f"('{event}',json_object({','.join(pairs)}),strftime('%Y-%m-%dT%H:%M:%fZ','now')); END;"
            )
            _install_trigger(connection, sql)
    if scope == "inventory":
        # 模板更新影响实例展示；整箱取货会同时改变箱内物料的在站范围。
        for table, predicate in (
            ("resource_template", "material.resource_template_uuid IN ({refs})"),
            (
                "station_handoff_source_location",
                "material.uuid IN (SELECT material_uuid FROM station_handoff_resource WHERE handoff_item_uuid IN ({refs}))",
            ),
        ):
            if not connection.table_exists(table):
                continue
            column = "uuid" if table == "resource_template" else "handoff_item_uuid"
            for operation in ("INSERT", "UPDATE", "DELETE"):
                refs = ("OLD", "NEW") if operation == "UPDATE" else ("OLD" if operation == "DELETE" else "NEW",)
                where = predicate.format(refs=",".join(f"{ref}.{column}" for ref in refs))
                _install_trigger(
                    connection,
                    f"CREATE TRIGGER IF NOT EXISTS ui_change_related_{table}_{operation.lower()} "
                    f"AFTER {operation} ON {table} BEGIN "
                    f"INSERT INTO {table_name}(event,data,create_time) "
                    "SELECT 'material.changed',json_object('material_uuids',json_array(material.uuid)),"
                    f"strftime('%Y-%m-%dT%H:%M:%fZ','now') FROM material WHERE {where}; END;",
                )
    # 独立序列与各 Store 的写锁一致，避免 PostgreSQL 跨库存/工作流事务乱序提交漏通知。
    _install_trigger(
        connection,
        f"""CREATE TRIGGER IF NOT EXISTS ui_change_retention_{scope}
        AFTER INSERT ON {table_name} WHEN NEW.id % 100 = 0 BEGIN
        DELETE FROM {table_name} WHERE id < NEW.id - 10000; END;""",
    )


def _install_trigger(connection, sql: str) -> None:
    if isinstance(connection, PostgreSQLDatabase):
        from unilabos.persistence.schema import trigger_sql

        connection._connection.execute(trigger_sql(sql))
    else:
        connection.execute(sql)


class ResourceChangeStore:
    """通过 Store 的公开读锁读取小型变更日志，独立于业务投影。"""

    def __init__(self, store, *, scope: str):
        self.store = store
        self.table = change_event_table(scope)

    @contextmanager
    def _read(self):
        reader = getattr(self.store, "read_connection", None) or self.store.read
        with reader() as connection:
            yield connection

    def latest_event_sequence(self) -> int:
        with self._read() as connection:
            return int(connection.execute(f"SELECT COALESCE(MAX(id),0) FROM {self.table}").fetchone()[0])

    def read_event_window(self, *, after_sequence: int, limit: int):
        with self._read() as connection:
            # 同一 SQL 快照同时取得水位和事件，避免两次 SELECT 间保留窗口前移。
            rows = connection.execute(
                f"""SELECT bounds.oldest,bounds.latest,e.id,e.event,e.data
                FROM (SELECT COALESCE(MIN(id),0) AS oldest,COALESCE(MAX(id),0) AS latest
                      FROM {self.table}) bounds
                LEFT JOIN {self.table} e ON e.id>? ORDER BY e.id LIMIT ?""",
                (after_sequence, limit),
            ).fetchall()
        first = rows[0]
        events = [
            {"id": row["id"], "event": row["event"], "data": json.loads(row["data"])}
            for row in rows
            if row["id"] is not None
        ]
        return int(first["oldest"]), int(first["latest"]), events
