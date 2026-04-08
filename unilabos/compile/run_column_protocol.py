from typing import List, Dict, Any, Union
import networkx as nx
import logging
import re
from .utils.vessel_parser import get_vessel, find_solvent_vessel
from .utils.resource_helper import get_resource_id, get_resource_data, get_resource_liquid_volume, update_vessel_volume
from .utils.logger_util import debug_print
from .pump_protocol import generate_pump_protocol_with_rinsing

logger = logging.getLogger(__name__)

def parse_percentage(pct_str: str) -> float:
    """
    解析百分比字符串为数值

    Args:
        pct_str: 百分比字符串（如 "40 %", "40%", "40"）

    Returns:
        float: 百分比数值（0-100）
    """
    if not pct_str or not pct_str.strip():
        return 0.0

    pct_str = pct_str.strip().lower()

    # 移除百分号和空格
    pct_clean = re.sub(r'[%\s]', '', pct_str)

    match = re.search(r'([0-9]*\.?[0-9]+)', pct_clean)
    if match:
        value = float(match.group(1))
        return value

    debug_print(f"无法解析百分比: '{pct_str}'，返回0.0")
    return 0.0

def parse_ratio(ratio_str: str) -> tuple:
    """
    解析比例字符串为两个数值

    Args:
        ratio_str: 比例字符串（如 "5:95", "1:1", "40:60"）

    Returns:
        tuple: (ratio1, ratio2) 两个比例值（百分比）
    """
    if not ratio_str or not ratio_str.strip():
        return (50.0, 50.0)

    ratio_str = ratio_str.strip()

    # 支持多种分隔符：: / -
    if ':' in ratio_str:
        parts = ratio_str.split(':')
    elif '/' in ratio_str:
        parts = ratio_str.split('/')
    elif '-' in ratio_str:
        parts = ratio_str.split('-')
    elif 'to' in ratio_str.lower():
        parts = ratio_str.lower().split('to')
    else:
        debug_print(f"无法解析比例格式: '{ratio_str}'，使用默认1:1")
        return (50.0, 50.0)

    if len(parts) >= 2:
        try:
            ratio1 = float(parts[0].strip())
            ratio2 = float(parts[1].strip())
            total = ratio1 + ratio2

            pct1 = (ratio1 / total) * 100
            pct2 = (ratio2 / total) * 100

            return (pct1, pct2)
        except ValueError as e:
            debug_print(f"比例数值转换失败: {str(e)}")

    debug_print(f"比例解析失败，使用默认1:1")
    return (50.0, 50.0)

def parse_rf_value(rf_str: str) -> float:
    """
    解析Rf值字符串

    Args:
        rf_str: Rf值字符串（如 "0.3", "0.45", "?"）

    Returns:
        float: Rf值（0-1）
    """
    if not rf_str or not rf_str.strip():
        return 0.3

    rf_str = rf_str.strip().lower()

    if rf_str in ['?', 'unknown', 'tbd', 'to be determined']:
        return 0.3

    match = re.search(r'([0-9]*\.?[0-9]+)', rf_str)
    if match:
        value = float(match.group(1))
        if value > 1.0:
            value = value / 100.0
        value = max(0.0, min(1.0, value))
        return value

    return 0.3

def find_column_device(G: nx.DiGraph) -> str:
    """查找柱层析设备"""
    for node in G.nodes():
        node_data = G.nodes[node]
        node_class = node_data.get('class', '') or ''

        if 'virtual_column' in node_class.lower() or 'column' in node_class.lower():
            debug_print(f"找到柱层析设备: {node}")
            return node

    possible_names = ['column_1', 'virtual_column_1', 'chromatography_column_1']
    for name in possible_names:
        if name in G.nodes():
            debug_print(f"找到柱设备: {name}")
            return name

    debug_print("未找到柱层析设备，将使用pump protocol直接转移")
    return ""

def find_column_vessel(G: nx.DiGraph, column: str) -> str:
    """查找柱容器"""
    if column in G.nodes():
        node_type = G.nodes[column].get('type', '')
        if node_type == 'container':
            return column

    possible_names = [
        f"column_{column}",
        f"{column}_column",
        f"vessel_{column}",
        f"{column}_vessel",
        "column_vessel",
        "chromatography_column",
        "silica_column",
        "preparative_column",
        "column"
    ]

    for vessel_name in possible_names:
        if vessel_name in G.nodes():
            node_type = G.nodes[vessel_name].get('type', '')
            if node_type == 'container':
                return vessel_name

    return ""

def calculate_solvent_volumes(total_volume: float, pct1: float, pct2: float) -> tuple:
    """根据百分比计算溶剂体积"""
    volume1 = (total_volume * pct1) / 100.0
    volume2 = (total_volume * pct2) / 100.0
    return (volume1, volume2)

def generate_run_column_protocol(
    G: nx.DiGraph,
    from_vessel: dict,
    to_vessel: dict,
    column: str,
    rf: str = "",
    pct1: str = "",
    pct2: str = "",
    solvent1: str = "",
    solvent2: str = "",
    ratio: str = "",
    **kwargs
) -> List[Dict[str, Any]]:
    """
    生成柱层析分离的协议序列 - 支持vessel字典和体积运算

    Args:
        G: 有向图，节点为设备和容器，边为流体管道
        from_vessel: 源容器字典（从XDL传入）
        to_vessel: 目标容器字典（从XDL传入）
        column: 所使用的柱子的名称（必需）
        rf: Rf值（可选，支持 "?" 表示未知）
        pct1: 第一种溶剂百分比（如 "40 %"，可选）
        pct2: 第二种溶剂百分比（如 "50 %"，可选）
        solvent1: 第一种溶剂名称（可选）
        solvent2: 第二种溶剂名称（可选）
        ratio: 溶剂比例（如 "5:95"，可选，优先级高于pct1/pct2）
        **kwargs: 其他可选参数

    Returns:
        List[Dict[str, Any]]: 柱层析分离操作的动作序列
    """

    from_vessel_id, _ = get_vessel(from_vessel)
    to_vessel_id, _ = get_vessel(to_vessel)

    debug_print(f"开始生成柱层析协议: {from_vessel_id} -> {to_vessel_id}, column={column}")

    action_sequence = []

    # 记录柱层析前的容器状态
    original_from_volume = get_resource_liquid_volume(from_vessel)
    original_to_volume = get_resource_liquid_volume(to_vessel)

    # === 参数验证 ===
    if not from_vessel_id:
        raise ValueError("from_vessel 参数不能为空")
    if not to_vessel_id:
        raise ValueError("to_vessel 参数不能为空")
    if not column:
        raise ValueError("column 参数不能为空")

    if from_vessel_id not in G.nodes():
        raise ValueError(f"源容器 '{from_vessel_id}' 不存在于系统中")
    if to_vessel_id not in G.nodes():
        raise ValueError(f"目标容器 '{to_vessel_id}' 不存在于系统中")

    # === 参数解析 ===
    final_rf = parse_rf_value(rf)

    if ratio and ratio.strip():
        final_pct1, final_pct2 = parse_ratio(ratio)
    else:
        final_pct1 = parse_percentage(pct1) if pct1 else 50.0
        final_pct2 = parse_percentage(pct2) if pct2 else 50.0

        total_pct = final_pct1 + final_pct2
        if total_pct == 0:
            final_pct1, final_pct2 = 50.0, 50.0
        elif total_pct != 100.0:
            final_pct1 = (final_pct1 / total_pct) * 100
            final_pct2 = (final_pct2 / total_pct) * 100

    final_solvent1 = solvent1.strip() if solvent1 else "ethyl_acetate"
    final_solvent2 = solvent2.strip() if solvent2 else "hexane"

    debug_print(f"参数: rf={final_rf}, 溶剂={final_solvent1}:{final_solvent2} = {final_pct1:.1f}%:{final_pct2:.1f}%")

    # === 查找设备和容器 ===
    column_device_id = find_column_device(G)
    column_vessel = find_column_vessel(G, column)
    solvent1_vessel = find_solvent_vessel(G, final_solvent1)
    solvent2_vessel = find_solvent_vessel(G, final_solvent2)

    # === 获取源容器体积 ===
    source_volume = original_from_volume
    if source_volume <= 0:
        source_volume = 50.0

    # === 计算溶剂体积 ===
    total_elution_volume = source_volume * 3.0
    solvent1_volume, solvent2_volume = calculate_solvent_volumes(
        total_elution_volume, final_pct1, final_pct2
    )

    # === 执行柱层析流程 ===
    current_from_volume = source_volume
    current_to_volume = original_to_volume
    current_column_volume = 0.0

    try:
        # 步骤1: 样品上柱
        if column_vessel and column_vessel != from_vessel_id:
            try:
                sample_transfer_actions = generate_pump_protocol_with_rinsing(
                    G=G,
                    from_vessel=from_vessel_id,
                    to_vessel=column_vessel,
                    volume=source_volume,
                    flowrate=1.0,
                    transfer_flowrate=0.5,
                    rinsing_solvent="",
                    rinsing_volume=0.0,
                    rinsing_repeats=0
                )
                action_sequence.extend(sample_transfer_actions)

                current_from_volume = 0.0
                current_column_volume = source_volume

                update_vessel_volume(from_vessel, G, current_from_volume, "样品上柱后，源容器清空")

                if column_vessel in G.nodes():
                    if 'data' not in G.nodes[column_vessel]:
                        G.nodes[column_vessel]['data'] = {}
                    G.nodes[column_vessel]['data']['liquid_volume'] = current_column_volume

            except Exception as e:
                debug_print(f"样品上柱失败: {str(e)}")

        # 步骤2: 添加洗脱溶剂1
        if solvent1_vessel and solvent1_volume > 0:
            try:
                target_vessel = column_vessel if column_vessel else from_vessel_id
                solvent1_transfer_actions = generate_pump_protocol_with_rinsing(
                    G=G,
                    from_vessel=solvent1_vessel,
                    to_vessel=target_vessel,
                    volume=solvent1_volume,
                    flowrate=2.0,
                    transfer_flowrate=1.0
                )
                action_sequence.extend(solvent1_transfer_actions)

                if target_vessel == column_vessel:
                    current_column_volume += solvent1_volume
                    if column_vessel in G.nodes():
                        G.nodes[column_vessel]['data']['liquid_volume'] = current_column_volume
                elif target_vessel == from_vessel_id:
                    current_from_volume += solvent1_volume
                    update_vessel_volume(from_vessel, G, current_from_volume, "添加溶剂1后")

            except Exception as e:
                debug_print(f"溶剂1添加失败: {str(e)}")

        # 步骤3: 添加洗脱溶剂2
        if solvent2_vessel and solvent2_volume > 0:
            try:
                target_vessel = column_vessel if column_vessel else from_vessel_id
                solvent2_transfer_actions = generate_pump_protocol_with_rinsing(
                    G=G,
                    from_vessel=solvent2_vessel,
                    to_vessel=target_vessel,
                    volume=solvent2_volume,
                    flowrate=2.0,
                    transfer_flowrate=1.0
                )
                action_sequence.extend(solvent2_transfer_actions)

                if target_vessel == column_vessel:
                    current_column_volume += solvent2_volume
                    if column_vessel in G.nodes():
                        G.nodes[column_vessel]['data']['liquid_volume'] = current_column_volume
                elif target_vessel == from_vessel_id:
                    current_from_volume += solvent2_volume
                    update_vessel_volume(from_vessel, G, current_from_volume, "添加溶剂2后")

            except Exception as e:
                debug_print(f"溶剂2添加失败: {str(e)}")

        # 步骤4: 使用柱层析设备执行分离
        if column_device_id:
            column_separation_action = {
                "device_id": column_device_id,
                "action_name": "run_column",
                "action_kwargs": {
                    "from_vessel": from_vessel_id,
                    "to_vessel": to_vessel_id,
                    "column": column,
                    "rf": rf,
                    "pct1": pct1,
                    "pct2": pct2,
                    "solvent1": solvent1,
                    "solvent2": solvent2,
                    "ratio": ratio
                }
            }
            action_sequence.append(column_separation_action)

            separation_time = max(30, min(120, int(total_elution_volume / 2)))
            action_sequence.append({
                "action_name": "wait",
                "action_kwargs": {"time": separation_time}
            })

        # 步骤5: 产物收集
        if column_vessel and column_vessel != to_vessel_id:
            try:
                product_volume = source_volume * 0.8

                product_transfer_actions = generate_pump_protocol_with_rinsing(
                    G=G,
                    from_vessel=column_vessel,
                    to_vessel=to_vessel_id,
                    volume=product_volume,
                    flowrate=1.5,
                    transfer_flowrate=0.8
                )
                action_sequence.extend(product_transfer_actions)

                current_to_volume += product_volume
                current_column_volume -= product_volume

                update_vessel_volume(to_vessel, G, current_to_volume, "产物收集后")

                if column_vessel in G.nodes():
                    G.nodes[column_vessel]['data']['liquid_volume'] = max(0.0, current_column_volume)

            except Exception as e:
                debug_print(f"产物收集失败: {str(e)}")

        # 步骤6: 简化模式 - 直接转移
        if not column_device_id and not column_vessel:
            try:
                direct_transfer_actions = generate_pump_protocol_with_rinsing(
                    G=G,
                    from_vessel=from_vessel_id,
                    to_vessel=to_vessel_id,
                    volume=source_volume,
                    flowrate=2.0,
                    transfer_flowrate=1.0
                )
                action_sequence.extend(direct_transfer_actions)

                current_from_volume = 0.0
                current_to_volume += source_volume

                update_vessel_volume(from_vessel, G, current_from_volume, "直接转移后，源容器清空")
                update_vessel_volume(to_vessel, G, current_to_volume, "直接转移后，目标容器增加")

            except Exception as e:
                debug_print(f"直接转移失败: {str(e)}")

    except Exception as e:
        debug_print(f"协议生成失败: {str(e)}")

    if not action_sequence:
        action_sequence.append({
            "action_name": "wait",
            "action_kwargs": {
                "time": 1.0,
                "description": "柱层析协议执行完成"
            }
        })

    final_from_volume = get_resource_liquid_volume(from_vessel)
    final_to_volume = get_resource_liquid_volume(to_vessel)

    debug_print(f"柱层析协议生成完成: {len(action_sequence)} 个动作, {from_vessel_id} -> {to_vessel_id}, 收集={final_to_volume - original_to_volume:.2f}mL")

    return action_sequence

# 便捷函数
def generate_ethyl_acetate_hexane_column_protocol(G: nx.DiGraph, from_vessel: dict, to_vessel: dict,
                                                 column: str, ratio: str = "30:70") -> List[Dict[str, Any]]:
    """乙酸乙酯-己烷柱层析（常用组合）"""
    return generate_run_column_protocol(G, from_vessel, to_vessel, column,
                                      solvent1="ethyl_acetate", solvent2="hexane", ratio=ratio)

def generate_methanol_dcm_column_protocol(G: nx.DiGraph, from_vessel: dict, to_vessel: dict,
                                        column: str, ratio: str = "5:95") -> List[Dict[str, Any]]:
    """甲醇-二氯甲烷柱层析"""
    return generate_run_column_protocol(G, from_vessel, to_vessel, column,
                                      solvent1="methanol", solvent2="dichloromethane", ratio=ratio)

def generate_gradient_column_protocol(G: nx.DiGraph, from_vessel: dict, to_vessel: dict,
                                    column: str, start_ratio: str = "10:90",
                                    end_ratio: str = "50:50") -> List[Dict[str, Any]]:
    """梯度洗脱柱层析（中等比例）"""
    return generate_run_column_protocol(G, from_vessel, to_vessel, column, ratio="30:70")

def generate_polar_column_protocol(G: nx.DiGraph, from_vessel: dict, to_vessel: dict,
                                 column: str) -> List[Dict[str, Any]]:
    """极性化合物柱层析（高极性溶剂比例）"""
    return generate_run_column_protocol(G, from_vessel, to_vessel, column,
                                      solvent1="ethyl_acetate", solvent2="hexane", ratio="70:30")

def generate_nonpolar_column_protocol(G: nx.DiGraph, from_vessel: dict, to_vessel: dict,
                                    column: str) -> List[Dict[str, Any]]:
    """非极性化合物柱层析（低极性溶剂比例）"""
    return generate_run_column_protocol(G, from_vessel, to_vessel, column,
                                      solvent1="ethyl_acetate", solvent2="hexane", ratio="5:95")

# 测试函数
def test_run_column_protocol():
    """测试柱层析协议"""
    debug_print("=== RUN COLUMN PROTOCOL 测试 ===")
    debug_print("测试完成")

if __name__ == "__main__":
    test_run_column_protocol()
