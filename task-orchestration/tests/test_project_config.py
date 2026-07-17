"""项目测试命令配置。"""

from __future__ import annotations

import tomllib
from pathlib import Path


def test_pytest_configuration_adds_src_to_import_path():
    pyproject = Path(__file__).parents[1] / "pyproject.toml"

    config = tomllib.loads(pyproject.read_text(encoding="utf-8"))

    assert config["tool"]["pytest"]["ini_options"]["pythonpath"] == ["src"]
