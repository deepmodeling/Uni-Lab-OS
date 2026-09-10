"""把公开动作合同编译为本地 ROS 运行时传输合同。"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from copy import deepcopy
from typing import Any

from unilabos.ros.msgs.message_converter import ros_action_to_json_schema
from unilabos_msgs.action import StrSingleInput

_SYNC_COMMAND_ACTION = "_execute_driver_command"
_ASYNC_COMMAND_ACTION = "_execute_driver_command_async"


def _action_type_name(action_type: Any) -> str:
    """把动作类型归一化为可判定传输种类的稳定名称。

    参数：``action_type`` 是公开动作映射中的 ROS 类型类或字符串类型名。
    返回：优先返回类的 ``__name__``，否则返回原字符串或字符串化结果。
    异常：无；无法提供名称的值会得到空字符串或其普通字符串表示。
    """

    if isinstance(action_type, str):
        return action_type
    return str(getattr(action_type, "__name__", action_type or ""))


def _generic_command_mapping() -> dict[str, Any]:
    """构造内部 JSON 驱动命令所需的 ROS 动作映射。

    参数：无。
    返回：以 ``StrSingleInput`` 承载 JSON 字符串的独立可变映射。
    异常：ROS Schema 转换失败时传播原始异常，禁止产生不完整运行时合同。
    """

    return {
        "type": StrSingleInput,
        "goal": {"string": "string"},
        "feedback": {},
        "result": {},
        "schema": ros_action_to_json_schema(StrSingleInput),
        "goal_default": {"string": ""},
        "handles": {},
    }


def build_runtime_action_mappings(
    public_action_mappings: Mapping[str, Any],
) -> dict[str, Any]:
    """从公开动作投影派生设备节点专用的 ROS 运行时映射。

    参数：``public_action_mappings`` 是包目录（Package Catalog）或注册表
    （Registry）发布的业务动作映射，不应包含本地传输实现要求。
    返回：深复制后的运行时映射；同步/异步 JSON 命令分别补齐对应内部端点。
    异常：输入内容无法深复制或内部 ROS Schema 无法生成时传播原始异常；调用方
    必须停止设备初始化，不能以缺失端点的状态继续宣告可调度。
    """

    # ``runtime_mappings`` 是设备实例私有副本，后续补齐不会污染公开投影。
    runtime_mappings = deepcopy(dict(public_action_mappings))
    # 两个布尔值分别表示当前设备是否需要同步或异步 JSON 命令传输端点。
    requires_sync = _SYNC_COMMAND_ACTION in runtime_mappings
    requires_async = _ASYNC_COMMAND_ACTION in runtime_mappings

    for action_name, action_mapping in public_action_mappings.items():
        if action_name.startswith("_execute_driver_command"):
            continue
        # ``mapping_type`` 是业务动作选择 ROS 原生传输或 JSON 命令传输的类型名。
        mapping_type = _action_type_name(
            action_mapping.get("type") if isinstance(action_mapping, Mapping) else None
        )
        if not action_name.startswith("auto-") and not mapping_type.startswith(
            "UniLabJsonCommand"
        ):
            continue
        if mapping_type.startswith("UniLabJsonCommandAsync"):
            requires_async = True
        else:
            requires_sync = True

    if requires_sync and _SYNC_COMMAND_ACTION not in runtime_mappings:
        runtime_mappings[_SYNC_COMMAND_ACTION] = _generic_command_mapping()
    if requires_async and _ASYNC_COMMAND_ACTION not in runtime_mappings:
        runtime_mappings[_ASYNC_COMMAND_ACTION] = _generic_command_mapping()
    return runtime_mappings


def required_action_endpoint_ids(
    device_id: str,
    runtime_action_mappings: Mapping[str, Any],
) -> tuple[str, ...]:
    """计算本地设备宣告可调度前必须全部就绪的 ROS 动作端点。

    参数：``device_id`` 是本地设备实例身份；``runtime_action_mappings`` 是已补齐
    内部动作的设备运行时映射。
    返回：去重并稳定排序的绝对 ROS 动作端点身份元组。
    异常：无；映射值缺失时按非 JSON 原生动作处理，由后续客户端创建负责报错。
    """

    # ``endpoint_names`` 聚合业务动作实际路由到的内部或原生 ROS 端点名称。
    endpoint_names: set[str] = set()
    for action_name, action_mapping in runtime_action_mappings.items():
        if action_name.startswith("_execute_driver_command"):
            endpoint_names.add(action_name)
            continue
        # ``mapping_type`` 决定业务动作是否折叠到同步或异步通用命令端点。
        mapping_type = _action_type_name(
            action_mapping.get("type") if isinstance(action_mapping, Mapping) else None
        )
        if action_name.startswith("auto-") or mapping_type.startswith(
            "UniLabJsonCommand"
        ):
            endpoint_names.add(
                _ASYNC_COMMAND_ACTION
                if mapping_type.startswith("UniLabJsonCommandAsync")
                else _SYNC_COMMAND_ACTION
            )
        else:
            endpoint_names.add(action_name)

    return tuple(
        f"/devices/{device_id}/{endpoint_name}"
        for endpoint_name in sorted(endpoint_names)
    )


def wait_for_action_endpoints(
    action_clients: Mapping[str, Any],
    required_endpoint_ids: Iterable[str],
    *,
    wait_timeout: float,
) -> bool:
    """在总超时内等待每个必需 ROS 动作客户端连接到服务端。

    参数：``action_clients`` 按绝对端点身份保存客户端；``required_endpoint_ids``
    是必须全部就绪的端点；``wait_timeout`` 是所有端点共享的最长等待秒数。
    返回：全部必需端点存在且已就绪时返回 ``True``，否则返回 ``False``。
    异常：客户端探测异常被视为未就绪并继续等待，防止异常导致设备错误上线。
    """

    # ``required_ids`` 固化一次性迭代器，确保每轮探测检查同一完整集合。
    required_ids = tuple(dict.fromkeys(required_endpoint_ids))
    if not required_ids:
        return True
    # ``deadline`` 是全部端点共享的单一截止时间，避免端点数放大总等待时长。
    deadline = time.monotonic() + max(wait_timeout, 0.0)
    while True:
        all_ready = True
        for endpoint_id in required_ids:
            # ``client`` 是当前端点已创建的 ROS 动作客户端；缺失即未就绪。
            client = action_clients.get(endpoint_id)
            try:
                if client is None or not client.server_is_ready():
                    all_ready = False
                    break
            # DDS 客户端实现可能抛出不同中间件异常；门禁统一按未就绪处理。
            except Exception:  # noqa: BLE001
                all_ready = False
                break
        if all_ready:
            return True
        # 零超时仍会完成一次同步探测，便于调用方实现非阻塞门禁。
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.05, remaining))


__all__ = [
    "build_runtime_action_mappings",
    "required_action_endpoint_ids",
    "wait_for_action_endpoints",
]
