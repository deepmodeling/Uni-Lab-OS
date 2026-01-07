from typing import Dict, Any, Optional, Type, TypeVar

from unilabos.ros.msgs.message_converter import (
    get_msg_type,
    get_action_type,
)
from unilabos.ros.nodes.base_device_node import init_wrapper, ROS2DeviceNode
from unilabos.resources.resource_tracker import ResourceDictInstance

# 定义泛型类型变量
T = TypeVar("T")


# noinspection PyMissingConstructor
class ROS2DeviceNodeWrapper(ROS2DeviceNode):
    def __init__(self, device_id: str, *args, **kwargs):
        pass


def ros2_device_node(
    cls: Type[T],
    device_config: Optional[ResourceDictInstance] = None,
    status_types: Optional[Dict[str, Any]] = None,
    action_value_mappings: Optional[Dict[str, Any]] = None,
    hardware_interface: Optional[Dict[str, Any]] = None,
    print_publish: bool = False,
) -> Type[ROS2DeviceNodeWrapper]:
    """Create a ROS2 Node class for a device class with properties and actions.

    Args:
        cls: 要封装的设备类
        status_types: 需要发布的状态和传感器信息，每个(PROP: TYPE)，PROP应该匹配cls.PROP或cls.get_PROP()，
            TYPE应该是ROS2消息类型。默认为{}。
        device_config: 初始化时的config。
        action_value_mappings: 设备动作。默认为{}。
            每个(ACTION: {'type': CMD_TYPE, 'goal': {FIELD: PROP}, 'feedback': {FIELD: PROP}, 'result': {FIELD: PROP}}),
        hardware_interface: 硬件接口配置。默认为{"name": "hardware_interface", "write": "send_command", "read": "read_data", "extra_info": []}。
        print_publish: 是否打印发布信息。默认为False。
        children: 物料/子节点信息。

    Returns:
        Type: 封装了设备类的ROS2节点类。
    """
    # 从属性中自动发现可发布状态
    if status_types is None:
        status_types = {}
    if device_config is None:
        raise ValueError("device_config cannot be None")
    if action_value_mappings is None:
        action_value_mappings = {}
    if hardware_interface is None:
        hardware_interface = {
            "name": "hardware_interface",
            "write": "send_command",
            "read": "read_data",
            "extra_info": [],
        }
    # FIXME 后面要删除
    for k, v in cls.__dict__.items():
        if not k.startswith("_") and isinstance(v, property):
            # noinspection PyUnresolvedReferences
            property_type = v.fget.__annotations__.get("return", str)
            get_method_name = f"get_{k}"
            set_method_name = f"set_{k}"

            if k not in status_types and hasattr(cls, get_method_name):
                status_types[k] = get_msg_type(property_type)

            if f"set_{k}" not in action_value_mappings and hasattr(cls, set_method_name):
                action_value_mappings[f"set_{k}"] = get_action_type(property_type)
    # 创建一个包装类来返回ROS2DeviceNode
    wrapper_class_name = f"ROS2NodeWrapper4{cls.__name__}"
    ROS2DeviceNodeWrapper = type(
        wrapper_class_name,
        (ROS2DeviceNode,),
        {
            "__init__": lambda self, *args, **kwargs: init_wrapper(
                self,
                driver_class=cls,
                device_config=device_config,
                status_types=status_types,
                action_value_mappings=action_value_mappings,
                hardware_interface=hardware_interface,
                print_publish=print_publish,
                *args,
                **kwargs,
            ),
        },
    )
    return ROS2DeviceNodeWrapper
