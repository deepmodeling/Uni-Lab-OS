"""
Action 工具函数模块

提供处理 ROS Action 相关的辅助函数
"""

import traceback
from typing import Any, Dict, Optional, Type, TypedDict

from rclpy.action import ActionClient, ActionServer
from rosidl_parser.definition import BasicType, NamespacedType, UnboundedSequence, UnboundedString

from unilabos.ros.msgs.message_converter import msg_converter_manager
from unilabos.ros.nodes.base_device_node import BaseROS2DeviceNode
from unilabos.utils import logger


class ActionInfoType(TypedDict):
    type_name: str
    type_name_convert: str
    action_path: str
    goal_info: str


def get_action_info(
    v: ActionClient | ActionServer, name: Optional[str] = None, full_name: Optional[str] = None
) -> ActionInfoType:
    # noinspection PyProtectedMember
    n: BaseROS2DeviceNode = v._node
    if full_name is None:
        assert name is not None
        full_name = n.namespace + "/" + name
    # noinspection PyProtectedMember
    return {
        "type_name": v._action_type.__module__ + "." + v._action_type.__name__,
        "type_name_convert": (v._action_type.__module__ + "." + v._action_type.__name__).replace(".", "/"),
        "action_path": full_name,
        "goal_info": get_yaml_from_goal_type(v._action_type.Goal),
    }


def get_ros_msg_instance_as_dict(ros_msg_instance):
    full_dict = {}
    lower_dir = {i.lower(): i for i in dir(ros_msg_instance)}
    for k in dir(ros_msg_instance):
        if k == "SLOT_TYPES" or k.startswith("_") or k.endswith("__DEFAULT") or k in ["get_fields_and_field_types"]:
            continue
        v = getattr(ros_msg_instance, k)
        if f"{k.lower()}__default" in lower_dir:
            v_default = getattr(ros_msg_instance, lower_dir[f"{k.lower()}__default"])
            v = v_default
        if isinstance(v, (str, int, float, list, dict)):
            full_dict[k] = v
        else:
            full_dict[k] = get_ros_msg_instance_as_dict(v)
    return full_dict


def get_yaml_from_goal_type(goal_type) -> str:
    """从Goal类型对象中生成默认YAML格式字符串

    Args:
        goal_type: Goal类型对象

    Returns:
        str: 默认Goal参数的YAML格式字符串
    """
    if isinstance(goal_type, str):
        return "{}"
    if not goal_type:
        return "{}"

    goal_dict = {}
    slot_type = None
    try:
        for ind, slot_info in enumerate(goal_type._fields_and_field_types.items()):
            slot_name, slot_type = slot_info
            type_info = goal_type.SLOT_TYPES[ind]
            if isinstance(type_info, UnboundedSequence):
                inner_type = type_info.value_type
                if isinstance(inner_type, NamespacedType):
                    cls_name = ".".join(inner_type.namespaces) + ":" + inner_type.name
                    type_class = msg_converter_manager.get_class(cls_name)
                    default_value = [get_ros_msg_instance_as_dict(type_class())]
                elif isinstance(inner_type, BasicType):
                    default_value = [get_default_value_for_ros_type(inner_type.typename)]
                elif isinstance(inner_type, UnboundedString):
                    default_value = [""]
                else:
                    default_value = []
            elif isinstance(type_info, NamespacedType):
                cls_name = ".".join(type_info.namespaces) + ":" + type_info.name
                type_class = msg_converter_manager.get_class(cls_name)
                if type_class is None:
                    print("type_class", type_class, cls_name)
                default_value = get_ros_msg_instance_as_dict(type_class())
            elif isinstance(type_info, BasicType):
                default_value = get_default_value_for_ros_type(type_info.typename)
            elif isinstance(type_info, UnboundedString):
                default_value = ""
            else:
                type_class = msg_converter_manager.search_class(slot_type, search_lower=True)
                if type_class is not None:
                    default_value = type_class().data
                else:
                    default_value = "unknown"
            goal_dict[slot_name] = default_value
    except Exception as e:
        logger.error(f"获取Goal字段 {slot_type} 信息时出错: {e}")
        logger.error(traceback.format_exc())

    # 将字典转换为YAML格式字符串
    yaml_str = "{"

    # 每个字段转换为YAML格式
    yaml_parts = []
    for key, value in goal_dict.items():
        if isinstance(value, str):
            yaml_parts.append(f"{key}: '{value}'")
        elif isinstance(value, bool):
            yaml_parts.append(f"{key}: {str(value).lower()}")
        elif isinstance(value, (int, float)):
            yaml_parts.append(f"{key}: {value}")
        elif isinstance(value, dict) and not value:
            yaml_parts.append(f"{key}: {{}}")
        else:
            yaml_parts.append(f"{key}: {value}")

    yaml_str += ", ".join(yaml_parts) + "}"

    return yaml_str


"""旧版本函数"""


def get_default_value_for_ros_type(type_hint_or_str: Any) -> Any:
    """生成基于ROS类型提示或字符串的默认值

    根据ROS2类型定义，生成适当的默认值。支持基本类型、数组类型和嵌套消息类型。

    Args:
        type_hint_or_str: ROS2类型提示或类型名称字符串

    Returns:
        Any: 对应类型的默认值
    """
    # 处理None或无效输入
    if type_hint_or_str is None:
        return None

    # 基本类型映射
    type_str = str(type_hint_or_str).lower()  # 使用字符串表示

    # 处理常见基本类型
    if "int" in type_str:
        return 0
    if "float" in type_str or "double" in type_str:
        return 0.0
    if "bool" in type_str:
        return False
    if "string" in type_str:
        return ""
    if "byte" in type_str or "char" in type_str:
        return 0  # 用整数表示
    if "time" == type_str or "duration" == type_str:
        return {"sec": 0, "nanosec": 0}

    # 处理数组 - 返回空列表
    if "sequence" in type_str or "vector" in type_str or "[]" in type_str:
        return []

    # 处理嵌套消息类型 - 返回空字典占位符
    if "." in str(type_hint_or_str):
        # 尝试用消息转换管理器查找类型并生成默认值
        try:
            type_name = str(type_hint_or_str).strip().split("[")[0]  # 移除数组部分
            # 尝试查找类型
            if msg_converter_manager:
                type_class = msg_converter_manager.search_class(type_name)
                if type_class:
                    # 递归生成默认值字典
                    return generate_example_dict_from_ros_class(type_class)
        except Exception as e:
            print(f"查找类型默认值时出错: {type_hint_or_str}, {e}")

        # 如果找不到或出错，返回空字典
        return {}

    # 特殊类型的默认值
    if "pose" in type_str or "position" in type_str:
        return {"x": 0.0, "y": 0.0, "z": 0.0}
    if "orientation" in type_str or "quaternion" in type_str:
        return {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0}
    if "header" in type_str:
        return {"frame_id": "", "stamp": {"sec": 0, "nanosec": 0}}

    return None  # 未知类型


def generate_example_dict_from_ros_class(ros_class: Any) -> Dict[str, Any]:
    """检查ROS消息/服务/动作类并生成带有默认值的字典

    分析ROS2消息类定义，提取其字段结构，并为每个字段生成合适的默认值。

    Args:
        ros_class: ROS2消息/服务/动作类或其实例

    Returns:
        Dict[str, Any]: 包含消息字段及默认值的字典
    """
    example_dict = {}

    # 检查是否已经是字典
    if isinstance(ros_class, dict):
        return ros_class

    # 处理无效输入
    if ros_class is None:
        return {}

    # 获取字段信息
    fields = {}
    try:
        if hasattr(ros_class, "_fields_and_field_types"):
            fields = ros_class._fields_and_field_types
        elif hasattr(ros_class, "__slots__") and hasattr(ros_class, "__annotations__"):
            for slot in getattr(ros_class, "__slots__", []):
                field_name = slot  # 假设slot名称与字段名称匹配
                if field_name in getattr(ros_class, "__annotations__", {}):
                    fields[field_name] = ros_class.__annotations__[field_name]
                else:
                    fields[field_name] = "unknown"  # 如果缺少类型提示则使用默认值
    except Exception as e:
        print(f"获取ROS类字段信息时出错: {e}")
        return {}

    # 为每个字段生成默认值
    for field_name, field_type in fields.items():
        example_dict[field_name] = get_default_value_for_ros_type(field_type)

    return example_dict


def extract_action_structures(action_type: Type) -> Dict[str, Any]:
    """从Action类型对象中提取Goal/Result/Feedback结构"""
    result = {"goal": {}, "result": {}, "feedback": {}}

    try:
        # 检查action_type是否为合法对象
        if hasattr(action_type, "Goal"):
            # 获取Goal类及其字段
            goal_class = getattr(action_type, "Goal", None)
            if goal_class:
                result["goal"] = generate_example_dict_from_ros_class(goal_class)

            # 获取Result类及其字段
            result_class = getattr(action_type, "Result", None)
            if result_class:
                result["result"] = generate_example_dict_from_ros_class(result_class)

            # 获取Feedback类及其字段
            feedback_class = getattr(action_type, "Feedback", None)
            if feedback_class:
                result["feedback"] = generate_example_dict_from_ros_class(feedback_class)
    except Exception as e:
        print(f"提取Action结构时出错: {type(action_type)}")
        print(traceback.format_exc())

    return result


def process_device_actions(action_config: Dict[str, Any], action_type: Type, action_name: str) -> Dict[str, Any]:
    """处理设备动作，生成命令示例和结构信息

    Args:
        action_config: 动作配置信息，包含topic等内容
        action_type: 动作类型，可以是类型对象或字符串
        action_name: 动作名称

    Returns:
        Dict[str, Any]: 包含命令示例和结构信息的字典
    """
    # 检查action_type是否为None或非法值
    if action_type is None:
        # 返回基本结构，确保前端不会报错
        return {
            "topic": action_config.get("topic", "UNKNOWN_TOPIC"),
            "type_str": "UNKNOWN_TYPE",
            "goal": "{}",
            "full_command": f"ros2 action send_goal {action_config.get('topic', 'UNKNOWN_TOPIC')} UNKNOWN_TYPE '{{}}'",
            "goal_dict": {},
            "result_dict": {},
            "feedback_dict": {},
        }

    # 提取类型路径字符串，从<class 'package.action._action_name.ActionName'>格式转换为package/action/ActionName
    type_str = str(action_type)[8:-2]  # 去除<class ' ... '>
    parts = type_str.split(".")

    # 构造ROS2类型字符串
    if len(parts) >= 3 and "action" in parts:
        action_idx = parts.index("action")
        if action_idx >= 0 and action_idx < len(parts) - 1:
            package_name = parts[0]
            action_class_name = parts[-1]
            ros2_type_str = f"{package_name}/action/{action_class_name}"
        else:
            ros2_type_str = type_str.replace(".", "/")
    else:
        ros2_type_str = type_str.replace(".", "/")

    # 提取动作结构
    action_structures = extract_action_structures(action_type)

    # 获取goal部分并转换为YAML格式
    goal_dict = action_structures["goal"]
    goal_yaml = dict_to_yaml_str(goal_dict)

    # 获取topic
    topic = action_config.get("topic", "UNKNOWN_TOPIC")

    return {
        "topic": topic,
        "type_str": ros2_type_str,
        "goal": goal_yaml,
        "full_command": f"ros2 action send_goal {topic} {ros2_type_str} '{goal_yaml}'",
        "goal_dict": goal_dict,
        "result_dict": action_structures["result"],
        "feedback_dict": action_structures["feedback"],
    }


def dict_to_yaml_str(d: Dict) -> str:
    """将字典转换为YAML字符串（单行格式）

    Args:
        d: 要转换的字典

    Returns:
        str: YAML格式的字符串
    """
    if not d:
        return "{}"

    parts = []

    def format_value(v):
        if isinstance(v, str):
            return f"'{v}'"
        elif isinstance(v, bool):
            return str(v).lower()
        elif isinstance(v, (int, float)) or v is None:
            return str(v)
        elif isinstance(v, list):
            items = [format_value(item) for item in v]
            return f"[{', '.join(items)}]"
        elif isinstance(v, dict):
            return dict_to_yaml_str(v)
        return "null"

    for key, value in d.items():
        parts.append(f"{key}: {format_value(value)}")

    return "{" + ", ".join(parts) + "}"
