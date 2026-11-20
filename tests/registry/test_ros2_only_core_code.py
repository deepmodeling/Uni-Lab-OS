"""backend 不是 ros2 时，建注册表不碰 ROS 2 专用核心代码：``devices/ros_dev`` 整个不扫描，ROS 2 transport
里的模块一个也不导入，``--check_mode`` 也不校验它们；不报错，也不提示。

以前没装 ROS 的环境建注册表时，内置 ``camera`` 设备（``client/host/ros2/presets/camera.py``）的入参类型
解析会导入 ``base_device_node``，打出缺少 rclpy 的错误和整段回溯。``camera`` 与 YAML 声明的 ``serial``
照常登记：HostLink 据它们的 ``supported_backends`` 把图里这类节点标为不可用，缺条目则会让启动失败。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from unilabos.registry import ast_registry_scanner
from unilabos.registry import registry as registry_module
from unilabos.registry.registry import Registry
from unilabos.utils import import_manager

#: ROS 2 专用核心包
ROS2_ONLY_PACKAGES = ("unilabos.client.host.ros2", "unilabos.devices.ros_dev")
#: 核心注册表里类在这些包中的设备（AST 扫描的 camera、device_comms YAML 的 serial）
ROS2_ONLY_DEVICES = {"camera", "serial"}
ROS_DEV_DIR = Path(registry_module.__file__).resolve().parents[1] / "devices" / "ros_dev"


def _in_ros2_core(module: str) -> bool:
    name = module.split(":", 1)[0]
    return any(name == prefix or name.startswith(prefix + ".") for prefix in ROS2_ONLY_PACKAGES)


class _Build:
    """用独立实例建核心注册表（不碰全局单例），记下解析过的文件和每一次模块导入尝试。"""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.parsed: list[Path] = []
        self.imported: list[str] = []
        parse_file = ast_registry_scanner._parse_file
        load_module = import_manager.ImportManager.load_module

        def parsing(filepath, *args, **kwargs):
            self.parsed.append(Path(filepath).resolve())
            return parse_file(filepath, *args, **kwargs)

        def loading(manager, module_path, *args, **kwargs):
            self.imported.append(module_path)
            return load_module(manager, module_path, *args, **kwargs)

        monkeypatch.setattr(ast_registry_scanner, "_parse_file", parsing)
        monkeypatch.setattr(import_manager.ImportManager, "load_module", loading)
        monkeypatch.setattr(sys, "path", list(sys.path))

    def setup(self, backend=None) -> Registry:
        registry = Registry.__wrapped__()
        # 库调用不传 backend：这就是「不知道 backend」
        registry.setup() if backend is None else registry.setup(backend=backend)
        return registry

    def scanned_ros_dev(self) -> bool:
        return any(ROS_DEV_DIR in path.parents for path in self.parsed)

    def ros2_core_imports(self) -> list[str]:
        return [module for module in self.imported if _in_ros2_core(module)]


def test_non_ros2_backend_never_touches_ros2_only_core_code(monkeypatch: pytest.MonkeyPatch) -> None:
    build = _Build(monkeypatch)
    registry = build.setup("hostlink")

    assert not build.scanned_ros_dev()
    assert build.ros2_core_imports() == []
    assert registry.scan_errors == [] and registry.yaml_errors == []
    # 仍然登记：HostLink 把图里这类节点标为不可用，而不是因注册表缺条目启动失败
    assert ROS2_ONLY_DEVICES <= set(registry.device_type_registry)
    assert registry.device_type_registry["camera"]["class"]["supported_backends"] == ["ros2"]
    config = registry.device_type_registry["camera"]["init_param_schema"]["config"]
    assert config["properties"]["resource_tracker"]["type"] == "object"

    # --check_mode 的导入校验同样不碰它们，也不记成「跳过」
    registry.device_type_registry = {
        device_id: registry.device_type_registry[device_id] for device_id in ROS2_ONLY_DEVICES
    }
    registry.resource_type_registry = {}
    assert registry.verify_and_resolve_registry() == []
    assert registry.verify_skipped == []
    assert build.ros2_core_imports() == []


def test_ros2_backend_scans_and_registers_ros2_only_code(monkeypatch: pytest.MonkeyPatch) -> None:
    build = _Build(monkeypatch)
    registry = build.setup("ros2")

    assert build.scanned_ros_dev()
    assert ROS2_ONLY_DEVICES <= set(registry.device_type_registry)


@pytest.mark.parametrize("installed", [False, True])
def test_unknown_backend_follows_the_ros2_installation(monkeypatch: pytest.MonkeyPatch, installed: bool) -> None:
    """库调用与 --check_mode 不知道 backend：装了 ROS 2 就照常扫描、导入、校验（CI 的 ROS 注册表检查
    照常覆盖这些设备），没装就静默跳过。"""

    monkeypatch.setattr(registry_module, "_ros2_installed", lambda: installed, raising=False)
    build = _Build(monkeypatch)
    registry = build.setup()

    assert build.scanned_ros_dev() is installed
    if not installed:
        assert build.ros2_core_imports() == []
    assert ROS2_ONLY_DEVICES <= set(registry.device_type_registry)
    enabled = registry_module.ros2_core_enabled
    assert enabled(None) is installed
    assert enabled("hostlink") is False and enabled("ros2") is True
    assert set(registry_module.ROS2_ONLY_CORE_MODULES) == set(ROS2_ONLY_PACKAGES)
