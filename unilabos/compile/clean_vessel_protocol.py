from typing import List, Dict, Any
import networkx as nx
from .utils.vessel_parser import get_vessel, find_solvent_vessel, find_connected_heatchill
from .utils.logger_util import debug_print
from .pump_protocol import generate_pump_protocol
from .utils.resource_helper import get_resource_liquid_volume


def find_solvent_vessel_by_any_match(G: nx.DiGraph, solvent: str) -> str:
    """
    增强版溶剂容器查找，支持各种匹配方式的别名函数
    """
    return find_solvent_vessel(G, solvent)


def find_waste_vessel(G: nx.DiGraph) -> str:
    """
    查找废液容器
    """
    possible_waste_names = [
        "waste_workup",
        "flask_waste",
        "bottle_waste",
        "waste",
        "waste_vessel",
        "waste_container"
    ]

    for waste_name in possible_waste_names:
        if waste_name in G.nodes():
            return waste_name

    raise ValueError(f"未找到废液容器。尝试了以下名称: {possible_waste_names}")


def generate_clean_vessel_protocol(
    G: nx.DiGraph,
    vessel: dict,
    solvent: str,
    volume: float,
    temp: float,
    repeats: int = 1
) -> List[Dict[str, Any]]:
    """
    生成容器清洗操作的协议序列，复用 pump_protocol 的成熟算法

    清洗流程：
    1. 查找溶剂容器和废液容器
    2. 如果需要加热，启动加热设备
    3. 重复以下操作 repeats 次：
       a. 使用 pump_protocol 将溶剂从溶剂容器转移到目标容器
       b. (可选) 等待清洗作用时间
       c. 使用 pump_protocol 将清洗液从目标容器转移到废液容器
    4. 如果加热了，停止加热

    Args:
        G: 有向图，节点为设备和容器，边为流体管道
        vessel: 要清洗的容器字典（包含id字段）
        solvent: 用于清洗的溶剂名称
        volume: 每次清洗使用的溶剂体积
        temp: 清洗时的温度
        repeats: 清洗操作的重复次数，默认为 1

    Returns:
        List[Dict[str, Any]]: 容器清洗操作的动作序列
    """
    vessel_id, vessel_data = get_vessel(vessel)

    action_sequence = []

    debug_print(f"开始生成容器清洗协议: vessel={vessel_id}, solvent={solvent}, volume={volume}mL, temp={temp}°C, repeats={repeats}")

    # 验证目标容器存在
    if vessel_id not in G.nodes():
        raise ValueError(f"目标容器 '{vessel_id}' 不存在于系统中")

    # 查找溶剂容器
    try:
        solvent_vessel = find_solvent_vessel(G, solvent)
        debug_print(f"找到溶剂容器: {solvent_vessel}")
    except ValueError as e:
        raise ValueError(f"无法找到溶剂容器: {str(e)}")

    # 查找废液容器
    try:
        waste_vessel = find_waste_vessel(G)
        debug_print(f"找到废液容器: {waste_vessel}")
    except ValueError as e:
        raise ValueError(f"无法找到废液容器: {str(e)}")

    # 查找加热设备（可选）
    heatchill_id = find_connected_heatchill(G, vessel_id)
    if heatchill_id:
        debug_print(f"找到加热设备: {heatchill_id}")
    else:
        debug_print(f"未找到加热设备，将在室温下清洗")

    # 记录清洗前的容器状态
    original_liquid_volume = 0.0
    if "data" in vessel and "liquid_volume" in vessel["data"]:
        current_volume = vessel["data"]["liquid_volume"]
        if isinstance(current_volume, list) and len(current_volume) > 0:
            original_liquid_volume = current_volume[0]
        elif isinstance(current_volume, (int, float)):
            original_liquid_volume = current_volume

    # 第一步：如果需要加热且有加热设备，启动加热
    if temp > 25.0 and heatchill_id:
        debug_print(f"启动加热至 {temp}°C")
        heatchill_start_action = {
            "device_id": heatchill_id,
            "action_name": "heat_chill_start",
            "action_kwargs": {
                "vessel": {"id": vessel_id},
                "temp": temp,
                "purpose": f"cleaning with {solvent}"
            }
        }
        action_sequence.append(heatchill_start_action)

        wait_action = {
            "action_name": "wait",
            "action_kwargs": {"time": 30}
        }
        action_sequence.append(wait_action)

    # 第二步：重复清洗操作
    for repeat in range(repeats):
        debug_print(f"执行第 {repeat + 1}/{repeats} 次清洗")

        # 2a. 使用 pump_protocol 将溶剂转移到目标容器
        try:
            add_solvent_actions = generate_pump_protocol(
                G=G,
                from_vessel=solvent_vessel,
                to_vessel=vessel_id,
                volume=volume,
                flowrate=2.5,
                transfer_flowrate=2.5
            )
            action_sequence.extend(add_solvent_actions)

            # 更新容器体积（添加清洗溶剂）
            if "data" not in vessel:
                vessel["data"] = {}

            if "liquid_volume" in vessel["data"]:
                current_volume = vessel["data"]["liquid_volume"]
                if isinstance(current_volume, list):
                    if len(current_volume) > 0:
                        vessel["data"]["liquid_volume"][0] += volume
                    else:
                        vessel["data"]["liquid_volume"] = [volume]
                elif isinstance(current_volume, (int, float)):
                    vessel["data"]["liquid_volume"] += volume
                else:
                    vessel["data"]["liquid_volume"] = volume
            else:
                vessel["data"]["liquid_volume"] = volume

            # 同时更新图中的容器数据
            if vessel_id in G.nodes():
                if 'data' not in G.nodes[vessel_id]:
                    G.nodes[vessel_id]['data'] = {}

                vessel_node_data = G.nodes[vessel_id]['data']
                current_node_volume = vessel_node_data.get('liquid_volume', 0.0)

                if isinstance(current_node_volume, list):
                    if len(current_node_volume) > 0:
                        G.nodes[vessel_id]['data']['liquid_volume'][0] += volume
                    else:
                        G.nodes[vessel_id]['data']['liquid_volume'] = [volume]
                else:
                    G.nodes[vessel_id]['data']['liquid_volume'] = current_node_volume + volume

        except Exception as e:
            raise ValueError(f"无法将溶剂转移到容器: {str(e)}")

        # 2b. 等待清洗作用时间
        cleaning_wait_time = 60 if temp > 50.0 else 30
        wait_action = {
            "action_name": "wait",
            "action_kwargs": {"time": cleaning_wait_time}
        }
        action_sequence.append(wait_action)

        # 2c. 使用 pump_protocol 将清洗液转移到废液容器
        try:
            remove_waste_actions = generate_pump_protocol(
                G=G,
                from_vessel=vessel_id,
                to_vessel=waste_vessel,
                volume=volume,
                flowrate=2.5,
                transfer_flowrate=2.5
            )
            action_sequence.extend(remove_waste_actions)

            # 更新容器体积（移除清洗液）
            if "data" in vessel and "liquid_volume" in vessel["data"]:
                current_volume = vessel["data"]["liquid_volume"]
                if isinstance(current_volume, list):
                    if len(current_volume) > 0:
                        vessel["data"]["liquid_volume"][0] = max(0.0, vessel["data"]["liquid_volume"][0] - volume)
                    else:
                        vessel["data"]["liquid_volume"] = [0.0]
                elif isinstance(current_volume, (int, float)):
                    vessel["data"]["liquid_volume"] = max(0.0, current_volume - volume)
                else:
                    vessel["data"]["liquid_volume"] = 0.0

            # 同时更新图中的容器数据
            if vessel_id in G.nodes():
                vessel_node_data = G.nodes[vessel_id].get('data', {})
                current_node_volume = vessel_node_data.get('liquid_volume', 0.0)

                if isinstance(current_node_volume, list):
                    if len(current_node_volume) > 0:
                        G.nodes[vessel_id]['data']['liquid_volume'][0] = max(0.0, current_node_volume[0] - volume)
                    else:
                        G.nodes[vessel_id]['data']['liquid_volume'] = [0.0]
                else:
                    G.nodes[vessel_id]['data']['liquid_volume'] = max(0.0, current_node_volume - volume)

        except Exception as e:
            raise ValueError(f"无法将清洗液转移到废液容器: {str(e)}")

        # 2d. 清洗循环间的短暂等待
        if repeat < repeats - 1:
            wait_action = {
                "action_name": "wait",
                "action_kwargs": {"time": 10}
            }
            action_sequence.append(wait_action)

    # 第三步：如果加热了，停止加热
    if temp > 25.0 and heatchill_id:
        heatchill_stop_action = {
            "device_id": heatchill_id,
            "action_name": "heat_chill_stop",
            "action_kwargs": {
                "vessel": {"id": vessel_id},
            }
        }
        action_sequence.append(heatchill_stop_action)

    # 清洗完成后的状态
    final_liquid_volume = 0.0
    if "data" in vessel and "liquid_volume" in vessel["data"]:
        current_volume = vessel["data"]["liquid_volume"]
        if isinstance(current_volume, list) and len(current_volume) > 0:
            final_liquid_volume = current_volume[0]
        elif isinstance(current_volume, (int, float)):
            final_liquid_volume = current_volume

    debug_print(f"清洗完成: {len(action_sequence)} 个动作, 体积 {original_liquid_volume:.2f} -> {final_liquid_volume:.2f}mL")

    return action_sequence


# 便捷函数
def generate_quick_clean_protocol(
    G: nx.DiGraph,
    vessel: dict,
    solvent: str = "water",
    volume: float = 100.0
) -> List[Dict[str, Any]]:
    """快速清洗：室温，单次清洗"""
    return generate_clean_vessel_protocol(G, vessel, solvent, volume, 25.0, 1)


def generate_thorough_clean_protocol(
    G: nx.DiGraph,
    vessel: dict,
    solvent: str = "water",
    volume: float = 150.0,
    temp: float = 60.0
) -> List[Dict[str, Any]]:
    """深度清洗：加热，多次清洗"""
    return generate_clean_vessel_protocol(G, vessel, solvent, volume, temp, 3)


def generate_organic_clean_protocol(
    G: nx.DiGraph,
    vessel: dict,
    volume: float = 100.0
) -> List[Dict[str, Any]]:
    """有机清洗：先用有机溶剂，再用水清洗"""
    action_sequence = []

    # 第一步：有机溶剂清洗
    try:
        organic_actions = generate_clean_vessel_protocol(
            G, vessel, "acetone", volume, 25.0, 2
        )
        action_sequence.extend(organic_actions)
    except ValueError:
        try:
            organic_actions = generate_clean_vessel_protocol(
                G, vessel, "ethanol", volume, 25.0, 2
            )
            action_sequence.extend(organic_actions)
        except ValueError:
            debug_print("未找到有机溶剂，跳过有机清洗步骤")

    # 第二步：水清洗
    water_actions = generate_clean_vessel_protocol(
        G, vessel, "water", volume, 25.0, 2
    )
    action_sequence.extend(water_actions)

    return action_sequence


def get_vessel_liquid_types(G: nx.DiGraph, vessel: str) -> List[str]:
    """获取容器中所有液体的类型"""
    if vessel not in G.nodes():
        return []

    vessel_data = G.nodes[vessel].get('data', {})
    liquids = vessel_data.get('liquid', [])

    liquid_types = []
    for liquid in liquids:
        if isinstance(liquid, dict):
            liquid_type = liquid.get('liquid_type') or liquid.get('name', '')
            if liquid_type:
                liquid_types.append(liquid_type)

    return liquid_types


def find_vessel_by_content(G: nx.DiGraph, content: str) -> List[str]:
    """
    根据内容物查找所有匹配的容器
    """
    matching_vessels = []

    for node_id in G.nodes():
        if G.nodes[node_id].get('type') == 'container':
            node_name = G.nodes[node_id].get('name', '').lower()
            if content.lower() in node_id.lower() or content.lower() in node_name:
                matching_vessels.append(node_id)
                continue

            vessel_data = G.nodes[node_id].get('data', {})
            liquids = vessel_data.get('liquid', [])
            config_data = G.nodes[node_id].get('config', {})

            reagent_name = vessel_data.get('reagent_name', '').lower()
            config_reagent = config_data.get('reagent', '').lower()

            if (content.lower() == reagent_name or
                content.lower() == config_reagent):
                matching_vessels.append(node_id)
                continue

            for liquid in liquids:
                if isinstance(liquid, dict):
                    liquid_type = liquid.get('liquid_type') or liquid.get('name', '')
                    if liquid_type.lower() == content.lower():
                        matching_vessels.append(node_id)
                        break

    return matching_vessels
