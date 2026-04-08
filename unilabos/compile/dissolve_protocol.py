from functools import partial

import networkx as nx
import logging
from typing import List, Dict, Any, Union

from .utils.logger_util import debug_print, action_log
from .utils.unit_parser import parse_volume_input, parse_mass_input, parse_time_input, parse_temperature_input
from .utils.vessel_parser import get_vessel, find_solvent_vessel, find_connected_heatchill, find_connected_stirrer, find_solid_dispenser
from .pump_protocol import generate_pump_protocol_with_rinsing

logger = logging.getLogger(__name__)

# 创建进度日志动作
create_action_log = partial(action_log, prefix="[DISSOLVE]")

def generate_dissolve_protocol(
    G: nx.DiGraph,
    vessel: dict,  # 🔧 修改：从字符串改为字典类型
    # 🔧 修复：按照checklist.md的DissolveProtocol参数
    solvent: str = "",
    volume: Union[str, float] = 0.0,
    amount: str = "",
    temp: Union[str, float] = 25.0,
    time: Union[str, float] = 0.0,
    stir_speed: float = 300.0,
    # 🔧 关键修复：添加缺失的参数，防止"unexpected keyword argument"错误
    mass: Union[str, float] = 0.0,  # 这个参数在action文件中存在，必须包含
    mol: str = "",                  # 这个参数在action文件中存在，必须包含
    reagent: str = "",              # 这个参数在action文件中存在，必须包含
    event: str = "",                # 这个参数在action文件中存在，必须包含
    **kwargs                        # 🔧 关键：接受所有其他参数，防止unexpected keyword错误
) -> List[Dict[str, Any]]:
    """
    生成溶解操作的协议序列 - 增强版
    
    🔧 修复要点：
    1. 修改vessel参数类型为dict，并提取vessel_id
    2. 添加action文件中的所有参数（mass, mol, reagent, event）
    3. 使用 **kwargs 接受所有额外参数，防止 unexpected keyword argument 错误
    4. 支持固体溶解和液体溶解两种模式
    5. 添加详细的体积运算逻辑
    
    支持两种溶解模式：
    1. 液体溶解：指定 solvent + volume，使用pump protocol转移溶剂
    2. 固体溶解：指定 mass/mol + reagent，使用固体加样器添加固体试剂
    
    支持所有XDL参数和单位：
    - volume: "10 mL", "?" 或数值
    - mass: "2.9 g", "?" 或数值  
    - temp: "60 °C", "room temperature", "?" 或数值
    - time: "30 min", "1 h", "?" 或数值
    - mol: "0.12 mol", "16.2 mmol"
    """
    
    # 从字典中提取容器ID
    vessel_id, vessel_data = get_vessel(vessel)

    debug_print(f"溶解协议: vessel={vessel_id}, solvent='{solvent}', volume={volume}, "
                f"mass={mass}, temp={temp}, time={time}")

    action_sequence = []

    if not vessel_id:
        raise ValueError("vessel 参数不能为空")
    
    if vessel_id not in G.nodes():
        raise ValueError(f"容器 '{vessel_id}' 不存在于系统中")

    # 记录溶解前的容器状态
    original_liquid_volume = 0.0
    if "data" in vessel and "liquid_volume" in vessel["data"]:
        current_volume = vessel["data"]["liquid_volume"]
        if isinstance(current_volume, list) and len(current_volume) > 0:
            original_liquid_volume = current_volume[0]
        elif isinstance(current_volume, (int, float)):
            original_liquid_volume = current_volume

    # === 参数解析 ===
    final_volume = parse_volume_input(volume)
    final_mass = parse_mass_input(mass)
    final_temp = parse_temperature_input(temp)
    final_time = parse_time_input(time)

    debug_print(f"参数解析: vol={final_volume}mL, mass={final_mass}g, temp={final_temp}°C, time={final_time}s")

    # === 判断溶解类型 ===
    
    # 判断是固体溶解还是液体溶解
    is_solid_dissolve = (final_mass > 0 or (mol and mol.strip() != "") or (reagent and reagent.strip() != ""))
    is_liquid_dissolve = (final_volume > 0 and solvent and solvent.strip() != "")
    
    if not is_solid_dissolve and not is_liquid_dissolve:
        # 默认为液体溶解，50mL
        is_liquid_dissolve = True
        final_volume = 50.0
        if not solvent:
            solvent = "water"  # 默认溶剂
        debug_print("未明确指定溶解参数，默认为50mL水溶解")
    
    dissolve_type = "固体溶解" if is_solid_dissolve else "液体溶解"
    debug_print(f"溶解类型: {dissolve_type}")

    action_sequence.append(create_action_log(f"溶解类型: {dissolve_type}", "📋"))

    # === 查找设备 ===
    heatchill_id = find_connected_heatchill(G, vessel_id)
    stirrer_id = find_connected_stirrer(G, vessel_id)
    
    # 优先使用加热搅拌器，否则使用独立搅拌器
    stir_device_id = heatchill_id or stirrer_id
    debug_print(f"设备: heatchill='{heatchill_id}', stirrer='{stirrer_id}'")

    if not stir_device_id:
        action_sequence.append(create_action_log("未找到搅拌设备，将跳过搅拌", "⚠️"))
    
    # === 执行溶解流程 ===
    try:
        # 启动加热搅拌（如果需要）
        if stir_device_id and (final_temp > 25.0 or final_time > 0 or stir_speed > 0):

            if heatchill_id and (final_temp > 25.0 or final_time > 0):
                # 使用加热搅拌器
                
                heatchill_action = {
                    "device_id": heatchill_id,
                    "action_name": "heat_chill_start",
                    "action_kwargs": {
                        "vessel": {"id": vessel_id},
                        "temp": final_temp,
                        "purpose": f"溶解准备 - {event}" if event else "溶解准备"
                    }
                }
                action_sequence.append(heatchill_action)
                
                # 等待温度稳定
                if final_temp > 25.0:
                    wait_time = min(60, abs(final_temp - 25.0) * 1.5)
                    action_sequence.append({
                        "action_name": "wait",
                        "action_kwargs": {"time": wait_time}
                    })
            
            elif stirrer_id:
                # 使用独立搅拌器
                
                stir_action = {
                    "device_id": stirrer_id,
                    "action_name": "start_stir",
                    "action_kwargs": {
                        "vessel": {"id": vessel_id},
                        "stir_speed": stir_speed,
                        "purpose": f"溶解搅拌 - {event}" if event else "溶解搅拌"
                    }
                }
                action_sequence.append(stir_action)

                # 等待搅拌稳定
                action_sequence.append({
                    "action_name": "wait",
                    "action_kwargs": {"time": 5}
                })
        
        if is_solid_dissolve:
            # === 固体溶解路径 ===
            solid_dispenser = find_solid_dispenser(G)
            if solid_dispenser:
                
                # 固体加样
                add_kwargs = {
                    "vessel": {"id": vessel_id},
                    "reagent": reagent or amount or "solid reagent",
                    "purpose": f"溶解固体试剂 - {event}" if event else "溶解固体试剂",
                    "event": event
                }
                
                if final_mass > 0:
                    add_kwargs["mass"] = str(final_mass)
                if mol and mol.strip():
                    add_kwargs["mol"] = mol

                action_sequence.append({
                    "device_id": solid_dispenser,
                    "action_name": "add_solid",
                    "action_kwargs": add_kwargs
                })

                # 固体溶解体积运算 - 固体本身不会显著增加体积
                
            else:
                debug_print("未找到固体加样器，跳过固体添加")
                action_sequence.append(create_action_log("未找到固体加样器，无法添加固体", "❌"))
        
        elif is_liquid_dissolve:
            # === 液体溶解路径 ===
            try:
                solvent_vessel = find_solvent_vessel(G, solvent)
            except ValueError as e:
                debug_print(f"溶剂容器查找失败: {str(e)}，跳过溶剂添加")
                action_sequence.append(create_action_log(f"溶剂容器查找失败: {str(e)}", "❌"))
                solvent_vessel = None
            
            if solvent_vessel:
                # 计算流速 - 溶解时通常用较慢的速度，避免飞溅
                flowrate = 1.0  # 较慢的注入速度
                transfer_flowrate = 0.5  # 较慢的转移速度

                # 调用pump protocol
                pump_actions = generate_pump_protocol_with_rinsing(
                    G=G,
                    from_vessel=solvent_vessel,
                    to_vessel=vessel_id,
                    volume=final_volume,
                    amount=amount,
                    time=0.0,  # 不在pump level控制时间
                    viscous=False,
                    rinsing_solvent="",
                    rinsing_volume=0.0,
                    rinsing_repeats=0,
                    solid=False,
                    flowrate=flowrate,
                    transfer_flowrate=transfer_flowrate,
                    rate_spec="",
                    event=event,
                    through="",
                    **kwargs
                )
                action_sequence.extend(pump_actions)

                # 液体溶解体积运算 - 添加溶剂后更新容器体积

                # 确保vessel有data字段
                if "data" not in vessel:
                    vessel["data"] = {}
                
                if "liquid_volume" in vessel["data"]:
                    current_volume = vessel["data"]["liquid_volume"]
                    if isinstance(current_volume, list):
                        if len(current_volume) > 0:
                            vessel["data"]["liquid_volume"][0] += final_volume
                        else:
                            vessel["data"]["liquid_volume"] = [final_volume]
                    elif isinstance(current_volume, (int, float)):
                        vessel["data"]["liquid_volume"] += final_volume
                    else:
                        vessel["data"]["liquid_volume"] = final_volume
                else:
                    vessel["data"]["liquid_volume"] = final_volume
                
                # 🔧 同时更新图中的容器数据
                if vessel_id in G.nodes():
                    if 'data' not in G.nodes[vessel_id]:
                        G.nodes[vessel_id]['data'] = {}
                    
                    vessel_node_data = G.nodes[vessel_id]['data']
                    current_node_volume = vessel_node_data.get('liquid_volume', 0.0)
                    
                    if isinstance(current_node_volume, list):
                        if len(current_node_volume) > 0:
                            G.nodes[vessel_id]['data']['liquid_volume'][0] += final_volume
                        else:
                            G.nodes[vessel_id]['data']['liquid_volume'] = [final_volume]
                    else:
                        G.nodes[vessel_id]['data']['liquid_volume'] = current_node_volume + final_volume

                # 溶剂添加后等待
                action_sequence.append({
                    "action_name": "wait",
                    "action_kwargs": {"time": 5}
                })
        
        # 等待溶解完成
        if final_time > 0:
            wait_minutes = final_time / 60

            if heatchill_id:
                # 使用定时加热搅拌
                
                dissolve_action = {
                    "device_id": heatchill_id,
                    "action_name": "heat_chill",
                    "action_kwargs": {
                        "vessel": {"id": vessel_id},
                        "temp": final_temp,
                        "time": final_time,
                        "stir": True,
                        "stir_speed": stir_speed,
                        "purpose": f"溶解等待 - {event}" if event else "溶解等待"
                    }
                }
                action_sequence.append(dissolve_action)
            
            elif stirrer_id:
                # 使用定时搅拌
                
                stir_action = {
                    "device_id": stirrer_id,
                    "action_name": "stir",
                    "action_kwargs": {
                        "vessel": {"id": vessel_id},
                        "stir_time": final_time,
                        "stir_speed": stir_speed,
                        "settling_time": 0,
                        "purpose": f"溶解搅拌 - {event}" if event else "溶解搅拌"
                    }
                }
                action_sequence.append(stir_action)
            
            else:
                # 简单等待
                action_sequence.append({
                    "action_name": "wait",
                    "action_kwargs": {"time": final_time}
                })
        
        # 步骤5.5: 停止加热搅拌（如果需要）
        if heatchill_id and final_time == 0 and final_temp > 25.0:

            stop_action = {
                "device_id": heatchill_id,
                "action_name": "heat_chill_stop",
                "action_kwargs": {
                    "vessel": {"id": vessel_id},
                }
            }
            action_sequence.append(stop_action)
        
    except Exception as e:
        debug_print(f"溶解流程执行失败: {str(e)}")
        action_sequence.append(create_action_log(f"溶解流程失败: {str(e)}", "❌"))
        # 添加错误日志
        action_sequence.append({
            "device_id": "system",
            "action_name": "log_message",
            "action_kwargs": {
                "message": f"溶解失败: {str(e)}"
            }
        })
    
    # 🔧 新增：溶解完成后的状态报告
    final_liquid_volume = 0.0
    if "data" in vessel and "liquid_volume" in vessel["data"]:
        current_volume = vessel["data"]["liquid_volume"]
        if isinstance(current_volume, list) and len(current_volume) > 0:
            final_liquid_volume = current_volume[0]
        elif isinstance(current_volume, (int, float)):
            final_liquid_volume = current_volume
    
    # === 最终结果 ===
    debug_print(f"溶解协议完成: {vessel_id}, 类型={dissolve_type}, "
                f"动作数={len(action_sequence)}, 体积={original_liquid_volume:.2f}→{final_liquid_volume:.2f}mL")
    
    # 添加完成日志
    summary_msg = f"溶解协议完成: {vessel_id}"
    if is_liquid_dissolve:
        summary_msg += f" (使用 {final_volume}mL {solvent})"
    if is_solid_dissolve:
        summary_msg += f" (溶解 {final_mass}g {reagent})"
    
    action_sequence.append(create_action_log(summary_msg, "✅"))
    
    return action_sequence


# === 便捷函数 ===
# 🔧 修改便捷函数的参数类型

def dissolve_solid_by_mass(G: nx.DiGraph, vessel: dict, reagent: str, mass: Union[str, float], 
                          temp: Union[str, float] = 25.0, time: Union[str, float] = "10 min") -> List[Dict[str, Any]]:
    """按质量溶解固体"""
    vessel_id = vessel["id"]
    debug_print(f"快速固体溶解: {reagent} ({mass}) → {vessel_id}")
    return generate_dissolve_protocol(
        G, vessel, 
        mass=mass, 
        reagent=reagent,
        temp=temp, 
        time=time
    )

def dissolve_solid_by_moles(G: nx.DiGraph, vessel: dict, reagent: str, mol: str, 
                           temp: Union[str, float] = 25.0, time: Union[str, float] = "10 min") -> List[Dict[str, Any]]:
    """按摩尔数溶解固体"""
    vessel_id = vessel["id"]
    debug_print(f"按摩尔数溶解固体: {reagent} ({mol}) → {vessel_id}")
    return generate_dissolve_protocol(
        G, vessel, 
        mol=mol, 
        reagent=reagent,
        temp=temp, 
        time=time
    )

def dissolve_with_solvent(G: nx.DiGraph, vessel: dict, solvent: str, volume: Union[str, float], 
                         temp: Union[str, float] = 25.0, time: Union[str, float] = "5 min") -> List[Dict[str, Any]]:
    """用溶剂溶解"""
    vessel_id = vessel["id"]
    debug_print(f"溶剂溶解: {solvent} ({volume}) → {vessel_id}")
    return generate_dissolve_protocol(
        G, vessel, 
        solvent=solvent, 
        volume=volume,
        temp=temp, 
        time=time
    )

def dissolve_at_room_temp(G: nx.DiGraph, vessel: dict, solvent: str, volume: Union[str, float]) -> List[Dict[str, Any]]:
    """室温溶解"""
    vessel_id = vessel["id"]
    debug_print(f"室温溶解: {solvent} ({volume}) → {vessel_id}")
    return generate_dissolve_protocol(
        G, vessel, 
        solvent=solvent, 
        volume=volume,
        temp="room temperature", 
        time="5 min"
    )

def dissolve_with_heating(G: nx.DiGraph, vessel: dict, solvent: str, volume: Union[str, float], 
                         temp: Union[str, float] = "60 °C", time: Union[str, float] = "15 min") -> List[Dict[str, Any]]:
    """加热溶解"""
    vessel_id = vessel["id"]
    debug_print(f"加热溶解: {solvent} ({volume}) → {vessel_id} @ {temp}")
    return generate_dissolve_protocol(
        G, vessel, 
        solvent=solvent, 
        volume=volume,
        temp=temp, 
        time=time
    )

# 测试函数
def test_dissolve_protocol():
    """测试溶解协议的各种参数解析"""
    # 测试体积解析
    volumes = ["10 mL", "?", 10.0, "1 L", "500 μL"]
    for vol in volumes:
        result = parse_volume_input(vol)
        debug_print(f"体积解析: {vol} → {result}mL")

    # 测试质量解析
    masses = ["2.9 g", "?", 2.5, "500 mg"]
    for mass in masses:
        result = parse_mass_input(mass)
        debug_print(f"质量解析: {mass} → {result}g")

    # 测试温度解析
    temps = ["60 °C", "room temperature", "?", 25.0, "reflux"]
    for temp in temps:
        result = parse_temperature_input(temp)
        debug_print(f"温度解析: {temp} → {result}°C")

    # 测试时间解析
    times = ["30 min", "1 h", "?", 60.0]
    for time in times:
        result = parse_time_input(time)
        debug_print(f"时间解析: {time} → {result}s")

    debug_print("测试完成")

if __name__ == "__main__":
    test_dissolve_protocol()