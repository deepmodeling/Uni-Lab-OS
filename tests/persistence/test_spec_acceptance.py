"""技术方案第十章里、现有双库夹具覆盖不到的验收路径。

这些用例不连接真实 PostgreSQL：路由用替身数据库，连接失败用驱动异常。
SQLite 业务结果、回填幂等和 Store 代码约束在本机直接执行。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from unilabos.config.config import BasicConfig, load_config
from unilabos.persistence.config import (
    DatabaseSettings,
    resolve_database_settings,
    verify_database_connection,
)
from unilabos.persistence.database import (
    OperationalError,
    SQLiteDatabase,
    open_database,
    prepare_database_path,
)
from unilabos.persistence.migrations import (
    _backfill_blank_material_type,
    _backfill_release_tenancy_uuid,
    backfill_execution_lock_identities,
)
from unilabos.workflow.resource_lock_key import material_lock_key
from unilabos.workflow.store import WorkflowStore

_STORE_FILES = (
    "unilabos/app/scheduler/inventory/store.py",
    "unilabos/app/scheduler/history.py",
    "unilabos/app/scheduler/device_state.py",
    "unilabos/app/edge_control/store.py",
    "unilabos/app/edge_control/local_authority.py",
    "unilabos/workflow/store.py",
)


class _PostgreSQLStandIn:
    """只记录启动时选中的数据库，不连接服务器。"""

    def __init__(self, settings: DatabaseSettings, scope: str):
        self.settings = settings
        self.scope = scope
        self.row_factory = None
        self.closed = False
        # 只在内存实现适配器的事务和空结构查询，不连接 PostgreSQL 或请求路径。
        # WorkflowStore 启动必须检查既有中心账本，路由替身也遵循该公共协议。
        self._connection = sqlite3.connect(":memory:", isolation_level=None)

    @property
    def in_transaction(self):
        return self._connection.in_transaction

    def execute(self, sql, parameters=()):
        return self._connection.execute(sql, parameters)

    def commit(self):
        self._connection.commit()

    def rollback(self):
        self._connection.rollback()

    def initialize(self, sqlite_initializer, **kwargs) -> None:
        return None

    def close(self) -> None:
        self.closed = True
        self._connection.close()


@pytest.fixture(autouse=True)
def restore_settings(monkeypatch):
    monkeypatch.setattr(BasicConfig, "database_type", "sqlite")
    monkeypatch.setattr(BasicConfig, "database_url", "")
    monkeypatch.delenv("UNILABOS_BASICCONFIG_DATABASE_TYPE", raising=False)
    monkeypatch.delenv("UNILABOS_BASICCONFIG_DATABASE_URL", raising=False)
    monkeypatch.delenv("UNILABOS_BASICCONFIG_CONFIG_PATH", raising=False)


def test_environment_overrides_file_and_cli_overrides_environment(tmp_path, monkeypatch):
    config = tmp_path / "local_config.py"
    config.write_text(
        'class BasicConfig:\n    database_type = "sqlite"\n    database_url = ""\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("UNILABOS_BASICCONFIG_DATABASE_TYPE", "postgresql")
    monkeypatch.setenv(
        "UNILABOS_BASICCONFIG_DATABASE_URL",
        "postgresql://user:secret@localhost/lab",
    )
    load_config(str(config))
    selected = resolve_database_settings({})
    assert selected.database_type == "postgresql"
    assert selected.database_url.endswith("/lab")

    overridden = resolve_database_settings(
        {"database_type": "sqlite", "database_url": None}
    )
    assert overridden.database_type == "sqlite"


def test_invalid_environment_database_type_fails_without_sqlite_fallback(
    tmp_path, monkeypatch
):
    config = tmp_path / "local_config.py"
    config.write_text(
        'class BasicConfig:\n    database_type = "sqlite"\n    database_url = ""\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("UNILABOS_BASICCONFIG_DATABASE_TYPE", "mysql")
    load_config(str(config))
    with pytest.raises(ValueError, match="sqlite 或 postgresql"):
        resolve_database_settings({})
    assert BasicConfig.database_type == "mysql"


def test_postgresql_connection_failure_does_not_open_sqlite(monkeypatch):
    import psycopg

    def refuse(*args, **kwargs):
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(psycopg, "connect", refuse)
    settings = DatabaseSettings(
        "postgresql", "postgresql://user:secret@127.0.0.1:1/lab"
    )
    with pytest.raises(OperationalError, match="无法连接 PostgreSQL"):
        verify_database_connection(settings)
    assert settings.database_type == "postgresql"


def test_definition_store_stays_sqlite_when_postgresql_is_selected(monkeypatch):
    monkeypatch.setattr(BasicConfig, "database_type", "postgresql")
    monkeypatch.setattr(
        BasicConfig, "database_url", "postgresql://user:secret@localhost/lab"
    )
    definitions = WorkflowStore(":memory:")
    try:
        assert isinstance(definitions.db, SQLiteDatabase)
        assert definitions.db.execute("SELECT 1").fetchone()[0] == 1
    finally:
        definitions.close()


def test_runtime_store_selects_postgresql_and_skips_sqlite_file(monkeypatch, tmp_path):
    monkeypatch.setattr(BasicConfig, "database_type", "postgresql")
    monkeypatch.setattr(
        BasicConfig, "database_url", "postgresql://user:secret@localhost/lab"
    )
    monkeypatch.setattr(
        "unilabos.persistence.database.PostgreSQLDatabase", _PostgreSQLStandIn
    )
    path = tmp_path / "nested" / "workflow_history.db"
    runtime = WorkflowStore(path, persist_workflow_definitions=False)
    try:
        assert isinstance(runtime.db, _PostgreSQLStandIn)
        assert runtime.db.scope == "workflow"
        assert not path.exists()
        assert not path.parent.exists()
    finally:
        runtime.close()


def test_prepared_runtime_path_still_selects_postgresql(monkeypatch, tmp_path):
    monkeypatch.setattr(BasicConfig, "database_type", "postgresql")
    monkeypatch.setattr(
        BasicConfig, "database_url", "postgresql://user:secret@localhost/lab"
    )
    monkeypatch.setattr(
        "unilabos.persistence.database.PostgreSQLDatabase", _PostgreSQLStandIn
    )
    requested = tmp_path / "missing" / "workflow_history.db"
    prepared = prepare_database_path(requested, create_parent=True)
    assert prepared == ":memory:"
    assert not requested.parent.exists()
    runtime = WorkflowStore(prepared, persist_workflow_definitions=False)
    try:
        assert isinstance(runtime.db, _PostgreSQLStandIn)
    finally:
        runtime.close()


def test_sqlite_prepare_database_path_keeps_file_and_parent(tmp_path):
    target = tmp_path / "state" / "inventory.db"
    prepared = prepare_database_path(target, create_parent=True)
    assert Path(prepared) == target.resolve()
    assert target.parent.is_dir()
    database = open_database(prepared, "inventory")
    try:
        assert isinstance(database, SQLiteDatabase)
        database.execute("CREATE TABLE marker (value TEXT)")
        database.execute("INSERT INTO marker VALUES (?)", ("kept",))
        database.commit()
    finally:
        database.close()
    assert target.is_file()


def test_blank_material_type_backfill_is_idempotent_and_keeps_existing_type():
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE resource_template (uuid TEXT, resource_type TEXT)")
    connection.execute(
        "CREATE TABLE material (uuid TEXT, type TEXT, resource_template_uuid TEXT)"
    )
    connection.execute("INSERT INTO resource_template VALUES ('template', 'reagent')")
    connection.execute("INSERT INTO material VALUES ('blank', '', 'template')")
    connection.execute("INSERT INTO material VALUES ('named', 'sample', 'template')")
    _backfill_blank_material_type(connection)
    _backfill_blank_material_type(connection)
    rows = dict(connection.execute("SELECT uuid, type FROM material"))
    assert rows == {"blank": "reagent", "named": "sample"}


def test_release_tenancy_backfill_fills_only_missing_identity():
    connection = sqlite3.connect(":memory:")
    connection.execute(
        """
        CREATE TABLE task_device_tenancy (
            uuid TEXT, workflow_task_uuid TEXT, material_uuid TEXT,
            device_lock_key TEXT, acquired_at TEXT
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE job_device_tenancy_transition (
            workflow_task_uuid TEXT, material_uuid TEXT,
            release_device_lock_key TEXT, create_time TEXT,
            release_tenancy_uuid TEXT
        )
        """
    )
    connection.execute(
        "INSERT INTO task_device_tenancy VALUES ('tenancy-1', 'task', 'material', '/devices/arm', '1')"
    )
    connection.execute(
        "INSERT INTO job_device_tenancy_transition VALUES ('task', 'material', '/devices/arm', '2', NULL)"
    )
    connection.execute(
        "INSERT INTO job_device_tenancy_transition VALUES ('task', 'material', '/devices/arm', '3', 'kept')"
    )
    _backfill_release_tenancy_uuid(connection)
    _backfill_release_tenancy_uuid(connection)
    filled = connection.execute(
        "SELECT release_tenancy_uuid FROM job_device_tenancy_transition ORDER BY create_time"
    ).fetchall()
    assert [row[0] for row in filled] == ["tenancy-1", "kept"]


def test_execution_lock_backfill_repairs_blank_identity_and_rejects_active_garbage():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE execution_lock_lease (
            uuid TEXT, lock_key TEXT, scope TEXT, material_uuid TEXT,
            site_uuid TEXT, state TEXT, deleted_at TEXT
        )
        """
    )
    connection.execute(
        "INSERT INTO execution_lock_lease VALUES (?, ?, 'material', '', '', 'reserved', NULL)",
        ("lease-1", material_lock_key("material-1")),
    )
    backfill_execution_lock_identities(connection)
    repaired = connection.execute(
        "SELECT material_uuid, site_uuid FROM execution_lock_lease WHERE uuid='lease-1'"
    ).fetchone()
    assert repaired["material_uuid"] == "material-1"
    assert repaired["site_uuid"] is None
    connection.execute(
        "INSERT INTO execution_lock_lease VALUES ('lease-2', 'not-a-key', 'material', '', '', 'running', NULL)"
    )
    with pytest.raises(sqlite3.IntegrityError, match="非规范"):
        backfill_execution_lock_identities(connection)


def test_business_stores_do_not_open_sqlite_or_branch_on_database_type():
    root = Path(__file__).parents[2]
    for relative in _STORE_FILES:
        source = (root / relative).read_text(encoding="utf-8")
        assert "sqlite3.connect" not in source, relative
        assert "database_type" not in source, relative
