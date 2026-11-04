"""工作目录解析：默认 ~/.unilabos；.unilabos / unilabos_data 直接使用或被识别为子目录。"""

from __future__ import annotations

from pathlib import Path

import pytest

from unilabos.config.working_dir import resolve_working_dir


@pytest.fixture(autouse=True)
def home(monkeypatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


def test_default_is_home_unilabos(home: Path, tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    assert resolve_working_dir(cwd=str(project)) == str(home / ".unilabos")


@pytest.mark.parametrize("name", ["unilabos_data", ".unilabos"])
def test_directory_with_known_name_is_used_directly(tmp_path: Path, name: str) -> None:
    folder = tmp_path / "lab" / name
    folder.mkdir(parents=True)
    assert resolve_working_dir(cwd=str(folder)) == str(folder)
    assert resolve_working_dir(str(folder)) == str(folder)


@pytest.mark.parametrize("name", ["unilabos_data", ".unilabos"])
def test_existing_subdirectory_is_adopted(tmp_path: Path, name: str) -> None:
    project = tmp_path / "project"
    (project / name).mkdir(parents=True)
    assert resolve_working_dir(cwd=str(project)) == str(project / name)
    assert resolve_working_dir(str(project)) == str(project / name)


def test_legacy_unilabos_data_wins_over_dot_unilabos(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / "unilabos_data").mkdir(parents=True)
    (project / ".unilabos").mkdir()
    assert resolve_working_dir(cwd=str(project)) == str(project / "unilabos_data")


def test_explicit_working_dir_is_used_as_is(tmp_path: Path) -> None:
    custom = tmp_path / "labs" / "lab_a"
    assert resolve_working_dir(str(custom)) == str(custom)  # 不存在也原样使用，由首次启动创建


def test_config_parent_is_used(tmp_path: Path) -> None:
    config = tmp_path / "lab_b" / "local_config.py"
    config.parent.mkdir()
    config.write_text("", encoding="utf-8")
    assert resolve_working_dir(None, str(config)) == str(config.parent)


def test_missing_config_falls_back_to_cwd_rules(home: Path, tmp_path: Path) -> None:
    project = tmp_path / "p"
    project.mkdir()
    assert resolve_working_dir(None, str(tmp_path / "missing.py"), cwd=str(project)) == str(home / ".unilabos")


def test_home_itself_resolves_to_dot_unilabos(home: Path) -> None:
    (home / ".unilabos").mkdir()
    assert resolve_working_dir(cwd=str(home)) == str(home / ".unilabos")


def test_tilde_in_explicit_path_is_expanded(home: Path) -> None:
    assert resolve_working_dir("~/labs/x") == str(home / "labs" / "x")
