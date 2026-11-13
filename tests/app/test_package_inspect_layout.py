"""``unilab package inspect`` 对标准仓库布局（``<repo>/pyproject.toml`` + ``<repo>/<包>/``）
推导的模块路径必须与运行时挂载一致，扫描失败的文件不能被静默丢弃。"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from unilabos.app.cli.package import PackageCLIError, inspect_package


def _write(path: Path, source: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(source), encoding="utf-8")


@pytest.fixture()
def driver_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "my-driver-repo"
    _write(
        repo / "pyproject.toml",
        """
        [project]
        name = "my-driver"
        version = "0.2.0"
        dependencies = []

        [tool.setuptools.packages.find]
        include = ["my_driver*"]
        """,
    )
    _write(repo / "my_driver" / "__init__.py", "")
    _write(
        repo / "my_driver" / "photometer.py",
        '''
        from __future__ import annotations

        from typing import Annotated, TypedDict

        from pydantic import Field

        from unilabos.registry.decorators import action, device


        class Reading(TypedDict):
            absorbance: float


        @device(id="layout_photometer", category=["layout"])
        class Photometer:
            @action(contract="typed", description="read")
            def read(self, wavelength_nm: Annotated[float, Field(ge=190, le=1100)] = 600.0) -> Reading:
                return Reading(absorbance=0.1)
        ''',
    )
    _write(
        repo / "my_driver" / "handlers" / "__init__.py",
        "",
    )
    _write(
        repo / "my_driver" / "handlers" / "liquid.py",
        '''
        from unilabos.registry.decorators import action, device


        @device(id="layout_handler", category=["layout"])
        class LiquidHandler:
            @action(description="transfer")
            def transfer(self, volume: float) -> dict:
                return {}
        ''',
    )
    # 仓库根目录的测试 / 脚本不属于包：不参与扫描
    _write(repo / "tests" / "test_driver.py", "def test_nothing():\n    pass\n")
    return repo


def test_inspect_reports_importable_module_paths(driver_repo: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    info = inspect_package(str(driver_repo), out_dir=str(out))

    resources = {item["id"]: item for item in json.loads((out / "resources.json").read_text(encoding="utf-8"))}
    assert set(resources) == {"layout_photometer", "layout_handler"}
    assert resources["layout_photometer"]["class"]["module"] == "my_driver.photometer:Photometer"
    assert resources["layout_handler"]["class"]["module"] == "my_driver.handlers.liquid:LiquidHandler"
    assert sorted(info["devices"]) == ["layout_handler", "layout_photometer"]


def test_inspect_writes_into_the_repository_dist_by_default(driver_repo: Path) -> None:
    info = inspect_package(str(driver_repo))
    archive = Path(info["archive_path"])
    assert archive.parent == driver_repo / "dist"
    assert not (driver_repo.parent / "dist").exists()


def test_inspect_fails_when_a_driver_file_cannot_be_scanned(driver_repo: Path, tmp_path: Path) -> None:
    _write(
        driver_repo / "my_driver" / "broken.py",
        '''
        from unilabos.registry.decorators import action, device


        @device(id="layout_broken", category=["layout"])
        class Broken:
            @action(contract="typed", action_type="SendCmd")
            def run(self) -> None:
                return None
        ''',
    )
    with pytest.raises(PackageCLIError, match="broken.py"):
        inspect_package(str(driver_repo), out_dir=str(tmp_path / "out"))
