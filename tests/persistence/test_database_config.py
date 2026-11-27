"""数据库选择的默认值、配置覆盖及错误边界。"""

import pytest

from unilabos.config.config import BasicConfig, _update_config_from_env
from unilabos.persistence.config import DatabaseSettings, resolve_database_settings
from unilabos.persistence.database import (
    SQLiteDatabase,
    open_database,
    prepare_database_path,
)


@pytest.fixture(autouse=True)
def restore_settings(monkeypatch):
    monkeypatch.setattr(BasicConfig, "database_type", "sqlite")
    monkeypatch.setattr(BasicConfig, "database_url", "")
    monkeypatch.delenv("UNILABOS_BASICCONFIG_DATABASE_TYPE", raising=False)
    monkeypatch.delenv("UNILABOS_BASICCONFIG_DATABASE_URL", raising=False)


def test_default_keeps_existing_sqlite_file(tmp_path):
    path = tmp_path / "inventory.db"
    settings = resolve_database_settings({})
    assert settings.database_type == "sqlite"
    database = open_database(str(path), "inventory")
    assert isinstance(database, SQLiteDatabase)
    database.execute("CREATE TABLE existing (value TEXT)")
    database.execute("INSERT INTO existing VALUES (?)", ("kept",))
    database.commit()
    database.close()
    reopened = open_database(str(path), "inventory")
    assert reopened.query_one("SELECT value FROM existing")[0] == "kept"
    reopened.close()


def test_explicit_cli_overrides_environment_and_config(monkeypatch):
    BasicConfig.database_type = "sqlite"
    monkeypatch.setenv("UNILABOS_BASICCONFIG_DATABASE_TYPE", "postgresql")
    monkeypatch.setenv(
        "UNILABOS_BASICCONFIG_DATABASE_URL", "postgresql://user:secret@localhost/test"
    )
    _update_config_from_env()
    assert resolve_database_settings({}).database_type == "postgresql"
    assert (
        resolve_database_settings({"database_type": "sqlite"}).database_type == "sqlite"
    )


@pytest.mark.parametrize(
    "kind,url",
    [
        ("pg", ""),
        ("postgresql", ""),
        ("postgresql", "sqlite:///tmp/file"),
        ("postgresql", "postgresql://localhost"),
    ],
)
def test_invalid_configuration_fails_without_fallback(kind, url):
    with pytest.raises(ValueError):
        DatabaseSettings(kind, url)


def test_sqlite_ignores_unused_url():
    assert DatabaseSettings("sqlite", "unused").database_type == "sqlite"


def test_connection_secret_not_in_settings_repr():
    assert "secret" not in repr(
        DatabaseSettings("postgresql", "postgresql://user:secret@localhost/test")
    )


def test_postgresql_does_not_prepare_sqlite_path(monkeypatch, tmp_path):
    monkeypatch.setattr(BasicConfig, "database_type", "postgresql")
    monkeypatch.setattr(
        BasicConfig, "database_url", "postgresql://user:secret@localhost/test"
    )
    target = tmp_path / "missing" / "device_state.db"
    assert prepare_database_path(target, create_parent=True) == ":memory:"
    assert not target.parent.exists()
