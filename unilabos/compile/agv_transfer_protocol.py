"""
AGV 单物料转运编译器

从 physical_setup_graph 中查询 AGV 配置（device_roles, route_table），
不再硬编码 device_id 和路由表。
"""

import networkx as nx
from unilabos.compile._agv_utils import find_agv_config


def generate_agv_transfer_protocol(
    G: nx.Graph,
    from_repo: dict,
    from_repo_position: str,
    to_repo: dict = {},
    to_repo_position: str = ""
):
    from_repo_ = list(from_repo.values())[0]
    to_repo_ = list(to_repo.values())[0]
    resource_to_move = from_repo_["children"].pop(from_repo_position)
    resource_to_move["parent"] = to_repo_["id"]
    to_repo_["children"][to_repo_position] = resource_to_move

    from_repo_id = from_repo_["id"]
    to_repo_id = to_repo_["id"]

    # 从 G 中查询 AGV 配置
    agv_cfg = find_agv_config(G)
    device_roles = agv_cfg["device_roles"]
    route_table = agv_cfg["route_table"]

    route_key = f"{from_repo_id}->{to_repo_id}"
    if route_key not in route_table:
        raise KeyError(f"AGV 路由表中未找到路线: {route_key}，可用路线: {list(route_table.keys())}")

    route = route_table[route_key]
    nav_device = device_roles.get("navigator", device_roles.get("nav"))
    arm_device = device_roles.get("arm")

    return [
        {
            "device_id": nav_device,
            "action_name": "send_nav_task",
            "action_kwargs": {
                "command": route["nav_command"]
            }
        },
        {
            "device_id": arm_device,
            "action_name": "move_pos_task",
            "action_kwargs": {
                "command": route.get("arm_command", route.get("arm_place", ""))
            }
        }
    ]
