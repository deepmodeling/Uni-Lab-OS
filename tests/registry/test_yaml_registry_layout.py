"""设备 YAML 注册表可以按子目录分组；声明了 ``category`` 的条目不再被文件名改写分类。"""

from __future__ import annotations

import sys
import textwrap
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from unilabos.registry.registry import Registry

_DEVICE = """
{device_id}:
  {category}
  description: layout test
  class:
    module: tests.registry.fixtures.initializer_drivers:SharedDevice
    type: python
    action_value_mappings: {{}}
    status_types: {{}}
"""

_RESOURCE = """
{resource_id}:
  {category}
  class:
    module: unilabos.resources.presets.container:get_regular_container
    type: pylabrobot
"""


def _write(path: Path, source: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(source), encoding="utf-8")


def _registry() -> Registry:
    registry = Registry.__wrapped__()  # 独立实例，不碰全局单例
    registry._startup_executor = ThreadPoolExecutor(max_workers=2)
    return registry


def test_device_yaml_in_subdirectories_keeps_declared_categories(tmp_path: Path) -> None:
    root = tmp_path / "registry"
    _write(root / "devices" / "analytical" / "pumps.yaml", _DEVICE.format(device_id="layout.hplc-a", category="category: [hplc]"))
    _write(root / "devices" / "lab_basics" / "misc.yaml", _DEVICE.format(device_id="layout.misc", category=""))
    _write(root / "devices" / "flat.yaml", _DEVICE.format(device_id="layout.flat", category="category: [flat, extra]"))
    _write(root / "device_comms" / "bus" / "serial.yaml", _DEVICE.format(device_id="layout.bus", category=""))

    registry = _registry()
    registry.load_device_types(root)

    devices = registry.device_type_registry
    assert devices["layout.hplc-a"]["category"] == ["hplc"]
    # 没有声明分类时仍以文件名兜底
    assert devices["layout.misc"]["category"] == ["misc"]
    assert devices["layout.flat"]["category"] == ["flat", "extra"]
    assert devices["layout.bus"]["category"] == ["serial"]


def test_resource_yaml_keeps_declared_categories(tmp_path: Path) -> None:
    root = tmp_path / "registry"
    _write(root / "resources" / "vendor" / "plates.yaml", _RESOURCE.format(resource_id="layout_plate", category="category: [plate]"))
    _write(root / "resources" / "vendor" / "tubes.yaml", _RESOURCE.format(resource_id="layout_tube", category=""))

    registry = _registry()
    registry.load_resource_types(root, upload_registry=False)

    assert registry.resource_type_registry["layout_plate"]["category"] == ["plate"]
    assert registry.resource_type_registry["layout_tube"]["category"] == ["tubes"]


def test_mounted_python_package_does_not_load_nested_driver_yaml(tmp_path: Path, monkeypatch) -> None:
    """``--devices`` 目录本身兜底当注册表根时只读 ``devices/*.yaml`` 顶层：驱动代码目录里的配置 YAML
    不能被当成设备注册表（更不能在 check_mode 下被补全写回）。"""

    monkeypatch.setattr(sys, "path", list(sys.path))
    pkg = tmp_path / "layout_pkg"
    _write(pkg / "__init__.py", "")
    _write(pkg / "devices" / "__init__.py", "")
    _write(pkg / "devices" / "flat.yaml", _DEVICE.format(device_id="layout.pkg_flat", category="category: [flat]"))
    config = pkg / "devices" / "vendor" / "settings.yaml"
    _write(config, "station:\n  host: 127.0.0.1\n")
    _write(pkg / "registry" / "devices" / "family" / "nested.yaml", _DEVICE.format(device_id="layout.pkg_nested", category="category: [nested]"))
    before = config.read_text(encoding="utf-8")

    registry = _registry()
    registry._load_devices_dir_registries([str(pkg)], complete_registry=True)

    assert "layout.pkg_flat" in registry.device_type_registry
    assert "layout.pkg_nested" in registry.device_type_registry
    assert "station" not in registry.device_type_registry
    assert config.read_text(encoding="utf-8") == before
