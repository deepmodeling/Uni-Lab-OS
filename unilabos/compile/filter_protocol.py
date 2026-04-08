from typing import List, Dict, Any, Optional
import networkx as nx
import logging
from .utils.vessel_parser import get_vessel
from .utils.logger_util import debug_print
from .pump_protocol import generate_pump_protocol_with_rinsing

logger = logging.getLogger(__name__)

def find_filter_device(G: nx.DiGraph) -> str:
    """查找过滤器设备"""
    for node in G.nodes():
        node_data = G.nodes[node]
        node_class = node_data.get('class', '') or ''

        if 'filter' in node_class.lower() or 'filter' in node.lower():
            debug_print(f"找到过滤器设备: {node}")
            return node

    possible_names = ["filter", "filter_1", "virtual_filter", "filtration_unit"]
    for name in possible_names:
        if name in G.nodes():
            debug_print(f"找到过滤器设备: {name}")
            return name

    raise ValueError("未找到过滤器设备")

def validate_vessel(G: nx.DiGraph, vessel: str, vessel_type: str = "容器") -> None:
    """验证容器是否存在"""
    if not vessel:
        raise ValueError(f"{vessel_type}不能为空")

    if vessel not in G.nodes():
        raise ValueError(f"{vessel_type} '{vessel}' 不存在于系统中")

def generate_filter_protocol(
    G: nx.DiGraph,
    vessel: dict,
    filtrate_vessel: dict = {"id": "waste"},
    **kwargs
) -> List[Dict[str, Any]]:
    """
    生成过滤操作的协议序列 - 支持体积运算

    Args:
        G: 设备图
        vessel: 过滤容器字典（必需）- 包含需要过滤的混合物
        filtrate_vessel: 滤液容器名称（可选）- 如果提供则收集滤液
        **kwargs: 其他参数（兼容性）

    Returns:
        List[Dict[str, Any]]: 过滤操作的动作序列
    """

    vessel_id, vessel_data = get_vessel(vessel)
    filtrate_vessel_id, filtrate_vessel_data = get_vessel(filtrate_vessel)

    debug_print(f"开始生成过滤协议: vessel={vessel_id}, filtrate_vessel={filtrate_vessel_id}")

    action_sequence = []

    # 记录过滤前的容器状态
    original_liquid_volume = 0.0
    if "data" in vessel and "liquid_volume" in vessel["data"]:
        current_volume = vessel["data"]["liquid_volume"]
        if isinstance(current_volume, list) and len(current_volume) > 0:
            original_liquid_volume = current_volume[0]
        elif isinstance(current_volume, (int, float)):
            original_liquid_volume = current_volume

    # === 参数验证 ===
    validate_vessel(G, vessel_id, "过滤容器")

    if filtrate_vessel:
        validate_vessel(G, filtrate_vessel_id, "滤液容器")

    # === 查找设备 ===
    try:
        filter_device = find_filter_device(G)
        debug_print(f"使用过滤器设备: {filter_device}")
    except Exception as e:
        raise ValueError(f"设备查找失败: {str(e)}")

    # 过滤体积分配估算
    solid_ratio = 0.1
    liquid_ratio = 0.9
    volume_loss_ratio = 0.05

    if "solid_content" in kwargs:
        try:
            solid_ratio = float(kwargs["solid_content"])
            liquid_ratio = 1.0 - solid_ratio
        except:
            pass

    if original_liquid_volume > 0:
        expected_filtrate_volume = original_liquid_volume * liquid_ratio * (1.0 - volume_loss_ratio)
        expected_solid_volume = original_liquid_volume * solid_ratio
        volume_loss = original_liquid_volume * volume_loss_ratio

    # === 转移到过滤器（如果需要）===
    if vessel_id != filter_device:
        try:
            transfer_actions = generate_pump_protocol_with_rinsing(
                G=G,
                from_vessel={"id": vessel_id},
                to_vessel={"id": filter_device},
                volume=0.0,
                amount="",
                time=0.0,
                viscous=False,
                rinsing_solvent="",
                rinsing_volume=0.0,
                rinsing_repeats=0,
                solid=False,
                flowrate=2.0,
                transfer_flowrate=2.0
            )

            if transfer_actions:
                action_sequence.extend(transfer_actions)
                debug_print(f"添加了 {len(transfer_actions)} 个转移动作")

                # 更新容器体积
                if "data" in vessel and "liquid_volume" in vessel["data"]:
                    current_volume = vessel["data"]["liquid_volume"]
                    if isinstance(current_volume, list):
                        vessel["data"]["liquid_volume"] = [0.0] if len(current_volume) > 0 else [0.0]
                    else:
                        vessel["data"]["liquid_volume"] = 0.0

                if vessel_id in G.nodes():
                    if 'data' not in G.nodes[vessel_id]:
                        G.nodes[vessel_id]['data'] = {}
                    G.nodes[vessel_id]['data']['liquid_volume'] = 0.0

        except Exception as e:
            debug_print(f"转移失败: {str(e)}，继续执行")

    # === 执行过滤操作 ===
    filter_kwargs = {
        "vessel": {"id": filter_device},
        "filtrate_vessel": {"id": filtrate_vessel_id},
        "stir": kwargs.get("stir", False),
        "stir_speed": kwargs.get("stir_speed", 0.0),
        "temp": kwargs.get("temp", 25.0),
        "continue_heatchill": kwargs.get("continue_heatchill", False),
        "volume": kwargs.get("volume", 0.0)
    }

    filter_action = {
        "device_id": filter_device,
        "action_name": "filter",
        "action_kwargs": filter_kwargs
    }
    action_sequence.append(filter_action)

    # 过滤后等待
    action_sequence.append({
        "action_name": "wait",
        "action_kwargs": {"time": 10.0}
    })

    # === 收集滤液（如果需要）===
    if filtrate_vessel_id and filtrate_vessel_id not in G.neighbors(filter_device):
        try:
            collect_actions = generate_pump_protocol_with_rinsing(
                G=G,
                from_vessel=filter_device,
                to_vessel=filtrate_vessel,
                volume=0.0,
                amount="",
                time=0.0,
                viscous=False,
                rinsing_solvent="",
                rinsing_volume=0.0,
                rinsing_repeats=0,
                solid=False,
                flowrate=2.0,
                transfer_flowrate=2.0
            )

            if collect_actions:
                action_sequence.extend(collect_actions)

                # 更新滤液容器体积
                if filtrate_vessel_id in G.nodes():
                    if 'data' not in G.nodes[filtrate_vessel_id]:
                        G.nodes[filtrate_vessel_id]['data'] = {}

                    current_filtrate_volume = G.nodes[filtrate_vessel_id]['data'].get('liquid_volume', 0.0)
                    if isinstance(current_filtrate_volume, list):
                        if len(current_filtrate_volume) > 0:
                            G.nodes[filtrate_vessel_id]['data']['liquid_volume'][0] += expected_filtrate_volume
                        else:
                            G.nodes[filtrate_vessel_id]['data']['liquid_volume'] = [expected_filtrate_volume]
                    else:
                        G.nodes[filtrate_vessel_id]['data']['liquid_volume'] = current_filtrate_volume + expected_filtrate_volume

        except Exception as e:
            debug_print(f"收集滤液失败: {str(e)}，继续执行")

    # 过滤完成后容器状态更新
    if vessel_id == filter_device:
        if original_liquid_volume > 0:
            if filtrate_vessel:
                remaining_volume = expected_solid_volume
            else:
                remaining_volume = original_liquid_volume * (1.0 - volume_loss_ratio)

            if "data" in vessel and "liquid_volume" in vessel["data"]:
                current_volume = vessel["data"]["liquid_volume"]
                if isinstance(current_volume, list):
                    vessel["data"]["liquid_volume"] = [remaining_volume] if len(current_volume) > 0 else [remaining_volume]
                else:
                    vessel["data"]["liquid_volume"] = remaining_volume

            if vessel_id in G.nodes():
                if 'data' not in G.nodes[vessel_id]:
                    G.nodes[vessel_id]['data'] = {}
                G.nodes[vessel_id]['data']['liquid_volume'] = remaining_volume

    # === 最终等待 ===
    action_sequence.append({
        "action_name": "wait",
        "action_kwargs": {"time": 5.0}
    })

    # 最终状态
    final_vessel_volume = 0.0
    if "data" in vessel and "liquid_volume" in vessel["data"]:
        current_volume = vessel["data"]["liquid_volume"]
        if isinstance(current_volume, list) and len(current_volume) > 0:
            final_vessel_volume = current_volume[0]
        elif isinstance(current_volume, (int, float)):
            final_vessel_volume = current_volume

    debug_print(f"过滤协议生成完成: {len(action_sequence)} 个动作, 容器={vessel_id}, 过滤器={filter_device}")

    return action_sequence
