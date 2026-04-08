import networkx as nx

from .logger_util import debug_print
from .resource_helper import get_resource_id, get_resource_data


def get_vessel(vessel):
    """
    统一处理vessel参数，返回vessel_id和vessel_data。
    支持 dict、str、ResourceDictInstance。

    Args:
        vessel: 可以是一个字典、字符串或 ResourceDictInstance，表示vessel的ID或数据。

    Returns:
        tuple: 包含vessel_id和vessel_data。
    """
    # 统一使用 resource_helper 处理
    vessel_id = get_resource_id(vessel)
    vessel_data = get_resource_data(vessel)
    return vessel_id, vessel_data


def find_reagent_vessel(G: nx.DiGraph, reagent: str) -> str:
    """增强版试剂容器查找，支持固体和液体"""
    debug_print(f"🔍 开始查找试剂 '{reagent}' 的容器...")

    # 🔧 方法1：直接搜索 data.reagent_name 和 config.reagent
    debug_print(f"📋 方法1: 搜索reagent字段...")
    for node in G.nodes():
        node_data = G.nodes[node].get('data', {})
        node_type = G.nodes[node].get('type', '')
        config_data = G.nodes[node].get('config', {})

        # 只搜索容器类型的节点
        if node_type == 'container':
            reagent_name = node_data.get('reagent_name', '').lower()
            config_reagent = config_data.get('reagent', '').lower()

            # 精确匹配
            if reagent_name == reagent.lower() or config_reagent == reagent.lower():
                debug_print(f"✅ 通过reagent字段精确匹配到容器: {node} 🎯")
                return node

            # 模糊匹配
            if (reagent.lower() in reagent_name and reagent_name) or \
                    (reagent.lower() in config_reagent and config_reagent):
                debug_print(f"✅ 通过reagent字段模糊匹配到容器: {node} 🔍")
                return node

    # 🔧 方法2：常见的容器命名规则
    debug_print(f"📋 方法2: 使用命名规则查找...")
    reagent_clean = reagent.lower().replace(' ', '_').replace('-', '_')
    possible_names = [
        reagent_clean,
        f"flask_{reagent_clean}",
        f"bottle_{reagent_clean}",
        f"vessel_{reagent_clean}",
        f"{reagent_clean}_flask",
        f"{reagent_clean}_bottle",
        f"reagent_{reagent_clean}",
        f"reagent_bottle_{reagent_clean}",
        f"solid_reagent_bottle_{reagent_clean}",
        f"reagent_bottle_1",  # 通用试剂瓶
        f"reagent_bottle_2",
        f"reagent_bottle_3"
    ]

    debug_print(f"🔍 尝试的容器名称: {possible_names[:5]}... (共{len(possible_names)}个)")

    for name in possible_names:
        if name in G.nodes():
            node_type = G.nodes[name].get('type', '')
            if node_type == 'container':
                debug_print(f"✅ 通过命名规则找到容器: {name} 📝")
                return name

    # 🔧 方法3：节点名称模糊匹配
    debug_print(f"📋 方法3: 节点名称模糊匹配...")
    for node_id in G.nodes():
        node_data = G.nodes[node_id]
        if node_data.get('type') == 'container':
            # 检查节点名称是否包含试剂名称
            if reagent_clean in node_id.lower():
                debug_print(f"✅ 通过节点名称模糊匹配到容器: {node_id} 🔍")
                return node_id

            # 检查液体类型匹配
            vessel_data = node_data.get('data', {})
            liquids = vessel_data.get('liquid', [])
            for liquid in liquids:
                if isinstance(liquid, dict):
                    liquid_type = liquid.get('liquid_type') or liquid.get('name', '')
                    if liquid_type.lower() == reagent.lower():
                        debug_print(f"✅ 通过液体类型匹配到容器: {node_id} 💧")
                        return node_id

    # 🔧 方法4：使用第一个试剂瓶作为备选
    debug_print(f"📋 方法4: 查找备选试剂瓶...")
    for node_id in G.nodes():
        node_data = G.nodes[node_id]
        if (node_data.get('type') == 'container' and
                ('reagent' in node_id.lower() or 'bottle' in node_id.lower())):
            debug_print(f"⚠️ 未找到专用容器，使用备选试剂瓶: {node_id} 🔄")
            return node_id

    debug_print(f"❌ 所有方法都失败了，无法找到容器!")
    raise ValueError(f"找不到试剂 '{reagent}' 对应的容器")


def find_solvent_vessel(G: nx.DiGraph, solvent: str) -> str:
    """
    查找溶剂容器

    Args:
        G: 网络图
        solvent: 溶剂名称

    Returns:
        str: 溶剂容器ID
    """
    debug_print(f"🔍 正在查找溶剂 '{solvent}' 的容器... 🧪")

    # 第四步：通过数据中的试剂信息匹配
    debug_print("  🧪 步骤1: 数据试剂信息匹配...")
    for node_id in G.nodes():
        debug_print(f"查找 id {node_id}, type={G.nodes[node_id].get('type')}, data={G.nodes[node_id].get('data', {})} 的容器...")
        if G.nodes[node_id].get('type') == 'container':
            vessel_data = G.nodes[node_id].get('data', {})

            # 检查 data 中的 reagent_name 字段
            reagent_name = vessel_data.get('reagent_name', '').lower()
            if reagent_name and solvent.lower() == reagent_name:
                debug_print(f"  🎉 通过data.reagent_name匹配找到容器: {node_id} (试剂: {reagent_name}) ✨")
                return node_id

            # 检查 data 中的液体信息
            liquids = vessel_data.get('liquid', []) or vessel_data.get('liquids', [])
            for liquid in liquids:
                if isinstance(liquid, dict):
                    liquid_type = (liquid.get('liquid_type') or liquid.get('name', '')).lower()

                    if solvent.lower() == liquid_type or solvent.lower() in liquid_type:
                        debug_print(f"  🎉 通过液体类型匹配找到容器: {node_id} (液体类型: {liquid_type}) ✨")
                        return node_id

    # 构建可能的容器名称
    possible_names = [
        f"flask_{solvent}",
        f"bottle_{solvent}",
        f"reagent_{solvent}",
        f"reagent_bottle_{solvent}",
        f"{solvent}_flask",
        f"{solvent}_bottle",
        f"{solvent}",
        f"vessel_{solvent}",
    ]

    debug_print(f"📋 候选容器名称: {possible_names[:3]}... (共{len(possible_names)}个) 📝")

    # 第一步：通过容器名称匹配
    debug_print("  🎯 步骤2: 精确名称匹配...")
    for vessel_name in possible_names:
        if vessel_name in G.nodes():
            debug_print(f"  🎉 通过名称匹配找到容器: {vessel_name} ✨")
            return vessel_name

    # 第二步：通过模糊匹配（节点ID和名称）
    debug_print("  🔍 步骤3: 模糊名称匹配...")
    for node_id in G.nodes():
        if G.nodes[node_id].get('type') == 'container':
            node_name = G.nodes[node_id].get('name', '').lower()

            if solvent.lower() in node_id.lower() or solvent.lower() in node_name:
                debug_print(f"  🎉 通过模糊匹配找到容器: {node_id} (名称: {node_name}) ✨")
                return node_id

    # 第三步：通过配置中的试剂信息匹配
    debug_print("  🧪 步骤4: 配置试剂信息匹配...")
    for node_id in G.nodes():
        if G.nodes[node_id].get('type') == 'container':
            # 检查 config 中的 reagent 字段
            node_config = G.nodes[node_id].get('config', {})
            config_reagent = node_config.get('reagent', '').lower()

            if config_reagent and solvent.lower() == config_reagent:
                debug_print(f"  🎉 通过config.reagent匹配找到容器: {node_id} (试剂: {config_reagent}) ✨")
                return node_id

    # 第五步：部分匹配（如果前面都没找到）
    debug_print("  🔍 步骤5: 部分匹配...")
    for node_id in G.nodes():
        if G.nodes[node_id].get('type') == 'container':
            node_config = G.nodes[node_id].get('config', {})
            node_data = G.nodes[node_id].get('data', {})
            node_name = G.nodes[node_id].get('name', '').lower()

            config_reagent = node_config.get('reagent', '').lower()
            data_reagent = node_data.get('reagent_name', '').lower()

            # 检查是否包含溶剂名称
            if (solvent.lower() in config_reagent or
                    solvent.lower() in data_reagent or
                    solvent.lower() in node_name or
                    solvent.lower() in node_id.lower()):
                debug_print(f"  🎉 通过部分匹配找到容器: {node_id} ✨")
                debug_print(f"    - 节点名称: {node_name}")
                debug_print(f"    - 配置试剂: {config_reagent}")
                debug_print(f"    - 数据试剂: {data_reagent}")
                return node_id

    # 调试信息：列出所有容器
    debug_print("  🔎 调试信息：列出所有容器...")
    container_list = []
    for node_id in G.nodes():
        if G.nodes[node_id].get('type') == 'container':
            node_config = G.nodes[node_id].get('config', {})
            node_data = G.nodes[node_id].get('data', {})
            node_name = G.nodes[node_id].get('name', '')

            container_info = {
                'id': node_id,
                'name': node_name,
                'config_reagent': node_config.get('reagent', ''),
                'data_reagent': node_data.get('reagent_name', '')
            }
            container_list.append(container_info)
            debug_print(
                f"    - 容器: {node_id}, 名称: {node_name}, config试剂: {node_config.get('reagent', '')}, data试剂: {node_data.get('reagent_name', '')}")

    debug_print(f"❌ 找不到溶剂 '{solvent}' 对应的容器 😭")
    debug_print(f"🔍 查找的溶剂: '{solvent}' (小写: '{solvent.lower()}')")
    debug_print(f"📊 总共发现 {len(container_list)} 个容器")

    raise ValueError(f"找不到溶剂 '{solvent}' 对应的容器")


def find_connected_stirrer(G: nx.DiGraph, vessel: str) -> str:
    """查找连接到指定容器的搅拌器"""
    debug_print(f"🔍 查找连接到容器 '{vessel}' 的搅拌器...")

    stirrer_nodes = []
    for node in G.nodes():
        node_class = G.nodes[node].get('class', '').lower()
        if 'stirrer' in node_class:
            stirrer_nodes.append(node)
            debug_print(f"📋 发现搅拌器: {node}")

    debug_print(f"📊 共找到 {len(stirrer_nodes)} 个搅拌器")

    # 查找连接到容器的搅拌器
    for stirrer in stirrer_nodes:
        if G.has_edge(stirrer, vessel) or G.has_edge(vessel, stirrer):
            debug_print(f"✅ 找到连接的搅拌器: {stirrer} 🔗")
            return stirrer

    # 返回第一个搅拌器
    if stirrer_nodes:
        debug_print(f"⚠️ 未找到直接连接的搅拌器，使用第一个: {stirrer_nodes[0]} 🔄")
        return stirrer_nodes[0]

    debug_print(f"❌ 未找到任何搅拌器")
    return ""


def find_solid_dispenser(G: nx.DiGraph) -> str:
    """查找固体加样器"""
    debug_print(f"🔍 查找固体加样器...")

    for node in G.nodes():
        node_class = G.nodes[node].get('class', '').lower()
        if 'solid_dispenser' in node_class or 'dispenser' in node_class:
            debug_print(f"✅ 找到固体加样器: {node} 🥄")
            return node

    debug_print(f"❌ 未找到固体加样器")
    return ""


def find_connected_heatchill(G: nx.DiGraph, vessel: str) -> str:
    """查找与指定容器相连的加热/冷却设备"""
    heatchill_nodes = []
    for node in G.nodes():
        node_data = G.nodes[node]
        node_class = node_data.get('class', '') or ''
        node_name = node.lower()
        if ('heatchill' in node_class.lower() or 'virtual_heatchill' in node_class
                or 'heater' in node_name or 'heat' in node_name):
            heatchill_nodes.append(node)

    # 检查连接
    if vessel and heatchill_nodes:
        for hc in heatchill_nodes:
            if G.has_edge(hc, vessel) or G.has_edge(vessel, hc):
                debug_print(f"加热设备 '{hc}' 与容器 '{vessel}' 相连")
                return hc

    # 使用第一个可用设备
    if heatchill_nodes:
        debug_print(f"使用第一个加热设备: {heatchill_nodes[0]}")
        return heatchill_nodes[0]

    debug_print("未找到加热设备，使用默认设备")
    return "heatchill_1"