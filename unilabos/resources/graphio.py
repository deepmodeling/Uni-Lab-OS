import importlib
import inspect
import json
import os.path
import traceback
from typing import Union, Any, Dict, List, Tuple
import uuid
import networkx as nx
from pylabrobot.resources import ResourceHolder
from unilabos_msgs.msg import Resource

from unilabos.config.config import BasicConfig
from unilabos.resources.container import RegularContainer
from unilabos.resources.itemized_carrier import ItemizedCarrier, BottleCarrier
from unilabos.ros.msgs.message_converter import convert_to_ros_msg
from unilabos.resources.resource_tracker import (
    ResourceDictInstance,
    ResourceTreeSet,
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
    nodes: List[Dict[str, Any]], parent_relation: Dict[str, List[str]] = {}
) -> ResourceTreeSet:
    """
    标准化节点数据，使用 ResourceInstanceDictFlatten 进行规范化并创建 ResourceTreeSet

    Args:
        nodes: 原始节点列表
        parent_relation: 父子关系映射 {parent_id: [child_id1, child_id2, ...]}

    Returns:
        ResourceTreeSet: 标准化后的资源树集合
    """
    print_status(f"{len(nodes)} Resources loaded", "info")

    # 第一步：基本预处理（处理graphml的label字段）
    outer_host_node_id = None
    for idx, node in enumerate(nodes):
        if node.get("label") is not None:
            node_id = node.pop("label")
            node["id"] = node["name"] = node_id
        if node["id"] == "host_node":
            outer_host_node_id = idx
        if not isinstance(node.get("config"), dict):
            node["config"] = {}
        if not node.get("type"):
            node["type"] = "device"
            print_status(f"Warning: Node {node.get('id', 'unknown')} missing 'type', defaulting to 'device'", "warning")
        if node.get("name", None) is None:
            node["name"] = node.get("id")
            print_status(f"Warning: Node {node.get('id', 'unknown')} missing 'name', defaulting to {node['name']}", "warning")
        if not isinstance(node.get("position"), dict):
            node["pose"] = {"position": {}}
            x = node.pop("x", None)
            if x is not None:
                node["pose"]["position"]["x"] = x
            y = node.pop("y", None)
            if y is not None:
                node["pose"]["position"]["y"] = y
            z = node.pop("z", None)
            if z is not None:
                node["pose"]["position"]["z"] = z
        if "sample_id" in node:
            sample_id = node.pop("sample_id")
            if sample_id:
                logger.error(f"{node}的sample_id参数已弃用，sample_id: {sample_id}")
        for k in list(node.keys()):
            if k not in ["id", "uuid", "name", "description", "schema", "model", "icon", "parent_uuid", "parent", "type", "class", "position", "config", "data", "children", "pose"]:
                v = node.pop(k)
                node["config"][k] = v
    if outer_host_node_id is not None:
        nodes.pop(outer_host_node_id)
    # 第二步：处理parent_relation
    id2idx = {node["id"]: idx for idx, node in enumerate(nodes)}
    for parent, children in parent_relation.items():
        if parent in id2idx:
            nodes[id2idx[parent]]["children"] = children
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
            known_nodes[node["id"]] = resource_instance
            uuid_to_instance[resource_instance.res_content.uuid] = resource_instance
            standardized_instances.append(resource_instance)
        except Exception as e:
            print_status(f"Failed to standardize node {node.get('id', 'unknown')}:\n{traceback.format_exc()}", "error")
            continue

    # 第四步：建立 parent 和 children 关系
    for node in nodes:
        node_id = node["id"]
        if node_id not in known_nodes:
            continue

        current_instance = known_nodes[node_id]

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
    for node in resource_tree_set.all_nodes:
        id_to_uuid[node.res_content.id] = node.res_content.uuid

    # 第一遍处理：将字符串类型的port转换为字典格式
    for link in links:
        port = link.get("port")
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
    edges = {(link["source"], link["target"]): link["port"] for link in links}

    # 第二遍处理：填充反向边的dest信息
    delete_reverses = []
    for i, link in enumerate(links):
        s, t = link["source"], link["target"]
        current_port = link["port"]
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

    # 第三遍处理：为每个 link 添加 source_uuid 和 target_uuid
    for link in standardized_links:
        source_id = link.get("source")
        target_id = link.get("target")

        # 添加 source_uuid
        if source_id and source_id in id_to_uuid:
            link["source_uuid"] = id_to_uuid[source_id]

        # 添加 target_uuid
        if target_id and target_id in id_to_uuid:
            link["target_uuid"] = id_to_uuid[target_id]

    return standardized_links


def handle_communications(G: nx.Graph):
    available_communication_types = ["serial", "io_device", "plc", "io"]
    for e, edata in G.edges.items():
        if edata.get("type", "physical") != "communication":
            continue
        if G.nodes[e[0]].get("class") in available_communication_types:
            device_comm, device = e[0], e[1]
        elif G.nodes[e[1]].get("class") in available_communication_types:
            device_comm, device = e[1], e[0]
        else:
            continue

        if G.nodes[device_comm].get("class") == "serial":
            G.nodes[device]["config"]["port"] = device_comm
        elif G.nodes[device_comm].get("class") == "io_device":
            logger.warning(f'Modify {device}\'s io_device_port to {edata["port"][device_comm]}')
            G.nodes[device]["config"]["io_device_port"] = int(edata["port"][device_comm])


def read_node_link_json(
    json_info: Union[str, Dict[str, Any]],
) -> tuple[nx.Graph, ResourceTreeSet, List[Dict[str, Any]]]:
    """
    读取节点-边的JSON数据并构建图

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
    links = data.get("links", [])
    standardized_links = canonicalize_links_ports(links, resource_tree_set)

    # 构建 NetworkX 图（需要转换回 dict 格式）
    # 从 ResourceTreeSet 获取所有节点
    graph_data = {
        "nodes": [node.res_content.model_dump(by_alias=True) for node in resource_tree_set.all_nodes],
        "links": standardized_links,
    }
    physical_setup_graph = nx.node_link_graph(graph_data, edges="links", multigraph=False)
    handle_communications(physical_setup_graph)

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
        else:
            typ = edge.get("type")
            if typ == "communication":
                continue
        if target in port:
            edge["targetHandle"] = port[target]
        elif "target_port" in edge:
            edge["targetHandle"] = edge.pop("target_port")
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

    return physical_setup_graph, resource_tree_set, standardized_links


def dict_from_graph(graph: nx.Graph) -> dict:
    nodes_copy = {node_id: {"id": node_id, **node} for node_id, node in graph.nodes(data=True)}
    return nodes_copy


def dict_to_tree(nodes: dict, devices_only: bool = False) -> list[dict]:
    # 将节点转换为字典，以便通过 ID 快速查找
    nodes_list = [node for node in nodes.values() if node.get("type") == "device" or not devices_only]
    id_list = [node["id"] for node in nodes_list]
    is_root = {node["id"]: True for node in nodes_list}

    # 初始化每个节点的 children 为包含节点字典的列表
    for node in nodes_list:
        node["children"] = [nodes[child_id] for child_id in node.get("children", [])]
        for child_id in node.get("children", []):
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

    # 初始化每个节点的 children 为包含节点字典的列表
    for node in nodes_list:
        node["children"] = {
            child_id: nodes[child_id]
            for child_id in node.get("children", [])
            if nodes[child_id].get("type") == "device" or not devices_only
        }
        for child_id in node.get("children", []):
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
    def _tree_to_list(tree: list[dict], result: list[dict]):
        for node_ in tree:
            node = node_.copy()
            result.append(node)
            if node.get("children"):
                _tree_to_list(node["children"], result)
            node["children"] = [n["id"] for n in node["children"]]

    result = []
    _tree_to_list(tree, result)
    return result


def nested_dict_to_list(nested_dict: dict) -> list[dict]:  # FIXME 是tree？
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

    all_states = {resource["id"]: resource["data"]}

    def resource_ulab_to_plr_inner(resource: dict):
        all_states[resource["name"]] = resource["data"]
        extra = resource.pop("extra", {})
        d = {
            "name": resource["name"],
            "type": resource["type"],
            "size_x": resource["config"].get("size_x", 0),
            "size_y": resource["config"].get("size_y", 0),
            "size_z": resource["config"].get("size_z", 0),
            "location": {**resource["position"], "type": "Coordinate"},
            "rotation": {"x": 0, "y": 0, "z": 0, "type": "Rotation"},  # Resource如果没有rotation，是plr版本太低
            "category": resource["type"],
            "model": resource["config"].get("model", None),  # resource中deck没有model
            "children": (
                [resource_ulab_to_plr_inner(child) for child in resource["children"]]
                if isinstance(resource["children"], list)
                else [resource_ulab_to_plr_inner(child) for child_id, child in resource["children"].items()]
            ),
            "parent_name": resource["parent"] if resource["parent"] is not None else None,
            **resource["config"],
        }
        if not plr_model:
            d.pop("model")
        return d

    d = resource_ulab_to_plr_inner(resource)
    """无法通过Resource进行反序列化，例如TipSpot必须内部序列化好，直接用TipSpot序列化会多参数，导致出错"""
    from pylabrobot.utils.object_parsing import find_subclass

    sub_cls = find_subclass(d["type"], ResourcePLR)
    spect = inspect.signature(sub_cls)
    if "category" not in spect.parameters:
        d.pop("category")
    resource_plr = sub_cls.deserialize(d, allow_marshal=True)
    resource_plr.load_all_state(all_states)
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

    def resource_plr_to_ulab_inner(d: dict, all_states: dict, child=True) -> dict:
        r = {
            "id": d["name"],
            "name": d["name"],
            "sample_id": None,
            "children": [resource_plr_to_ulab_inner(child, all_states) for child in d["children"]] if child else [],
            "parent": d["parent_name"] if d["parent_name"] else parent_name if parent_name else None,
            "type": replace_plr_type_to_ulab(d.get("category")),  # FIXME plr自带的type是python class name
            "class": d.get("class", ""),
            "position": (
                {"x": d["location"]["x"], "y": d["location"]["y"], "z": d["location"]["z"]}
                if d["location"]
                else {"x": 0, "y": 0, "z": 0}
            ),
            "config": {k: v for k, v in d.items() if k not in ["name", "children", "parent_name", "location"]},
            "data": all_states[d["name"]],
        }
        return r

    d = resource_plr.serialize()
    all_states = resource_plr.serialize_all_state()
    r = resource_plr_to_ulab_inner(d, all_states, with_children)

    return r


def resource_bioyond_to_plr(bioyond_materials: list[dict], type_mapping: Dict[str, Tuple[str, str]] = {}, deck: Any = None) -> list[dict]:
    """
    将 bioyond 物料格式转换为 ulab 物料格式

    Args:
        bioyond_materials: bioyond 系统的物料查询结果列表
        type_mapping: 物料类型映射字典，格式 {model: (显示名称, UUID)} 或 {显示名称: (model, UUID)}
        location_id_mapping: 库位 ID 到名称的映射字典，格式 {location_id: location_name}

    Returns:
        pylabrobot 格式的物料列表
    """
    plr_materials = []

    # 创建反向映射: {显示名称: (model, UUID)} -> 用于从 Bioyond typeName 查找 model
    # 如果 type_mapping 的 key 已经是显示名称,则直接使用;否则创建反向映射
    reverse_type_mapping = {}
    for key, value in type_mapping.items():
        # value 可能是 tuple 或 list: (显示名称, UUID) 或 [显示名称, UUID]
        display_name = value[0] if isinstance(value, (tuple, list)) and len(value) >= 1 else None
        if display_name:
            # 反向映射: {显示名称: (原始key作为model, UUID)}
            resource_uuid = value[1] if len(value) >= 2 else ""
            # 如果已存在该显示名称,跳过(保留第一个遇到的映射)
            if display_name not in reverse_type_mapping:
                reverse_type_mapping[display_name] = (key, resource_uuid)

    logger.debug(f"[反向映射表] 共 {len(reverse_type_mapping)} 个条目: {list(reverse_type_mapping.keys())}")


    # 用于跟踪同名物料的计数器
    name_counter = {}

    for material in bioyond_materials:
        # 从反向映射中查找: typeName(显示名称) -> (model, UUID)
        type_info = reverse_type_mapping.get(material.get("typeName"))
        className = type_info[0] if type_info else "RegularContainer"

        # 为同名物料添加唯一后缀
        base_name = material["name"]
        if base_name in name_counter:
            name_counter[base_name] += 1
            unique_name = f"{base_name}_{name_counter[base_name]}"
        else:
            name_counter[base_name] = 1
            unique_name = base_name

        plr_material_result = initialize_resource(
            {"name": unique_name, "class": className}, resource_type=ResourcePLR
        )

        # initialize_resource 可能返回列表或单个对象
        if isinstance(plr_material_result, list):
            if len(plr_material_result) == 0:
                logger.warning(f"物料 {material['name']} 初始化失败，跳过")
                continue
            plr_material = plr_material_result[0]
        else:
            plr_material = plr_material_result

        # 确保 plr_material 是 ResourcePLR 实例
        if not isinstance(plr_material, ResourcePLR):
            logger.warning(f"物料 {unique_name} 不是有效的 ResourcePLR 实例，类型: {type(plr_material)}")
            continue

        plr_material.code = material.get("code", "") and material.get("barCode", "") or ""
        plr_material.unilabos_uuid = str(uuid.uuid4())

        # ⭐ 保存 Bioyond 原始信息到 unilabos_extra（用于出库时查询）
        plr_material.unilabos_extra = {
            "material_bioyond_id": material.get("id"),           # Bioyond 物料 UUID
            "material_bioyond_name": material.get("name"),       # Bioyond 原始名称（如 "MDA"）
            "material_bioyond_type": material.get("typeName"),   # Bioyond 物料类型名称
        }

        logger.debug(f"[转换物料] {material['name']} (ID:{material['id']}) → {unique_name} (类型:{className})")

        # 处理子物料（detail）
        if material.get("detail") and len(material["detail"]) > 0:
            for bottle in reversed(plr_material.children):
                plr_material.unassign_child_resource(bottle)
            child_ids = []

            # 确定detail物料的默认类型
            # 样品板的detail通常是样品瓶
            default_detail_type = "样品瓶" if "样品板" in material.get("typeName", "") else None

            for detail in material["detail"]:
                number = (
                    (detail.get("z", 0) - 1) * plr_material.num_items_x * plr_material.num_items_y
                    + (detail.get("y", 0) - 1) * plr_material.num_items_y
                    + (detail.get("x", 0) - 1)
                )

                # 检查索引是否超出范围
                max_index = plr_material.num_items_x * plr_material.num_items_y - 1
                if number < 0 or number > max_index:
                    logger.warning(
                        f"  └─ [子物料警告] {detail['name']} 的坐标 (x={detail.get('x')}, y={detail.get('y')}, z={detail.get('z')}) "
                        f"计算出索引 {number} 超出载架范围 [0-{max_index}] (布局: {plr_material.num_items_x}×{plr_material.num_items_y})，跳过"
                    )
                    continue

                # detail可能没有typeName，尝试从name推断，或使用默认类型
                typeName = detail.get("typeName")

                # 如果没有typeName，尝试根据父物料类型和位置推断
                if not typeName:
                    if "分装板" in material.get("typeName", ""):
                        # 分装板: 根据行(x)判断类型
                        # 第一行(x=1)是10%分装小瓶，第二行(x=2)是90%分装小瓶
                        x_pos = detail.get("x", 0)
                        y_pos = detail.get("y", 0)
                        # logger.debug(f"  └─ [推断类型] {detail['name']} 坐标(x={x_pos}, y={y_pos})")
                        if x_pos == 1:
                            typeName = "10%分装小瓶"
                        elif x_pos == 2:
                            typeName = "90%分装小瓶"
                        # logger.debug(f"  └─ [推断结果] {detail['name']} → {typeName}")
                    else:
                        typeName = default_detail_type

                if typeName and typeName in reverse_type_mapping:
                    bottle = plr_material[number] = initialize_resource(
                        {"name": f'{detail["name"]}_{number}', "class": reverse_type_mapping[typeName][0]}, resource_type=ResourcePLR
                    )
                    bottle.tracker.liquids = [
                        (detail["name"], float(detail.get("quantity", 0)) if detail.get("quantity") else 0)
                    ]
                    bottle.code = detail.get("code", "")
                    logger.debug(f"  └─ [子物料] {detail['name']} → {plr_material.name}[{number}] (类型:{typeName})")
                else:
                    logger.warning(f"  └─ [子物料警告] {detail['name']} 的类型 '{typeName}' 不在mapping中，跳过")
        else:
            # 只对有 capacity 属性的容器（液体容器）处理液体追踪
            if hasattr(plr_material, 'capacity'):
                bottle = plr_material[0] if plr_material.capacity > 0 else plr_material
                bottle.tracker.liquids = [
                    (material["name"], float(material.get("quantity", 0)) if material.get("quantity") else 0)
                ]

        plr_materials.append(plr_material)

        if deck and hasattr(deck, "warehouses"):
            locations = material.get("locations", [])
            if not locations:
                logger.debug(f"[物料位置] {unique_name} 没有location信息，跳过warehouse放置")

            # ⭐ 预先检查：如果物料的任何location在竖向warehouse中，提前交换尺寸
            # 这样可以避免多个location时尺寸不一致的问题
            needs_size_swap = False
            for loc in locations:
                wh_name_check = loc.get("whName")
                if wh_name_check in ["站内试剂存放堆栈", "测量小瓶仓库(测密度)"]:
                    needs_size_swap = True
                    break

            if needs_size_swap and hasattr(plr_material, 'size_x') and hasattr(plr_material, 'size_y'):
                original_x = plr_material.size_x
                original_y = plr_material.size_y
                plr_material.size_x = original_y
                plr_material.size_y = original_x
                logger.debug(f"   物料 {unique_name} 将放入竖向warehouse，预先交换尺寸: {original_x}×{original_y} → {plr_material.size_x}×{plr_material.size_y}")

            for loc in locations:
                wh_name = loc.get("whName")
                logger.debug(f"[物料位置] {unique_name} 尝试放置到 warehouse: {wh_name} (Bioyond坐标: x={loc.get('x')}, y={loc.get('y')}, z={loc.get('z')})")

                # 特殊处理: Bioyond的"堆栈1"需要映射到"堆栈1左"或"堆栈1右"
                # 根据列号(x)判断: 1-4映射到左侧, 5-8映射到右侧
                if wh_name == "堆栈1":
                    x_val = loc.get("x", 1)
                    if 1 <= x_val <= 4:
                        wh_name = "堆栈1左"
                    elif 5 <= x_val <= 8:
                        wh_name = "堆栈1右"
                    else:
                        logger.warning(f"物料 {material['name']} 的列号 x={x_val} 超出范围，无法映射到堆栈1左或堆栈1右")
                        continue

                # 特殊处理: Bioyond的"站内Tip盒堆栈"也需要进行拆分映射
                if wh_name == "站内Tip盒堆栈":
                    y_val = loc.get("y", 1)
                    if y_val == 1:
                        wh_name = "站内Tip盒堆栈(右)"
                    elif y_val in [2, 3]:
                        wh_name = "站内Tip盒堆栈(左)"
                        y = y - 1  # 调整列号，因为左侧仓库对应的 Bioyond y=2 实际上是它的第1列

                if hasattr(deck, "warehouses") and wh_name in deck.warehouses:
                    warehouse = deck.warehouses[wh_name]
                    logger.debug(f"[Warehouse匹配] 找到warehouse: {wh_name} (容量: {warehouse.capacity}, 行×列: {warehouse.num_items_x}×{warehouse.num_items_y})")

                    # Bioyond坐标映射 (重要！): x→行(1=A,2=B...), y→列(1=01,2=02...), z→层(通常=1)
                    x = loc.get("x", 1)  # 行号 (1-based: 1=A, 2=B, 3=C, 4=D)
                    y = loc.get("y", 1)  # 列号 (1-based: 1=01, 2=02, 3=03...)
                    z = loc.get("z", 1)  # 层号 (1-based, 通常为1)

                    # 如果是右侧堆栈，需要调整列号 (5→1, 6→2, 7→3, 8→4)
                    if wh_name == "堆栈1右":
                        y = y - 4  # 将5-8映射到1-4

                    # 特殊处理竖向warehouse（站内试剂存放堆栈、测量小瓶仓库）
                    # 这些warehouse使用 vertical-col-major 布局
                    if wh_name in ["站内试剂存放堆栈", "测量小瓶仓库(测密度)"]:
                        # vertical-col-major 布局的坐标映射：
                        # - Bioyond的x(1=A,2=B)对应warehouse的列(col, x方向)
                        # - Bioyond的y(1=01,2=02,3=03)对应warehouse的行(row, y方向)，从下到上
                        # vertical-col-major 中: row=0 对应底部，row=n-1 对应顶部
                        # Bioyond y=1(01) 对应底部 → row=0, y=2(02) 对应中间 → row=1
                        # 索引计算: idx = row * num_cols + col
                        col_idx = x - 1  # Bioyond的x(A,B) → col索引(0,1)
                        row_idx = y - 1  # Bioyond的y(01,02,03) → row索引(0,1,2)
                        layer_idx = z - 1

                        idx = layer_idx * (warehouse.num_items_x * warehouse.num_items_y) + row_idx * warehouse.num_items_y + col_idx
                        logger.debug(f"🔍 竖向warehouse {wh_name}: Bioyond(x={x},y={y},z={z}) → warehouse(col={col_idx},row={row_idx},layer={layer_idx}) → idx={idx}, capacity={warehouse.capacity}")

                    # 普通横向warehouse的处理
                    else:
                        # 多行warehouse: 根据 layout 使用不同的索引计算
                        row_idx = x - 1  # x表示行: 转为0-based
                        col_idx = y - 1  # y表示列: 转为0-based
                        layer_idx = z - 1  # 转为0-based

                        # 检查 warehouse 的排序方式属性
                        ordering_layout = getattr(warehouse, 'ordering_layout', 'col-major')
                        logger.debug(f"🔍 Warehouse {wh_name} layout检测: hasattr={hasattr(warehouse, 'ordering_layout')}, ordering_layout值='{ordering_layout}', warehouse类型={type(warehouse).__name__}")

                        if ordering_layout == "row-major":
                            # 行优先: A01,A02,A03,A04, B01,B02,B03,B04 (所有Bioyond堆栈)
                            # 索引计算: idx = (row) * num_cols + (col) + (layer) * (rows * cols)
                            idx = layer_idx * (warehouse.num_items_x * warehouse.num_items_y) + row_idx * warehouse.num_items_x + col_idx
                            logger.debug(f"行优先warehouse {wh_name}: x={x}(行),y={y}(列) → row={row_idx},col={col_idx} → idx={idx}")
                        else:
                            # 列优先 (后备): A01,B01,C01,D01, A02,B02,C02,D02
                            # 索引计算: idx = (col) * num_rows + (row) + (layer) * (rows * cols)
                            idx = layer_idx * (warehouse.num_items_x * warehouse.num_items_y) + col_idx * warehouse.num_items_y + row_idx
                            logger.debug(f"列优先warehouse {wh_name}: x={x}(行),y={y}(列) → row={row_idx},col={col_idx} → idx={idx}")

                    if 0 <= idx < warehouse.capacity:
                        if warehouse[idx] is None or isinstance(warehouse[idx], ResourceHolder):
                            # 物料尺寸已在放入warehouse前根据需要进行了交换
                            warehouse[idx] = plr_material
                            logger.debug(f"✅ 物料 {unique_name} 放置到 {wh_name}[{idx}] (Bioyond坐标: x={loc.get('x')}, y={loc.get('y')})")
                    else:
                        logger.warning(f"❌ 物料 {unique_name} 的索引 {idx} 超出仓库 {wh_name} 容量 {warehouse.capacity}")
                else:
                    if wh_name:
                        logger.warning(f"❌ 物料 {unique_name} 的warehouse '{wh_name}' 在deck中不存在。可用warehouses: {list(deck.warehouses.keys()) if hasattr(deck, 'warehouses') else '无'}")

    return plr_materials


def resource_plr_to_bioyond(plr_resources: list[ResourcePLR], type_mapping: dict = {}, warehouse_mapping: dict = {}, material_params: dict = {}) -> list[dict]:
    """
    将 PyLabRobot 资源转换为 Bioyond 格式

    Args:
        plr_resources: PyLabRobot 资源列表
        type_mapping: 物料类型映射字典
        warehouse_mapping: 仓库映射字典
        material_params: 物料默认参数字典 (格式: {物料名称: {参数字典}})

    Returns:
        Bioyond 格式的物料列表
    """
    bioyond_materials = []

    # 定义不需要发送 details 的载架类型
    # 说明：这些载架上自带试剂瓶或烧杯，作为整体物料上传即可，不需要在 details 中重复上传子物料
    CARRIERS_WITHOUT_DETAILS = {
        "BIOYOND_PolymerStation_1BottleCarrier",  # 聚合站-单试剂瓶载架
        "BIOYOND_PolymerStation_1FlaskCarrier",   # 聚合站-单烧杯载架
    }

    for resource in plr_resources:
        if isinstance(resource, BottleCarrier) and resource.capacity > 1:
            # 获取 BottleCarrier 的类型映射
            type_info = type_mapping.get(resource.model)
            if not type_info:
                logger.error(f"❌ [PLR→Bioyond] BottleCarrier 资源 '{resource.name}' 的 model '{resource.model}' 不在 type_mapping 中")
                logger.debug(f"[PLR→Bioyond] 可用的 type_mapping 键: {list(type_mapping.keys())}")
                raise ValueError(f"资源 model '{resource.model}' 未在 MATERIAL_TYPE_MAPPINGS 中配置")

            material = {
                "typeId": type_info[1],
                "code": "",
                "barCode": "",
                "name": resource.name,
                "unit": "个",
                "quantity": 1,
                "details": [],
                "Parameters": "{}"  # API 实际要求的字段（必需）
            }

            # 如果是自带试剂瓶的载架类型，不处理子物料（details留空）
            if resource.model in CARRIERS_WITHOUT_DETAILS:
                logger.info(f"[PLR→Bioyond] 载架 '{resource.name}' (model: {resource.model}) 自带试剂瓶，不添加 details")
            else:
                # 处理其他载架类型的子物料
                for bottle in resource.children:
                    if isinstance(resource, ItemizedCarrier):
                        # ⭐ 优化：直接使用 get_child_identifier 获取真实的子物料坐标
                        # 这个方法会遍历 resource.children 找到 bottle 对象的实际位置
                        site = resource.get_child_identifier(bottle)

                        # 🔧 如果 get_child_identifier 失败或返回无效坐标 (0,0)
                        # 这通常发生在子物料名称使用纯数字后缀时（如 "BTDA_0", "BTDA_4"）
                        if not site or (site.get("x") == 0 and site.get("y") == 0):
                            # 方法1: 尝试从名称中提取标识符并解析
                            bottle_identifier = None
                            if "_" in bottle.name:
                                bottle_identifier = bottle.name.split("_")[-1]

                            # 只有非纯数字标识符才尝试解析（如 "A1", "B2"）
                            if bottle_identifier and not bottle_identifier.isdigit():
                                try:
                                    x_idx, y_idx, z_idx = resource._parse_identifier_to_indices(bottle_identifier, 0)
                                    site = {"x": x_idx, "y": y_idx, "z": z_idx, "identifier": bottle_identifier}
                                    logger.debug(f"  🔧 [坐标修正-方法1] 从名称 {bottle.name} 解析标识符 {bottle_identifier} → ({x_idx}, {y_idx})")
                                except Exception as e:
                                    logger.warning(f"  ⚠️ [坐标解析] 标识符 {bottle_identifier} 解析失败: {e}")

                            # 方法2: 如果方法1失败，使用线性索引反推坐标
                            if not site or (site.get("x") == 0 and site.get("y") == 0):
                                # 找到bottle在children中的索引位置
                                try:
                                    # 遍历所有槽位找到bottle的实际位置
                                    for idx in range(resource.num_items_x * resource.num_items_y):
                                        if resource[idx] is bottle:
                                            # 根据载架布局计算行列坐标
                                            # ItemizedCarrier 默认是列优先布局 (A1,B1,C1,D1, A2,B2,C2,D2...)
                                            col_idx = idx // resource.num_items_y  # 列索引 (0-based)
                                            row_idx = idx % resource.num_items_y   # 行索引 (0-based)
                                            site = {"x": col_idx, "y": row_idx, "z": 0, "identifier": str(idx)}
                                            logger.debug(f"  🔧 [坐标修正-方法2] {bottle.name} 在索引 {idx} → 列={col_idx}, 行={row_idx}")
                                            break
                                except Exception as e:
                                    logger.error(f"  ❌ [坐标计算失败] {bottle.name}: {e}")
                                    # 最后的兜底：使用 (0,0)
                                    site = {"x": 0, "y": 0, "z": 0, "identifier": ""}
                    else:
                        site = {"x": bottle.location.x - 1, "y": bottle.location.y - 1, "identifier": ""}

                    # 获取子物料的类型映射
                    bottle_type_info = type_mapping.get(bottle.model)
                    if not bottle_type_info:
                        logger.error(f"❌ [PLR→Bioyond] 子物料 '{bottle.name}' 的 model '{bottle.model}' 不在 type_mapping 中")
                        raise ValueError(f"子物料 model '{bottle.model}' 未在 MATERIAL_TYPE_MAPPINGS 中配置")

                    # ⚠️ 坐标系转换说明:
                    # _parse_identifier_to_indices 返回: x=列索引, y=行索引 (0-based)
                    # Bioyond 系统要求: x=行号, y=列号 (1-based)
                    # 因此需要交换 x 和 y!
                    bioyond_x = site["y"] + 1  # 行索引 → Bioyond的x (行号)
                    bioyond_y = site["x"] + 1  # 列索引 → Bioyond的y (列号)

                    # 🐛 调试日志
                    logger.debug(f"🔍 [PLR→Bioyond] detail转换: {bottle.name} → PLR(x={site['x']},y={site['y']},id={site.get('identifier','?')}) → Bioyond(x={bioyond_x},y={bioyond_y})")

                    # 🔥 提取物料名称：从 tracker.liquids 中获取第一个液体的名称（去除PLR系统添加的后缀）
                    # tracker.liquids 格式: [(物料名称, 数量), ...]
                    material_name = bottle_type_info[0]  # 默认使用类型名称（如"样品瓶"）
                    if hasattr(bottle, "tracker") and bottle.tracker.liquids:
                        # 如果有液体，使用液体的名称
                        first_liquid_name = bottle.tracker.liquids[0][0]
                        # 去除PLR系统为了唯一性添加的后缀（如 "_0", "_1" 等）
                        if "_" in first_liquid_name and first_liquid_name.split("_")[-1].isdigit():
                            material_name = "_".join(first_liquid_name.split("_")[:-1])
                        else:
                            material_name = first_liquid_name
                        logger.debug(f"  💧 [物料名称] {bottle.name} 液体: {first_liquid_name} → 转换为: {material_name}")
                    else:
                        logger.debug(f"  📭 [物料名称] {bottle.name} 无液体，使用类型名: {material_name}")

                    detail_item = {
                        "typeId": bottle_type_info[1],
                        "code": bottle.code if hasattr(bottle, "code") else "",
                        "name": material_name,  # 使用物料名称（如"9090"），而不是类型名称（"样品瓶"）
                        "quantity": sum(qty for _, qty in bottle.tracker.liquids) if hasattr(bottle, "tracker") else 0,
                        "x": bioyond_x,
                        "y": bioyond_y,
                        "z": 1,
                        "unit": "微升",
                        "Parameters": "{}"  # API 实际要求的字段（必需）
                    }
                    material["details"].append(detail_item)
        else:
            # 单个瓶子(非载架)类型的资源
            bottle = resource[0] if hasattr(resource, "capacity") and resource.capacity > 0 else resource

            # 根据 resource.model 从 type_mapping 获取正确的 typeId
            type_info = type_mapping.get(resource.model)
            if type_info:
                type_id = type_info[1]
            else:
                # 如果找不到映射，记录警告并使用默认值
                logger.warning(f"[PLR→Bioyond] 资源 {resource.name} 的 model '{resource.model}' 不在 type_mapping 中，使用默认烧杯类型")
                type_id = "3a14196b-24f2-ca49-9081-0cab8021bf1a"  # 默认使用烧杯类型

            # 🔥 提取物料名称：优先使用液体名称，否则使用资源名称
            material_name = resource.name if hasattr(resource, "name") else ""
            if hasattr(bottle, "tracker") and bottle.tracker.liquids:
                # 如果有液体，使用液体的名称
                first_liquid_name = bottle.tracker.liquids[0][0]
                # 去除PLR系统为了唯一性添加的后缀（如 "_0", "_1" 等）
                if "_" in first_liquid_name and first_liquid_name.split("_")[-1].isdigit():
                    material_name = "_".join(first_liquid_name.split("_")[:-1])
                else:
                    material_name = first_liquid_name
                logger.debug(f"  💧 [单瓶物料] {resource.name} 液体: {first_liquid_name} → 转换为: {material_name}")
            else:
                logger.debug(f"  📭 [单瓶物料] {resource.name} 无液体，使用资源名: {material_name}")

            # 🎯 处理物料默认参数和单位
            # 优先级: typeId参数 > 物料名称参数 > 默认值
            default_unit = "个"  # 默认单位
            material_parameters = {}

            # 1️⃣ 首先检查是否有 typeId 对应的参数配置（从 material_params 中获取，key 格式为 "type:<typeId>"）
            type_params_key = f"type:{type_id}"
            if type_params_key in material_params:
                params_config = material_params[type_params_key].copy()

                # 提取 unit 字段（如果有）
                if "unit" in params_config:
                    default_unit = params_config.pop("unit")  # 从参数中移除，放到外层

                # 剩余的字段放入 Parameters
                material_parameters = params_config
                logger.debug(f"  🔧 [物料参数-按typeId] 为 typeId={type_id[:8]}... 应用配置: unit={default_unit}, parameters={material_parameters}")
            # 2️⃣ 其次检查是否有该物料名称的默认参数配置
            elif material_name in material_params:
                params_config = material_params[material_name].copy()

                # 提取 unit 字段（如果有）
                if "unit" in params_config:
                    default_unit = params_config.pop("unit")  # 从参数中移除，放到外层

                # 剩余的字段放入 Parameters
                material_parameters = params_config
                logger.debug(f"  🔧 [物料参数-按名称] 为 {material_name} 应用配置: unit={default_unit}, parameters={material_parameters}")

            # 转换为 JSON 字符串
            parameters_json = json.dumps(material_parameters) if material_parameters else "{}"

            material = {
                "typeId": type_id,
                "code": "",
                "barCode": "",
                "name": material_name,  # 使用物料名称而不是资源名称
                "unit": default_unit,  # 使用配置的单位或默认单位
                "quantity": sum(qty for _, qty in bottle.tracker.liquids) if hasattr(bottle, "tracker") else 0,
                "Parameters": parameters_json  # API 实际要求的字段（必需）
            }

        # ⭐ 处理 locations 信息
        # 优先级: update_resource_site (位置更新请求) > 当前 parent 位置
        extra_info = getattr(resource, "unilabos_extra", {})
        update_site = extra_info.get("update_resource_site")

        if update_site:
            # 情况1: 有明确的位置更新请求 (如从 A02 移动到 A03)
            # 需要从 warehouse_mapping 中查找目标库位的 UUID
            logger.debug(f"🔄 [PLR→Bioyond] 检测到位置更新请求: {resource.name} → {update_site}")

            # 遍历所有仓库查找目标库位
            target_warehouse_name = None
            target_location_uuid = None

            for warehouse_name, warehouse_info in warehouse_mapping.items():
                site_uuids = warehouse_info.get("site_uuids", {})
                if update_site in site_uuids:
                    target_warehouse_name = warehouse_name
                    target_location_uuid = site_uuids[update_site]
                    break

            if target_warehouse_name and target_location_uuid:
                # 从库位代码解析坐标 (如 "A03" -> x=1, y=3)
                # A=1, B=2, C=3, D=4...
                # 01=1, 02=2, 03=3...
                try:
                    row_letter = update_site[0]  # 'A', 'B', 'C', 'D'
                    col_number = int(update_site[1:])  # '01', '02', '03'...
                    bioyond_x = ord(row_letter) - ord('A') + 1  # A→1, B→2, C→3, D→4
                    bioyond_y = col_number  # 01→1, 02→2, 03→3

                    material["locations"] = [
                        {
                            "id": target_location_uuid,
                            "whid": warehouse_mapping[target_warehouse_name].get("uuid", ""),
                            "whName": target_warehouse_name,
                            "x": bioyond_x,
                            "y": bioyond_y,
                            "z": 1,
                            "quantity": 0
                        }
                    ]
                    logger.debug(f"✅ [PLR→Bioyond] 位置更新: {resource.name} → {target_warehouse_name}/{update_site} (x={bioyond_x}, y={bioyond_y})")
                except Exception as e:
                    logger.error(f"❌ [PLR→Bioyond] 解析库位代码失败: {update_site}, 错误: {e}")
            else:
                logger.warning(f"⚠️ [PLR→Bioyond] 未找到库位 {update_site} 的配置")

        elif resource.parent is not None and isinstance(resource.parent, ItemizedCarrier):
            # 情况2: 使用当前 parent 位置
            site_in_parent = resource.parent.get_child_identifier(resource)

            # ⚠️ 坐标系转换说明:
            # get_child_identifier 返回: x_idx=列索引, y_idx=行索引 (0-based)
            # Bioyond 系统要求: x=行号, y=列号 (1-based)
            # 因此需要交换 x 和 y!
            bioyond_x = site_in_parent["y"] + 1  # 行索引 → Bioyond的x (行号)
            bioyond_y = site_in_parent["x"] + 1  # 列索引 → Bioyond的y (列号)

            material["locations"] = [
                {
                    "id": warehouse_mapping[resource.parent.name]["site_uuids"][site_in_parent["identifier"]],
                    "whid": warehouse_mapping[resource.parent.name]["uuid"],
                    "whName": resource.parent.name,
                    "x": bioyond_x,
                    "y": bioyond_y,
                    "z": 1,
                    "quantity": 0
                }
            ]
            logger.debug(f"🔄 [PLR→Bioyond] 坐标转换: {resource.name} 在 {resource.parent.name}[{site_in_parent['identifier']}] → UniLab(列={site_in_parent['x']},行={site_in_parent['y']}) → Bioyond(x={bioyond_x},y={bioyond_y})")

        bioyond_materials.append(material)
    return bioyond_materials


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
                tree_sets = ResourceTreeSet.from_plr_resources([resource_plr], known_newly_created=True)
                r = tree_sets.dump()
            else:
                r = resource_plr
        elif resource_class_config["type"] == "unilabos":
            raise ValueError(f"No more support for unilabos Resource class {resource_class_config}")
            res_instance: RegularContainer = RESOURCE(id=resource_config["name"])
            res_instance.ulr_resource = convert_to_ros_msg(
                Resource, {k: v for k, v in resource_config.items() if k != "class"}
            )
            r = [res_instance.get_ulr_resource_as_dict()]
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
