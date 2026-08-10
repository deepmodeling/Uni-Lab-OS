import inspect

from unilabos.device_runtime import DeviceNode
from unilabos.ros.nodes.base_device_node import BaseROS2DeviceNode


def test_ros2_node_implements_backend_neutral_contract() -> None:
    assert issubclass(BaseROS2DeviceNode, DeviceNode)
    assert BaseROS2DeviceNode.backend_name == "ros2"
    assert not inspect.iscoroutinefunction(BaseROS2DeviceNode.create_task)
