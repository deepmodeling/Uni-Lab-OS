from typing import Optional

from unilabos.backend.runtime.definition import resolve_device_definition
from unilabos.registry.registry import lab_registry  # noqa: F401 - 兼容嵌入方/测试
from unilabos.backend.ros2.device_node_wrapper import ros2_device_node
from unilabos.backend.ros2.base_device_node import ROS2DeviceNode, DeviceInitError
from unilabos.resources.resource_tracker import ResourceDictInstance
from unilabos.backend.runtime.exception import DeviceClassInvalid  # noqa: F401 - 兼容公开属性


def initialize_device_from_dict(device_id, device_config: ResourceDictInstance) -> Optional[ROS2DeviceNode]:
    """Initializes a device based on its configuration.

    This function dynamically imports the appropriate device class and creates an
    instance of it using the provided device configuration.
    It also sets up action clients for the device based on its action value mappings.

    Args:
        device_id (str): The unique identifier for the device.
        device_config (dict): The configuration dictionary for the device.

    Returns:
        None
    """
    definition = resolve_device_definition(
        device_id,
        device_config,
        backend_name="ros2",
    )
    # 不管是 ROS2 驱动还是 Python 驱动，都统一包装为设备节点（HostNode 除外）。
    device_node_type = ros2_device_node(
        definition.driver_class,
        status_types=definition.status_types,
        device_config=device_config,
        action_value_mappings=definition.action_value_mappings,
        hardware_interface=definition.hardware_interface,
    )
    try:
        return device_node_type(
            device_id=device_id,
            resource_uuid=definition.resource_uuid,
            driver_is_ros=definition.is_native_ros,
            driver_params=definition.runtime_config,
        )
    except DeviceInitError:
        return None
