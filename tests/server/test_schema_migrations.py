"""Flyway 式线性迁移：备份、单事务、迁移后结构校验与回滚。"""

from __future__ import annotations

import sqlite3

import pytest

from unilabos.server.database import (
    DatabaseMigrationFailed,
    DatabaseMigrationRequired,
    DatabaseSpec,
    Migration,
    TableSpec,
    initialize_database,
    sql_migration,
)
from unilabos.server.database.schema import MIGRATION_HISTORY_TABLE, SCHEMA_IDENTITY_TABLE

ITEM_V1 = TableSpec(
    "item",
    "CREATE TABLE IF NOT EXISTS item (uuid TEXT PRIMARY KEY, name TEXT NOT NULL)",
    ("CREATE INDEX IF NOT EXISTS idx_item_name ON item(name)",),
)
ITEM_V2 = TableSpec(
    "item",
    "CREATE TABLE IF NOT EXISTS item (uuid TEXT PRIMARY KEY, name TEXT NOT NULL, qty INTEGER NOT NULL DEFAULT 0)",
    ("CREATE INDEX IF NOT EXISTS idx_item_name ON item(name)",),
)
NOTE = TableSpec(
    "note",
    "CREATE TABLE IF NOT EXISTS note (item_uuid TEXT NOT NULL REFERENCES item(uuid), body TEXT NOT NULL)",
)
V1 = DatabaseSpec(
    key="demo", filename="demo.db", role="demo", synchronous="NORMAL",
    tables=(SCHEMA_IDENTITY_TABLE, ITEM_V1), on_unmigratable="refuse",
)
TO_V2 = sql_migration(
    2, "add qty and note",
    "ALTER TABLE item ADD COLUMN qty INTEGER NOT NULL DEFAULT 0",
    "CREATE TABLE note (item_uuid TEXT NOT NULL REFERENCES item(uuid), body TEXT NOT NULL)",
)
V2 = DatabaseSpec(
    key="demo", filename="demo.db", role="demo", synchronous="NORMAL",
    tables=(SCHEMA_IDENTITY_TABLE, ITEM_V2, NOTE), schema_version=2, migrations=(TO_V2,), on_unmigratable="refuse",
)


def _v2_with(*migrations: Migration) -> DatabaseSpec:
    return DatabaseSpec(
        key="demo", filename="demo.db", role="demo", synchronous="NORMAL",
        tables=V2.tables, schema_version=2, migrations=migrations, on_unmigratable="refuse",
    )


def _seed_v1(path) -> None:
    connection = initialize_database(path, V1)
    with connection:
        connection.execute("INSERT INTO item(uuid, name) VALUES ('a', 'plate')")
    connection.close()


def _peek(path, sql: str):
    connection = sqlite3.connect(path)
    try:
        return connection.execute(sql).fetchall()
    finally:
        connection.close()


def test_new_database_records_current_version_without_backup(tmp_path) -> None:
    initialize_database(tmp_path / "demo.db", V2).close()
    assert _peek(tmp_path / "demo.db", "PRAGMA user_version") == [(2,)]
    assert not list(tmp_path.glob("*.bak"))


def test_existing_database_without_version_is_adopted_when_checksum_matches(tmp_path) -> None:
    path = tmp_path / "demo.db"
    initialize_database(path, V1).close()
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA user_version = 0")  # 迁移机制之前建的库
    connection.close()
    initialize_database(path, V1).close()
    assert _peek(path, "PRAGMA user_version") == [(1,)]


def test_forward_migration_keeps_rows_backs_up_and_records_history(tmp_path) -> None:
    path = tmp_path / "demo.db"
    _seed_v1(path)

    connection = initialize_database(path, V2)
    try:
        assert [tuple(row) for row in connection.execute("SELECT uuid, name, qty FROM item")] == [("a", "plate", 0)]
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert connection.execute("SELECT checksum FROM schema_identity").fetchone()[0] == V2.checksum
        history = connection.execute(f"SELECT version, description FROM {MIGRATION_HISTORY_TABLE}").fetchall()
        assert [tuple(row) for row in history] == [(2, "add qty and note")]
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        connection.close()

    backups = list(tmp_path.glob("demo.db.pre-v2-*.bak"))
    assert len(backups) == 1
    assert _peek(backups[0], "PRAGMA user_version") == [(1,)]
    assert _peek(backups[0], "SELECT name FROM item") == [("plate",)]

    initialize_database(path, V2).close()  # 已是当前版本：不再迁移、不再备份
    assert len(list(tmp_path.glob("demo.db.pre-v2-*.bak"))) == 1


def test_migration_that_misses_the_declared_structure_is_rolled_back(tmp_path) -> None:
    path = tmp_path / "demo.db"
    _seed_v1(path)
    incomplete = _v2_with(sql_migration(2, "forgets note", "ALTER TABLE item ADD COLUMN qty INTEGER NOT NULL DEFAULT 0"))

    with pytest.raises(DatabaseMigrationFailed, match="缺少"):
        initialize_database(path, incomplete)

    assert _peek(path, "PRAGMA user_version") == [(1,)]
    assert _peek(path, "SELECT checksum FROM schema_identity") == [(V1.checksum,)]
    columns = [row[1] for row in _peek(path, "PRAGMA table_info(item)")]
    assert columns == ["uuid", "name"]


def test_wrong_column_definition_is_detected(tmp_path) -> None:
    path = tmp_path / "demo.db"
    _seed_v1(path)
    wrong = _v2_with(sql_migration(
        2, "nullable qty",
        "ALTER TABLE item ADD COLUMN qty INTEGER",
        "CREATE TABLE note (item_uuid TEXT NOT NULL REFERENCES item(uuid), body TEXT NOT NULL)",
    ))
    with pytest.raises(DatabaseMigrationFailed, match="item"):
        initialize_database(path, wrong)
    assert _peek(path, "PRAGMA user_version") == [(1,)]


def test_failing_migration_rolls_back_every_step(tmp_path) -> None:
    path = tmp_path / "demo.db"
    _seed_v1(path)

    def boom(connection: sqlite3.Connection) -> None:
        connection.execute("ALTER TABLE item ADD COLUMN qty INTEGER NOT NULL DEFAULT 0")
        connection.execute("UPDATE item SET name = 'changed'")
        raise RuntimeError("data fix failed")

    with pytest.raises(DatabaseMigrationFailed, match="data fix failed"):
        initialize_database(path, _v2_with(Migration(2, "boom", boom)))
    assert _peek(path, "SELECT name FROM item") == [("plate",)]
    assert [row[1] for row in _peek(path, "PRAGMA table_info(item)")] == ["uuid", "name"]


def test_foreign_key_violations_block_the_migration(tmp_path) -> None:
    path = tmp_path / "demo.db"
    _seed_v1(path)
    orphan = _v2_with(sql_migration(
        2, "orphan note",
        "ALTER TABLE item ADD COLUMN qty INTEGER NOT NULL DEFAULT 0",
        "CREATE TABLE note (item_uuid TEXT NOT NULL REFERENCES item(uuid), body TEXT NOT NULL)",
        "INSERT INTO note(item_uuid, body) VALUES ('missing', 'x')",
    ))
    with pytest.raises(DatabaseMigrationFailed, match="外键"):
        initialize_database(path, orphan)
    assert _peek(path, "PRAGMA user_version") == [(1,)]


def test_executescript_inside_a_migration_is_reported(tmp_path) -> None:
    path = tmp_path / "demo.db"
    _seed_v1(path)

    def commits(connection: sqlite3.Connection) -> None:
        connection.executescript("SELECT 1;")

    with pytest.raises(DatabaseMigrationFailed, match="executescript"):
        initialize_database(path, _v2_with(Migration(2, "commits", commits)))


def test_unknown_older_schema_is_refused_and_left_untouched(tmp_path) -> None:
    path = tmp_path / "demo.db"
    _seed_v1(path)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA user_version = 0")
    connection.close()

    with pytest.raises(DatabaseMigrationRequired, match="迁移机制之前"):
        initialize_database(path, V2)
    assert _peek(path, "SELECT name FROM item") == [("plate",)]


def test_newer_database_than_code_is_refused(tmp_path) -> None:
    path = tmp_path / "demo.db"
    initialize_database(path, V2).close()
    with pytest.raises(DatabaseMigrationRequired, match="不低于代码版本"):
        initialize_database(path, V1)


@pytest.mark.parametrize("versions", [(3,), (2, 4)])
def test_migrations_must_be_contiguous(versions) -> None:
    with pytest.raises(ValueError, match="migrations must cover"):
        DatabaseSpec(
            key="demo", filename="demo.db", role="demo", synchronous="NORMAL", tables=V2.tables,
            schema_version=versions[-1], migrations=tuple(sql_migration(v, "x") for v in versions),
        )
