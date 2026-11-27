"""内容合表必须保留三类旧表的约束，并修复已有 SQLite / PostgreSQL 库。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.persistence.test_sqlite_atomicity import _snapshot
from unilabos.app.scheduler.inventory.store import SCHEMA_VERSION, InventoryStore
from unilabos.persistence.database import IntegrityError, open_database
from unilabos.persistence.sql import postgres_sql

TABLES = ("sample", "current_substance", "reagent")
INVALID_INPUTS = [
    pytest.param("sample", {"quantity": -1}, id="sample-negative-quantity"),
    pytest.param("sample", {"quantity_unit": ""}, id="sample-empty-unit"),
    pytest.param("sample", {"name": None}, id="sample-null-name"),
    pytest.param("sample", {"expires_at": "2026-01-01"}, id="sample-reversed-dates"),
    pytest.param("current_substance", {"quantity": -1}, id="substance-negative-quantity"),
    pytest.param("current_substance", {"quantity_unit": ""}, id="substance-empty-unit"),
    pytest.param("current_substance", {"revision": 0}, id="substance-zero-revision"),
    pytest.param("current_substance", {"composition": "{}"}, id="substance-composition-object"),
    pytest.param("current_substance", {"composition": "not-json"}, id="substance-invalid-json"),
    pytest.param("reagent", {"quantity": -1}, id="reagent-negative-quantity"),
    pytest.param("reagent", {"quantity_unit": ""}, id="reagent-empty-unit"),
    pytest.param("reagent", {"revision": 0}, id="reagent-zero-revision"),
    pytest.param("reagent", {"density_g_per_ml": -1}, id="reagent-negative-density"),
    pytest.param("reagent", {"concentration_unit": None}, id="reagent-concentration-without-unit"),
    # 原表其余非空及成组字段约束也必须保留，不能被 SQL 的 NULL 检查语义绕过。
    pytest.param("sample", {"code": None}, id="sample-null-code"),
    pytest.param("sample", {"quantity": None}, id="sample-null-quantity"),
    pytest.param("sample", {"quantity_unit": None}, id="sample-null-unit"),
    pytest.param("current_substance", {"quantity": None}, id="substance-null-quantity"),
    pytest.param("current_substance", {"quantity_unit": None}, id="substance-null-unit"),
    pytest.param("current_substance", {"physical_state": None}, id="substance-null-state"),
    pytest.param("current_substance", {"physical_state": ""}, id="substance-empty-state"),
    pytest.param("current_substance", {"revision": None}, id="substance-null-revision"),
    pytest.param("current_substance", {"observed_at": None}, id="substance-null-observed-at"),
    pytest.param("reagent", {"quantity": None}, id="reagent-null-quantity"),
    pytest.param("reagent", {"quantity_unit": None}, id="reagent-null-unit"),
    pytest.param("reagent", {"revision": None}, id="reagent-null-revision"),
    pytest.param("reagent", {"reagent_info_uuid": None}, id="reagent-null-info"),
    pytest.param("reagent", {"density_g_per_ml": 0}, id="reagent-zero-density"),
    pytest.param("reagent", {"concentration_value": None}, id="reagent-unit-without-concentration"),
]


def _valid_content(table: str) -> dict[str, Any]:
    row = {
        "uuid": "content-" + table,
        "create_time": "2026-01-01",
        "update_time": "2026-01-01",
        "meta_data": "{}",
        "material_uuid": "material-" + table,
        "quantity": 1,
        "quantity_unit": "mL",
    }
    if table == "sample":
        row.update(code="S-1", name="样品", collected_at="2026-02-01", expires_at="2026-03-01")
    elif table == "current_substance":
        row.update(name="内容物", composition="[]", physical_state="liquid", revision=1, observed_at="2026-01-01")
    else:
        row.update(reagent_info_uuid="info", concentration_value=1, concentration_unit="mol/L", revision=1, density_g_per_ml=1)
    return row


def _insert(connection, table: str, values: dict[str, Any]) -> None:
    columns = ", ".join(values)
    placeholders = ", ".join("?" for _ in values)
    connection.execute(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", tuple(values.values()))


def _seed_references(store: InventoryStore) -> None:
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO resource_template(uuid,create_time,update_time,name,display_name,resource_type) "
            "VALUES ('template','2026-01-01','2026-01-01','模板','模板','container')"
        )
        for table in TABLES:
            connection.execute(
                "INSERT INTO material(uuid,create_time,update_time,resource_template_uuid,class,barcode,name,config,data) "
                "VALUES (?,'2026-01-01','2026-01-01','template','container','',?,'{}','{}')",
                ("material-" + table, "容器-" + table),
            )
        connection.execute(
            "INSERT INTO reagent_info(uuid,create_time,update_time,name,physical_state) "
            "VALUES ('info','2026-01-01','2026-01-01','水','liquid')"
        )


def _restore_v18_content_table(store: InventoryStore, mode: str) -> None:
    """安装固定的旧结构，不能从待测的新定义反推旧约束。"""

    source = (Path(__file__).parent / "fixtures/container_content_v18.sql").read_text()
    with store.transaction() as connection:
        connection.execute("CREATE TEMP TABLE content_backup AS SELECT * FROM container_content")
        for table in TABLES:
            connection.execute(f"DROP VIEW {table}")
        connection.execute("DROP TABLE container_content")
        connection.execute(postgres_sql(source) if mode == "postgresql" else source)
        connection.execute("INSERT INTO container_content SELECT * FROM content_backup")
        connection.execute("DROP TABLE content_backup")
        # 修复需要依结构识别旧表，也要支持版本号已经是当前值的幂等修复。
        if mode == "sqlite":
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


@pytest.fixture(params=["fresh", "repaired"])
def content_store(request, database_mode, tmp_path):
    path = str(tmp_path / "inventory.db")
    store = InventoryStore(path)
    try:
        _seed_references(store)
        if request.param == "repaired":
            _restore_v18_content_table(store, database_mode)
            store.close()
            store = InventoryStore(path)
        yield store
    finally:
        store.close()


@pytest.mark.parametrize("operation", ["insert", "update"])
@pytest.mark.parametrize("table,changes", INVALID_INPUTS)
def test_compatibility_views_reject_invalid_content(content_store, operation, table, changes):
    """遗漏任一原表约束，或已有合表库未修复，都必须使本测试失败。"""

    values = _valid_content(table)
    if operation == "update":
        with content_store.transaction() as connection:
            _insert(connection, table, values)
    before = content_store.query_all(f"SELECT * FROM {table}")
    with pytest.raises(IntegrityError):
        with content_store.transaction() as connection:
            if operation == "insert":
                _insert(connection, table, {**values, **changes})
            else:
                assignments = ", ".join(f"{column}=?" for column in changes)
                connection.execute(
                    f"UPDATE {table} SET {assignments} WHERE uuid=?",
                    (*changes.values(), values["uuid"]),
                )
    assert content_store.query_all(f"SELECT * FROM {table}") == before


@pytest.mark.parametrize("operation", ["insert", "update"])
@pytest.mark.parametrize("table", TABLES)
def test_compatibility_views_accept_zero_quantity(content_store, operation, table):
    values = _valid_content(table)
    with content_store.transaction() as connection:
        _insert(connection, table, {**values, "quantity": 0 if operation == "insert" else 1})
        if operation == "update":
            connection.execute(f"UPDATE {table} SET quantity=0 WHERE uuid=?", (values["uuid"],))
    assert content_store.query_one(f"SELECT quantity FROM {table}")["quantity"] == 0


@pytest.mark.parametrize("table,changes", [
    ("sample", {"collected_at": None, "expires_at": None}),
    ("current_substance", {"name": None}),
    ("reagent", {"physical_state": None, "density_g_per_ml": None, "concentration_value": None, "concentration_unit": None}),
])
def test_content_keeps_kind_specific_optional_values(content_store, table, changes):
    """不能为了共享表约束，把其他内容类型的可空字段一并收紧。"""

    with content_store.transaction() as connection:
        _insert(connection, table, {**_valid_content(table), **changes})
    saved = content_store.query_one(f"SELECT * FROM {table}")
    assert all(saved[key] is None for key in changes)


@pytest.mark.parametrize("table", TABLES)
def test_direct_content_table_cannot_bypass_constraints(content_store, table):
    values = _valid_content(table)
    values["kind"] = "substance" if table == "current_substance" else table
    values["quantity"] = -1
    with pytest.raises(IntegrityError):
        with content_store.transaction() as connection:
            _insert(connection, "container_content", values)


def test_existing_content_repair_preserves_rows_and_is_idempotent(database_mode, tmp_path):
    path = str(tmp_path / "inventory.db")
    store = InventoryStore(path)
    try:
        _seed_references(store)
        with store.transaction() as connection:
            for table in TABLES:
                _insert(connection, table, {**_valid_content(table), "quantity": 0})
            _insert(connection, "sample", {**_valid_content("sample"), "uuid": "sample-history", "deleted_at": "2026-02-01"})
        before = store.query_all("SELECT * FROM container_content ORDER BY uuid")
        _restore_v18_content_table(store, database_mode)
        with store.transaction() as connection:
            if database_mode == "sqlite":
                connection.execute("PRAGMA user_version = 18")
                connection.execute("CREATE TABLE content_audit(uuid TEXT)")
                connection.execute(
                    "CREATE TRIGGER audit_content_update AFTER UPDATE ON container_content "
                    "BEGIN INSERT INTO content_audit VALUES (NEW.uuid); END"
                )
            connection.execute("CREATE INDEX content_description ON container_content(description)")
        store.close()
        for attempt in range(2):
            store = InventoryStore(path)
            assert store.query_all("SELECT * FROM container_content ORDER BY uuid") == before
            assert store.query_one("SELECT COUNT(*) AS n FROM sample")["n"] == 2
            # 重建兼容视图及唯一索引之后，仍拒绝重复的活动内容。
            with pytest.raises(IntegrityError):
                with store.transaction() as connection:
                    _insert(connection, "sample", {**_valid_content("sample"), "uuid": "duplicate"})
            if database_mode == "sqlite":
                assert store.query_one("SELECT name FROM sqlite_master WHERE name='content_description'")
                with store.transaction() as connection:
                    connection.execute("UPDATE sample SET quantity=0 WHERE uuid='content-sample'")
                assert store.query_one("SELECT COUNT(*) AS n FROM content_audit")["n"] == attempt + 1
            else:
                assert store.query_one("SELECT indexname FROM pg_indexes WHERE indexname='content_description'")
            store.close()
    finally:
        store.close()


def _content_snapshot(path: str, mode: str):
    if mode == "sqlite":
        return _snapshot(Path(path))
    database = open_database(path, "inspection")
    try:
        queries = [
            "SELECT * FROM container_content ORDER BY uuid",
            "SELECT conname,pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid='container_content'::regclass ORDER BY conname",
            "SELECT column_name,is_nullable,column_default FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name='container_content' ORDER BY ordinal_position",
            "SELECT indexname,indexdef FROM pg_indexes WHERE tablename='container_content' ORDER BY indexname",
        ]
        return [[tuple(row) for row in database.query_all(query)] for query in queries]
    finally:
        database.close()


@pytest.mark.parametrize("table", TABLES)
def test_invalid_existing_content_rejects_repair_without_changing_database(database_mode, tmp_path, table):
    path = str(tmp_path / "inventory.db")
    store = InventoryStore(path)
    try:
        _seed_references(store)
        _restore_v18_content_table(store, database_mode)
        values = _valid_content(table)
        values.update(kind="substance" if table == "current_substance" else table, quantity=-1)
        with store.transaction() as connection:
            _insert(connection, "container_content", values)
    finally:
        store.close()
    before = _content_snapshot(path, database_mode)
    error_type = IntegrityError if database_mode == "sqlite" else pytest.importorskip("psycopg").IntegrityError
    with pytest.raises(error_type):
        InventoryStore(path)
    assert _content_snapshot(path, database_mode) == before
