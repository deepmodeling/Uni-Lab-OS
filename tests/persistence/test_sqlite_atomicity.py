"""库存 SQLite 升级失败时完整恢复结构、数据和版本。"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from tests.app.test_inventory_backend_schema_v5 import _create_v4_database
from tests.persistence.test_table_compaction_requirements import _seed_legacy_inventory
from unilabos.app.scheduler.inventory import table_compaction
from unilabos.app.scheduler.inventory.store import SCHEMA_VERSION, InventoryStore
from unilabos.config.config import BasicConfig
from unilabos.persistence.database import SQLiteDatabase


def _snapshot(path: Path) -> dict:
    """独立连接读取全部结构及数据，包括 SQLite 的内部序列表。"""

    with closing(sqlite3.connect(path)) as connection:
        objects = connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "ORDER BY type, name"
        ).fetchall()
        data = {}
        for kind, name, _, _ in objects:
            if kind == "table":
                identifier = '"' + name.replace('"', '""') + '"'
                data[name] = sorted(
                    connection.execute(f"SELECT * FROM {identifier}").fetchall(),
                    key=repr,
                )
        return {
            "version": connection.execute("PRAGMA user_version").fetchone()[0],
            "objects": objects,
            "data": data,
        }


@pytest.mark.parametrize("initial_version", [4, 17, SCHEMA_VERSION])
@pytest.mark.parametrize(
    "stage",
    [
        "_merge_material_children",
        "_merge_container_content",
        "_collapse_lab_meta",
        "_reinstall_compatibility_views",
    ],
    ids=["material_copy", "content_copy", "source_drop", "views"],
)
def test_inventory_migration_failure_restores_snapshot_and_can_retry(
    tmp_path: Path,
    monkeypatch,
    initial_version: int,
    stage: str,
) -> None:
    """INV-015：中途失败后完整回滚，同一个文件仍可重试升级。"""

    monkeypatch.setattr(BasicConfig, "database_type", "sqlite")
    path = tmp_path / "inventory.db"
    if initial_version == 4:
        _create_v4_database(str(path))
    else:
        _seed_legacy_inventory(path)
        if initial_version == SCHEMA_VERSION:
            InventoryStore(str(path)).close()
    before = _snapshot(path)
    assert before["version"] == initial_version
    original = getattr(table_compaction, stage)

    def fail_after_migration_step(connection) -> None:
        original(connection)
        raise RuntimeError("注入库存迁移异常")

    with monkeypatch.context() as patch:
        patch.setattr(table_compaction, stage, fail_after_migration_step)
        with pytest.raises(RuntimeError, match="注入库存迁移异常"):
            InventoryStore(str(path))

    assert _snapshot(path) == before

    store = InventoryStore(str(path))
    try:
        assert store.query_one("PRAGMA user_version")["user_version"] == SCHEMA_VERSION
        assert store.query_one("PRAGMA integrity_check")["integrity_check"] == "ok"
        assert store.query_all("PRAGMA foreign_key_check") == []
        # 原始物料仍在；重建的兼容视图也必须可读。
        expected_materials = 2 if initial_version == 4 else 6
        assert store.query_one("SELECT COUNT(*) AS n FROM material")["n"] == expected_materials
        assert store.query_one("SELECT COUNT(*) AS n FROM material_instance")["n"] == expected_materials
        assert store.db.in_transaction is False
        assert store.query_one("PRAGMA foreign_keys")["foreign_keys"] == 1
    finally:
        store.close()


def test_content_conflict_restores_entire_legacy_inventory(tmp_path, monkeypatch) -> None:
    """内容冲突也必须撤销已执行的修复步骤，保留原 v17 库。"""

    monkeypatch.setattr(BasicConfig, "database_type", "sqlite")
    path = tmp_path / "inventory.db"
    _seed_legacy_inventory(path, duplicate_active_content=True)
    before = _snapshot(path)

    with pytest.raises(sqlite3.IntegrityError, match="container_content.material_uuid"):
        InventoryStore(str(path))

    assert _snapshot(path) == before


@pytest.mark.parametrize("failure", ["body", "foreign_key"])
def test_schema_migration_rolls_back_scripts_and_restores_connection(tmp_path, failure: str) -> None:
    """脚本中的触发器和字符串分号不能打破事务，失败后连接仍可复用。"""

    database = SQLiteDatabase(str(tmp_path / "migration.db"))
    database.execute("PRAGMA foreign_keys = ON")
    try:
        error_type = RuntimeError if failure == "body" else sqlite3.IntegrityError
        with pytest.raises(error_type):
            with database.schema_migration():
                database.executescript(
                    """
                    CREATE TABLE source(value TEXT PRIMARY KEY);
                    CREATE TABLE audit(value TEXT);
                    CREATE TRIGGER source_insert AFTER INSERT ON source
                    BEGIN
                        INSERT INTO audit VALUES ('first;second');
                        INSERT INTO audit VALUES (NEW.value);
                    END;
                    INSERT INTO source VALUES ('a;b'); INSERT INTO source VALUES ('c');
                    """
                )
                assert [row[0] for row in database.query_all("SELECT value FROM audit")] == [
                    "first;second",
                    "a;b",
                    "first;second",
                    "c",
                ]
                if failure == "body":
                    raise RuntimeError("注入迁移异常")
                database.executescript(
                    "CREATE TABLE child(value TEXT REFERENCES source(value));"
                    "INSERT INTO child VALUES ('missing');"
                )
        assert database.table_names() == set()
        assert database.in_transaction is False
        assert database.query_one("PRAGMA foreign_keys")[0] == 1
        assert database.query_one("PRAGMA legacy_alter_table")[0] == 0

        # 上下文已退出，普通脚本仍可自行管理事务。
        database.executescript("BEGIN; CREATE TABLE retry(id INTEGER); COMMIT;")
        assert database.table_names() == {"retry"}
    finally:
        database.close()
