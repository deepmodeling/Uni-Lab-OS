"""
批量物料转运编译器

将 BatchTransferProtocol 编译为多批次的 nav → pick × N → nav → place × N 动作序列。
自动按 AGV 容量分批，全程维护三方 children dict 的物料系统一致性。
"""

import copy
from typing import Any, Dict, List

import networkx as nx

from unilabos.compile._agv_utils import find_agv_config, split_batches


def generate_batch_transfer_protocol(
    G: nx.Graph,
    from_repo: dict,
    to_repo: dict,
    transfer_resources: list,
    from_positions: list,
    to_positions: list,
) -> List[Dict[str, Any]]:
    """编译批量转运协议为可执行的 action steps

    Args:
        G: 设备图 (physical_setup_graph)
        from_repo: 来源工站资源 dict（{station_id: {..., children: {...}}}）
        to_repo: 目标工站资源 dict（含堆栈和位置信息）
        transfer_resources: 被转运的物料列表（Resource dict）
        from_positions: 来源 slot 位置列表（与 transfer_resources 平行）
        to_positions: 目标 slot 位置列表（与 transfer_resources 平行）

    Returns:
        action steps 列表，ROS2WorkstationNode 按序执行
    """
    if not transfer_resources:
        return []

    n = len(transfer_resources)
    if len(from_positions) != n or len(to_positions) != n:
        raise ValueError(
            f"transfer_resources({n}), from_positions({len(from_positions)}), "
            f"to_positions({len(to_positions)}) 长度不一致"
        )

    # 组合为内部 transfer_items 便于分批处理
    transfer_items = []
    for i in range(n):
        res = transfer_resources[i] if isinstance(transfer_resources[i], dict) else {}
        transfer_items.append({
            "resource_id": res.get("id", res.get("name", "")),
            "resource_uuid": res.get("sample_id", ""),
            "from_position": from_positions[i],
            "to_position": to_positions[i],
            "resource": res,
        })

    # 查询 AGV 配置
    agv_cfg = find_agv_config(G)
    agv_id = agv_cfg["agv_id"]
    device_roles = agv_cfg["device_roles"]
    route_table = agv_cfg["route_table"]
    capacity = agv_cfg["capacity"]

    if capacity <= 0:
        raise ValueError(f"AGV {agv_id} 容量为 0，请检查 Warehouse 子节点配置")

    nav_device = device_roles.get("navigator", device_roles.get("nav"))
    arm_device = device_roles.get("arm")
    if not nav_device or not arm_device:
        raise ValueError(f"AGV {agv_id} device_roles 缺少 navigator 或 arm: {device_roles}")

    from_repo_ = list(from_repo.values())[0]
    to_repo_ = list(to_repo.values())[0]
    from_station_id = from_repo_["id"]
    to_station_id = to_repo_["id"]

    # 查找路由
    route_to_source = _find_route(route_table, agv_id, from_station_id)
    route_to_target = _find_route(route_table, from_station_id, to_station_id)

    # 构建 AGV carrier 的 children dict（用于 compile 阶段状态追踪）
    agv_carrier_children: Dict[str, Any] = {}

    # 计算 slot 名称（A01, A02, B01, ...）
    agv_slot_names = _get_agv_slot_names(G, agv_cfg)

    # 分批
    batches = split_batches(transfer_items, capacity)

    steps: List[Dict[str, Any]] = []

    for batch_idx, batch in enumerate(batches):
        is_last_batch = (batch_idx == len(batches) - 1)

        # 阶段 1: AGV 导航到来源工站
        steps.append({
            "device_id": nav_device,
            "action_name": "send_nav_task",
            "action_kwargs": {
                "command": route_to_source.get("nav_command", "")
            },
            "_comment": f"批次{batch_idx + 1}/{len(batches)}: AGV 导航至来源 {from_station_id}"
        })

        # 阶段 2: 逐个 pick
        for item_idx, item in enumerate(batch):
            from_pos = item["from_position"]
            slot = agv_slot_names[item_idx] if item_idx < len(agv_slot_names) else f"S{item_idx + 1}"

            # compile 阶段更新 children dict
            if from_pos in from_repo_.get("children", {}):
                resource_data = from_repo_["children"].pop(from_pos)
                resource_data["parent"] = agv_id
                agv_carrier_children[slot] = resource_data

            steps.append({
                "device_id": arm_device,
                "action_name": "move_pos_task",
                "action_kwargs": {
                    "command": route_to_source.get("arm_pick", route_to_source.get("arm_command", ""))
                },
                "_transfer_meta": {
                    "phase": "pick",
                    "resource_uuid": item.get("resource_uuid", ""),
                    "resource_id": item.get("resource_id", ""),
                    "from_parent": from_station_id,
                    "from_position": from_pos,
                    "agv_slot": slot,
                },
                "_comment": f"Pick {item.get('resource_id', from_pos)} → AGV.{slot}"
            })

        # 阶段 3: AGV 导航到目标工站
        steps.append({
            "device_id": nav_device,
            "action_name": "send_nav_task",
            "action_kwargs": {
                "command": route_to_target.get("nav_command", "")
            },
            "_comment": f"批次{batch_idx + 1}: AGV 导航至目标 {to_station_id}"
        })

        # 阶段 4: 逐个 place
        for item_idx, item in enumerate(batch):
            to_pos = item["to_position"]
            slot = agv_slot_names[item_idx] if item_idx < len(agv_slot_names) else f"S{item_idx + 1}"

            # compile 阶段更新 children dict
            if slot in agv_carrier_children:
                resource_data = agv_carrier_children.pop(slot)
                resource_data["parent"] = to_repo_["id"]
                to_repo_["children"][to_pos] = resource_data

            steps.append({
                "device_id": arm_device,
                "action_name": "move_pos_task",
                "action_kwargs": {
                    "command": route_to_target.get("arm_place", route_to_target.get("arm_command", ""))
                },
                "_transfer_meta": {
                    "phase": "place",
                    "resource_uuid": item.get("resource_uuid", ""),
                    "resource_id": item.get("resource_id", ""),
                    "to_parent": to_station_id,
                    "to_position": to_pos,
                    "agv_slot": slot,
                },
                "_comment": f"Place AGV.{slot} → {to_station_id}.{to_pos}"
            })

        # 如果还有下一批，AGV 需要返回来源取料
        if not is_last_batch:
            steps.append({
                "device_id": nav_device,
                "action_name": "send_nav_task",
                "action_kwargs": {
                    "command": route_to_source.get("nav_command", "")
                },
                "_comment": f"AGV 返回来源 {from_station_id} 取下一批"
            })

    return steps


def _find_route(route_table: Dict[str, Any], from_id: str, to_id: str) -> Dict[str, str]:
    """在路由表中查找路线，支持 A->B 和 (A, B) 两种 key 格式"""
    # 优先 "A->B" 格式
    key = f"{from_id}->{to_id}"
    if key in route_table:
        return route_table[key]
    # 兼容 tuple key（JSON 中以逗号分隔字符串表示）
    tuple_key = f"({from_id}, {to_id})"
    if tuple_key in route_table:
        return route_table[tuple_key]
    raise KeyError(f"路由表中未找到: {key}，可用路线: {list(route_table.keys())}")


def _get_agv_slot_names(G: nx.Graph, agv_cfg: dict) -> List[str]:
    """从设备图中获取 AGV Warehouse 的 slot 名称列表"""
    agv_id = agv_cfg["agv_id"]
    neighbors = G.successors(agv_id) if G.is_directed() else G.neighbors(agv_id)
    for neighbor in neighbors:
        ndata = G.nodes[neighbor]
        node_type = ndata.get("type", "")
        res_content = ndata.get("res_content")
        if hasattr(res_content, "type"):
            node_type = res_content.type or node_type
        elif isinstance(res_content, dict):
            node_type = res_content.get("type", node_type)
        if node_type == "warehouse":
            config = ndata.get("config", {})
            if hasattr(res_content, "config") and isinstance(res_content.config, dict):
                config = res_content.config
            elif isinstance(res_content, dict):
                config = res_content.get("config", config)
            num_x = config.get("num_items_x", 1)
            num_y = config.get("num_items_y", 1)
            num_z = config.get("num_items_z", 1)
            # 与 warehouse_factory 一致的命名
            letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
            len_x = num_x if num_z == 1 else (num_y if num_x == 1 else num_x)
            len_y = num_y if num_z == 1 else (num_z if num_x == 1 else num_z)
            return [f"{letters[j]}{i + 1:02d}" for i in range(len_x) for j in range(len_y)]
    # 兜底生成通用名称
    capacity = agv_cfg.get("capacity", 4)
    return [f"S{i + 1}" for i in range(capacity)]
