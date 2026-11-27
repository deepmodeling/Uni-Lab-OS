"""将已有 Store 使用的 SQL 转为 PostgreSQL SQL，差异集中在此处。"""

from __future__ import annotations

import re
from functools import lru_cache

import sqlglot
from sqlglot import exp

from unilabos.persistence.sql_dialects import StorePostgres, StoreSQLite

# 只替换 SQL 代码中的标记，字符串、标识符及注释保持原样。
_TOKEN = re.compile(r"('(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|--[^\n]*|/\*.*?\*/)", re.S)


def code_sub(sql: str, pattern: str, replacement: str) -> str:
    return "".join(
        part if index % 2 else re.sub(pattern, replacement, part, flags=re.I)
        for index, part in enumerate(_TOKEN.split(sql))
    )


def _function(name: str, *args: exp.Expression) -> exp.Expression:
    return exp.Anonymous(this=name, expressions=list(args))


def _parents(node: exp.Expression):
    while node.parent is not None:
        node = node.parent
        yield node


def _json_value(node: exp.Expression) -> exp.Expression:
    """JSON 构造器内部保留数字、布尔、对象等 JSON 类型。"""
    if isinstance(node, exp.Anonymous) and node.name.lower() == "ulab_json_extract":
        return _function("ulab_json_scalar", *node.expressions)
    return node


def _transform(node: exp.Expression) -> exp.Expression:
    # SQLite 的 ``expr IS NOT 1`` 对 NULL 返回真；PostgreSQL 不接受
    # ``text IS NOT 1``，需要使用同样保留 NULL 语义的 IS DISTINCT FROM。
    if (
        isinstance(node, exp.Not)
        and isinstance(node.this, exp.Is)
        and isinstance(node.this.this, exp.Anonymous)
        and node.this.this.name.lower() == "ulab_json_extract"
        and isinstance(node.this.expression, exp.Literal)
        and not node.this.expression.is_string
        and node.this.expression.this == "1"
    ):
        return exp.NullSafeNEQ(
            this=node.this.this,
            expression=exp.Literal.string("1"),
        )
    if (
        isinstance(node, exp.Cast)
        and node.to.this in {exp.DataType.Type.INT, exp.DataType.Type.BIGINT}
        and isinstance(node.this, (exp.Mul, exp.Div, exp.Sub, exp.Add))
    ):
        node.set("this", _function("trunc", node.this))
    if isinstance(node, exp.Is) and isinstance(node.this, exp.Placeholder):
        node.set("this", exp.Cast(this=node.this.copy(), to=exp.DataType.build("TEXT")))
    if (
        isinstance(node, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE))
        and isinstance(node.this, exp.Anonymous)
        and node.this.name.lower() == "ulab_json_extract"
    ):
        if isinstance(node.expression, exp.Literal) and not node.expression.is_string:
            node.set("expression", exp.Literal.string(node.expression.this))
        elif isinstance(node.expression, exp.Placeholder):
            node.set(
                "expression",
                exp.Cast(this=node.expression.copy(), to=exp.DataType.build("TEXT")),
            )
    if isinstance(node, exp.Column) and node.name.lower() == "rowid":
        node.set("this", exp.to_identifier("_ulab_rowid"))
    if isinstance(node, exp.Limit) and isinstance(node.expression, exp.Neg):
        node.set("expression", exp.Null())
    if isinstance(node, exp.Ordered) and isinstance(node.parent, exp.PrimaryKey):
        return node.this
    if (
        isinstance(node, (exp.EQ, exp.NEQ))
        and isinstance(node.this, exp.Anonymous)
        and node.this.name.lower() == "ulab_json_valid"
        and isinstance(node.expression, exp.Literal)
    ):
        node.set("expression", exp.Boolean(this=node.expression.this == "1"))
    if (
        isinstance(node, exp.DefaultColumnConstraint)
        and "RANDOMBLOB" in node.sql().upper()
    ):
        return exp.DefaultColumnConstraint(
            this=exp.Cast(
                this=_function("gen_random_uuid"), to=exp.DataType.build("TEXT")
            )
        )
    if isinstance(node, exp.DataType):
        if node.this in {exp.DataType.Type.DATETIME, exp.DataType.Type.TIMESTAMP}:
            return exp.DataType.build("TEXT")
        if node.this in {exp.DataType.Type.INT, exp.DataType.Type.BIGINT}:
            return exp.DataType.build("BIGINT")
        if node.this in {exp.DataType.Type.FLOAT, exp.DataType.Type.DOUBLE}:
            return exp.DataType.build("DOUBLE PRECISION")
    if isinstance(node, exp.CurrentTimestamp):
        return sqlglot.parse_one(
            "to_char(CURRENT_TIMESTAMP AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')",
            read="postgres",
        )
    if isinstance(node, exp.Glob):
        return _function("ulab_glob", node.this, node.expression)
    if isinstance(node, exp.Anonymous):
        name = node.name.lower()
        args = node.expressions
        if name == "json":
            return exp.Cast(this=args[0], to=exp.DataType.build("JSONB"))
        if name == "ulab_json_set":
            result = args[0]
            for index in range(1, len(args), 2):
                value = args[index + 1]
                if isinstance(value, exp.Anonymous) and value.name.lower() == "json":
                    value = exp.Cast(
                        this=value.expressions[0], to=exp.DataType.build("JSONB")
                    )
                else:
                    if isinstance(value, exp.Placeholder):
                        value = exp.Cast(this=value, to=exp.DataType.build("TEXT"))
                    value = _function("to_jsonb", value)
                result = _function("ulab_json_set", result, args[index], value)
            return result
        if name == "ulab_json_object":
            values = [
                arg if index % 2 == 0 else _json_value(arg)
                for index, arg in enumerate(args)
            ]
            return _function("jsonb_build_object", *values)
        if name == "ulab_json_group_array":
            return exp.Coalesce(
                this=_function("jsonb_agg", _json_value(args[0])),
                expressions=[
                    exp.Cast(
                        this=exp.Literal.string("[]"), to=exp.DataType.build("JSONB")
                    )
                ],
            )
        if name == "ulab_json_array":
            return _function("jsonb_build_array", *[_json_value(arg) for arg in args])
    return node


@lru_cache(maxsize=4096)
def _postgres_sql(sql: str) -> str:
    # 两种数据库的保存点语法相同；通用解析器会把 SAVEPOINT name 错读成别名。
    if re.fullmatch(
        r"(?:SAVEPOINT|RELEASE(?:\s+SAVEPOINT)?|ROLLBACK\s+TO(?:\s+SAVEPOINT)?)"
        r'\s+(?:[A-Za-z_][A-Za-z_0-9]*|"(?:""|[^"])+")\s*;?',
        sql.strip(),
        flags=re.I,
    ):
        return sql.strip()
    # 先保留 SQLite JSON 函数的参数形式，防止通用转译器改变返回值语义。
    sql = code_sub(
        sql,
        r"\b(json_extract|json_object|json_array|json_group_array|json_each|json_valid|json_type|json_set|strftime|julianday)\s*\(",
        r"ulab_\1(",
    )
    sql = code_sub(sql, r"\bIS\s+NOT\s+(?=(?:NEW|OLD)\.)", "IS DISTINCT FROM ")
    tree = sqlglot.parse_one(sql, read=StoreSQLite)
    if isinstance(tree, exp.Insert) and tree.args.get("alternative"):
        alternative = tree.args.pop("alternative")
        if alternative == "IGNORE":
            if not tree.args.get("conflict"):
                tree.set("conflict", exp.OnConflict(action=exp.Var(this="DO NOTHING")))
        else:
            raise ValueError("INSERT OR REPLACE 必须显式改为带冲突键的 UPSERT")
    if isinstance(tree, exp.Insert) and tree.args.get("conflict"):
        table = tree.this.this if isinstance(tree.this, exp.Schema) else tree.this
        conflict = tree.args["conflict"]
        sources = [assignment.expression for assignment in conflict.expressions]
        if conflict.args.get("where"):
            sources.append(conflict.args["where"])
        for source in sources:
            for column in source.find_all(exp.Column):
                if not column.table:
                    column.set("table", exp.to_identifier(table.alias_or_name))
    # 先处理子表达式，嵌套 JSON 对象和 JSON 修改不能漏过内部函数。
    for node in reversed(list(tree.walk())):
        replacement = _transform(node)
        if replacement is not node:
            if node.parent is None:
                tree = replacement
            else:
                node.replace(replacement)
    # 最外层 JSON 构造结果仍按原有 TEXT 返回；嵌套对象保持 JSON 类型。
    for node in list(tree.find_all(exp.Anonymous)):
        if node.name.lower() not in {
            "jsonb_build_object",
            "jsonb_build_array",
            "jsonb_agg",
        }:
            continue
        if any(
            isinstance(parent, exp.Anonymous)
            and parent.name.lower()
            in {"jsonb_build_object", "jsonb_build_array", "jsonb_agg"}
            for parent in _parents(node)
        ):
            continue
        # 聚合的 COALESCE 必须整体转换。
        target = node.parent if isinstance(node.parent, exp.Coalesce) else node
        target.replace(exp.Cast(this=target.copy(), to=exp.DataType.build("TEXT")))
    return tree.sql(dialect=StorePostgres)


def postgres_sql(sql: str) -> str:
    from sqlite3 import OperationalError

    from sqlglot.errors import ParseError, UnsupportedError

    try:
        return _postgres_sql(sql)
    except (ParseError, UnsupportedError) as error:
        raise OperationalError(f"SQL 无法转换为 PostgreSQL：{error}") from error
