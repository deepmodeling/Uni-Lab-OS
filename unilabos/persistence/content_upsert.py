"""把内容兼容视图的 UPSERT 定向到底表，仍由数据库原子检查冲突和约束。"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from sqlite3 import OperationalError, ProgrammingError
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import OptimizeError, ParseError, UnsupportedError
from sqlglot.optimizer.scope import Scope, traverse_scope

from unilabos.persistence.content_views import _TOKENS, CONTENT_VIEW_COLUMNS, CONTENT_VIEW_DEFAULTS
from unilabos.persistence.sql_dialects import StoreSQLite

ColumnNames = Callable[[exp.Table], set[str]]


def _unwrapped(expression: exp.Expression) -> exp.Expression:
    while isinstance(expression, exp.Paren):
        expression = expression.this
    return expression


@lru_cache(maxsize=64)
def _matches_view(definition: str, view: str, namespace: str, dialect: str) -> bool:
    tree = sqlglot.parse_one(definition, read=dialect)
    query = tree.expression if isinstance(tree, exp.Create) else tree
    if not isinstance(query, exp.Select):
        return False
    if any(value for name, value in query.args.items() if name not in {"expressions", "from", "where"}):
        return False
    if tuple(column.name for column in query.expressions) != CONTENT_VIEW_COLUMNS[view]:
        return False
    if not all(isinstance(column, exp.Column) for column in query.expressions):
        return False
    source = query.args.get("from")
    source = source.this if source is not None else None
    if not isinstance(source, exp.Table) or source.name.lower() != "container_content":
        return False
    if source.db and source.db != namespace:
        return False
    if any(column.table and column.table.lower() != source.alias_or_name.lower() for column in query.expressions):
        return False
    where = query.args.get("where")
    condition = _unwrapped(where.this) if where is not None else None
    if not isinstance(condition, exp.EQ):
        return False
    column, value = _unwrapped(condition.this), _unwrapped(condition.expression)
    # PG 目录会把文本字面量显示为 ::text；其他 CAST 可能改变用户视图过滤语义。
    if isinstance(value, exp.Cast) and value.to.this == exp.DataType.Type.TEXT:
        value = _unwrapped(value.this)
    kind = "substance" if view == "current_substance" else view
    return (
        isinstance(column, exp.Column) and column.name.lower() == "kind"
        and (not column.table or column.table.lower() == source.alias_or_name.lower())
        and isinstance(value, exp.Literal) and value.is_string and value.this == kind
    )


@dataclass(frozen=True)
class ContentUpsert:
    sql: str
    slot_names: tuple[tuple[int, str | None], ...]
    slot_count: int
    binding_order: tuple[int, ...]

    def prepare(
        self, parameters: Sequence[Any] | Mapping[str, Any], *, postgresql: bool = False,
    ) -> tuple[str, Sequence[Any]]:
        if isinstance(parameters, Mapping):
            names = dict(self.slot_names)
            if len(names) != self.slot_count or any(name is None for name in names.values()):
                raise ProgrammingError("包含无名位置参数，不能使用字典绑定")
            try:
                parameters = tuple(parameters[names[index]] for index in range(1, self.slot_count + 1))
            except KeyError as error:
                raise ProgrammingError(f"缺少 SQL 参数：{error.args[0]}") from error
        if not postgresql:
            return self.sql, parameters
        if len(parameters) != self.slot_count:
            raise ProgrammingError(f"SQL 需要 {self.slot_count} 个参数，实际提供 {len(parameters)} 个")
        # PostgreSQL 使用按出现顺序的参数；保留原 SQLite 槽位身份后再排列。
        sql = "".join("?" if token[0].startswith("?") else token[0] for token in _TOKENS.finditer(self.sql))
        return sql, tuple(parameters[index - 1] for index in self.binding_order)


def _mask_parameters(sql: str) -> tuple[str, dict[str, int], tuple[tuple[int, str | None], ...], int]:
    parameters = {}
    slot_names = {}
    named_slots = {}
    slot_count = 0
    parts = []
    position = 0
    for token in _TOKENS.finditer(sql):
        if token[0][0] not in ":@$?":
            continue
        name = token[0]
        if name.startswith("?"):
            slot = int(name[1:]) if name[1:] else slot_count + 1
            if slot < 1:
                raise OperationalError("SQL 参数编号必须大于 0")
            if slot_names.get(slot) is None:
                # 后续 ?N 可以为之前匿名的同一槽位赋名，沿用 SQLite 字典绑定语义。
                slot_names[slot] = name[1:] or None
        else:
            slot = named_slots.setdefault(name, slot_count + 1)
            slot_names.setdefault(slot, name[1:])
        slot_count = max(slot_count, slot)
        marker = f"__ulab_content_parameter_{len(parameters)}"
        parameters[marker] = slot
        parts.extend((sql[position:token.start()], ":" + marker))
        position = token.end()
    parts.append(sql[position:])
    return "".join(parts), parameters, tuple(sorted(slot_names.items())), slot_count


def _restore_parameters(
    sql: str, parameters: dict[str, int], slot_names: tuple[tuple[int, str | None], ...], slot_count: int,
) -> ContentUpsert:
    parts, order = [], []
    for token in _TOKENS.finditer(sql):
        slot = parameters.get(token[0][1:]) if token[0].startswith(":") else None
        if slot is None:
            parts.append(token[0])
        else:
            order.append(slot)
            parts.append(f"?{slot}")
    return ContentUpsert("".join(parts), slot_names, slot_count, tuple(order))


def _check_columns(
    expression: exp.Expression | None, columns: set[str], alias_identifier: exp.Identifier,
    ctes: exp.With | None, column_names: ColumnNames,
) -> exp.Expression | None:
    if expression is None:
        return None
    expression = expression.copy()
    wrapper = exp.Select(expressions=[expression])
    if ctes is not None:
        wrapper.set("with", ctes.copy())
    # 同一子查询允许多个无名派生表；建作用域前命名，避免解析器空别名冲突。
    reserved = {alias_identifier.name.lower()}
    reserved.update(node.alias_or_name.lower() for node in wrapper.find_all(exp.Table, exp.Subquery))
    serial = 0
    for query in wrapper.find_all(exp.Select):
        source = query.args.get("from")
        sources = ([source.this] if source is not None else []) + [join.this for join in query.args.get("joins") or []]
        for source in sources:
            derived = isinstance(source, (exp.Subquery, exp.Values))
            function = isinstance(source, exp.Table) and not isinstance(source.this, exp.Identifier)
            if (derived or function) and not source.alias:
                while True:
                    name = f"_ulab_content_local_{serial}"
                    serial += 1
                    if name.lower() not in reserved:
                        break
                reserved.add(name.lower())
                source.set("alias", exp.TableAlias(this=exp.to_identifier(name, quoted=True)))
    scopes = {id(scope.expression): scope for scope in traverse_scope(wrapper)}
    source_cache = {}

    def source_columns(source: exp.Table | Scope, visited: set[int] | None = None) -> set[str]:
        if isinstance(source, exp.Table):
            key = source.sql()
            if key not in source_cache:
                source_cache[key] = set(column_names(source))
            return source_cache[key]
        if not isinstance(source, Scope):
            return set()
        if source.outer_columns:
            return {name.lower() for name in source.outer_columns}
        visited = set() if visited is None else visited
        if id(source) in visited:
            return set()
        visited = visited | {id(source)}
        if source.union_scopes:
            return source_columns(source.union_scopes[0], visited)
        names = set()
        for projection in source.expression.selects:
            if projection.is_star:
                qualifier = projection.table if isinstance(projection, exp.Column) else ""
                for name, (_, child) in source.selected_sources.items():
                    if not qualifier or qualifier.lower() == name.lower():
                        names.update(source_columns(child, visited))
            else:
                names.add(projection.output_name.lower())
        return names

    for column in expression.find_all(exp.Column):
        if column.is_star:
            if column.find_ancestor(exp.Select) is wrapper and column.table.lower() != alias_identifier.name.lower():
                raise OperationalError(f"兼容视图没有此来源：{column.table}")
            continue
        scope = scopes.get(id(column.find_ancestor(exp.Select)))
        local = False
        while scope is not None and scope.expression is not wrapper:
            matches = [
                (name, node) for name, (node, source) in scope.selected_sources.items()
                if (not column.table or name.lower() == column.table.lower())
                and column.name.lower() in source_columns(source)
            ]
            if matches:
                if len(matches) > 1:
                    raise OperationalError(f"子查询字段来源不明确：{column.name}")
                name, node = matches[0]
                if not column.table:
                    owner = node if isinstance(node, exp.Table) else node.parent
                    alias = owner.args.get("alias")
                    identifier = alias.this.copy() if alias is not None else None
                    if identifier is None and isinstance(node, exp.Table):
                        identifier = node.this.copy()
                    column.set("table", identifier)
                local = True
                break
            scope = scope.parent
        if local:
            continue
        if column.name.lower() not in columns:
            raise OperationalError(f"兼容视图没有此列：{column.name}")
        if column.table.lower() == "excluded":
            continue
        if column.table and column.table.lower() != alias_identifier.name.lower():
            raise OperationalError(f"兼容视图没有此来源：{column.table}")
        column.set("table", alias_identifier.copy())
        column.set("db", None)
        column.meta["content_target"] = True
    return expression


@lru_cache(maxsize=512)
def _parsed_insert(sql: str) -> exp.Expression:
    return sqlglot.parse_one(sql, read=StoreSQLite)


def content_upsert_sql(
    sql: str, view: str, namespace: str, definition: str, dialect: str, column_names: ColumnNames,
) -> ContentUpsert | None:
    try:
        if not _matches_view(definition, view, namespace, dialect):
            return None
        masked, parameters, slot_names, slot_count = _mask_parameters(sql)
        tree = _parsed_insert(masked).copy()
        if not isinstance(tree, exp.Insert) or tree.args.get("conflict") is None:
            return None
        if tree.args.get("alternative") == "REPLACE":
            raise OperationalError("内容 UPSERT 不支持 OR REPLACE，避免替换其他类型的记录")
        target = tree.this
        # 解析器会把 AS alias(cols) 读成表别名列，需要恢复 INSERT 列清单。
        if isinstance(target, exp.Table) and target.args.get("alias"):
            alias_columns = target.args["alias"].args.pop("columns", None)
            if alias_columns:
                target = exp.Schema(this=target, expressions=alias_columns)
                tree.set("this", target)
        if isinstance(target, exp.Schema):
            table = target.this
            columns = list(target.expressions)
        else:
            table = target
            columns = [exp.to_identifier(name, quoted=True) for name in CONTENT_VIEW_COLUMNS[view]]
            target = exp.Schema(this=table, expressions=columns)
            tree.set("this", target)
        allowed = set(CONTENT_VIEW_COLUMNS[view])
        names = {column.name.lower() for column in columns}
        if not names <= allowed:
            raise OperationalError(f"兼容视图没有此列：{sorted(names - allowed)[0]}")
        alias = table.args.get("alias")
        alias_identifier = alias.this.copy() if alias is not None else table.this.copy()
        conflict = tree.args["conflict"]
        ctes = tree.args.get("with")
        for assignment in conflict.expressions:
            for column in assignment.this.find_all(exp.Column):
                if column.name.lower() not in allowed:
                    raise OperationalError(f"兼容视图没有此列：{column.name}")
            assignment.set("expression", _check_columns(assignment.expression, allowed, alias_identifier, ctes, column_names))
        conflict.set("where", _check_columns(conflict.args.get("where"), allowed, alias_identifier, ctes, column_names))
        returning = tree.args.get("returning")
        returning = _check_columns(returning, allowed, alias_identifier, ctes, column_names)
        tree.set("returning", returning)

        kind = "substance" if view == "current_substance" else view
        defaults = {name: value for name, value in CONTENT_VIEW_DEFAULTS[view].items() if name not in names}
        defaults["kind"] = kind
        target.set("expressions", columns + [exp.to_identifier(name, quoted=True) for name in defaults])
        source = tree.expression
        if source is None:
            raise OperationalError("内容 UPSERT 必须提供 INSERT 数据源")
        def append_defaults(query: exp.Expression) -> None:
            query = _unwrapped(query)
            if isinstance(query, exp.Subquery):
                append_defaults(query.this)
            elif isinstance(query, exp.Values):
                for row in query.expressions:
                    row.set("expressions", row.expressions + [exp.convert(value) for value in defaults.values()])
            elif isinstance(query, exp.SetOperation):
                append_defaults(query.this)
                append_defaults(query.expression)
            elif isinstance(query, exp.Select):
                query.set("expressions", query.expressions + [exp.convert(value) for value in defaults.values()])
                if query.args.get("where") is None:
                    query.set("where", exp.Where(this=exp.true()))
            else:
                raise OperationalError("内容 UPSERT 的 INSERT 数据源不受支持")

        # 直接扩展数据源，保留 PG 对 NULL 参数的目标列类型推断。
        append_defaults(source)
        if dialect == "postgres":
            for where in tree.find_all(exp.Where):
                if isinstance(where.this, exp.Literal) and not where.this.is_string:
                    where.set("this", exp.Boolean(this=float(where.this.this) != 0))
        table.set("this", exp.to_identifier("container_content", quoted=True))
        table.set("db", exp.to_identifier(namespace, quoted=True))
        table.set("alias", exp.TableAlias(this=alias_identifier))

        keys = conflict.args.get("conflict_keys") or []
        predicate = conflict.args.get("index_predicate")
        if (
            view == "sample" and len(keys) == 1
            and predicate is not None
        ):
            key = _unwrapped(keys[0])
            if isinstance(key, exp.Collate) and key.expression.name.upper() == "BINARY":
                key = _unwrapped(key.this)
            key_column = _unwrapped(key.this) if isinstance(key, exp.Lower) else None
            condition = _unwrapped(predicate.this)
            if (isinstance(key_column, exp.Column) and key_column.name.lower() == "code"
                    and isinstance(condition, exp.Is) and isinstance(condition.this, exp.Column)
                    and condition.this.name.lower() == "deleted_at" and isinstance(condition.expression, exp.Null)):
                # 顺序与真实部分索引一致，SQLite 不把交换 AND 的谓词视为同一索引。
                conflict.set("index_predicate", exp.Where(this=exp.and_(
                    "deleted_at IS NULL", "kind = 'sample'",
                )))
                conflict.set("conflict_keys", [exp.Lower(this=exp.column("code"))])
        if conflict.expressions:
            existing_kind = exp.Column(this=exp.to_identifier("kind"), table=alias_identifier.copy())
            # 命中其他类型的全局唯一键时使 NOT NULL 失败，整条语句原子回滚。
            guard = exp.Case().when(existing_kind.eq(exp.Literal.string(kind)), exp.Literal.string(kind)).else_(exp.Null())
            conflict.append("expressions", exp.column("kind").eq(guard))
        if returning is not None:
            projection = []
            for expression in returning.expressions:
                if (isinstance(expression, exp.Star) or isinstance(expression, exp.Column)
                        and expression.is_star and expression.table.lower() == alias_identifier.name.lower()):
                    projection.extend(exp.column(name, quoted=True) for name in CONTENT_VIEW_COLUMNS[view])
                else:
                    projection.append(expression)
            returning.set("expressions", projection)
            if dialect == "sqlite":
                for column in returning.find_all(exp.Column):
                    if column.meta.get("content_target"):
                        column.set("table", exp.to_identifier("container_content", quoted=True))
        return _restore_parameters(tree.sql(dialect=StoreSQLite), parameters, slot_names, slot_count)
    except (OptimizeError, ParseError, UnsupportedError) as error:
        raise OperationalError(f"内容 UPSERT 无法解析：{error}") from error
