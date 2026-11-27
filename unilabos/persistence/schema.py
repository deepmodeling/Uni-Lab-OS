"""PostgreSQL 当前结构初始化，SQLite 历史迁移保留在原 Store 中。"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from importlib.resources import files

from unilabos.persistence.sql import postgres_sql


@lru_cache(maxsize=1)
def catalog() -> dict:
    return json.loads(files("unilabos.persistence").joinpath("schema.json").read_text())


_WORKFLOW_DEFINITION_TABLES = frozenset(
    {
        "workflow",
        "workflow_node",
        "workflow_edge",
        "workflow_node_template",
        "workflow_handle_template",
        "workflow_source_registration",
        "workflow_authoring",
        # 工作流图中的数量需求也只存在于进程内定义目录。
        "workflow_inventory_requirement",
    }
)


def _workflow_runtime_objects(objects: list[dict]) -> list[dict]:
    """从完整工作流 catalog 中筛出运行事实对象。

    除定义表外，还排除引用定义表的索引和表；否则 PostgreSQL 会在运行库
    初始化时重新引入对内存定义的外键依赖。
    """

    def target_table(obj: dict) -> str | None:
        if obj["type"] == "table":
            return obj["name"]
        match = re.search(r"\b(?:ON|FROM|JOIN|INTO|VIEW)\s+(\w+)", obj["sql"], re.I)
        return match.group(1) if match else None

    filtered = []
    for obj in objects:
        if obj["type"] == "table":
            if obj["name"] in _WORKFLOW_DEFINITION_TABLES:
                continue
            if any(
                foreign_key.get("table") in _WORKFLOW_DEFINITION_TABLES
                for foreign_key in obj.get("foreign_keys", [])
            ):
                continue
        if target_table(obj) in _WORKFLOW_DEFINITION_TABLES:
            continue
        filtered.append(obj)
    return filtered


def objects_for_scope(
    scope: str,
    *,
    persist_workflow_definitions: bool = True,
) -> list[dict]:
    objects = catalog()[scope]
    if scope == "workflow" and not persist_workflow_definitions:
        return _workflow_runtime_objects(objects)
    return objects


def _ordered_tables(objects: list[dict]) -> list[dict]:
    pending = {o["name"]: o for o in objects if o["type"] == "table"}
    result = []
    while pending:
        ready = [
            o
            for name, o in pending.items()
            if not any(
                f["table"] in pending and f["table"] != name for f in o["foreign_keys"]
            )
        ]
        if not ready:
            raise ValueError(f"数据库表存在未处理的循环外键：{list(pending)}")
        for obj in ready:
            result.append(obj)
            del pending[obj["name"]]
    return result


def trigger_sql(source: str) -> str:
    match = re.match(
        r"CREATE TRIGGER\s+(?:IF NOT EXISTS\s+)?(\w+)\s+(INSTEAD OF|AFTER|BEFORE)\s+(.+?)\s+ON\s+(\w+)\s*(.*?)\bBEGIN\s*(.*)\bEND\s*;?\s*$",
        source,
        re.S | re.I,
    )
    if not match:
        raise ValueError("无法解析触发器定义")
    name, timing, event, table, when, body = match.groups()
    # SQLite 完整语句检测可以准确区分字面量内分号和语句边界。
    import sqlite3

    statements = []
    current = ""
    for char in body:
        current += char
        if char == ";" and sqlite3.complete_statement(current):
            statements.append(current.strip())
            current = ""
    if current.strip():
        raise ValueError(f"触发器 {name} 有未闭合的语句")
    translated = []
    for statement in statements:
        guard = re.match(
            r"SELECT\s+RAISE\(ABORT,\s*('(?:''|[^'])*')\)\s+WHERE\s+(.*);\s*$",
            statement,
            re.S | re.I,
        )
        if guard:
            message, condition = guard.groups()
            translated.append(
                f"IF {postgres_sql(condition)} THEN RAISE EXCEPTION {message} USING ERRCODE = '23514'; END IF;"
            )
        else:
            translated.append(postgres_sql(statement).rstrip(";") + ";")
    when = re.sub(r"^FOR\s+EACH\s+ROW\s*", "", when.strip(), flags=re.I)
    when_sql = ""
    if when.strip():
        condition = re.sub(r"^WHEN\s+", "", when.strip(), flags=re.I)
        when_sql = f" WHEN ({postgres_sql(condition)})"
    return_value = "OLD" if event.upper() == "DELETE" else "NEW"
    return (
        f"CREATE OR REPLACE FUNCTION ulab_trigger_{name}() RETURNS trigger LANGUAGE plpgsql AS $ulab$\nBEGIN\n"
        + "\n".join(translated)
        + f"\nRETURN {return_value};\nEND $ulab$;\n"
        + f"DROP TRIGGER IF EXISTS {name} ON {table};\n"
        + f"CREATE TRIGGER {name} {timing} {event} ON {table} FOR EACH ROW{when_sql} EXECUTE FUNCTION ulab_trigger_{name}();"
    )


def initialize(
    connection,
    scope: str,
    *,
    persist_workflow_definitions: bool = True,
) -> None:
    objects = objects_for_scope(
        scope,
        persist_workflow_definitions=persist_workflow_definitions,
    )
    # 初始化使用同一事务和跨进程锁；失败时不留下部分建表结果。
    with connection.transaction():
        connection.execute("SELECT pg_advisory_xact_lock(836271945)")
        connection.execute(
            files("unilabos.persistence").joinpath("functions.sql").read_text()
        )
        tables = _ordered_tables(objects)
        for obj in tables:
            ddl = postgres_sql(obj["sql"])
            ddl = re.sub(
                r"^CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ", ddl, flags=re.I
            )
            connection.execute(ddl)
            if obj["name"] in {
                "inventory_lot",
                "inventory_reservation",
                "processed_command",
                "edge_event_outbox",
                "station_handoff_outbox",
            }:
                # SQLite 隐式 rowid 用于同毫秒 FIFO；PG 使用内部序列，结果集不对外暴露。
                connection.execute(
                    f"ALTER TABLE {obj['name']} ADD COLUMN IF NOT EXISTS _ulab_rowid BIGINT GENERATED BY DEFAULT AS IDENTITY"
                )
        from unilabos.persistence.migrations import upgrade
        from unilabos.persistence.schema_contract import reconcile

        upgrade(
            connection,
            scope,
            persist_workflow_definitions=persist_workflow_definitions,
        )
        timescaledb_tables = set()
        if scope == "device":
            from unilabos.persistence.timescale import (
                ensure_device_property_history_hypertable,
            )

            if ensure_device_property_history_hypertable(connection):
                timescaledb_tables.add("device_property_history")
        reconcile(
            connection,
            tables,
            objects,
            ignore_primary_key_tables=timescaledb_tables,
        )
        for obj in objects:
            if obj["type"] == "index":
                ddl = postgres_sql(obj["sql"])
                ddl = re.sub(
                    r"^(CREATE (?:UNIQUE )?INDEX) ",
                    r"\1 IF NOT EXISTS ",
                    ddl,
                    flags=re.I,
                )
                connection.execute(ddl)
            elif obj["type"] == "view":
                connection.execute(
                    re.sub(
                        r"^CREATE VIEW ",
                        "CREATE OR REPLACE VIEW ",
                        postgres_sql(obj["sql"]),
                        flags=re.I,
                    )
                )
        for obj in objects:
            if obj["type"] == "trigger":
                connection.execute(trigger_sql(obj["sql"]))
        if scope == "inventory":
            from psycopg import sql

            from unilabos.persistence.content_views import CONTENT_VIEW_DEFAULTS

            # PG 视图原生支持列默认值：仅填充省略字段，不改写显式 NULL。
            # 每次初始化都对齐默认值，同时修复已有的合表数据库。
            for view, defaults in CONTENT_VIEW_DEFAULTS.items():
                for column, value in defaults.items():
                    connection.execute(
                        sql.SQL("ALTER VIEW {} ALTER COLUMN {} SET DEFAULT {}").format(
                            sql.Identifier(view), sql.Identifier(column), sql.Literal(value)
                        )
                    )



def initialize_workflow_runtime(connection) -> None:
    """初始化工作流运行时按需表。

    这些表原来由具体运行时对象首次使用时创建。PostgreSQL 的 Store 初始化
    不能依赖请求顺序，因此在工作流持久 Store 启动时统一幂等创建；定义 Store
    仍保持原有内存/文件来源，不会因此写入工作流定义。
    """

    from unilabos.app.scheduler.timing_store import ensure_timing_observation_schema
    from unilabos.workflow.human_commands import ensure_human_interaction_schema
    from unilabos.workflow.job_evidence import ensure_job_evidence_schema
    from unilabos.workflow.manual_confirmation import ensure_manual_confirmation_schema

    with connection.transaction():
        ensure_job_evidence_schema(connection)
        ensure_manual_confirmation_schema(connection)
        ensure_human_interaction_schema(connection)
        ensure_timing_observation_schema(connection)
