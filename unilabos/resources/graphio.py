import copy
import importlib
import inspect
import json
import os.path
import traceback
from typing import Union, Any, Dict, List, Optional
import networkx as nx

from unilabos.config.config import BasicConfig
from unilabos.resources.objects.joint_state import ResourceJointState
from unilabos.resources.objects.pose import (
    ResourceDictPosition,
    ResourceDictPositionObject,
)
from unilabos.resources.objects.resource import (
    EXTRA_RESOURCE_CLASS,
    EXTRA_RESOURCE_JOINT_STATE,
    EXTRA_RESOURCE_META_DATA,
    EXTRA_RESOURCE_POSE,
    RESOURCE_ROOT_FIELDS,
)
from unilabos.resources.objects.site import ResourceSite
from unilabos.resources.objects.state import TRACKER_STATE_KEYS
from unilabos.resources.plr_identity import material_children, material_serialization, prepare_plr_restore, restore_tip_counters
from unilabos.resources.resource_tracker import (
    ResourceDictInstance,
    ResourceTreeSet,
    iter_serialized_plr_children,
    apply_plr_site_metadata,
    extract_plr_sites,
    find_plr_resource_class,
    require_plr_config_type,
    get_plr_template_name,
    plr_class_accepts_serialized_sites,
    repair_itemized_ordering,
    sites_for_plr_deserialization,
    set_plr_template_name,
)
from unilabos.utils import logger
from unilabos.utils.banner_print import print_status

try:
    from pylabrobot.resources.resource import Resource as ResourcePLR
except ImportError:
    pass
from typing import get_origin

physical_setup_graph: nx.Graph = None


def canonicalize_nodes_data(
    nodes: List[Dict[str, Any]],
    parent_relation: Optional[Dict[str, List[str]]] = None,
) -> ResourceTreeSet:
    """
    标准化节点数据，使用 ResourceInstanceDictFlatten 进行规范化并创建 ResourceTreeSet

    Args:
        nodes: 原始节点列表
        parent_relation: 父子关系映射 {parent_id: [child_id1, child_id2, ...]}

    Returns:
        ResourceTreeSet: 标准化后的资源树集合
    """
    parent_relation = parent_relation or {}
    print_status(f"{len(nodes)} Resources loaded", "info")

    # 第一步：基本预处理（处理graphml的label字段）
    for node in nodes:
        if not node.get("uuid"):
            transport_uuid = (node.get("data") or {}).get("unilabos_uuid")
            if not transport_uuid:
                raise ValueError(
                    f"资源 {node.get('id', node.get('name'))} 缺少 UUID；"
                    "-g strict import 必须携带资源/Site UUID"
                )
            node["uuid"] = str(transport_uuid)
    for node in nodes:
        if node.get("label") is not None:
            node_id = node.pop("label")
            node["id"] = node["name"] = node_id
        # 父子关系只由 parent/parent_uuid 表达；children 是派生字段，读取时丢弃。
        node.pop("children", None)
        if not isinstance(node.get("config"), dict):
            node["config"] = {}
        if not node.get("type"):
            node["type"] = "device"
            print_status(f"Warning: Node {node.get('id', 'unknown')} missing 'type', defaulting to 'device'", "warning")
        if node.get("name", None) is None:
            node["name"] = node.get("id")
            print_status(f"Warning: Node {node.get('id', 'unknown')} missing 'name', defaulting to {node['name']}", "warning")
        if "position" in node:
            raise ValueError(
                f"资源 {node.get('id', node.get('name'))} 的根字段 position 不受支持，"
                "请使用 pose.position"
            )
        legacy_position = {
            axis: node.pop(axis)
            for axis in ("x", "y", "z")
            if node.get(axis) is not None
        }
        if legacy_position:
            raw_pose = copy.deepcopy(node.get("pose") or {})
            if not isinstance(raw_pose, dict):
                raise ValueError(
                    f"资源 {node.get('id', node.get('name'))} 的 pose 必须是对象"
                )
            if raw_pose.get("position") not in (None, legacy_position):
                raise ValueError(
                    f"资源 {node.get('id', node.get('name'))} 的 pose.position "
                    "与旧 x/y/z 坐标冲突"
                )
            raw_pose["position"] = legacy_position
            node["pose"] = raw_pose
        if "sample_id" in node:
            sample_id = node.pop("sample_id")
            if sample_id:
                logger.error(f"{node} 的 sample_id 参数不受支持，已忽略: {sample_id}")
        for k in list(node.keys()):
            if k not in RESOURCE_ROOT_FIELDS:
                v = node.pop(k)
                node["config"][k] = v
    # 第二步：处理parent_relation（GraphML 的 "父::子" 命名）→ 写入子节点 parent
    id2idx = {node["id"]: idx for idx, node in enumerate(nodes)}
    for parent, children in parent_relation.items():
        for child in children:
            if child in id2idx:
                nodes[id2idx[child]]["parent"] = parent

    # 第三步：使用 ResourceInstanceDictFlatten 标准化每个节点
    standardized_instances = []
    known_nodes: Dict[str, ResourceDictInstance] = {}  # {node_id: ResourceDictInstance}
    uuid_to_instance: Dict[str, ResourceDictInstance] = {}  # {uuid: ResourceDictInstance}

    for node in nodes:
        try:
            # print_status(f"DeviceId: {node['id']}, Class: {node['class']}", "info")
            # 使用标准化方法
            resource_instance = ResourceDictInstance.get_resource_instance_from_dict(node)
            # 不同父节点下的子物料可以同名（多块板各有 A1 孔）；id 只在
            # 无 parent_uuid 时作回退键，uuid 才是身份。
            known_nodes.setdefault(node["id"], resource_instance)
            uuid_to_instance[resource_instance.res_content.uuid] = resource_instance
            standardized_instances.append(resource_instance)
        except Exception as e:
            print_status(f"Failed to standardize node {node.get('id', 'unknown')}:\n{traceback.format_exc()}", "error")
            continue

    # 第四步：建立 parent 和 children 关系
    for node in nodes:
        current_instance = uuid_to_instance.get(str(node.get("uuid") or ""))
        if current_instance is None:
            continue

        # 优先使用 parent_uuid 进行匹配，如果不存在则使用 parent
        parent_uuid = node.get("parent_uuid")
        parent_id = node.get("parent")
        parent_instance = None

        # 优先用 parent_uuid 匹配
        if parent_uuid and parent_uuid in uuid_to_instance:
            parent_instance = uuid_to_instance[parent_uuid]
        # 否则用 parent_id 匹配
        elif parent_id and parent_id in known_nodes:
            parent_instance = known_nodes[parent_id]

        # 设置 parent 引用
        if parent_instance:
            current_instance.res_content.parent = parent_instance.res_content
            # 将当前节点添加到父节点的 children 列表
            parent_instance.children.append(current_instance)

    # 第五步：创建 ResourceTreeSet
    resource_tree_set = ResourceTreeSet.from_nested_instance_list(standardized_instances)
    return resource_tree_set


def canonicalize_links_ports(links: List[Dict[str, Any]], resource_tree_set: ResourceTreeSet) -> List[Dict[str, Any]]:
    """
    标准化边/连接的端口信息

    Args:
        links: 原始连接列表
        resource_tree_set: 资源树集合，用于获取节点的UUID信息

    Returns:
        标准化后的连接列表
    """
    # 构建 id 到 uuid 的映射
    id_to_uuid: Dict[str, str] = {}
    uuid_to_id: Dict[str, str] = {}
    for node in resource_tree_set.all_nodes:
        id_to_uuid[node.res_content.id] = node.res_content.uuid
        uuid_to_id[node.res_content.uuid] = node.res_content.id

    # 第三遍处理：为每个 link 补齐 source/target 与 source_uuid/target_uuid
    # （Graph Authority 导出的边两者都有；只带其一时按节点集合补出另一方）。
    resolved_links: List[Dict[str, Any]] = []
    for link in links:
        source_id = link.get("source")
        target_id = link.get("target")

        # 添加 source_uuid
        if source_id and source_id in id_to_uuid:
            link["source_uuid"] = id_to_uuid[source_id]

        # 添加 target_uuid
        if target_id and target_id in id_to_uuid:
            link["target_uuid"] = id_to_uuid[target_id]

        source_uuid = link.get("source_uuid")
        target_uuid = link.get("target_uuid")

        # 添加 source_uuid
        if source_uuid and source_uuid in uuid_to_id:
            link["source"] = uuid_to_id[source_uuid]

        # 添加 target_uuid
        if target_uuid and target_uuid in uuid_to_id:
            link["target"] = uuid_to_id[target_uuid]

        if not link.get("source") or not link.get("target"):
            logger.warning(
                f"[GraphIO] 边 {link} 的端点不在节点集合中，已忽略"
            )
            continue
        resolved_links.append(link)
    links[:] = resolved_links

    # 第一遍处理：将字符串类型的port转换为字典格式
    for link in links:
        port = link.get("port")
        if port is None:
            continue
        if link.get("type", "physical") == "physical":
            link["type"] = "fluid"
        if isinstance(port, int):
            port = str(port)
        if isinstance(port, str):
            port_str = port.strip()
            if port_str.startswith("(") and port_str.endswith(")"):
                # 处理格式为 "(A,B)" 的情况
                content = port_str[1:-1].strip()
                parts = [p.strip() for p in content.split(",", 1)]
                source_port = parts[0]
                dest_port = parts[1] if len(parts) > 1 else None
            else:
                # 处理格式为 "A" 的情况
                source_port = port_str
                dest_port = None
            link["port"] = {link["source"]: source_port, link["target"]: dest_port}
        elif not isinstance(port, dict):
            # 若port既非字符串也非字典，初始化为空结构
            link["port"] = {link["source"]: None, link["target"]: None}

    # 构建边字典，键为(source节点, target节点)，值为对应的port信息
    edges = {(link["source"], link["target"]): link["port"] for link in links if link.get("port")}

    # 第二遍处理：填充反向边的dest信息
    delete_reverses = []
    for i, link in enumerate(links):
        s, t = link["source"], link["target"]
        current_port = link.get("port")
        if current_port is None:
            continue
        if current_port.get(t) is None:
            reverse_key = (t, s)
            reverse_port = edges.get(reverse_key)
            if reverse_port:
                reverse_source = reverse_port.get(s)
                if reverse_source is not None:
                    # 设置当前边的dest为反向边的source
                    current_port[t] = reverse_source
                    delete_reverses.append(i)
            else:
                # 若不存在反向边，初始化为空结构
                current_port[t] = current_port[s]
    # 删除已被使用反向端口信息的反向边
    standardized_links = [link for i, link in enumerate(links) if i not in delete_reverses]
    return standardized_links


def handle_communications(G: nx.Graph):
    available_communication_types = ["serial", "io_device", "plc", "io"]
    for e, edata in G.edges.items():
        if edata.get("type", "physical") != "communication":
            continue
        if G.nodes[e[0]].get("template_name") in available_communication_types:
            device_comm, device = e[0], e[1]
        elif G.nodes[e[1]].get("template_name") in available_communication_types:
            device_comm, device = e[1], e[0]
        else:
            continue

        comm_template = G.nodes[device_comm].get("template_name")
        if comm_template == "serial":
            G.nodes[device]["config"]["port"] = device_comm
        elif comm_template == "io_device":
            logger.warning(f'Modify {device}\'s io_device_port to {edata["port"][device_comm]}')
            G.nodes[device]["config"]["io_device_port"] = int(edata["port"][device_comm])


def read_node_link_json(
    json_info: Union[str, Dict[str, Any]],
) -> tuple[nx.Graph, ResourceTreeSet, List[Dict[str, Any]]]:
    """把 node-link JSON payload 装配为运行时图结构。

    规范输入是 Graph Authority 导出的权威 payload（``unilab -g <名称|uuid>``
    的缓存文件即该形态）。``-g`` 本地 JSON 文件在启动早期先经
    ``GraphService.upsert_graph`` 对账/发号登记，再以权威 payload 进入本函数。

    Args:
        json_info: JSON文件路径或字典数据

    Returns:
        tuple[nx.Graph, ResourceTreeSet, List[Dict[str, Any]]]:
            返回NetworkX图对象、资源树集合和标准化后的连接列表
    """
    global physical_setup_graph
    if isinstance(json_info, str):
        data = json.load(open(json_info, encoding="utf-8"))
    else:
        data = json_info

    # 标准化节点数据并创建 ResourceTreeSet
    nodes = data.get("nodes", [])
    resource_tree_set = canonicalize_nodes_data(nodes)

    # 标准化边数据
    links = data.get("links", data.get("edges", []))
    standardized_links = canonicalize_links_ports(links, resource_tree_set)

    # 构建 NetworkX 图（需要转换回 dict 格式）
    # 从 ResourceTreeSet 获取所有节点
    graph_data = {
        "nodes": [node.res_content.model_dump(by_alias=True) for node in resource_tree_set.all_nodes],
        "links": standardized_links,
    }
    physical_setup_graph = nx.node_link_graph(graph_data, edges="links", multigraph=False)
    handle_communications(physical_setup_graph)

    # Stamp machine_name on device trees only (resources are cloud-managed)
    local_machine = BasicConfig.machine_name or "本地"
    for tree in resource_tree_set.trees:
        if tree.root_node.res_content.type != "device":
            continue
        for node in tree.get_all_nodes():
            if not node.res_content.machine_name:
                node.res_content.machine_name = local_machine

    return physical_setup_graph, resource_tree_set, standardized_links


def modify_to_backend_format(data: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for edge in data:
        port = edge.pop("port", {})
        source = edge["source"]
        target = edge["target"]
        if source in port:
            edge["sourceHandle"] = port[source]
        elif "source_port" in edge:
            edge["sourceHandle"] = edge.pop("source_port")
        elif "source_handle" in edge:
            edge["sourceHandle"] = edge.pop("source_handle")
        else:
            typ = edge.get("type")
            if typ == "communication":
                continue
        if target in port:
            edge["targetHandle"] = port[target]
        elif "target_port" in edge:
            edge["targetHandle"] = edge.pop("target_port")
        elif "target_handle" in edge:
            edge["targetHandle"] = edge.pop("target_handle")
        else:
            typ = edge.get("type")
            if typ == "communication":
                continue
        edge["id"] = f"reactflow__edge-{source}-{edge['sourceHandle']}-{target}-{edge['targetHandle']}"
        for key in ["source_port", "target_port"]:
            if key in edge:
                edge.pop(key)
    return data


def read_graphml(graphml_file: str) -> tuple[nx.Graph, ResourceTreeSet, List[Dict[str, Any]]]:
    """
    读取GraphML文件并构建图

    Args:
        graphml_file: GraphML文件路径

    Returns:
        tuple[nx.Graph, ResourceTreeSet, List[Dict[str, Any]]]:
            返回NetworkX图对象、资源树集合和标准化后的连接列表
    """
    global physical_setup_graph

    G = nx.read_graphml(graphml_file)
    mapping = {}
    parent_relation = {}
    for node in G.nodes():
        label = G.nodes[node].pop("label", G.nodes[node].get("id", G.nodes[node].get("name", "NaN")))
        mapping[node] = label
        if "::" in node:
            parent = mapping[node.split("::")[0]]
            if parent not in parent_relation:
                parent_relation[parent] = []
            parent_relation[parent].append(label)

    G2 = nx.relabel_nodes(G, mapping)
    data = nx.node_link_data(G2)

    # 标准化节点数据并创建 ResourceTreeSet
    nodes = data.get("nodes", [])
    resource_tree_set = canonicalize_nodes_data(nodes, parent_relation=parent_relation)

    # 标准化边数据
    links = data.get("links", [])
    standardized_links = canonicalize_links_ports(links, resource_tree_set)

    # 构建 NetworkX 图（需要转换回 dict 格式）
    # 从 ResourceTreeSet 获取所有节点
    graph_data = {
        "nodes": [node.res_content.model_dump(by_alias=True) for node in resource_tree_set.all_nodes],
        "links": standardized_links,
    }
    dump_json_path = os.path.join(BasicConfig.working_dir, os.path.basename(graphml_file).rsplit(".")[0] + ".json")
    with open(dump_json_path, "w", encoding="utf-8") as f:
        f.write(json.dumps(graph_data, indent=4, ensure_ascii=False))
        print_status(f"GraphML converted to JSON and saved to {dump_json_path}", "info")
    physical_setup_graph = nx.node_link_graph(graph_data, link="links", multigraph=False)
    handle_communications(physical_setup_graph)

    # Stamp machine_name on device trees only (resources are cloud-managed)
    local_machine = BasicConfig.machine_name or "本地"
    for tree in resource_tree_set.trees:
        if tree.root_node.res_content.type != "device":
            continue
        for node in tree.get_all_nodes():
            if not node.res_content.machine_name:
                node.res_content.machine_name = local_machine

    return physical_setup_graph, resource_tree_set, standardized_links


def dict_from_graph(graph: nx.Graph) -> dict:
    nodes_copy = {node_id: {"id": node_id, **node} for node_id, node in graph.nodes(data=True)}
    return nodes_copy


def _child_ids_by_parent(nodes: dict) -> dict[str, list[str]]:
    """按 ``parent`` 派生每个节点的子节点 id 列表（保持输入顺序）。

    扁平节点列表的父子关系只由 ``parent`` 表达；仅当整批节点都没有 parent
    时才回退读取旧数据的 ``children`` 列表。
    """

    children: dict[str, list[str]] = {node_id: [] for node_id in nodes}
    for node_id, node in nodes.items():
        parent_id = node.get("parent")
        if isinstance(parent_id, dict):
            parent_id = parent_id.get("id")
        if parent_id in children and parent_id != node_id:
            children[parent_id].append(node_id)
    if any(children.values()):
        return children
    return {
        node_id: [child for child in (node.get("children") or []) if isinstance(child, str) and child in nodes]
        for node_id, node in nodes.items()
    }


def dict_to_tree(nodes: dict, devices_only: bool = False) -> list[dict]:
    # 将节点转换为字典，以便通过 ID 快速查找
    nodes_list = [node for node in nodes.values() if node.get("type") == "device" or not devices_only]
    is_root = {node["id"]: True for node in nodes_list}
    child_ids = _child_ids_by_parent(nodes)

    # 初始化每个节点的 children 为包含节点字典的列表
    for node in nodes_list:
        node["children"] = [nodes[child_id] for child_id in child_ids[node["id"]]]
        for child_id in child_ids[node["id"]]:
            if child_id in is_root:
                is_root[child_id] = False

    # 找到根节点并返回
    root_nodes = [node for node in nodes_list if is_root.get(node["id"], False) or len(nodes_list) == 1]

    # 如果存在多个根节点，返回所有根节点
    return root_nodes


def dict_to_nested_dict(nodes: dict, devices_only: bool = False) -> dict:
    # 将节点转换为字典，以便通过 ID 快速查找
    nodes_list = [node for node in nodes.values() if node.get("type") == "device" or not devices_only]
    is_root = {node["id"]: True for node in nodes_list}
    child_ids = _child_ids_by_parent(nodes)

    # 初始化每个节点的 children 为包含节点字典的列表
    for node in nodes_list:
        node["children"] = {
            child_id: nodes[child_id]
            for child_id in child_ids[node["id"]]
            if nodes[child_id].get("type") == "device" or not devices_only
        }
        for child_id in child_ids[node["id"]]:
            if child_id in is_root:
                is_root[child_id] = False
        if len(node["children"]) > 0 and node["type"].lower() == "device":
            node["config"]["children"] = node["children"]

    # 找到根节点并返回
    root_nodes = {node["id"]: node for node in nodes_list if is_root.get(node["id"], False) or len(nodes_list) == 1}

    # 如果存在多个根节点，返回所有根节点
    return root_nodes


def list_to_nested_dict(nodes: list[dict]) -> dict:
    nodes_dict = {node["id"]: node for node in nodes}
    return dict_to_nested_dict(nodes_dict)


def tree_to_list(tree: list[dict]) -> list[dict]:
    """嵌套树 → 扁平节点列表；层级只由每个节点的 ``parent`` 表达，不再输出 children。"""

    def _tree_to_list(tree: list[dict], result: list[dict], parent_id: Optional[str]):
        for node_ in tree:
            node = node_.copy()
            children = node.pop("children", None) or []
            if not node.get("parent"):
                node["parent"] = parent_id
            result.append(node)
            _tree_to_list(children, result, node["id"])

    result: list[dict] = []
    _tree_to_list(tree, result, None)
    return result


def nested_dict_to_list(nested_dict: dict) -> list[dict]:
    """
    将嵌套字典转换为扁平列表

    嵌套字典的层次结构将通过children属性表示

    Args:
        nested_dict: 嵌套的字典结构

    Returns:
        扁平化的字典列表
    """
    result = []

    # 如果输入本身是一个节点，先添加它
    if "id" in nested_dict:
        node = nested_dict.copy()
        # 暂存子节点
        children_dict = node.get("children", {})
        # 如果children是字典，将其转换为键列表
        if isinstance(children_dict, dict):
            node["children"] = list(children_dict.keys())
        elif not isinstance(children_dict, list):
            node["children"] = []
        result.append(node)

        # 处理子节点字典
        if isinstance(children_dict, dict):
            for child_id, child_data in children_dict.items():
                if isinstance(child_data, dict):
                    # 为子节点添加ID（如果不存在）
                    if "id" not in child_data:
                        child_data["id"] = child_id
                    # 递归处理子节点
                    result.extend(nested_dict_to_list(child_data))

    # 处理children字段
    elif "children" in nested_dict:
        children_dict = nested_dict.get("children", {})
        if isinstance(children_dict, dict):
            for child_id, child_data in children_dict.items():
                if isinstance(child_data, dict):
                    # 为子节点添加ID（如果不存在）
                    if "id" not in child_data:
                        child_data["id"] = child_id
                    # 递归处理子节点
                    result.extend(nested_dict_to_list(child_data))

    return result


def convert_resources_to_type(
    resources_list: list[dict], resource_type: Union[type, list[type]], *, plr_model: bool = False
) -> Union[list[dict], dict, None, "ResourcePLR"]:
    """
    Convert resources to a given type (PyLabRobot or NestedDict) from flattened list of dictionaries.

    Args:
        resources: List of resources in the flattened dictionary format.
        resource_type: Type of the resources to convert to.
        plr_model: 是否有plr_model类型

    Returns:
        List of resources in the given type.
    """
    if resource_type == dict or resource_type == str:
        return list_to_nested_dict(resources_list)
    elif isinstance(resource_type, type) and issubclass(resource_type, ResourcePLR):
        if isinstance(resources_list, dict):
            return resource_ulab_to_plr(resources_list, plr_model)
        resources_tree = dict_to_tree({r["id"]: r for r in resources_list})
        return resource_ulab_to_plr(resources_tree[0], plr_model)
    elif isinstance(resource_type, list):
        if all((get_origin(t) is Union) for t in resource_type):
            resources_tree = dict_to_tree({r["id"]: r for r in resources_list})
            return [resource_ulab_to_plr(r, plr_model) for r in resources_tree]
        elif all(issubclass(t, ResourcePLR) for t in resource_type):
            resources_tree = dict_to_tree({r["id"]: r for r in resources_list})
            return [resource_ulab_to_plr(r, plr_model) for r in resources_tree]
    else:
        return None


def convert_resources_from_type(
    resources_list, resource_type: Union[type, list[type]], *, is_plr: bool = False
) -> Union[list[dict], dict, None, "ResourcePLR"]:
    """
    Convert resources from a given type (PyLabRobot or NestedDict) to flattened list of dictionaries.

    Args:
        resources_list: List of resources in the given type.
        resource_type: Type of the resources to convert from.

    Returns:
        List of resources in the flattened dictionary format.
    """
    if resource_type == dict:
        return nested_dict_to_list(resources_list)
    elif isinstance(resource_type, type) and issubclass(resource_type, ResourcePLR):
        resources_tree = [resource_plr_to_ulab(resources_list)]
        return tree_to_list(resources_tree)
    elif isinstance(resource_type, list):
        if all((get_origin(t) is Union) for t in resource_type):
            resources_tree = [resource_plr_to_ulab(r) for r in resources_list]
            return tree_to_list(resources_tree)
        elif is_plr or all(issubclass(t, ResourcePLR) for t in resource_type):
            resources_tree = [resource_plr_to_ulab(r) for r in resources_list]
            return tree_to_list(resources_tree)
    else:
        return None


def resource_ulab_to_plr(resource: dict, plr_model=False) -> "ResourcePLR":
    """
    Resource有model字段，但是Deck下没有，这个plr由外面判断传入
    """
    if ResourcePLR is None:
        raise ImportError("pylabrobot not found")

    resource = copy.deepcopy(resource)

    def prepare_identity(node: dict) -> None:
        if not node.get("uuid"):
            transport_uuid = (node.get("data") or {}).get("unilabos_uuid")
            if not transport_uuid:
                raise ValueError(
                    f"资源 {node.get('id', node.get('name'))} 缺少微后端分配的 UUID"
                )
            node["uuid"] = str(transport_uuid)
        children = node.get("children") or []
        child_values = children.values() if isinstance(children, dict) else children
        for child in child_values:
            prepare_identity(child)

    prepare_identity(resource)

    def tracker_state(resource_dict: dict) -> dict:
        """兼容根字段新形态和 data 内旧形态，组装 PLR state。"""

        state = copy.deepcopy(resource_dict.get("data") or {})
        # 位置由 pose/Site 组装，不能让旧 state 快照再次覆盖。
        state.pop("location", None)
        for state_key in TRACKER_STATE_KEYS:
            if resource_dict.get(state_key) is not None:
                state[state_key] = copy.deepcopy(resource_dict[state_key])
        return state

    all_states = {resource["id"]: tracker_state(resource)}
    all_sites: Dict[str, List[Dict[str, Any]]] = {}
    all_templates: Dict[str, str] = {}
    all_uuids: Dict[str, str] = {}
    all_extras: Dict[str, Dict[str, Any]] = {}

    def resource_ulab_to_plr_inner(resource: dict):
        all_states[resource["name"]] = tracker_state(resource)
        config = dict(resource.get("config") or {})
        plr_type = require_plr_config_type(
            config, uuid=str(resource.get("uuid") or ""),
            template_name=str(resource.get("template_name") or ""),
        )
        missing = object()
        config_meta_data = config.pop("meta_data", missing)
        root_sites = resource.get("sites")
        if root_sites is None:
            root_sites = config.get("sites")
        config.pop("sites", None)
        config_template_name = config.pop("template_name", None)
        config_model = config.get("model")
        resource_extra = resource.get("extra") or {}
        if not isinstance(resource_extra, dict):
            raise ValueError(f"资源 {resource['name']} 的 extra 必须是对象")
        template_name = str(
            resource.get("template_name")
            or resource_extra.get(EXTRA_RESOURCE_CLASS)
            or config_template_name
            or (config_model if isinstance(config_model, str) else None)
            or config.get("type")
            or resource.get("type")
            or ""
        )
        all_templates[resource["name"]] = template_name
        all_uuids[resource["name"]] = str(resource["uuid"])
        plr_extra = copy.deepcopy(resource_extra)
        sidecar_meta_data = plr_extra.pop(EXTRA_RESOURCE_META_DATA, missing)
        root_meta_data = resource.get("meta_data")
        if "meta_data" not in resource:
            root_meta_data = {}
            if config_meta_data is not missing:
                root_meta_data = config_meta_data
            if sidecar_meta_data is not missing:
                root_meta_data = sidecar_meta_data
        if not isinstance(root_meta_data, dict):
            raise ValueError(f"资源 {resource['name']} 的 meta_data 必须是对象")
        if sidecar_meta_data is not missing:
            if not isinstance(sidecar_meta_data, dict):
                raise ValueError(
                    f"资源 {resource['name']} 的 extra.{EXTRA_RESOURCE_META_DATA} 必须是对象"
                )
            if sidecar_meta_data != root_meta_data:
                raise ValueError(
                    f"资源 {resource['name']} 的根字段 meta_data 与 "
                    f"extra.{EXTRA_RESOURCE_META_DATA} 冲突"
                )
        if config_meta_data is not missing:
            if not isinstance(config_meta_data, dict):
                raise ValueError(f"资源 {resource['name']} 的 config.meta_data 必须是对象")
            if config_meta_data != root_meta_data:
                raise ValueError(
                    f"资源 {resource['name']} 的根字段 meta_data 与 config.meta_data 冲突"
                )
        plr_extra[EXTRA_RESOURCE_META_DATA] = copy.deepcopy(root_meta_data)
        normalized_pose = ResourceDictPosition.model_validate(
            resource.get("pose") or {}
        )
        plr_extra[EXTRA_RESOURCE_POSE] = normalized_pose.model_dump(
            exclude={"position"}
        )
        if resource.get("joint_state") is not None:
            plr_extra[EXTRA_RESOURCE_JOINT_STATE] = ResourceJointState.model_validate(
                resource["joint_state"]
            ).model_dump()
        all_extras[resource["name"]] = plr_extra
        if root_sites is not None:
            normalized_sites = []
            for ordinal, raw_site in enumerate(root_sites):
                site = copy.deepcopy(raw_site)
                normalized_sites.append(
                    ResourceSite.model_validate(site).model_dump()
                )
            all_sites[resource["name"]] = normalized_sites
            root_sites = normalized_sites
        actual_position = (
            normalized_pose.position.model_dump()
            if normalized_pose.position is not None
            else None
        )
        serialized_children = (
            [resource_ulab_to_plr_inner(child) for child in resource["children"]]
            if isinstance(resource["children"], list)
            else [resource_ulab_to_plr_inner(child) for child_id, child in resource["children"].items()]
        )
        repair_itemized_ordering(config, serialized_children)
        d = {
            "name": resource["name"],
            "type": plr_type,
            "size_x": config.get("size_x", 0),
            "size_y": config.get("size_y", 0),
            "size_z": config.get("size_z", 0),
            "location": (
                {**actual_position, "type": "Coordinate"}
                if actual_position is not None
                else None
            ),
            # PLR 反序列化输入需要显式 rotation 对象。
            "rotation": {"x": 0, "y": 0, "z": 0, "type": "Rotation"},
            "category": resource["type"],
            "model": config.get("model", None),  # resource中deck没有model
            "children": serialized_children,
            "parent_name": resource["parent"] if resource["parent"] is not None else None,
            **config,
        }
        if root_sites is not None:
            site_cls = find_plr_resource_class(d["type"])
            if site_cls is not None and plr_class_accepts_serialized_sites(site_cls):
                d["sites"] = sites_for_plr_deserialization(root_sites)
        if not plr_model:
            d.pop("model")
        return d

    d = resource_ulab_to_plr_inner(resource)
    prepare_plr_restore(d, all_states)
    """无法通过Resource进行反序列化，例如TipSpot必须内部序列化好，直接用TipSpot序列化会多参数，导致出错"""
    sub_cls = find_plr_resource_class(d["type"])
    if sub_cls is None:
        raise ValueError(f"无法找到类型 {d['type']} 对应的 PLR 资源类：{d.get('name')!r}")
    spect = inspect.signature(sub_cls)
    if "category" not in spect.parameters:
        d.pop("category")
    resource_plr = sub_cls.deserialize(d, allow_marshal=True)
    resource_plr.load_all_state(all_states)
    restore_tip_counters(resource_plr, all_states)
    def restore_resource_identity(current: "ResourcePLR") -> None:
        current.unilabos_extra = copy.deepcopy(all_extras.get(current.name, {}))
        set_plr_template_name(current, all_templates.get(current.name, current.__class__.__name__))
        current_uuid = all_uuids.get(current.name)
        if not current_uuid:
            raise ValueError(f"资源 {current.name} 缺少微后端分配的 UUID")
        current.unilabos_uuid = current_uuid
        for child in material_children(current):
            restore_resource_identity(child)

    restore_resource_identity(resource_plr)
    apply_plr_site_metadata(resource_plr, all_sites)
    return resource_plr


def resource_plr_to_ulab(resource_plr: "ResourcePLR", parent_name: str = None, with_children=True):
    def replace_plr_type_to_ulab(source: str):
        replace_info = {
            "plate": "plate",
            "well": "well",
            "tip_spot": "tip_spot",
            "trash": "trash",
            "deck": "deck",
            "tip_rack": "tip_rack",
            "warehouse": "warehouse",
            "container": "container",
            "tube": "tube",
            "bottle_carrier": "bottle_carrier",
            "plate_adapter": "plate_adapter",
            "electrode_sheet": "electrode_sheet",
            "material_hole": "material_hole",
        }
        if source in replace_info:
            return replace_info[source]
        else:
            if source is not None:
                logger.warning(f"转换pylabrobot的时候，出现未知类型: {source}")
            return source

    def resource_plr_to_ulab_inner(
        plr_node: "ResourcePLR",
        d: dict,
        all_states: dict,
        child=True,
    ) -> dict:
        site_defs = extract_plr_sites(plr_node, d)
        plr_extra = copy.deepcopy(getattr(plr_node, "unilabos_extra", {}) or {})
        missing = object()
        resource_meta_data = plr_extra.pop(EXTRA_RESOURCE_META_DATA, missing)
        if resource_meta_data is not missing and not isinstance(resource_meta_data, dict):
            raise ValueError(
                f"资源 {d['name']} 的 unilabos_extra.{EXTRA_RESOURCE_META_DATA} 必须是对象"
            )
        static_pose = plr_extra.pop(EXTRA_RESOURCE_POSE, None)
        joint_state = plr_extra.pop(EXTRA_RESOURCE_JOINT_STATE, None)
        state = copy.deepcopy(all_states[d["name"]])
        state_location = state.pop("location", None)
        tracker_roots = {}
        for state_key in TRACKER_STATE_KEYS:
            if state_key in state:
                tracker_roots[state_key] = state.pop(state_key)
        serialized_location = d.get("location", state_location)
        actual_position = (
            {
                "x": serialized_location["x"],
                "y": serialized_location["y"],
                "z": serialized_location["z"],
            }
            if serialized_location is not None
            else None
        )
        sidecar_position = (
            copy.deepcopy(static_pose.get("position"))
            if isinstance(static_pose, dict) and "position" in static_pose
            else missing
        )
        if static_pose is None:
            serialized_rotation = d.get("rotation") or {"x": 0, "y": 0, "z": 0}
            static_pose = {
                "size": {"width": d["size_x"], "height": d["size_y"], "depth": d["size_z"]},
                "rotation": {
                    "x": serialized_rotation["x"],
                    "y": serialized_rotation["y"],
                    "z": serialized_rotation["z"],
                },
                "layout": d.get("layout", "x-y"),
                "cross_section_type": d.get("cross_section_type", "rectangle"),
            }
        if actual_position is not None:
            if sidecar_position is not missing:
                normalized_sidecar_position = ResourceDictPositionObject.model_validate(
                    sidecar_position
                ).model_dump()
                if normalized_sidecar_position != actual_position:
                    raise ValueError(
                        f"PLR 资源 {d['name']} 的 location 与 "
                        f"unilabos_extra.{EXTRA_RESOURCE_POSE}.position 冲突"
                    )
            static_pose["position"] = actual_position
        r = {
            "id": d["name"],
            "uuid": getattr(plr_node, "unilabos_uuid", ""),
            "name": d["name"],
            "sample_id": None,
            "children": (
                [
                    resource_plr_to_ulab_inner(child_resource, child_dict, all_states)
                    for child_resource, child_dict in iter_serialized_plr_children(plr_node, d)
                ]
                if child
                else []
            ),
            "parent": d.get("parent_name") or parent_name,
            # PLR category 表示资源用途，需映射到 Uni-Lab-OS 节点类型。
            "type": replace_plr_type_to_ulab(d.get("category")),
            "class": d.get("class", ""),
            "template_name": get_plr_template_name(plr_node, d),
            "pose": static_pose,
            "joint_state": joint_state,
            "config": {
                k: v
                for k, v in d.items()
                if k not in ["name", "children", "parent_name", "location", "sites", "template_name"]
            },
            "data": state,
            # 未被提升为 canonical 根字段的 PLR sidecar 仍须以对象形态保留。
            # ResourceDict 对 data/extra 都执行严格对象校验，不能依赖字段缺失
            # 或 PLR serialize() 的偶然输出。
            "extra": plr_extra,
            "sites": [site.model_dump() for site in site_defs] if site_defs is not None else None,
            **tracker_roots,
        }
        if resource_meta_data is not missing:
            r["meta_data"] = resource_meta_data
        return r

    d, all_states = material_serialization(resource_plr)
    r = resource_plr_to_ulab_inner(resource_plr, d, all_states, with_children)

    return r


def initialize_resource(resource_config: dict, resource_type: Any = None) -> Union[list[dict], ResourcePLR]:
    """Initializes a resource based on its configuration.

    If the config is detailed, then do nothing;
    If it is a string, then import the appropriate class and create an instance of it.

    Args:
        resource_config (dict): The configuration dictionary for the resource, which includes the class type and other parameters.

    Returns:
        None
    """
    from unilabos.registry.registry import lab_registry

    resource_class_config = resource_config.get("class", None)
    if resource_class_config is None:
        return [resource_config]
    elif type(resource_class_config) == str:
        # Allow special resource class names to be used
        if resource_class_config not in lab_registry.resource_type_registry:
            logger.warning(f"❌ 类 {resource_class_config} 不在 registry 中，返回原始配置")
            logger.debug(f"   可用的类: {list(lab_registry.resource_type_registry.keys())[:10]}...")
            return [resource_config]
        # If the resource class is a string, look up the class in the
        # resource_type_registry and import it
        resource_class_config = resource_config["class"] = lab_registry.resource_type_registry[resource_class_config][
            "class"
        ]
    if type(resource_class_config) == dict:
        module = importlib.import_module(resource_class_config["module"].split(":")[0])
        mclass = resource_class_config["module"].split(":")[1]
        RESOURCE = getattr(module, mclass)

        if resource_class_config["type"] == "pylabrobot":
            resource_plr = RESOURCE(name=resource_config["name"])
            if resource_type != ResourcePLR:
                tree_sets = ResourceTreeSet.from_plr_resources([resource_plr])
                r = tree_sets.dump()
            else:
                r = resource_plr
        elif resource_class_config["type"] == "unilabos":
            raise ValueError(f"No more support for unilabos Resource class {resource_class_config}")
        elif isinstance(RESOURCE, dict):
            r = [RESOURCE.copy()]

    return r


def initialize_resources(resources_config) -> list[dict]:
    """Initializes a list of resources based on their configuration.

    If the config is detailed, then do nothing;
    If it is a string, then import the appropriate class and create an instance of it.

    Args:
        resources_config (list[dict]): The configuration dictionary for the resources, which includes the class type and other parameters.

    Returns:
        None
    """

    resources = []
    for resource_config in resources_config:
        resources.extend(initialize_resource(resource_config))

    return resources
