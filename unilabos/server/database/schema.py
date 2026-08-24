"""四个独立 SQLite 文件共用的声明式 schema 与建库入口。"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


class DatabaseIdentityConflict(RuntimeError):
    """物理 SQLite 文件已属于其他职责或没有可验证的数据库身份。"""


class SchemaDriftError(RuntimeError):
    """同一 schema 版本的实际声明与已落库 checksum 不一致。"""


@dataclass(frozen=True)
class TableSpec:
    """一张表及其索引的不可变建表规格。"""

    name: str
    create_sql: str
    indexes: tuple[str, ...] = ()


@dataclass(frozen=True)
class DatabaseSpec:
    """一个物理 SQLite 文件的完整 v1 schema。"""

    key: str
    filename: str
    role: str
    version: int
    synchronous: str
    tables: tuple[TableSpec, ...]

    @property
    def table_names(self) -> tuple[str, ...]:
        return tuple(table.name for table in self.tables)

    def statements(self) -> Iterable[str]:
        for table in self.tables:
            yield table.create_sql.strip()
            yield from (index.strip() for index in table.indexes)

    @property
    def checksum(self) -> str:
        canonical = "\n\n".join(self.statements()).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()


SCHEMA_MIGRATION_TABLE = TableSpec(
    name="schema_migration",
    create_sql="""
        CREATE TABLE IF NOT EXISTS schema_migration (
            database_key TEXT NOT NULL CHECK (TRIM(database_key) <> ''),
            version INTEGER NOT NULL CHECK (version > 0),
            name TEXT NOT NULL CHECK (TRIM(name) <> ''),
            checksum TEXT NOT NULL CHECK (TRIM(checksum) <> ''),
            applied_at_ms INTEGER NOT NULL CHECK (applied_at_ms >= 0),
            PRIMARY KEY(database_key, version)
        )
    """,
)


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        if not str(row[0]).startswith("sqlite_")
    }


def _validate_schema_migration_shape(connection: sqlite3.Connection) -> None:
    expected = {"database_key", "version", "name", "checksum", "applied_at_ms"}
    actual = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(schema_migration)")
    }
    if actual != expected:
        raise SchemaDriftError(
            "schema_migration has an incompatible shape; discard or explicitly "
            "migrate this pre-v1 database"
        )


def _validate_existing_database(
    connection: sqlite3.Connection,
    spec: DatabaseSpec,
    *,
    preexisting_tables: set[str],
) -> None:
    _validate_schema_migration_shape(connection)
    rows = connection.execute(
        "SELECT database_key,version,name,checksum FROM schema_migration"
    ).fetchall()
    existing_keys = {str(row[0]) for row in rows}
    if existing_keys and existing_keys != {spec.key}:
        keys = ", ".join(sorted(existing_keys))
        raise DatabaseIdentityConflict(
            f"database file belongs to {keys!r}, cannot open it as {spec.key!r}"
        )
    if preexisting_tables - {"schema_migration"} and not rows:
        raise DatabaseIdentityConflict(
            "database contains domain tables but has no verifiable database identity"
        )

    current = next((row for row in rows if int(row[1]) == spec.version), None)
    if current is not None:
        expected_name = f"{spec.key}_v{spec.version}"
        if str(current[2]) != expected_name or str(current[3]) != spec.checksum:
            raise SchemaDriftError(
                f"{expected_name} checksum differs from the embedded schema"
            )

    user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if user_version > spec.version:
        raise SchemaDriftError(
            f"database user_version {user_version} is newer than supported "
            f"version {spec.version}"
        )


def initialize_database(
    path: str | Path,
    spec: DatabaseSpec,
    *,
    timeout: float = 30.0,
) -> sqlite3.Connection:
    """创建或打开一个独立后端数据库并确保完整 v1 schema 已存在。"""

    database_path = Path(path).expanduser().resolve()
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(
        str(database_path),
        timeout=timeout,
        check_same_thread=False,
    )
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute(f"PRAGMA synchronous = {spec.synchronous}")
        connection.execute("PRAGMA busy_timeout = 30000")
        preexisting_tables = _table_names(connection)
        if preexisting_tables and "schema_migration" not in preexisting_tables:
            raise DatabaseIdentityConflict(
                "database contains tables but no schema_migration identity"
            )
        with connection:
            connection.execute(SCHEMA_MIGRATION_TABLE.create_sql.strip())
            _validate_existing_database(
                connection,
                spec,
                preexisting_tables=preexisting_tables,
            )
            for statement in spec.statements():
                connection.execute(statement)
            connection.execute(
                """
                INSERT OR IGNORE INTO schema_migration(
                    database_key, version, name, checksum, applied_at_ms
                ) VALUES (?, ?, ?, ?, CAST(strftime('%s', 'now') AS INTEGER) * 1000)
                """,
                (
                    spec.key,
                    spec.version,
                    f"{spec.key}_v{spec.version}",
                    spec.checksum,
                ),
            )
            connection.execute(f"PRAGMA user_version = {spec.version}")
        return connection
    except BaseException:
        connection.close()
        raise


__all__ = [
    "DatabaseIdentityConflict",
    "DatabaseSpec",
    "SCHEMA_MIGRATION_TABLE",
    "SchemaDriftError",
    "TableSpec",
    "initialize_database",
]
