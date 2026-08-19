"""每个物理数据库必须保有稳定职责和可验证的 schema checksum。"""

from __future__ import annotations

import sqlite3

import pytest

from unilabos.server.database import (
    DATABASE_SPECS,
    DatabaseIdentityConflict,
    SchemaDriftError,
    initialize_database,
)


def test_one_database_file_cannot_change_role(tmp_path) -> None:
    path = tmp_path / "one-role.db"
    connection = initialize_database(path, DATABASE_SPECS["runtime"])
    connection.close()

    with pytest.raises(DatabaseIdentityConflict, match="cannot open it as 'materials'"):
        initialize_database(path, DATABASE_SPECS["materials"])

    connection = sqlite3.connect(path)
    try:
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='material'"
            ).fetchone()
            is None
        )
    finally:
        connection.close()


def test_same_version_schema_drift_fails_closed(tmp_path) -> None:
    path = tmp_path / "runtime.db"
    connection = initialize_database(path, DATABASE_SPECS["runtime"])
    with connection:
        connection.execute(
            "UPDATE schema_migration SET checksum='tampered' "
            "WHERE database_key='runtime' AND version=1"
        )
    connection.close()

    with pytest.raises(SchemaDriftError, match="checksum differs"):
        initialize_database(path, DATABASE_SPECS["runtime"])


def test_unowned_database_is_not_silently_adopted(tmp_path) -> None:
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE legacy_fact(uuid TEXT PRIMARY KEY)")
    connection.commit()
    connection.close()

    with pytest.raises(DatabaseIdentityConflict, match="no schema_migration identity"):
        initialize_database(path, DATABASE_SPECS["runtime"])


def test_migration_records_real_embedded_checksum(tmp_path) -> None:
    spec = DATABASE_SPECS["history"]
    connection = initialize_database(tmp_path / spec.filename, spec)
    try:
        row = connection.execute(
            "SELECT database_key,name,checksum FROM schema_migration WHERE version=1"
        ).fetchone()
        assert tuple(row) == ("history", "history_v1", spec.checksum)
        assert len(row[2]) == 64
    finally:
        connection.close()
