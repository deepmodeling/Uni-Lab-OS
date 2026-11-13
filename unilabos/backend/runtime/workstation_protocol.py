"""workstation XDL protocol 编排的 backend 无关共享逻辑。

ROS2 :class:`unilabos.backend.ros2.presets.workstation.ROS2WorkstationNode` 与
HostLink :class:`unilabos.backend.hostlink.workstation.WorkstationNode`
共用本模块：协议名/参数模型解析、资源占位展开与协议结束后的资源回写
（均走 DeviceNode 权威 API：get_resource / update_resource）。

protocol 步骤生成（:mod:`unilabos.experiments.compile`）与参数模型
（:mod:`unilabos.experiments.models`）本身即纯 Python，双 backend 同一份。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, List

from unilabos.experiments import models as protocol_models

if TYPE_CHECKING:
    from unilabos.backend.runtime.node import DeviceNode


class WorkstationNodeTempError(Exception):
    """协议步骤内的日志占位（log_message），不代表失败。"""


#: 协议编译器读取的物理拓扑图（``networkx.Graph``，节点为设备 / 物料，边带端口）。
#: Host 启动装配 ``-g`` 图时写入（``unilabos.app.main._load_startup_graph``）。
_physical_setup_graph: Any = None


def set_physical_setup_graph(graph: Any) -> None:
    """登记本进程的物理拓扑图（Host 启动装配设备图后调用）。"""
    global _physical_setup_graph
    _physical_setup_graph = graph


def physical_setup_graph() -> Any:
    """协议编译器 ``generator(G=...)`` 使用的物理拓扑图；未装配设备图时为 None。"""
    return _physical_setup_graph


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
    return target if is_sequence else target[0][0]


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
            res_name = res_data.get("id") or res_data.get("name")
            if not res_name:
                continue
            plr = node.resource_tracker.figure_resource({"name": res_name}, try_mode=False)
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
    "protocol_model",
    "setup_protocol_names",
    "update_protocol_resources",
]
