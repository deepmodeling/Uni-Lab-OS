"""workstation XDL protocol 编排的 backend 无关共享逻辑。

ROS2 :class:`unilabos.client.host.ros2.presets.workstation.ROS2WorkstationNode` 与
HostLink :class:`unilabos.client.host.hostlink.workstation.WorkstationNode`
共用本模块：协议名/参数模型解析、资源占位展开与协议结束后的资源回写
（均走 DeviceNode 权威 API：get_resource / update_resource）。

protocol 步骤生成（:mod:`unilabos.experiments.compile`）与参数模型
（:mod:`unilabos.experiments.models`）本身即纯 Python，双 backend 同一份。

协议编译用的物理拓扑图有两个来源：本进程 ``-g`` 装配的启动图，以及 Slave 经 HostLink 从 Host
取到的完整图（:mod:`unilabos.client.host.hostlink.physical_graph`）。Slave 只装配了自己那份图
（受管 Slave 是一个根设备的子树），跨子树的连线、别处的容器与设备只在 Host 的图里；Slave 也可能
在另一台机器上，所以完整图只经 HostLink 同步。
"""

from __future__ import annotations

import threading
import uuid
from typing import TYPE_CHECKING, Any, List, Optional, Tuple

from unilabos.experiments import models as protocol_models
from unilabos.utils import logger

if TYPE_CHECKING:
    from unilabos.client.host.runtime.node import DeviceNode


class WorkstationNodeTempError(Exception):
    """协议步骤内的日志占位（log_message），不代表失败。"""


#: 本进程启动图的物理拓扑图（``networkx.Graph``，节点为设备 / 物料，边带端口）。
#: Host 与 Slave 装配 ``-g`` 图时写入（``unilabos.app.main._load_startup_graph``）。
_physical_setup_graph: Any = None
#: 本进程启动图的版本标记与它所标记的图对象：Host 在 HostLink 握手 / 心跳应答里带上标记，Slave 据此
#: 判断要不要重新取图。图对象换了（重新登记，或测试直接替换 ``_physical_setup_graph``）就换新标记。
_physical_setup_graph_revision: Tuple[Any, Optional[str]] = (None, None)
#: Slave 经 HostLink 从 Host 取到的完整物理拓扑图与它的版本标记
_host_physical_setup_graph: Any = None
_host_physical_setup_graph_revision: Optional[str] = None
#: 合并结果缓存：(Host 完整图, 本机启动图, 合并图)
_merged_physical_setup_graph: Optional[Tuple[Any, Any, Any]] = None
_graph_lock = threading.Lock()


def set_physical_setup_graph(graph: Any) -> None:
    """登记本进程启动图的物理拓扑图（Host / Slave 装配设备图后调用）。"""
    global _physical_setup_graph
    _physical_setup_graph = graph


def local_physical_setup_graph() -> Tuple[Any, Optional[str]]:
    """本进程启动图的物理拓扑图与版本标记，未装配设备图时为 ``(None, None)``。

    Host 经 HostLink 提供给 Slave 的就是它（``graph.physical.get``）。
    """
    global _physical_setup_graph_revision
    graph = _physical_setup_graph
    if graph is None:
        return None, None
    with _graph_lock:
        owner, revision = _physical_setup_graph_revision
        if owner is not graph or revision is None:
            revision = uuid.uuid4().hex
            _physical_setup_graph_revision = (graph, revision)
    return graph, revision


def set_host_physical_setup_graph(graph: Any, revision: Optional[str]) -> None:
    """Slave 登记从 Host 取到的完整物理拓扑图；``graph=None`` 时清掉，协议编译回到只用本机启动图。"""
    global _host_physical_setup_graph, _host_physical_setup_graph_revision
    with _graph_lock:
        _host_physical_setup_graph = graph
        _host_physical_setup_graph_revision = revision if graph is not None else None


def host_physical_setup_graph_revision() -> Optional[str]:
    """Slave 当前用着的 Host 完整图的版本标记；没有（只用本机启动图）时为 None。"""
    return _host_physical_setup_graph_revision


def merge_physical_setup_graphs(host_graph: Any, local_graph: Any) -> Any:
    """Host 的完整图为底，叠上本机启动图：节点与连线取并集，两边都有的以本机的属性为准。

    同一节点 id 在两边是不同的物料（uuid 不同，只是各自的图里恰好重名）时只留本机那个：Host 那个
    节点连同它的连线不进合并图，免得协议把本机的容器接到 Host 图里别的设备上。
    """
    if local_graph is None:
        return host_graph
    import networkx as nx

    clashes = [
        node
        for node, data in local_graph.nodes(data=True)
        if host_graph.has_node(node) and host_graph.nodes[node].get("uuid") != data.get("uuid")
    ]
    base = host_graph
    if clashes:
        base = host_graph.copy()
        base.remove_nodes_from(clashes)
        logger.warning(
            "[Workstation] Host 物理拓扑图与本机启动图有同名不同 uuid 的节点，以本机为准，"
            f"Host 图里的这些节点及其连线不参与协议编译: {sorted(map(str, clashes))}"
        )
    return nx.compose(base, local_graph)


def physical_setup_graph() -> Any:
    """协议编译器 ``generator(G=...)`` 使用的物理拓扑图；未装配设备图时为 None。

    Slave 从 Host 取到完整图之后返回 Host 完整图叠上本机启动图的结果
    （:func:`merge_physical_setup_graphs`）；Host 自己、Slave 还没取到（没连上 Host、``--slave_no_host``
    离线启动、旧版 Host 不提供）以及 ``--disable_hostlink``（纯 DDS）时就是本进程启动图。
    """
    global _merged_physical_setup_graph
    local = _physical_setup_graph
    host = _host_physical_setup_graph
    if host is None:
        return local
    with _graph_lock:
        cached = _merged_physical_setup_graph
        if cached is not None and cached[0] is host and cached[1] is local:
            return cached[2]
        merged = merge_physical_setup_graphs(host, local)
        _merged_physical_setup_graph = (host, local, merged)
        return merged


def setup_protocol_names(protocol_type: Any) -> List[str]:
    """把 registry 的 protocol_type 配置规范成协议名列表。"""
    if not protocol_type:
        return []
    if isinstance(protocol_type, str):
        if "," in protocol_type:
            return [protocol.strip() for protocol in protocol_type.split(",")]
        return [protocol_type]
    return list(protocol_type)


def protocol_model(protocol_name: str) -> Any:
    """按协议名解析 experiments.models 中的参数模型。"""
    model = getattr(protocol_models, protocol_name, None)
    if model is None:
        raise KeyError(f"未知协议类型: {protocol_name}（experiments.models 中不存在）")
    return model


async def expand_resource_value(node: "DeviceNode", value: Any) -> Any:
    """把仅含 id/uuid 的资源占位展开为权威的完整资源 dict。

    单个资源返回 dump 后的首节点 dict；列表返回 dump 全量（与 ROS2
    goal 查询语义一致）。非资源形态原样返回。
    """
    is_sequence = isinstance(value, list)
    probe = value[0] if is_sequence and value else value
    if not isinstance(probe, dict):
        return value
    resource_uuid = probe.get("uuid") or None
    resource_id = probe.get("id") or None
    if not resource_uuid and not resource_id:
        return value
    if resource_uuid:
        tree_set = await node.get_resource([resource_uuid], with_children=True)
    else:
        tree_set = await node.get_resource_by_id(resource_id, with_children=True)
    target = tree_set.dump()
    for tree in target:
        for resource in tree:
            _use_physical_node_id(resource)
    return target if is_sequence else target[0][0]


def _use_physical_node_id(resource: dict) -> None:
    """把权威资源 dict 的 ``id`` 换成物理拓扑图里同一 uuid 的节点名。

    权威的 resource id 是带父级前缀的路径（``Station/flask``），协议编译器却按 ``-g`` 图的节点
    id（``flask``）在物理拓扑图里查容器、找连通的泵阀；不换就报「容器不存在于系统中」。
    """
    graph = physical_setup_graph()
    resource_uuid = resource.get("uuid")
    if graph is None or not resource_uuid:
        return
    for node_id, data in graph.nodes(data=True):
        if data.get("uuid") == resource_uuid:
            resource["id"] = node_id
            return


async def update_protocol_resources(node: "DeviceNode", values: List[Any]) -> None:
    """协议执行后把涉及的资源回写权威（按 tracker 索引，父资源去重）。"""
    seen: set[int] = set()
    unique_resources: List[Any] = []
    for value in values:
        resource_list: List[Any] = []
        items = value if isinstance(value, list) else [value]
        for item in items:
            if isinstance(item, list):
                resource_list.extend(item)
            else:
                resource_list.append(item)
        for res_data in resource_list:
            if not isinstance(res_data, dict):
                continue
            # 按 uuid 找本地实例；没有 uuid 时按 PLR 键（图节点 id）
            if res_data.get("uuid"):
                query = {"uuid": res_data["uuid"]}
            else:
                res_name = res_data.get("id") or (res_data.get("config") or {}).get("name") or res_data.get("name")
                if not res_name:
                    continue
                query = {"name": res_name}
            plr = node.resource_tracker.figure_resource(query, try_mode=False)
            res = node.resource_tracker.parent_resource(plr)
            if res is None:
                res = plr
            if id(res) not in seen:
                seen.add(id(res))
                unique_resources.append(res)
    if unique_resources:
        await node.update_resource(unique_resources)


__all__ = [
    "WorkstationNodeTempError",
    "expand_resource_value",
    "host_physical_setup_graph_revision",
    "local_physical_setup_graph",
    "merge_physical_setup_graphs",
    "physical_setup_graph",
    "protocol_model",
    "set_host_physical_setup_graph",
    "set_physical_setup_graph",
    "setup_protocol_names",
    "update_protocol_resources",
]
