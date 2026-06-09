"""
装饰器注册表系统

通过 @device, @action, @resource 装饰器替代 YAML 配置文件来定义设备/动作/资源注册表信息。

Usage:
    from unilabos.registry.decorators import (
        device, action, resource,
        InputHandle, OutputHandle,
        ActionInputHandle, ActionOutputHandle,
        HardwareInterface, Side, DataSource, NodeType,
    )

    @device(
        id="solenoid_valve_mock",
        category=["pump_and_valve"],
        description="模拟电磁阀设备",
        displayname="模拟电磁阀",
        handles=[
            InputHandle(key="in", data_type="fluid", label="in", side=Side.NORTH),
            OutputHandle(key="out", data_type="fluid", label="out", side=Side.SOUTH),
        ],
        hardware_interface=HardwareInterface(
            name="hardware_interface",
            read="send_command",
            write="send_command",
        ),
    )
    class SolenoidValveMock:
        @action(action_type=EmptyIn)
        def close(self):
            ...

        @action(
            handles=[
                ActionInputHandle(key="in", data_type="fluid", label="in"),
                ActionOutputHandle(key="out", data_type="fluid", label="out"),
            ],
        )
        def set_valve_position(self, position):
            ...

        # 无 @action 装饰器 => auto- 前缀动作
        def is_open(self):
            ...
"""

from enum import Enum
from functools import wraps
import re
from typing import Any, Callable, Dict, List, Optional, TypeVar

from pydantic import BaseModel, ConfigDict, Field

F = TypeVar("F", bound=Callable[..., Any])
_DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9_]+$")

# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------


class Side(str, Enum):
    """UI 上 Handle 的显示位置"""

    NORTH = "NORTH"
    SOUTH = "SOUTH"
    EAST = "EAST"
    WEST = "WEST"


class DataSource(str, Enum):
    """Handle 的数据来源"""

    HANDLE = "handle"  # 从上游 handle 获取数据 (用于 InputHandle)
    EXECUTOR = "executor"  # 从执行器输出数据 (用于 OutputHandle)


class NodeType(str, Enum):
    """动作的节点类型（用于区分 ILab 节点和人工确认节点等）"""

    ILAB = "ILab"
    MANUAL_CONFIRM = "manual_confirm"


# ---------------------------------------------------------------------------
# Device / Resource Handle (设备/资源级别端口, 序列化时包含 io_type)
# ---------------------------------------------------------------------------


class _DeviceHandleBase(BaseModel):
    """设备/资源端口基类 (内部使用)"""

    model_config = ConfigDict(populate_by_name=True)

    key: str = Field(serialization_alias="handler_key")
    data_type: str
    label: str
    side: Optional[Side] = None
    data_key: Optional[str] = None
    data_source: Optional[str] = None
    description: Optional[str] = None

    # 子类覆盖
    io_type: str = ""

    def to_registry_dict(self) -> Dict[str, Any]:
        return self.model_dump(by_alias=True, exclude_none=True)


class InputHandle(_DeviceHandleBase):
    """
    输入端口 (io_type="target"), 用于 @device / @resource handles

    Example:
        InputHandle(key="in", data_type="fluid", label="in", side=Side.NORTH)
    """

    io_type: str = "target"


class OutputHandle(_DeviceHandleBase):
    """
    输出端口 (io_type="source"), 用于 @device / @resource handles

    Example:
        OutputHandle(key="out", data_type="fluid", label="out", side=Side.SOUTH)
    """

    io_type: str = "source"


# ---------------------------------------------------------------------------
# Action Handle (动作级别端口, 序列化时不含 io_type, 按类型自动分组)
# ---------------------------------------------------------------------------


class _ActionHandleBase(BaseModel):
    """动作端口基类 (内部使用)"""

    model_config = ConfigDict(populate_by_name=True)

    key: str = Field(serialization_alias="handler_key")
    data_type: str
    label: str
    side: Optional[Side] = None
    data_key: Optional[str] = None
    data_source: Optional[str] = None
    description: Optional[str] = None
    io_type: Optional[str] = None  # source/sink (dataflow) or target/source (device-style)

    def to_registry_dict(self) -> Dict[str, Any]:
        return self.model_dump(by_alias=True, exclude_none=True)


class ActionInputHandle(_ActionHandleBase):
    """
    动作输入端口, 用于 @action handles, 序列化后归入 "input" 组

    Example:
        ActionInputHandle(
            key="material_input", data_type="workbench_material",
            label="物料编号", data_key="material_number", data_source="handle",
        )
    """

    pass


class ActionOutputHandle(_ActionHandleBase):
    """
    动作输出端口, 用于 @action handles, 序列化后归入 "output" 组

    Example:
        ActionOutputHandle(
            key="station_output", data_type="workbench_station",
            label="加热台ID", data_key="station_id", data_source="executor",
        )
    """

    pass


# ---------------------------------------------------------------------------
# HardwareInterface
# ---------------------------------------------------------------------------


class HardwareInterface(BaseModel):
    """
    硬件通信接口定义

    描述设备与底层硬件通信的方式 (串口、Modbus 等)。

    Example:
        HardwareInterface(name="hardware_interface", read="send_command", write="send_command")
    """

    name: str
    read: Optional[str] = None
    write: Optional[str] = None
    extra_info: Optional[List[str]] = None


# ---------------------------------------------------------------------------
# 全局注册表 -- 记录所有被装饰器标记的类/函数
# ---------------------------------------------------------------------------
_registered_devices: Dict[str, type] = {}  # device_id -> class
_registered_resources: Dict[str, Any] = {}  # resource_id -> class or function


def _device_handles_to_list(
    handles: Optional[List[_DeviceHandleBase]],
) -> List[Dict[str, Any]]:
    """将设备/资源 Handle 列表序列化为字典列表 (含 io_type)"""
    if handles is None:
        return []
    return [h.to_registry_dict() for h in handles]


def _action_handles_to_dict(
    handles: Optional[List[_ActionHandleBase]],
) -> Dict[str, Any]:
    """
    将动作 Handle 列表序列化为 {"input": [...], "output": [...]} 格式。

    ActionInputHandle => "input", ActionOutputHandle => "output"
    """
    if handles is None:
        return {}
    input_list = [h.to_registry_dict() for h in handles if isinstance(h, ActionInputHandle)]
    output_list = [h.to_registry_dict() for h in handles if isinstance(h, ActionOutputHandle)]
    result: Dict[str, Any] = {}
    if input_list:
        result["input"] = input_list
    if output_list:
        result["output"] = output_list
    return result


# ---------------------------------------------------------------------------
# @device 类装饰器
# ---------------------------------------------------------------------------


# noinspection PyShadowingBuiltins
def device(
    id: Optional[str] = None,
    ids: Optional[List[str]] = None,
    id_meta: Optional[Dict[str, Dict[str, Any]]] = None,
    category: Optional[List[str]] = None,
    description: str = "",
    display_name: str = "",
    displayname: str = "",
    icon: str = "",
    version: str = "1.0.0",
    handles: Optional[List[_DeviceHandleBase]] = None,
    model: Optional[Dict[str, Any]] = None,
    device_type: str = "python",
    hardware_interface: Optional[HardwareInterface] = None,
):
    """
    设备类装饰器

    将类标记为一个 UniLab-OS 设备，并附加注册表元数据。

    支持两种模式:
      1. 单设备: id="xxx", category=[...]
      2. 多设备: ids=["id1","id2"], id_meta={"id1":{handles:[...]}, "id2":{...}}

    Args:
        id: 单设备时的注册表唯一标识
        ids: 多设备时的 id 列表，与 id_meta 配合使用
        id_meta: 每个 device_id 的覆盖元数据 (handles/description/icon/model)
        category: 设备分类标签列表 (必填)
        description: 设备描述
        displayname: 人类可读的设备显示名称，缺失时默认使用 id
        display_name: 兼容旧代码的显示名称参数；新代码优先使用 displayname
        icon: 图标路径
        version: 版本号
        handles: 设备端口列表 (单设备或 id_meta 未覆盖时使用)
        model: 可选的 3D 模型配置
        device_type: 设备实现类型 ("python" / "ros2")
        hardware_interface: 硬件通信接口 (HardwareInterface)
    """
    # Resolve device ids
    if ids is not None:
        device_ids = list(ids)
        if not device_ids:
            raise ValueError("@device ids 不能为空")
        id_meta = id_meta or {}
    elif id is not None:
        device_ids = [id]
        id_meta = {}
    else:
        raise ValueError("@device 必须提供 id 或 ids")

    invalid_ids = [did for did in device_ids if not _DEVICE_ID_RE.fullmatch(did)]
    if invalid_ids:
        raise ValueError(
            "@device id 只能包含英文、数字、下划线: "
            + ", ".join(repr(did) for did in invalid_ids)
        )

    if category is None:
        raise ValueError("@device category 必填")

    resolved_display_name = displayname or display_name
    base_meta = {
        "category": category,
        "description": description,
        "display_name": resolved_display_name,
        "icon": icon,
        "version": version,
        "handles": _device_handles_to_list(handles),
        "model": model,
        "device_type": device_type,
        "hardware_interface": (hardware_interface.model_dump(exclude_none=True) if hardware_interface else None),
    }

    def decorator(cls):
        cls._device_registry_meta = base_meta
        cls._device_registry_id_meta = id_meta
        cls._device_registry_ids = device_ids

        for did in device_ids:
            if did in _registered_devices:
                raise ValueError(f"@device id 重复: '{did}' 已被 {_registered_devices[did]} 注册")
            _registered_devices[did] = cls

        return cls

    return decorator


# ---------------------------------------------------------------------------
# @action 方法装饰器
# ---------------------------------------------------------------------------

# 区分 "用户没传 action_type" 和 "用户传了 None"
_ACTION_TYPE_UNSET = object()


# noinspection PyShadowingNames
def action(
    action_type: Any = _ACTION_TYPE_UNSET,
    goal: Optional[Dict[str, str]] = None,
    feedback: Optional[Dict[str, str]] = None,
    result: Optional[Dict[str, str]] = None,
    handles: Optional[List[_ActionHandleBase]] = None,
    goal_default: Optional[Dict[str, Any]] = None,
    placeholder_keys: Optional[Dict[str, str]] = None,
    always_free: bool = False,
    is_protocol: bool = False,
    description: str = "",
    auto_prefix: bool = False,
    parent: bool = False,
    node_type: Optional["NodeType"] = None,
    feedback_interval: Optional[float] = None,
):
    """
    动作方法装饰器

    标记方法为注册表动作。有三种用法:
      1. @action(action_type=EmptyIn, ...)  -- 非 auto, 使用指定 ROS Action 类型
      2. @action()                          -- 非 auto, UniLabJsonCommand (从方法签名生成 schema)
      3. 不加 @action                       -- auto- 前缀, UniLabJsonCommand

    Protocol 用法:
      @action(action_type=Add, is_protocol=True)
      def AddProtocol(self): ...
      标记该动作为高级协议 (protocol)，运行时通过 ROS Action 路由到
      protocol generator 执行。action_type 指向 unilabos_msgs 的 Action 类型。

    Args:
        action_type: ROS Action 消息类型 (如 EmptyIn, SendCmd, HeatChill).
                     不传/默认 = UniLabJsonCommand (非 auto).
        goal: Goal 字段映射 (ROS字段名 -> 设备参数名).
              protocol 模式下可留空，系统自动生成 identity 映射.
        feedback: Feedback 字段映射
        result: Result 字段映射
        handles: 动作端口列表 (ActionInputHandle / ActionOutputHandle)
        goal_default: Goal 字段默认值映射 (字段名 -> 默认值), 与自动生成的 goal_default 合并
        placeholder_keys: 参数占位符配置
        always_free: 是否为永久闲置动作 (不受排队限制)
        is_protocol: 是否为工作站协议 (protocol)。True 时运行时走 protocol generator 路径。
        description: 动作描述
        auto_prefix: 若为 True，动作名使用 auto-{method_name} 形式（与无 @action 时一致）
        parent: 若为 True，当方法参数为空 (*args, **kwargs) 时，通过 MRO 从父类获取真实方法参数
        node_type: 动作的节点类型 (NodeType.ILAB / NodeType.MANUAL_CONFIRM)。
                   不填写时不写入注册表。
    """

    def decorator(func: F) -> F:
        import asyncio as _asyncio

        if _asyncio.iscoroutinefunction(func):
            @wraps(func)
            async def wrapper(*args, **kwargs):
                return await func(*args, **kwargs)
        else:
            @wraps(func)
            def wrapper(*args, **kwargs):
                return func(*args, **kwargs)

        # action_type 为哨兵值 => 用户没传, 视为 None (UniLabJsonCommand)
        resolved_type = None if action_type is _ACTION_TYPE_UNSET else action_type

        meta = {
            "action_type": resolved_type,
            "goal": goal or {},
            "feedback": feedback or {},
            "result": result or {},
            "handles": _action_handles_to_dict(handles),
            "goal_default": goal_default or {},
            "placeholder_keys": placeholder_keys or {},
            "always_free": always_free,
            "is_protocol": is_protocol,
            "description": description,
            "auto_prefix": auto_prefix,
            "parent": parent,
        }
        if feedback_interval is not None:
            meta["feedback_interval"] = feedback_interval
        if node_type is not None:
            meta["node_type"] = node_type.value if isinstance(node_type, NodeType) else str(node_type)
        wrapper._action_registry_meta = meta  # type: ignore[attr-defined]

        # 设置 _is_always_free 保持与旧 @always_free 装饰器兼容
        if always_free:
            wrapper._is_always_free = True  # type: ignore[attr-defined]

        return wrapper  # type: ignore[return-value]

    return decorator


def get_action_meta(func) -> Optional[Dict[str, Any]]:
    """获取方法上的 @action 装饰器元数据"""
    return getattr(func, "_action_registry_meta", None)


def has_action_decorator(func) -> bool:
    """检查函数是否带有 @action 装饰器"""
    return hasattr(func, "_action_registry_meta")


# ---------------------------------------------------------------------------
# @resource 类/函数装饰器
# ---------------------------------------------------------------------------


def resource(
    id: str,
    category: List[str],
    description: str = "",
    icon: str = "",
    version: str = "1.0.0",
    handles: Optional[List[_DeviceHandleBase]] = None,
    model: Optional[Dict[str, Any]] = None,
    class_type: str = "pylabrobot",
):
    """
    资源类/函数装饰器

    将类或工厂函数标记为一个 UniLab-OS 资源，附加注册表元数据。

    Args:
        id: 注册表唯一标识 (必填, 不可重复)
        category: 资源分类标签列表 (必填)
        description: 资源描述
        icon: 图标路径
        version: 版本号
        handles: 端口列表 (InputHandle / OutputHandle)
        model: 可选的 3D 模型配置
        class_type: 资源实现类型 ("python" / "pylabrobot" / "unilabos")
    """

    def decorator(obj):
        meta = {
            "resource_id": id,
            "category": category,
            "description": description,
            "icon": icon,
            "version": version,
            "handles": _device_handles_to_list(handles),
            "model": model,
            "class_type": class_type,
        }
        obj._resource_registry_meta = meta

        if id in _registered_resources:
            raise ValueError(f"@resource id 重复: '{id}' 已被 {_registered_resources[id]} 注册")
        _registered_resources[id] = obj

        return obj

    return decorator


def get_device_meta(cls, device_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    获取类上的 @device 装饰器元数据。

    当 device_id 存在且类使用 ids+id_meta 时，返回合并后的 meta
    (base_meta 与 id_meta[device_id] 深度合并)。
    """
    base = getattr(cls, "_device_registry_meta", None)
    if base is None:
        return None
    id_meta = getattr(cls, "_device_registry_id_meta", None) or {}
    if device_id is None or device_id not in id_meta:
        result = dict(base)
        ids = getattr(cls, "_device_registry_ids", None)
        result["device_id"] = device_id if device_id is not None else (ids[0] if ids else None)
        return result

    overrides = id_meta[device_id]
    result = dict(base)
    result["device_id"] = device_id
    for key in ["handles", "description", "display_name", "displayname", "icon", "model"]:
        if key in overrides:
            val = overrides[key]
            if key == "handles" and isinstance(val, list):
                # handles 必须是 Handle 对象列表
                result[key] = [h.to_registry_dict() for h in val]
            elif key == "displayname":
                result["display_name"] = val
            else:
                result[key] = val
    return result


def get_resource_meta(obj) -> Optional[Dict[str, Any]]:
    """获取对象上的 @resource 装饰器元数据"""
    return getattr(obj, "_resource_registry_meta", None)


def get_all_registered_devices() -> Dict[str, type]:
    """获取所有已注册的设备类"""
    return _registered_devices.copy()


def get_all_registered_resources() -> Dict[str, Any]:
    """获取所有已注册的资源"""
    return _registered_resources.copy()


def clear_registry():
    """清空全局注册表 (用于测试)"""
    _registered_devices.clear()
    _registered_resources.clear()


# ---------------------------------------------------------------------------
# 枚举值归一化
# ---------------------------------------------------------------------------


def normalize_enum_value(raw: Any, enum_cls) -> Optional[str]:
    """将 AST 提取的枚举成员名 / YAML 值字符串 / 旧格式长路径统一归一化为枚举值。

    适用于 Side、DataSource、NodeType 等继承自 ``str, Enum`` 的装饰器枚举。

    处理以下格式:
      - "MANUAL_CONFIRM"  →  NodeType["MANUAL_CONFIRM"].value = "manual_confirm"
      - "manual_confirm"  →  NodeType("manual_confirm").value = "manual_confirm"
      - "HANDLE"          →  DataSource["HANDLE"].value = "handle"
      - "NORTH"           →  Side["NORTH"].value = "NORTH"
      - 旧缓存长路径 "unilabos...NodeType.MANUAL_CONFIRM" → 先 rsplit 再查找
    """
    if not raw:
        return None
    raw_str = str(raw)
    if "." in raw_str:
        raw_str = raw_str.rsplit(".", 1)[-1]
    try:
        return enum_cls[raw_str].value
    except KeyError:
        pass
    try:
        return enum_cls(raw_str).value
    except ValueError:
        return raw_str


# ---------------------------------------------------------------------------
# topic_config / not_action / always_free 装饰器
# ---------------------------------------------------------------------------


def topic_config(
    period: Optional[float] = None,
    print_publish: Optional[bool] = None,
    qos: Optional[int] = None,
    name: Optional[str] = None,
) -> Callable[[F], F]:
    """
    Topic发布配置装饰器

    用于装饰 get_{attr_name} 方法或 @property，控制对应属性的ROS topic发布行为。

    Args:
        period: 发布周期（秒）。None 表示使用默认值 5.0
        print_publish: 是否打印发布日志。None 表示使用节点默认配置
        qos: QoS深度配置。None 表示使用默认值 10
        name: 自定义发布名称。None 表示使用方法名（去掉 get_ 前缀）

    Note:
        与 @property 连用时，@topic_config 必须放在 @property 下面，
        这样装饰器执行顺序为：先 topic_config 添加配置，再 property 包装。
    """

    def decorator(func: F) -> F:
        @wraps(func)
        def wrapper(*args, **kwargs):
            return func(*args, **kwargs)

        wrapper._topic_period = period  # type: ignore[attr-defined]
        wrapper._topic_print_publish = print_publish  # type: ignore[attr-defined]
        wrapper._topic_qos = qos  # type: ignore[attr-defined]
        wrapper._topic_name = name  # type: ignore[attr-defined]
        wrapper._has_topic_config = True  # type: ignore[attr-defined]

        return wrapper  # type: ignore[return-value]

    return decorator


def get_topic_config(func) -> dict:
    """获取函数上的 topic 配置 (period, print_publish, qos, name)"""
    if hasattr(func, "_has_topic_config") and getattr(func, "_has_topic_config", False):
        return {
            "period": getattr(func, "_topic_period", None),
            "print_publish": getattr(func, "_topic_print_publish", None),
            "qos": getattr(func, "_topic_qos", None),
            "name": getattr(func, "_topic_name", None),
        }
    return {}


def always_free(func: F) -> F:
    """
    标记动作为永久闲置(不受busy队列限制)的装饰器

    被此装饰器标记的 action 方法，在执行时不会受到设备级别的排队限制，
    任何时候请求都可以立即执行。适用于查询类、状态读取类等轻量级操作。
    """

    @wraps(func)
    def wrapper(*args, **kwargs):
        return func(*args, **kwargs)

    wrapper._is_always_free = True  # type: ignore[attr-defined]

    return wrapper  # type: ignore[return-value]


def is_always_free(func) -> bool:
    """检查函数是否被标记为永久闲置"""
    return getattr(func, "_is_always_free", False)


def not_action(func: F) -> F:
    """
    标记方法为非动作的装饰器

    用于装饰 driver 类中的方法，使其在注册表扫描时不被识别为动作。
    适用于辅助方法、内部工具方法等不应暴露为设备动作的公共方法。
    """

    @wraps(func)
    def wrapper(*args, **kwargs):
        return func(*args, **kwargs)

    wrapper._is_not_action = True  # type: ignore[attr-defined]

    return wrapper  # type: ignore[return-value]


def is_not_action(func) -> bool:
    """检查函数是否被标记为非动作"""
    return getattr(func, "_is_not_action", False)
