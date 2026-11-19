"""runtime.v1 endpoint 能力快照组装。

把执行适配器（经 ``adapter_registry`` 暴露）维护的在线设备与
``action_value_mappings`` 投影成 runtime.v1 的
``device_routes`` + ``action_capabilities``。

``GET /api/v1/runtime/endpoints`` 返回该快照，供微前端的设备页、单点动作
参数表单与工作流画布节点目录使用。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Tuple

from unilabos.server.database.tables.runtime import (
    DeviceActionCapability,
    DeviceRoute,
)
from unilabos.protocol.base import canonical_hash

# descriptor 白名单：registry action config 里对前端有用且 JSON-safe 的字段。
# `type`（动作消息类型）在运行期可能被 resolve 成类对象，单独字符串化为
# action_type，不进 descriptor。
_DESCRIPTOR_FIELDS = (
    "display_name",
    "schema",
    "goal_default",
    "handles",
    "placeholder_keys",
    "always_free",
    "feedback_interval",
    "node_type",
    "materials_need_lock",
    "timeout",
    "execution_timeout",
    "goal",
    "feedback",
    "result",
)

_HOST_NODE_REGISTRY_NAME = "host_node"


def _registry_name_for_device(adapter: Any, device_id: str) -> str:
    """从两种执行适配器的运行对象读取设备 registry 名。

    runtime.v1 的设备 route 原本只有 ``device_uuid``，不能可靠地区分
    ``host_node/manual_confirm`` 与普通设备恰好提供的同名动作。HostLink
    已有 ``_device_descriptors``；ROS2 HostNode 则把通用包装节点放在
    ``devices_instances``。这里仅做只读探测，旧/精简测试适配器没有这些
    属性时由稳定的 host_node id 约定兜底。
    """

    registry_names = getattr(adapter, "device_registry_names", None)
    if isinstance(registry_names, dict):
        value = registry_names.get(device_id)
        if isinstance(value, str) and value.strip():
            return value.strip()

    descriptors = getattr(adapter, "_device_descriptors", None)
    if isinstance(descriptors, dict):
        descriptor = descriptors.get(device_id)
        if isinstance(descriptor, dict):
            for key in ("registry_name", "template_name", "class"):
                value = descriptor.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()

    instances = getattr(adapter, "devices_instances", None)
    if isinstance(instances, dict):
        instance = instances.get(device_id)
        candidates = (
            instance,
            getattr(instance, "_ros_node", None),
            getattr(instance, "ros_node_instance", None),
        )
        for candidate in candidates:
            value = getattr(candidate, "registry_name", None)
            if isinstance(value, str) and value.strip():
                return value.strip()

    runtime = getattr(adapter, "runtime", None)
    local = getattr(runtime, "local", None)
    local_devices = getattr(local, "devices", None)
    if isinstance(local_devices, dict):
        instance = local_devices.get(device_id)
        value = getattr(instance, "registry_name", None)
        if isinstance(value, str) and value.strip():
            return value.strip()

    return ""


def _is_host_node_device(adapter: Any, device_id: str) -> tuple[bool, str]:
    registry_name = _registry_name_for_device(adapter, device_id)
    # 配置允许把 host 服务实例重命名；没有 registry 元数据的旧适配器
    # 仍遵循 host_node / host_node_* 的兼容约定。
    is_host_node = registry_name == _HOST_NODE_REGISTRY_NAME or (
        not registry_name
        and (device_id == _HOST_NODE_REGISTRY_NAME or device_id.startswith("host_node_"))
    )
    return is_host_node, registry_name


def _json_safe(value: Any) -> Any:
    """把 action config 片段规范成纯 JSON 结构（非常规对象字符串化）。"""

    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def build_endpoint_capabilities(
    adapter: Any, *, observed_at_ms: int
) -> Tuple[List[DeviceRoute], List[DeviceActionCapability]]:
    """从执行适配器构建 (device_routes, action_capabilities)。

    HostNode 的两种 transport 形态均满足以下适配器契约：
    ``devices_names``（device_id → namespace）为在线设备面，
    ``_action_value_mappings``（device_id → action_name → config）为动作面。
    """

    devices_names: Dict[str, str] = dict(getattr(adapter, "devices_names", None) or {})
    mappings: Dict[str, Dict[str, Any]] = dict(
        getattr(adapter, "_action_value_mappings", None) or {}
    )
    routes: List[DeviceRoute] = []
    capabilities: List[DeviceActionCapability] = []
    for device_id in sorted(devices_names):
        is_host_node, registry_name = _is_host_node_device(adapter, device_id)
        route_config: Dict[str, Any] = {}
        if registry_name:
            route_config["registry_name"] = registry_name
        if is_host_node:
            # 这是 route 元数据而非新的数据库列：旧快照仍可按空 config
            # 读取，新前端据此只把真正的 host_node 放入人工确认候选。
            route_config["is_host_node"] = True
        routes.append(
            DeviceRoute(
                route_uuid=f"route:{device_id}",
                device_uuid=device_id,
                driver_key=device_id,
                enabled=True,
                selected=True,
                config_hash=canonical_hash(route_config),
                config=route_config,
            )
        )
        device_actions = mappings.get(device_id) or {}
        for action_name in sorted(device_actions):
            entry = device_actions[action_name]
            if not isinstance(entry, dict):
                continue
            descriptor = _json_safe(
                {key: entry[key] for key in _DESCRIPTOR_FIELDS if key in entry}
            )
            action_type = entry.get("type")
            capabilities.append(
                DeviceActionCapability(
                    device_uuid=device_id,
                    action_name=str(action_name),
                    action_type=(
                        str(getattr(action_type, "__name__", action_type))
                        if action_type
                        else None
                    ),
                    concurrency_mode=(
                        "unbounded" if entry.get("always_free") else "exclusive"
                    ),
                    state="active",
                    availability="unknown",
                    descriptor=descriptor,
                    descriptor_hash=canonical_hash(descriptor),
                    observed_at_ms=observed_at_ms,
                )
            )
    # 设备图里有、但本 backend 跑不了而被跳过的设备：登记一条停用的 route（不带动作能力），
    # 设备目录据此显示"不可用"及原因，而不是让它像离线设备一样无声无息。
    unavailable: Dict[str, Dict[str, Any]] = dict(
        getattr(adapter, "unavailable_devices", None) or {}
    )
    for device_id in sorted(set(unavailable) - set(devices_names)):
        record = _json_safe(dict(unavailable[device_id] or {}))
        route_config = {"unavailable": record}
        registry_name = str(record.get("registry_name") or "")
        if registry_name:
            route_config["registry_name"] = registry_name
        routes.append(
            DeviceRoute(
                route_uuid=f"route:{device_id}",
                device_uuid=device_id,
                driver_key=device_id,
                enabled=False,
                selected=False,
                config_hash=canonical_hash(route_config),
                config=route_config,
            )
        )
    return routes, capabilities


__all__ = ["build_endpoint_capabilities"]
