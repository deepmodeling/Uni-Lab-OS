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

import re
from enum import Enum
from functools import wraps
from typing import Any, Callable, Dict, List, Optional, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from unilabos.resources.site_definition import normalize_available_sites

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


class ExecutorKind(str, Enum):
    """动作的受控运行时执行器类型。"""

    MATERIAL_TRANSFER = "material_transfer"


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
    displayname: str = "",
    display_name: str = "",
    icon: str = "",
    version: str = "1.0.0",
    handles: Optional[List[_DeviceHandleBase]] = None,
    model: Optional[Dict[str, Any]] = None,
    device_type: str = "python",
    hardware_interface: Optional[HardwareInterface] = None,
    metadata: Optional[Dict[str, Any]] = None,
    available_sites: Optional[List[Dict[str, Any]]] = None,
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
        id_meta: 每个 device_id 的覆盖元数据，包括可选的 available_sites
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
        metadata: 设备扩展元数据，如供应商、规格、容量、孔位数等
        available_sites: 资源模板（ResourceTemplate）拥有的库位（Site）固定定义

    Returns:
        保存规范 Registry 元数据的类装饰器。

    Raises:
        ValueError: 设备身份、分类或库位模板定义非法时抛出。
    """
    # Resolve device ids
    if ids is not None:
        device_ids = list(ids)
        if not device_ids:
            raise ValueError("@device ids 不能为空")
        id_meta = id_meta or {}
        # ``id_meta`` 是多设备模板的逐身份覆盖；此处先冻结库位定义，避免装饰器
        # 返回后调用方修改原始数组导致同一资源模板代际漂移。
        id_meta = {
            device_id: {
                **meta,
                **(
                    {
                        "available_sites": normalize_available_sites(
                            meta.get("available_sites")
                        )
                    }
                    if "available_sites" in meta
                    else {}
                ),
            }
            for device_id, meta in id_meta.items()
        }
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
        "displayname": resolved_display_name,
        "icon": icon,
        "version": version,
        "handles": _device_handles_to_list(handles),
        "available_sites": normalize_available_sites(available_sites),
        "model": model,
        "device_type": device_type,
        "hardware_interface": (hardware_interface.model_dump(exclude_none=True) if hardware_interface else None),
        "metadata": dict(metadata or {}),
    }

    def decorator(cls):
        """把设备类注册为一个或多个资源模板（ResourceTemplate）。

        参数：``cls`` 是被装饰的设备类。返回：原设备类；注册身份重复时抛出
        ``ValueError``。库位（Site）模板已在外层冻结，本函数只绑定同一代元数据。
        """

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
    executor_kind: Optional["ExecutorKind"] = None,
    feedback_interval: Optional[float] = None,
    action_name: Optional[str] = None,
    displayname: str = "",
    exception_handling: bool = True,
    timeout: Optional[float] = None,
    execution_timeout: Optional[float] = None,
    error_policy: Optional[Dict[str, Any]] = None,
    default_on_user_timeout: str = "abort",
    resource_contract: Optional[Dict[str, Any]] = None,
    estimate_duration_fixed: Optional[float] = 60.0,
    estimate_duration_express: str = "",
    motion: Optional[Any] = None,
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
        action_name: 对外暴露的动作名。None 表示使用被装饰的方法名。
        displayname: 人类可读的动作显示名。为空时回退为实际 action_name。
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
        node_type: 动作的节点类型。
                   不填写时不写入注册表。
        executor_kind: 动作的运行时执行器类型。该字段与画布节点类型解耦；
                       不填写时由节点类型决定执行器。
        exception_handling: 是否将异常和超时上报给人工决策通道。
        timeout: 动作执行硬超时（秒）。
        execution_timeout: 业务层软超时（秒）。
        error_policy: 人工决策策略，支持 allow_retry / allow_skip / options。
        default_on_user_timeout: 人工决策等待超时后的默认动作。
        resource_contract: 只含参数名、资源角色和托管转换的声明式动作资源合同；
                           AST 会把它编译成 JSON，运行时不得自行取得或释放锁。
        estimate_duration_fixed: 预计时长兜底值（秒），默认 60 秒；None 表示不提供兜底
        estimate_duration_express: 根据动作入参计算预计时长的中缀表达式
        motion: 这个动作对应的资产机构动作，仅在名字或参数与资产行为类不同时填写。
                写机构动作名，或 {"action": 名字, "args": {机构参数: "用动作入参写的表达式"}}；
                驱动与资产行为类的方法同名、参数同名时不用写。只影响仿真与孪生的画面，不改变真机执行。

    Returns:
        把方法标记为规范动作合同（ActionContract）的装饰器。
    """

    def decorator(func: F) -> F:
        """把动作元数据附加到原始设备方法。

        Args:
            func: 需要注册为规范动作（Action）的设备方法。

        Returns:
            保留原函数签名和元数据的包装函数。
        """

        import asyncio as _asyncio

        if _asyncio.iscoroutinefunction(func):
            @wraps(func)
            async def wrapper(*args, **kwargs):
                """透明调用异步设备动作，并保留注册表元数据。"""

                return await func(*args, **kwargs)
        else:
            @wraps(func)
            def wrapper(*args, **kwargs):
                """透明调用同步设备动作，并保留注册表元数据。"""

                return func(*args, **kwargs)

        # action_type 为哨兵值 => 用户没传, 视为 None (UniLabJsonCommand)
        resolved_type = None if action_type is _ACTION_TYPE_UNSET else action_type
        if estimate_duration_fixed is not None:
            if not isinstance(estimate_duration_fixed, (int, float)):
                raise TypeError("estimate_duration_fixed 必须是秒数或 None")
            if estimate_duration_fixed < 0:
                raise ValueError("estimate_duration_fixed 不能小于 0")
        if not isinstance(estimate_duration_express, str):
            raise TypeError("estimate_duration_express 必须是字符串")
        if motion is not None:
            if isinstance(motion, str):
                valid_motion = bool(motion)
            else:
                valid_motion = (
                    isinstance(motion, dict)
                    and isinstance(motion.get("action"), str)
                    and bool(motion["action"])
                    and set(motion) <= {"action", "args"}
                    and isinstance(motion.get("args", {}), dict)
                    and all(isinstance(k, str) and isinstance(v, (str, int, float)) and not isinstance(v, bool)
                            for k, v in motion.get("args", {}).items())
                )
            if not valid_motion:
                raise TypeError('motion 必须是机构动作名，或 {"action": 名字, "args": {机构参数: 表达式}}')
        for field_name, value in (("timeout", timeout), ("execution_timeout", execution_timeout)):
            if value is not None and (
                not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0
            ):
                raise ValueError(f"{field_name} 必须是大于 0 的秒数或 None")
        if not isinstance(exception_handling, bool):
            raise TypeError("exception_handling 必须是布尔值")
        if default_on_user_timeout not in {"abort", "retry", "skip"}:
            raise ValueError("default_on_user_timeout 仅支持 abort/retry/skip")

        meta = {
            "action_type": resolved_type,
            "action_name": action_name,
            "displayname": displayname,
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
            "estimate_duration_fixed": estimate_duration_fixed,
            "estimate_duration_express": estimate_duration_express,
            "exception_handling": exception_handling,
            "timeout": timeout,
            "execution_timeout": execution_timeout,
            "default_on_user_timeout": default_on_user_timeout,
        }
        if feedback_interval is not None:
            meta["feedback_interval"] = feedback_interval
        if motion is not None:
            meta["motion"] = motion
        if node_type is not None:
            meta["node_type"] = node_type.value if isinstance(node_type, NodeType) else str(node_type)
        if executor_kind is not None:
            normalized_executor_kind = normalize_enum_value(executor_kind, ExecutorKind)
            allowed_executor_kinds = {kind.value for kind in ExecutorKind}
            if normalized_executor_kind not in allowed_executor_kinds:
                raise ValueError(f"不支持的 executor_kind: {executor_kind}")
            meta["executor_kind"] = normalized_executor_kind
        normalized_error_policy = None
        if error_policy:
            from unilabos.registry.action_policy import normalize_error_policy

            normalized_error_policy = normalize_error_policy(
                error_policy,
                default_on_user_timeout=default_on_user_timeout,
            )
            meta["error_policy"] = normalized_error_policy
        if resource_contract is not None:
            from unilabos.registry.action_resource_contract import (
                normalize_action_resource_contract,
            )

            meta["resource_contract"] = normalize_action_resource_contract(
                resource_contract
            )
        wrapper._action_registry_meta = meta  # type: ignore[attr-defined]
        wrapper._action_error_policy = normalized_error_policy  # type: ignore[attr-defined]
        wrapper._action_contract_kind = "typed"  # type: ignore[attr-defined]

        # 设置 _is_always_free 保持与旧 @always_free 装饰器兼容
        if always_free:
            wrapper._is_always_free = True  # type: ignore[attr-defined]

        return wrapper  # type: ignore[return-value]

    return decorator


def legacy_action(*args: Any, **kwargs: Any):
    """声明只供遗留设备传输层使用、不可成为规范工作流权威的动作。

    Args:
        args: 原 ``action`` 装饰器的兼容位置参数。
        kwargs: 原 ``action`` 装饰器的兼容关键字参数；不再接受字符串物料锁声明。

    Returns:
        具有遗留动作标记的设备方法装饰器。
    """

    typed_decorator = action(*args, **kwargs)

    def decorator(func: F) -> F:
        """把设备方法明确标记为遗留动作。

        Args:
            func: 仍需通过旧设备传输合同执行的方法。

        Returns:
            带遗留动作标记的包装函数。
        """

        wrapped = typed_decorator(func)
        wrapped._action_contract_kind = "legacy"  # type: ignore[attr-defined]
        return wrapped

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
    displayname: str = "",
    icon: str = "",
    version: str = "1.0.0",
    handles: Optional[List[_DeviceHandleBase]] = None,
    model: Optional[Dict[str, Any]] = None,
    class_type: str = "pylabrobot",
    metadata: Optional[Dict[str, Any]] = None,
    available_sites: Optional[List[Dict[str, Any]]] = None,
):
    """
    资源类/函数装饰器

    将类或工厂函数标记为一个 UniLab-OS 资源，附加注册表元数据。

    Args:
        id: 注册表唯一标识 (必填, 不可重复)
        category: 资源分类标签列表 (必填)
        description: 资源描述
        displayname: 人类可读的资源显示名称，缺失时默认使用 id
        icon: 图标路径
        version: 版本号
        handles: 端口列表 (InputHandle / OutputHandle)
        model: 可选的 3D 模型配置
        class_type: 资源实现类型 ("python" / "pylabrobot" / "unilabos")
        metadata: 物料扩展元数据，如供应商、规格、容量、孔位数等
        available_sites: 资源模板（ResourceTemplate）拥有的库位（Site）固定定义

    Returns:
        保存规范 Registry 元数据的类或工厂函数装饰器。

    Raises:
        ValueError: 资源身份或库位模板定义非法时抛出。
    """

    def decorator(obj):
        """把器材类或工厂函数注册为资源模板（ResourceTemplate）。

        参数：``obj`` 是被装饰的器材类或工厂函数。返回：原对象；注册身份重复时
        抛出 ``ValueError``。库位（Site）模板在写入 Registry 元数据前完成冻结。
        """

        meta = {
            "resource_id": id,
            "category": category,
            "description": description,
            "displayname": displayname,
            "icon": icon,
            "version": version,
            "handles": _device_handles_to_list(handles),
            "available_sites": normalize_available_sites(available_sites),
            "model": model,
            "class_type": class_type,
            "metadata": dict(metadata or {}),
        }
        obj._resource_registry_meta = meta

        if id in _registered_resources:
            previous = getattr(_registered_resources[id], "_resource_registry_meta", {}).get("metadata") or {}
            # 由同一个耗材资产生成的物料定义：几个设备包按各自的资产锁各生成一份，内容由资产决定。
            # 资产 ID 和包摘要都相同时视为同一份定义，不算重复；任何一项不同仍然报错。
            same_asset = (meta["metadata"].get("generated_from_asset") and previous.get("generated_from_asset")
                          and meta["metadata"].get("asset_id") == previous.get("asset_id")
                          and meta["metadata"].get("package_digest") == previous.get("package_digest"))
            if not same_asset:
                raise ValueError(f"@resource id 重复: '{id}' 已被 {_registered_resources[id]} 注册")
        _registered_resources[id] = obj

        return obj

    return decorator


def get_device_meta(cls, device_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """获取类上的 ``@device`` 装饰器元数据。

    当 device_id 存在且类使用 ids+id_meta 时，返回合并后的 meta
    (base_meta 与 id_meta[device_id] 深度合并)。

    参数：``cls`` 是设备类，``device_id`` 是可选的多设备模板身份。返回：不与逐
    设备覆盖共享库位容器的 Registry 元数据；类未声明 ``@device`` 时返回 ``None``；
    元数据被外部破坏而导致库位模板非法时抛出 ``ValueError``。
    """
    base = getattr(cls, "_device_registry_meta", None)
    if base is None:
        return None
    id_meta = getattr(cls, "_device_registry_id_meta", None) or {}
    if device_id is None or device_id not in id_meta:
        result = dict(base)
        result["available_sites"] = normalize_available_sites(
            base.get("available_sites")
        )
        ids = getattr(cls, "_device_registry_ids", None)
        result["device_id"] = device_id if device_id is not None else (ids[0] if ids else None)
        return result

    overrides = id_meta[device_id]
    result = dict(base)
    result["device_id"] = device_id
    for key in [
        "handles",
        "available_sites",
        "description",
        "displayname",
        "icon",
        "model",
        "metadata",
    ]:
        if key in overrides:
            val = overrides[key]
            if key == "handles" and isinstance(val, list):
                # handles 必须是 Handle 对象列表
                result[key] = [h.to_registry_dict() for h in val]
            elif key == "available_sites":
                result[key] = normalize_available_sites(val)
            elif key == "metadata" and isinstance(val, dict):
                result[key] = {**(base.get("metadata") or {}), **val}
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

    适用于 Side、DataSource、NodeType、ExecutorKind 等继承自 ``str, Enum`` 的装饰器枚举。

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
