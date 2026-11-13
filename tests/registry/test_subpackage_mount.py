"""``--devices`` 挂载驱动包里的一个家族子目录：模块路径按真实导入根推导，包级 YAML 注册表只取
该子目录里的条目。"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

from unilabos.registry.package_layout import find_import_root
from unilabos.registry.registry import Registry

_YAML = """
fam_a.yaml-dev:
  category: [fam_a]
  class:
    module: vendorpkg.devices.fam_a.driver:PlainA
    type: python
    action_value_mappings: {}
    status_types: {}
fam_b.yaml-dev:
  category: [fam_b]
  class:
    module: vendorpkg.devices.fam_b.driver:PlainB
    type: python
    action_value_mappings: {}
    status_types: {}
"""

_DRIVER = '''
from unilabos.registry.decorators import action, device

from vendorpkg.common import SCALE


class Plain{suffix}:
    def __init__(self, **kwargs):
        pass


@device(id="{family}_dev", category=["{family}"])
class Fam{suffix}:
    def __init__(self, **kwargs):
        self.scale = SCALE

    @action(description="ping")
    def ping(self) -> bool:
        return True
'''


def _write(path: Path, source: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(source), encoding="utf-8")


@pytest.fixture()
def vendor_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "fam-repo"
    _write(repo / "pyproject.toml", '[project]\nname = "fam-repo"\nversion = "0.1.0"\n')
    pkg = repo / "vendorpkg"
    _write(pkg / "__init__.py", "")
    _write(pkg / "common.py", "SCALE = 2\n")
    _write(pkg / "devices" / "__init__.py", "")
    for family, suffix in (("fam_a", "A"), ("fam_b", "B")):
        _write(pkg / "devices" / family / "__init__.py", "")
        _write(pkg / "devices" / family / "driver.py", _DRIVER.format(family=family, suffix=suffix))
    _write(pkg / "registry" / "devices" / "mixed.yaml", _YAML)
    return repo


def test_import_root_of_a_family_directory(vendor_repo: Path, tmp_path: Path) -> None:
    assert find_import_root(vendor_repo / "vendorpkg" / "devices" / "fam_a") == vendor_repo
    assert find_import_root(vendor_repo / "vendorpkg") == vendor_repo
    namespace_dir = tmp_path / "plain_dir"
    namespace_dir.mkdir()
    assert find_import_root(namespace_dir) == tmp_path


def test_family_mount_registers_importable_modules_and_its_own_yaml(vendor_repo: Path, monkeypatch) -> None:
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(sys, "modules", dict(sys.modules))
    registry = Registry.__wrapped__()  # 独立实例，不碰全局单例
    registry.setup(devices_dirs=[str(vendor_repo / "vendorpkg" / "devices" / "fam_a")], external_only=True)

    devices = registry.device_type_registry
    assert devices["fam_a_dev"]["class"]["module"] == "vendorpkg.devices.fam_a.driver:FamA"
    assert "fam_a.yaml-dev" in devices
    # 包级注册表里别的家族不随子目录挂载进来
    assert "fam_b_dev" not in devices and "fam_b.yaml-dev" not in devices
    assert registry.scan_errors == []
    assert registry.verify_and_resolve_registry() == []


def test_two_family_mounts_share_the_package_registry(vendor_repo: Path, monkeypatch) -> None:
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(sys, "modules", dict(sys.modules))
    registry = Registry.__wrapped__()
    devices_dir = vendor_repo / "vendorpkg" / "devices"
    registry.setup(devices_dirs=[str(devices_dir / "fam_a"), str(devices_dir / "fam_b")], external_only=True)
    assert {"fam_a_dev", "fam_b_dev", "fam_a.yaml-dev", "fam_b.yaml-dev"} <= set(registry.device_type_registry)


def test_whole_package_mount_is_unchanged(vendor_repo: Path, monkeypatch) -> None:
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(sys, "modules", dict(sys.modules))
    registry = Registry.__wrapped__()
    registry.setup(devices_dirs=[str(vendor_repo / "vendorpkg")], external_only=True)
    devices = registry.device_type_registry
    assert devices["fam_b_dev"]["class"]["module"] == "vendorpkg.devices.fam_b.driver:FamB"
    assert {"fam_a_dev", "fam_b_dev", "fam_a.yaml-dev", "fam_b.yaml-dev"} <= set(devices)
