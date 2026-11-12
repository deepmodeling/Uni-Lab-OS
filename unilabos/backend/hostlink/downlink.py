"""Host→Device 物料/管理下行链路（append_resource / 资源树同步 / 设备管理）。

本模块以纯函数提供下行能力。跨机调用使用 HostLink RPC，本进程调用直接
调度设备实例协程；ROS2 与 HostLink backend 共用同一业务逻辑：

- 本进程设备（含 host_node 服务设备）：ros2 形态查 ``registered_devices``、
  hostlink 形态查 ``HostLinkLocalRuntime``，把协程调度到对应 executor 执行；
- 跨机（Slave）设备：Host 经 ``HostLinkServer.request_device`` 下行 RPC；Slave
  进程用 :func:`register_hostlink_resource_handlers` 把 handler 挂到
  ``HostLinkClient``，收到请求后同样经协程桥调用本地设备节点实例方法。

微后端权威完成变更后的设备侧分发入口是模块级
:func:`notify_resource_tree_update`（与 materials 的 get/set/discard 工具
函数同范式），不挂在任何 host 编排类上。
"""

from __future__ import annotations

from typing import Any, Dict, Iterator, List, Optional, Tuple

from unilabos.backend.runtime.async_utils import run_node_coroutine
from unilabos.backend.hostlink.protocol import ActionType
from unilabos.backend.hostlink.server import get_hostlink_server
from unilabos.utils import logger

DEFAULT_DOWNLINK_TIMEOUT = 30.0

# 调度权威进程没有设备也没有 HostLink server：物料投影下行经控制面 WS 交给 Host 进程
# 在进程内执行 /api/v1/hostlink/{material-sync,notify-device}（server/api/host_relay.py）。
_remote_device_relay = False


def configure_remote_device_relay(enabled: bool) -> None:
    global _remote_device_relay
    _remote_device_relay = bool(enabled)


def remote_device_relay() -> bool:
    return _remote_device_relay


def _relay_post(path: str, payload: Dict[str, Any], timeout: float) -> Any:
    import json

    from unilabos.client.utils.envelope import EnvelopeError
    from unilabos.server.api.edge_proxy import edge_http, edge_response_data

    response = edge_http(
        "POST",
        path,
        headers={"content-type": "application/json"},
        body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        timeout=timeout,
    )
    if response is None:
        raise RuntimeError(f"Host 进程不在线，无法经控制面执行 {path}")
    try:
        return edge_response_data(response)
    except EnvelopeError as exc:
        raise RuntimeError(f"Host 中继 {path} 失败（业务码 {exc.code}）: {exc.error}") from exc
    except ValueError as exc:
        raise RuntimeError(f"Host 中继 {path} 返回无效响应（HTTP {response.status_code}）") from exc


def remote_material_sync_dispatcher(command: Any) -> Dict[str, Any]:
    """materials 权威的 resource-sync dispatcher：经 Host 中继投影到设备。"""

    data = command.model_dump(mode="json", exclude_none=False)
    return _relay_post("/api/v1/hostlink/material-sync", data, DEFAULT_DOWNLINK_TIMEOUT)


def get_local_device_node(device_id: str) -> Optional[Any]:
    """按 device_id 取本进程的设备节点实例（DeviceNode 子类，含 host_node）。

    ros2 形态设备节点登记在 ``registered_devices``；hostlink 形态登记在
    ``HostLinkLocalRuntime.devices``。两处的节点都实现 DeviceNode 协程契约
    （apply_resource_tree_update / append_resource / material_sync /
    device_manage），下行分发对 backend 无感。
    """
    from unilabos.config.config import BasicConfig

    if BasicConfig.backend == "hostlink":
        from unilabos.backend.hostlink.main_hostlink_run import get_runtime

        runtime = get_runtime()
        if runtime is None:
            return None
        return runtime.local.get_device(str(device_id))

    from unilabos.backend.ros2.base_device_node import registered_devices

    info = registered_devices.get(str(device_id))
    if info is None:
        return None
    return info.get("base_node_instance")


def iter_local_device_nodes() -> Iterator[Tuple[str, Any]]:
    """遍历本进程全部设备节点 ``(device_id, DeviceNode)``（含 host_node 服务设备）。

    与 :func:`get_local_device_node` 同一数据源；供调用方做本进程零通信反查
    （如「物料此刻在哪台设备台面上」——扫各节点 tracker），未命中再走
    权威 / 跨机链路。
    """
    from unilabos.config.config import BasicConfig

    if BasicConfig.backend == "hostlink":
        from unilabos.backend.hostlink.main_hostlink_run import get_runtime

        runtime = get_runtime()
        if runtime is None:
            return
        for device_id, node in list(runtime.local.devices.items()):
            yield str(device_id), node
        return

    try:
        from unilabos.backend.ros2.base_device_node import registered_devices
    except ImportError:  # 无 rclpy 的进程（纯 server / 单测）：本进程没有 ROS 设备节点
        return
    for device_id, info in list(registered_devices.items()):
        node = info.get("base_node_instance")
        if node is not None:
            yield str(device_id), node


def _require_local_device_node(device_id: str) -> Any:
    node = get_local_device_node(device_id)
    if node is None:
        raise ValueError(f"本进程没有设备节点: {device_id!r}")
    return node


def local_resource_tree_sync(data: Dict[str, Any]) -> Dict[str, Any]:
    """``RESOURCE_TREE_SYNC`` 的本进程执行：分发资源树变更到设备节点。

    data: {"device_id": str, "operations": [{"action", "data", ...}]}
    """
    device_id = str(data.get("device_id") or "")
    operations = list(data.get("operations") or [])
    node = _require_local_device_node(device_id)
    return run_node_coroutine(node, node.apply_resource_tree_update(operations))


def local_resource_append(data: Dict[str, Any]) -> Dict[str, Any]:
    """``RESOURCE_APPEND`` 的本进程执行：把微后端权威物料挂载到设备节点。

    data: {"device_id": str, "resource_uuid": [...], "bind_parent_id": str,
           "bind_location": {x,y,z}, "other_calling_param": {...}}
    """
    device_id = str(data.get("device_id") or "")
    node = _require_local_device_node(device_id)
    payload = {key: value for key, value in data.items() if key != "device_id"}
    return run_node_coroutine(node, node.append_resource(payload))


def local_material_sync(data: Dict[str, Any]) -> Dict[str, Any]:
    """``MATERIAL_SYNC`` 的本进程执行：微后端 transfer 的设备侧投影（unload/load）。

    data: {"device_id": str, "action": "unload"|"load", "material_uuids": [...],
           "transfer_uuid": str, "destination_site_uuids": [...]}
    """
    device_id = str(data.get("device_id") or "")
    node = _require_local_device_node(device_id)
    return run_node_coroutine(node, node.material_sync(dict(data)))


def local_device_manage(data: Dict[str, Any]) -> Dict[str, Any]:
    """``DEVICE_MANAGE`` 的本进程执行：动态 add/remove 设备。

    data: {"device_id": str（目标节点）, "action": "add"|"remove",
           "data": {...device config, 含 id...}}
    """
    device_id = str(data.get("device_id") or "")
    node = _require_local_device_node(device_id)
    payload = {key: value for key, value in data.items() if key != "device_id"}
    return run_node_coroutine(node, node.device_manage(payload))


def register_hostlink_resource_handlers(client: Any) -> None:
    """把物料/管理下行 handler 挂到 Slave 的 HostLinkClient（幂等）。"""
    client.register_handler(ActionType.RESOURCE_TREE_SYNC, local_resource_tree_sync)
    client.register_handler(ActionType.RESOURCE_APPEND, local_resource_append)
    client.register_handler(ActionType.MATERIAL_SYNC, local_material_sync)
    client.register_handler(ActionType.DEVICE_MANAGE, local_device_manage)


async def material_dispatch(
    device_id: str, action_type: str, payload: Dict[str, Any]
) -> Dict[str, Any]:
    """host 物料动作的下行通道（本进程直调 / 跨机 HostLink RPC）。

    链路不走 ROS service：本进程设备直接 await 实例协程，跨机（Slave）
    设备经 HostLink 下行 RPC（同步 RPC，多线程 executor 下阻塞可接受；
    设备不在线/未启用 HostLink 时直接抛错，物料链路不回退 ROS 发现）。

    :class:`unilabos.backend.host_services.HostServices` 未显式注入 dispatcher
    时使用本函数。
    """
    if action_type == ActionType.RESOURCE_APPEND:
        local_node = get_local_device_node(device_id)
        if local_node is not None:
            return await local_node.append_resource(dict(payload))
        return append_resource_via_hostlink(device_id, payload, DEFAULT_DOWNLINK_TIMEOUT)
    if action_type == ActionType.RESOURCE_TREE_SYNC:
        return sync_resource_tree_to_device(
            device_id, payload["operations"], DEFAULT_DOWNLINK_TIMEOUT
        )
    raise ValueError(f"未支持的物料下行类型: {action_type}")


def sync_resource_tree_to_device(
    device_id: str,
    operations: List[Dict[str, Any]],
    timeout: float = DEFAULT_DOWNLINK_TIMEOUT,
) -> Dict[str, Any]:
    """Host 侧资源树变更分发：本进程直调，否则经 HostLink 下行到 Slave。

    设备既不在本进程、又不在 HostLink 在线表时抛异常（物料链路不回退 ROS 发现）。
    """
    payload = {"device_id": str(device_id), "operations": list(operations)}
    if get_local_device_node(device_id) is not None:
        return local_resource_tree_sync(payload)
    server = get_hostlink_server()
    if server is None:
        raise RuntimeError(f"HostLink server 未启动，无法向跨机设备 {device_id!r} 分发资源树变更")
    return server.request_device(str(device_id), ActionType.RESOURCE_TREE_SYNC, payload, timeout)


def append_resource_via_hostlink(
    device_id: str,
    payload: Dict[str, Any],
    timeout: float = DEFAULT_DOWNLINK_TIMEOUT,
) -> Dict[str, Any]:
    """Host 侧跨机挂载请求：经 HostLink 下行到广播该设备的 Slave。"""
    server = get_hostlink_server()
    if server is None:
        raise RuntimeError(f"HostLink server 未启动，无法向跨机设备 {device_id!r} 分发物料挂载")
    data = {"device_id": str(device_id), **payload}
    return server.request_device(str(device_id), ActionType.RESOURCE_APPEND, data, timeout)


def material_sync_to_device(
    device_id: str,
    command: Dict[str, Any],
    timeout: float = DEFAULT_DOWNLINK_TIMEOUT,
) -> Dict[str, Any]:
    """Host 侧 transfer 投影分发：本进程直调，否则经 HostLink 下行到 Slave。"""
    payload = {**command, "device_id": str(device_id)}
    if get_local_device_node(device_id) is not None:
        return local_material_sync(payload)
    server = get_hostlink_server()
    if server is None:
        if _remote_device_relay:
            return _relay_post("/api/v1/hostlink/material-sync", payload, timeout)
        raise RuntimeError(f"HostLink server 未启动，无法向跨机设备 {device_id!r} 分发物料同步")
    return server.request_device(str(device_id), ActionType.MATERIAL_SYNC, payload, timeout)


def device_manage_to_device(
    device_id: str,
    action: str,
    config: Dict[str, Any],
    timeout: float = DEFAULT_DOWNLINK_TIMEOUT,
) -> Dict[str, Any]:
    """Host 侧设备管理分发：本进程直调，否则经 HostLink 下行到 Slave。"""
    payload = {
        "device_id": str(device_id),
        "action": str(action),
        "data": dict(config),
    }
    if get_local_device_node(device_id) is not None:
        return local_device_manage(payload)
    server = get_hostlink_server()
    if server is None:
        raise RuntimeError(f"HostLink server 未启动，无法向跨机设备 {device_id!r} 分发设备管理")
    return server.request_device(str(device_id), ActionType.DEVICE_MANAGE, payload, timeout)


def notify_resource_tree_update(
    device_id: str, action: str, resource_uuid_list: List[str]
) -> Optional[bool]:
    """把权威已完成的物料变更（前端经微后端）分发到目标设备。

    与 materials 的 get/set/discard 工具同范式的模块级函数——微后端直接
    调用，不经任何 host 编排类。本进程设备直调实例协程，跨机（Slave）
    设备经 HostLink 下行 RPC。

    Returns:
        True 分发完成；False 分发失败；None 设备不可达（有意跳过）。
    """
    operations = [{"action": str(action), "data": list(resource_uuid_list)}]
    try:
        if get_local_device_node(device_id) is None:
            server = get_hostlink_server()
            if server is None and _remote_device_relay:
                # 调度权威进程：设备都在 Host 进程里，整条通知转交 Host 中继
                result = _relay_post(
                    "/api/v1/hostlink/notify-device",
                    {"device_id": str(device_id), "action": str(action), "resource_uuids": list(resource_uuid_list)},
                    DEFAULT_DOWNLINK_TIMEOUT,
                )
                notified = result.get("notified") if isinstance(result, dict) else None
                return notified if notified is None else bool(notified)
            if server is None or not server.has_device(device_id):
                logger.info(
                    "[Downlink] 设备 %s 不在本进程、也不在 HostLink 在线表，跳过资源树 %s 分发",
                    device_id,
                    action,
                )
                return None
        sync_resource_tree_to_device(device_id, operations, DEFAULT_DOWNLINK_TIMEOUT)
        return True
    except Exception:
        logger.exception("[Downlink] 资源树 %s 分发到 %s 失败", action, device_id)
        return False


__all__ = [
    "DEFAULT_DOWNLINK_TIMEOUT",
    "append_resource_via_hostlink",
    "configure_remote_device_relay",
    "device_manage_to_device",
    "get_local_device_node",
    "iter_local_device_nodes",
    "local_device_manage",
    "local_material_sync",
    "local_resource_append",
    "local_resource_tree_sync",
    "material_dispatch",
    "material_sync_to_device",
    "notify_resource_tree_update",
    "register_hostlink_resource_handlers",
    "remote_device_relay",
    "remote_material_sync_dispatcher",
    "run_node_coroutine",
    "sync_resource_tree_to_device",
]
