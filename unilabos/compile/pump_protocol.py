import traceback
import numpy as np
import networkx as nx
import asyncio
import time as time_module  # 重命名time模块
from typing import List, Dict, Any
import logging
import sys

from .utils.logger_util import debug_print
from .utils.vessel_parser import get_vessel
from .utils.resource_helper import get_resource_liquid_volume

logger = logging.getLogger(__name__)


def is_integrated_pump(node_class: str, node_name: str = "") -> bool:
    """
    判断是否为泵阀一体设备
    """
    class_lower = (node_class or "").lower()
    name_lower = (node_name or "").lower()

    if "pump" not in class_lower and "pump" not in name_lower:
        return False

    integrated_markers = [
        "valve",
        "pump_valve",
        "pumpvalve",
        "integrated",
        "transfer_pump",
    ]

    for marker in integrated_markers:
        if marker in class_lower or marker in name_lower:
            return True

    return False


def find_connected_pump(G, valve_node):
    """
    查找与阀门相连的泵节点
    区分电磁阀和多通阀，电磁阀不参与泵查找
    """
    # 检查节点类型，电磁阀不应该查找泵
    node_data = G.nodes.get(valve_node, {})
    node_class = node_data.get("class", "") or ""

    # 如果是电磁阀，不应该查找泵（电磁阀只是开关）
    if ("solenoid" in node_class.lower() or "solenoid_valve" in valve_node.lower()):
        raise ValueError(f"电磁阀 {valve_node} 不应该参与泵查找逻辑")

    # 只有多通阀等复杂阀门才需要查找连接的泵
    if ("multiway" in node_class.lower() or "valve" in node_class.lower()):
        # 方法1：直接相邻的泵
        for neighbor in G.neighbors(valve_node):
            neighbor_class = G.nodes[neighbor].get("class", "") or ""
            if "pump" in neighbor_class.lower():
                return neighbor

        # 方法2：通过路径查找泵（最多2跳）
        pump_nodes = [
            node_id for node_id in G.nodes()
            if "pump" in (G.nodes[node_id].get("class", "") or "").lower()
        ]

        for pump_node in pump_nodes:
            try:
                if nx.has_path(G, valve_node, pump_node):
                    path = nx.shortest_path(G, valve_node, pump_node)
                    if len(path) - 1 <= 2:  # 最多允许2跳
                        return pump_node
            except nx.NetworkXNoPath:
                continue

    raise ValueError(f"未找到与阀 {valve_node} 相连的泵节点")


def build_pump_valve_maps(G, pump_backbone):
    """
    构建泵-阀门映射
    过滤掉电磁阀，只处理需要泵的多通阀
    """
    pumps_from_node = {}
    valve_from_node = {}

    # 过滤掉电磁阀
    filtered_backbone = []
    for node in pump_backbone:
        node_data = G.nodes.get(node, {})
        node_class = node_data.get("class", "") or ""

        if ("solenoid" in node_class.lower() or "solenoid_valve" in node.lower()):
            continue

        filtered_backbone.append(node)

    for node in filtered_backbone:
        node_data = G.nodes.get(node, {})
        node_class = node_data.get("class", "") or ""
        if is_integrated_pump(node_class, node):
            pumps_from_node[node] = node
            valve_from_node[node] = node
        else:
            try:
                pump_node = find_connected_pump(G, node)
                pumps_from_node[node] = pump_node
                valve_from_node[node] = node
            except ValueError:
                continue

    debug_print(f"泵-阀映射: pumps={pumps_from_node}, valves={valve_from_node}")
    return pumps_from_node, valve_from_node


def generate_pump_protocol(
        G: nx.DiGraph,
        from_vessel_id: str,
        to_vessel_id: str,
        volume: float,
        flowrate: float = 2.5,
        transfer_flowrate: float = 0.5,
) -> List[Dict[str, Any]]:
    """
    生成泵操作的动作序列
    正确处理包含电磁阀的路径
    """
    pump_action_sequence = []
    nodes = G.nodes(data=True)

    # 验证输入参数
    if volume <= 0:
        logger.error(f"无效的体积参数: {volume}mL")
        return pump_action_sequence

    if flowrate <= 0:
        flowrate = 2.5
        logger.warning(f"flowrate <= 0，使用默认值 {flowrate}mL/s")

    if transfer_flowrate <= 0:
        transfer_flowrate = 0.5
        logger.warning(f"transfer_flowrate <= 0，使用默认值 {transfer_flowrate}mL/s")

    # 验证容器存在
    if from_vessel_id not in G.nodes():
        logger.error(f"源容器 '{from_vessel_id}' 不存在")
        return pump_action_sequence

    if to_vessel_id not in G.nodes():
        logger.error(f"目标容器 '{to_vessel_id}' 不存在")
        return pump_action_sequence

    try:
        shortest_path = nx.shortest_path(G, source=from_vessel_id, target=to_vessel_id)
        debug_print(f"PUMP_TRANSFER: 路径 {from_vessel_id} -> {to_vessel_id}: {shortest_path}")
    except nx.NetworkXNoPath:
        logger.error(f"无法找到从 '{from_vessel_id}' 到 '{to_vessel_id}' 的路径")
        return pump_action_sequence

    # 正确构建泵骨架，排除容器和电磁阀
    pump_backbone = []
    for node in shortest_path:
        if node == from_vessel_id or node == to_vessel_id:
            continue

        node_data = G.nodes.get(node, {})
        node_class = node_data.get("class", "") or ""
        if ("solenoid" in node_class.lower() or "solenoid_valve" in node.lower()):
            continue

        if ("multiway" in node_class.lower() or "valve" in node_class.lower() or "pump" in node_class.lower()):
            pump_backbone.append(node)

    debug_print(f"PUMP_TRANSFER: 泵骨架: {pump_backbone}")

    if not pump_backbone:
        debug_print("PUMP_TRANSFER: 没有泵骨架节点")
        return pump_action_sequence

    if transfer_flowrate == 0:
        transfer_flowrate = flowrate

    try:
        pumps_from_node, valve_from_node = build_pump_valve_maps(G, pump_backbone)
    except Exception as e:
        debug_print(f"PUMP_TRANSFER: 构建泵-阀门映射失败: {str(e)}")
        return pump_action_sequence

    if not pumps_from_node:
        debug_print("PUMP_TRANSFER: 没有可用的泵映射")
        return pump_action_sequence

    # 安全地获取最小转移体积
    try:
        min_transfer_volumes = []
        for node in pump_backbone:
            if node in pumps_from_node:
                pump_node = pumps_from_node[node]
                if pump_node in nodes:
                    pump_config = nodes[pump_node].get("config", {})
                    max_volume = pump_config.get("max_volume")
                    if max_volume is not None:
                        min_transfer_volumes.append(max_volume)

        if min_transfer_volumes:
            min_transfer_volume = min(min_transfer_volumes)
        else:
            min_transfer_volume = 25.0  # 默认值
            debug_print(f"PUMP_TRANSFER: 无法获取泵的最大体积，使用默认值: {min_transfer_volume}mL")
    except Exception as e:
        debug_print(f"PUMP_TRANSFER: 获取最小转移体积失败: {str(e)}")
        min_transfer_volume = 25.0  # 默认值

    repeats = int(np.ceil(volume / min_transfer_volume))

    if repeats > 1 and (from_vessel_id.startswith("pump") or to_vessel_id.startswith("pump")):
        logger.error("Cannot transfer volume larger than min_transfer_volume between two pumps.")
        return pump_action_sequence

    volume_left = volume
    debug_print(f"PUMP_TRANSFER: 需要 {repeats} 次转移，单次最大体积 {min_transfer_volume} mL")

    # 只在开头打印总体概览
    if repeats > 1:
        debug_print(f"分批转移: 总体积 {volume:.2f}mL, {repeats} 次, 单次最大 {min_transfer_volume} mL")
        logger.info(f"分批转移: 总体积 {volume:.2f}mL, {repeats} 次转移")

    # 创建一个自定义的wait动作，用于在执行时打印日志
    def create_progress_log_action(message: str) -> Dict[str, Any]:
        """创建一个特殊的等待动作，在执行时打印进度日志"""
        return {
            "action_name": "wait",
            "action_kwargs": {
                "time": 0.1,
                "progress_message": message
            }
        }

    # 生成泵操作序列
    for i in range(repeats):
        current_volume = min(volume_left, min_transfer_volume)

        if repeats > 1:
            pump_action_sequence.append(create_progress_log_action(
                f"第 {i + 1}/{repeats} 次转移: {current_volume:.2f}mL ({from_vessel_id} -> {to_vessel_id})"
            ))

        # 安全地获取边数据
        def get_safe_edge_data(node_a, node_b, key):
            try:
                edge_data = G.get_edge_data(node_a, node_b)
                if edge_data and "port" in edge_data:
                    port_data = edge_data["port"]
                    if isinstance(port_data, dict) and key in port_data:
                        return port_data[key]
                return "default"
            except Exception as e:
                debug_print(f"PUMP_TRANSFER: 获取边数据失败 {node_a}->{node_b}: {str(e)}")
                return "default"

        # 从源容器吸液
        if not from_vessel_id.startswith("pump") and pump_backbone:
            first_pump_node = pump_backbone[0]
            if first_pump_node in valve_from_node and first_pump_node in pumps_from_node:
                port_command = get_safe_edge_data(first_pump_node, from_vessel_id, first_pump_node)
                pump_action_sequence.extend([
                    {
                        "device_id": valve_from_node[first_pump_node],
                        "action_name": "set_valve_position",
                        "action_kwargs": {
                            "command": port_command
                        }
                    },
                    {
                        "device_id": pumps_from_node[first_pump_node],
                        "action_name": "set_position",
                        "action_kwargs": {
                            "position": float(current_volume),
                            "max_velocity": transfer_flowrate
                        }
                    }
                ])
                pump_action_sequence.append({"action_name": "wait", "action_kwargs": {"time": 3}})

        # 泵间转移
        for nodeA, nodeB in zip(pump_backbone[:-1], pump_backbone[1:]):
            if nodeA in valve_from_node and nodeB in valve_from_node and nodeA in pumps_from_node and nodeB in pumps_from_node:
                port_a = get_safe_edge_data(nodeA, nodeB, nodeA)
                port_b = get_safe_edge_data(nodeB, nodeA, nodeB)

                pump_action_sequence.append([
                    {
                        "device_id": valve_from_node[nodeA],
                        "action_name": "set_valve_position",
                        "action_kwargs": {
                            "command": port_a
                        }
                    },
                    {
                        "device_id": valve_from_node[nodeB],
                        "action_name": "set_valve_position",
                        "action_kwargs": {
                            "command": port_b
                        }
                    }
                ])
                pump_action_sequence.append([
                    {
                        "device_id": pumps_from_node[nodeA],
                        "action_name": "set_position",
                        "action_kwargs": {
                            "position": 0.0,
                            "max_velocity": transfer_flowrate
                        }
                    },
                    {
                        "device_id": pumps_from_node[nodeB],
                        "action_name": "set_position",
                        "action_kwargs": {
                            "position": float(current_volume),
                            "max_velocity": transfer_flowrate
                        }
                    }
                ])
                pump_action_sequence.append({"action_name": "wait", "action_kwargs": {"time": 3}})

        # 排液到目标容器
        if not to_vessel_id.startswith("pump") and pump_backbone:
            last_pump_node = pump_backbone[-1]
            if last_pump_node in valve_from_node and last_pump_node in pumps_from_node:
                port_command = get_safe_edge_data(last_pump_node, to_vessel_id, last_pump_node)
                pump_action_sequence.extend([
                    {
                        "device_id": valve_from_node[last_pump_node],
                        "action_name": "set_valve_position",
                        "action_kwargs": {
                            "command": port_command
                        }
                    },
                    {
                        "device_id": pumps_from_node[last_pump_node],
                        "action_name": "set_position",
                        "action_kwargs": {
                            "position": 0.0,
                            "max_velocity": flowrate
                        }
                    }
                ])
                pump_action_sequence.append({"action_name": "wait", "action_kwargs": {"time": 3}})

        # 在每次循环结束时添加完成日志
        if repeats > 1:
            remaining_volume = volume_left - current_volume
            if remaining_volume > 0:
                end_message = f"第 {i + 1}/{repeats} 次完成, 剩余 {remaining_volume:.2f}mL"
            else:
                end_message = f"第 {i + 1}/{repeats} 次完成, 全部 {volume:.2f}mL 转移完毕"

            pump_action_sequence.append(create_progress_log_action(end_message))

        volume_left -= current_volume

    return pump_action_sequence


# 保持原有的同步版本兼容性
def generate_pump_protocol_with_rinsing(
        G: nx.DiGraph,
        from_vessel: dict,
        to_vessel: dict,
        volume: float = 0.0,
        amount: str = "",
        time: float = 0.0,
        viscous: bool = False,
        rinsing_solvent: str = "",
        rinsing_volume: float = 0.0,
        rinsing_repeats: int = 0,
        solid: bool = False,
        flowrate: float = 2.5,
        transfer_flowrate: float = 0.5,
        rate_spec: str = "",
        event: str = "",
        through: str = "",
        **kwargs
) -> List[Dict[str, Any]]:
    """
    原有的同步版本，添加防冲突机制
    """

    # 添加执行锁，防止并发调用
    import threading
    if not hasattr(generate_pump_protocol_with_rinsing, '_lock'):
        generate_pump_protocol_with_rinsing._lock = threading.Lock()

    from_vessel_id, _ = get_vessel(from_vessel)
    to_vessel_id, _ = get_vessel(to_vessel)

    with generate_pump_protocol_with_rinsing._lock:
        debug_print(f"PUMP_TRANSFER: {from_vessel_id} -> {to_vessel_id}, volume={volume}, flowrate={flowrate}")

        # 短暂延迟，避免快速重复调用
        time_module.sleep(0.01)

        # 1. 处理体积参数
        final_volume = volume

        # 如果volume为0，从容器读取实际体积
        if volume == 0.0:

            actual_volume = get_resource_liquid_volume(G.nodes.get(from_vessel_id, {}))

            if actual_volume > 0:
                final_volume = actual_volume
            else:
                final_volume = 10.0
                logger.warning(f"无法从容器读取体积，使用默认值: {final_volume}mL")
        # 处理 amount 参数
        if amount and amount.strip():
            parsed_volume = _parse_amount_to_volume(amount)

            if parsed_volume > 0:
                final_volume = parsed_volume
            elif parsed_volume == 0.0 and amount.lower().strip() == "all":
                actual_volume = get_resource_liquid_volume(G.nodes.get(from_vessel_id, {}))
                if actual_volume > 0:
                    final_volume = actual_volume

        # 最终体积验证
        if final_volume <= 0:
            logger.error(f"体积无效: {final_volume}mL")
            final_volume = 10.0
            logger.warning(f"强制设置为默认值: {final_volume}mL")

        debug_print(f"最终体积: {final_volume}mL")

        # 2. 处理流速参数
        final_flowrate = flowrate if flowrate > 0 else 2.5
        final_transfer_flowrate = transfer_flowrate if transfer_flowrate > 0 else 0.5

        if flowrate <= 0:
            logger.warning(f"flowrate <= 0，修正为: {final_flowrate}mL/s")
        if transfer_flowrate <= 0:
            logger.warning(f"transfer_flowrate <= 0，修正为: {final_transfer_flowrate}mL/s")

        # 3. 根据时间计算流速
        if time > 0 and final_volume > 0:
            calculated_flowrate = final_volume / time

            if flowrate <= 0 or flowrate == 2.5:
                final_flowrate = min(calculated_flowrate, 10.0)
            if transfer_flowrate <= 0 or transfer_flowrate == 0.5:
                final_transfer_flowrate = min(calculated_flowrate, 5.0)

        # 4. 根据速度规格调整
        if rate_spec:
            if rate_spec == "dropwise":
                final_flowrate = min(final_flowrate, 0.1)
                final_transfer_flowrate = min(final_transfer_flowrate, 0.1)
            elif rate_spec == "slowly":
                final_flowrate = min(final_flowrate, 0.5)
                final_transfer_flowrate = min(final_transfer_flowrate, 0.3)
            elif rate_spec == "quickly":
                final_flowrate = max(final_flowrate, 5.0)
                final_transfer_flowrate = max(final_transfer_flowrate, 2.0)
            debug_print(f"速度规格 '{rate_spec}': flowrate={final_flowrate}, transfer={final_transfer_flowrate}")

    # 5. 处理冲洗参数
    final_rinsing_solvent = rinsing_solvent
    final_rinsing_volume = rinsing_volume if rinsing_volume > 0 else 5.0
    final_rinsing_repeats = rinsing_repeats if rinsing_repeats > 0 else 2

    if rinsing_volume <= 0:
        logger.warning(f"rinsing_volume <= 0，修正为: {final_rinsing_volume}mL")
    if rinsing_repeats <= 0:
        logger.warning(f"rinsing_repeats <= 0，修正为: {final_rinsing_repeats}次")

    # 根据物理属性调整冲洗参数
    if viscous or solid:
        final_rinsing_repeats = max(final_rinsing_repeats, 3)
        final_rinsing_volume = max(final_rinsing_volume, 10.0)

    # 参数总结
    debug_print(f"最终参数: volume={final_volume}mL, flowrate={final_flowrate}mL/s, "
                f"transfer_flowrate={final_transfer_flowrate}mL/s, "
                f"rinsing={final_rinsing_solvent}/{final_rinsing_volume}mL/{final_rinsing_repeats}次")

    # 执行基础转移
    try:

        pump_action_sequence = generate_pump_protocol(
            G, from_vessel_id, to_vessel_id, final_volume,
            final_flowrate, final_transfer_flowrate
        )

        debug_print(f"基础转移生成了 {len(pump_action_sequence)} 个动作")

        if not pump_action_sequence:
            debug_print("基础转移协议为空")

            if from_vessel_id in G.nodes() and to_vessel_id in G.nodes():
                try:
                    path = nx.shortest_path(G, source=from_vessel_id, target=to_vessel_id)
                    debug_print(f"路径存在: {path}")
                except Exception:
                    pass

            return [
                {
                    "device_id": "system",
                    "action_name": "log_message",
                    "action_kwargs": {
                        "message": f"路径问题，无法转移: {final_volume}mL 从 {from_vessel_id} 到 {to_vessel_id}"
                    }
                }
            ]

    except Exception as e:
        debug_print(f"基础转移失败: {str(e)}\n{traceback.format_exc()}")
        return [
            {
                "device_id": "system",
                "action_name": "log_message",
                "action_kwargs": {
                    "message": f"转移失败: {final_volume}mL 从 {from_vessel_id} 到 {to_vessel_id}, 错误: {str(e)}"
                }
            }
        ]

    # 执行冲洗操作
    if final_rinsing_solvent and final_rinsing_solvent.strip() and final_rinsing_repeats > 0:

        try:
            if final_rinsing_solvent.strip() != "air":
                rinsing_actions = _generate_rinsing_sequence(
                    G, from_vessel_id, to_vessel_id, final_rinsing_solvent,
                    final_rinsing_volume, final_rinsing_repeats,
                    final_flowrate, final_transfer_flowrate
                )
                pump_action_sequence.extend(rinsing_actions)
            else:
                air_rinsing_actions = _generate_air_rinsing_sequence(
                    G, from_vessel_id, to_vessel_id, final_rinsing_volume, final_rinsing_repeats,
                    final_flowrate, final_transfer_flowrate
                )
                pump_action_sequence.extend(air_rinsing_actions)
        except Exception as e:
            debug_print(f"冲洗操作失败: {str(e)}")
    else:
        debug_print(f"跳过冲洗 (solvent='{final_rinsing_solvent}', repeats={final_rinsing_repeats})")

    # 最终结果
    debug_print(f"PUMP_TRANSFER 完成: {from_vessel_id} -> {to_vessel_id}, "
                f"volume={final_volume}mL, 动作数={len(pump_action_sequence)}")

    # 最终验证
    if len(pump_action_sequence) == 0:
        return [
            {
                "device_id": "system",
                "action_name": "log_message",
                "action_kwargs": {
                    "message": "协议生成失败: 无法生成任何动作序列"
                }
            }
        ]

    return pump_action_sequence


def _parse_amount_to_volume(amount: str) -> float:
    """解析 amount 字符串为体积"""
    if not amount:
        return 0.0

    amount = amount.lower().strip()

    # 处理特殊关键词
    if amount == "all":
        return 0.0  # 返回0.0，让调用者处理

    # 提取数字
    import re
    numbers = re.findall(r'[\d.]+', amount)

    if numbers:
        volume = float(numbers[0])

        # 单位转换
        if 'ml' in amount or 'milliliter' in amount:
            return volume
        elif 'l' in amount and 'ml' not in amount:
            return volume * 1000
        elif 'μl' in amount or 'microliter' in amount:
            return volume / 1000
        else:
            return volume  # 默认mL

    return 0.0


def _generate_rinsing_sequence(
    G: nx.DiGraph,
    from_vessel_id: str,
    to_vessel_id: str,
    rinsing_solvent: str,
    rinsing_volume: float,
    rinsing_repeats: int,
    flowrate: float,
    transfer_flowrate: float
) -> List[Dict[str, Any]]:
    """生成冲洗动作序列"""
    rinsing_actions = []

    try:
        shortest_path = nx.shortest_path(G, source=from_vessel_id, target=to_vessel_id)
        pump_backbone = shortest_path[1:-1]

        if not pump_backbone:
            return rinsing_actions

        nodes = G.nodes(data=True)
        pumps_from_node, valve_from_node = build_pump_valve_maps(G, pump_backbone)
        min_transfer_volume = min([nodes[pumps_from_node[node]]["config"]["max_volume"] for node in pump_backbone])

        waste_vessel = "waste_workup"

        # 处理多种溶剂情况
        if "," in rinsing_solvent:
            rinsing_solvents = rinsing_solvent.split(",")
            if len(rinsing_solvents) != rinsing_repeats:
                rinsing_solvents = [rinsing_solvent] * rinsing_repeats
        else:
            rinsing_solvents = [rinsing_solvent] * rinsing_repeats

        for solvent in rinsing_solvents:
            solvent_vessel = f"flask_{solvent.strip()}"

            # 检查溶剂容器是否存在
            if solvent_vessel not in G.nodes():
                logger.warning(f"溶剂容器 {solvent_vessel} 不存在，跳过该溶剂冲洗")
                continue

            # 清洗泵系统
            rinsing_actions.extend(
                generate_pump_protocol(G, solvent_vessel, pump_backbone[0], min_transfer_volume, flowrate,
                                       transfer_flowrate)
            )

            if len(pump_backbone) > 1:
                rinsing_actions.extend(
                    generate_pump_protocol(G, pump_backbone[0], pump_backbone[-1], min_transfer_volume, flowrate,
                                           transfer_flowrate)
                )

            # 排到废液容器
            if waste_vessel in G.nodes():
                rinsing_actions.extend(
                    generate_pump_protocol(G, pump_backbone[-1], waste_vessel, min_transfer_volume, flowrate,
                                           transfer_flowrate)
                )

            # 第一种冲洗溶剂稀释源容器和目标容器
            if solvent == rinsing_solvents[0]:
                rinsing_actions.extend(
                    generate_pump_protocol(G, solvent_vessel, from_vessel_id, rinsing_volume, flowrate,
                                           transfer_flowrate)
                )
                rinsing_actions.extend(
                    generate_pump_protocol(G, solvent_vessel, to_vessel_id, rinsing_volume, flowrate, transfer_flowrate)
                )

    except Exception as e:
        logger.error(f"生成冲洗序列失败: {str(e)}")

    return rinsing_actions


def _generate_air_rinsing_sequence(G: nx.DiGraph, from_vessel_id: str, to_vessel_id: str,
                                   rinsing_volume: float, repeats: int,
                                   flowrate: float, transfer_flowrate: float) -> List[Dict[str, Any]]:
    """生成空气冲洗序列"""
    air_rinsing_actions = []

    try:
        air_vessel = "flask_air"
        if air_vessel not in G.nodes():
            logger.warning("空气容器 flask_air 不存在，跳过空气冲洗")
            return air_rinsing_actions

        for _ in range(repeats):
            # 空气冲洗源容器
            air_rinsing_actions.extend(
                generate_pump_protocol(G, air_vessel, from_vessel_id, rinsing_volume, flowrate, transfer_flowrate)
            )

            # 空气冲洗目标容器
            air_rinsing_actions.extend(
                generate_pump_protocol(G, air_vessel, to_vessel_id, rinsing_volume, flowrate, transfer_flowrate)
            )

    except Exception as e:
        logger.warning(f"空气冲洗失败: {str(e)}")

    return air_rinsing_actions