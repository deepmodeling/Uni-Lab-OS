import networkx as nx
import re
import logging
from typing import List, Dict, Any, Tuple, Union
from .utils.vessel_parser import get_vessel, find_solvent_vessel
from .utils.unit_parser import parse_volume_input
from .utils.logger_util import debug_print
from .pump_protocol import generate_pump_protocol_with_rinsing

logger = logging.getLogger(__name__)


def parse_ratio(ratio_str: str) -> Tuple[float, float]:
    """
    解析比例字符串，支持多种格式

    Args:
        ratio_str: 比例字符串（如 "1:1", "3:7", "50:50"）

    Returns:
        Tuple[float, float]: 比例元组 (ratio1, ratio2)
    """
    try:
        if ":" in ratio_str:
            parts = ratio_str.split(":")
            if len(parts) == 2:
                ratio1 = float(parts[0])
                ratio2 = float(parts[1])
                return ratio1, ratio2

        if "-" in ratio_str:
            parts = ratio_str.split("-")
            if len(parts) == 2:
                ratio1 = float(parts[0])
                ratio2 = float(parts[1])
                return ratio1, ratio2

        if "," in ratio_str:
            parts = ratio_str.split(",")
            if len(parts) == 2:
                ratio1 = float(parts[0])
                ratio2 = float(parts[1])
                return ratio1, ratio2

        debug_print(f"无法解析比例 '{ratio_str}'，使用默认比例 1:1")
        return 1.0, 1.0

    except ValueError:
        debug_print(f"比例解析错误 '{ratio_str}'，使用默认比例 1:1")
        return 1.0, 1.0


def generate_recrystallize_protocol(
    G: nx.DiGraph,
    vessel: dict,
    ratio: str,
    solvent1: str,
    solvent2: str,
    volume: Union[str, float],
    **kwargs
) -> List[Dict[str, Any]]:
    """
    生成重结晶协议序列 - 支持vessel字典和体积运算

    Args:
        G: 有向图，节点为容器和设备
        vessel: 目标容器字典（从XDL传入）
        ratio: 溶剂比例（如 "1:1", "3:7"）
        solvent1: 第一种溶剂名称
        solvent2: 第二种溶剂名称
        volume: 总体积（支持 "100 mL", "50", "2.5 L" 等）
        **kwargs: 其他可选参数

    Returns:
        List[Dict[str, Any]]: 动作序列
    """

    vessel_id, vessel_data = get_vessel(vessel)

    action_sequence = []

    debug_print(f"开始生成重结晶协议: vessel={vessel_id}, ratio={ratio}, solvent1={solvent1}, solvent2={solvent2}, volume={volume}")

    # 记录重结晶前的容器状态
    original_liquid_volume = 0.0
    if "data" in vessel and "liquid_volume" in vessel["data"]:
        current_volume = vessel["data"]["liquid_volume"]
        if isinstance(current_volume, list) and len(current_volume) > 0:
            original_liquid_volume = current_volume[0]
        elif isinstance(current_volume, (int, float)):
            original_liquid_volume = current_volume

    # 1. 验证目标容器存在
    if vessel_id not in G.nodes():
        raise ValueError(f"目标容器 '{vessel_id}' 不存在于系统中")

    # 2. 解析体积（支持单位）
    final_volume = parse_volume_input(volume, "mL")
    debug_print(f"体积解析: {volume} -> {final_volume}mL")

    # 3. 解析比例
    ratio1, ratio2 = parse_ratio(ratio)
    total_ratio = ratio1 + ratio2

    # 4. 计算各溶剂体积
    volume1 = final_volume * (ratio1 / total_ratio)
    volume2 = final_volume * (ratio2 / total_ratio)

    debug_print(f"溶剂体积: {solvent1}={volume1:.2f}mL, {solvent2}={volume2:.2f}mL")

    # 5. 查找溶剂容器
    try:
        solvent1_vessel = find_solvent_vessel(G, solvent1)
    except ValueError as e:
        raise ValueError(f"无法找到溶剂1 '{solvent1}': {str(e)}")

    try:
        solvent2_vessel = find_solvent_vessel(G, solvent2)
    except ValueError as e:
        raise ValueError(f"无法找到溶剂2 '{solvent2}': {str(e)}")

    # 6. 验证路径存在
    try:
        path1 = nx.shortest_path(G, source=solvent1_vessel, target=vessel_id)
    except nx.NetworkXNoPath:
        raise ValueError(f"从溶剂1容器 '{solvent1_vessel}' 到目标容器 '{vessel_id}' 没有可用路径")

    try:
        path2 = nx.shortest_path(G, source=solvent2_vessel, target=vessel_id)
    except nx.NetworkXNoPath:
        raise ValueError(f"从溶剂2容器 '{solvent2_vessel}' 到目标容器 '{vessel_id}' 没有可用路径")

    # 7. 添加第一种溶剂
    try:
        pump_actions1 = generate_pump_protocol_with_rinsing(
            G=G,
            from_vessel=solvent1_vessel,
            to_vessel=vessel_id,
            volume=volume1,
            amount="",
            time=0.0,
            viscous=False,
            rinsing_solvent="",
            rinsing_volume=0.0,
            rinsing_repeats=0,
            solid=False,
            flowrate=2.0,
            transfer_flowrate=0.5
        )

        action_sequence.extend(pump_actions1)

    except Exception as e:
        raise ValueError(f"生成溶剂1泵协议时出错: {str(e)}")

    # 更新容器体积 - 添加溶剂1后
    new_volume_after_solvent1 = original_liquid_volume + volume1

    if "data" in vessel and "liquid_volume" in vessel["data"]:
        current_volume = vessel["data"]["liquid_volume"]
        if isinstance(current_volume, list):
            if len(current_volume) > 0:
                vessel["data"]["liquid_volume"][0] = new_volume_after_solvent1
            else:
                vessel["data"]["liquid_volume"] = [new_volume_after_solvent1]
        else:
            vessel["data"]["liquid_volume"] = new_volume_after_solvent1

    if vessel_id in G.nodes():
        if 'data' not in G.nodes[vessel_id]:
            G.nodes[vessel_id]['data'] = {}

        vessel_node_data = G.nodes[vessel_id]['data']
        current_node_volume = vessel_node_data.get('liquid_volume', 0.0)

        if isinstance(current_node_volume, list):
            if len(current_node_volume) > 0:
                G.nodes[vessel_id]['data']['liquid_volume'][0] = new_volume_after_solvent1
            else:
                G.nodes[vessel_id]['data']['liquid_volume'] = [new_volume_after_solvent1]
        else:
            G.nodes[vessel_id]['data']['liquid_volume'] = new_volume_after_solvent1

    # 8. 等待溶剂1稳定
    action_sequence.append({
        "action_name": "wait",
        "action_kwargs": {
            "time": 5.0,
            "description": f"等待溶剂1 {solvent1} 稳定"
        }
    })

    # 9. 添加第二种溶剂
    try:
        pump_actions2 = generate_pump_protocol_with_rinsing(
            G=G,
            from_vessel=solvent2_vessel,
            to_vessel=vessel_id,
            volume=volume2,
            amount="",
            time=0.0,
            viscous=False,
            rinsing_solvent="",
            rinsing_volume=0.0,
            rinsing_repeats=0,
            solid=False,
            flowrate=2.0,
            transfer_flowrate=0.5
        )

        action_sequence.extend(pump_actions2)

    except Exception as e:
        raise ValueError(f"生成溶剂2泵协议时出错: {str(e)}")

    # 更新容器体积 - 添加溶剂2后
    final_liquid_volume = new_volume_after_solvent1 + volume2

    if "data" in vessel and "liquid_volume" in vessel["data"]:
        current_volume = vessel["data"]["liquid_volume"]
        if isinstance(current_volume, list):
            if len(current_volume) > 0:
                vessel["data"]["liquid_volume"][0] = final_liquid_volume
            else:
                vessel["data"]["liquid_volume"] = [final_liquid_volume]
        else:
            vessel["data"]["liquid_volume"] = final_liquid_volume

    if vessel_id in G.nodes():
        if 'data' not in G.nodes[vessel_id]:
            G.nodes[vessel_id]['data'] = {}

        vessel_node_data = G.nodes[vessel_id]['data']
        current_node_volume = vessel_node_data.get('liquid_volume', 0.0)

        if isinstance(current_node_volume, list):
            if len(current_node_volume) > 0:
                G.nodes[vessel_id]['data']['liquid_volume'][0] = final_liquid_volume
            else:
                G.nodes[vessel_id]['data']['liquid_volume'] = [final_liquid_volume]
        else:
            G.nodes[vessel_id]['data']['liquid_volume'] = final_liquid_volume

    # 10. 等待溶剂2稳定
    action_sequence.append({
        "action_name": "wait",
        "action_kwargs": {
            "time": 5.0,
            "description": f"等待溶剂2 {solvent2} 稳定"
        }
    })

    # 11. 等待重结晶完成
    original_crystallize_time = 600.0
    simulation_time_limit = 60.0

    final_crystallize_time = min(original_crystallize_time, simulation_time_limit)

    if original_crystallize_time > simulation_time_limit:
        debug_print(f"模拟运行优化: {original_crystallize_time}s -> {final_crystallize_time}s")

    action_sequence.append({
        "action_name": "wait",
        "action_kwargs": {
            "time": final_crystallize_time,
            "description": f"等待重结晶完成（{solvent1}:{solvent2} = {ratio}，总体积 {final_volume}mL）" + (f" (模拟时间)" if original_crystallize_time != final_crystallize_time else "")
        }
    })

    debug_print(f"重结晶协议生成完成: {len(action_sequence)} 个动作, 容器={vessel_id}, 体积变化: {original_liquid_volume:.2f} -> {final_liquid_volume:.2f}mL")

    return action_sequence


# 测试函数
def test_recrystallize_protocol():
    """测试重结晶协议"""
    debug_print("=== RECRYSTALLIZE PROTOCOL 测试 ===")

    test_volumes = ["100 mL", "2.5 L", "500", "50.5", "?", "invalid"]
    for vol in test_volumes:
        parsed = parse_volume_input(vol)
        debug_print(f"体积 '{vol}' -> {parsed}mL")

    test_ratios = ["1:1", "3:7", "50:50", "1-1", "2,8", "invalid"]
    for ratio in test_ratios:
        r1, r2 = parse_ratio(ratio)
        debug_print(f"比例 '{ratio}' -> {r1}:{r2}")

    debug_print("测试完成")

if __name__ == "__main__":
    test_recrystallize_protocol()
