from typing import List, Dict, Any, Optional, Union
import networkx as nx
import logging
import re
from .utils.vessel_parser import get_vessel
from .utils.unit_parser import parse_time_input
from .utils.logger_util import debug_print

logger = logging.getLogger(__name__)


def find_rotavap_device(G: nx.DiGraph, vessel: str = None) -> Optional[str]:
    """
    在组态图中查找旋转蒸发仪设备

    Args:
        G: 设备图
        vessel: 指定的设备名称（可选）

    Returns:
        str: 找到的旋转蒸发仪设备ID，如果没找到返回None
    """
    # 如果指定了vessel，先检查是否存在且是旋转蒸发仪
    if vessel:
        if vessel in G.nodes():
            node_data = G.nodes[vessel]
            node_class = node_data.get('class', '')
            node_type = node_data.get('type', '')

            if any(keyword in str(node_class).lower() for keyword in ['rotavap', 'rotary', 'evaporat']):
                debug_print(f"找到指定的旋转蒸发仪: {vessel}")
                return vessel
            elif node_type == 'device':
                debug_print(f"指定设备存在，尝试直接使用: {vessel}")
                return vessel

    # 在所有设备中查找旋转蒸发仪
    rotavap_candidates = []

    for node_id, node_data in G.nodes(data=True):
        node_class = node_data.get('class', '')
        node_type = node_data.get('type', '')

        if node_type != 'device':
            continue

        if any(keyword in str(node_class).lower() for keyword in ['rotavap', 'rotary', 'evaporat']):
            rotavap_candidates.append(node_id)
        elif any(keyword in str(node_id).lower() for keyword in ['rotavap', 'rotary', 'evaporat']):
            rotavap_candidates.append(node_id)

    if rotavap_candidates:
        selected = rotavap_candidates[0]
        debug_print(f"选择旋转蒸发仪: {selected}")
        return selected

    debug_print("未找到旋转蒸发仪设备")
    return None

def find_connected_vessel(G: nx.DiGraph, rotavap_device: str) -> Optional[str]:
    """
    查找与旋转蒸发仪连接的容器
    """
    rotavap_data = G.nodes[rotavap_device]
    children = rotavap_data.get('children', [])

    for child_id in children:
        if child_id in G.nodes():
            child_data = G.nodes[child_id]
            child_type = child_data.get('type', '')

            if child_type == 'container':
                debug_print(f"找到连接的容器: {child_id}")
                return child_id

    for neighbor in G.neighbors(rotavap_device):
        neighbor_data = G.nodes[neighbor]
        neighbor_type = neighbor_data.get('type', '')

        if neighbor_type == 'container':
            debug_print(f"找到邻接的容器: {neighbor}")
            return neighbor

    debug_print("未找到连接的容器")
    return None

def generate_evaporate_protocol(
    G: nx.DiGraph,
    vessel: dict,
    pressure: float = 0.1,
    temp: float = 60.0,
    time: Union[str, float] = "180",
    stir_speed: float = 100.0,
    solvent: str = "",
    **kwargs
) -> List[Dict[str, Any]]:
    """
    生成蒸发操作的协议序列 - 支持单位和体积运算

    Args:
        G: 设备图
        vessel: 容器字典（从XDL传入）
        pressure: 真空度 (bar)，默认0.1
        temp: 加热温度 (°C)，默认60
        time: 蒸发时间（支持 "3 min", "180", "0.5 h" 等）
        stir_speed: 旋转速度 (RPM)，默认100
        solvent: 溶剂名称（用于参数优化）
        **kwargs: 其他参数（兼容性）

    Returns:
        List[Dict[str, Any]]: 动作序列
    """

    vessel_id, vessel_data = get_vessel(vessel)

    debug_print(f"开始生成蒸发协议: vessel={vessel_id}, pressure={pressure}, temp={temp}, time={time}")

    # 记录蒸发前的容器状态
    original_liquid_volume = 0.0
    if "data" in vessel and "liquid_volume" in vessel["data"]:
        current_volume = vessel["data"]["liquid_volume"]
        if isinstance(current_volume, list) and len(current_volume) > 0:
            original_liquid_volume = current_volume[0]
        elif isinstance(current_volume, (int, float)):
            original_liquid_volume = current_volume

    # 查找旋转蒸发仪设备
    if not vessel_id:
        raise ValueError("vessel 参数不能为空")

    rotavap_device = find_rotavap_device(G, vessel_id)
    if not rotavap_device:
        raise ValueError(f"未找到旋转蒸发仪设备。请检查组态图中是否包含 class 包含 'rotavap'、'rotary' 或 'evaporat' 的设备")

    # 确定目标容器
    target_vessel = vessel_id

    if vessel_id == rotavap_device:
        connected_vessel = find_connected_vessel(G, rotavap_device)
        if connected_vessel:
            target_vessel = connected_vessel
        else:
            target_vessel = rotavap_device
    elif vessel_id in G.nodes() and G.nodes[vessel_id].get('type') == 'container':
        target_vessel = vessel_id
    else:
        target_vessel = rotavap_device

    # 单位解析处理
    final_time = parse_time_input(time)
    debug_print(f"时间解析: {time} -> {final_time}s ({final_time/60:.1f}分钟)")

    # 参数验证和修正
    if pressure <= 0 or pressure > 1.0:
        pressure = 0.1

    if temp < 10.0 or temp > 200.0:
        temp = 60.0

    if final_time <= 0:
        final_time = 180.0

    if stir_speed < 10.0 or stir_speed > 300.0:
        stir_speed = 100.0

    # 根据溶剂优化参数
    if solvent:
        solvent_lower = solvent.lower()

        if any(s in solvent_lower for s in ['water', 'aqueous', 'h2o']):
            temp = max(temp, 80.0)
            pressure = max(pressure, 0.2)
        elif any(s in solvent_lower for s in ['ethanol', 'methanol', 'acetone']):
            temp = min(temp, 50.0)
            pressure = min(pressure, 0.05)
        elif any(s in solvent_lower for s in ['dmso', 'dmi', 'toluene']):
            temp = max(temp, 100.0)
            pressure = min(pressure, 0.01)

    debug_print(f"最终参数: pressure={pressure}bar, temp={temp}°C, time={final_time}s, stir_speed={stir_speed}RPM")

    # 蒸发体积计算
    evaporation_volume = 0.0
    if original_liquid_volume > 0:
        base_evap_rate = 0.5
        temp_factor = 1.0 + (temp - 25.0) / 100.0
        vacuum_factor = 1.0 + (1.0 - pressure) * 2.0

        solvent_factor = 1.0
        if solvent:
            solvent_lower = solvent.lower()
            if any(s in solvent_lower for s in ['water', 'h2o']):
                solvent_factor = 0.8
            elif any(s in solvent_lower for s in ['ethanol', 'methanol', 'acetone']):
                solvent_factor = 1.5
            elif any(s in solvent_lower for s in ['dmso', 'dmi']):
                solvent_factor = 0.3

        total_evap_rate = base_evap_rate * temp_factor * vacuum_factor * solvent_factor
        evaporation_volume = min(
            original_liquid_volume * 0.95,
            total_evap_rate * (final_time / 60.0)
        )

        debug_print(f"预计蒸发量: {evaporation_volume:.2f}mL ({evaporation_volume/original_liquid_volume*100:.1f}%)")

    # 生成动作序列
    action_sequence = []

    # 1. 等待稳定
    action_sequence.append({
        "action_name": "wait",
        "action_kwargs": {"time": 10}
    })

    # 2. 执行蒸发
    evaporate_action = {
        "device_id": rotavap_device,
        "action_name": "evaporate",
        "action_kwargs": {
            "vessel": {"id": target_vessel},
            "pressure": float(pressure),
            "temp": float(temp),
            "time": float(final_time),
            "stir_speed": float(stir_speed),
            "solvent": str(solvent)
        }
    }
    action_sequence.append(evaporate_action)

    # 蒸发过程中的体积变化
    if evaporation_volume > 0:
        new_volume = max(0.0, original_liquid_volume - evaporation_volume)

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

        debug_print(f"蒸发体积变化: {original_liquid_volume:.2f}mL -> {new_volume:.2f}mL (-{evaporation_volume:.2f}mL)")

    # 3. 蒸发后等待
    action_sequence.append({
        "action_name": "wait",
        "action_kwargs": {"time": 10}
    })

    # 最终状态
    final_liquid_volume = 0.0
    if "data" in vessel and "liquid_volume" in vessel["data"]:
        current_volume = vessel["data"]["liquid_volume"]
        if isinstance(current_volume, list) and len(current_volume) > 0:
            final_liquid_volume = current_volume[0]
        elif isinstance(current_volume, (int, float)):
            final_liquid_volume = current_volume

    debug_print(f"蒸发协议生成完成: {len(action_sequence)} 个动作, 设备={rotavap_device}, 容器={target_vessel}")

    return action_sequence
