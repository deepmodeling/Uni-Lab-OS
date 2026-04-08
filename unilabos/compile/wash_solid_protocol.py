from typing import List, Dict, Any, Union
import networkx as nx
import logging
import re

from .utils.unit_parser import parse_time_input, parse_volume_input
from .utils.resource_helper import get_resource_id, get_resource_display_info, get_resource_liquid_volume, update_vessel_volume
from .utils.logger_util import debug_print

logger = logging.getLogger(__name__)


def find_solvent_source(G: nx.DiGraph, solvent: str) -> str:
    """查找溶剂源"""
    search_patterns = [
        f"flask_{solvent}", f"bottle_{solvent}", f"reagent_{solvent}",
        "liquid_reagent_bottle_1", "flask_1", "solvent_bottle"
    ]

    for pattern in search_patterns:
        if pattern in G.nodes():
            debug_print(f"找到溶剂源: {pattern}")
            return pattern

    debug_print(f"使用默认溶剂源: flask_{solvent}")
    return f"flask_{solvent}"

def find_filtrate_vessel(G: nx.DiGraph, filtrate_vessel: str = "") -> str:
    """查找滤液容器"""
    if filtrate_vessel and filtrate_vessel in G.nodes():
        return filtrate_vessel

    default_vessels = ["waste_workup", "filtrate_vessel", "flask_1", "collection_bottle_1"]

    for vessel in default_vessels:
        if vessel in G.nodes():
            debug_print(f"找到滤液容器: {vessel}")
            return vessel

    return "waste_workup"

def extract_vessel_id(vessel) -> str:
    """从vessel参数中提取vessel_id，兼容 str / dict / ResourceDictInstance"""
    return get_resource_id(vessel)

def get_vessel_display_info(vessel) -> str:
    """获取容器的显示信息（用于日志），兼容 str / dict / ResourceDictInstance"""
    return get_resource_display_info(vessel)

def generate_wash_solid_protocol(
    G: nx.DiGraph,
    vessel: Union[str, dict],
    solvent: str,
    volume: Union[float, str] = "50",
    filtrate_vessel: Union[str, dict] = "",
    temp: float = 25.0,
    stir: bool = False,
    stir_speed: float = 0.0,
    time: Union[str, float] = "0",
    repeats: int = 1,
    volume_spec: str = "",
    repeats_spec: str = "",
    mass: str = "",
    event: str = "",
    **kwargs
) -> List[Dict[str, Any]]:
    """
    生成固体清洗协议 - 支持vessel字典和体积运算

    Args:
        G: 有向图，节点为设备和容器，边为流体管道
        vessel: 清洗容器字典（从XDL传入）或容器ID字符串
        solvent: 清洗溶剂名称
        volume: 溶剂体积（每次清洗）
        filtrate_vessel: 滤液收集容器字典或容器ID字符串
        temp: 清洗温度（°C）
        stir: 是否搅拌
        stir_speed: 搅拌速度（RPM）
        time: 搅拌时间
        repeats: 清洗重复次数
        volume_spec: 体积规格（small/medium/large）
        repeats_spec: 重复次数规格（few/several/many）
        mass: 固体质量（用于计算溶剂用量）
        event: 事件描述
        **kwargs: 其他可选参数

    Returns:
        List[Dict[str, Any]]: 固体清洗操作的动作序列
    """

    vessel_id = extract_vessel_id(vessel)
    vessel_display = get_vessel_display_info(vessel)

    filtrate_vessel_id = extract_vessel_id(filtrate_vessel) if filtrate_vessel else ""

    debug_print(f"开始生成固体清洗协议: vessel={vessel_id}, solvent={solvent}, volume={volume}, repeats={repeats}")

    # 记录清洗前的容器状态
    if isinstance(vessel, dict):
        original_volume = get_resource_liquid_volume(vessel)
    else:
        original_volume = 0.0

    # 快速验证
    if not vessel_id or vessel_id not in G.nodes():
        raise ValueError("vessel 参数无效")

    if not solvent:
        raise ValueError("solvent 参数不能为空")

    # 参数解析
    final_volume = parse_volume_input(volume, volume_spec, mass)
    final_time = parse_time_input(time)

    # 重复次数处理
    if repeats_spec:
        spec_map = {'few': 2, 'several': 3, 'many': 4, 'thorough': 5}
        final_repeats = next((v for k, v in spec_map.items() if k in repeats_spec.lower()), repeats)
    else:
        final_repeats = max(1, min(repeats, 5))

    # 模拟时间优化
    original_time = final_time
    if final_time > 60.0:
        final_time = 60.0
        debug_print(f"时间优化: {original_time}s -> {final_time}s")

    # 参数修正
    temp = max(25.0, min(temp, 80.0))
    stir_speed = max(0.0, min(stir_speed, 300.0)) if stir else 0.0

    debug_print(f"最终参数: 体积={final_volume}mL, 时间={final_time}s, 重复={final_repeats}次")

    # 查找设备
    try:
        solvent_source = find_solvent_source(G, solvent)
        actual_filtrate_vessel = find_filtrate_vessel(G, filtrate_vessel_id)
    except Exception as e:
        raise ValueError(f"设备查找失败: {str(e)}")

    # 生成动作序列
    action_sequence = []

    current_volume = original_volume
    total_solvent_used = 0.0

    for cycle in range(final_repeats):
        debug_print(f"第{cycle+1}/{final_repeats}次清洗")

        # 1. 转移溶剂
        try:
            from .pump_protocol import generate_pump_protocol_with_rinsing

            transfer_actions = generate_pump_protocol_with_rinsing(
                G=G,
                from_vessel=solvent_source,
                to_vessel=vessel_id,
                volume=final_volume,
                amount="",
                time=0.0,
                viscous=False,
                rinsing_solvent="",
                rinsing_volume=0.0,
                rinsing_repeats=0,
                solid=False,
                flowrate=2.5,
                transfer_flowrate=0.5
            )

            if transfer_actions:
                action_sequence.extend(transfer_actions)

                current_volume += final_volume
                total_solvent_used += final_volume

                if isinstance(vessel, dict):
                    update_vessel_volume(vessel, G, current_volume,
                                       f"第{cycle+1}次清洗添加{final_volume}mL溶剂后")

        except Exception as e:
            debug_print(f"转移失败: {str(e)}")

        # 2. 搅拌（如果需要）
        if stir and final_time > 0:
            stir_action = {
                "device_id": "stirrer_1",
                "action_name": "stir",
                "action_kwargs": {
                    "vessel": {"id": vessel_id},
                    "time": str(time),
                    "stir_time": final_time,
                    "stir_speed": stir_speed,
                    "settling_time": 10.0
                }
            }
            action_sequence.append(stir_action)

        # 3. 过滤
        filter_action = {
            "device_id": "filter_1",
            "action_name": "filter",
            "action_kwargs": {
                "vessel": {"id": vessel_id},
                "filtrate_vessel": actual_filtrate_vessel,
                "temp": temp,
                "volume": final_volume
            }
        }
        action_sequence.append(filter_action)

        # 更新体积 - 过滤后
        filtered_volume = current_volume * 0.9
        current_volume = current_volume - filtered_volume

        if isinstance(vessel, dict):
            update_vessel_volume(vessel, G, current_volume,
                               f"第{cycle+1}次清洗过滤后")

        # 4. 等待
        wait_time = 5.0
        action_sequence.append({
            "action_name": "wait",
            "action_kwargs": {"time": wait_time}
        })

    # 最终状态
    if isinstance(vessel, dict):
        final_volume_vessel = get_resource_liquid_volume(vessel)
    else:
        final_volume_vessel = current_volume

    debug_print(f"固体清洗协议生成完成: {len(action_sequence)} 个动作, {final_repeats}次清洗, 溶剂总用量={total_solvent_used:.2f}mL")

    return action_sequence

# 便捷函数
def wash_with_water(G: nx.DiGraph, vessel: Union[str, dict],
                   volume: Union[float, str] = "50",
                   repeats: int = 2) -> List[Dict[str, Any]]:
    """用水清洗固体"""
    return generate_wash_solid_protocol(G, vessel, "water", volume=volume, repeats=repeats)

def wash_with_ethanol(G: nx.DiGraph, vessel: Union[str, dict],
                     volume: Union[float, str] = "30",
                     repeats: int = 1) -> List[Dict[str, Any]]:
    """用乙醇清洗固体"""
    return generate_wash_solid_protocol(G, vessel, "ethanol", volume=volume, repeats=repeats)

def wash_with_acetone(G: nx.DiGraph, vessel: Union[str, dict],
                     volume: Union[float, str] = "25",
                     repeats: int = 1) -> List[Dict[str, Any]]:
    """用丙酮清洗固体"""
    return generate_wash_solid_protocol(G, vessel, "acetone", volume=volume, repeats=repeats)

def wash_with_ether(G: nx.DiGraph, vessel: Union[str, dict],
                   volume: Union[float, str] = "40",
                   repeats: int = 2) -> List[Dict[str, Any]]:
    """用乙醚清洗固体"""
    return generate_wash_solid_protocol(G, vessel, "diethyl_ether", volume=volume, repeats=repeats)

def wash_with_cold_solvent(G: nx.DiGraph, vessel: Union[str, dict],
                          solvent: str, volume: Union[float, str] = "30",
                          repeats: int = 1) -> List[Dict[str, Any]]:
    """用冷溶剂清洗固体"""
    return generate_wash_solid_protocol(G, vessel, solvent, volume=volume,
                                      temp=5.0, repeats=repeats)

def wash_with_hot_solvent(G: nx.DiGraph, vessel: Union[str, dict],
                         solvent: str, volume: Union[float, str] = "50",
                         repeats: int = 1) -> List[Dict[str, Any]]:
    """用热溶剂清洗固体"""
    return generate_wash_solid_protocol(G, vessel, solvent, volume=volume,
                                      temp=60.0, repeats=repeats)

def wash_with_stirring(G: nx.DiGraph, vessel: Union[str, dict],
                      solvent: str, volume: Union[float, str] = "50",
                      stir_time: Union[str, float] = "5 min",
                      repeats: int = 1) -> List[Dict[str, Any]]:
    """带搅拌的溶剂清洗"""
    return generate_wash_solid_protocol(G, vessel, solvent, volume=volume,
                                      stir=True, stir_speed=200.0,
                                      time=stir_time, repeats=repeats)

def thorough_wash(G: nx.DiGraph, vessel: Union[str, dict],
                 solvent: str, volume: Union[float, str] = "50") -> List[Dict[str, Any]]:
    """彻底清洗（多次重复）"""
    return generate_wash_solid_protocol(G, vessel, solvent, volume=volume, repeats=5)

def quick_rinse(G: nx.DiGraph, vessel: Union[str, dict],
               solvent: str, volume: Union[float, str] = "20") -> List[Dict[str, Any]]:
    """快速冲洗（单次，小体积）"""
    return generate_wash_solid_protocol(G, vessel, solvent, volume=volume, repeats=1)

def sequential_wash(G: nx.DiGraph, vessel: Union[str, dict],
                   solvents: list, volume: Union[float, str] = "40") -> List[Dict[str, Any]]:
    """连续多溶剂清洗"""
    action_sequence = []
    for solvent in solvents:
        wash_actions = generate_wash_solid_protocol(G, vessel, solvent,
                                                   volume=volume, repeats=1)
        action_sequence.extend(wash_actions)

    return action_sequence

# 测试函数
def test_wash_solid_protocol():
    """测试固体清洗协议"""
    debug_print("=== WASH SOLID PROTOCOL 测试 ===")

    vessel_dict = {"id": "filter_flask_1", "name": "过滤瓶1",
                  "data": {"liquid_volume": 25.0}}
    vessel_id = extract_vessel_id(vessel_dict)
    vessel_display = get_vessel_display_info(vessel_dict)
    volume = get_resource_liquid_volume(vessel_dict)
    debug_print(f"字典格式: ID={vessel_id}, 显示={vessel_display}, 体积={volume}mL")

    vessel_str = "filter_flask_2"
    vessel_id = extract_vessel_id(vessel_str)
    vessel_display = get_vessel_display_info(vessel_str)
    debug_print(f"字符串格式: ID={vessel_id}, 显示={vessel_display}")

    debug_print("测试完成")

if __name__ == "__main__":
    test_wash_solid_protocol()
