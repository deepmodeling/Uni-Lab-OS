import networkx as nx
from typing import List, Dict, Any

from .utils.vessel_parser import get_vessel, find_connected_heatchill
from .utils.logger_util import debug_print


def generate_dry_protocol(
    G: nx.DiGraph,
    vessel: dict,
    compound: str = "",
    **kwargs
) -> List[Dict[str, Any]]:
    """
    生成干燥协议序列

    Args:
        G: 有向图，节点为容器和设备
        vessel: 目标容器字典（从XDL传入）
        compound: 化合物名称（从XDL传入，可选）
        **kwargs: 其他可选参数，但不使用

    Returns:
        List[Dict[str, Any]]: 动作序列
    """
    vessel_id, vessel_data = get_vessel(vessel)

    action_sequence = []

    # 默认参数
    dry_temp = 60.0
    dry_time = 3600.0
    simulation_time = 60.0

    debug_print(f"开始生成干燥协议: vessel={vessel_id}, compound={compound or '未指定'}, temp={dry_temp}°C")

    # 记录干燥前的容器状态
    original_liquid_volume = 0.0
    if "data" in vessel and "liquid_volume" in vessel["data"]:
        current_volume = vessel["data"]["liquid_volume"]
        if isinstance(current_volume, list) and len(current_volume) > 0:
            original_liquid_volume = current_volume[0]
        elif isinstance(current_volume, (int, float)):
            original_liquid_volume = current_volume

    # 1. 验证目标容器存在
    if vessel_id not in G.nodes():
        debug_print(f"容器 '{vessel_id}' 不存在于系统中，跳过干燥")
        return action_sequence

    # 2. 查找相连的加热器
    heater_id = find_connected_heatchill(G, vessel_id)

    if heater_id is None:
        debug_print(f"未找到与容器 '{vessel_id}' 相连的加热器，添加模拟干燥动作")
        action_sequence.append({
            "action_name": "wait",
            "action_kwargs": {
                "time": 10.0,
                "description": f"模拟干燥 {compound or '化合物'} (无加热器可用)"
            }
        })

        # 模拟干燥的体积变化
        if original_liquid_volume > 0:
            volume_loss = original_liquid_volume * 0.1
            new_volume = max(0.0, original_liquid_volume - volume_loss)

            if "data" in vessel and "liquid_volume" in vessel["data"]:
                current_volume = vessel["data"]["liquid_volume"]
                if isinstance(current_volume, list):
                    if len(current_volume) > 0:
                        vessel["data"]["liquid_volume"][0] = new_volume
                    else:
                        vessel["data"]["liquid_volume"] = [new_volume]
                elif isinstance(current_volume, (int, float)):
                    vessel["data"]["liquid_volume"] = new_volume
                else:
                    vessel["data"]["liquid_volume"] = new_volume

            if vessel_id in G.nodes():
                if 'data' not in G.nodes[vessel_id]:
                    G.nodes[vessel_id]['data'] = {}

                vessel_node_data = G.nodes[vessel_id]['data']
                current_node_volume = vessel_node_data.get('liquid_volume', 0.0)

                if isinstance(current_node_volume, list):
                    if len(current_node_volume) > 0:
                        G.nodes[vessel_id]['data']['liquid_volume'][0] = new_volume
                    else:
                        G.nodes[vessel_id]['data']['liquid_volume'] = [new_volume]
                else:
                    G.nodes[vessel_id]['data']['liquid_volume'] = new_volume

            debug_print(f"模拟干燥体积变化: {original_liquid_volume:.2f}mL -> {new_volume:.2f}mL")

        debug_print(f"协议生成完成，共 {len(action_sequence)} 个动作")
        return action_sequence

    debug_print(f"找到加热器: {heater_id}")

    # 3. 启动加热器进行干燥
    # 3.1 启动加热
    action_sequence.append({
        "device_id": heater_id,
        "action_name": "heat_chill_start",
        "action_kwargs": {
            "vessel": {"id": vessel_id},
            "temp": dry_temp,
            "purpose": f"干燥 {compound or '化合物'}"
        }
    })

    # 3.2 等待温度稳定
    action_sequence.append({
        "action_name": "wait",
        "action_kwargs": {
            "time": 10.0,
            "description": f"等待温度稳定到 {dry_temp}°C"
        }
    })

    # 3.3 保持干燥温度
    action_sequence.append({
        "device_id": heater_id,
        "action_name": "heat_chill",
        "action_kwargs": {
            "vessel": {"id": vessel_id},
            "temp": dry_temp,
            "time": simulation_time,
            "purpose": f"干燥 {compound or '化合物'}，保持温度 {dry_temp}°C"
        }
    })

    # 干燥过程中的体积变化计算
    if original_liquid_volume > 0:
        evaporation_rate = 0.001 * dry_temp
        total_evaporation = min(original_liquid_volume * 0.8,
                               evaporation_rate * simulation_time)

        new_volume = max(0.0, original_liquid_volume - total_evaporation)

        if "data" in vessel and "liquid_volume" in vessel["data"]:
            current_volume = vessel["data"]["liquid_volume"]
            if isinstance(current_volume, list):
                if len(current_volume) > 0:
                    vessel["data"]["liquid_volume"][0] = new_volume
                else:
                    vessel["data"]["liquid_volume"] = [new_volume]
            elif isinstance(current_volume, (int, float)):
                vessel["data"]["liquid_volume"] = new_volume
            else:
                vessel["data"]["liquid_volume"] = new_volume

        if vessel_id in G.nodes():
            if 'data' not in G.nodes[vessel_id]:
                G.nodes[vessel_id]['data'] = {}

            vessel_node_data = G.nodes[vessel_id]['data']
            current_node_volume = vessel_node_data.get('liquid_volume', 0.0)

            if isinstance(current_node_volume, list):
                if len(current_node_volume) > 0:
                    G.nodes[vessel_id]['data']['liquid_volume'][0] = new_volume
                else:
                    G.nodes[vessel_id]['data']['liquid_volume'] = [new_volume]
            else:
                G.nodes[vessel_id]['data']['liquid_volume'] = new_volume

        debug_print(f"干燥体积变化: {original_liquid_volume:.2f}mL -> {new_volume:.2f}mL (-{total_evaporation:.2f}mL)")

    # 3.4 停止加热
    action_sequence.append({
        "device_id": heater_id,
        "action_name": "heat_chill_stop",
        "action_kwargs": {
            "vessel": {"id": vessel_id},
            "purpose": f"干燥完成，停止加热"
        }
    })

    # 3.5 等待冷却
    action_sequence.append({
        "action_name": "wait",
        "action_kwargs": {
            "time": 10.0,
            "description": f"等待 {compound or '化合物'} 冷却"
        }
    })

    # 最终状态
    final_liquid_volume = 0.0
    if "data" in vessel and "liquid_volume" in vessel["data"]:
        current_volume = vessel["data"]["liquid_volume"]
        if isinstance(current_volume, list) and len(current_volume) > 0:
            final_liquid_volume = current_volume[0]
        elif isinstance(current_volume, (int, float)):
            final_liquid_volume = current_volume

    debug_print(f"干燥协议生成完成: {len(action_sequence)} 个动作, 体积 {original_liquid_volume:.2f} -> {final_liquid_volume:.2f}mL")

    return action_sequence


# 便捷函数
def generate_quick_dry_protocol(G: nx.DiGraph, vessel: dict, compound: str = "",
                               temp: float = 40.0, time: float = 30.0) -> List[Dict[str, Any]]:
    """快速干燥：低温短时间"""
    return generate_dry_protocol(G, vessel, compound)


def generate_thorough_dry_protocol(G: nx.DiGraph, vessel: dict, compound: str = "",
                                  temp: float = 80.0, time: float = 120.0) -> List[Dict[str, Any]]:
    """深度干燥：高温长时间"""
    return generate_dry_protocol(G, vessel, compound)


def generate_gentle_dry_protocol(G: nx.DiGraph, vessel: dict, compound: str = "",
                                temp: float = 30.0, time: float = 180.0) -> List[Dict[str, Any]]:
    """温和干燥：低温长时间"""
    return generate_dry_protocol(G, vessel, compound)


# 测试函数
def test_dry_protocol():
    """测试干燥协议"""
    debug_print("=== DRY PROTOCOL 测试 ===")
    debug_print("测试完成")


if __name__ == "__main__":
    test_dry_protocol()
