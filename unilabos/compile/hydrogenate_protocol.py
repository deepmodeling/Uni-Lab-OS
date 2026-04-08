import networkx as nx
from typing import List, Dict, Any, Optional
from .utils.vessel_parser import get_vessel
from .utils.logger_util import debug_print
from .utils.unit_parser import parse_temperature_input, parse_time_input


def find_associated_solenoid_valve(G: nx.DiGraph, device_id: str) -> Optional[str]:
    """查找与指定设备相关联的电磁阀"""
    solenoid_valves = [
        node for node in G.nodes()
        if ('solenoid' in (G.nodes[node].get('class') or '').lower()
            or 'solenoid_valve' in node)
    ]

    # 通过网络连接查找直接相连的电磁阀
    for solenoid in solenoid_valves:
        if G.has_edge(device_id, solenoid) or G.has_edge(solenoid, device_id):
            return solenoid

    # 通过命名规则查找关联的电磁阀
    device_type = ""
    if 'gas' in device_id.lower():
        device_type = "gas"
    elif 'h2' in device_id.lower() or 'hydrogen' in device_id.lower():
        device_type = "gas"

    if device_type:
        for solenoid in solenoid_valves:
            if device_type in solenoid.lower():
                return solenoid

    return None


def find_connected_device(G: nx.DiGraph, vessel: str, device_type: str) -> str:
    """
    查找与容器相连的指定类型设备

    Args:
        G: 网络图
        vessel: 容器名称
        device_type: 设备类型 ('heater', 'stirrer', 'gas_source')

    Returns:
        str: 设备ID，如果没有则返回None
    """
    # 根据设备类型定义搜索关键词
    if device_type == 'heater':
        keywords = ['heater', 'heat', 'heatchill']
        device_class = 'virtual_heatchill'
    elif device_type == 'stirrer':
        keywords = ['stirrer', 'stir']
        device_class = 'virtual_stirrer'
    elif device_type == 'gas_source':
        keywords = ['gas', 'h2', 'hydrogen']
        device_class = 'virtual_gas_source'
    else:
        return None

    # 查找设备节点
    device_nodes = []
    for node in G.nodes():
        node_data = G.nodes[node]
        node_name = node.lower()
        node_class = node_data.get('class', '').lower()

        if any(keyword in node_name for keyword in keywords):
            device_nodes.append(node)
        elif device_class in node_class:
            device_nodes.append(node)

    debug_print(f"找到的{device_type}节点: {device_nodes}")

    # 检查是否有设备与目标容器相连
    for device in device_nodes:
        if G.has_edge(device, vessel) or G.has_edge(vessel, device):
            debug_print(f"找到与容器 '{vessel}' 相连的{device_type}: {device}")
            return device

    # 如果没有直接连接，查找距离最近的设备
    for device in device_nodes:
        try:
            path = nx.shortest_path(G, source=device, target=vessel)
            if len(path) <= 3:  # 最多2个中间节点
                debug_print(f"找到距离较近的{device_type}: {device}")
                return device
        except nx.NetworkXNoPath:
            continue

    debug_print(f"未找到与容器 '{vessel}' 相连的{device_type}")
    return None


def generate_hydrogenate_protocol(
    G: nx.DiGraph,
    vessel: dict,  # 🔧 修改：从字符串改为字典类型
    temp: str,
    time: str,
    **kwargs  # 接收其他可能的参数但不使用
) -> List[Dict[str, Any]]:
    """
    生成氢化反应协议序列 - 支持vessel字典

    Args:
        G: 有向图，节点为容器和设备
        vessel: 反应容器字典（从XDL传入）
        temp: 反应温度（如 "45 °C"）
        time: 反应时间（如 "2 h"）
        **kwargs: 其他可选参数，但不使用

    Returns:
        List[Dict[str, Any]]: 动作序列
    """

    # 🔧 核心修改：从字典中提取容器ID
    vessel_id, vessel_data = get_vessel(vessel)

    action_sequence = []

    # 解析参数
    temperature = parse_temperature_input(temp)
    reaction_time = parse_time_input(time)

    debug_print(f"开始生成氢化反应协议: vessel={vessel_id}, "
                f"temp={temperature}°C, time={reaction_time/3600:.1f}h")

    # 记录氢化前的容器状态
    original_liquid_volume = 0.0
    if "data" in vessel and "liquid_volume" in vessel["data"]:
        current_volume = vessel["data"]["liquid_volume"]
        if isinstance(current_volume, list) and len(current_volume) > 0:
            original_liquid_volume = current_volume[0]
        elif isinstance(current_volume, (int, float)):
            original_liquid_volume = current_volume

    # 1. 验证目标容器存在
    if vessel_id not in G.nodes():
        debug_print(f"⚠️ 容器 '{vessel_id}' 不存在于系统中，跳过氢化反应")
        return action_sequence

    # 2. 查找相连的设备
    heater_id = find_connected_device(G, vessel_id, 'heater')
    stirrer_id = find_connected_device(G, vessel_id, 'stirrer')
    gas_source_id = find_connected_device(G, vessel_id, 'gas_source')

    debug_print(f"设备配置: heater={heater_id or '未找到'}, "
                f"stirrer={stirrer_id or '未找到'}, gas={gas_source_id or '未找到'}")

    # 3. 启动搅拌器
    if stirrer_id:
        action_sequence.append({
            "device_id": stirrer_id,
            "action_name": "start_stir",
            "action_kwargs": {
                "vessel": {"id": vessel_id},
                "stir_speed": 300.0,
                "purpose": "氢化反应: 开始搅拌"
            }
        })
    else:
        debug_print(f"⚠️ 未找到搅拌器，继续执行")

    # 4. 启动气源（氢气）
    if gas_source_id:
        action_sequence.append({
            "device_id": gas_source_id,
            "action_name": "set_status",
            "action_kwargs": {
                "string": "ON"
            }
        })

        # 查找相关的电磁阀
        gas_solenoid = find_associated_solenoid_valve(G, gas_source_id)
        if gas_solenoid:
            action_sequence.append({
                "device_id": gas_solenoid,
                "action_name": "set_valve_position",
                "action_kwargs": {
                    "command": "OPEN"
                }
            })
    else:
        debug_print(f"⚠️ 未找到气源，继续执行")

    # 5. 等待气体稳定
    action_sequence.append({
        "action_name": "wait",
        "action_kwargs": {
            "time": 30.0,
            "description": "等待氢气环境稳定"
        }
    })

    # 6. 启动加热器
    if heater_id:
        action_sequence.append({
            "device_id": heater_id,
            "action_name": "heat_chill_start",
            "action_kwargs": {
                "vessel": {"id": vessel_id},
                "temp": temperature,
                "purpose": f"氢化反应: 加热到 {temperature}°C"
            }
        })

        # 等待温度稳定
        action_sequence.append({
            "action_name": "wait",
            "action_kwargs": {
                "time": 20.0,
                "description": f"等待温度稳定到 {temperature}°C"
            }
        })

        # 模拟运行时间优化
        original_reaction_time = reaction_time
        simulation_time_limit = 60.0

        if reaction_time > simulation_time_limit:
            reaction_time = simulation_time_limit
            debug_print(f"模拟运行优化: {original_reaction_time}s → {reaction_time}s")

        # 保持反应温度
        action_sequence.append({
            "device_id": heater_id,
            "action_name": "heat_chill",
            "action_kwargs": {
                "vessel": {"id": vessel_id},
                "temp": temperature,
                "time": reaction_time,
                "purpose": f"氢化反应: 保持 {temperature}°C，反应 {reaction_time/60:.1f}分钟" + (f" (模拟时间)" if original_reaction_time != reaction_time else "")
            }
        })

    else:
        debug_print(f"⚠️ 未找到加热器，使用室温反应")

        # 室温反应也需要时间优化
        original_reaction_time = reaction_time
        simulation_time_limit = 60.0

        if reaction_time > simulation_time_limit:
            reaction_time = simulation_time_limit
            debug_print(f"室温反应时间优化: {original_reaction_time}s → {reaction_time}s")

        # 室温反应，只等待时间
        action_sequence.append({
            "action_name": "wait",
            "action_kwargs": {
                "time": reaction_time,
                "description": f"室温氢化反应 {reaction_time/60:.1f}分钟" + (f" (模拟时间)" if original_reaction_time != reaction_time else "")
            }
        })

    # 7. 停止加热
    if heater_id:
        action_sequence.append({
            "device_id": heater_id,
            "action_name": "heat_chill_stop",
            "action_kwargs": {
                "vessel": {"id": vessel_id},
                "purpose": "氢化反应完成，停止加热"
            }
        })

    # 8. 等待冷却
    action_sequence.append({
        "action_name": "wait",
        "action_kwargs": {
            "time": 300.0,
            "description": "等待反应混合物冷却"
        }
    })

    # 9. 停止气源
    if gas_source_id:
        # 先关闭电磁阀
        gas_solenoid = find_associated_solenoid_valve(G, gas_source_id)
        if gas_solenoid:
            action_sequence.append({
                "device_id": gas_solenoid,
                "action_name": "set_valve_position",
                "action_kwargs": {
                    "command": "CLOSED"
                }
            })

        # 再关闭气源
        action_sequence.append({
            "device_id": gas_source_id,
            "action_name": "set_status",
            "action_kwargs": {
                "string": "OFF"
            }
        })

    # 10. 停止搅拌
    if stirrer_id:
        action_sequence.append({
            "device_id": stirrer_id,
            "action_name": "stop_stir",
            "action_kwargs": {
                "vessel": {"id": vessel_id},
                "purpose": "氢化反应完成，停止搅拌"
            }
        })

    # 氢化完成后的状态（氢化反应通常不改变体积）
    final_liquid_volume = original_liquid_volume

    # 总结
    debug_print(f"氢化反应协议生成完成: {len(action_sequence)} 个动作, "
                f"vessel={vessel_id}, temp={temperature}°C, time={reaction_time/60:.1f}min, "
                f"volume={original_liquid_volume:.2f}→{final_liquid_volume:.2f}mL")

    return action_sequence
