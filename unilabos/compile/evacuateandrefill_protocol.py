from functools import partial

import networkx as nx
import logging
import uuid
from typing import List, Dict, Any, Optional
from .utils.vessel_parser import get_vessel, find_connected_stirrer
from .utils.logger_util import debug_print, action_log
from .pump_protocol import generate_pump_protocol_with_rinsing, generate_pump_protocol

# 设置日志
logger = logging.getLogger(__name__)

create_action_log = partial(action_log, prefix="[抽真空充气]")

def find_gas_source(G: nx.DiGraph, gas: str) -> str:
    """
    根据气体名称查找对应的气源，支持多种匹配模式：
    1. 容器名称匹配
    2. 气体类型匹配（data.gas_type）
    3. 默认气源
    """
    debug_print(f"正在查找气体 '{gas}' 的气源...")

    # 通过容器名称匹配
    gas_source_patterns = [
        f"gas_source_{gas}",
        f"gas_{gas}",
        f"flask_{gas}",
        f"{gas}_source",
        f"source_{gas}",
        f"reagent_bottle_{gas}",
        f"bottle_{gas}"
    ]

    for pattern in gas_source_patterns:
        if pattern in G.nodes():
            debug_print(f"通过名称找到气源: {pattern}")
            return pattern

    # 通过气体类型匹配 (data.gas_type)
    for node_id in G.nodes():
        node_data = G.nodes[node_id]
        node_class = node_data.get('class', '') or ''

        if ('gas_source' in node_class or
            'gas' in node_id.lower() or
            node_id.startswith('flask_')):

            data = node_data.get('data', {})
            gas_type = data.get('gas_type', '')

            if gas_type.lower() == gas.lower():
                debug_print(f"通过气体类型找到气源: {node_id} (气体类型: {gas_type})")
                return node_id

            config = node_data.get('config', {})
            config_gas_type = config.get('gas_type', '')

            if config_gas_type.lower() == gas.lower():
                debug_print(f"通过配置气体类型找到气源: {node_id} (配置气体类型: {config_gas_type})")
                return node_id

    # 查找所有可用的气源设备
    available_gas_sources = []
    for node_id in G.nodes():
        node_data = G.nodes[node_id]
        node_class = node_data.get('class', '') or ''

        if ('gas_source' in node_class or
            'gas' in node_id.lower() or
            (node_id.startswith('flask_') and any(g in node_id.lower() for g in ['air', 'nitrogen', 'argon']))):

            data = node_data.get('data', {})
            gas_type = data.get('gas_type', '未知')
            available_gas_sources.append(f"{node_id} (气体类型: {gas_type})")

    # 如果找不到特定气体，使用默认的第一个气源
    default_gas_sources = [
        node for node in G.nodes()
        if ((G.nodes[node].get('class') or '').find('virtual_gas_source') != -1
            or 'gas_source' in node)
    ]

    if default_gas_sources:
        default_source = default_gas_sources[0]
        debug_print(f"未找到特定气体 '{gas}'，使用默认气源: {default_source}")
        return default_source

    raise ValueError(f"无法找到气体 '{gas}' 的气源。可用气源: {available_gas_sources}")

def find_vacuum_pump(G: nx.DiGraph) -> str:
    """查找真空泵设备"""
    vacuum_pumps = []
    for node in G.nodes():
        node_data = G.nodes[node]
        node_class = node_data.get('class', '') or ''

        if ('virtual_vacuum_pump' in node_class or
            'vacuum_pump' in node.lower() or
            'vacuum' in node_class.lower()):
            vacuum_pumps.append(node)

    if not vacuum_pumps:
        raise ValueError("系统中未找到真空泵")

    debug_print(f"使用真空泵: {vacuum_pumps[0]}")
    return vacuum_pumps[0]

def find_vacuum_solenoid_valve(G: nx.DiGraph, vacuum_pump: str) -> Optional[str]:
    """查找真空泵相关的电磁阀"""
    solenoid_valves = []
    for node in G.nodes():
        node_data = G.nodes[node]
        node_class = node_data.get('class', '') or ''

        if ('solenoid' in node_class.lower() or 'solenoid_valve' in node.lower()):
            solenoid_valves.append(node)

    # 检查连接关系
    for solenoid in solenoid_valves:
        if G.has_edge(solenoid, vacuum_pump) or G.has_edge(vacuum_pump, solenoid):
            debug_print(f"找到连接的真空电磁阀: {solenoid}")
            return solenoid

    # 通过命名规则查找
    for solenoid in solenoid_valves:
        if 'vacuum' in solenoid.lower() or solenoid == 'solenoid_valve_1':
            debug_print(f"通过命名找到真空电磁阀: {solenoid}")
            return solenoid

    debug_print("未找到真空电磁阀")
    return None

def find_gas_solenoid_valve(G: nx.DiGraph, gas_source: str) -> Optional[str]:
    """查找气源相关的电磁阀"""
    solenoid_valves = []
    for node in G.nodes():
        node_data = G.nodes[node]
        node_class = node_data.get('class', '') or ''

        if ('solenoid' in node_class.lower() or 'solenoid_valve' in node.lower()):
            solenoid_valves.append(node)

    # 检查连接关系
    for solenoid in solenoid_valves:
        if G.has_edge(gas_source, solenoid) or G.has_edge(solenoid, gas_source):
            debug_print(f"找到连接的气源电磁阀: {solenoid}")
            return solenoid

    # 通过命名规则查找
    for solenoid in solenoid_valves:
        if 'gas' in solenoid.lower() or solenoid == 'solenoid_valve_2':
            debug_print(f"通过命名找到气源电磁阀: {solenoid}")
            return solenoid

    debug_print("未找到气源电磁阀")
    return None

def generate_evacuateandrefill_protocol(
    G: nx.DiGraph,
    vessel: dict,
    gas: str,
    **kwargs
) -> List[Dict[str, Any]]:
    """
    生成抽真空和充气操作的动作序列

    Args:
        G: 设备图
        vessel: 目标容器字典（必需）
        gas: 气体名称（必需）
        **kwargs: 其他参数（兼容性）

    Returns:
        List[Dict[str, Any]]: 动作序列
    """

    vessel_id, vessel_data = get_vessel(vessel)

    # 硬编码重复次数为 3
    repeats = 3

    protocol_id = str(uuid.uuid4())

    debug_print(f"开始生成抽真空充气协议: vessel={vessel_id}, gas={gas}, repeats={repeats}")

    action_sequence = []

    # === 参数验证和修正 ===
    action_sequence.append(create_action_log(f"开始抽真空充气操作 - 容器: {vessel_id}", "🎬"))
    action_sequence.append(create_action_log(f"目标气体: {gas}", "💨"))
    action_sequence.append(create_action_log(f"循环次数: {repeats}", "🔄"))

    if not vessel_id:
        raise ValueError("容器参数不能为空")

    if not gas:
        raise ValueError("气体参数不能为空")

    if vessel_id not in G.nodes():
        raise ValueError(f"容器 '{vessel_id}' 在系统中不存在")

    action_sequence.append(create_action_log("参数验证通过", "✅"))

    # 标准化气体名称
    gas_aliases = {
        'n2': 'nitrogen',
        'ar': 'argon',
        'air': 'air',
        'o2': 'oxygen',
        'co2': 'carbon_dioxide',
        'h2': 'hydrogen',
        '氮气': 'nitrogen',
        '氩气': 'argon',
        '空气': 'air',
        '氧气': 'oxygen',
        '二氧化碳': 'carbon_dioxide',
        '氢气': 'hydrogen'
    }

    original_gas = gas
    gas_lower = gas.lower().strip()
    if gas_lower in gas_aliases:
        gas = gas_aliases[gas_lower]
        debug_print(f"标准化气体名称: {original_gas} -> {gas}")
        action_sequence.append(create_action_log(f"气体名称标准化: {original_gas} -> {gas}", "🔄"))

    debug_print(f"最终参数: 容器={vessel_id}, 气体={gas}, 重复={repeats}")

    # === 查找设备 ===
    action_sequence.append(create_action_log("正在查找相关设备...", "🔍"))

    try:
        vacuum_pump = find_vacuum_pump(G)
        action_sequence.append(create_action_log(f"找到真空泵: {vacuum_pump}", "🌪️"))

        gas_source = find_gas_source(G, gas)
        action_sequence.append(create_action_log(f"找到气源: {gas_source}", "💨"))

        vacuum_solenoid = find_vacuum_solenoid_valve(G, vacuum_pump)
        if vacuum_solenoid:
            action_sequence.append(create_action_log(f"找到真空电磁阀: {vacuum_solenoid}", "🚪"))
        else:
            action_sequence.append(create_action_log("未找到真空电磁阀", "⚠️"))

        gas_solenoid = find_gas_solenoid_valve(G, gas_source)
        if gas_solenoid:
            action_sequence.append(create_action_log(f"找到气源电磁阀: {gas_solenoid}", "🚪"))
        else:
            action_sequence.append(create_action_log("未找到气源电磁阀", "⚠️"))

        stirrer_id = find_connected_stirrer(G, vessel_id)
        if stirrer_id:
            action_sequence.append(create_action_log(f"找到搅拌器: {stirrer_id}", "🌪️"))
        else:
            action_sequence.append(create_action_log("未找到搅拌器", "⚠️"))

        debug_print(f"设备配置: 真空泵={vacuum_pump}, 气源={gas_source}, 搅拌器={stirrer_id}")

    except Exception as e:
        debug_print(f"设备查找失败: {str(e)}")
        action_sequence.append(create_action_log(f"设备查找失败: {str(e)}", "❌"))
        raise ValueError(f"设备查找失败: {str(e)}")

    # === 参数设置 ===
    action_sequence.append(create_action_log("设置操作参数...", "⚙️"))

    # 根据气体类型调整参数
    if gas.lower() in ['nitrogen', 'argon']:
        VACUUM_VOLUME = 25.0
        REFILL_VOLUME = 25.0
        PUMP_FLOW_RATE = 2.0
        VACUUM_TIME = 30.0
        REFILL_TIME = 20.0
        action_sequence.append(create_action_log("检测到惰性气体，使用标准参数", "💨"))
    elif gas.lower() in ['air', 'oxygen']:
        VACUUM_VOLUME = 20.0
        REFILL_VOLUME = 20.0
        PUMP_FLOW_RATE = 1.5
        VACUUM_TIME = 45.0
        REFILL_TIME = 25.0
        action_sequence.append(create_action_log("检测到活性气体，使用保守参数", "🔥"))
    else:
        VACUUM_VOLUME = 15.0
        REFILL_VOLUME = 15.0
        PUMP_FLOW_RATE = 1.0
        VACUUM_TIME = 60.0
        REFILL_TIME = 30.0
        action_sequence.append(create_action_log("未知气体类型，使用安全参数", "❓"))

    STIR_SPEED = 200.0

    action_sequence.append(create_action_log(f"真空体积: {VACUUM_VOLUME}mL", "📏"))
    action_sequence.append(create_action_log(f"充气体积: {REFILL_VOLUME}mL", "📏"))
    action_sequence.append(create_action_log(f"泵流速: {PUMP_FLOW_RATE}mL/s", "⚡"))

    # === 路径验证 ===
    action_sequence.append(create_action_log("验证传输路径...", "🛤️"))

    try:
        if nx.has_path(G, vessel_id, vacuum_pump):
            vacuum_path = nx.shortest_path(G, source=vessel_id, target=vacuum_pump)
            action_sequence.append(create_action_log(f"真空路径: {' -> '.join(vacuum_path)}", "🛤️"))
        else:
            action_sequence.append(create_action_log("真空路径检查: 路径不存在", "⚠️"))

        if nx.has_path(G, gas_source, vessel_id):
            gas_path = nx.shortest_path(G, source=gas_source, target=vessel_id)
            action_sequence.append(create_action_log(f"气体路径: {' -> '.join(gas_path)}", "🛤️"))
        else:
            action_sequence.append(create_action_log("气体路径检查: 路径不存在", "⚠️"))

    except Exception as e:
        action_sequence.append(create_action_log(f"路径验证失败: {str(e)}", "⚠️"))

    # === 启动搅拌器 ===
    if stirrer_id:
        action_sequence.append(create_action_log(f"启动搅拌器 {stirrer_id} (速度: {STIR_SPEED}rpm)", "🌪️"))

        action_sequence.append({
            "device_id": stirrer_id,
            "action_name": "start_stir",
            "action_kwargs": {
                "vessel": {"id": vessel_id},
                "stir_speed": STIR_SPEED,
                "purpose": "抽真空充气前预搅拌"
            }
        })

        action_sequence.append(create_action_log("等待搅拌稳定...", "⏳"))
        action_sequence.append({
            "action_name": "wait",
            "action_kwargs": {"time": 5.0}
        })
    else:
        action_sequence.append(create_action_log("跳过搅拌器启动", "⏭️"))

    # === 执行循环 ===
    action_sequence.append(create_action_log(f"开始 {repeats} 次抽真空-充气循环", "🔄"))

    for cycle in range(repeats):
        action_sequence.append(create_action_log(f"第 {cycle+1}/{repeats} 轮循环开始", "🚀"))

        # ============ 抽真空阶段 ============
        action_sequence.append(create_action_log("开始抽真空阶段", "🌪️"))

        # 启动真空泵
        action_sequence.append(create_action_log(f"启动真空泵: {vacuum_pump}", "🔛"))
        action_sequence.append({
            "device_id": vacuum_pump,
            "action_name": "set_status",
            "action_kwargs": {"string": "ON"}
        })

        # 开启真空电磁阀
        if vacuum_solenoid:
            action_sequence.append(create_action_log(f"打开真空电磁阀: {vacuum_solenoid}", "🚪"))
            action_sequence.append({
                "device_id": vacuum_solenoid,
                "action_name": "set_valve_position",
                "action_kwargs": {"command": "OPEN"}
            })

        # 抽真空操作
        action_sequence.append(create_action_log(f"开始抽真空: {vessel_id} -> {vacuum_pump}", "🌪️"))

        try:
            vacuum_transfer_actions = generate_pump_protocol_with_rinsing(
                G=G,
                from_vessel=vessel_id,
                to_vessel=vacuum_pump,
                volume=VACUUM_VOLUME,
                amount="",
                time=0.0,
                viscous=False,
                rinsing_solvent="",
                rinsing_volume=0.0,
                rinsing_repeats=0,
                solid=False,
                flowrate=PUMP_FLOW_RATE,
                transfer_flowrate=PUMP_FLOW_RATE
            )

            if vacuum_transfer_actions:
                action_sequence.extend(vacuum_transfer_actions)
                action_sequence.append(create_action_log(f"抽真空协议完成 ({len(vacuum_transfer_actions)} 个操作)", "✅"))
            else:
                action_sequence.append(create_action_log("抽真空协议为空，使用手动等待", "⚠️"))
                action_sequence.append({
                    "action_name": "wait",
                    "action_kwargs": {"time": VACUUM_TIME}
                })

        except Exception as e:
            debug_print(f"抽真空失败: {str(e)}")
            action_sequence.append(create_action_log(f"抽真空失败: {str(e)}", "❌"))
            action_sequence.append({
                "action_name": "wait",
                "action_kwargs": {"time": VACUUM_TIME}
            })

        # 抽真空后等待
        wait_minutes = VACUUM_TIME / 60
        action_sequence.append(create_action_log(f"抽真空后等待 ({wait_minutes:.1f} 分钟)", "⏳"))
        action_sequence.append({
            "action_name": "wait",
            "action_kwargs": {"time": VACUUM_TIME}
        })

        # 关闭真空电磁阀
        if vacuum_solenoid:
            action_sequence.append(create_action_log(f"关闭真空电磁阀: {vacuum_solenoid}", "🚪"))
            action_sequence.append({
                "device_id": vacuum_solenoid,
                "action_name": "set_valve_position",
                "action_kwargs": {"command": "CLOSED"}
            })

        # 关闭真空泵
        action_sequence.append(create_action_log(f"停止真空泵: {vacuum_pump}", "🔴"))
        action_sequence.append({
            "device_id": vacuum_pump,
            "action_name": "set_status",
            "action_kwargs": {"string": "OFF"}
        })

        # 阶段间等待
        action_sequence.append(create_action_log("抽真空阶段完成，短暂等待", "⏳"))
        action_sequence.append({
            "action_name": "wait",
            "action_kwargs": {"time": 5.0}
        })

        # ============ 充气阶段 ============
        action_sequence.append(create_action_log("开始气体充气阶段", "💨"))

        # 启动气源
        action_sequence.append(create_action_log(f"启动气源: {gas_source}", "🔛"))
        action_sequence.append({
            "device_id": gas_source,
            "action_name": "set_status",
            "action_kwargs": {"string": "ON"}
        })

        # 开启气源电磁阀
        if gas_solenoid:
            action_sequence.append(create_action_log(f"打开气源电磁阀: {gas_solenoid}", "🚪"))
            action_sequence.append({
                "device_id": gas_solenoid,
                "action_name": "set_valve_position",
                "action_kwargs": {"command": "OPEN"}
            })

        # 充气操作
        action_sequence.append(create_action_log(f"开始气体充气: {gas_source} -> {vessel_id}", "💨"))

        try:
            gas_transfer_actions = generate_pump_protocol_with_rinsing(
                G=G,
                from_vessel=gas_source,
                to_vessel=vessel_id,
                volume=REFILL_VOLUME,
                amount="",
                time=0.0,
                viscous=False,
                rinsing_solvent="",
                rinsing_volume=0.0,
                rinsing_repeats=0,
                solid=False,
                flowrate=PUMP_FLOW_RATE,
                transfer_flowrate=PUMP_FLOW_RATE
            )

            if gas_transfer_actions:
                action_sequence.extend(gas_transfer_actions)
                action_sequence.append(create_action_log(f"气体充气协议完成 ({len(gas_transfer_actions)} 个操作)", "✅"))
            else:
                action_sequence.append(create_action_log("充气协议为空，使用手动等待", "⚠️"))
                action_sequence.append({
                    "action_name": "wait",
                    "action_kwargs": {"time": REFILL_TIME}
                })

        except Exception as e:
            debug_print(f"气体充气失败: {str(e)}")
            action_sequence.append(create_action_log(f"气体充气失败: {str(e)}", "❌"))
            action_sequence.append({
                "action_name": "wait",
                "action_kwargs": {"time": REFILL_TIME}
            })

        # 充气后等待
        refill_wait_minutes = REFILL_TIME / 60
        action_sequence.append(create_action_log(f"充气后等待 ({refill_wait_minutes:.1f} 分钟)", "⏳"))
        action_sequence.append({
            "action_name": "wait",
            "action_kwargs": {"time": REFILL_TIME}
        })

        # 关闭气源电磁阀
        if gas_solenoid:
            action_sequence.append(create_action_log(f"关闭气源电磁阀: {gas_solenoid}", "🚪"))
            action_sequence.append({
                "device_id": gas_solenoid,
                "action_name": "set_valve_position",
                "action_kwargs": {"command": "CLOSED"}
            })

        # 关闭气源
        action_sequence.append(create_action_log(f"停止气源: {gas_source}", "🔴"))
        action_sequence.append({
            "device_id": gas_source,
            "action_name": "set_status",
            "action_kwargs": {"string": "OFF"}
        })

        # 循环间等待
        if cycle < repeats - 1:
            action_sequence.append(create_action_log("等待下一个循环...", "⏳"))
            action_sequence.append({
                "action_name": "wait",
                "action_kwargs": {"time": 10.0}
            })
        else:
            action_sequence.append(create_action_log(f"第 {cycle+1}/{repeats} 轮循环完成", "✅"))

    # === 停止搅拌器 ===
    if stirrer_id:
        action_sequence.append(create_action_log(f"停止搅拌器: {stirrer_id}", "🛑"))
        action_sequence.append({
            "device_id": stirrer_id,
            "action_name": "stop_stir",
            "action_kwargs": {"vessel": {"id": vessel_id},}
        })
    else:
        action_sequence.append(create_action_log("跳过搅拌器停止", "⏭️"))

    # === 最终等待 ===
    action_sequence.append(create_action_log("最终稳定等待...", "⏳"))
    action_sequence.append({
        "action_name": "wait",
        "action_kwargs": {"time": 10.0}
    })

    # === 总结 ===
    total_time = (VACUUM_TIME + REFILL_TIME + 25) * repeats + 20

    debug_print(f"抽真空充气协议生成完成: {len(action_sequence)} 个动作, 预计 {total_time:.0f}s")

    summary_msg = f"抽真空充气协议完成: {vessel_id} (使用 {gas}，{repeats} 次循环)"
    action_sequence.append(create_action_log(summary_msg, "🎉"))

    return action_sequence

# === 便捷函数 ===

def generate_nitrogen_purge_protocol(G: nx.DiGraph, vessel: dict, **kwargs) -> List[Dict[str, Any]]:
    """生成氮气置换协议"""
    return generate_evacuateandrefill_protocol(G, vessel, "nitrogen", **kwargs)

def generate_argon_purge_protocol(G: nx.DiGraph, vessel: dict, **kwargs) -> List[Dict[str, Any]]:
    """生成氩气置换协议"""
    return generate_evacuateandrefill_protocol(G, vessel, "argon", **kwargs)

def generate_air_purge_protocol(G: nx.DiGraph, vessel: dict, **kwargs) -> List[Dict[str, Any]]:
    """生成空气置换协议"""
    return generate_evacuateandrefill_protocol(G, vessel, "air", **kwargs)

def generate_inert_atmosphere_protocol(G: nx.DiGraph, vessel: dict, gas: str = "nitrogen", **kwargs) -> List[Dict[str, Any]]:
    """生成惰性气氛协议"""
    return generate_evacuateandrefill_protocol(G, vessel, gas, **kwargs)

# 测试函数
def test_evacuateandrefill_protocol():
    """测试抽真空充气协议"""
    debug_print("=== 抽真空充气协议测试 ===")
    debug_print("测试完成")

if __name__ == "__main__":
    test_evacuateandrefill_protocol()
