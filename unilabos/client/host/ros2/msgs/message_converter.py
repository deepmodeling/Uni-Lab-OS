"""
消息转换器

该模块提供了在Python对象（dataclass, Pydantic模型）和ROS消息类型之间进行转换的功能。
使用ImportManager动态导入和管理所需模块。
"""

import json
import traceback
from io import StringIO
from typing import Iterable, Any, Dict, Type, TypeVar, Union

import yaml
from msgcenterpy.instances.ros2_instance import ROS2MessageInstance
from pydantic import BaseModel
from dataclasses import asdict, is_dataclass

from rosidl_parser.definition import UnboundedSequence, NamespacedType, BasicType, UnboundedString

from unilabos.utils import logger
from unilabos.utils.import_manager import ImportManager
from unilabos.config.config import ROSConfig
from unilabos.resources.objects.joint_state import ResourceJointState

# 定义泛型类型
T = TypeVar("T")
DataClassT = TypeVar("DataClassT")

# 从配置中获取需要导入的模块列表
ROS_MODULES = ROSConfig.modules

msg_converter_manager = ImportManager(ROS_MODULES)


"""geometry_msgs"""
Point = msg_converter_manager.get_class("geometry_msgs.msg:Point")
Pose = msg_converter_manager.get_class("geometry_msgs.msg:Pose")
"""std_msgs"""
Float64 = msg_converter_manager.get_class("std_msgs.msg:Float64")
Float64MultiArray = msg_converter_manager.get_class("std_msgs.msg:Float64MultiArray")
Int32 = msg_converter_manager.get_class("std_msgs.msg:Int32")
Int64 = msg_converter_manager.get_class("std_msgs.msg:Int64")
String = msg_converter_manager.get_class("std_msgs.msg:String")
Bool = msg_converter_manager.get_class("std_msgs.msg:Bool")
"""sensor_msgs"""
JointState = msg_converter_manager.get_class("sensor_msgs.msg:JointState")
"""nav2_msgs"""
NavigateToPose = msg_converter_manager.get_class("nav2_msgs.action:NavigateToPose")
NavigateThroughPoses = msg_converter_manager.get_class("nav2_msgs.action:NavigateThroughPoses")
SingleJointPosition = msg_converter_manager.get_class("control_msgs.action:SingleJointPosition")
"""unilabos_msgs"""
Resource = msg_converter_manager.get_class("unilabos_msgs.msg:Resource")
SendCmd = msg_converter_manager.get_class("unilabos_msgs.action:SendCmd")
"""unilabos"""
imsg = msg_converter_manager.get_module("unilabos.experiments.models")
Point3D = msg_converter_manager.get_class("unilabos.experiments.models:Point3D")

from control_msgs.action import *

# 基本消息类型映射
_msg_mapping: Dict[Type, Type] = {
    float: Float64,
    list[float]: Float64MultiArray,
    int: Int32,
    str: String,
    bool: Bool,
    Point3D: Point,
}

# Action类型映射
_action_mapping: Dict[Type, Dict[str, Any]] = {
    float: {
        "type": SingleJointPosition,
        "goal": {"position": "position", "max_velocity": "max_velocity"},
        "feedback": {"position": "position"},
        "result": {},
    },
    str: {
        "type": SendCmd,
        "goal": {"command": "position"},
        "feedback": {"status": "status"},
        "result": {},
    },
    Point3D: {
        "type": NavigateToPose,
        "goal": {"pose.pose.position": "position"},
        "feedback": {
            "current_pose.pose.position": "position",
            "navigation_time.sec": "time_spent",
            "estimated_time_remaining.sec": "time_remaining",
        },
        "result": {},
    },
    list[Point3D]: {
        "type": NavigateThroughPoses,
        "goal": {"poses[].pose.position": "positions[]"},
        "feedback": {
            "current_pose.pose.position": "position",
            "navigation_time.sec": "time_spent",
            "estimated_time_remaining.sec": "time_remaining",
            "number_of_poses_remaining": "pose_number_remaining",
        },
        "result": {},
    },
}

# 添加Protocol action类型到映射
for py_msgtype in imsg.__all__:
    if py_msgtype not in _action_mapping and (py_msgtype.endswith("Protocol") or py_msgtype.startswith("Protocol")):
        try:
            protocol_class = msg_converter_manager.get_class(f"unilabos.experiments.models.{py_msgtype}")
            action_name = py_msgtype.replace("Protocol", "")
            action_type = msg_converter_manager.get_class(f"unilabos_msgs.action.{action_name}")

            if action_type:
                _action_mapping[protocol_class] = {
                    "type": action_type,
                    "goal": {k: k for k in action_type.Goal().get_fields_and_field_types().keys()},
                    "feedback": {
                        (k if "time" not in k else f"{k}.sec"): k
                        for k in action_type.Feedback().get_fields_and_field_types().keys()
                    },
                    "result": {k: k for k in action_type.Result().get_fields_and_field_types().keys()},
                }
        except Exception:
            traceback.print_exc()
            logger.debug(f"Failed to load Protocol class: {py_msgtype}")

def _resource_to_ros(x: Any) -> Any:
    """资源整体 JSON 化后放进 ``Resource.data`` 单字段传输。

    ``id``/``name`` 冗余填充以便抓包排查；完整结构保存在 ``data``，
    接收端也只解析该字段。
    """

    if isinstance(x, BaseModel):
        x = x.model_dump(by_alias=True)
    if not isinstance(x, dict):
        raise ValueError(f"Resource 消息载荷必须是对象，实际为 {type(x).__name__}")
    return Resource(
        id=str(x.get("id", "") or ""),
        name=str(x.get("name", "") or ""),
        data=json.dumps(x, ensure_ascii=False),
    )


def _joint_state_to_ros(x: Any) -> Any:
    canonical = ResourceJointState.model_validate(_extract_data(x))
    return JointState(
        name=list(canonical.name),
        position=list(canonical.position),
        velocity=list(canonical.velocity),
        effort=list(canonical.effort),
    )


# Python 值到 ROS 消息的显式转换器。
_msg_converter: Dict[Type, Any] = {
    float: float,
    Float64: lambda x: Float64(data=float(x)),
    Float64MultiArray: lambda x: Float64MultiArray(data=[float(i) for i in x]),
    int: int,
    Int32: lambda x: Int32(data=int(x)),
    Int64: lambda x: Int64(data=int(x)),
    bool: bool,
    Bool: lambda x: Bool(data=bool(x)),
    JointState: _joint_state_to_ros,
    str: str,
    String: lambda x: String(data=str(x)),
    Point: lambda x: (
        Point(x=x.x, y=x.y, z=x.z)
        if not isinstance(x, dict)
        else Point(x=float(x.get("x", 0.0)), y=float(x.get("y", 0.0)), z=float(x.get("z", 0.0)))
    ),
    Resource: _resource_to_ros,
}

def json_or_yaml_loads(data: str) -> Any:
    try:
        return json.loads(data)
    except Exception as e:
        try:
            return yaml.safe_load(StringIO(data))
        except:
            pass
        raise e


def resource_msg_to_dict(x: Any) -> Dict[str, Any]:
    """把 ``Resource.data`` 整体 JSON 解码还原成规范资源 dict。"""

    payload = json_or_yaml_loads(x.data or "{}")
    if not isinstance(payload, dict):
        raise ValueError("ROS Resource.data 必须解码为对象")
    return payload


# ROS消息到Python转换器
_msg_converter_back: Dict[Type, Any] = {
    float: float,
    Float64: lambda x: x.data,
    Float64MultiArray: lambda x: x.data,
    int: int,
    Int32: lambda x: x.data,
    Int64: lambda x: x.data,
    bool: bool,
    Bool: lambda x: x.data,
    JointState: lambda x: ResourceJointState(
        name=list(x.name),
        position=list(x.position),
        velocity=list(x.velocity),
        effort=list(x.effort),
    ).model_dump(),
    str: str,
    String: lambda x: x.data,
    Point: lambda x: Point3D(x=x.x, y=x.y, z=x.z),
    Resource: resource_msg_to_dict,
}

# 消息数据类型映射
_msg_data_mapping: Dict[str, Type] = {
    "double": float,
    "float": float,
    "int": int,
    "bool": bool,
    "str": str,
}


def compare_model_fields(cls1: Any, cls2: Any) -> bool:
    """比较两个类的字段是否相同"""

    def get_class_fields(cls: Any) -> set:
        if hasattr(cls, "__annotations__"):
            return set(cls.__annotations__.keys())
        else:
            return set(cls.__dict__.keys())

    fields1 = get_class_fields(cls1)
    fields2 = get_class_fields(cls2)
    return fields1 == fields2


def get_msg_type(datatype: Type) -> Type:
    """
    获取与Python数据类型对应的ROS消息类型

    Args:
        datatype: Python数据类型、Pydantic模型或dataclass

    Returns:
        对应的ROS消息类型

    Raises:
        ValueError: 如果不支持的消息类型
    """
    # 直接匹配已知类型
    if isinstance(datatype, type) and datatype in _msg_mapping:
        return _msg_mapping[datatype]

    # 尝试通过字段比较匹配
    for k, v in _msg_mapping.items():
        if compare_model_fields(k, datatype):
            return v

    raise ValueError(f"Unsupported message type: {datatype}")


def get_action_type(datatype: Type) -> Dict[str, Any]:
    """
    获取与Python数据类型对应的ROS动作类型

    Args:
        datatype: Python数据类型、Pydantic模型或dataclass

    Returns:
        对应的ROS动作类型配置

    Raises:
        ValueError: 如果不支持的动作类型
    """
    # 直接匹配已知类型
    if isinstance(datatype, type) and datatype in _action_mapping:
        return _action_mapping[datatype]

    # 尝试通过字段比较匹配
    for k, v in _action_mapping.items():
        if compare_model_fields(k, datatype):
            return v

    raise ValueError(f"Unsupported action type: {datatype}")


def get_ros_type_by_msgname(msgname: str) -> Type:
    """
    通过消息名称获取ROS类型

    Args:
        msgname: ROS消息名称，格式为 'package_name/(action,msg,srv)/TypeName'

    Returns:
        对应的ROS类型

    Raises:
        ValueError: 如果无效的ROS消息名称
        ImportError: 如果无法加载类型
    """
    parts = msgname.split("/")
    if len(parts) != 3 or parts[1] not in ("action", "msg", "srv"):
        raise ValueError(
            f"Invalid ROS message name: {msgname}. Format should be 'package_name/(action,msg,srv)/TypeName'"
        )

    package_name, msg_type, type_name = parts
    full_module_path = f"{package_name}.{msg_type}"

    try:
        # 尝试通过ImportManager获取
        return msg_converter_manager.get_class(f"{full_module_path}.{type_name}")
    except KeyError:
        # 尝试动态导入
        try:
            msg_converter_manager.load_module(full_module_path)
            return msg_converter_manager.get_class(f"{full_module_path}.{type_name}")
        except Exception as e:
            raise ImportError(f"Failed to load type {type_name}. Make sure the package is installed.") from e


def _extract_data(obj: Any) -> Dict[str, Any]:
    """提取对象数据为字典"""
    if is_dataclass(obj) and not isinstance(obj, type) and hasattr(obj, "__dataclass_fields__"):
        return asdict(obj)
    elif isinstance(obj, BaseModel):
        return obj.model_dump()
    elif isinstance(obj, dict):
        return obj
    else:
        return {"data": obj}


def convert_to_ros_msg(ros_msg_type: Union[Type, Any], obj: Any) -> Any:
    """
    将Python对象转换为ROS消息实例

    Args:
        ros_msg_type: 目标ROS消息类型
        obj: Python对象(基本类型、dataclass或Pydantic实例)

    Returns:
        ROS消息实例
    """
    # 尝试使用预定义转换器
    try:
        if isinstance(ros_msg_type, type) and ros_msg_type in _msg_converter:
            return _msg_converter[ros_msg_type](obj)
    except Exception as e:
        logger.error(f"Converter error: {type(ros_msg_type)} -> {obj}")
        traceback.print_exc()

    # 创建ROS消息实例
    ros_msg = ros_msg_type() if isinstance(ros_msg_type, type) else ros_msg_type

    # 提取数据
    extract_data = dict(_extract_data(obj))

    # 转换数据到ROS消息
    for ind, data in enumerate(ros_msg.get_fields_and_field_types().items()):
        key, type_name = data
        if key not in extract_data:
            continue
        value = extract_data[key]
        if hasattr(ros_msg, key):
            attr = getattr(ros_msg, key)
            if isinstance(attr, (float, int, str, bool)):
                setattr(ros_msg, key, type(attr)(value))
            elif isinstance(attr, (list, tuple)) and isinstance(value, Iterable):
                td = ros_msg.SLOT_TYPES[ind].value_type
                if isinstance(td, NamespacedType):
                    target_class = msg_converter_manager.get_class(f"{'.'.join(td.namespaces)}.{td.name}")
                    setattr(ros_msg, key, [convert_to_ros_msg(target_class, v) for v in value])
                elif isinstance(td, UnboundedString):
                    setattr(ros_msg, key, value)
                else:
                    logger.warning(f"Not Supported type: {td}")
                    # 未支持的序列元素类型使用空列表，避免构造无效 ROS 消息。
                    setattr(ros_msg, key, [])
            elif "array.array" in str(type(attr)):
                if attr.typecode == "f" or attr.typecode == "d":
                    setattr(ros_msg, key, [float(i) for i in value])
                else:
                    setattr(ros_msg, key, value)
            else:
                nested_ros_msg = convert_to_ros_msg(type(attr)(), value)
                setattr(ros_msg, key, nested_ros_msg)
        else:
            # 跳过不存在的字段，防止报错
            continue

    return ros_msg


def convert_to_ros_msg_with_mapping(ros_msg_type: Type, obj: Any, value_mapping: Dict[str, str]) -> Any:
    """
    根据字段映射将Python对象转换为ROS消息

    Args:
        ros_msg_type: 目标ROS消息类型
        obj: Python对象
        value_mapping: 字段名映射关系字典

    Returns:
        ROS消息实例
    """
    # 创建ROS消息实例
    ros_msg = ros_msg_type() if isinstance(ros_msg_type, type) else ros_msg_type

    # 提取数据
    data = _extract_data(obj)

    # 按照映射关系处理每个字段
    for msg_name, attr_name in value_mapping.items():
        msg_path = msg_name.split(".")
        attr_base = attr_name.rstrip("[]")

        if attr_base not in data:
            continue

        value = data[attr_base]
        if value is None:
            continue

        try:
            if not attr_name.endswith("[]"):
                # 处理单值映射，如 {"pose.position": "position"}
                current = ros_msg
                for i, name in enumerate(msg_path[:-1]):
                    current = getattr(current, name)

                last_field = msg_path[-1]
                field_type = type(getattr(current, last_field))
                setattr(current, last_field, convert_to_ros_msg(field_type, value))
            else:
                # 处理列表值映射，如 {"poses[].position": "positions[]"}
                if not isinstance(value, Iterable) or isinstance(value, (str, dict)):
                    continue

                items = list(value)
                if not items:
                    continue

                # 仅支持简单路径的数组映射
                if len(msg_path) <= 2:
                    array_field = msg_path[0]
                    if hasattr(ros_msg, array_field):
                        if len(msg_path) == 1:
                            # 直接设置数组
                            setattr(ros_msg, array_field, items)
                        else:
                            # 设置数组元素的属性
                            target_field = msg_path[1]
                            array_items = getattr(ros_msg, array_field)

                            # 确保数组大小匹配
                            while len(array_items) < len(items):
                                # 添加新元素类型
                                if array_items:
                                    elem_type = type(array_items[0])
                                    array_items.append(elem_type())
                                else:
                                    # 无法确定元素类型时中断
                                    break

                            # 设置每个元素的属性
                            for i, val in enumerate(items):
                                if i < len(array_items):
                                    setattr(array_items[i], target_field, val)
        except Exception as e:
            # 忽略映射错误
            logger.debug(f"Mapping error for {msg_name} -> {attr_name}: {str(e)}")
            continue

    return ros_msg


def convert_from_ros_msg(msg: Any) -> Any:
    """
    将ROS消息对象递归转换为Python对象

    Args:
        msg: ROS消息实例

    Returns:
        Python对象(字典或基本类型)
    """
    # 使用预定义转换器
    if type(msg) in _msg_converter_back:
        return _msg_converter_back[type(msg)](msg)

    # 处理标准ROS消息
    elif hasattr(msg, "__slots__") and hasattr(msg, "_fields_and_field_types"):
        result = {}
        for field in msg.__slots__:
            field_value = getattr(msg, field)
            field_name = field[1:] if field.startswith("_") else field
            result[field_name] = convert_from_ros_msg(field_value)
        return result

    # 处理列表或元组
    elif isinstance(msg, (list, tuple)):
        return [convert_from_ros_msg(item) for item in msg]

    # 返回基本类型
    else:
        return msg


def convert_from_ros_msg_with_mapping(ros_msg: Any, value_mapping: Dict[str, str]) -> Dict[str, Any]:
    """
    根据字段映射将ROS消息转换为Python字典

    Args:
        ros_msg: ROS消息实例
        value_mapping: 字段名映射关系字典

    Returns:
        Python字典
    """
    data: Dict[str, Any] = {}

    for msg_name, attr_name in value_mapping.items():
        msg_path = msg_name.split(".")
        current = ros_msg

        try:
            if not attr_name.endswith("[]"):
                # 处理单值映射
                field_missing = False
                for name in msg_path:
                    if hasattr(current, name):
                        current = getattr(current, name)
                    else:
                        field_missing = True
                        break
                if field_missing:
                    # 注册表映射与消息定义漂移（字段不存在）：跳过该项让驱动
                    # 默认值生效，绝不把整个消息透传给单个参数。
                    logger.warning(
                        f"goal 映射字段 {msg_name!r} 在 {type(ros_msg).__name__} 中不存在，已跳过"
                    )
                    continue

                converted_value = convert_from_ros_msg(current)
                data[attr_name] = converted_value
            else:
                # 处理列表值映射
                for i, name in enumerate(msg_path):
                    if name.endswith("[]"):
                        base_name = name[:-2]
                        if hasattr(current, base_name):
                            current = list(getattr(current, base_name))
                        else:
                            current = []
                            break
                    else:
                        if isinstance(current, list):
                            next_level = []
                            for item in current:
                                if hasattr(item, name):
                                    next_level.append(getattr(item, name))
                            current = next_level
                        elif hasattr(current, name):
                            current = getattr(current, name)
                        else:
                            current = []
                            break

                attr_key = attr_name[:-2]
                if current:
                    converted_list = [convert_from_ros_msg(item) for item in current]
                    data[attr_key] = converted_list
                else:
                    print(f"⚠️ 列表为空，跳过 '{attr_key}'")
        except (AttributeError, TypeError) as e:
            logger.debug(f"Mapping conversion error for {msg_name} -> {attr_name}")
            continue

    return data


def set_msg_data(dtype_str: str, data: Any) -> Any:
    """
    将数据转换为指定消息类型

    Args:
        dtype_str: 消息类型字符串
        data: 要转换的数据

    Returns:
        转换后的数据
    """
    converter = _msg_data_mapping.get(dtype_str, str)
    return converter(data)


"""
ROS Action 到 JSON Schema 转换器

该模块提供了将 ROS Action 定义转换为 JSON Schema 的功能，
用于规范化 Action 接口和生成文档。
"""

import json
import yaml
from typing import Any, Dict, Type, Union, Optional

from unilabos.utils import logger
from unilabos.utils.import_manager import ImportManager
from unilabos.config.config import ROSConfig

basic_type_map = {
    "bool": {"type": "boolean"},
    "int8": {"type": "integer", "minimum": -128, "maximum": 127},
    "uint8": {"type": "integer", "minimum": 0, "maximum": 255},
    "int16": {"type": "integer", "minimum": -32768, "maximum": 32767},
    "uint16": {"type": "integer", "minimum": 0, "maximum": 65535},
    "int32": {"type": "integer", "minimum": -2147483648, "maximum": 2147483647},
    "uint32": {"type": "integer", "minimum": 0, "maximum": 4294967295},
    "int64": {"type": "integer"},
    "uint64": {"type": "integer", "minimum": 0},
    "double": {"type": "number"},
    "float": {"type": "number"},
    "float32": {"type": "number"},
    "float64": {"type": "number"},
    "string": {"type": "string"},
    "boolean": {"type": "boolean"},
    "char": {"type": "string", "maxLength": 1},
    "byte": {"type": "integer", "minimum": 0, "maximum": 255},
}


def ros_field_type_to_json_schema(
    type_info: Type | str, field_name: str
) -> Dict[str, Any]:
    """
    将 ROS 字段类型转换为 JSON Schema 类型定义

    Args:
        type_info: ROS 类型
        slot_type: ROS 类型
        field_name: 字段名，用于设置复杂类型的title

    Returns:
        对应的 JSON Schema 类型定义
    """
    if isinstance(type_info, UnboundedSequence):
        return {"type": "array", "items": ros_field_type_to_json_schema(type_info.value_type, field_name)}  # type: ignore
    if isinstance(type_info, NamespacedType):
        cls_name = ".".join(type_info.namespaces) + ":" + type_info.name
        type_class = msg_converter_manager.get_class(cls_name)
        return ros_field_type_to_json_schema(type_class, field_name)
    elif isinstance(type_info, BasicType):
        return ros_field_type_to_json_schema(type_info.typename, field_name)
    elif isinstance(type_info, UnboundedString):
        return basic_type_map["string"]
    elif isinstance(type_info, str):
        if type_info in basic_type_map:
            return basic_type_map[type_info]

        # 处理时间和持续时间类型
        if type_info in ("time", "duration", "builtin_interfaces/Time", "builtin_interfaces/Duration"):
            return {
                "type": "object",
                "properties": {
                    "sec": {"type": "integer", "description": "秒"},
                    "nanosec": {"type": "integer", "description": "纳秒"},
                },
                "required": ["sec", "nanosec"],
            }
        return {}
    else:
        return ros_message_to_json_schema(type_info, field_name)


def _strip_rosidl_descriptions(schema: Any) -> None:
    """递归清除 rosidl_parser 自动生成的无意义 description（含内存地址）。"""
    if isinstance(schema, dict):
        desc = schema.get("description", "")
        if isinstance(desc, str) and "rosidl_parser" in desc:
            del schema["description"]
        for v in schema.values():
            _strip_rosidl_descriptions(v)
    elif isinstance(schema, list):
        for item in schema:
            _strip_rosidl_descriptions(item)


def ros_message_to_json_schema(msg_class: Any, field_name: str) -> Dict[str, Any]:
    """
    将 ROS 消息类转换为 JSON Schema

    Args:
        msg_class: ROS 消息类
        field_name: 字段名，用于设置schema的title，如果为None则使用类名

    Returns:
        对应的 JSON Schema 定义
    """
    schema = ROS2MessageInstance(msg_class()).get_json_schema()
    schema["title"] = field_name
    schema.pop("description", None)
    _strip_rosidl_descriptions(schema)
    return schema


def ros_action_result_mapping(action_class: Any) -> Dict[str, str]:
    """生成 action Result 字段映射，并消除跨 ROS 发行版的不稳定字段。

    ``NavigateThroughPoses.Result`` 在 Humble 中包装 ``std_msgs/Empty``，在
    Jazzy 中则是 ``error_code/error_msg``。Uni-Lab-OS 的轨迹动作不读取这些
    字段，因此统一声明为无结果映射，避免共享 registry 随发行版来回变化。
    """
    if action_class is NavigateThroughPoses:
        return {}
    return {
        field_name: field_name
        for field_name in action_class.Result.get_fields_and_field_types()
    }


def ros_action_result_to_json_schema(action_class: Any) -> Dict[str, Any]:
    """生成可跨受支持 ROS 发行版复用的 action Result schema。"""
    if action_class is NavigateThroughPoses:
        return {
            "title": action_class.Result.__name__,
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        }
    return ros_message_to_json_schema(action_class.Result, action_class.Result.__name__)


def ros_action_to_json_schema(
    action_class: Any, description="", previous_schema: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    将 ROS Action 类转换为 JSON Schema

    Args:
        action_class: ROS Action 类
        description: 描述
        previous_schema: 已有 schema，用于保留 goal/feedback/result 子字段说明

    Returns:
        完整的 JSON Schema 定义
    """
    if (
        not hasattr(action_class, "Goal")
        or not hasattr(action_class, "Feedback")
        or not hasattr(action_class, "Result")
    ):
        raise ValueError(f"{action_class.__name__} 不是有效的 ROS Action 类")

    # 创建基础 schema
    schema = {
        "title": action_class.__name__,
        "description": description,
        "type": "object",
        "properties": {
            "goal": {
                # 'description': 'Action 目标 - 从客户端发送到服务器',
                **ros_message_to_json_schema(action_class.Goal, action_class.Goal.__name__)
            },
            "feedback": {
                # 'description': 'Action 反馈 - 执行过程中从服务器发送到客户端',
                **ros_message_to_json_schema(action_class.Feedback, action_class.Feedback.__name__)
            },
            "result": {
                # 'description': 'Action 结果 - 完成后从服务器发送到客户端',
                **ros_action_result_to_json_schema(action_class)
            },
        },
        "required": ["goal"],
    }

    _strip_rosidl_descriptions(schema)

    # 合并已有 schema 中 goal/feedback/result 子字段的 description。
    if previous_schema:
        _preserve_field_descriptions(schema, previous_schema)

    return schema


def _preserve_field_descriptions(
    new_schema: Dict[str, Any], previous_schema: Dict[str, Any]
) -> None:
    """
    保留已有 schema 中 goal/feedback/result 子字段的 description 和 title。

    Args:
        new_schema: 待补充的 schema（会被修改）
        previous_schema: 提供说明文本的已有 schema
    """
    for section in ["goal", "feedback", "result"]:
        new_section = new_schema.get("properties", {}).get(section, {})
        prev_section = previous_schema.get("properties", {}).get(section, {})

        if not new_section or not prev_section:
            continue

        new_props = new_section.get("properties", {})
        prev_props = prev_section.get("properties", {})

        for field_name, field_schema in new_props.items():
            if field_name in prev_props:
                prev_field = prev_props[field_name]
                # 保留字段的 description
                if "description" in prev_field and prev_field["description"]:
                    field_schema["description"] = prev_field["description"]
                # 保留字段的 title（用户自定义的中文名）
                if "title" in prev_field and prev_field["title"]:
                    field_schema["title"] = prev_field["title"]


def convert_ros_action_to_jsonschema(
    action_name_or_type: Union[str, Type], output_file: Optional[str] = None, format: str = "json"
) -> Dict[str, Any]:
    """
    将 ROS Action 类型转换为 JSON Schema，并可选地保存到文件

    Args:
        action_name_or_type: ROS Action 类型名称或类
        output_file: 可选，输出 JSON Schema 的文件路径
        format: 输出格式，'json' 或 'yaml'

    Returns:
        JSON Schema 定义（字典）
    """
    # 处理输入参数
    if isinstance(action_name_or_type, str):
        # 如果是字符串，尝试加载 Action 类型
        action_type = get_ros_type_by_msgname(action_name_or_type)
    else:
        action_type = action_name_or_type

    # 生成 JSON Schema
    schema = ros_action_to_json_schema(action_type)

    # 如果指定了输出文件，将 Schema 保存到文件
    if output_file:
        if format.lower() == "json":
            with open(output_file, "w", encoding="utf-8") as f:
                json.dump(schema, f, indent=2, ensure_ascii=False)
        elif format.lower() == "yaml":
            with open(output_file, "w", encoding="utf-8") as f:
                yaml.safe_dump(schema, f, default_flow_style=False, allow_unicode=True)
        else:
            raise ValueError(f"不支持的格式: {format}，请使用 'json' 或 'yaml'")

    return schema


# 示例用法
if __name__ == "__main__":
    # 示例：转换 NavigateToPose action
    try:
        from nav2_msgs.action import NavigateToPose

        # 转换为 JSON Schema 并打印
        schema = convert_ros_action_to_jsonschema(NavigateToPose)
        print(json.dumps(schema, indent=2, ensure_ascii=False))

    except ImportError:
        print("无法导入 NavigateToPose action，请确保已安装相关 ROS 包。")
