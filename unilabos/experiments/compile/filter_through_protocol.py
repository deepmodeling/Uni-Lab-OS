from typing import List, Dict, Any
import networkx as nx
from .pump_protocol import generate_pump_protocol
from .utils.vessel_parser import get_vessel


def get_vessel_liquid_volume(G: nx.DiGraph, vessel: str) -> float:
    """获取容器中的液体体积"""
    if vessel not in G.nodes():
        return 0.0
    
    vessel_data = G.nodes[vessel].get('data', {})
    liquids = vessel_data.get('liquid', [])
    
    total_volume = 0.0
    for liquid in liquids:
        if isinstance(liquid, dict) and 'liquid_volume' in liquid:
            total_volume += liquid['liquid_volume']
    
    return total_volume


def find_filter_through_vessel(G: nx.DiGraph, filter_through: str) -> str:
    """查找过滤介质容器"""
    # 直接使用 filter_through 参数作为容器名称
    if filter_through in G.nodes():
        return filter_through
    
    # 尝试常见的过滤介质容器命名
    possible_names = [
        f"filter_{filter_through}",
        f"{filter_through}_filter",
        f"column_{filter_through}",
        f"{filter_through}_column",
        "filter_through_vessel",
        "column_vessel",
        "chromatography_column",
        "filter_column"
    ]
    
    for vessel_name in possible_names:
        if vessel_name in G.nodes():
            return vessel_name
    
    raise ValueError(f"未找到过滤介质容器 '{filter_through}'。尝试了以下名称: {[filter_through] + possible_names}")


def find_eluting_solvent_vessel(G: nx.DiGraph, eluting_solvent: str) -> str:
    """查找洗脱溶剂容器"""
    if not eluting_solvent:
        return ""
    
    # 按照命名规则查找溶剂瓶
    solvent_vessel_id = f"flask_{eluting_solvent}"
    
    if solvent_vessel_id in G.nodes():
        return solvent_vessel_id
    
    # 如果直接匹配失败，尝试模糊匹配
    for node in G.nodes():
        if node.startswith('flask_') and eluting_solvent.lower() in node.lower():
            return node
    
    # 如果还是找不到，列出所有可用的溶剂瓶
    available_flasks = [node for node in G.nodes() 
                       if node.startswith('flask_') 
                       and G.nodes[node].get('type') == 'container']
    
    raise ValueError(f"找不到洗脱溶剂 '{eluting_solvent}' 对应的溶剂瓶。可用溶剂瓶: {available_flasks}")


def generate_filter_through_protocol(
    G: nx.DiGraph,
    from_vessel: dict | str,
    to_vessel: dict | str,
    filter_through: str,
    eluting_solvent: str = "",
    eluting_volume: float = 0.0,
    eluting_repeats: int = 0,
    residence_time: float = 0.0
) -> List[Dict[str, Any]]:
    """
    生成通过过滤介质过滤的协议序列，复用 pump_protocol 的成熟算法
    
    过滤流程：
    1. 液体转移：将样品从源容器转移到过滤介质
    2. 重力过滤：液体通过过滤介质自动流到目标容器
    3. 洗脱操作：将洗脱溶剂通过过滤介质洗脱目标物质
    
    Args:
        G: 有向图，节点为设备和容器，边为流体管道
        from_vessel: 源容器的名称，即物质起始所在的容器
        to_vessel: 目标容器的名称，物质过滤后要到达的容器
        filter_through: 过滤时所通过的介质，如滤纸、柱子等
        eluting_solvent: 洗脱溶剂的名称，可选参数
        eluting_volume: 洗脱溶剂的体积，可选参数
        eluting_repeats: 洗脱操作的重复次数，默认为 0
        residence_time: 物质在过滤介质中的停留时间，可选参数
    
    Returns:
        List[Dict[str, Any]]: 过滤操作的动作序列
    
    Raises:
        ValueError: 当找不到必要的设备或容器时
    
    Examples:
        filter_through_actions = generate_filter_through_protocol(
            G, "reaction_mixture", "collection_bottle_1", "celite", "ethanol", 20.0, 2, 30.0
        )
    """
    action_sequence = []
    # 协议模型的容器参数是 {"id": ...} 字典（也兼容直接给容器 id）
    from_vessel, _ = get_vessel(from_vessel)
    to_vessel, _ = get_vessel(to_vessel)

    print(f"FILTER_THROUGH: 开始生成通过过滤协议")
    print(f"  - 源容器: {from_vessel}")
    print(f"  - 目标容器: {to_vessel}")
    print(f"  - 过滤介质: {filter_through}")
    print(f"  - 洗脱溶剂: {eluting_solvent}")
    print(f"  - 洗脱体积: {eluting_volume} mL" if eluting_volume > 0 else "  - 洗脱体积: 无")
    print(f"  - 洗脱重复次数: {eluting_repeats}")
    print(f"  - 停留时间: {residence_time}s" if residence_time > 0 else "  - 停留时间: 无")
    
    # 验证源容器和目标容器存在
    if from_vessel not in G.nodes():
        raise ValueError(f"源容器 '{from_vessel}' 不存在于系统中")
    
    if to_vessel not in G.nodes():
        raise ValueError(f"目标容器 '{to_vessel}' 不存在于系统中")
    
    # 获取源容器中的液体体积
    source_volume = get_vessel_liquid_volume(G, from_vessel)
    print(f"FILTER_THROUGH: 源容器 {from_vessel} 中有 {source_volume} mL 液体")
    
    # 查找过滤介质容器
    try:
        filter_through_vessel = find_filter_through_vessel(G, filter_through)
        print(f"FILTER_THROUGH: 找到过滤介质容器: {filter_through_vessel}")
    except ValueError as e:
        raise ValueError(f"无法找到过滤介质容器: {str(e)}")
    
    # 查找洗脱溶剂容器（如果需要）
    eluting_vessel = ""
    if eluting_solvent and eluting_volume > 0 and eluting_repeats > 0:
        try:
            eluting_vessel = find_eluting_solvent_vessel(G, eluting_solvent)
            print(f"FILTER_THROUGH: 找到洗脱溶剂容器: {eluting_vessel}")
        except ValueError as e:
            raise ValueError(f"无法找到洗脱溶剂容器: {str(e)}")
    
    # === 第一步：将样品从源容器转移到过滤介质 ===
    transfer_volume = source_volume if source_volume > 0 else 100.0  # 默认100mL
    print(f"FILTER_THROUGH: 将 {transfer_volume} mL 样品从 {from_vessel} 转移到 {filter_through_vessel}")
    
    try:
        # 使用成熟的 pump_protocol 算法进行液体转移
        sample_transfer_actions = generate_pump_protocol(
            G=G,
            from_vessel_id=from_vessel,
            to_vessel_id=filter_through_vessel,
            volume=transfer_volume,
            flowrate=0.8,  # 较慢的流速，避免冲击过滤介质
            transfer_flowrate=1.2
        )
        action_sequence.extend(sample_transfer_actions)
    except Exception as e:
        raise ValueError(f"无法将样品转移到过滤介质: {str(e)}")
    
    # === 第二步：等待样品通过过滤介质（停留时间） ===
    if residence_time > 0:
        print(f"FILTER_THROUGH: 等待样品在过滤介质中停留 {residence_time}s")
        action_sequence.append({
            "action_name": "wait",
            "action_kwargs": {"time": residence_time}
        })
    else:
        # 即使没有指定停留时间，也等待一段时间让液体通过
        default_wait_time = max(10, transfer_volume / 10)  # 根据体积估算等待时间
        print(f"FILTER_THROUGH: 等待样品通过过滤介质 {default_wait_time}s")
        action_sequence.append({
            "action_name": "wait",
            "action_kwargs": {"time": default_wait_time}
        })
    
    # === 第三步：洗脱操作（如果指定了洗脱参数） ===
    if eluting_solvent and eluting_volume > 0 and eluting_repeats > 0 and eluting_vessel:
        print(f"FILTER_THROUGH: 开始洗脱操作 - {eluting_repeats} 次，每次 {eluting_volume} mL {eluting_solvent}")
        
        for repeat_idx in range(eluting_repeats):
            print(f"FILTER_THROUGH: 第 {repeat_idx + 1}/{eluting_repeats} 次洗脱")
            
            try:
                # 将洗脱溶剂转移到过滤介质
                eluting_transfer_actions = generate_pump_protocol(
                    G=G,
                    from_vessel_id=eluting_vessel,
                    to_vessel_id=filter_through_vessel,
                    volume=eluting_volume,
                    flowrate=0.6,  # 洗脱用更慢的流速
                    transfer_flowrate=1.0
                )
                action_sequence.extend(eluting_transfer_actions)
            except Exception as e:
                raise ValueError(f"第 {repeat_idx + 1} 次洗脱转移失败: {str(e)}")
            
            # 等待洗脱溶剂通过过滤介质
            eluting_wait_time = max(30, eluting_volume / 5)  # 根据洗脱体积估算等待时间
            print(f"FILTER_THROUGH: 等待第 {repeat_idx + 1} 次洗脱液通过 {eluting_wait_time}s")
            action_sequence.append({
                "action_name": "wait",
                "action_kwargs": {"time": eluting_wait_time}
            })
            
            # 洗脱间隔等待
            if repeat_idx < eluting_repeats - 1:  # 不是最后一次洗脱
                action_sequence.append({
                    "action_name": "wait",
                    "action_kwargs": {"time": 10}
                })
    
    # === 第四步：最终等待，确保所有液体完全通过 ===
    print(f"FILTER_THROUGH: 最终等待，确保所有液体完全通过过滤介质")
    action_sequence.append({
        "action_name": "wait",
        "action_kwargs": {"time": 20}
    })
    
    print(f"FILTER_THROUGH: 生成了 {len(action_sequence)} 个动作")
    print(f"FILTER_THROUGH: 通过过滤协议生成完成")
    print(f"FILTER_THROUGH: 样品从 {from_vessel} 通过 {filter_through} 到达 {to_vessel}")
    if eluting_repeats > 0:
        total_eluting_volume = eluting_volume * eluting_repeats
        print(f"FILTER_THROUGH: 总洗脱体积: {total_eluting_volume} mL {eluting_solvent}")
    
    return action_sequence


# 便捷函数：常用过滤方案
# 测试函数
def test_filter_through_protocol():
    """测试通过过滤协议的示例"""
    print("=== FILTER THROUGH PROTOCOL 测试 ===")
    print("测试完成")


if __name__ == "__main__":
    test_filter_through_protocol()
