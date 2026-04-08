from typing import List, Dict, Any, Union
import networkx as nx
import logging

from .utils.unit_parser import parse_time_input
from .utils.resource_helper import get_resource_id, get_resource_display_info
from .utils.logger_util import debug_print
from .utils.vessel_parser import find_connected_stirrer

logger = logging.getLogger(__name__)

def validate_and_fix_params(stir_time: float, stir_speed: float, settling_time: float) -> tuple:
    """验证和修正参数"""
    if stir_time < 0:
        debug_print(f"搅拌时间 {stir_time}s 无效，修正为 100s")
        stir_time = 100.0
    elif stir_time > 100:  # 限制为100s
        debug_print(f"搅拌时间 {stir_time}s 过长，仿真运行时修正为 100s")
        stir_time = 100.0

    if stir_speed < 10.0 or stir_speed > 1500.0:
        debug_print(f"搅拌速度 {stir_speed} RPM 超出范围，修正为 300 RPM")
        stir_speed = 300.0

    if settling_time < 0 or settling_time > 600:  # 限制为10分钟
        debug_print(f"沉降时间 {settling_time}s 超出范围，修正为 60s")
        settling_time = 60.0

    return stir_time, stir_speed, settling_time

def extract_vessel_id(vessel) -> str:
    """从vessel参数中提取vessel_id，兼容 str / dict / ResourceDictInstance"""
    return get_resource_id(vessel)

def get_vessel_display_info(vessel) -> str:
    """获取容器的显示信息（用于日志），兼容 str / dict / ResourceDictInstance"""
    return get_resource_display_info(vessel)

def generate_stir_protocol(
    G: nx.DiGraph,
    vessel: Union[str, dict],  # 支持vessel字典或字符串
    time: Union[str, float, int] = "300",
    stir_time: Union[str, float, int] = "0",
    time_spec: str = "",
    event: str = "",
    stir_speed: float = 300.0,
    settling_time: Union[str, float] = "60",
    **kwargs
) -> List[Dict[str, Any]]:
    """生成搅拌操作的协议序列 - 修复vessel参数传递"""
    
    vessel_id = extract_vessel_id(vessel)
    vessel_display = get_vessel_display_info(vessel)

    # 确保vessel_resource是完整的Resource对象
    if isinstance(vessel, dict):
        vessel_resource = vessel
    else:
        vessel_resource = {
            "id": vessel,
            "name": "",
            "category": "",
            "children": [],
            "config": "",
            "data": "",
            "parent": "",
            "pose": {
                "orientation": {"w": 1.0, "x": 0.0, "y": 0.0, "z": 0.0},
                "position": {"x": 0.0, "y": 0.0, "z": 0.0}
            },
            "sample_id": "",
            "type": ""
        }

    # 参数验证
    if not vessel_id:
        raise ValueError("vessel 参数不能为空")

    if vessel_id not in G.nodes():
        raise ValueError(f"容器 '{vessel_id}' 不存在于系统中")
    
    # 参数解析 — 确定实际时间（优先级：time_spec > stir_time > time）
    if time_spec:
        parsed_time = parse_time_input(time_spec)
    elif stir_time not in ["0", 0, 0.0]:
        parsed_time = parse_time_input(stir_time)
    else:
        parsed_time = parse_time_input(time)
    
    # 解析沉降时间
    parsed_settling_time = parse_time_input(settling_time)
    
    # 模拟运行时间优化
    original_stir_time = parsed_time
    original_settling_time = parsed_settling_time

    # 搅拌时间限制为60秒
    stir_time_limit = 60.0
    if parsed_time > stir_time_limit:
        parsed_time = stir_time_limit

    # 沉降时间限制为30秒
    settling_time_limit = 30.0
    if parsed_settling_time > settling_time_limit:
        parsed_settling_time = settling_time_limit
    
    # 参数修正
    parsed_time, stir_speed, parsed_settling_time = validate_and_fix_params(
        parsed_time, stir_speed, parsed_settling_time
    )
    
    debug_print(f"最终参数: time={parsed_time}s, speed={stir_speed}RPM, settling={parsed_settling_time}s")

    # 查找设备
    try:
        stirrer_id = find_connected_stirrer(G, vessel_id)
    except Exception as e:
        raise ValueError(f"无法找到搅拌设备: {str(e)}")

    # 生成动作
    
    action_sequence = []
    stir_action = {
        "device_id": stirrer_id,
        "action_name": "stir",
        "action_kwargs": {
            "vessel": {"id": vessel_id},
            "time": str(time),
            "event": event,
            "time_spec": time_spec,
            "stir_time": float(parsed_time),
            "stir_speed": float(stir_speed),
            "settling_time": float(parsed_settling_time)
        }
    }
    action_sequence.append(stir_action)

    # 时间优化信息
    if original_stir_time != parsed_time or original_settling_time != parsed_settling_time:
        debug_print(f"模拟优化: 搅拌 {original_stir_time/60:.1f}min→{parsed_time/60:.1f}min, "
                    f"沉降 {original_settling_time/60:.1f}min→{parsed_settling_time/60:.1f}min")

    debug_print(f"搅拌协议生成完成: {vessel_display}, {stir_speed}RPM, "
                f"{parsed_time}s, 沉降{parsed_settling_time}s, 总{(parsed_time + parsed_settling_time)/60:.1f}min")
    
    return action_sequence

def generate_start_stir_protocol(
    G: nx.DiGraph,
    vessel: Union[str, dict],
    stir_speed: float = 300.0,
    purpose: str = "",
    **kwargs
) -> List[Dict[str, Any]]:
    """生成开始搅拌操作的协议序列 - 修复vessel参数传递"""
    
    vessel_id = extract_vessel_id(vessel)
    vessel_display = get_vessel_display_info(vessel)

    # 确保vessel_resource是完整的Resource对象
    if isinstance(vessel, dict):
        vessel_resource = vessel
    else:
        vessel_resource = {
            "id": vessel,
            "name": "",
            "category": "",
            "children": [],
            "config": "",
            "data": "",
            "parent": "",
            "pose": {
                "orientation": {"w": 1.0, "x": 0.0, "y": 0.0, "z": 0.0},
                "position": {"x": 0.0, "y": 0.0, "z": 0.0}
            },
            "sample_id": "",
            "type": ""
        }

    # 基础验证
    if not vessel_id or vessel_id not in G.nodes():
        raise ValueError("vessel 参数无效")

    # 参数修正
    if stir_speed < 10.0 or stir_speed > 1500.0:
        stir_speed = 300.0

    # 查找设备
    stirrer_id = find_connected_stirrer(G, vessel_id)

    action_sequence = [{
        "device_id": stirrer_id,
        "action_name": "start_stir",
        "action_kwargs": {
            "vessel": {"id": vessel_id},
            "stir_speed": stir_speed,
            "purpose": purpose or f"启动搅拌 {stir_speed} RPM"
        }
    }]

    debug_print(f"启动搅拌协议: {vessel_display}, {stir_speed}RPM, device={stirrer_id}")
    return action_sequence

def generate_stop_stir_protocol(
    G: nx.DiGraph,
    vessel: Union[str, dict],
    **kwargs
) -> List[Dict[str, Any]]:
    """生成停止搅拌操作的协议序列 - 修复vessel参数传递"""
    
    vessel_id = extract_vessel_id(vessel)
    vessel_display = get_vessel_display_info(vessel)

    # 确保vessel_resource是完整的Resource对象
    if isinstance(vessel, dict):
        vessel_resource = vessel
    else:
        vessel_resource = {
            "id": vessel,
            "name": "",
            "category": "",
            "children": [],
            "config": "",
            "data": "",
            "parent": "",
            "pose": {
                "orientation": {"w": 1.0, "x": 0.0, "y": 0.0, "z": 0.0},
                "position": {"x": 0.0, "y": 0.0, "z": 0.0}
            },
            "sample_id": "",
            "type": ""
        }

    # 基础验证
    if not vessel_id or vessel_id not in G.nodes():
        raise ValueError("vessel 参数无效")

    # 查找设备
    stirrer_id = find_connected_stirrer(G, vessel_id)

    action_sequence = [{
        "device_id": stirrer_id,
        "action_name": "stop_stir",
        "action_kwargs": {
            "vessel": {"id": vessel_id},
        }
    }]

    debug_print(f"停止搅拌协议: {vessel_display}, device={stirrer_id}")
    return action_sequence

# 便捷函数
def stir_briefly(G: nx.DiGraph, vessel: Union[str, dict], 
                speed: float = 300.0) -> List[Dict[str, Any]]:
    """短时间搅拌（30秒）"""
    vessel_display = get_vessel_display_info(vessel)
    debug_print(f"短时间搅拌: {vessel_display} @ {speed}RPM (30s)")
    return generate_stir_protocol(G, vessel, time="30", stir_speed=speed)

def stir_slowly(G: nx.DiGraph, vessel: Union[str, dict], 
               time: Union[str, float] = "10 min") -> List[Dict[str, Any]]:
    """慢速搅拌"""
    vessel_display = get_vessel_display_info(vessel)
    debug_print(f"慢速搅拌: {vessel_display} @ 150RPM")
    return generate_stir_protocol(G, vessel, time=time, stir_speed=150.0)

def stir_vigorously(G: nx.DiGraph, vessel: Union[str, dict], 
                   time: Union[str, float] = "5 min") -> List[Dict[str, Any]]:
    """剧烈搅拌"""
    vessel_display = get_vessel_display_info(vessel)
    debug_print(f"剧烈搅拌: {vessel_display} @ 800RPM")
    return generate_stir_protocol(G, vessel, time=time, stir_speed=800.0)

def stir_for_reaction(G: nx.DiGraph, vessel: Union[str, dict], 
                     time: Union[str, float] = "1 h") -> List[Dict[str, Any]]:
    """反应搅拌（标准速度，长时间）"""
    vessel_display = get_vessel_display_info(vessel)
    debug_print(f"反应搅拌: {vessel_display} @ 400RPM")
    return generate_stir_protocol(G, vessel, time=time, stir_speed=400.0)

def stir_for_dissolution(G: nx.DiGraph, vessel: Union[str, dict], 
                        time: Union[str, float] = "15 min") -> List[Dict[str, Any]]:
    """溶解搅拌（中等速度）"""
    vessel_display = get_vessel_display_info(vessel)
    debug_print(f"溶解搅拌: {vessel_display} @ 500RPM")
    return generate_stir_protocol(G, vessel, time=time, stir_speed=500.0)

def stir_gently(G: nx.DiGraph, vessel: Union[str, dict], 
               time: Union[str, float] = "30 min") -> List[Dict[str, Any]]:
    """温和搅拌"""
    vessel_display = get_vessel_display_info(vessel)
    debug_print(f"温和搅拌: {vessel_display} @ 200RPM")
    return generate_stir_protocol(G, vessel, time=time, stir_speed=200.0)

def stir_overnight(G: nx.DiGraph, vessel: Union[str, dict]) -> List[Dict[str, Any]]:
    """过夜搅拌（模拟时缩短为2小时）"""
    vessel_display = get_vessel_display_info(vessel)
    debug_print(f"过夜搅拌（模拟2小时）: {vessel_display} @ 300RPM")
    return generate_stir_protocol(G, vessel, time="2 h", stir_speed=300.0)

def start_continuous_stirring(G: nx.DiGraph, vessel: Union[str, dict], 
                             speed: float = 300.0, purpose: str = "continuous stirring") -> List[Dict[str, Any]]:
    """开始连续搅拌"""
    vessel_display = get_vessel_display_info(vessel)
    debug_print(f"开始连续搅拌: {vessel_display} @ {speed}RPM")
    return generate_start_stir_protocol(G, vessel, stir_speed=speed, purpose=purpose)

def stop_all_stirring(G: nx.DiGraph, vessel: Union[str, dict]) -> List[Dict[str, Any]]:
    """停止所有搅拌"""
    vessel_display = get_vessel_display_info(vessel)
    debug_print(f"停止搅拌: {vessel_display}")
    return generate_stop_stir_protocol(G, vessel)

# 测试函数
def test_stir_protocol():
    """测试搅拌协议"""
    # 测试字典格式
    vessel_dict = {"id": "flask_1", "name": "反应瓶1"}
    vessel_id = extract_vessel_id(vessel_dict)
    vessel_display = get_vessel_display_info(vessel_dict)
    debug_print(f"字典格式: {vessel_dict} -> ID: {vessel_id}, 显示: {vessel_display}")

    # 测试字符串格式
    vessel_str = "flask_2"
    vessel_id = extract_vessel_id(vessel_str)
    vessel_display = get_vessel_display_info(vessel_str)
    debug_print(f"字符串格式: {vessel_str} -> ID: {vessel_id}, 显示: {vessel_display}")

    debug_print("测试完成")

if __name__ == "__main__":
    test_stir_protocol()
