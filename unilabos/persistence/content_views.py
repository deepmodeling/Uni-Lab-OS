"""内容兼容视图的默认值合同，以及 SQLite 项目 SQL 入口的默认值补齐。"""

from __future__ import annotations

import re
import sqlite3
from functools import lru_cache
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlglot import exp

    from unilabos.persistence.content_upsert import ContentUpsert


# main 原物理表的默认值；显式 NULL 不使用默认值，仍交给数据库约束拒绝。
CONTENT_VIEW_DEFAULTS = {
    "sample": {"meta_data": "{}"},
    "current_substance": {"meta_data": "{}", "composition": "[]"},
    "reagent": {"meta_data": "{}", "revision": 1},
}

_COMMON_COLUMNS = "uuid create_time update_time deleted_at description meta_data material_uuid"
CONTENT_VIEW_COLUMNS = {
    "sample": tuple((_COMMON_COLUMNS + " code name sample_type source quantity quantity_unit collected_at expires_at").split()),
    "current_substance": tuple((_COMMON_COLUMNS + " name composition quantity quantity_unit physical_state revision observed_at").split()),
    "reagent": tuple((_COMMON_COLUMNS + " reagent_info_uuid concentration_value concentration_unit quantity quantity_unit revision physical_state density_g_per_ml density_source").split()),
}


# 只定位 INSERT 的列清单和数据源边界，不解析或重新生成调用方的表达式、参数。
# 字符串、引号标识符和注释各自作为整体，内部的关键字及分号不参与分割。
_TOKENS = re.compile(
    r"\ufeff|\s+|--[^\n]*(?:\n|$)|/\*.*?\*/|'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\""
    r"|[:@$][A-Za-z_0-9$\x80-\U0010ffff]+(?:::[A-Za-z_0-9$\x80-\U0010ffff]+)*(?:\([^)]*\))?|\?[0-9]*"
    r"|`(?:``|[^`])*`|\[[^\]]*\]|[A-Za-z_\x80-\U0010ffff][A-Za-z_0-9$\x80-\U0010ffff]*|.",
    re.S,
)


def _identifier(token: str) -> str:
    if token[0] in "\"'`":
        return token[1:-1].replace(token[0] * 2, token[0])
    if token[0] == "[":
        return token[1:-1]
    return token


@lru_cache(maxsize=512)
def _upsert_target(sql: str) -> tuple[str | None, str, str] | None:
    """先用轻量标记扫描，普通 SQL 和旧物理表不引入解析器运行依赖。"""

    if not re.search(r"\bCONFLICT\b", sql, re.I):
        return None
    tokens = [
        token[0] for token in _TOKENS.finditer(sql)
        if token[0] != "\ufeff" and not token[0].isspace() and not token[0].startswith(("--", "/*"))
    ]
    if not tokens or tokens[0].upper() not in {"INSERT", "REPLACE", "WITH"}:
        return None
    if not any(a.upper() == "ON" and b.upper() == "CONFLICT" for a, b in zip(tokens, tokens[1:])):
        return None
    depth = 0
    for position, word in enumerate(tokens):
        if depth == 0 and word.upper() in {"INSERT", "REPLACE"}:
            following = position + 1
            if following < len(tokens) and tokens[following].upper() == "OR":
                following += 2
            if following < len(tokens) and tokens[following].upper() == "INTO":
                break
        depth += (word == "(") - (word == ")")
    else:
        return None
    position = following + 1
    if position >= len(tokens):
        return None
    schema = None
    table = tokens[position]
    if position + 2 < len(tokens) and tokens[position + 1] == ".":
        schema, table = table, tokens[position + 2]
    key = _identifier(table).lower()
    return (schema, table, key) if key in CONTENT_VIEW_COLUMNS else None


def _sqlite_relation(
    connection: sqlite3.Connection, schema: str | None, table: str,
) -> tuple[str, str, str] | None:
    schemas = [_identifier(schema)] if schema is not None else ["temp"] + [
        row[1] for row in connection.execute("PRAGMA database_list") if row[1] != "temp"
    ]
    for namespace in schemas:
        quoted = '"' + namespace.replace('"', '""') + '"'
        row = connection.execute(
            f"SELECT type,sql FROM {quoted}.sqlite_master WHERE name=? COLLATE NOCASE "
            "AND type IN ('table','view')", (_identifier(table),),
        ).fetchone()
        if row is not None:
            return namespace, row[0], row[1]
    return None


def postgres_content_upsert_sql(connection: Any, sql: str) -> ContentUpsert | None:
    target = _upsert_target(sql)
    if target is None:
        return None
    schema, table, view = target

    def quoted_identifier(raw: str) -> str:
        name = _identifier(raw)
        if raw[0] not in "\"'`[":
            name = name.lower()
        return '"' + name.replace('"', '""') + '"'

    relation = quoted_identifier(table)
    if schema is not None:
        relation = quoted_identifier(schema) + "." + relation
    row = connection.execute(
        "SELECT n.nspname,pg_get_viewdef(c.oid) FROM pg_class c "
        "JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE c.oid=to_regclass(%s) AND c.relkind='v'", (relation,),
    ).fetchone()
    if row is None:
        return None
    from sqlglot import exp

    from unilabos.persistence.content_upsert import content_upsert_sql

    def column_names(source: exp.Table) -> set[str]:
        if not isinstance(source.this, exp.Identifier):
            if source.this.name.lower() != "json_each":
                return set()
            # 与现有 SQL 翻译器一致，从实际兼容函数读取 TABLE 输出列。
            return {
                field[0].lower() for field in connection.execute(
                    "SELECT argument.name FROM pg_proc p, "
                    "unnest(p.proargnames,p.proargmodes) AS argument(name,mode) "
                    "WHERE p.oid=to_regprocedure('public.ulab_json_each(text,text)') "
                    "AND argument.mode IN ('o','b','t')"
                )
            }
        relation = quoted_identifier(source.this.sql(dialect="sqlite"))
        if source.args.get("db") is not None:
            relation = quoted_identifier(source.args["db"].sql(dialect="sqlite")) + "." + relation
        return {
            field[0].lower() for field in connection.execute(
                "SELECT attname FROM pg_attribute WHERE attrelid=to_regclass(%s) "
                "AND attnum>0 AND NOT attisdropped", (relation,),
            )
        }

    return content_upsert_sql(sql, view, row[0], row[1], "postgres", column_names)


@lru_cache(maxsize=512)
def _insert_defaults(sql: str) -> tuple[str | None, str, str] | None:
    if not re.search(r"\b(?:INSERT|REPLACE)\b", sql, re.I):
        return None
    tokens = [
        token for token in _TOKENS.finditer(sql)
        if token[0] != "\ufeff" and not token[0].isspace() and not token[0].startswith(("--", "/*"))
    ]
    if not tokens or tokens[0][0].upper() not in {"INSERT", "REPLACE", "WITH"}:
        return None
    depth = 0
    for position, token in enumerate(tokens):
        word = token[0].upper()
        if (
            depth == 0 and word in {"INSERT", "REPLACE"}
            and position + 1 < len(tokens) and tokens[position + 1][0].upper() in {"INTO", "OR"}
        ):
            break
        depth += (word == "(") - (word == ")")
    else:
        return None

    position += 1
    if position < len(tokens) and tokens[position][0].upper() == "OR":
        position += 2
    if position >= len(tokens) or tokens[position][0].upper() != "INTO":
        return None
    position += 1
    if position >= len(tokens):
        return None
    schema = None
    table = _identifier(tokens[position][0])
    position += 1
    if position + 1 < len(tokens) and tokens[position][0] == ".":
        schema, table = table, _identifier(tokens[position + 1][0])
        position += 2
    table = table.lower()
    if table not in CONTENT_VIEW_DEFAULTS:
        return None
    if position < len(tokens) and tokens[position][0].upper() == "AS":
        position += 2
    if position >= len(tokens) or tokens[position][0] != "(":
        # 没有列清单的 INSERT 已按位置提供全部字段，不能把 NULL 当作省略。
        return None
    position += 1
    columns = set()
    while position < len(tokens) and tokens[position][0] != ")":
        columns.add(_identifier(tokens[position][0]).lower())
        position += 1
        if position < len(tokens) and tokens[position][0] == ",":
            position += 1
        elif position < len(tokens) and tokens[position][0] != ")":
            return None
    if position + 1 >= len(tokens):
        return None
    defaults = {
        column: value for column, value in CONTENT_VIEW_DEFAULTS[table].items()
        if column not in columns
    }
    if not defaults:
        return None
    closing = tokens[position].start()
    position += 1
    if tokens[position][0].upper() not in {"VALUES", "SELECT", "WITH"}:
        return None
    source_start = tokens[position].start()
    source_end = len(sql)
    depth = 0
    for index in range(position, len(tokens)):
        token = tokens[index]
        word = token[0].upper()
        conflict = word == "ON" and index + 1 < len(tokens) and tokens[index + 1][0].upper() == "CONFLICT"
        if depth == 0 and (word in {"RETURNING", ";"} or conflict):
            source_end = token.start()
            break
        depth += (word == "(") - (word == ")")
    names = ", ".join(f'"{column}"' for column in defaults)
    literals = ", ".join(
        str(value) if isinstance(value, int) else "'" + value.replace("'", "''") + "'"
        for value in defaults.values()
    )
    # 外包 SELECT 同时支持多行 VALUES、SELECT *、UNION 和 WITH，参数顺序保持不变。
    rewritten = (
        sql[:closing] + ", " + names + sql[closing:source_start]
        + f"SELECT *, {literals} FROM (" + sql[source_start:source_end]
        + "\n) AS _ulab_insert_defaults " + sql[source_end:]
    )
    return schema, table, rewritten


@lru_cache(maxsize=32)
def _is_content_view(sql: str) -> bool:
    """同名的用户视图不属于本合同；只匹配直接读取合表的项目兼容视图。"""

    tokens = [
        token[0] for token in _TOKENS.finditer(sql)
        if token[0] != "\ufeff" and not token[0].isspace() and not token[0].startswith(("--", "/*"))
    ]
    depth = 0
    for position, word in enumerate(tokens):
        if depth == 0 and word.upper() == "FROM" and position + 1 < len(tokens):
            return _identifier(tokens[position + 1]).lower() == "container_content"
        depth += (word == "(") - (word == ")")
    return False


def sqlite_content_upsert_sql(connection: sqlite3.Connection, sql: str) -> ContentUpsert | None:
    target = _upsert_target(sql)
    if target is not None:
        schema, table, view = target
        relation = _sqlite_relation(connection, schema, table)
        if relation is not None and relation[1] == "view":
            from sqlglot import exp

            from unilabos.persistence.content_upsert import content_upsert_sql

            def column_names(source: exp.Table) -> set[str]:
                database = source.args.get("db")
                if isinstance(source.this, exp.Identifier):
                    local = _sqlite_relation(connection, database.sql(dialect="sqlite") if database is not None else None,
                                             source.this.sql(dialect="sqlite"))
                    if local is None:
                        return set()
                    namespace, name = local[0], source.name
                else:
                    # 表值函数也是 SQLite 虚拟表，可直接读取其完整输出列。
                    namespace = _identifier(database.sql(dialect="sqlite")) if database is not None else "main"
                    name = source.this.name
                namespace = '"' + namespace.replace('"', '""') + '"'
                name = '"' + name.replace('"', '""') + '"'
                return {field[1].lower() for field in connection.execute(f"PRAGMA {namespace}.table_xinfo({name})")}

            compiled = content_upsert_sql(sql, view, relation[0], relation[2], "sqlite", column_names)
            if compiled is not None:
                return compiled
    return None


def content_insert_sql(connection: sqlite3.Connection, sql: str) -> str:
    """只为实际存在的兼容视图补默认值，旧物理表和其他 SQL 保持原样。"""

    plan = _insert_defaults(sql)
    if plan is None:
        return sql
    schema, table, rewritten = plan
    # 每次按 SQLite 的名称解析顺序查询，避免临时表遮蔽或迁移重建使缓存过期。
    schemas = [schema] if schema is not None else ["temp"] + [
        row[1] for row in connection.execute("PRAGMA database_list") if row[1] != "temp"
    ]
    for namespace in schemas:
        quoted = '"' + namespace.replace('"', '""') + '"'
        row = connection.execute(
            f"SELECT type,sql FROM {quoted}.sqlite_master WHERE name=? COLLATE NOCASE "
            "AND type IN ('table','view')", (table,),
        ).fetchone()
        if row is not None:
            if row[0] == "view" and _is_content_view(row[1]):
                # 只投影部分字段的同名用户视图不承接原表默认值合同。
                columns = {
                    field[1].lower()
                    for field in connection.execute(f'PRAGMA {quoted}.table_info("{table}")')
                }
                if CONTENT_VIEW_DEFAULTS[table].keys() <= columns:
                    return rewritten
            return sql
    return sql
