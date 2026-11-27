"""补齐现有 SQL 解析器对表达式冲突键和部分唯一索引的支持。"""

from sqlglot import exp
from sqlglot.dialects.postgres import Postgres
from sqlglot.dialects.sqlite import SQLite
from sqlglot.tokens import TokenType


class OnConflict(exp.OnConflict):
    arg_types = {**exp.OnConflict.arg_types, "index_predicate": False}


def _on_conflict_sql(generator: SQLite.Generator | Postgres.Generator, expression: OnConflict) -> str:
    keys = generator.expressions(expression, key="conflict_keys", flat=True)
    target = f"({keys})" if keys else ""
    constraint = generator.sql(expression, "constraint")
    if constraint:
        target = f"ON CONSTRAINT {constraint}"
    predicate = generator.sql(expression, "index_predicate")
    assignments = generator.expressions(expression, flat=True)
    assignments = f" SET {assignments}" if assignments else ""
    return (
        f"ON CONFLICT {target}{predicate} {generator.sql(expression, 'action')}"
        f"{assignments}{generator.sql(expression, 'where')}"
    )


class StoreSQLite(SQLite):
    class Parser(SQLite.Parser):
        def _parse_on_conflict(self) -> exp.OnConflict | None:
            if not self._match_text_seq("ON", "CONFLICT", advance=False):
                return super()._parse_on_conflict()
            self._match_text_seq("ON", "CONFLICT")
            keys = None
            constraint = None
            if self._match_text_seq("ON", "CONSTRAINT"):
                constraint = self._parse_id_var()
            elif self._match(TokenType.L_PAREN):
                keys = self._parse_csv(self._parse_ordered)
                keys = [key.this if isinstance(key, exp.Ordered) else key for key in keys]
                self._match_r_paren()
            predicate = self._parse_where()
            action = self._parse_var_from_options(self.CONFLICT_ACTIONS)
            assignments = None
            if self._prev.token_type == TokenType.UPDATE:
                self._match(TokenType.SET)
                assignments = self._parse_csv(self._parse_equality)
            return self.expression(
                OnConflict, conflict_keys=keys, constraint=constraint,
                index_predicate=predicate, action=action, expressions=assignments,
                where=self._parse_where(),
            )

    class Generator(SQLite.Generator):
        def onconflict_sql(self, expression: exp.OnConflict) -> str:
            if isinstance(expression, OnConflict):
                return _on_conflict_sql(self, expression)
            return super().onconflict_sql(expression)


class StorePostgres(Postgres):
    class Generator(Postgres.Generator):
        def onconflict_sql(self, expression: exp.OnConflict) -> str:
            if isinstance(expression, OnConflict):
                return _on_conflict_sql(self, expression)
            return super().onconflict_sql(expression)
