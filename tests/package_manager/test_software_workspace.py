"""领域软件验收必须隔离调用进程的数据库配置。"""

from types import SimpleNamespace

from unilabos.config.config import BasicConfig
from unilabos.persistence.config import DatabaseSettings, configured_settings, use_database_settings
from unilabos.testing.workspace import software_workspace


def test_software_workspace_owns_sqlite_even_under_postgresql_configuration(tmp_path, monkeypatch):
    settings = DatabaseSettings("postgresql", "postgresql://unused@invalid.example/production")
    monkeypatch.setattr(BasicConfig, "database_type", "postgresql")
    monkeypatch.setattr(BasicConfig, "database_url", settings.database_url)
    catalog = SimpleNamespace(definitions=SimpleNamespace(devices=[], resources=[]))
    with use_database_settings(settings):
        with software_workspace(catalog, tmp_path / "isolated", device_ids=set(),
                                resource_nodes=[], source_id="isolated-test") as workspace:
            assert (workspace.path / "inventory.db").is_file()
            assert workspace.store.db.query_one("SELECT 1")[0] == 1
            assert configured_settings() == settings
        assert configured_settings() == settings
