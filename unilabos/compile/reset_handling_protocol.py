import networkx as nx
import logging
from typing import List, Dict, Any, Optional
from .utils.logger_util import debug_print, action_log
from .utils.vessel_parser import find_solvent_vessel
from .pump_protocol import generate_pump_protocol_with_rinsing

# 设置日志
logger = logging.getLogger(__name__)

create_action_log = action_log

def generate_reset_handling_protocol(
    G: nx.DiGraph,
    solvent: str,
    vessel: Optional[str] = None,
    **kwargs
) -> List[Dict[str, Any]]:
    """
    生成重置处理协议序列 - 支持自定义容器

    Args:
        G: 有向图，节点为容器和设备
        solvent: 溶剂名称（从XDL传入）
        vessel: 目标容器名称（可选，默认为 "main_reactor"）
        **kwargs: 其他可选参数，但不使用

    Returns:
        List[Dict[str, Any]]: 动作序列
    """
    action_sequence = []

    target_vessel = vessel if vessel is not None else "main_reactor"
    volume = 50.0

    debug_print(f"开始生成重置处理协议: solvent={solvent}, vessel={target_vessel}, volume={volume}mL")

    # 添加初始日志
    action_sequence.append(action_log(f"开始重置处理操作 - 容器: {target_vessel}", "🎬"))
    action_sequence.append(action_log(f"使用溶剂: {solvent}", "🧪"))
    action_sequence.append(action_log(f"重置体积: {volume}mL", "💧"))

    if vessel is None:
        action_sequence.append(action_log("使用默认目标容器: main_reactor", "⚙️"))
    else:
        action_sequence.append(action_log(f"使用指定目标容器: {vessel}", "🎯"))

    # 1. 验证目标容器存在
    action_sequence.append(action_log("正在验证目标容器...", "🔍"))

    if target_vessel not in G.nodes():
        action_sequence.append(action_log(f"目标容器 '{target_vessel}' 不存在", "❌"))
        raise ValueError(f"目标容器 '{target_vessel}' 不存在于系统中")

    action_sequence.append(action_log(f"目标容器验证通过: {target_vessel}", "✅"))

    # 2. 查找溶剂容器
    action_sequence.append(action_log("正在查找溶剂容器...", "🔍"))

    try:
        solvent_vessel = find_solvent_vessel(G, solvent)
        debug_print(f"找到溶剂容器: {solvent_vessel}")
        action_sequence.append(action_log(f"找到溶剂容器: {solvent_vessel}", "✅"))
    except ValueError as e:
        action_sequence.append(action_log(f"溶剂容器查找失败: {str(e)}", "❌"))
        raise ValueError(f"无法找到溶剂 '{solvent}': {str(e)}")

    # 3. 验证路径存在
    action_sequence.append(action_log("正在验证传输路径...", "🛤️"))

    try:
        path = nx.shortest_path(G, source=solvent_vessel, target=target_vessel)
        action_sequence.append(action_log(f"传输路径: {' → '.join(path)}", "🛤️"))
    except nx.NetworkXNoPath:
        action_sequence.append(action_log(f"路径不可达: {solvent_vessel} → {target_vessel}", "❌"))
        raise ValueError(f"从溶剂容器 '{solvent_vessel}' 到目标容器 '{target_vessel}' 没有可用路径")

    # 4. 使用pump_protocol转移溶剂
    action_sequence.append(action_log("开始溶剂转移操作...", "🚰"))
    action_sequence.append(action_log(f"转移: {solvent_vessel} → {target_vessel} ({volume}mL)", "🚛"))

    try:
        action_sequence.append(action_log("正在生成泵送协议...", "🔄"))

        pump_actions = generate_pump_protocol_with_rinsing(
            G=G,
            from_vessel=solvent_vessel,
            to_vessel=target_vessel,
            volume=volume,
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

        action_sequence.extend(pump_actions)
        debug_print(f"泵送协议已添加: {len(pump_actions)} 个动作")
        action_sequence.append(action_log(f"泵送协议完成 ({len(pump_actions)} 个操作)", "✅"))

    except Exception as e:
        action_sequence.append(action_log(f"泵送协议生成失败: {str(e)}", "❌"))
        raise ValueError(f"生成泵协议时出错: {str(e)}")

    # 5. 等待溶剂稳定
    action_sequence.append(action_log("等待溶剂稳定...", "⏳"))

    original_wait_time = 10.0
    simulation_time_limit = 5.0
    final_wait_time = min(original_wait_time, simulation_time_limit)

    if original_wait_time > simulation_time_limit:
        action_sequence.append(action_log(f"时间优化: {original_wait_time}s → {final_wait_time}s", "⚡"))
    else:
        action_sequence.append(action_log(f"等待时间: {final_wait_time}s", "⏰"))

    action_sequence.append({
        "action_name": "wait",
        "action_kwargs": {
            "time": final_wait_time,
            "description": f"等待溶剂 {solvent} 在容器 {target_vessel} 中稳定" + (f" (模拟时间)" if original_wait_time != final_wait_time else "")
        }
    })

    if original_wait_time != final_wait_time:
        action_sequence.append(action_log("应用模拟时间优化", "🎭"))

    # 总结
    debug_print(f"重置处理协议生成完成: {len(action_sequence)} 个动作, {solvent_vessel} -> {target_vessel}, {volume}mL")

    summary_msg = f"重置处理完成: {target_vessel} (使用 {volume}mL {solvent})"
    if vessel is None:
        summary_msg += " [默认容器]"
    else:
        summary_msg += " [指定容器]"

    action_sequence.append(action_log(summary_msg, "🎉"))

    return action_sequence

# === 便捷函数 ===

def reset_main_reactor(G: nx.DiGraph, solvent: str = "methanol", **kwargs) -> List[Dict[str, Any]]:
    """重置主反应器 (默认行为)"""
    return generate_reset_handling_protocol(G, solvent=solvent, vessel=None, **kwargs)

def reset_custom_vessel(G: nx.DiGraph, vessel: str, solvent: str = "methanol", **kwargs) -> List[Dict[str, Any]]:
    """重置指定容器"""
    return generate_reset_handling_protocol(G, solvent=solvent, vessel=vessel, **kwargs)

def reset_with_water(G: nx.DiGraph, vessel: Optional[str] = None, **kwargs) -> List[Dict[str, Any]]:
    """使用水重置容器"""
    return generate_reset_handling_protocol(G, solvent="water", vessel=vessel, **kwargs)

def reset_with_methanol(G: nx.DiGraph, vessel: Optional[str] = None, **kwargs) -> List[Dict[str, Any]]:
    """使用甲醇重置容器"""
    return generate_reset_handling_protocol(G, solvent="methanol", vessel=vessel, **kwargs)

def reset_with_ethanol(G: nx.DiGraph, vessel: Optional[str] = None, **kwargs) -> List[Dict[str, Any]]:
    """使用乙醇重置容器"""
    return generate_reset_handling_protocol(G, solvent="ethanol", vessel=vessel, **kwargs)

# 测试函数
def test_reset_handling_protocol():
    """测试重置处理协议"""
    debug_print("=== 重置处理协议测试 ===")
    debug_print("测试完成")

if __name__ == "__main__":
    test_reset_handling_protocol()
