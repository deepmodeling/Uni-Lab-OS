"""Python 驱动激活结果模型的不可变与隔离合同。"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from unilabos.package_manager.driver_runtime import PythonDriverActivation


class _Driver:
    """不接触硬件的驱动类身份。"""


def test_python_driver_activation_is_frozen_and_isolates_mutable_configuration() -> (
    None
):
    """激活结果不可重新赋值，且构造后不共享调用方的可变配置。

    参数：无；测试直接构造一份 Python 驱动激活结果。
    返回：无；断言模型字段冻结，嵌套驱动参数与映射均已隔离复制。
    异常：若模型允许重新赋值或外部修改污染结果，则断言失败。
    """

    # ``driver_params`` 是传入设备实例构造器的运行配置，必须与输入容器隔离。
    driver_params = {"transport": {"port": "COM1"}}
    # ``status_types`` 是驱动状态字段合同，不允许被注册表原容器后续修改污染。
    status_types = {"temperature": "float"}
    activation = PythonDriverActivation(
        definition_identity="community.demo.heater",
        source_identity="demo.heater:Heater",
        content_hash="sha256:" + "1" * 64,
        package_catalog_digest="sha256:" + "2" * 64,
        driver_class=_Driver,
        driver_params=driver_params,
        status_types=status_types,
        action_value_mappings={"heat": {"goal": {"value": "value"}}},
        hardware_interface={"name": "hardware_interface"},
        driver_is_ros=False,
    )

    driver_params["transport"]["port"] = "COM9"
    status_types["temperature"] = "string"

    assert activation.driver_params == {"transport": {"port": "COM1"}}
    assert activation.status_types == {"temperature": "float"}
    with pytest.raises(FrozenInstanceError):
        activation.definition_identity = "changed"  # type: ignore[misc]


def test_python_driver_activation_returns_fresh_mutable_views() -> None:
    """调用方可使用配置副本，但不能反向修改激活结果中的稳定证据。

    参数：无；测试构造最小内置驱动激活结果。
    返回：无；断言两次读取配置得到内容相同、容器独立的普通字典。
    异常：若返回共享容器或只读代理破坏驱动兼容性，则断言失败。
    """

    activation = PythonDriverActivation(
        definition_identity="builtin-heater",
        source_identity="demo.heater:Heater",
        content_hash=None,
        package_catalog_digest=None,
        driver_class=_Driver,
        driver_params={"limits": [1, 2]},
        status_types={},
        action_value_mappings={},
        hardware_interface={"name": "hardware_interface"},
        driver_is_ros=False,
    )

    # ``first_view`` 是交给一个驱动实例的配置副本；修改不能泄漏到后续实例。
    first_view = activation.driver_params
    first_view["limits"].append(3)

    assert activation.driver_params == {"limits": [1, 2]}
    assert isinstance(first_view, dict)
