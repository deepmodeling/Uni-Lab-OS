"""设备组合根接入 ROS 动作传输适配器（ROS Action Transport Adapter）的测试。"""

from types import SimpleNamespace
from typing import Any

from unilabos.package_manager.driver_runtime import PythonDriverActivation
from unilabos.ros import initialize_device
from unilabos_msgs.action import StrSingleInput


def test_device_initialization_wraps_runtime_endpoints_without_polluting_activation(
    monkeypatch,
) -> None:
    """设备包装只消费补齐后的运行时副本，驱动激活结果继续保持公开合同。

    参数：``monkeypatch`` 替换驱动激活和 ROS 包装边界，避免启动真实 ROS 节点。
    返回：无；断言包装器获得内部端点，激活结果和公开动作映射仍无内部实现项。
    异常：组合根绕过传输适配器或反向修改激活结果时触发断言失败。
    """

    class Driver:
        """代表从包工作区加载的最小 Python 设备驱动。"""

    # ``public_mappings`` 是包目录（Package Catalog）发布的业务动作合同。
    public_mappings = {
        "scan": {
            "type": "UniLabJsonCommand",
            "schema": {"type": "object"},
        }
    }
    # ``activation`` 是驱动运行时（Driver Runtime）的不可变激活结果。
    activation = PythonDriverActivation(
        definition_identity="community.szlab.scanner",
        source_identity="szlab.scanner:Scanner",
        content_hash="sha256:" + "1" * 64,
        package_catalog_digest="sha256:" + "2" * 64,
        driver_class=Driver,
        driver_params={},
        status_types={},
        action_value_mappings=public_mappings,
        hardware_interface={},
        driver_is_ros=False,
    )
    # ``wrapped_contracts`` 收集组合根传入 ROS 包装器的动作映射。
    wrapped_contracts: list[dict[str, Any]] = []

    def activate(*_args: Any, **_kwargs: Any) -> PythonDriverActivation:
        """返回固定驱动激活结果。

        参数：忽略组合根传入的注册表（Registry）、定义身份、配置和加载器。
        返回：预先构造的不可变驱动激活结果。
        异常：无。
        """

        return activation

    def wrap_driver(
        driver_class: type[Any],
        **options: Any,
    ) -> type[Any]:
        """记录 ROS 包装合同并返回可实例化的最小包装类。

        参数：``driver_class`` 是选中的驱动类；``options`` 是状态、动作与硬件合同。
        返回：保存实例化关键字参数的最小包装类。
        异常：动作映射缺失时由直接索引传播 ``KeyError``。
        """

        assert driver_class is Driver
        wrapped_contracts.append(options["action_value_mappings"])

        class Wrapped:
            """记录组合根传递的设备实例参数。"""

            def __init__(self, **instance_options: Any) -> None:
                """保存设备实例参数。

                参数：``instance_options`` 是设备身份、UUID 和驱动配置。
                返回：无。
                异常：无。
                """

                self.instance_options = instance_options

        return Wrapped

    monkeypatch.setattr(initialize_device, "activate_python_driver", activate)
    monkeypatch.setattr(initialize_device, "ros2_device_node", wrap_driver)
    # ``device_config`` 是物理资源图（Physical Resource Graph）的设备实例。
    device_config = SimpleNamespace(
        res_content=SimpleNamespace(
            klass="community.szlab.scanner",
            uuid="72000000-0000-4000-8000-000000000007",
            config={},
        )
    )

    initialized = initialize_device.initialize_device_from_dict(
        "szlab_scanner",
        device_config,
    )

    assert initialized is not None
    assert len(wrapped_contracts) == 1
    assert (
        wrapped_contracts[0]["_execute_driver_command"]["type"]
        is StrSingleInput
    )
    assert activation.action_value_mappings == public_mappings
    assert "_execute_driver_command" not in activation.action_value_mappings
