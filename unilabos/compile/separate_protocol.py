from functools import partial

import networkx as nx
import re
import logging
import sys
from typing import List, Dict, Any, Union
from .utils.vessel_parser import get_vessel
from .utils.logger_util import action_log
from .pump_protocol import generate_pump_protocol_with_rinsing

logger = logging.getLogger(__name__)

# 确保输出编码为UTF-8
if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except:
        pass


def debug_print(message):
    """调试输出函数 - 支持中文"""
    try:
        # 确保消息是字符串格式
        safe_message = str(message)
        logger.info(f"[SEPARATE] {safe_message}")
    except UnicodeEncodeError:
        # 如果编码失败，尝试替换不支持的字符
        safe_message = str(message).encode('utf-8', errors='replace').decode('utf-8')
        logger.info(f"[SEPARATE] {safe_message}")
    except Exception as e:
        # 最后的安全措施
        fallback_message = f"日志输出错误: {repr(message)}"
        logger.info(f"[SEPARATE] {fallback_message}")


create_action_log = partial(action_log, prefix="[SEPARATE]")


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

    debug_print("🌀" * 20)
    debug_print("🚀 开始生成分离协议（支持vessel字典和体积运算）✨")
    debug_print(f"📝 输入参数:")
    debug_print(f"  🥽 vessel: {vessel} (ID: {vessel_id})")
    debug_print(f"  🎯 分离目的: '{purpose}'")
    debug_print(f"  📊 产物相: '{product_phase}'")
    debug_print(f"  💧 溶剂: '{solvent}'")
    debug_print(f"  📏 体积: {volume} (类型: {type(volume)})")
    debug_print(f"  🔄 重复次数: {repeats}")
    debug_print(f"  🎯 产物容器: '{product_vessel}'")
    debug_print(f"  🗑️ 废液容器: '{waste_vessel}'")
    debug_print(f"  📦 其他参数: {kwargs}")
    debug_print("🌀" * 20)

    action_sequence = []

    # 🔧 新增：记录分离前的容器状态
    debug_print("🔍 记录分离前容器状态...")
    original_liquid_volume = get_vessel_liquid_volume(vessel)
    debug_print(f"📊 分离前液体体积: {original_liquid_volume:.2f}mL")

    # === 参数验证和标准化 ===
    debug_print("🔍 步骤1: 参数验证和标准化...")
    action_sequence.append(create_action_log(f"开始分离操作 - 容器: {vessel_id}", "🎬"))
    action_sequence.append(create_action_log(f"分离目的: {purpose}", "🧪"))
    action_sequence.append(create_action_log(f"产物相: {product_phase}", "📊"))

    # 统一容器参数 - 支持字典和字符串
    def extract_vessel_id(vessel_param):
        if isinstance(vessel_param, dict):
            return vessel_param.get("id", "")
        elif isinstance(vessel_param, str):
            return vessel_param
        else:
            return ""

    final_vessel_id, _ = vessel_id
    final_to_vessel_id, _ = get_vessel(to_vessel) or get_vessel(product_vessel)
    final_waste_vessel_id, _ = get_vessel(waste_phase_to_vessel) or get_vessel(waste_vessel)

    # 统一体积参数
    final_volume = parse_volume_input(volume or solvent_volume)

    # 🔧 修复：确保repeats至少为1
    if repeats <= 0:
        repeats = 1
        debug_print(f"⚠️ 重复次数参数 <= 0，自动设置为 1")

    debug_print(f"🔧 标准化后的参数:")
    debug_print(f"  🥼 分离容器: '{final_vessel_id}'")
    debug_print(f"  🎯 产物容器: '{final_to_vessel_id}'")
    debug_print(f"  🗑️ 废液容器: '{final_waste_vessel_id}'")
    debug_print(f"  📏 溶剂体积: {final_volume}mL")
    debug_print(f"  🔄 重复次数: {repeats}")

    action_sequence.append(create_action_log(f"分离容器: {final_vessel_id}", "🧪"))
    action_sequence.append(create_action_log(f"溶剂体积: {final_volume}mL", "📏"))
    action_sequence.append(create_action_log(f"重复次数: {repeats}", "🔄"))

    # 验证必需参数
    if not purpose:
        purpose = "separate"
    if not product_phase:
        product_phase = "top"
    if purpose not in ["wash", "extract", "separate"]:
        debug_print(f"⚠️ 未知的分离目的 '{purpose}'，使用默认值 'separate'")
        purpose = "separate"
        action_sequence.append(create_action_log(f"未知目的，使用: {purpose}", "⚠️"))
    if product_phase not in ["top", "bottom"]:
        debug_print(f"⚠️ 未知的产物相 '{product_phase}'，使用默认值 'top'")
        product_phase = "top"
        action_sequence.append(create_action_log(f"未知相别，使用: {product_phase}", "⚠️"))

    debug_print("✅ 参数验证通过")
    action_sequence.append(create_action_log("参数验证通过", "✅"))

    # === 查找设备 ===
    debug_print("🔍 步骤2: 查找设备...")
    action_sequence.append(create_action_log("正在查找相关设备...", "🔍"))

    # 查找分离器设备
    separator_device = find_separator_device(G, final_vessel_id)  # 🔧 使用 final_vessel_id
    if separator_device:
        action_sequence.append(create_action_log(f"找到分离器设备: {separator_device}", "🧪"))
    else:
        debug_print("⚠️ 未找到分离器设备，可能无法执行分离")
        action_sequence.append(create_action_log("未找到分离器设备", "⚠️"))

    # 查找搅拌器
    stirrer_device = find_connected_stirrer(G, final_vessel_id)  # 🔧 使用 final_vessel_id
    if stirrer_device:
        action_sequence.append(create_action_log(f"找到搅拌器: {stirrer_device}", "🌪️"))
    else:
        action_sequence.append(create_action_log("未找到搅拌器", "⚠️"))

    # 查找溶剂容器（如果需要）
    solvent_vessel = ""
    if solvent and solvent.strip():
        solvent_vessel = find_solvent_vessel(G, solvent)
        if solvent_vessel:
            action_sequence.append(create_action_log(f"找到溶剂容器: {solvent_vessel}", "💧"))
        else:
            action_sequence.append(create_action_log(f"未找到溶剂容器: {solvent}", "⚠️"))

    debug_print(f"📊 设备配置:")
    debug_print(f"  🧪 分离器设备: '{separator_device}'")
    debug_print(f"  🌪️ 搅拌器设备: '{stirrer_device}'")
    debug_print(f"  💧 溶剂容器: '{solvent_vessel}'")

    # === 执行分离流程 ===
    debug_print("🔍 步骤3: 执行分离流程...")
    action_sequence.append(create_action_log("开始分离工作流程", "🎯"))

    # 🔧 新增：体积变化跟踪变量
    current_volume = original_liquid_volume

    try:
        for repeat_idx in range(repeats):
            cycle_num = repeat_idx + 1
            debug_print(f"🔄 第{cycle_num}轮: 开始分离循环 {cycle_num}/{repeats}")
            action_sequence.append(create_action_log(f"分离循环 {cycle_num}/{repeats} 开始", "🔄"))

            # 步骤3.1: 添加溶剂（如果需要）
            if solvent_vessel and final_volume > 0:
                debug_print(f"🔄 第{cycle_num}轮 步骤1: 添加溶剂 {solvent} ({final_volume}mL)")
                action_sequence.append(create_action_log(f"向分离容器添加 {final_volume}mL {solvent}", "💧"))

                try:
                    # 使用pump protocol添加溶剂
                    pump_actions = generate_pump_protocol_with_rinsing(
                        G=G,
                        from_vessel=solvent_vessel,
                        to_vessel=final_vessel_id,  # 🔧 使用 final_vessel_id
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
                    debug_print(f"✅ 溶剂添加完成，添加了 {len(pump_actions)} 个动作")
                    action_sequence.append(create_action_log(f"溶剂转移完成 ({len(pump_actions)} 个操作)", "✅"))

                    # 🔧 新增：更新体积 - 添加溶剂后
                    current_volume += final_volume
                    update_vessel_volume(vessel, G, current_volume, f"添加{final_volume}mL {solvent}后")

                except Exception as e:
                    debug_print(f"❌ 溶剂添加失败: {str(e)}")
                    action_sequence.append(create_action_log(f"溶剂添加失败: {str(e)}", "❌"))
            else:
                debug_print(f"🔄 第{cycle_num}轮 步骤1: 无需添加溶剂")
                action_sequence.append(create_action_log("无需添加溶剂", "⏭️"))

            # 步骤3.2: 启动搅拌（如果有搅拌器）
            if stirrer_device and stir_time > 0:
                debug_print(f"🔄 第{cycle_num}轮 步骤2: 开始搅拌 ({stir_speed}rpm，持续 {stir_time}s)")
                action_sequence.append(create_action_log(f"开始搅拌: {stir_speed}rpm，持续 {stir_time}s", "🌪️"))

                action_sequence.append({
                    "device_id": stirrer_device,
                    "action_name": "start_stir",
                    "action_kwargs": {
                        "vessel": {"id": final_vessel_id},  # 🔧 使用 final_vessel_id
                        "stir_speed": stir_speed,
                        "purpose": f"分离混合 - {purpose}"
                    }
                })

                # 搅拌等待
                stir_minutes = stir_time / 60
                action_sequence.append(create_action_log(f"搅拌中，持续 {stir_minutes:.1f} 分钟", "⏱️"))
                action_sequence.append({
                    "action_name": "wait",
                    "action_kwargs": {"time": stir_time}
                })

                # 停止搅拌
                action_sequence.append(create_action_log("停止搅拌器", "🛑"))
                action_sequence.append({
                    "device_id": stirrer_device,
                    "action_name": "stop_stir",
                    "action_kwargs": {"vessel": final_vessel_id}  # 🔧 使用 final_vessel_id
                })

            else:
                debug_print(f"🔄 第{cycle_num}轮 步骤2: 无需搅拌")
                action_sequence.append(create_action_log("无需搅拌", "⏭️"))

            # 步骤3.3: 静置分层
            if settling_time > 0:
                debug_print(f"🔄 第{cycle_num}轮 步骤3: 静置分层 ({settling_time}s)")
                settling_minutes = settling_time / 60
                action_sequence.append(create_action_log(f"静置分层 ({settling_minutes:.1f} 分钟)", "⚖️"))
                action_sequence.append({
                    "action_name": "wait",
                    "action_kwargs": {"time": settling_time}
                })
            else:
                debug_print(f"🔄 第{cycle_num}轮 步骤3: 未指定静置时间")
                action_sequence.append(create_action_log("未指定静置时间", "⏭️"))

            # 步骤3.4: 执行分离操作
            if separator_device:
                debug_print(f"🔄 第{cycle_num}轮 步骤4: 执行分离操作")
                action_sequence.append(create_action_log(f"执行分离: 收集{product_phase}相", "🧪"))

                # 🔧 替换为具体的分离操作逻辑（基于old版本）

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
                separation_vessel_bottom = find_separation_vessel_bottom(G, final_vessel_id)  # ✅

                if product_phase == "bottom":
                    debug_print(f"🔄 收集底相产物到 {final_to_vessel_id}")
                    action_sequence.append(create_action_log("收集底相产物", "📦"))

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
                    debug_print(f"🔄 收集上相产物到 {final_to_vessel_id}")
                    action_sequence.append(create_action_log("收集上相产物", "📦"))

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

                debug_print(f"✅ 分离操作已完成")
                action_sequence.append(create_action_log("分离操作完成", "✅"))

                # 🔧 新增：分离后体积估算
                separated_volume = phase_volume * 0.95  # 假设5%损失，只保留产物相体积
                update_vessel_volume(vessel, G, separated_volume, f"分离操作后（第{cycle_num}轮）")
                current_volume = separated_volume

                # 收集结果
                if final_to_vessel_id:
                    action_sequence.append(
                        create_action_log(f"产物 ({product_phase}相) 收集到: {final_to_vessel_id}", "📦"))
                if final_waste_vessel_id:
                    action_sequence.append(create_action_log(f"废相收集到: {final_waste_vessel_id}", "🗑️"))

            else:
                debug_print(f"🔄 第{cycle_num}轮 步骤4: 无分离器设备，跳过分离")
                action_sequence.append(create_action_log("无分离器设备可用", "❌"))
                # 添加等待时间模拟分离
                action_sequence.append({
                    "action_name": "wait",
                    "action_kwargs": {"time": 10.0}
                })

            # 🔧 新增：如果不是最后一次，从中转瓶转移回分液漏斗（基于old版本逻辑）
            if repeat_idx < repeats - 1 and final_to_vessel_id and final_to_vessel_id != final_vessel_id:
                debug_print(f"🔄 第{cycle_num}轮: 产物转移回分离容器准备下一轮")
                action_sequence.append(create_action_log("产物转回分离容器，准备下一轮", "🔄"))

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
                debug_print(f"🔄 第{cycle_num}轮: 等待下一次循环...")
                action_sequence.append(create_action_log("等待下一次循环...", "⏳"))
                action_sequence.append({
                    "action_name": "wait",
                    "action_kwargs": {"time": 5}
                })
            else:
                action_sequence.append(create_action_log(f"分离循环 {cycle_num}/{repeats} 完成", "🌟"))

    except Exception as e:
        debug_print(f"❌ 分离工作流程执行失败: {str(e)}")
        action_sequence.append(create_action_log(f"分离工作流程失败: {str(e)}", "❌"))

    # 🔧 新增：分离完成后的最终状态报告
    final_liquid_volume = get_vessel_liquid_volume(vessel)

    # === 最终结果 ===
    total_time = (stir_time + settling_time + 15) * repeats  # 估算总时间

    debug_print("🌀" * 20)
    debug_print(f"🎉 分离协议生成完成")
    debug_print(f"📊 协议统计:")
    debug_print(f"  📋 总动作数: {len(action_sequence)}")
    debug_print(f"  ⏱️ 预计总时间: {total_time:.0f}s ({total_time / 60:.1f} 分钟)")
    debug_print(f"  🥼 分离容器: {final_vessel_id}")
    debug_print(f"  🎯 分离目的: {purpose}")
    debug_print(f"  📊 产物相: {product_phase}")
    debug_print(f"  🔄 重复次数: {repeats}")
    debug_print(f"💧 体积变化统计:")
    debug_print(f"  - 分离前体积: {original_liquid_volume:.2f}mL")
    debug_print(f"  - 分离后体积: {final_liquid_volume:.2f}mL")
    if solvent:
        debug_print(f"  💧 溶剂: {solvent} ({final_volume}mL × {repeats}轮 = {final_volume * repeats:.2f}mL)")
    if final_to_vessel_id:
        debug_print(f"  🎯 产物容器: {final_to_vessel_id}")
    if final_waste_vessel_id:
        debug_print(f"  🗑️ 废液容器: {final_waste_vessel_id}")
    debug_print("🌀" * 20)

    # 添加完成日志
    summary_msg = f"分离协议完成: {final_vessel_id} ({purpose}，{repeats} 次循环)"
    if solvent:
        summary_msg += f"，使用 {final_volume * repeats:.2f}mL {solvent}"
    action_sequence.append(create_action_log(summary_msg, "🎉"))

    return action_sequence


def parse_volume_input(volume_input: Union[str, float]) -> float:
    """
    解析体积输入，支持带单位的字符串

    Args:
        volume_input: 体积输入（如 "200 mL", "?", 50.0）

    Returns:
        float: 体积（毫升）
    """
    if isinstance(volume_input, (int, float)):
        debug_print(f"📏 体积输入为数值: {volume_input}")
        return float(volume_input)

    if not volume_input or not str(volume_input).strip():
        debug_print(f"⚠️ 体积输入为空，返回 0.0mL")
        return 0.0

    volume_str = str(volume_input).lower().strip()
    debug_print(f"🔍 解析体积输入: '{volume_str}'")

    # 处理未知体积
    if volume_str in ['?', 'unknown', 'tbd', 'to be determined', '未知', '待定']:
        default_volume = 100.0  # 默认100mL
        debug_print(f"❓ 检测到未知体积，使用默认值: {default_volume}mL")
        return default_volume

    # 移除空格并提取数字和单位
    volume_clean = re.sub(r'\s+', '', volume_str)

    # 匹配数字和单位的正则表达式
    match = re.match(r'([0-9]*\.?[0-9]+)\s*(ml|l|μl|ul|microliter|milliliter|liter|毫升|升|微升)?', volume_clean)

    if not match:
        debug_print(f"⚠️ 无法解析体积: '{volume_str}'，使用默认值 100mL")
        return 100.0

    value = float(match.group(1))
    unit = match.group(2) or 'ml'  # 默认单位为毫升

    # 转换为毫升
    if unit in ['l', 'liter', '升']:
        volume = value * 1000.0  # L -> mL
        debug_print(f"🔄 体积转换: {value}L -> {volume}mL")
    elif unit in ['μl', 'ul', 'microliter', '微升']:
        volume = value / 1000.0  # μL -> mL
        debug_print(f"🔄 体积转换: {value}μL -> {volume}mL")
    else:  # ml, milliliter, 毫升 或默认
        volume = value  # 已经是mL
        debug_print(f"✅ 体积已为毫升单位: {volume}mL")

    return volume


def find_solvent_vessel(G: nx.DiGraph, solvent: str) -> str:
    """查找溶剂容器，支持多种匹配模式"""
    if not solvent or not solvent.strip():
        debug_print("⏭️ 未指定溶剂，跳过溶剂容器查找")
        return ""

    debug_print(f"🔍 正在查找溶剂 '{solvent}' 的容器...")

    # 🔧 方法1：直接搜索 data.reagent_name 和 config.reagent
    debug_print(f"📋 方法1: 搜索试剂字段...")
    for node in G.nodes():
        node_data = G.nodes[node].get('data', {})
        node_type = G.nodes[node].get('type', '')
        config_data = G.nodes[node].get('config', {})

        # 只搜索容器类型的节点
        if node_type == 'container':
            reagent_name = node_data.get('reagent_name', '').lower()
            config_reagent = config_data.get('reagent', '').lower()

            # 精确匹配
            if reagent_name == solvent.lower() or config_reagent == solvent.lower():
                debug_print(f"✅ 通过试剂字段精确匹配找到容器: {node}")
                return node

            # 模糊匹配
            if (solvent.lower() in reagent_name and reagent_name) or \
               (solvent.lower() in config_reagent and config_reagent):
                debug_print(f"✅ 通过试剂字段模糊匹配找到容器: {node}")
                return node

    # 🔧 方法2：常见的容器命名规则
    debug_print(f"📋 方法2: 使用命名规则...")
    solvent_clean = solvent.lower().replace(' ', '_').replace('-', '_')
    possible_names = [
        f"flask_{solvent_clean}",
        f"bottle_{solvent_clean}",
        f"vessel_{solvent_clean}",
        f"{solvent_clean}_flask",
        f"{solvent_clean}_bottle",
        f"solvent_{solvent_clean}",
        f"reagent_{solvent_clean}",
        f"reagent_bottle_{solvent_clean}",
        f"reagent_bottle_1",  # 通用试剂瓶
        f"reagent_bottle_2",
        f"reagent_bottle_3"
    ]

    debug_print(f"🎯 尝试的容器名称: {possible_names[:5]}... (共 {len(possible_names)} 个)")

    for name in possible_names:
        if name in G.nodes():
            node_type = G.nodes[name].get('type', '')
            if node_type == 'container':
                debug_print(f"✅ 通过命名规则找到容器: {name}")
                return name

    # 🔧 方法3：使用第一个试剂瓶作为备选
    debug_print(f"📋 方法3: 查找备用试剂瓶...")
    for node_id in G.nodes():
        node_data = G.nodes[node_id]
        if (node_data.get('type') == 'container' and
                ('reagent' in node_id.lower() or 'bottle' in node_id.lower())):
            debug_print(f"⚠️ 未找到专用容器，使用备用容器: {node_id}")
            return node_id

    debug_print(f"❌ 无法找到溶剂 '{solvent}' 的容器")
    return ""


def find_separator_device(G: nx.DiGraph, vessel: str) -> str:
    """查找分离器设备，支持多种查找方式"""
    debug_print(f"🔍 正在查找容器 '{vessel}' 的分离器设备...")

    # 方法1：查找连接到容器的分离器设备
    debug_print(f"📋 方法1: 检查连接的分离器...")
    separator_nodes = []
    for node in G.nodes():
        node_class = G.nodes[node].get('class', '').lower()
        if 'separator' in node_class:
            separator_nodes.append(node)
            debug_print(f"📋 发现分离器设备: {node}")

            # 检查是否连接到目标容器
            if G.has_edge(node, vessel) or G.has_edge(vessel, node):
                debug_print(f"✅ 找到连接的分离器: {node}")
                return node

    debug_print(f"📊 找到的分离器总数: {len(separator_nodes)}")

    # 方法2：根据命名规则查找
    debug_print(f"📋 方法2: 使用命名规则...")
    possible_names = [
        f"{vessel}_controller",
        f"{vessel}_separator",
        vessel,  # 容器本身可能就是分离器
        "separator_1",
        "virtual_separator",
        "liquid_handler_1",  # 液体处理器也可能用于分离
        "controller_1"
    ]

    debug_print(f"🎯 尝试的分离器名称: {possible_names}")

    for name in possible_names:
        if name in G.nodes():
            node_class = G.nodes[name].get('class', '').lower()
            if 'separator' in node_class or 'controller' in node_class:
                debug_print(f"✅ 通过命名规则找到分离器: {name}")
                return name

    # 方法3：查找第一个分离器设备
    debug_print(f"📋 方法3: 使用第一个可用分离器...")
    if separator_nodes:
        debug_print(f"⚠️ 使用第一个分离器设备: {separator_nodes[0]}")
        return separator_nodes[0]

    debug_print(f"❌ 未找到分离器设备")
    return ""


def find_connected_stirrer(G: nx.DiGraph, vessel: str) -> str:
    """查找连接到指定容器的搅拌器"""
    debug_print(f"🔍 正在查找与容器 {vessel} 连接的搅拌器...")

    stirrer_nodes = []
    for node in G.nodes():
        node_data = G.nodes[node]
        node_class = node_data.get('class', '') or ''

        if 'stirrer' in node_class.lower():
            stirrer_nodes.append(node)
            debug_print(f"📋 发现搅拌器: {node}")

    debug_print(f"📊 找到的搅拌器总数: {len(stirrer_nodes)}")

    # 检查哪个搅拌器与目标容器相连
    for stirrer in stirrer_nodes:
        if G.has_edge(stirrer, vessel) or G.has_edge(vessel, stirrer):
            debug_print(f"✅ 找到连接的搅拌器: {stirrer}")
            return stirrer

    # 如果没有连接的搅拌器，返回第一个可用的
    if stirrer_nodes:
        debug_print(f"⚠️ 未找到直接连接的搅拌器，使用第一个可用的: {stirrer_nodes[0]}")
        return stirrer_nodes[0]

    debug_print("❌ 未找到搅拌器")
    return ""


def get_vessel_liquid_volume(vessel: dict) -> float:
    """
    获取容器中的液体体积 - 支持vessel字典

    Args:
        vessel: 容器字典

    Returns:
        float: 液体体积（mL）
    """
    if not vessel or "data" not in vessel:
        debug_print(f"⚠️ 容器数据为空，返回 0.0mL")
        return 0.0

    vessel_data = vessel["data"]
    vessel_id = vessel.get("id", "unknown")

    debug_print(f"🔍 读取容器 '{vessel_id}' 体积数据: {vessel_data}")

    # 检查liquid_volume字段
    if "liquid_volume" in vessel_data:
        liquid_volume = vessel_data["liquid_volume"]

        # 处理列表格式
        if isinstance(liquid_volume, list):
            if len(liquid_volume) > 0:
                volume = liquid_volume[0]
                if isinstance(volume, (int, float)):
                    debug_print(f"✅ 容器 '{vessel_id}' 体积: {volume}mL (列表格式)")
                    return float(volume)

        # 处理直接数值格式
        elif isinstance(liquid_volume, (int, float)):
            debug_print(f"✅ 容器 '{vessel_id}' 体积: {liquid_volume}mL (数值格式)")
            return float(liquid_volume)

    # 检查其他可能的体积字段
    volume_keys = ['current_volume', 'total_volume', 'volume']
    for key in volume_keys:
        if key in vessel_data:
            try:
                volume = float(vessel_data[key])
                if volume > 0:
                    debug_print(f"✅ 容器 '{vessel_id}' 体积: {volume}mL (字段: {key})")
                    return volume
            except (ValueError, TypeError):
                continue

    debug_print(f"⚠️ 无法获取容器 '{vessel_id}' 的体积，返回默认值 50.0mL")
    return 50.0


def update_vessel_volume(vessel: dict, G: nx.DiGraph, new_volume: float, description: str = "") -> None:
    """
    更新容器体积（同时更新vessel字典和图节点）

    Args:
        vessel: 容器字典
        G: 网络图
        new_volume: 新体积
        description: 更新描述
    """
    vessel_id = vessel.get("id", "unknown")

    if description:
        debug_print(f"🔧 更新容器体积 - {description}")

    # 更新vessel字典中的体积
    if "data" in vessel:
        if "liquid_volume" in vessel["data"]:
            current_volume = vessel["data"]["liquid_volume"]
            if isinstance(current_volume, list):
                if len(current_volume) > 0:
                    vessel["data"]["liquid_volume"][0] = new_volume
                else:
                    vessel["data"]["liquid_volume"] = [new_volume]
            else:
                vessel["data"]["liquid_volume"] = new_volume
        else:
            vessel["data"]["liquid_volume"] = new_volume
    else:
        vessel["data"] = {"liquid_volume": new_volume}

    # 同时更新图中的容器数据
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

    debug_print(f"📊 容器 '{vessel_id}' 体积已更新为: {new_volume:.2f}mL")


def find_separation_vessel_bottom(G: nx.DiGraph, vessel_id: str) -> str:
    """
    智能查找分离容器的底部容器（假设为flask或vessel类型）

    Args:
        G: 网络图
        vessel_id: 分离容器ID

    Returns:
        str: 底部容器ID
    """
    debug_print(f"🔍 查找分离容器 {vessel_id} 的底部容器...")

    # 方法1：根据命名规则推测
    possible_bottoms = [
        f"{vessel_id}_bottom",
        f"flask_{vessel_id}",
        f"vessel_{vessel_id}",
        f"{vessel_id}_flask",
        f"{vessel_id}_vessel"
    ]

    debug_print(f"📋 尝试的底部容器名称: {possible_bottoms}")

    for bottom_id in possible_bottoms:
        if bottom_id in G.nodes():
            node_type = G.nodes[bottom_id].get('type', '')
            if node_type == 'container':
                debug_print(f"✅ 通过命名规则找到底部容器: {bottom_id}")
                return bottom_id

    # 方法2：查找与分离器相连的容器（假设底部容器会与分离器相连）
    debug_print(f"📋 方法2: 查找连接的容器...")
    for node in G.nodes():
        node_data = G.nodes[node]
        node_class = node_data.get('class', '') or ''

        if 'separator' in node_class.lower():
            # 检查分离器的输入端
            if G.has_edge(node, vessel_id):
                for neighbor in G.neighbors(node):
                    if neighbor != vessel_id:
                        neighbor_type = G.nodes[neighbor].get('type', '')
                        if neighbor_type == 'container':
                            debug_print(f"✅ 通过连接找到底部容器: {neighbor}")
                            return neighbor

    debug_print(f"❌ 无法找到分离容器 {vessel_id} 的底部容器")
    return ""
