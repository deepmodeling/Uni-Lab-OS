import networkx as nx
from typing import List, Dict, Any, Union
from .utils.vessel_parser import get_vessel, find_solvent_vessel, find_connected_stirrer
from .utils.resource_helper import get_resource_liquid_volume, update_vessel_volume
from .utils.logger_util import debug_print, action_log
from .utils.unit_parser import parse_volume_input
from .pump_protocol import generate_pump_protocol_with_rinsing


def generate_separate_protocol(
    G: nx.DiGraph,
    # 🔧 基础参数，支持XDL的vessel参数
    vessel: dict = None,  # 🔧 修改：从字符串改为字典类型
    purpose: str = "separate",  # 分离目的
    product_phase: str = "top",  # 产物相
    # 🔧 可选的详细参数
    from_vessel: Union[str, dict] = "",  # 源容器（通常在separate前已经transfer了）
    separation_vessel: Union[str, dict] = "",  # 分离容器（与vessel同义）
    to_vessel: Union[str, dict] = "",  # 目标容器（可选）
    waste_phase_to_vessel: Union[str, dict] = "",  # 废相目标容器
    product_vessel: Union[str, dict] = "",  # XDL: 产物容器（与to_vessel同义）
    waste_vessel: Union[str, dict] = "",  # XDL: 废液容器（与waste_phase_to_vessel同义）
    # 🔧 溶剂相关参数
    solvent: str = "",  # 溶剂名称
    solvent_volume: Union[str, float] = 0.0,  # 溶剂体积
    volume: Union[str, float] = 0.0,  # XDL: 体积（与solvent_volume同义）
    # 🔧 操作参数
    through: str = "",  # 通过材料
    repeats: int = 1,  # 重复次数
    stir_time: float = 30.0,  # 搅拌时间（秒）
    stir_speed: float = 300.0,  # 搅拌速度
    settling_time: float = 300.0,  # 沉降时间（秒）
    **kwargs
) -> List[Dict[str, Any]]:
    """
    生成分离操作的协议序列 - 支持vessel字典和体积运算

    支持XDL参数格式：
    - vessel: 分离容器字典（必需）
    - purpose: "wash", "extract", "separate"
    - product_phase: "top", "bottom"
    - product_vessel: 产物收集容器
    - waste_vessel: 废液收集容器
    - solvent: 溶剂名称
    - volume: "200 mL", "?" 或数值
    - repeats: 重复次数

    分离流程：
    1. （可选）添加溶剂到分离容器
    2. 搅拌混合
    3. 静置分层
    4. 收集指定相到目标容器
    5. 重复指定次数
    """

    # 🔧 核心修改：vessel参数兼容处理
    if vessel is None:
        if isinstance(separation_vessel, dict):
            vessel = separation_vessel
        else:
            raise ValueError("必须提供vessel字典参数")

    # 🔧 核心修改：从字典中提取容器ID
    vessel_id, vessel_data = get_vessel(vessel)

    debug_print(f"开始生成分离协议: vessel={vessel_id}, purpose={purpose}, "
                f"product_phase={product_phase}, solvent={solvent}, "
                f"volume={volume}, repeats={repeats}")

    action_sequence = []

    # 记录分离前的容器状态
    original_liquid_volume = get_resource_liquid_volume(vessel)
    debug_print(f"分离前液体体积: {original_liquid_volume:.2f}mL")

    # === 参数验证和标准化 ===
    action_sequence.append(action_log(f"开始分离操作 - 容器: {vessel_id}", "🎬", prefix="[SEPARATE]"))
    action_sequence.append(action_log(f"分离目的: {purpose}", "🧪", prefix="[SEPARATE]"))
    action_sequence.append(action_log(f"产物相: {product_phase}", "📊", prefix="[SEPARATE]"))

    # 统一容器参数 - 支持字典和字符串
    final_vessel_id = vessel_id

    to_vessel_result = get_vessel(to_vessel) if to_vessel else None
    if to_vessel_result is None or to_vessel_result[0] == "":
        to_vessel_result = get_vessel(product_vessel) if product_vessel else None
    final_to_vessel_id = to_vessel_result[0] if to_vessel_result else ""

    waste_vessel_result = get_vessel(waste_phase_to_vessel) if waste_phase_to_vessel else None
    if waste_vessel_result is None or waste_vessel_result[0] == "":
        waste_vessel_result = get_vessel(waste_vessel) if waste_vessel else None
    final_waste_vessel_id = waste_vessel_result[0] if waste_vessel_result else ""

    # 统一体积参数
    final_volume = parse_volume_input(volume or solvent_volume)

    # 🔧 修复：确保repeats至少为1
    if repeats <= 0:
        repeats = 1
        debug_print(f"⚠️ 重复次数参数 <= 0，自动设置为 1")

    debug_print(f"标准化参数: vessel={final_vessel_id}, to={final_to_vessel_id}, "
                f"waste={final_waste_vessel_id}, volume={final_volume}mL, repeats={repeats}")

    action_sequence.append(action_log(f"分离容器: {final_vessel_id}", "🧪", prefix="[SEPARATE]"))
    action_sequence.append(action_log(f"溶剂体积: {final_volume}mL", "📏", prefix="[SEPARATE]"))
    action_sequence.append(action_log(f"重复次数: {repeats}", "🔄", prefix="[SEPARATE]"))

    # 验证必需参数
    if not purpose:
        purpose = "separate"
    if not product_phase:
        product_phase = "top"
    if purpose not in ["wash", "extract", "separate"]:
        debug_print(f"⚠️ 未知的分离目的 '{purpose}'，使用默认值 'separate'")
        purpose = "separate"
        action_sequence.append(action_log(f"未知目的，使用: {purpose}", "⚠️", prefix="[SEPARATE]"))
    if product_phase not in ["top", "bottom"]:
        debug_print(f"⚠️ 未知的产物相 '{product_phase}'，使用默认值 'top'")
        product_phase = "top"
        action_sequence.append(action_log(f"未知相别，使用: {product_phase}", "⚠️", prefix="[SEPARATE]"))

    action_sequence.append(action_log("参数验证通过", "✅", prefix="[SEPARATE]"))

    # === 查找设备 ===
    action_sequence.append(action_log("正在查找相关设备...", "🔍", prefix="[SEPARATE]"))

    # 查找分离器设备
    separator_device = find_separator_device(G, final_vessel_id)
    if separator_device:
        action_sequence.append(action_log(f"找到分离器设备: {separator_device}", "🧪", prefix="[SEPARATE]"))
    else:
        debug_print("⚠️ 未找到分离器设备，可能无法执行分离")
        action_sequence.append(action_log("未找到分离器设备", "⚠️", prefix="[SEPARATE]"))

    # 查找搅拌器
    stirrer_device = find_connected_stirrer(G, final_vessel_id)
    if stirrer_device:
        action_sequence.append(action_log(f"找到搅拌器: {stirrer_device}", "🌪️", prefix="[SEPARATE]"))
    else:
        action_sequence.append(action_log("未找到搅拌器", "⚠️", prefix="[SEPARATE]"))

    # 查找溶剂容器（如果需要）
    solvent_vessel = ""
    if solvent and solvent.strip():
        try:
            solvent_vessel = find_solvent_vessel(G, solvent)
        except ValueError:
            solvent_vessel = ""
        if solvent_vessel:
            action_sequence.append(action_log(f"找到溶剂容器: {solvent_vessel}", "💧", prefix="[SEPARATE]"))
        else:
            action_sequence.append(action_log(f"未找到溶剂容器: {solvent}", "⚠️", prefix="[SEPARATE]"))

    debug_print(f"设备配置: separator={separator_device}, stirrer={stirrer_device}, solvent_vessel={solvent_vessel}")

    # === 执行分离流程 ===
    action_sequence.append(action_log("开始分离工作流程", "🎯", prefix="[SEPARATE]"))

    # 体积变化跟踪变量
    current_volume = original_liquid_volume

    try:
        for repeat_idx in range(repeats):
            cycle_num = repeat_idx + 1
            debug_print(f"分离循环 {cycle_num}/{repeats} 开始")
            action_sequence.append(action_log(f"分离循环 {cycle_num}/{repeats} 开始", "🔄", prefix="[SEPARATE]"))

            # 步骤3.1: 添加溶剂（如果需要）
            if solvent_vessel and final_volume > 0:
                action_sequence.append(action_log(f"向分离容器添加 {final_volume}mL {solvent}", "💧", prefix="[SEPARATE]"))

                try:
                    # 使用pump protocol添加溶剂
                    pump_actions = generate_pump_protocol_with_rinsing(
                        G=G,
                        from_vessel=solvent_vessel,
                        to_vessel=final_vessel_id,
                        volume=final_volume,
                        amount="",
                        time=0.0,
                        viscous=False,
                        rinsing_solvent="",
                        rinsing_volume=0.0,
                        rinsing_repeats=0,
                        solid=False,
                        flowrate=2.5,
                        transfer_flowrate=0.5,
                        rate_spec="",
                        event="",
                        through="",
                        **kwargs
                    )
                    action_sequence.extend(pump_actions)
                    action_sequence.append(action_log(f"溶剂转移完成 ({len(pump_actions)} 个操作)", "✅", prefix="[SEPARATE]"))

                    # 更新体积 - 添加溶剂后
                    current_volume += final_volume
                    update_vessel_volume(vessel, G, current_volume, f"添加{final_volume}mL {solvent}后")

                except Exception as e:
                    debug_print(f"❌ 溶剂添加失败: {str(e)}")
                    action_sequence.append(action_log(f"溶剂添加失败: {str(e)}", "❌", prefix="[SEPARATE]"))
            else:
                action_sequence.append(action_log("无需添加溶剂", "⏭️", prefix="[SEPARATE]"))

            # 步骤3.2: 启动搅拌（如果有搅拌器）
            if stirrer_device and stir_time > 0:
                action_sequence.append(action_log(f"开始搅拌: {stir_speed}rpm，持续 {stir_time}s", "🌪️", prefix="[SEPARATE]"))

                action_sequence.append({
                    "device_id": stirrer_device,
                    "action_name": "start_stir",
                    "action_kwargs": {
                        "vessel": {"id": final_vessel_id},
                        "stir_speed": stir_speed,
                        "purpose": f"分离混合 - {purpose}"
                    }
                })

                # 搅拌等待
                stir_minutes = stir_time / 60
                action_sequence.append(action_log(f"搅拌中，持续 {stir_minutes:.1f} 分钟", "⏱️", prefix="[SEPARATE]"))
                action_sequence.append({
                    "action_name": "wait",
                    "action_kwargs": {"time": stir_time}
                })

                # 停止搅拌
                action_sequence.append(action_log("停止搅拌器", "🛑", prefix="[SEPARATE]"))
                action_sequence.append({
                    "device_id": stirrer_device,
                    "action_name": "stop_stir",
                    "action_kwargs": {"vessel": final_vessel_id}
                })

            else:
                action_sequence.append(action_log("无需搅拌", "⏭️", prefix="[SEPARATE]"))

            # 步骤3.3: 静置分层
            if settling_time > 0:
                settling_minutes = settling_time / 60
                action_sequence.append(action_log(f"静置分层 ({settling_minutes:.1f} 分钟)", "⚖️", prefix="[SEPARATE]"))
                action_sequence.append({
                    "action_name": "wait",
                    "action_kwargs": {"time": settling_time}
                })
            else:
                action_sequence.append(action_log("未指定静置时间", "⏭️", prefix="[SEPARATE]"))

            # 步骤3.4: 执行分离操作
            if separator_device:
                action_sequence.append(action_log(f"执行分离: 收集{product_phase}相", "🧪", prefix="[SEPARATE]"))

                # 首先进行分液判断（电导突跃）
                action_sequence.append({
                    "device_id": separator_device,
                    "action_name": "valve_open",
                    "action_kwargs": {
                        "command": "delta > 0.05"
                    }
                })

                # 估算每相的体积（假设大致平分）
                phase_volume = current_volume / 2

                # 智能查找分离容器底部
                separation_vessel_bottom = find_separation_vessel_bottom(G, final_vessel_id)

                if product_phase == "bottom":
                    action_sequence.append(action_log("收集底相产物", "📦", prefix="[SEPARATE]"))

                    # 产物转移到目标瓶
                    if final_to_vessel_id:
                        pump_actions = generate_pump_protocol_with_rinsing(
                            G=G,
                            from_vessel=separation_vessel_bottom,
                            to_vessel=final_to_vessel_id,
                            volume=current_volume,
                            flowrate=2.5,
                            **kwargs
                        )
                        action_sequence.extend(pump_actions)

                    # 放出上面那一相，60秒后关阀门
                    action_sequence.append({
                        "device_id": separator_device,
                        "action_name": "valve_open",
                        "action_kwargs": {
                            "command": "time > 60"
                        }
                    })

                    # 弃去上面那一相进废液
                    if final_waste_vessel_id:
                        pump_actions = generate_pump_protocol_with_rinsing(
                            G=G,
                            from_vessel=separation_vessel_bottom,
                            to_vessel=final_waste_vessel_id,
                            volume=current_volume,
                            flowrate=2.5,
                            **kwargs
                        )
                        action_sequence.extend(pump_actions)

                elif product_phase == "top":
                    action_sequence.append(action_log("收集上相产物", "📦", prefix="[SEPARATE]"))

                    # 弃去下面那一相进废液
                    if final_waste_vessel_id:
                        pump_actions = generate_pump_protocol_with_rinsing(
                            G=G,
                            from_vessel=separation_vessel_bottom,
                            to_vessel=final_waste_vessel_id,
                            volume=phase_volume,
                            flowrate=2.5,
                            **kwargs
                        )
                        action_sequence.extend(pump_actions)

                    # 放出上面那一相，60秒后关阀门
                    action_sequence.append({
                        "device_id": separator_device,
                        "action_name": "valve_open",
                        "action_kwargs": {
                            "command": "time > 60"
                        }
                    })

                    # 产物转移到目标瓶
                    if final_to_vessel_id:
                        pump_actions = generate_pump_protocol_with_rinsing(
                            G=G,
                            from_vessel=separation_vessel_bottom,
                            to_vessel=final_to_vessel_id,
                            volume=phase_volume,
                            flowrate=2.5,
                            **kwargs
                        )
                        action_sequence.extend(pump_actions)

                action_sequence.append(action_log("分离操作完成", "✅", prefix="[SEPARATE]"))

                # 分离后体积估算
                separated_volume = phase_volume * 0.95  # 假设5%损失，只保留产物相体积
                update_vessel_volume(vessel, G, separated_volume, f"分离操作后（第{cycle_num}轮）")
                current_volume = separated_volume

                # 收集结果
                if final_to_vessel_id:
                    action_sequence.append(
                        action_log(f"产物 ({product_phase}相) 收集到: {final_to_vessel_id}", "📦", prefix="[SEPARATE]"))
                if final_waste_vessel_id:
                    action_sequence.append(action_log(f"废相收集到: {final_waste_vessel_id}", "🗑️", prefix="[SEPARATE]"))

            else:
                action_sequence.append(action_log("无分离器设备可用", "❌", prefix="[SEPARATE]"))
                # 添加等待时间模拟分离
                action_sequence.append({
                    "action_name": "wait",
                    "action_kwargs": {"time": 10.0}
                })

            # 如果不是最后一次，从中转瓶转移回分液漏斗
            if repeat_idx < repeats - 1 and final_to_vessel_id and final_to_vessel_id != final_vessel_id:
                action_sequence.append(action_log("产物转回分离容器，准备下一轮", "🔄", prefix="[SEPARATE]"))

                pump_actions = generate_pump_protocol_with_rinsing(
                    G=G,
                    from_vessel=final_to_vessel_id,
                    to_vessel=final_vessel_id,
                    volume=current_volume,
                    flowrate=2.5,
                    **kwargs
                )
                action_sequence.extend(pump_actions)

                # 更新体积回到分离容器
                update_vessel_volume(vessel, G, current_volume, f"产物转回分离容器（第{cycle_num}轮后）")

            # 循环间等待（除了最后一次）
            if repeat_idx < repeats - 1:
                action_sequence.append(action_log("等待下一次循环...", "⏳", prefix="[SEPARATE]"))
                action_sequence.append({
                    "action_name": "wait",
                    "action_kwargs": {"time": 5}
                })
            else:
                action_sequence.append(action_log(f"分离循环 {cycle_num}/{repeats} 完成", "🌟", prefix="[SEPARATE]"))

    except Exception as e:
        debug_print(f"❌ 分离工作流程执行失败: {str(e)}")
        action_sequence.append(action_log(f"分离工作流程失败: {str(e)}", "❌", prefix="[SEPARATE]"))

    # 分离完成后的最终状态报告
    final_liquid_volume = get_resource_liquid_volume(vessel)

    # === 最终结果 ===
    total_time = (stir_time + settling_time + 15) * repeats  # 估算总时间

    debug_print(f"分离协议生成完成: {len(action_sequence)} 个动作, "
                f"预计 {total_time:.0f}s, 体积 {original_liquid_volume:.2f}→{final_liquid_volume:.2f}mL")

    # 添加完成日志
    summary_msg = f"分离协议完成: {final_vessel_id} ({purpose}，{repeats} 次循环)"
    if solvent:
        summary_msg += f"，使用 {final_volume * repeats:.2f}mL {solvent}"
    action_sequence.append(action_log(summary_msg, "🎉", prefix="[SEPARATE]"))

    return action_sequence


def find_separator_device(G: nx.DiGraph, vessel: str) -> str:
    """查找分离器设备，支持多种查找方式"""
    # 方法1：查找连接到容器的分离器设备
    separator_nodes = []
    for node in G.nodes():
        node_class = G.nodes[node].get('class', '').lower()
        if 'separator' in node_class:
            separator_nodes.append(node)
            # 检查是否连接到目标容器
            if G.has_edge(node, vessel) or G.has_edge(vessel, node):
                return node

    # 方法2：根据命名规则查找
    possible_names = [
        f"{vessel}_controller",
        f"{vessel}_separator",
        vessel,  # 容器本身可能就是分离器
        "separator_1",
        "virtual_separator",
        "liquid_handler_1",
        "controller_1"
    ]

    for name in possible_names:
        if name in G.nodes():
            node_class = G.nodes[name].get('class', '').lower()
            if 'separator' in node_class or 'controller' in node_class:
                return name

    # 方法3：使用第一个可用分离器
    if separator_nodes:
        debug_print(f"⚠️ 使用第一个分离器设备: {separator_nodes[0]}")
        return separator_nodes[0]

    debug_print(f"❌ 未找到分离器设备")
    return ""


def find_separation_vessel_bottom(G: nx.DiGraph, vessel_id: str) -> str:
    """
    智能查找分离容器的底部容器（假设为flask或vessel类型）

    Args:
        G: 网络图
        vessel_id: 分离容器ID

    Returns:
        str: 底部容器ID
    """
    # 方法1：根据命名规则推测
    possible_bottoms = [
        f"{vessel_id}_bottom",
        f"flask_{vessel_id}",
        f"vessel_{vessel_id}",
        f"{vessel_id}_flask",
        f"{vessel_id}_vessel"
    ]

    for bottom_id in possible_bottoms:
        if bottom_id in G.nodes():
            node_type = G.nodes[bottom_id].get('type', '')
            if node_type == 'container':
                return bottom_id

    # 方法2：查找与分离器相连的容器
    for node in G.nodes():
        node_data = G.nodes[node]
        node_class = node_data.get('class', '') or ''

        if 'separator' in node_class.lower():
            if G.has_edge(node, vessel_id):
                for neighbor in G.neighbors(node):
                    if neighbor != vessel_id:
                        neighbor_type = G.nodes[neighbor].get('type', '')
                        if neighbor_type == 'container':
                            return neighbor

    debug_print(f"❌ 无法找到分离容器 {vessel_id} 的底部容器")
    return ""
