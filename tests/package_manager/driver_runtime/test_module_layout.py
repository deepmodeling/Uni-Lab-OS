"""驱动运行时（Driver Runtime）模块依赖方向与组合根合同。"""

from __future__ import annotations

import ast
import builtins
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from unilabos.package_manager import WorkspaceSource, compile_package_source
from unilabos.package_manager.driver_runtime import (
    DriverActivationError,
    PythonDriverActivation,
)
from unilabos.ros.nodes.base_device_node import DeviceInitError
from unilabos.utils.exception import DeviceClassInvalid

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


def _imports(path: Path) -> set[str]:
    """提取 Python 文件中的静态导入模块集合。

    参数：``path`` 是待审计源码文件。
    返回：包含 ``import`` 与 ``from ... import`` 根路径的集合。
    异常：文件不可读或语法无效时传播原始异常。
    """

    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(("." * node.level) + (node.module or ""))
    return imported


def test_driver_runtime_has_no_forbidden_runtime_dependencies() -> None:
    """驱动运行时不得依赖工作区、分发、ROS、调度、库存或 Backend。

    参数：无。
    返回：无；断言三个规范模块的静态导入不越过已接受接缝。
    异常：出现反向依赖或设备运行职责泄漏时断言失败。
    """

    driver_root = REPOSITORY_ROOT / "unilabos/package_manager/driver_runtime"
    forbidden_fragments = (
        "workspace_runtime",
        "package_distribution",
        "unilabos.ros",
        "scheduler",
        "inventory",
        "backend",
    )
    imported = {item for path in driver_root.glob("*.py") for item in _imports(path)}

    assert not {
        item
        for item in imported
        if any(fragment in item for fragment in forbidden_fragments)
    }


def test_driver_runtime_cold_import_keeps_higher_layers_and_author_code_unloaded(
    tmp_path: Path,
) -> None:
    """隔离解释器冷导入驱动运行时只加载该层，不加载高层或作者驱动。

    参数：``tmp_path`` 提供一个位于 ``PYTHONPATH``、但绝不应执行的作者模块。
    返回：无；断言包分发、工作区运行时和作者模块均未进入冷进程模块表。
    异常：子进程导入失败、惰性门面退化或作者副作用执行时断言失败。
    """

    author_module = tmp_path / "phase4_author_driver.py"
    author_module.write_text(
        "import builtins\n"
        "builtins._phase4_author_driver_executed = True\n"
        "class Driver:\n"
        "    pass\n",
        encoding="utf-8",
    )
    # ``probe`` 是全新解释器执行的冷导入审计，不受 pytest 模块缓存影响。
    probe = """
import builtins
import json
import sys

import unilabos.package_manager.driver_runtime

print(json.dumps({
    "package_distribution": "unilabos.package_manager.package_distribution" in sys.modules,
    "workspace_runtime": "unilabos.package_manager.workspace_runtime" in sys.modules,
    "author_module": "phase4_author_driver" in sys.modules,
    "author_side_effect": hasattr(builtins, "_phase4_author_driver_executed"),
}))
"""
    environment = dict(os.environ)
    existing_pythonpath = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = os.pathsep.join(
        item
        for item in (str(tmp_path), str(REPOSITORY_ROOT), existing_pythonpath)
        if item
    )

    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPOSITORY_ROOT,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip()) == {
        "package_distribution": False,
        "workspace_runtime": False,
        "author_module": False,
        "author_side_effect": False,
    }


def test_initialize_device_delegates_resolution_loading_and_merging_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ROS 组合根只委派驱动激活，并消费一次完整激活结果。

    参数：``monkeypatch`` 替换驱动激活与 ROS 包装器以隔离硬件。
    返回：无；断言定义解析、加载和配置合并不再由组合根重复实现。
    异常：组合根绕过新模块或改变实例参数时断言失败。
    """

    initialize_device = importlib.import_module("unilabos.ros.initialize_device")
    activation_requests: list[tuple[Any, str, Any, Any]] = []

    class SelectedDriver:
        """记录 ROS 组合根最终实例化参数的测试驱动。"""

        def __init__(self, **kwargs: Any) -> None:
            """保存组合根传入的设备运行参数。

            参数：``kwargs`` 是设备身份、UUID、驱动类型和配置。
            返回：无。
            异常：无。
            """

            self.kwargs = kwargs

    def activate(
        registry: Any,
        definition_identity: str,
        runtime_config: Any,
        *,
        loader: Any,
    ) -> PythonDriverActivation:
        """记录组合根的唯一激活委派并返回固定结果。

        参数：``registry`` 是实时注册表；``definition_identity`` 是图中设备定义；
        ``runtime_config`` 是设备实例配置；``loader`` 是动态类加载 Adapter。
        返回：固定的 Python 驱动激活结果。
        异常：无。
        """

        activation_requests.append(
            (registry, definition_identity, runtime_config, loader)
        )
        return PythonDriverActivation(
            definition_identity="community.demo.heater",
            source_identity="demo.heater:Heater",
            content_hash="sha256:" + "1" * 64,
            package_catalog_digest="sha256:" + "2" * 64,
            driver_class=SelectedDriver,
            driver_params={"port": "COM1"},
            status_types={"temperature": "float"},
            action_value_mappings={"heat": {}},
            hardware_interface={"name": "hardware_interface"},
            driver_is_ros=False,
        )

    def preserve_driver(
        driver: type[SelectedDriver], **_options: Any
    ) -> type[SelectedDriver]:
        """返回未改变的测试驱动类。

        参数：``driver`` 是激活结果驱动类；``_options`` 是 ROS 包装合同。
        返回：原测试驱动类。
        异常：无。
        """

        return driver

    monkeypatch.setattr(initialize_device, "activate_python_driver", activate)
    monkeypatch.setattr(initialize_device, "ros2_device_node", preserve_driver)
    # ``device_config`` 是物理图中的具体设备实例配置。
    device_config = SimpleNamespace(
        res_content=SimpleNamespace(
            klass="heater",
            uuid="72000000-0000-4000-8000-000000000001",
            config={"port": "ignored-by-test-activation"},
        )
    )

    initialized = initialize_device.initialize_device_from_dict(
        "heater-a",
        device_config,
    )

    assert len(activation_requests) == 1
    assert activation_requests[0][1:3] == (
        "heater",
        {"port": "ignored-by-test-activation"},
    )
    assert initialized.kwargs == {
        "device_id": "heater-a",
        "device_uuid": "72000000-0000-4000-8000-000000000001",
        "driver_is_ros": False,
        "driver_params": {"port": "COM1"},
    }


def test_initialize_device_maps_activation_error_to_device_class_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ROS 组合根把稳定激活错误映射为既有 ``DeviceClassInvalid``。

    参数：``monkeypatch`` 让驱动运行时返回固定关闭式错误。
    返回：无；断言既有调用方异常类型保留，且稳定激活错误仍是 cause。
    异常：若兼容异常类型或 cause 链丢失则断言失败。
    """

    initialize_device = importlib.import_module("unilabos.ros.initialize_device")
    activation_error = DriverActivationError(
        "package_evidence_incomplete",
        "community.demo.heater",
        "包证据不完整",
    )

    def fail_activation(
        _registry: Any,
        _definition_identity: str,
        _runtime_config: Any,
        *,
        loader: Any,
    ) -> PythonDriverActivation:
        """模拟驱动运行时关闭式失败。

        参数：前三项是组合根输入；``loader`` 是不会被调用的类加载 Adapter。
        返回：不会返回。
        异常：始终抛出固定 ``DriverActivationError``。
        """

        del loader
        raise activation_error

    monkeypatch.setattr(initialize_device, "activate_python_driver", fail_activation)
    # ``device_config`` 是携带非法包定义选择的设备实例。
    device_config = SimpleNamespace(
        res_content=SimpleNamespace(
            klass="community.demo.heater",
            uuid="72000000-0000-4000-8000-000000000001",
            config={},
        )
    )

    with pytest.raises(DeviceClassInvalid) as caught:
        initialize_device.initialize_device_from_dict("heater-a", device_config)

    assert caught.value.__cause__ is activation_error


def test_initialize_device_keeps_device_init_error_empty_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """驱动构造器的 ``DeviceInitError`` 继续返回空结果供既有启动流程处理。

    参数：``monkeypatch`` 提供固定激活结果和抛错包装驱动。
    返回：无；断言公开初始化函数返回 ``None``。
    异常：若既有 ``DeviceInitError`` 行为改变则断言失败。
    """

    initialize_device = importlib.import_module("unilabos.ros.initialize_device")

    class FailingDriver:
        """实例化时报告既有设备初始化错误的测试驱动。"""

        def __init__(self, **_kwargs: Any) -> None:
            """模拟驱动构造期间无法连接设备。

            参数：``_kwargs`` 是组合根传入的设备运行参数。
            返回：不会返回。
            异常：始终抛出 ``DeviceInitError``。
            """

            raise DeviceInitError("offline")

    def activate(
        _registry: Any,
        _definition_identity: str,
        _runtime_config: Any,
        *,
        loader: Any,
    ) -> PythonDriverActivation:
        """返回触发既有构造错误的固定激活结果。

        参数：前三项是组合根输入；``loader`` 是本测试不调用的加载 Adapter。
        返回：包含 ``FailingDriver`` 的激活结果。
        异常：无。
        """

        del loader
        return PythonDriverActivation(
            definition_identity="builtin-heater",
            source_identity="demo.heater:Heater",
            content_hash=None,
            package_catalog_digest=None,
            driver_class=FailingDriver,
            driver_params={},
            status_types={},
            action_value_mappings={},
            hardware_interface={"name": "hardware_interface"},
            driver_is_ros=False,
        )

    def preserve_driver(
        driver: type[FailingDriver],
        **_options: Any,
    ) -> type[FailingDriver]:
        """保留抛出既有初始化错误的测试驱动。

        参数：``driver`` 是固定测试驱动；``_options`` 是 ROS 包装合同。
        返回：未改变的测试驱动。
        异常：无。
        """

        return driver

    monkeypatch.setattr(initialize_device, "activate_python_driver", activate)
    monkeypatch.setattr(initialize_device, "ros2_device_node", preserve_driver)
    # ``device_config`` 是本例的内置设备实例。
    device_config = SimpleNamespace(
        res_content=SimpleNamespace(
            klass="builtin-heater",
            uuid="72000000-0000-4000-8000-000000000001",
            config={},
        )
    )

    assert (
        initialize_device.initialize_device_from_dict("heater-a", device_config) is None
    )


def test_package_import_and_catalog_compile_do_not_execute_author_driver_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """导入包管理与静态编译包目录不得执行作者驱动模块。

    参数：``tmp_path`` 提供隔离工作区；``monkeypatch`` 清理作者代码副作用标记。
    返回：无；断言只有显式驱动激活才可能加载作者实现。
    异常：静态发现阶段执行作者代码时断言失败。
    """

    package_root = tmp_path / "safe_compile_lab"
    package_root.mkdir()
    package_root.joinpath("__init__.py").write_text("", encoding="utf-8")
    package_root.joinpath("driver.py").write_text(
        "import builtins\n"
        "from unilabos.registry.decorators import device\n"
        "builtins._driver_runtime_compile_executed_author_code = True\n"
        '@device(id="heater", category=["test"])\n'
        "class Heater:\n"
        "    pass\n",
        encoding="utf-8",
    )
    tmp_path.joinpath("pyproject.toml").write_text(
        '[project]\nname = "safe-compile-lab"\nversion = "1.0.0"\n',
        encoding="utf-8",
    )
    tmp_path.joinpath("package.yaml").write_text(
        "package:\n  name: safe_compile_lab\nworkflows: []\n",
        encoding="utf-8",
    )
    monkeypatch.delattr(
        builtins,
        "_driver_runtime_compile_executed_author_code",
        raising=False,
    )

    importlib.import_module("unilabos.package_manager")
    catalog = compile_package_source(WorkspaceSource(tmp_path))

    assert [item.id for item in catalog.definitions.devices] == ["heater"]
    assert not hasattr(builtins, "_driver_runtime_compile_executed_author_code")
