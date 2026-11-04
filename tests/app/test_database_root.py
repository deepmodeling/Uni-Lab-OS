"""四库根目录：命令行 > BasicConfig（配置文件 / 环境变量）> ~/.unilabos，与工作目录无关。"""

from __future__ import annotations

from pathlib import Path

import pytest

from unilabos.app.cli.parser import build_parser
from unilabos.config import config as config_module
from unilabos.config.config import BasicConfig
from unilabos.server.startup import DEFAULT_DATABASE_ROOT, configured_database_root, resolve_database_paths


@pytest.fixture(autouse=True)
def _restore_basic_config():
    previous = (BasicConfig.server_database_root, BasicConfig.server_database_paths)
    yield
    BasicConfig.server_database_root, BasicConfig.server_database_paths = previous


def test_parser_leaves_database_root_unset_so_config_can_apply() -> None:
    assert build_parser().parse_args([]).server_database_root is None
    dashed = build_parser().parse_args(["--server-database-root", "/data/lab"])
    assert dashed.server_database_root == "/data/lab"


def test_cli_root_wins_over_config(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(BasicConfig, "server_database_root", str(tmp_path / "from-config"))
    paths = resolve_database_paths({"server_database_root": str(tmp_path / "from-cli")}, working_dir=tmp_path)
    assert paths.root == (tmp_path / "from-cli").resolve()
    assert BasicConfig.server_database_paths is paths


def test_config_root_applies_without_cli_and_ignores_working_dir(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(BasicConfig, "server_database_root", str(tmp_path / "from-config"))
    paths = resolve_database_paths({"server_database_root": None}, working_dir=tmp_path / "work")
    assert paths.root == (tmp_path / "from-config").resolve()
    assert paths.materials_db == (tmp_path / "from-config" / "materials.db").resolve()


def test_default_root_is_home_unilabos(monkeypatch) -> None:
    monkeypatch.setattr(BasicConfig, "server_database_root", "  ")
    assert configured_database_root({}) == DEFAULT_DATABASE_ROOT == "~/.unilabos"
    assert configured_database_root({"server_database_root": ""}) == "~/.unilabos"


def test_env_variable_overrides_config_field(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("UNILABOS_BASICCONFIG_SERVER_DATABASE_ROOT", str(tmp_path / "from-env"))
    config_module._update_config_from_env()
    assert BasicConfig.server_database_root == str(tmp_path / "from-env")
    assert configured_database_root({}) == str(tmp_path / "from-env")
