"""把设备属性的公开类型引用转换为 ROS 消息类，不修改包目录。"""
from typing import Any

from unilabos.ros.msgs.message_converter import String, msg_converter_manager


def resolve_message_type(type_ref: Any) -> Any:
    """接受 ROS 类、消息名称和 Python 注解；复杂或未知值保持 String 传输语义。"""
    if type_ref in (str, bool, int, float, dict, list, tuple, set):
        type_ref = type_ref.__name__
    if not isinstance(type_ref, str) or not type_ref:
        return type_ref
    mapping = {
        "str": "String", "bool": "Bool", "int": "Int64", "float": "Float64",
        "List[int]": "Int64MultiArray", "list[int]": "Int64MultiArray",
        "List[float]": "Float64MultiArray", "list[float]": "Float64MultiArray",
        "List[bool]": "Int8MultiArray", "list[bool]": "Int8MultiArray",
    }
    name = mapping.get(type_ref, type_ref)
    if "[" in name or name in {"dict", "list", "tuple", "set"}:
        return String
    resolved = (msg_converter_manager.get_class(name) if ":" in name
                else msg_converter_manager.search_class(name))
    return resolved or String
