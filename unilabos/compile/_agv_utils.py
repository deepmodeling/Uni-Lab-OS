"""
AGV 编译器共用工具函数

从 physical_setup_graph 中发现 AGV 节点配置，
供 agv_transfer_protocol 和 batch_transfer_protocol 复用。
"""

from typing import Any, Dict, List, Optional

import networkx as nx


def find_agv_config(G: nx.Graph, agv_id: Optional[str] = None) -> Dict[str, Any]:
    """从设备图中发现 AGV 节点，返回其配置

    查找策略：
    1. 如果指定 agv_id，直接读取该节点
    2. 否则查找 class 为 "agv_transport_station" 的节点
    3. 兜底查找 config 中包含 device_roles 的 workstation 节点

    Returns:
        {
            "agv_id": str,
            "device_roles": {"navigator": "...", "arm": "..."},
            "route_table": {"A->B": {"nav_command": ..., "arm_pick": ..., "arm_place": ...}},
            "capacity": int,
        }
    """
    if agv_id and agv_id in G.nodes:
        node_data = G.nodes[agv_id]
        config = _extract_config(node_data)
        if config and "device_roles" in config:
            return _build_agv_cfg(agv_id, config, G)

    # 查找 agv_transport_station 类型
    for nid, ndata in G.nodes(data=True):
        node_class = _get_node_class(ndata)
        if node_class == "agv_transport_station":
            config = _extract_config(ndata)
            return _build_agv_cfg(nid, config or {}, G)

    # 兜底：查找带有 device_roles 的 workstation
    for nid, ndata in G.nodes(data=True):
        node_class = _get_node_class(ndata)
        if node_class == "workstation":
            config = _extract_config(ndata)
            if config and "device_roles" in config:
                return _build_agv_cfg(nid, config, G)

    raise ValueError("设备图中未找到 AGV 节点（需 class=agv_transport_station 或 config.device_roles）")


def get_agv_capacity(G: nx.Graph, agv_id: str) -> int:
    """从 AGV 的 Warehouse 子节点计算载具容量"""
    for neighbor in G.successors(agv_id) if G.is_directed() else G.neighbors(agv_id):
        ndata = G.nodes[neighbor]
        node_type = _get_node_type(ndata)
        if node_type == "warehouse":
            config = _extract_config(ndata)
            if config:
                x = config.get("num_items_x", 1)
                y = config.get("num_items_y", 1)
                z = config.get("num_items_z", 1)
                return x * y * z
    # 如果没有 warehouse 子节点，尝试从配置中读取
    return 0


def split_batches(items: list, capacity: int) -> List[list]:
    """按 AGV 容量分批

    Args:
        items: 待转运的物料列表
        capacity: AGV 单批次容量

    Returns:
        分批后的列表的列表
    """
    if capacity <= 0:
        raise ValueError(f"AGV 容量必须 > 0，当前: {capacity}")
    return [items[i:i + capacity] for i in range(0, len(items), capacity)]


def _extract_config(node_data: dict) -> Optional[dict]:
    """从节点数据中提取 config 字段，兼容多种格式"""
    # 直接 config 字段
    config = node_data.get("config")
    if isinstance(config, dict):
        return config
    # res_content 嵌套格式
    res_content = node_data.get("res_content")
    if hasattr(res_content, "config"):
        return res_content.config if isinstance(res_content.config, dict) else None
    if isinstance(res_content, dict):
        return res_content.get("config")
    return None


def _get_node_class(node_data: dict) -> str:
    """获取节点的 class 字段"""
    res_content = node_data.get("res_content")
    if hasattr(res_content, "model_dump"):
        d = res_content.model_dump()
        return d.get("class_", d.get("class", ""))
    if isinstance(res_content, dict):
        return res_content.get("class_", res_content.get("class", ""))
    return node_data.get("class_", node_data.get("class", ""))


def _get_node_type(node_data: dict) -> str:
    """获取节点的 type 字段"""
    res_content = node_data.get("res_content")
    if hasattr(res_content, "type"):
        return res_content.type or ""
    if isinstance(res_content, dict):
        return res_content.get("type", "")
    return node_data.get("type", "")


def _build_agv_cfg(agv_id: str, config: dict, G: nx.Graph) -> Dict[str, Any]:
    """构建标准化的 AGV 配置"""
    return {
        "agv_id": agv_id,
        "device_roles": config.get("device_roles", {}),
        "route_table": config.get("route_table", {}),
        "capacity": get_agv_capacity(G, agv_id),
    }
