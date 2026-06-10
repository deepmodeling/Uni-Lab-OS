from typing import List, Dict, Any
import networkx as nx
from .pump_protocol import generate_pump_protocol


def get_vessel_liquid_volume(G: nx.DiGraph, vessel: str) -> float:
    """
    获取容器中的液体体积
    """
    if vessel not in G.nodes():
        return 0.0

    vessel_data = G.nodes[vessel].get('data', {})
    liquids = vessel_data.get('liquid', [])

    total_volume = 0.0
    for liquid in liquids:
        if isinstance(liquid, dict) and 'liquid_volume' in liquid:
            total_volume += liquid['liquid_volume']

    return total_volume


def find_centrifuge_device(G: nx.DiGraph) -> str:
    """
    查找离心机设备
    """
    centrifuge_nodes = [node for node in G.nodes()
                        if (G.nodes[node].get('class') or '') == 'virtual_centrifuge']

    if centrifuge_nodes:
        return centrifuge_nodes[0]

    raise ValueError("系统中未找到离心机设备")


def find_centrifuge_vessel(G: nx.DiGraph) -> str:
    """
    查找离心机专用容器
    """
    possible_names = [
        "centrifuge_tube",
        "centrifuge_vessel",
        "tube_centrifuge",
        "vessel_centrifuge",
        "centrifuge",
        "tube_15ml",
        "tube_50ml"
    ]

    for vessel_name in possible_names:
        if vessel_name in G.nodes():
            return vessel_name

    raise ValueError(f"未找到离心机容器。尝试了以下名称: {possible_names}")


def generate_centrifuge_protocol(
    G: nx.DiGraph,
    vessel: str,
    speed: float,
    time: float,
    temp: float = 25.0
) -> List[Dict[str, Any]]:
    """
    生成离心操作的协议序列，复用 pump_protocol 的成熟算法

    离心流程：
    1. 液体转移：将待离心溶液从源容器转移到离心机容器
    2. 离心操作：执行离心分离
    3. 上清液转移：将离心后的上清液转移回原容器或新容器
    4. 沉淀处理：处理离心沉淀（可选）

    Args:
        G: 有向图，节点为设备和容器，边为流体管道
        vessel: 包含待离心溶液的容器名称
        speed: 离心速度 (rpm)
        time: 离心时间 (秒)
        temp: 离心温度 (°C)，默认25°C

    Returns:
        List[Dict[str, Any]]: 离心操作的动作序列

    Raises:
        ValueError: 当找不到必要的设备时抛出异常

    Examples:
        centrifuge_actions = generate_centrifuge_protocol(G, "reaction_mixture", 5000, 600, 4.0)
    """
    action_sequence = []

    print(f"CENTRIFUGE: 开始生成离心协议")
    print(f"  - 源容器: {vessel}")
    print(f"  - 离心速度: {speed} rpm")
    print(f"  - 离心时间: {time}s ({time/60:.1f}分钟)")
    print(f"  - 离心温度: {temp}°C")

    # 验证源容器存在
    if vessel not in G.nodes():
        raise ValueError(f"源容器 '{vessel}' 不存在于系统中")

    # 获取源容器中的液体体积
    source_volume = get_vessel_liquid_volume(G, vessel)
    print(f"CENTRIFUGE: 源容器 {vessel} 中有 {source_volume} mL 液体")

    # 查找离心机设备
    try:
        centrifuge_id = find_centrifuge_device(G)
        print(f"CENTRIFUGE: 找到离心机: {centrifuge_id}")
    except ValueError as e:
        raise ValueError(f"无法找到离心机: {str(e)}")

    # 查找离心机容器
    try:
        centrifuge_vessel = find_centrifuge_vessel(G)
        print(f"CENTRIFUGE: 找到离心机容器: {centrifuge_vessel}")
    except ValueError as e:
        raise ValueError(f"无法找到离心机容器: {str(e)}")

    # === 简化的体积计算策略 ===
    if source_volume > 0:
        # 如果能检测到液体体积，使用实际体积的大部分
        transfer_volume = min(source_volume * 0.9, 15.0)  # 90%或最多15mL（离心管通常较小）
        print(f"CENTRIFUGE: 检测到液体体积，将转移 {transfer_volume} mL")
    else:
        # 如果检测不到液体体积，默认转移标准量
        transfer_volume = 10.0  # 标准离心管体积
        print(f"CENTRIFUGE: 未检测到液体体积，默认转移 {transfer_volume} mL")

    # === 第一步：将待离心溶液转移到离心机容器 ===
    print(f"CENTRIFUGE: 将 {transfer_volume} mL 溶液从 {vessel} 转移到 {centrifuge_vessel}")
    try:
        # 使用成熟的 pump_protocol 算法进行液体转移
        transfer_to_centrifuge_actions = generate_pump_protocol(
            G=G,
            from_vessel=vessel,
            to_vessel=centrifuge_vessel,
            volume=transfer_volume,
            flowrate=1.0,  # 离心转移用慢速，避免气泡
            transfer_flowrate=1.0
        )
        action_sequence.extend(transfer_to_centrifuge_actions)
    except Exception as e:
        raise ValueError(f"无法将溶液转移到离心机: {str(e)}")

    # 转移后等待
    wait_action = {
        "action_name": "wait",
        "action_kwargs": {"time": 5}
    }
    action_sequence.append(wait_action)

    # === 第二步：执行离心操作 ===
    print(f"CENTRIFUGE: 执行离心操作")
    centrifuge_action = {
        "device_id": centrifuge_id,
        "action_name": "centrifuge",
        "action_kwargs": {
            "vessel": {"id": centrifuge_vessel},
            "speed": speed,
            "time": time,
            "temp": temp
        }
    }
    action_sequence.append(centrifuge_action)

    # 离心后等待系统稳定
    wait_action = {
        "action_name": "wait",
        "action_kwargs": {"time": 10}  # 离心后等待稍长，让沉淀稳定
    }
    action_sequence.append(wait_action)

    # === 第三步：将上清液转移回原容器 ===
    print(f"CENTRIFUGE: 将上清液从离心机转移回 {vessel}")
    try:
        # 估算上清液体积（约为转移体积的80% - 假设20%成为沉淀）
        supernatant_volume = transfer_volume * 0.8
        print(f"CENTRIFUGE: 预计上清液体积 {supernatant_volume} mL")

        transfer_back_actions = generate_pump_protocol(
            G=G,
            from_vessel=centrifuge_vessel,
            to_vessel=vessel,
            volume=supernatant_volume,
            flowrate=0.5,  # 上清液转移更慢，避免扰动沉淀
            transfer_flowrate=0.5
        )
        action_sequence.extend(transfer_back_actions)
    except Exception as e:
        print(f"CENTRIFUGE: 将上清液转移回容器失败: {str(e)}")

    # === 第四步：清洗离心机容器 ===
    print(f"CENTRIFUGE: 清洗离心机容器")
    try:
        # 查找清洗溶剂
        cleaning_solvent = None
        for solvent in ["flask_water", "flask_ethanol", "flask_acetone"]:
            if solvent in G.nodes():
                cleaning_solvent = solvent
                break

        if cleaning_solvent:
            # 用少量溶剂清洗离心管
            cleaning_volume = 5.0  # 5mL清洗
            print(f"CENTRIFUGE: 用 {cleaning_volume} mL {cleaning_solvent} 清洗")

            # 清洗溶剂加入
            cleaning_actions = generate_pump_protocol(
                G=G,
                from_vessel=cleaning_solvent,
                to_vessel=centrifuge_vessel,
                volume=cleaning_volume,
                flowrate=2.0,
                transfer_flowrate=2.0
            )
            action_sequence.extend(cleaning_actions)

            # 将清洗液转移到废液
            if "waste_workup" in G.nodes():
                waste_actions = generate_pump_protocol(
                    G=G,
                    from_vessel=centrifuge_vessel,
                    to_vessel="waste_workup",
                    volume=cleaning_volume,
                    flowrate=2.0,
                    transfer_flowrate=2.0
                )
                action_sequence.extend(waste_actions)

    except Exception as e:
        print(f"CENTRIFUGE: 清洗步骤失败: {str(e)}")

    print(f"CENTRIFUGE: 生成了 {len(action_sequence)} 个动作")
    print(f"CENTRIFUGE: 离心协议生成完成")
    print(f"CENTRIFUGE: 总处理体积: {transfer_volume} mL")

    return action_sequence


# 便捷函数：常用离心方案
def generate_low_speed_centrifuge_protocol(
    G: nx.DiGraph,
    vessel: str,
    time: float = 300.0  # 5分钟
) -> List[Dict[str, Any]]:
    """低速离心：细胞分离或大颗粒沉淀"""
    return generate_centrifuge_protocol(G, vessel, 1000.0, time, 4.0)


def generate_high_speed_centrifuge_protocol(
    G: nx.DiGraph,
    vessel: str,
    time: float = 600.0  # 10分钟
) -> List[Dict[str, Any]]:
    """高速离心：蛋白质沉淀或小颗粒分离"""
    return generate_centrifuge_protocol(G, vessel, 12000.0, time, 4.0)


def generate_standard_centrifuge_protocol(
    G: nx.DiGraph,
    vessel: str,
    time: float = 600.0  # 10分钟
) -> List[Dict[str, Any]]:
    """标准离心：常规样品处理"""
    return generate_centrifuge_protocol(G, vessel, 5000.0, time, 25.0)


def generate_cold_centrifuge_protocol(
    G: nx.DiGraph,
    vessel: str,
    speed: float = 5000.0,
    time: float = 600.0
) -> List[Dict[str, Any]]:
    """冷冻离心：热敏感样品处理"""
    return generate_centrifuge_protocol(G, vessel, speed, time, 4.0)


def generate_ultra_centrifuge_protocol(
    G: nx.DiGraph,
    vessel: str,
    time: float = 1800.0  # 30分钟
) -> List[Dict[str, Any]]:
    """超高速离心：超细颗粒分离"""
    return generate_centrifuge_protocol(G, vessel, 15000.0, time, 4.0)
