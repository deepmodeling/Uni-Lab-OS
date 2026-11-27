"""两种数据库连接实现。Store 保持原来的事务边界和 SQL 调用方式。"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from unilabos.persistence.config import DatabaseSettings, configured_settings

# 对外异常保持现有 Store 的异常捕获行为，驱动异常不穿透业务层。
IntegrityError = sqlite3.IntegrityError
OperationalError = sqlite3.OperationalError
DatabaseError = sqlite3.DatabaseError


def _script_statements(script: str) -> Iterator[str]:
    statement = ""
    for char in script:
        statement += char
        if char == ";" and sqlite3.complete_statement(statement):
            yield statement
            statement = ""
    if statement.strip():
        yield statement


def _normalize_value(value: Any) -> Any:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, Decimal):
        return float(value) if value.as_tuple().exponent < 0 else int(value)
    return value


class Row:
    """兼容既有按列名、列号及 dict(row) 读取的结果。"""

    def __init__(self, names: Sequence[str], values: Sequence[Any]):
        self._names = tuple(names)
        self._values = tuple(_normalize_value(value) for value in values)

    def __getitem__(self, key: int | slice | str) -> Any:
        if isinstance(key, str):
            try:
                key = self._names.index(key)
            except ValueError:
                matches = [
                    index
                    for index, name in enumerate(self._names)
                    if name.lower() == key.lower()
                ]
                if not matches:
                    raise IndexError(f"没有此列：{key}") from None
                key = matches[0]
        return self._values[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def keys(self):
        return self._names


class Cursor:
    def __init__(
        self,
        rows: Iterable[Sequence[Any]] = (),
        names: Sequence[str] = (),
        *,
        row_factory=None,
        rowcount: int = -1,
        lastrowid=None,
    ):
        self._rows = iter(
            (
                Row(names, row)
                if row_factory
                else tuple(_normalize_value(value) for value in row)
            )
            for row in rows
        )
        self.rowcount = rowcount
        self.lastrowid = lastrowid
        self.description = [
            (name, None, None, None, None, None, None) for name in names
        ] or None

    def fetchone(self):
        return next(self._rows, None)

    def fetchall(self):
        return list(self._rows)

    def fetchmany(self, size: int = 1):
        result = []
        for _ in range(size):
            row = self.fetchone()
            if row is None:
                break
            result.append(row)
        return result

    def __iter__(self):
        return self._rows

    def close(self):
        self._rows = iter(())


class Database:
    _connection: Any
    row_factory = None

    def execute(self, sql: str, parameters: Sequence[Any] = ()) -> Any:
        return self._connection.execute(sql, parameters)

    def executemany(self, sql: str, parameters: Iterable[Sequence[Any]]) -> Any:
        return self._connection.executemany(sql, parameters)

    def executescript(self, script: str) -> Any:
        return self._connection.executescript(script)

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def close(self) -> None:
        self._connection.close()

    def query_one(self, sql: str, parameters: Sequence[Any] = ()):
        return self.execute(sql, parameters).fetchone()

    def query_all(self, sql: str, parameters: Sequence[Any] = ()):
        return self.execute(sql, parameters).fetchall()

    def query_many(
        self, queries: Sequence[tuple[str, Sequence[Any]]]
    ) -> list[list[Any]]:
        """读取互不依赖的多个结果集；不提交或改变调用方的事务边界。"""
        return [self.query_all(sql, parameters) for sql, parameters in queries]

    def insert_returning(
        self, sql: str, parameters: Sequence[Any] = (), column: str = "id"
    ):
        if not column.isidentifier():
            raise ValueError("返回列名称不合法")
        return self.execute(
            f'{sql.rstrip(";")} RETURNING {column}', parameters
        ).fetchone()[0]

    @contextmanager
    def transaction(self) -> Iterator[Database]:
        self.begin()
        try:
            yield self
        except BaseException:
            self.rollback()
            raise
        else:
            self.commit()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if exc_type is None:
            self.commit()
        else:
            self.rollback()


class SQLiteDatabase(Database):
    def __init__(
        self, path: str, *, create_parent: bool = False, private: bool = False
    ):
        if path != ":memory:" and create_parent:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._schema_migration_active = False
        if private and path != ":memory:":
            try:
                Path(path).chmod(0o600)
            except OSError:
                pass

    @property
    def row_factory(self):
        return self._connection.row_factory

    @row_factory.setter
    def row_factory(self, value):
        self._connection.row_factory = value

    def __getattr__(self, name: str):
        return getattr(self._connection, name)

    def initialize(self, sqlite_initializer, *, persist_workflow_definitions: bool = True):
        try:
            sqlite_initializer()
        except BaseException:
            self.close()
            raise

    def begin(self):
        self._connection.execute("BEGIN IMMEDIATE")

    def execute(self, sql: str, parameters: Sequence[Any] = ()):
        from unilabos.persistence.content_views import content_insert_sql, sqlite_content_upsert_sql

        upsert = sqlite_content_upsert_sql(self._connection, sql)
        if upsert is not None:
            sql, parameters = upsert.prepare(parameters)
        else:
            sql = content_insert_sql(self._connection, sql)
        return self._connection.execute(sql, parameters)

    def executemany(self, sql: str, parameters: Iterable[Sequence[Any]]):
        from unilabos.persistence.content_views import content_insert_sql, sqlite_content_upsert_sql

        upsert = sqlite_content_upsert_sql(self._connection, sql)
        if upsert is not None:
            return self._connection.executemany(upsert.sql, (upsert.prepare(row)[1] for row in parameters))
        return self._connection.executemany(content_insert_sql(self._connection, sql), parameters)

    def executescript(self, script: str):
        # 原生 executescript 会先提交已有事务；迁移内逐条执行，保持事务边界。
        # 使用 SQLite 自身识别完整语句，保留触发器及字符串中的分号。
        isolation_level = self._connection.isolation_level
        if not self._schema_migration_active:
            # 普通脚本沿用原生语义：先提交已有事务，脚本内由 BEGIN/COMMIT 自行管理。
            self._connection.commit()
            self._connection.isolation_level = None
        try:
            for statement in _script_statements(script):
                # 与原生脚本一致，SELECT/RETURNING 也必须执行到最后一行。
                for _ in self.execute(statement):
                    pass
        finally:
            if not self._schema_migration_active:
                self._connection.isolation_level = isolation_level
        if not self._schema_migration_active:
            return self._connection.cursor()

    @contextmanager
    def schema_migration(self) -> Iterator[SQLiteDatabase]:
        """原子执行 SQLite 结构迁移，提交前检查外键并恢复连接设置。"""

        if self.in_transaction:
            raise OperationalError("不能在现有事务内开启 SQLite 结构迁移")
        foreign_keys = self.execute("PRAGMA foreign_keys").fetchone()[0]
        legacy_alter_table = self.execute("PRAGMA legacy_alter_table").fetchone()[0]
        # SQLite 重建被引用表需要在事务前关闭外键，并禁止 RENAME 改写旧引用。
        self.execute("PRAGMA foreign_keys = OFF")
        self.execute("PRAGMA legacy_alter_table = ON")
        try:
            self.begin()
            self._schema_migration_active = True
            yield self
            if self.execute("PRAGMA foreign_key_check").fetchall():
                raise IntegrityError("SQLite 结构迁移后外键检查失败")
            self.commit()
        except BaseException:
            self.rollback()
            raise
        finally:
            self._schema_migration_active = False
            self.execute(f"PRAGMA legacy_alter_table = {legacy_alter_table}")
            self.execute(f"PRAGMA foreign_keys = {foreign_keys}")

    def table_exists(self, name: str) -> bool:
        return (
            self.query_one(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
            )
            is not None
        )

    def index_exists(self, name: str) -> bool:
        return (
            self.query_one(
                "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (name,)
            )
            is not None
        )

    def table_names(self) -> set[str]:
        return {
            row[0]
            for row in self.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }


class PostgreSQLDatabase(Database):
    def __init__(self, settings: DatabaseSettings, scope: str):
        try:
            import psycopg
            import sqlglot  # noqa: F401
        except ImportError:
            raise RuntimeError(
                'PostgreSQL 模式需要安装依赖：pip install -e ".[postgresql]"'
            ) from None
        try:
            self._connection = psycopg.connect(
                settings.database_url, autocommit=True, connect_timeout=10
            )
        except psycopg.Error:
            raise OperationalError(
                "无法连接 PostgreSQL，请检查数据库地址、凭据和服务状态"
            ) from None
        # 工作流 SQL 种类较多，避免默认 100 条缓存反复淘汰常用查询。
        self._connection.prepared_max = 1024
        self._connection.execute("SET search_path = public")
        self._scope = scope
        # 原同一 SQLite 文件中的 Store 在 PG 中仍共享写入序列。
        lock_scope = "workflow" if scope == "history" else scope
        self._lock_key = int.from_bytes(
            hashlib.sha256(lock_scope.encode()).digest()[:8], "big", signed=True
        )
        self._identities: dict[str, str] = {}
        self.row_factory = sqlite3.Row

    @property
    def in_transaction(self) -> bool:
        from psycopg.pq import TransactionStatus

        return self._connection.info.transaction_status != TransactionStatus.IDLE

    def initialize(self, sqlite_initializer, *, persist_workflow_definitions: bool = True):
        from unilabos.persistence.schema import initialize, initialize_workflow_runtime

        try:
            initialize(
                self._connection,
                self._scope,
                persist_workflow_definitions=persist_workflow_definitions,
            )
            if self._scope == "workflow":
                initialize_workflow_runtime(self)
            rows = self._connection.execute(
                "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema=current_schema() AND is_identity='YES'"
            ).fetchall()
            self._identities = dict(rows)
        except BaseException:
            self.close()
            raise

    def begin(self):
        if self.in_transaction:
            raise OperationalError("不能在现有事务内重复开启事务")
        with self._connection.pipeline():
            self._connection.execute("BEGIN")
            self._connection.execute(
                "SELECT pg_advisory_xact_lock(%s)", (self._lock_key,)
            )

    def commit(self):
        self._connection.commit()

    def rollback(self):
        self._connection.rollback()

    def close(self):
        self._connection.close()

    def table_exists(self, name: str) -> bool:
        return (
            self._connection.execute(
                "SELECT 1 FROM information_schema.tables WHERE table_schema=current_schema() AND table_type='BASE TABLE' AND table_name=%s",
                (name,),
            ).fetchone()
            is not None
        )

    def index_exists(self, name: str) -> bool:
        return (
            self._connection.execute(
                "SELECT 1 FROM pg_indexes WHERE schemaname=current_schema() AND indexname=%s",
                (name,),
            ).fetchone()
            is not None
        )

    def _sync_identity_sequence(self, table: str, column: str) -> None:
        """让 PostgreSQL identity 序列追上显式写入的 SQLite 自增主键。"""

        # SQLite 迁移和回归工具会显式保留历史 INTEGER PRIMARY KEY。PostgreSQL
        # identity 不会因 BY DEFAULT 的显式值自动前进；同步到当前 MAX 后，下一次
        # 无主键 INSERT 才能保持 SQLite 的兼容行为。表名和列名来自初始化目录，
        # 不接受外部输入，直接引用是安全的。
        quoted_table = '"' + table.replace('"', '""') + '"'
        quoted_column = '"' + column.replace('"', '""') + '"'
        self._connection.execute(
            f"""
            SELECT setval(
                pg_get_serial_sequence(%s, %s),
                COALESCE((SELECT MAX({quoted_column}) FROM {quoted_table}), 1),
                COALESCE((SELECT MAX({quoted_column}) FROM {quoted_table}), 0) > 0
            )
            """,
            (table, column),
        )

    def table_names(self) -> set[str]:
        return {
            row[0]
            for row in self._connection.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname=current_schema()"
            )
        }

    def _pragma(self, sql: str):
        import re

        match = re.fullmatch(
            r"PRAGMA\s+table_info\s*\(\s*[\"\']?(\w+)[\"\']?\s*\)\s*;?", sql, re.I
        )
        if match:
            names = ("cid", "name", "type", "notnull", "dflt_value", "pk")
            rows = self._connection.execute(
                """SELECT a.attnum-1,a.attname,format_type(a.atttypid,a.atttypmod),a.attnotnull::integer,
                pg_get_expr(d.adbin,d.adrelid),coalesce(array_position(i.indkey::smallint[],a.attnum)+1,0)
                FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
                JOIN pg_namespace n ON n.oid=c.relnamespace
                LEFT JOIN pg_attrdef d ON d.adrelid=c.oid AND d.adnum=a.attnum
                LEFT JOIN pg_index i ON i.indrelid=c.oid AND i.indisprimary
                WHERE n.nspname=current_schema() AND c.relname=%s AND a.attnum>0 AND NOT a.attisdropped ORDER BY a.attnum""",
                (match.group(1),),
            ).fetchall()
            return Cursor(rows, names, row_factory=self.row_factory)
        # 以下参数仅对 SQLite 文件生效；PG 不借用它们存储结构版本。
        if re.match(
            r"PRAGMA\s+(journal_mode|synchronous|foreign_keys|busy_timeout)\b",
            sql,
            re.I,
        ):
            return Cursor()
        raise OperationalError(f"PostgreSQL 不支持此 SQLite 元数据操作：{sql}")

    def execute(self, sql: str, parameters: Sequence[Any] = ()) -> Cursor:
        import re

        import psycopg

        from unilabos.persistence.content_views import postgres_content_upsert_sql
        from unilabos.persistence.sql import code_sub, postgres_sql

        stripped = sql.strip()
        if stripped.upper().startswith("PRAGMA "):
            return self._pragma(stripped)
        if stripped.upper().rstrip(";") in {
            "BEGIN",
            "BEGIN IMMEDIATE",
            "BEGIN TRANSACTION",
        }:
            self.begin()
            return Cursor()
        if stripped.upper().rstrip(";") in {"COMMIT", "END"}:
            self.commit()
            return Cursor()
        if stripped.upper().rstrip(";") == "ROLLBACK":
            self.rollback()
            return Cursor()
        content_upsert = postgres_content_upsert_sql(self._connection, sql)
        if content_upsert is not None:
            sql, parameters = content_upsert.prepare(parameters, postgresql=True)
        compiled = postgres_sql(sql)
        mutation = content_upsert is not None or bool(
            re.match(
                r"\s*(INSERT|UPDATE|DELETE|REPLACE|ALTER|CREATE|DROP)\b", compiled, re.I
            )
        )
        if mutation and not self.in_transaction:
            self.begin()
        identity = None
        insert = re.match(r"INSERT INTO\s+[\"]?(\w+)", compiled, re.I)
        if insert and "RETURNING" not in compiled.upper():
            identity = self._identities.get(insert.group(1))
            if identity:
                compiled = compiled.rstrip(";") + " RETURNING " + identity
        if parameters:
            compiled = code_sub(compiled.replace("%", "%%"), r"%%s", "%s")
        savepoint = self.in_transaction and mutation
        try:
            if savepoint:
                # Savepoint recovery must run as ordinary synchronous statements.
                # A failed statement leaves a psycopg pipeline in BAD state and the
                # subsequent rollback/release can invalidate its prepared statement
                # cache, so a caught constraint error poisons the surrounding
                # transaction.  Savepoints are already scoped to this connection;
                # avoiding the pipeline here preserves the SQLite-compatible
                # "catch, continue, commit" contract.
                self._connection.execute("SAVEPOINT ulab_statement")
                result = self._connection.execute(compiled, parameters or None)
                if identity:
                    self._sync_identity_sequence(insert.group(1), identity)
                self._connection.execute("RELEASE SAVEPOINT ulab_statement")
            else:
                result = self._connection.execute(compiled, parameters or None)
            names = (
                [col.name for col in result.description] if result.description else []
            )
            rows = result.fetchall() if names else []
            lastrowid = rows[-1][0] if identity and rows else None
            if identity:
                rows, names = [], []
            if "_ulab_rowid" in names:
                visible = [i for i, name in enumerate(names) if name != "_ulab_rowid"]
                rows = [tuple(row[i] for i in visible) for row in rows]
                names = [names[i] for i in visible]
            cursor = Cursor(
                rows,
                names,
                row_factory=self.row_factory,
                rowcount=result.rowcount,
                lastrowid=lastrowid,
            )
            return cursor
        except psycopg.Error as error:
            if savepoint:
                self._connection.execute("ROLLBACK TO SAVEPOINT ulab_statement")
                self._connection.execute("RELEASE SAVEPOINT ulab_statement")
            error_type = (
                IntegrityError
                if isinstance(error, psycopg.IntegrityError)
                else OperationalError
            )
            translated = error_type(str(error))
            translated.sqlstate = error.sqlstate
            raise translated from error

    def query_many(
        self, queries: Sequence[tuple[str, Sequence[Any]]]
    ) -> list[list[Any]]:
        import psycopg

        from unilabos.persistence.sql import code_sub, postgres_sql

        compiled = []
        for sql, parameters in queries:
            statement = postgres_sql(sql)
            if not statement.lstrip().upper().startswith("SELECT "):
                raise ValueError("批量读取只接受 SELECT")
            if parameters:
                statement = code_sub(statement.replace("%", "%%"), r"%%s", "%s")
            compiled.append((statement, parameters or None))
        try:
            with self._connection.pipeline():
                results = [
                    self._connection.execute(sql, parameters)
                    for sql, parameters in compiled
                ]
            return [
                Cursor(
                    result.fetchall(),
                    [column.name for column in result.description],
                    row_factory=self.row_factory,
                ).fetchall()
                for result in results
            ]
        except psycopg.Error as error:
            raise OperationalError(str(error)) from error

    def executemany(self, sql: str, parameters: Iterable[Sequence[Any]]) -> Cursor:
        total = 0
        for values in parameters:
            total += max(0, self.execute(sql, values).rowcount)
        return Cursor(rowcount=total)

    def executescript(self, script: str):
        from unilabos.persistence.content_views import _TOKENS

        # 只供现有按需建表助手使用；主结构初始化由 initialize 完成。
        for statement in _script_statements(script):
            if any(not token[0].isspace() and token[0] not in {";", "\ufeff"}
                   and not token[0].startswith(("--", "/*")) for token in _TOKENS.finditer(statement)):
                self.execute(statement)


def open_database(
    path: str,
    scope: str,
    *,
    transient: bool = False,
    create_parent: bool = False,
    private: bool = False,
) -> Database:
    settings = configured_settings()
    if transient or settings.database_type == "sqlite":
        return SQLiteDatabase(str(path), create_parent=create_parent, private=private)
    return PostgreSQLDatabase(settings, scope)


def prepare_database_path(path: str | Path, *, create_parent: bool = False) -> str:
    """只为 SQLite 准备本地文件路径。

    PostgreSQL 模式不依赖 SQLite 文件；返回内存标记供调用方继续传递给
    Store，同时避免展开路径、创建目录或触碰本地数据库文件。
    """

    if configured_settings().database_type == "postgresql":
        return ":memory:"
    value = str(path)
    if value == ":memory:":
        return value
    value = os.path.abspath(os.path.expanduser(value))
    if create_parent:
        Path(value).parent.mkdir(parents=True, exist_ok=True)
    return value


def open_readonly_database(path: Path):
    """检查已有运行事实，不创建表或 SQLite 文件。"""
    settings = configured_settings()
    if settings.database_type == "postgresql":
        database = PostgreSQLDatabase(settings, "inspection")
        database._connection.execute("SET default_transaction_read_only = on")
        return database
    if not path.exists():
        return None
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection
