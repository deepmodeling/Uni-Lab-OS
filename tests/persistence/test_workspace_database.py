"""PostgreSQL 模式下 Host 读取真实数据库事实，不依赖 SQLite 文件是否存在。"""

from uuid import uuid4

import pytest

from tests.workflow.test_f05_task_runtime_projection import TASK_UUID, _seed_task
from unilabos.app.scheduler.inventory.material_exports import InventoryMaterialAuthority
from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.config.config import BasicConfig
from unilabos.persistence.config import configured_settings
from unilabos.persistence.database import open_readonly_database
from unilabos.persistence.workspace import (
    database_environment,
    reset_postgresql_domain,
    workspace_settings,
)
from unilabos.workflow.store import WorkflowStore
from unilabos.workspace_host.model import WorkspacePaths
from unilabos.workspace_host.reset_safety import inspect_local_reset_blockers


@pytest.mark.parametrize("cli_selected", [False, True])
def test_workspace_inspects_and_resets_configured_database(database_mode, tmp_path, cli_selected):
    if database_mode != "postgresql":
        import pytest

        pytest.skip("SQLite 文件检查由现有 Workspace Host 测试覆盖")
    deployment = tmp_path / "deployment"
    deployment.mkdir()
    (deployment / "local_config.py").write_text(
        'class BasicConfig:\n    database_type = "postgresql"\n    database_url = '
        + repr(BasicConfig.database_url)
        + "\n"
    )
    if cli_selected:
        from unilabos.persistence.config import DatabaseSettings
        from unilabos.persistence.workspace import save_workspace_database

        (deployment / "local_config.py").write_text("# CLI 选择，不配置数据库字段\n")
        save_workspace_database(tmp_path, DatabaseSettings("postgresql", BasicConfig.database_url))
    paths = WorkspacePaths.resolve(tmp_path)
    store = WorkflowStore(tmp_path / "unused.db")
    try:
        # 重置契约独立创建按需物料表，不能依赖其它测试先运行才暴露遗漏。
        inventory = InventoryStore(tmp_path / "inventory.db")
        try:
            InventoryMaterialAuthority(inventory, station_uuid=str(uuid4()),
                instance_uuid=str(uuid4()), event_epoch_uuid=str(uuid4()))
        finally:
            inventory.close()
        _seed_task(store, job_count=1)
        assert not (tmp_path / "unused.db").exists()
        assert (
            database_environment(tmp_path)["UNILABOS_BASICCONFIG_DATABASE_TYPE"]
            == "postgresql"
        )
        blockers = inspect_local_reset_blockers(paths)
        assert any(blocker.identity == TASK_UUID for blocker in blockers)
        with store.transaction() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS other_application_data (value TEXT)")
            connection.execute("INSERT INTO other_application_data VALUES ('keep')")
            connection.execute("UPDATE workflow_task SET status='succeeded'")
        assert inspect_local_reset_blockers(paths) == []
        truncated = reset_postgresql_domain(workspace_settings(tmp_path))
        assert {"station_material_export_identity", "station_material_export",
                "station_material_export_item", "station_material_boundary_receipt"}.issubset(truncated)
        assert store.db.query_one("SELECT count(*) FROM station_material_export_identity")[0] == 0
        assert store.count_rows("workflow_task") == 0
        assert (
            store.db.query_one("SELECT value FROM other_application_data")[0] == "keep"
        )
        readonly = open_readonly_database(tmp_path / "missing.db")
        try:
            import pytest

            from unilabos.persistence.database import OperationalError

            with pytest.raises(OperationalError):
                readonly.execute("DELETE FROM other_application_data")
        finally:
            readonly.close()
    finally:
        store.close()


def test_reset_refuses_external_foreign_key_without_partial_deletion(database_mode, tmp_path):
    if database_mode != "postgresql":
        pytest.skip("PostgreSQL显式TRUNCATE的外部引用保护")
    import psycopg

    store = WorkflowStore(tmp_path / "unused.db")
    try:
        _seed_task(store, job_count=1)
        with store.transaction() as connection:
            connection.execute("CREATE TABLE external_reset_guard (task_uuid TEXT REFERENCES workflow_task(uuid))")
            connection.execute("INSERT INTO external_reset_guard VALUES (?)", (TASK_UUID,))
        with pytest.raises(psycopg.errors.FeatureNotSupported):
            reset_postgresql_domain(configured_settings())
        assert store.count_rows("workflow_task") == 1
        assert store.db.query_one("SELECT task_uuid FROM external_reset_guard")[0] == TASK_UUID
    finally:
        with store.transaction() as connection:
            connection.execute("DROP TABLE IF EXISTS external_reset_guard")
        store.close()
