from functools import partial

import networkx as nx
from typing import List, Dict, Any, Union
from .utils.vessel_parser import get_vessel, find_connected_stirrer
from .utils.logger_util import action_log, debug_print
from .pump_protocol import generate_pump_protocol_with_rinsing

create_action_log = partial(action_log, prefix="[ADJUST_PH]")

def find_acid_base_vessel(G: nx.DiGraph, reagent: str) -> str:
    """
    查找酸碱试剂容器，支持多种匹配模式
    
    Args:
        G: 网络图
        reagent: 试剂名称（如 "hydrochloric acid", "sodium hydroxide"）
    
    Returns:
        str: 试剂容器ID
    """
    # 常见酸碱试剂的别名映射
    reagent_aliases = {
        "hydrochloric acid": ["HCl", "hydrochloric_acid", "hcl", "muriatic_acid"],
        "sodium hydroxide": ["NaOH", "sodium_hydroxide", "naoh", "caustic_soda"],
        "sulfuric acid": ["H2SO4", "sulfuric_acid", "h2so4"],
        "nitric acid": ["HNO3", "nitric_acid", "hno3"],
        "acetic acid": ["CH3COOH", "acetic_acid", "glacial_acetic_acid"],
        "ammonia": ["NH3", "ammonium_hydroxide", "nh3"],
        "potassium hydroxide": ["KOH", "potassium_hydroxide", "koh"]
    }
    
    # 构建搜索名称列表
    search_names = [reagent.lower()]

    # 添加别名
    for base_name, aliases in reagent_aliases.items():
        if reagent.lower() in base_name.lower() or base_name.lower() in reagent.lower():
            search_names.extend([alias.lower() for alias in aliases])
            break
    
    # 构建可能的容器名称
    possible_names = []
    for name in search_names:
        name_clean = name.replace(" ", "_").replace("-", "_")
        possible_names.extend([
            f"flask_{name_clean}",
            f"bottle_{name_clean}",
            f"reagent_{name_clean}",
            f"acid_{name_clean}" if "acid" in name else f"base_{name_clean}",
            f"{name_clean}_bottle",
            f"{name_clean}_flask",
            name_clean
        ])
    
    debug_print(f"搜索容器: {len(possible_names)} 个候选名称")

    # 第一步：通过容器名称匹配
    for vessel_name in possible_names:
        if vessel_name in G.nodes():
            debug_print(f"通过名称匹配找到容器: {vessel_name}")
            return vessel_name

    # 第二步：通过模糊匹配
    for node_id in G.nodes():
        if G.nodes[node_id].get('type') == 'container':
            node_name = G.nodes[node_id].get('name', '').lower()
            
            # 检查是否包含任何搜索名称
            for search_name in search_names:
                if search_name in node_id.lower() or search_name in node_name:
                    debug_print(f"通过模糊匹配找到容器: {node_id}")
                    return node_id

    # 第三步：通过液体类型匹配
    for node_id in G.nodes():
        if G.nodes[node_id].get('type') == 'container':
            vessel_data = G.nodes[node_id].get('data', {})
            liquids = vessel_data.get('liquid', [])
            
            for liquid in liquids:
                if isinstance(liquid, dict):
                    liquid_type = (liquid.get('liquid_type') or liquid.get('name', '')).lower()
                    reagent_name = vessel_data.get('reagent_name', '').lower()
                    
                    for search_name in search_names:
                        if search_name in liquid_type or search_name in reagent_name:
                            debug_print(f"通过液体类型匹配找到容器: {node_id}")
                            return node_id
    
    # 列出可用容器帮助调试
    available_containers = [node_id for node_id in G.nodes()
                           if G.nodes[node_id].get('type') == 'container']
    debug_print(f"所有匹配方法失败，可用容器: {available_containers}")
    raise ValueError(f"找不到试剂 '{reagent}' 对应的容器。尝试了: {possible_names[:10]}...")

def calculate_reagent_volume(target_ph_value: float, reagent: str, vessel_volume: float = 100.0) -> float:
    """
    估算需要的试剂体积来调节pH
    
    Args:
        target_ph_value: 目标pH值
        reagent: 试剂名称
        vessel_volume: 容器体积 (mL)
    
    Returns:
        float: 估算的试剂体积 (mL)
    """
    debug_print(f"计算试剂体积: pH={target_ph_value}, reagent={reagent}, vessel={vessel_volume}mL")

    # 简化的pH调节体积估算
    if "acid" in reagent.lower() or "hcl" in reagent.lower():
        if target_ph_value < 3:
            volume = vessel_volume * 0.05
        elif target_ph_value < 5:
            volume = vessel_volume * 0.02
        else:
            volume = vessel_volume * 0.01

    elif "hydroxide" in reagent.lower() or "naoh" in reagent.lower():
        if target_ph_value > 11:
            volume = vessel_volume * 0.05
        elif target_ph_value > 9:
            volume = vessel_volume * 0.02
        else:
            volume = vessel_volume * 0.01

    else:
        # 未知试剂，使用默认值
        volume = vessel_volume * 0.01

    debug_print(f"估算试剂体积: {volume:.2f}mL")
    return volume

def generate_adjust_ph_protocol(
    G: nx.DiGraph,
    vessel:Union[dict,str],  # 🔧 修改：从字符串改为字典类型
    ph_value: float,
    reagent: str,
    **kwargs
) -> List[Dict[str, Any]]:
    """
    生成调节pH的协议序列
    
    Args:
        G: 有向图，节点为容器和设备
        vessel: 目标容器字典（需要调节pH的容器）
        ph_value: 目标pH值（从XDL传入）
        reagent: 酸碱试剂名称（从XDL传入）
        **kwargs: 其他可选参数，使用默认值
    
    Returns:
        List[Dict[str, Any]]: 动作序列
    """
    
    vessel_id, vessel_data = get_vessel(vessel)

    if not vessel_id:
        raise ValueError("vessel 参数无效，必须包含id字段或直接提供容器ID")

    debug_print(f"pH调节协议: vessel={vessel_id}, ph={ph_value}, reagent='{reagent}'")

    action_sequence = []

    # 从kwargs中获取可选参数
    volume = kwargs.get('volume', 0.0)
    stir = kwargs.get('stir', True)
    stir_speed = kwargs.get('stir_speed', 300.0)
    stir_time = kwargs.get('stir_time', 60.0)
    settling_time = kwargs.get('settling_time', 30.0)

    # 开始处理
    action_sequence.append(create_action_log(f"开始调节pH至 {ph_value}", "🧪"))
    action_sequence.append(create_action_log(f"目标容器: {vessel_id}", "🥼"))
    action_sequence.append(create_action_log(f"使用试剂: {reagent}", "⚗️"))

    # 1. 验证目标容器存在
    if vessel_id not in G.nodes():
        raise ValueError(f"目标容器 '{vessel_id}' 不存在于系统中")

    action_sequence.append(create_action_log("目标容器验证通过", "✅"))

    # 2. 查找酸碱试剂容器
    action_sequence.append(create_action_log("正在查找试剂容器...", "🔍"))
    
    try:
        reagent_vessel = find_acid_base_vessel(G, reagent)
        action_sequence.append(create_action_log(f"找到试剂容器: {reagent_vessel}", "🧪"))
    except ValueError as e:
        action_sequence.append(create_action_log(f"试剂容器查找失败: {str(e)}", "❌"))
        raise ValueError(f"无法找到试剂 '{reagent}': {str(e)}")

    # 3. 体积估算
    if volume <= 0:
        action_sequence.append(create_action_log("开始自动估算试剂体积", "🧮"))
        
        # 获取目标容器的体积信息
        vessel_data = G.nodes[vessel_id].get('data', {})
        vessel_volume = vessel_data.get('max_volume', 100.0)

        estimated_volume = calculate_reagent_volume(ph_value, reagent, vessel_volume)
        volume = estimated_volume
        action_sequence.append(create_action_log(f"估算试剂体积: {volume:.2f}mL", "📊"))
    else:
        action_sequence.append(create_action_log(f"使用指定体积: {volume}mL", "📏"))

    # 4. 验证路径存在
    action_sequence.append(create_action_log("验证转移路径...", "🛤️"))
    
    try:
        path = nx.shortest_path(G, source=reagent_vessel, target=vessel_id)
        action_sequence.append(create_action_log(f"找到转移路径: {' -> '.join(path)}", "🛤️"))
    except nx.NetworkXNoPath:
        action_sequence.append(create_action_log("转移路径不存在", "❌"))
        raise ValueError(f"从试剂容器 '{reagent_vessel}' 到目标容器 '{vessel_id}' 没有可用路径")

    # 5. 搅拌器设置
    stirrer_id = None
    if stir:
        action_sequence.append(create_action_log("准备启动搅拌器", "🌪️"))
        
        try:
            stirrer_id = find_connected_stirrer(G, vessel_id)
            
            if stirrer_id:
                action_sequence.append(create_action_log(f"启动搅拌器 {stirrer_id} (速度: {stir_speed}rpm)", "🔄"))
                
                action_sequence.append({
                    "device_id": stirrer_id,
                    "action_name": "start_stir",
                    "action_kwargs": {
                        "vessel": {"id": vessel_id},
                        "stir_speed": stir_speed,
                        "purpose": f"pH调节: 启动搅拌，准备添加 {reagent}"
                    }
                })
                
                # 等待搅拌稳定
                action_sequence.append(create_action_log("等待搅拌稳定...", "⏳"))
                action_sequence.append({
                    "action_name": "wait",
                    "action_kwargs": {"time": 5}
                })
            else:
                action_sequence.append(create_action_log("未找到搅拌器，跳过搅拌", "⚠️"))

        except Exception as e:
            action_sequence.append(create_action_log(f"搅拌器配置失败: {str(e)}", "❌"))
    else:
        action_sequence.append(create_action_log("跳过搅拌设置", "⏭️"))

    # 6. 试剂添加
    action_sequence.append(create_action_log(f"开始添加试剂 {volume:.2f}mL", "🚰"))
    
    # 计算添加时间（pH调节需要缓慢添加）
    addition_time = max(30.0, volume * 2.0)
    action_sequence.append(create_action_log(f"设置添加时间: {addition_time:.0f}s (缓慢注入)", "⏱️"))
    
    try:
        action_sequence.append(create_action_log("调用泵协议进行试剂转移", "🔄"))
        
        pump_actions = generate_pump_protocol_with_rinsing(
            G=G,
            from_vessel=reagent_vessel,
            to_vessel=vessel_id,
            volume=volume,
            amount="",
            time=addition_time,
            viscous=False,
            rinsing_solvent="",  # pH调节不需要清洗
            rinsing_volume=0.0,
            rinsing_repeats=0,
            solid=False,
            flowrate=0.5,  # 缓慢注入
            transfer_flowrate=0.3
        )
        
        action_sequence.extend(pump_actions)
        action_sequence.append(create_action_log(f"试剂转移完成 ({len(pump_actions)} 个操作)", "✅"))

        # 体积运算 - 试剂添加成功后更新容器液体体积
        if "data" in vessel and "liquid_volume" in vessel["data"]:
            current_volume = vessel["data"]["liquid_volume"]

            # 处理不同的体积数据格式
            if isinstance(current_volume, list):
                if len(current_volume) > 0:
                    # 增加体积（添加试剂）
                    vessel["data"]["liquid_volume"][0] += volume
                else:
                    # 如果列表为空，创建新的体积记录
                    vessel["data"]["liquid_volume"] = [volume]
            elif isinstance(current_volume, (int, float)):
                # 直接数值类型
                vessel["data"]["liquid_volume"] += volume
            else:
                debug_print(f"未知的体积数据格式: {type(current_volume)}")
                # 创建新的体积记录
                vessel["data"]["liquid_volume"] = volume
        else:
            # 确保vessel有data字段
            if "data" not in vessel:
                vessel["data"] = {}
            vessel["data"]["liquid_volume"] = volume
            
        # 🔧 同时更新图中的容器数据
        if vessel_id in G.nodes():
            vessel_node_data = G.nodes[vessel_id].get('data', {})
            current_node_volume = vessel_node_data.get('liquid_volume', 0.0)
            
            if isinstance(current_node_volume, list):
                if len(current_node_volume) > 0:
                    G.nodes[vessel_id]['data']['liquid_volume'][0] += volume
                else:
                    G.nodes[vessel_id]['data']['liquid_volume'] = [volume]
            else:
                G.nodes[vessel_id]['data']['liquid_volume'] = current_node_volume + volume

        action_sequence.append(create_action_log(f"容器体积已更新 (+{volume:.2f}mL)", "📊"))
        
    except Exception as e:
        debug_print(f"生成泵协议时出错: {str(e)}")
        action_sequence.append(create_action_log(f"泵协议生成失败: {str(e)}", "❌"))
        raise ValueError(f"生成泵协议时出错: {str(e)}")
    
    # 7. 混合搅拌
    if stir and stirrer_id:
        action_sequence.append(create_action_log(f"开始混合搅拌 {stir_time:.0f}s", "🌀"))
        
        action_sequence.append({
            "device_id": stirrer_id,
            "action_name": "stir",
            "action_kwargs": {
                "stir_time": stir_time,
                "stir_speed": stir_speed,
                "settling_time": settling_time,
                "purpose": f"pH调节: 混合试剂，目标pH={ph_value}"
            }
        })
    else:
        action_sequence.append(create_action_log("跳过混合搅拌", "⏭️"))

    # 8. 等待平衡
    action_sequence.append(create_action_log(f"等待pH平衡 {settling_time:.0f}s", "⚖️"))
    
    action_sequence.append({
        "action_name": "wait",
        "action_kwargs": {
            "time": settling_time,
            "description": f"等待pH平衡到目标值 {ph_value}"
        }
    })
    
    # 9. 完成总结
    total_time = addition_time + stir_time + settling_time
    debug_print(f"pH调节协议完成: {len(action_sequence)} 个动作, {total_time:.0f}s, {volume:.2f}mL {reagent} → {vessel_id} pH {ph_value}")
    
    # 添加完成日志
    summary_msg = f"pH调节协议完成: {vessel_id} → pH {ph_value} (使用 {volume:.2f}mL {reagent})"
    action_sequence.append(create_action_log(summary_msg, "🎉"))
    
    return action_sequence

def generate_adjust_ph_protocol_stepwise(
    G: nx.DiGraph,
    vessel: dict,  # 🔧 修改：从字符串改为字典类型
    ph_value: float,
    reagent: str,
    max_volume: float = 10.0,
    steps: int = 3
) -> List[Dict[str, Any]]:
    """
    分步调节pH的协议（更安全，避免过度调节）
    
    Args:
        G: 网络图
        vessel: 目标容器字典
        ph_value: 目标pH值
        reagent: 酸碱试剂
        max_volume: 最大试剂体积
        steps: 分步数量
    
    Returns:
        List[Dict[str, Any]]: 动作序列
    """
    # 🔧 核心修改：从字典中提取容器ID
    vessel_id = vessel["id"]

    debug_print(f"分步pH调节: vessel={vessel_id}, ph={ph_value}, reagent={reagent}, max_volume={max_volume}mL, steps={steps}")
    
    action_sequence = []
    
    # 每步添加的体积
    step_volume = max_volume / steps
    
    action_sequence.append(create_action_log(f"开始分步pH调节 ({steps}步)", "🔄"))
    action_sequence.append(create_action_log(f"每步添加: {step_volume:.2f}mL", "📏"))
    
    for i in range(steps):
        action_sequence.append(create_action_log(f"第 {i+1}/{steps} 步开始", "🚀"))
        
        # 生成单步协议
        step_actions = generate_adjust_ph_protocol(
            G=G,
            vessel=vessel,  # 🔧 直接传递vessel字典
            ph_value=ph_value,
            reagent=reagent,
            volume=step_volume,
            stir=True,
            stir_speed=300.0,
            stir_time=30.0,
            settling_time=20.0
        )
        
        action_sequence.extend(step_actions)
        action_sequence.append(create_action_log(f"第 {i+1}/{steps} 步完成", "✅"))
        
        # 步骤间等待
        if i < steps - 1:
            action_sequence.append(create_action_log("步骤间等待...", "⏳"))
            action_sequence.append({
                "action_name": "wait",
                "action_kwargs": {
                    "time": 30,
                    "description": f"pH调节第{i+1}步完成，等待下一步"
                }
            })
    
    debug_print(f"分步pH调节完成: {len(action_sequence)} 个动作")
    action_sequence.append(create_action_log("分步pH调节全部完成", "🎉"))
    
    return action_sequence

# 便捷函数：常用pH调节
def generate_acidify_protocol(
    G: nx.DiGraph,
    vessel: dict,  # 🔧 修改：从字符串改为字典类型
    target_ph: float = 2.0,
    acid: str = "hydrochloric acid"
) -> List[Dict[str, Any]]:
    """酸化协议"""
    vessel_id = vessel["id"]
    debug_print(f"酸化协议: {vessel_id} → pH {target_ph} ({acid})")
    return generate_adjust_ph_protocol(
        G, vessel, target_ph, acid
    )

def generate_basify_protocol(
    G: nx.DiGraph,
    vessel: dict,  # 🔧 修改：从字符串改为字典类型
    target_ph: float = 12.0,
    base: str = "sodium hydroxide"
) -> List[Dict[str, Any]]:
    """碱化协议"""
    vessel_id = vessel["id"]
    debug_print(f"碱化协议: {vessel_id} → pH {target_ph} ({base})")
    return generate_adjust_ph_protocol(
        G, vessel, target_ph, base
    )

def generate_neutralize_protocol(
    G: nx.DiGraph,
    vessel: dict,  # 🔧 修改：从字符串改为字典类型
    reagent: str = "sodium hydroxide"
) -> List[Dict[str, Any]]:
    """中和协议（pH=7）"""
    vessel_id = vessel["id"]
    debug_print(f"中和协议: {vessel_id} → pH 7.0 ({reagent})")
    return generate_adjust_ph_protocol(
        G, vessel, 7.0, reagent
    )

# 测试函数
def test_adjust_ph_protocol():
    """测试pH调节协议"""
    # 测试体积计算
    test_cases = [
        (2.0, "hydrochloric acid", 100.0),
        (4.0, "hydrochloric acid", 100.0),
        (12.0, "sodium hydroxide", 100.0),
        (10.0, "sodium hydroxide", 100.0),
        (7.0, "unknown reagent", 100.0)
    ]

    for ph, reagent, volume in test_cases:
        result = calculate_reagent_volume(ph, reagent, volume)
        debug_print(f"{reagent} → pH {ph}: {result:.2f}mL")

    debug_print("测试完成")

if __name__ == "__main__":
    test_adjust_ph_protocol()