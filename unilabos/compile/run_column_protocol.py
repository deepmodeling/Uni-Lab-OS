from typing import List, Dict, Any, Union
import networkx as nx
import logging
import re
from .utils.vessel_parser import get_vessel
from .pump_protocol import generate_pump_protocol_with_rinsing

logger = logging.getLogger(__name__)


def debug_print(message):
    """调试输出"""
    logger.info(f"[RUN_COLUMN] {message}")


def parse_percentage(pct_str: str) -> float:
    """
    解析百分比字符串为数值

    Args:
        pct_str: 百分比字符串（如 "40 %", "40%", "40"）

    Returns:
        float: 百分比数值（0-100）
    """
    if not pct_str or not pct_str.strip():
        return 0.0

    pct_str = pct_str.strip().lower()
    debug_print(f"🔍 解析百分比: '{pct_str}'")

    # 移除百分号和空格
    pct_clean = re.sub(r'[%\s]', '', pct_str)

    # 提取数字
    match = re.search(r'([0-9]*\.?[0-9]+)', pct_clean)
    if match:
        value = float(match.group(1))
        debug_print(f"✅ 百分比解析结果: {value}%")
        return value

    debug_print(f"⚠️ 无法解析百分比: '{pct_str}'，返回0.0")
    return 0.0


def parse_ratio(ratio_str: str) -> tuple:
    """
    解析比例字符串为两个数值

    Args:
        ratio_str: 比例字符串（如 "5:95", "1:1", "40:60"）

    Returns:
        tuple: (ratio1, ratio2) 两个比例值
    """
    if not ratio_str or not ratio_str.strip():
        return (50.0, 50.0)  # 默认1:1

    ratio_str = ratio_str.strip()
    debug_print(f"🔍 解析比例: '{ratio_str}'")

    # 支持多种分隔符：: / -
    if ':' in ratio_str:
        parts = ratio_str.split(':')
    elif '/' in ratio_str:
        parts = ratio_str.split('/')
    elif '-' in ratio_str:
        parts = ratio_str.split('-')
    elif 'to' in ratio_str.lower():
        parts = ratio_str.lower().split('to')
    else:
        debug_print(f"⚠️ 无法解析比例格式: '{ratio_str}'，使用默认1:1")
        return (50.0, 50.0)

    if len(parts) >= 2:
        try:
            ratio1 = float(parts[0].strip())
            ratio2 = float(parts[1].strip())
            total = ratio1 + ratio2

            # 转换为百分比
            pct1 = (ratio1 / total) * 100
            pct2 = (ratio2 / total) * 100

            debug_print(f"✅ 比例解析结果: {ratio1}:{ratio2} -> {pct1:.1f}%:{pct2:.1f}%")
            return (pct1, pct2)
        except ValueError as e:
            debug_print(f"⚠️ 比例数值转换失败: {str(e)}")

    debug_print(f"⚠️ 比例解析失败，使用默认1:1")
    return (50.0, 50.0)


def parse_rf_value(rf_str: str) -> float:
    """
    解析Rf值字符串

    Args:
        rf_str: Rf值字符串（如 "0.3", "0.45", "?"）

    Returns:
        float: Rf值（0-1）
    """
    if not rf_str or not rf_str.strip():
        return 0.3  # 默认Rf值

    rf_str = rf_str.strip().lower()
    debug_print(f"🔍 解析Rf值: '{rf_str}'")

    # 处理未知Rf值
    if rf_str in ['?', 'unknown', 'tbd', 'to be determined']:
        default_rf = 0.3
        debug_print(f"❓ 检测到未知Rf值，使用默认值: {default_rf}")
        return default_rf

    # 提取数字
    match = re.search(r'([0-9]*\.?[0-9]+)', rf_str)
    if match:
        value = float(match.group(1))
        # 确保Rf值在0-1范围内
        if value > 1.0:
            value = value / 100.0  # 可能是百分比形式
        value = max(0.0, min(1.0, value))  # 限制在0-1范围
        debug_print(f"✅ Rf值解析结果: {value}")
        return value

    debug_print(f"⚠️ 无法解析Rf值: '{rf_str}'，使用默认值0.3")
    return 0.3


def find_column_device(G: nx.DiGraph) -> str:
    """查找柱层析设备"""
    debug_print("🔍 查找柱层析设备...")

    # 查找虚拟柱设备
    for node in G.nodes():
        node_data = G.nodes[node]
        node_class = node_data.get('class', '') or ''

        if 'virtual_column' in node_class.lower() or 'column' in node_class.lower():
            debug_print(f"🎉 找到柱层析设备: {node} ✨")
            return node

    # 如果没有找到，尝试创建虚拟设备名称
    possible_names = ['column_1', 'virtual_column_1', 'chromatography_column_1']
    for name in possible_names:
        if name in G.nodes():
            debug_print(f"🎉 找到柱设备: {name} ✨")
            return name

    debug_print("⚠️ 未找到柱层析设备，将使用pump protocol直接转移")
    return ""


def find_column_vessel(G: nx.DiGraph, column: str) -> str:
    """查找柱容器"""
    debug_print(f"🔍 查找柱容器: '{column}'")

    # 直接检查column参数是否是容器
    if column in G.nodes():
        node_type = G.nodes[column].get('type', '')
        if node_type == 'container':
            debug_print(f"🎉 找到柱容器: {column} ✨")
            return column

    # 尝试常见的命名规则
    possible_names = [
        f"column_{column}",
        f"{column}_column",
        f"vessel_{column}",
        f"{column}_vessel",
        "column_vessel",
        "chromatography_column",
        "silica_column",
        "preparative_column",
        "column"
    ]

    for vessel_name in possible_names:
        if vessel_name in G.nodes():
            node_type = G.nodes[vessel_name].get('type', '')
            if node_type == 'container':
                debug_print(f"🎉 找到柱容器: {vessel_name} ✨")
                return vessel_name

    debug_print(f"⚠️ 未找到柱容器，将直接在源容器中进行分离")
    return ""


def find_solvent_vessel(G: nx.DiGraph, solvent: str) -> str:
    """查找溶剂容器 - 增强版"""
    if not solvent or not solvent.strip():
        return ""

    solvent = solvent.strip().replace(' ', '_').lower()
    debug_print(f"🔍 查找溶剂容器: '{solvent}'")

    # 🔧 方法1：直接搜索 data.reagent_name
    for node in G.nodes():
        node_data = G.nodes[node].get('data', {})
        node_type = G.nodes[node].get('type', '')

        # 只搜索容器类型的节点
        if node_type == 'container':
            reagent_name = node_data.get('reagent_name', '').lower()
            reagent_config = G.nodes[node].get('config', {}).get('reagent', '').lower()

            # 检查 data.reagent_name 和 config.reagent
            if reagent_name == solvent or reagent_config == solvent:
                debug_print(f"🎉 通过reagent_name找到溶剂容器: {node} (reagent: {reagent_name or reagent_config}) ✨")
                return node

            # 模糊匹配 reagent_name
            if solvent in reagent_name or reagent_name in solvent:
                debug_print(f"🎉 通过reagent_name模糊匹配到溶剂容器: {node} (reagent: {reagent_name}) ✨")
                return node

            if solvent in reagent_config or reagent_config in solvent:
                debug_print(f"🎉 通过config.reagent模糊匹配到溶剂容器: {node} (reagent: {reagent_config}) ✨")
                return node

    # 🔧 方法2：常见的溶剂容器命名规则
    possible_names = [
        f"flask_{solvent}",
        f"bottle_{solvent}",
        f"reagent_{solvent}",
        f"{solvent}_bottle",
        f"{solvent}_flask",
        f"solvent_{solvent}",
        f"reagent_bottle_{solvent}"
    ]

    for vessel_name in possible_names:
        if vessel_name in G.nodes():
            node_type = G.nodes[vessel_name].get('type', '')
            if node_type == 'container':
                debug_print(f"🎉 通过命名规则找到溶剂容器: {vessel_name} ✨")
                return vessel_name

    # 🔧 方法3：节点名称模糊匹配
    for node in G.nodes():
        node_type = G.nodes[node].get('type', '')
        if node_type == 'container':
            if ('flask_' in node or 'bottle_' in node or 'reagent_' in node) and solvent in node.lower():
                debug_print(f"🎉 通过节点名称模糊匹配到溶剂容器: {node} ✨")
                return node

    # 🔧 方法4：特殊溶剂名称映射
    solvent_mapping = {
        'dmf': ['dmf', 'dimethylformamide', 'n,n-dimethylformamide'],
        'ethyl_acetate': ['ethyl_acetate', 'ethylacetate', 'etoac', 'ea'],
        'hexane': ['hexane', 'hexanes', 'n-hexane'],
        'methanol': ['methanol', 'meoh', 'ch3oh'],
        'water': ['water', 'h2o', 'distilled_water'],
        'acetone': ['acetone', 'ch3coch3', '2-propanone'],
        'dichloromethane': ['dichloromethane', 'dcm', 'ch2cl2', 'methylene_chloride'],
        'chloroform': ['chloroform', 'chcl3', 'trichloromethane']
    }

    # 查找映射的同义词
    for canonical_name, synonyms in solvent_mapping.items():
        if solvent in synonyms:
            debug_print(f"🔍 检测到溶剂同义词: '{solvent}' -> '{canonical_name}'")
            return find_solvent_vessel(G, canonical_name)  # 递归搜索

    debug_print(f"⚠️ 未找到溶剂 '{solvent}' 的容器")
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


def calculate_solvent_volumes(total_volume: float, pct1: float, pct2: float) -> tuple:
    """根据百分比计算溶剂体积"""
    volume1 = (total_volume * pct1) / 100.0
    volume2 = (total_volume * pct2) / 100.0

    debug_print(f"🧮 溶剂体积计算: 总体积{total_volume}mL")
    debug_print(f"  - 溶剂1: {pct1}% = {volume1}mL")
    debug_print(f"  - 溶剂2: {pct2}% = {volume2}mL")

    return (volume1, volume2)


def generate_run_column_protocol(
    G: nx.DiGraph,
    from_vessel: dict,  # 🔧 修改：从字符串改为字典类型
    to_vessel: dict,    # 🔧 修改：从字符串改为字典类型
    column: str,
    rf: str = "",
    pct1: str = "",
    pct2: str = "",
    solvent1: str = "",
    solvent2: str = "",
    ratio: str = "",
    **kwargs
) -> List[Dict[str, Any]]:
    """
    生成柱层析分离的协议序列 - 支持vessel字典和体积运算

    Args:
        G: 有向图，节点为设备和容器，边为流体管道
        from_vessel: 源容器字典（从XDL传入）
        to_vessel: 目标容器字典（从XDL传入）
        column: 所使用的柱子的名称（必需）
        rf: Rf值（可选，支持 "?" 表示未知）
        pct1: 第一种溶剂百分比（如 "40 %"，可选）
        pct2: 第二种溶剂百分比（如 "50 %"，可选）
        solvent1: 第一种溶剂名称（可选）
        solvent2: 第二种溶剂名称（可选）
        ratio: 溶剂比例（如 "5:95"，可选，优先级高于pct1/pct2）
        **kwargs: 其他可选参数

    Returns:
        List[Dict[str, Any]]: 柱层析分离操作的动作序列
    """

    # 🔧 核心修改：从字典中提取容器ID
    from_vessel_id, _ = get_vessel(from_vessel)
    to_vessel_id, _ = get_vessel(to_vessel)

    debug_print("🏛️" * 20)
    debug_print("🚀 开始生成柱层析协议（支持vessel字典和体积运算）✨")
    debug_print(f"📝 输入参数:")
    debug_print(f"  🥽 from_vessel: {from_vessel} (ID: {from_vessel_id})")
    debug_print(f"  🥽 to_vessel: {to_vessel} (ID: {to_vessel_id})")
    debug_print(f"  🏛️ column: '{column}'")
    debug_print(f"  📊 rf: '{rf}'")
    debug_print(f"  🧪 溶剂配比: pct1='{pct1}', pct2='{pct2}', ratio='{ratio}'")
    debug_print(f"  🧪 溶剂名称: solvent1='{solvent1}', solvent2='{solvent2}'")
    debug_print("🏛️" * 20)

    action_sequence = []

    # 🔧 新增：记录柱层析前的容器状态
    debug_print("🔍 记录柱层析前容器状态...")
    original_from_volume = get_vessel_liquid_volume(from_vessel)
    original_to_volume = get_vessel_liquid_volume(to_vessel)

    debug_print(f"📊 柱层析前状态:")
    debug_print(f"  - 源容器 {from_vessel_id}: {original_from_volume:.2f}mL")
    debug_print(f"  - 目标容器 {to_vessel_id}: {original_to_volume:.2f}mL")

    # === 参数验证 ===
    debug_print("📍 步骤1: 参数验证...")

    if not from_vessel_id:  # 🔧 使用 from_vessel_id
        raise ValueError("from_vessel 参数不能为空")
    if not to_vessel_id:    # 🔧 使用 to_vessel_id
        raise ValueError("to_vessel 参数不能为空")
    if not column:
        raise ValueError("column 参数不能为空")

    if from_vessel_id not in G.nodes():  # 🔧 使用 from_vessel_id
        raise ValueError(f"源容器 '{from_vessel_id}' 不存在于系统中")
    if to_vessel_id not in G.nodes():    # 🔧 使用 to_vessel_id
        raise ValueError(f"目标容器 '{to_vessel_id}' 不存在于系统中")

    debug_print("✅ 基本参数验证通过")

    # === 参数解析 ===
    debug_print("📍 步骤2: 参数解析...")

    # 解析Rf值
    final_rf = parse_rf_value(rf)
    debug_print(f"🎯 最终Rf值: {final_rf}")

    # 解析溶剂比例（ratio优先级高于pct1/pct2）
    if ratio and ratio.strip():
        final_pct1, final_pct2 = parse_ratio(ratio)
        debug_print(f"📊 使用ratio参数: {final_pct1:.1f}% : {final_pct2:.1f}%")
    else:
        final_pct1 = parse_percentage(pct1) if pct1 else 50.0
        final_pct2 = parse_percentage(pct2) if pct2 else 50.0

        # 如果百分比和不是100%，进行归一化
        total_pct = final_pct1 + final_pct2
        if total_pct == 0:
            final_pct1, final_pct2 = 50.0, 50.0
        elif total_pct != 100.0:
            final_pct1 = (final_pct1 / total_pct) * 100
            final_pct2 = (final_pct2 / total_pct) * 100

        debug_print(f"📊 使用百分比参数: {final_pct1:.1f}% : {final_pct2:.1f}%")

    # 设置默认溶剂（如果未指定）
    final_solvent1 = solvent1.strip() if solvent1 else "ethyl_acetate"
    final_solvent2 = solvent2.strip() if solvent2 else "hexane"

    debug_print(f"🧪 最终溶剂: {final_solvent1} : {final_solvent2}")

    # === 查找设备和容器 ===
    debug_print("📍 步骤3: 查找设备和容器...")

    # 查找柱层析设备
    column_device_id = find_column_device(G)

    # 查找柱容器
    column_vessel = find_column_vessel(G, column)

    # 查找溶剂容器
    solvent1_vessel = find_solvent_vessel(G, final_solvent1)
    solvent2_vessel = find_solvent_vessel(G, final_solvent2)

    debug_print(f"🔧 设备映射:")
    debug_print(f"  - 柱设备: '{column_device_id}'")
    debug_print(f"  - 柱容器: '{column_vessel}'")
    debug_print(f"  - 溶剂1容器: '{solvent1_vessel}'")
    debug_print(f"  - 溶剂2容器: '{solvent2_vessel}'")

    # === 获取源容器体积 ===
    debug_print("📍 步骤4: 获取源容器体积...")

    source_volume = original_from_volume
    if source_volume <= 0:
        source_volume = 50.0  # 默认体积
        debug_print(f"⚠️ 无法获取源容器体积，使用默认值: {source_volume}mL")
    else:
        debug_print(f"✅ 源容器体积: {source_volume}mL")

    # === 计算溶剂体积 ===
    debug_print("📍 步骤5: 计算溶剂体积...")

    # 洗脱溶剂通常是样品体积的2-5倍
    total_elution_volume = source_volume * 3.0
    solvent1_volume, solvent2_volume = calculate_solvent_volumes(
        total_elution_volume, final_pct1, final_pct2
    )

    # === 执行柱层析流程 ===
    debug_print("📍 步骤6: 执行柱层析流程...")

    # 🔧 新增：体积变化跟踪变量
    current_from_volume = source_volume
    current_to_volume = original_to_volume
    current_column_volume = 0.0

    try:
        # 步骤6.1: 样品上柱（如果有独立的柱容器）
        if column_vessel and column_vessel != from_vessel_id:  # 🔧 使用 from_vessel_id
            debug_print(f"📍 6.1: 样品上柱 - {source_volume}mL 从 {from_vessel_id} 到 {column_vessel}")

            try:
                sample_transfer_actions = generate_pump_protocol_with_rinsing(
                    G=G,
                    from_vessel=from_vessel_id,  # 🔧 使用 from_vessel_id
                    to_vessel=column_vessel,
                    volume=source_volume,
                    flowrate=1.0,  # 慢速上柱
                    transfer_flowrate=0.5,
                    rinsing_solvent="",  # 暂不冲洗
                    rinsing_volume=0.0,
                    rinsing_repeats=0
                )
                action_sequence.extend(sample_transfer_actions)
                debug_print(f"✅ 样品上柱完成，添加了 {len(sample_transfer_actions)} 个动作")

                # 🔧 新增：更新体积 - 样品转移到柱上
                current_from_volume = 0.0  # 源容器体积变为0
                current_column_volume = source_volume  # 柱容器体积增加

                update_vessel_volume(from_vessel, G, current_from_volume, "样品上柱后，源容器清空")

                # 如果柱容器在图中，也更新其体积
                if column_vessel in G.nodes():
                    if 'data' not in G.nodes[column_vessel]:
                        G.nodes[column_vessel]['data'] = {}
                    G.nodes[column_vessel]['data']['liquid_volume'] = current_column_volume
                    debug_print(f"📊 柱容器 '{column_vessel}' 体积更新为: {current_column_volume:.2f}mL")

            except Exception as e:
                debug_print(f"⚠️ 样品上柱失败: {str(e)}")

        # 步骤6.2: 添加洗脱溶剂1（如果有溶剂容器）
        if solvent1_vessel and solvent1_volume > 0:
            debug_print(f"📍 6.2: 添加洗脱溶剂1 - {solvent1_volume:.1f}mL {final_solvent1}")

            try:
                target_vessel = column_vessel if column_vessel else from_vessel_id  # 🔧 使用 from_vessel_id
                solvent1_transfer_actions = generate_pump_protocol_with_rinsing(
                    G=G,
                    from_vessel=solvent1_vessel,
                    to_vessel=target_vessel,
                    volume=solvent1_volume,
                    flowrate=2.0,
                    transfer_flowrate=1.0
                )
                action_sequence.extend(solvent1_transfer_actions)
                debug_print(f"✅ 溶剂1添加完成，添加了 {len(solvent1_transfer_actions)} 个动作")

                # 🔧 新增：更新体积 - 添加溶剂1
                if target_vessel == column_vessel:
                    current_column_volume += solvent1_volume
                    if column_vessel in G.nodes():
                        G.nodes[column_vessel]['data']['liquid_volume'] = current_column_volume
                        debug_print(f"📊 柱容器体积增加: +{solvent1_volume:.2f}mL = {current_column_volume:.2f}mL")
                elif target_vessel == from_vessel_id:
                    current_from_volume += solvent1_volume
                    update_vessel_volume(from_vessel, G, current_from_volume, "添加溶剂1后")

            except Exception as e:
                debug_print(f"⚠️ 溶剂1添加失败: {str(e)}")

        # 步骤6.3: 添加洗脱溶剂2（如果有溶剂容器）
        if solvent2_vessel and solvent2_volume > 0:
            debug_print(f"📍 6.3: 添加洗脱溶剂2 - {solvent2_volume:.1f}mL {final_solvent2}")

            try:
                target_vessel = column_vessel if column_vessel else from_vessel_id  # 🔧 使用 from_vessel_id
                solvent2_transfer_actions = generate_pump_protocol_with_rinsing(
                    G=G,
                    from_vessel=solvent2_vessel,
                    to_vessel=target_vessel,
                    volume=solvent2_volume,
                    flowrate=2.0,
                    transfer_flowrate=1.0
                )
                action_sequence.extend(solvent2_transfer_actions)
                debug_print(f"✅ 溶剂2添加完成，添加了 {len(solvent2_transfer_actions)} 个动作")

                # 🔧 新增：更新体积 - 添加溶剂2
                if target_vessel == column_vessel:
                    current_column_volume += solvent2_volume
                    if column_vessel in G.nodes():
                        G.nodes[column_vessel]['data']['liquid_volume'] = current_column_volume
                        debug_print(f"📊 柱容器体积增加: +{solvent2_volume:.2f}mL = {current_column_volume:.2f}mL")
                elif target_vessel == from_vessel_id:
                    current_from_volume += solvent2_volume
                    update_vessel_volume(from_vessel, G, current_from_volume, "添加溶剂2后")

            except Exception as e:
                debug_print(f"⚠️ 溶剂2添加失败: {str(e)}")

        # 步骤6.4: 使用柱层析设备执行分离（如果有设备）
        if column_device_id:
            debug_print(f"📍 6.4: 使用柱层析设备执行分离")

            column_separation_action = {
                "device_id": column_device_id,
                "action_name": "run_column",
                "action_kwargs": {
                    "from_vessel": from_vessel_id,  # 🔧 使用 from_vessel_id
                    "to_vessel": to_vessel_id,      # 🔧 使用 to_vessel_id
                    "column": column,
                    "rf": rf,
                    "pct1": pct1,
                    "pct2": pct2,
                    "solvent1": solvent1,
                    "solvent2": solvent2,
                    "ratio": ratio
                }
            }
            action_sequence.append(column_separation_action)
            debug_print(f"✅ 柱层析设备动作已添加")

            # 等待分离完成
            separation_time = max(30, min(120, int(total_elution_volume / 2)))  # 30-120秒，基于体积
            action_sequence.append({
                "action_name": "wait",
                "action_kwargs": {"time": separation_time}
            })
            debug_print(f"✅ 等待分离完成: {separation_time}秒")

        # 步骤6.5: 产物收集（从柱容器到目标容器）
        if column_vessel and column_vessel != to_vessel_id:  # 🔧 使用 to_vessel_id
            debug_print(f"📍 6.5: 产物收集 - 从 {column_vessel} 到 {to_vessel_id}")

            try:
                # 估算产物体积（原始样品体积的70-90%，收率考虑）
                product_volume = source_volume * 0.8

                product_transfer_actions = generate_pump_protocol_with_rinsing(
                    G=G,
                    from_vessel=column_vessel,
                    to_vessel=to_vessel_id,  # 🔧 使用 to_vessel_id
                    volume=product_volume,
                    flowrate=1.5,
                    transfer_flowrate=0.8
                )
                action_sequence.extend(product_transfer_actions)
                debug_print(f"✅ 产物收集完成，添加了 {len(product_transfer_actions)} 个动作")

                # 🔧 新增：更新体积 - 产物收集到目标容器
                current_to_volume += product_volume
                current_column_volume -= product_volume  # 柱容器体积减少

                update_vessel_volume(to_vessel, G, current_to_volume, "产物收集后")

                # 更新柱容器体积
                if column_vessel in G.nodes():
                    G.nodes[column_vessel]['data']['liquid_volume'] = max(0.0, current_column_volume)
                    debug_print(f"📊 柱容器体积减少: -{product_volume:.2f}mL = {current_column_volume:.2f}mL")

            except Exception as e:
                debug_print(f"⚠️ 产物收集失败: {str(e)}")

        # 步骤6.6: 如果没有独立的柱设备和容器，执行简化的直接转移
        if not column_device_id and not column_vessel:
            debug_print(f"📍 6.6: 简化模式 - 直接转移 {source_volume}mL 从 {from_vessel_id} 到 {to_vessel_id}")

            try:
                direct_transfer_actions = generate_pump_protocol_with_rinsing(
                    G=G,
                    from_vessel=from_vessel_id,  # 🔧 使用 from_vessel_id
                    to_vessel=to_vessel_id,      # 🔧 使用 to_vessel_id
                    volume=source_volume,
                    flowrate=2.0,
                    transfer_flowrate=1.0
                )
                action_sequence.extend(direct_transfer_actions)
                debug_print(f"✅ 直接转移完成，添加了 {len(direct_transfer_actions)} 个动作")

                # 🔧 新增：更新体积 - 直接转移
                current_from_volume = 0.0  # 源容器清空
                current_to_volume += source_volume  # 目标容器增加

                update_vessel_volume(from_vessel, G, current_from_volume, "直接转移后，源容器清空")
                update_vessel_volume(to_vessel, G, current_to_volume, "直接转移后，目标容器增加")

            except Exception as e:
                debug_print(f"⚠️ 直接转移失败: {str(e)}")

    except Exception as e:
        debug_print(f"❌ 协议生成失败: {str(e)} 😭")

        # 不添加不确定的动作，直接让action_sequence保持为空列表
        # action_sequence 已经在函数开始时初始化为 []

    # 确保至少有一个有效的动作，如果完全失败就返回空列表
    if not action_sequence:
        debug_print("⚠️ 没有生成任何有效动作")
        # 可以选择返回空列表或添加一个基本的等待动作
        action_sequence.append({
            "action_name": "wait",
            "action_kwargs": {
                "time": 1.0,
                "description": "柱层析协议执行完成"
            }
        })

    # 🔧 新增：柱层析完成后的最终状态报告
    final_from_volume = get_vessel_liquid_volume(from_vessel)
    final_to_volume = get_vessel_liquid_volume(to_vessel)

    # 🎊 总结
    debug_print("🏛️" * 20)
    debug_print(f"🎉 柱层析协议生成完成! ✨")
    debug_print(f"📊 总动作数: {len(action_sequence)} 个")
    debug_print(f"🥽 路径: {from_vessel_id} → {to_vessel_id}")
    debug_print(f"🏛️ 柱子: {column}")
    debug_print(f"🧪 溶剂: {final_solvent1}:{final_solvent2} = {final_pct1:.1f}%:{final_pct2:.1f}%")
    debug_print(f"📊 体积变化统计:")
    debug_print(f"  源容器 {from_vessel_id}:")
    debug_print(f"    - 柱层析前: {original_from_volume:.2f}mL")
    debug_print(f"    - 柱层析后: {final_from_volume:.2f}mL")
    debug_print(f"  目标容器 {to_vessel_id}:")
    debug_print(f"    - 柱层析前: {original_to_volume:.2f}mL")
    debug_print(f"    - 柱层析后: {final_to_volume:.2f}mL")
    debug_print(f"    - 收集体积: {final_to_volume - original_to_volume:.2f}mL")
    debug_print(f"⏱️ 预计总时间: {len(action_sequence) * 5:.0f} 秒 ⌛")
    debug_print("🏛️" * 20)

    return action_sequence

# 🔧 新增：便捷函数


def generate_ethyl_acetate_hexane_column_protocol(G: nx.DiGraph, from_vessel: dict, to_vessel: dict,
                                                  column: str, ratio: str = "30:70") -> List[Dict[str, Any]]:
    """乙酸乙酯-己烷柱层析（常用组合）"""
    from_vessel_id = from_vessel["id"]
    to_vessel_id = to_vessel["id"]
    debug_print(f"🧪⛽ 乙酸乙酯-己烷柱层析: {from_vessel_id} → {to_vessel_id} @ {ratio}")
    return generate_run_column_protocol(G, from_vessel, to_vessel, column,
                                        solvent1="ethyl_acetate", solvent2="hexane", ratio=ratio)


def generate_methanol_dcm_column_protocol(G: nx.DiGraph, from_vessel: dict, to_vessel: dict,
                                          column: str, ratio: str = "5:95") -> List[Dict[str, Any]]:
    """甲醇-二氯甲烷柱层析"""
    from_vessel_id = from_vessel["id"]
    to_vessel_id = to_vessel["id"]
    debug_print(f"🧪🧪 甲醇-DCM柱层析: {from_vessel_id} → {to_vessel_id} @ {ratio}")
    return generate_run_column_protocol(G, from_vessel, to_vessel, column,
                                        solvent1="methanol", solvent2="dichloromethane", ratio=ratio)


def generate_gradient_column_protocol(G: nx.DiGraph, from_vessel: dict, to_vessel: dict,
                                      column: str, start_ratio: str = "10:90",
                                      end_ratio: str = "50:50") -> List[Dict[str, Any]]:
    """梯度洗脱柱层析（中等比例）"""
    from_vessel_id, _ = get_vessel(from_vessel)
    to_vessel_id, _ = get_vessel(to_vessel)
    debug_print(f"📈 梯度柱层析: {from_vessel_id} → {to_vessel_id} ({start_ratio} → {end_ratio})")
    # 使用中间比例作为近似
    return generate_run_column_protocol(G, from_vessel, to_vessel, column, ratio="30:70")


def generate_polar_column_protocol(G: nx.DiGraph, from_vessel: dict, to_vessel: dict,
                                   column: str) -> List[Dict[str, Any]]:
    """极性化合物柱层析（高极性溶剂比例）"""
    from_vessel_id, _ = get_vessel(from_vessel)
    to_vessel_id, _ = get_vessel(to_vessel)
    debug_print(f"⚡ 极性化合物柱层析: {from_vessel_id} → {to_vessel_id}")
    return generate_run_column_protocol(G, from_vessel, to_vessel, column,
                                        solvent1="ethyl_acetate", solvent2="hexane", ratio="70:30")


def generate_nonpolar_column_protocol(G: nx.DiGraph, from_vessel: dict, to_vessel: dict,
                                      column: str) -> List[Dict[str, Any]]:
    """非极性化合物柱层析（低极性溶剂比例）"""
    from_vessel_id, _ = get_vessel(from_vessel)
    to_vessel_id, _ = get_vessel(to_vessel)
    debug_print(f"🛢️ 非极性化合物柱层析: {from_vessel_id} → {to_vessel_id}")
    return generate_run_column_protocol(G, from_vessel, to_vessel, column,
                                        solvent1="ethyl_acetate", solvent2="hexane", ratio="5:95")

# 测试函数


def test_run_column_protocol():
    """测试柱层析协议"""
    debug_print("🧪 === RUN COLUMN PROTOCOL 测试 === ✨")
    debug_print("✅ 测试完成 🎉")


if __name__ == "__main__":
    test_run_column_protocol()
