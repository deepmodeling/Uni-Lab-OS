"""在 HostLink Host 进程中装配 host_node 服务设备。

host_node 的动作定义与实现是 backend 无关的
:class:`unilabos.backend.host_services.HostServices`；本模块把该驱动注册进
本进程 runtime（device_id 即 ``BasicConfig.host_node_name``），使微前端单点
动作（ad_hoc_device_action）与工作流画布节点在两种 backend 下都能对同一
device_id 提交这些动作。

action_value_mappings 直接复用 registry 中 AST 扫描出的 host_node 条目
（HostLink schema-only 模式保留 JSON 描述），保证 schema / placeholder /
handles 与 ROS2 模式完全一致。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from unilabos.backend.host_services import HOST_SERVICE_ACTIONS, HostServices
from unilabos.config.config import resolve_host_node_name
from unilabos.utils.log import logger


def host_service_action_mappings() -> Dict[str, Dict[str, Any]]:
    """从 registry 的 host_node 条目取内置服务动作的 action_value_mappings。

    当前定义直接使用稳定动作名；``auto-`` 回退兼容进程内缓存的早期 AST
    描述。
    """
    from unilabos.registry.registry import lab_registry

    entry = lab_registry.device_type_registry.get("host_node", {})
    mappings = entry.get("class", {}).get("action_value_mappings", {})
    result: Dict[str, Dict[str, Any]] = {}
    for name in HOST_SERVICE_ACTIONS:
        if name in mappings:
            result[name] = mappings[name]
        elif f"auto-{name}" in mappings:
            result[name] = mappings[f"auto-{name}"]
    return result


def register_host_services(backend: Any) -> Optional[Any]:
    """在 HostLink Host 进程注册内置 host 服务设备（幂等）。

    registry 未加载（如精简单测环境）时跳过并返回 None——此时没有
    schema/placeholder 描述，注册空动作设备没有意义。
    """
    from unilabos.backend.hostlink.local_runtime import HostLinkDriverSpec

    device_id = resolve_host_node_name()
    existing = backend.local.devices.get(device_id)
    if existing is not None:
        return existing
    mappings = host_service_action_mappings()
    if not mappings:
        logger.info(
            "[HostServices] registry 中没有 host_node 动作描述，跳过内置服务设备注册"
        )
        return None
    # host node 全图唯一（按 class 判别）；图中声明过时复用其资源 uuid，
    # 保持权威身份稳定。图节点 id 与配置实例名不一致时以配置为准。
    graph_uuids = getattr(backend.local, "graph_host_resource_uuids", {}) or {}
    resource_uuid = next(iter(graph_uuids.values()), "")
    graph_declared_id = next(iter(graph_uuids), "")
    if graph_declared_id and graph_declared_id != device_id:
        logger.info(
            f"[HostServices] 图中 host node id '{graph_declared_id}' 与配置实例名 "
            f"'{device_id}' 不一致，以配置为准"
        )
    return backend.local.add_driver(
        HostLinkDriverSpec(
            device_id=device_id,
            driver_class=HostServices,
            config={"backend": backend},
            registry_name="host_node",
            display_name="Host 服务",
            resource_uuid=str(resource_uuid),
            action_names=tuple(mappings),
            action_value_mappings=mappings,
        )
    )


__all__ = [
    "HOST_SERVICE_ACTIONS",
    "HostServices",
    "host_service_action_mappings",
    "register_host_services",
]
