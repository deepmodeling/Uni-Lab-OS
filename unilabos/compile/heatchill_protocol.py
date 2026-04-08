from typing import List, Dict, Any, Union
import networkx as nx
from .utils.vessel_parser import get_vessel, find_connected_heatchill
from .utils.unit_parser import parse_time_input, parse_temperature_input
from .utils.logger_util import debug_print


def validate_and_fix_params(temp: float, time: float, stir_speed: float) -> tuple:
    """验证和修正参数"""
    if temp < -50.0 or temp > 300.0:
        debug_print(f"⚠️ 温度 {temp}°C 超出范围，修正为 25°C")
        temp = 25.0

    if time < 0:
        debug_print(f"⚠️ 时间 {time}s 无效，修正为 300s")
        time = 300.0

    if stir_speed < 0 or stir_speed > 1500.0:
        debug_print(f"⚠️ 搅拌速度 {stir_speed} RPM 超出范围，修正为 300 RPM")
        stir_speed = 300.0

    return temp, time, stir_speed

def generate_heat_chill_protocol(
    G: nx.DiGraph,
    vessel: dict,  # 🔧 修改：从字符串改为字典类型
    temp: float = 25.0,
    time: Union[str, float] = "300",
    temp_spec: str = "",
    time_spec: str = "",
    pressure: str = "",
    reflux_solvent: str = "",
    stir: bool = False,
    stir_speed: float = 300.0,
    purpose: str = "",
    **kwargs
) -> List[Dict[str, Any]]:
    """
    生成加热/冷却操作的协议序列 - 支持vessel字典

    Args:
        G: 设备图
        vessel: 容器字典（从XDL传入）
        temp: 目标温度 (°C)
        time: 加热时间（支持字符串如 "30 min"）
        temp_spec: 温度规格说明（优先级高于temp）
        time_spec: 时间规格说明（优先级高于time）
        pressure: 压力设置
        reflux_solvent: 回流溶剂
        stir: 是否搅拌
        stir_speed: 搅拌速度 (RPM)
        purpose: 操作目的说明
        **kwargs: 其他参数（兼容性）

    Returns:
        List[Dict[str, Any]]: 加热/冷却操作的动作序列
    """

    # 🔧 核心修改：从字典中提取容器ID
    vessel_id, vessel_data = get_vessel(vessel)

    debug_print(f"开始生成加热冷却协议: vessel={vessel_id}, temp={temp}°C, "
                f"time={time}, stir={stir} ({stir_speed} RPM), purpose='{purpose}'")

    # 参数验证
    if not vessel_id:
        raise ValueError("vessel 参数不能为空")

    if vessel_id not in G.nodes():
        raise ValueError(f"容器 '{vessel_id}' 不存在于系统中")

    # 参数解析
    # 温度解析：优先使用 temp_spec
    final_temp = parse_temperature_input(temp_spec, temp) if temp_spec else temp

    # 时间解析：优先使用 time_spec
    final_time = parse_time_input(time_spec) if time_spec else parse_time_input(time)

    # 参数修正
    final_temp, final_time, stir_speed = validate_and_fix_params(final_temp, final_time, stir_speed)

    debug_print(f"最终参数: temp={final_temp}°C, time={final_time}s, stir_speed={stir_speed} RPM")

    # 查找设备
    try:
        heatchill_id = find_connected_heatchill(G, vessel_id)
        debug_print(f"使用加热设备: {heatchill_id}")
    except Exception as e:
        raise ValueError(f"无法找到加热设备: {str(e)}")

    # 生成动作
    # 模拟运行时间优化
    original_time = final_time
    simulation_time_limit = 100.0  # 模拟运行时间限制：100秒

    if final_time > simulation_time_limit:
        final_time = simulation_time_limit
        debug_print(f"模拟运行优化: {original_time}s → {final_time}s (限制为{simulation_time_limit}s)")

    action_sequence = []
    heatchill_action = {
        "device_id": heatchill_id,
        "action_name": "heat_chill",
        "action_kwargs": {
            "vessel": {"id": vessel_id},
            "temp": float(final_temp),
            "time": float(final_time),
            "stir": bool(stir),
            "stir_speed": float(stir_speed),
            "purpose": str(purpose or f"加热到 {final_temp}°C") + (f" (模拟时间: {final_time}s)" if original_time != final_time else "")
        }
    }
    action_sequence.append(heatchill_action)

    debug_print(f"加热冷却协议生成完成: {len(action_sequence)} 个动作, "
                f"vessel={vessel_id}, temp={final_temp}°C, time={final_time}s")

    return action_sequence

def generate_heat_chill_to_temp_protocol(
    G: nx.DiGraph,
    vessel: dict,  # 🔧 修改参数类型
    temp: float = 25.0,
    time: Union[str, float] = 100.0,
    **kwargs
) -> List[Dict[str, Any]]:
    """生成加热到指定温度的协议（简化版）"""
    vessel_id, _ = get_vessel(vessel)
    debug_print(f"生成加热到温度协议: {vessel_id} → {temp}°C")
    return generate_heat_chill_protocol(G, vessel, temp, time, **kwargs)

def generate_heat_chill_start_protocol(
    G: nx.DiGraph,
    vessel: dict,  # 🔧 修改参数类型
    temp: float = 25.0,
    purpose: str = "",
    **kwargs
) -> List[Dict[str, Any]]:
    """生成开始加热操作的协议序列"""

    # 🔧 核心修改：从字典中提取容器ID
    vessel_id, _ = get_vessel(vessel)

    debug_print(f"生成启动加热协议: vessel={vessel_id}, temp={temp}°C")

    # 基础验证
    if not vessel_id or vessel_id not in G.nodes():
        raise ValueError("vessel 参数无效")

    # 查找设备
    heatchill_id = find_connected_heatchill(G, vessel_id)

    # 生成动作
    action_sequence = [{
        "device_id": heatchill_id,
        "action_name": "heat_chill_start",
        "action_kwargs": {
            "temp": temp,
            "purpose": purpose or f"开始加热到 {temp}°C",
            "vessel": {"id": vessel_id},
        }
    }]

    debug_print(f"启动加热协议生成完成")
    return action_sequence

def generate_heat_chill_stop_protocol(
    G: nx.DiGraph,
    vessel: dict,  # 🔧 修改参数类型
    **kwargs
) -> List[Dict[str, Any]]:
    """生成停止加热操作的协议序列"""

    # 🔧 核心修改：从字典中提取容器ID
    vessel_id, _ = get_vessel(vessel)

    debug_print(f"生成停止加热协议: vessel={vessel_id}")

    # 基础验证
    if not vessel_id or vessel_id not in G.nodes():
        raise ValueError("vessel 参数无效")

    # 查找设备
    heatchill_id = find_connected_heatchill(G, vessel_id)

    # 生成动作
    action_sequence = [{
        "device_id": heatchill_id,
        "action_name": "heat_chill_stop",
        "action_kwargs": {
        }
    }]

    debug_print(f"停止加热协议生成完成")
    return action_sequence
