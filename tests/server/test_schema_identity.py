"""验证数据库职责身份，以及没有迁移路径时各库的拒绝 / 重建策略。"""

from __future__ import annotations

import sqlite3

import pytest

from unilabos.config.config import BasicConfig
from unilabos.server.database import (
    DATABASE_SPECS,
    DatabaseIdentityConflict,
    DatabaseMigrationRequired,
    initialize_database,
    sql_migration,
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


def _tamper(path, key: str) -> None:
    connection = initialize_database(path, DATABASE_SPECS[key])
    with connection:
        connection.execute(f"UPDATE schema_identity SET checksum='tampered' WHERE database_key='{key}'")
        connection.execute("CREATE TABLE drift_marker(x TEXT)")
    connection.close()


def _table_set(path) -> set[str]:
    connection = sqlite3.connect(path)
    try:
        return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        connection.close()


def _refusing(key: str):
    """发布时 runtime / materials 将使用的拒绝策略；开发期默认是 rebuild。"""
    from dataclasses import replace

    return replace(DATABASE_SPECS[key], on_unmigratable="refuse")


@pytest.mark.parametrize("key", ["runtime", "materials", "telemetry", "history"])
def test_schema_drift_rebuilds_during_development(tmp_path, key) -> None:
    """开发期：指纹漂移且没有迁移路径时把旧库改名保留（.bak-时间戳）并按当前声明重建。"""
    path = tmp_path / DATABASE_SPECS[key].filename
    _tamper(path, key)

    connection = initialize_database(path, DATABASE_SPECS[key])
    try:
        row = connection.execute("SELECT database_key, checksum FROM schema_identity").fetchone()
        assert tuple(row) == (key, DATABASE_SPECS[key].checksum)
    finally:
        connection.close()
    assert "drift_marker" not in _table_set(path)
    backups = [item for item in tmp_path.glob(f"{DATABASE_SPECS[key].filename}.bak-*")
               if not item.name.endswith(("-wal", "-shm"))]
    assert len(backups) == 1 and "drift_marker" in _table_set(backups[0])


@pytest.mark.parametrize("key", ["runtime", "materials"])
def test_refuse_policy_keeps_the_old_database(tmp_path, key) -> None:
    """发布后的拒绝策略：指纹漂移且没有迁移路径时拒绝启动，旧库原样保留。"""
    path = tmp_path / DATABASE_SPECS[key].filename
    _tamper(path, key)

    with pytest.raises(DatabaseMigrationRequired, match="拒绝启动"):
        initialize_database(path, _refusing(key))

    assert "drift_marker" in _table_set(path)
    assert not list(tmp_path.glob(f"{DATABASE_SPECS[key].filename}.bak-*"))


def test_refuse_policy_rebuild_can_be_allowed_explicitly(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(BasicConfig, "database_rebuild_on_mismatch", True)
    path = tmp_path / "runtime.db"
    _tamper(path, "runtime")

    connection = initialize_database(path, _refusing("runtime"))
    try:
        row = connection.execute("SELECT database_key, checksum FROM schema_identity").fetchone()
        assert tuple(row) == ("runtime", DATABASE_SPECS["runtime"].checksum)
    finally:
        connection.close()
    assert "drift_marker" not in _table_set(path)
    backups = [item for item in tmp_path.glob("runtime.db.bak-*") if not item.name.endswith(("-wal", "-shm"))]
    assert len(backups) == 1 and "drift_marker" in _table_set(backups[0])


def test_contract_version_bump_without_migration_is_refused_under_refuse_policy(tmp_path) -> None:
    """DDL 不变、行内容契约变化（contract_version +1）同样改变指纹：拒绝策略下没有迁移就拒绝启动。"""
    from dataclasses import replace

    spec = _refusing("materials")
    upcoming = replace(spec, contract_version=spec.contract_version + 1)
    assert upcoming.checksum != spec.checksum
    path = tmp_path / spec.filename
    initialize_database(path, spec).close()

    with pytest.raises(DatabaseMigrationRequired):
        initialize_database(path, upcoming)


def test_contract_version_bump_with_migration_keeps_unaffected_rows(tmp_path) -> None:
    from dataclasses import replace

    spec = DATABASE_SPECS["materials"]
    # 在当前基线之上再加一步迁移（与库的真实版本号无关）
    next_version = spec.schema_version + 1
    current = replace(
        spec,
        contract_version=spec.contract_version + 1,
        schema_version=next_version,
        migrations=spec.migrations + (
            sql_migration(next_version, "drop effects derived with the old request envelope",
                          "DELETE FROM inventory_command_effect WHERE operation = 'sync_template'"),
        ),
    )
    path = tmp_path / spec.filename
    connection = initialize_database(path, spec)
    with connection:
        for command, operation in (("old", "sync_template"), ("keep", "patch")):
            connection.execute(
                """
                INSERT INTO inventory_command_effect(
                    command_uuid, effect_key, operation, request_json, request_hash,
                    status, result_json, error_code, error_message,
                    started_at_ms, updated_at_ms, completed_at_ms
                ) VALUES (?, 'effect', ?, '{}', 'h', 'rejected', '{}', 'conflict', 'stale', 1, 1, 1)
                """,
                (command, operation),
            )
    connection.close()

    connection = initialize_database(path, current)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == next_version
        assert tuple(connection.execute("SELECT database_key, checksum FROM schema_identity").fetchone()) == (
            "materials", current.checksum,
        )
        assert [row[0] for row in connection.execute("SELECT command_uuid FROM inventory_command_effect")] == ["keep"]
    finally:
        connection.close()
    assert len(list(tmp_path.glob(f"{spec.filename}.pre-v{next_version}-*.bak"))) == 1


@pytest.mark.parametrize("key", ["runtime", "materials", "telemetry", "history"])
@pytest.mark.parametrize(
    ("stored_version", "same_checksum"),
    [(12, True), (8, False), (0, False)],
    ids=["newer-same-fingerprint", "newer-drifted", "unversioned-drifted"],
)
def test_database_from_before_the_v1_baseline_is_backed_up_and_rebuilt(
    tmp_path, key, stored_version, same_checksum
) -> None:
    """0.13 之前的开发库（库内版本更高或没有版本、带旧迁移记录）：改名备份后按 v1 基线重建。

    指纹一致但库内版本更高也不复用：旧迁移记录会与之后同号的迁移冲突。
    """
    spec = DATABASE_SPECS[key]
    path = tmp_path / spec.filename
    connection = initialize_database(path, spec)
    with connection:
        connection.execute(
            "CREATE TABLE schema_migration (version INTEGER PRIMARY KEY, description TEXT NOT NULL, "
            "from_checksum TEXT NOT NULL, to_checksum TEXT NOT NULL, applied_at_ms INTEGER NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO schema_migration VALUES (?, 'pre-baseline', 'old', 'old', 0)",
            [(version,) for version in range(2, stored_version + 1)],
        )
        connection.execute("CREATE TABLE pre_baseline_marker(x TEXT)")
        if not same_checksum:
            connection.execute(f"UPDATE schema_identity SET checksum='pre-baseline' WHERE database_key='{key}'")
    connection.execute(f"PRAGMA user_version = {stored_version}")
    connection.close()

    connection = initialize_database(path, spec)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == spec.schema_version == 1
        assert tuple(connection.execute("SELECT database_key, checksum FROM schema_identity").fetchone()) == (
            key, spec.checksum,
        )
    finally:
        connection.close()
    assert not {"pre_baseline_marker", "schema_migration"} & _table_set(path)
    backups = [item for item in tmp_path.glob(f"{spec.filename}.bak-*") if not item.name.endswith(("-wal", "-shm"))]
    assert len(backups) == 1 and "pre_baseline_marker" in _table_set(backups[0])
    peek = sqlite3.connect(backups[0])
    try:
        assert peek.execute("PRAGMA user_version").fetchone()[0] == stored_version
    finally:
        peek.close()


def test_unidentified_database_is_rebuilt_for_derived_databases(tmp_path) -> None:
    """缺少 schema_identity 的数据库：history / telemetry 按当前声明重建。"""
    path = tmp_path / "unidentified.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE foreign_fact(uuid TEXT PRIMARY KEY)")
    connection.commit()
    connection.close()

    connection = initialize_database(path, DATABASE_SPECS["history"])
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "foreign_fact" not in tables
        assert "schema_identity" in tables
    finally:
        connection.close()


def test_refuse_policy_does_not_wipe_unidentified_files(tmp_path) -> None:
    path = tmp_path / "unidentified.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE foreign_fact(uuid TEXT PRIMARY KEY)")
    connection.commit()
    connection.close()

    with pytest.raises(DatabaseMigrationRequired, match="schema_identity"):
        initialize_database(path, _refusing("runtime"))
    assert "foreign_fact" in _table_set(path)


def test_identity_records_real_embedded_checksum(tmp_path) -> None:
    spec = DATABASE_SPECS["history"]
    connection = initialize_database(tmp_path / spec.filename, spec)
    try:
        row = connection.execute(
            "SELECT database_key, checksum FROM schema_identity"
        ).fetchone()
        assert tuple(row) == ("history", spec.checksum)
        assert len(row[1]) == 64
    finally:
        connection.close()
