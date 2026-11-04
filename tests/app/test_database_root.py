"""四库根目录：命令行 > BasicConfig（配置文件 / 环境变量）> <工作目录>/database，旧位置 ~/.unilabos 兼容沿用。"""

from __future__ import annotations

from pathlib import Path

import pytest

from unilabos.app.cli.parser import build_parser
from unilabos.config import config as config_module
from unilabos.config.config import BasicConfig
from unilabos.server import startup
from unilabos.server.startup import configured_database_root, resolve_database_paths


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path: Path):
    """隔离 HOME（旧位置 ~/.unilabos）与 BasicConfig，测试不读写真实用户目录。"""

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(startup, "_legacy_warned", set())
    previous = (BasicConfig.server_database_root, BasicConfig.server_database_paths)
    BasicConfig.server_database_root = ""
    yield home
    BasicConfig.server_database_root, BasicConfig.server_database_paths = previous


def test_parser_leaves_database_root_unset_so_config_can_apply() -> None:
    assert build_parser().parse_args([]).server_database_root is None
    dashed = build_parser().parse_args(["--server-database-root", "/data/lab"])
    assert dashed.server_database_root == "/data/lab"


def test_default_root_follows_the_working_directory(tmp_path: Path) -> None:
    work = tmp_path / "lab-a" / "unilabos_data"
    paths = resolve_database_paths({"server_database_root": None}, working_dir=work)
    assert paths.root == (work / "database").resolve()
    assert paths.runtime_db == (work / "database" / "runtime.db").resolve()
    other = resolve_database_paths({}, working_dir=tmp_path / "lab-b" / "unilabos_data")
    assert other.root != paths.root  # 每个工作目录各自一套


def test_cli_root_wins_over_config(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(BasicConfig, "server_database_root", str(tmp_path / "from-config"))
    paths = resolve_database_paths({"server_database_root": str(tmp_path / "from-cli")}, working_dir=tmp_path)
    assert paths.root == (tmp_path / "from-cli").resolve()
    assert BasicConfig.server_database_paths is paths


def test_config_root_wins_over_working_directory(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(BasicConfig, "server_database_root", str(tmp_path / "from-config"))
    paths = resolve_database_paths({"server_database_root": None}, working_dir=tmp_path / "work")
    assert paths.root == (tmp_path / "from-config").resolve()


def test_legacy_home_databases_keep_being_used_until_moved(_isolated: Path, tmp_path: Path) -> None:
    legacy = _isolated / ".unilabos"
    legacy.mkdir()
    (legacy / "materials.db").write_bytes(b"")
    work = tmp_path / "unilabos_data"

    assert configured_database_root({}, working_dir=work) == str(legacy)
    assert startup._legacy_warned == {str(legacy)}

    # 迁移完成（工作目录下已有数据库）后改用工作目录，旧位置不再生效。
    (work / "database").mkdir(parents=True)
    (work / "database" / "runtime.db").write_bytes(b"")
    assert configured_database_root({}, working_dir=work) == str(work / "database")


def test_unrelated_files_in_legacy_home_do_not_trigger_fallback(_isolated: Path, tmp_path: Path) -> None:
    legacy = _isolated / ".unilabos"
    legacy.mkdir()
    (legacy / "inventory.db").write_bytes(b"")  # 更早版本的其他库，不是四个微后端库
    assert configured_database_root({}, working_dir=tmp_path / "w") == str(tmp_path / "w" / "database")


def test_env_variable_overrides_config_field(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("UNILABOS_BASICCONFIG_SERVER_DATABASE_ROOT", str(tmp_path / "from-env"))
    config_module._update_config_from_env()
    assert BasicConfig.server_database_root == str(tmp_path / "from-env")
    assert configured_database_root({}, working_dir=tmp_path) == str(tmp_path / "from-env")
