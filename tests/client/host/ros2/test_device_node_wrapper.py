"""ROS 2 设备包装：按公共 property 补全状态与 ``set_`` 动作映射，字符串形式的注解也要解析成类型。

驱动模块常用 ``from __future__ import annotations``（本模块同样启用），property 的返回注解是字符串；
修复前包装器把字符串直接交给 ROS 类型映射，``virtual_transfer_pump`` 因此在 ROS 2 上起不来。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

wrapper = pytest.importorskip(
    "unilabos.client.host.ros2.device_node_wrapper",
    reason="ROS 2 设备包装依赖 rclpy 与 Jazzy 版 unilabos_msgs",
    exc_type=ImportError,
)
from unilabos.client.host.ros2.msgs.message_converter import get_action_type, get_msg_type  # noqa: E402


class FutureAnnotatedDriver:
    def __init__(self) -> None:
        self._speed = 1.0
        self._label = ""

    @property
    def speed(self) -> float:
        return self._speed

    def set_speed(self, speed: float) -> None:
        self._speed = float(speed)

    @property
    def label(self) -> str:
        return self._label

    def get_label(self) -> str:
        return self._label


def _device_config() -> SimpleNamespace:
    return SimpleNamespace(res_content=SimpleNamespace(template_name="probe"), children=[])


def test_string_property_annotations_resolve_to_ros_types() -> None:
    assert FutureAnnotatedDriver.speed.fget.__annotations__["return"] == "float"
    status_types: dict = {}
    mappings: dict = {}

    wrapper.ros2_device_node(
        FutureAnnotatedDriver,
        device_config=_device_config(),
        status_types=status_types,
        action_value_mappings=mappings,
    )

    assert mappings["set_speed"] == get_action_type(float)
    assert status_types["label"] == get_msg_type(str)


def test_virtual_transfer_pump_builds_its_ros_wrapper() -> None:
    """注册表把 set_max_velocity 登记成 auto-set_max_velocity；包装器按 float property 补出 set_max_velocity。"""

    from unilabos.devices.virtual.virtual_transferpump import VirtualTransferPump

    mappings = {"auto-set_max_velocity": {"type": "UniLabJsonCommand"}}
    node_type = wrapper.ros2_device_node(
        VirtualTransferPump,
        device_config=_device_config(),
        status_types={},
        action_value_mappings=mappings,
    )

    assert node_type.__name__ == "ROS2NodeWrapper4VirtualTransferPump"
    assert mappings["set_max_velocity"] == get_action_type(float)
