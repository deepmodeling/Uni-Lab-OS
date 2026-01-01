import re
import uuid

import networkx as nx
from networkx.drawing.nx_agraph import to_agraph
import matplotlib.pyplot as plt
from typing import Dict, List, Any, Tuple, Optional

Json = Dict[str, Any]

# ---------------- Graph ----------------


class WorkflowGraph:
    """简单的有向图实现：使用 params 单层参数；inputs 内含连线；支持 node-link 导出"""

    def __init__(self):
        self.nodes: Dict[str, Dict[str, Any]] = {}
        self.edges: List[Dict[str, Any]] = []

    def add_node(self, node_id: str, **attrs):
        self.nodes[node_id] = attrs

    def add_edge(self, source: str, target: str, **attrs):
        # 将 source_port/target_port 映射为服务端期望的 source_handle_key/target_handle_key
        source_handle_key = attrs.pop("source_port", "") or attrs.pop("source_handle_key", "")
        target_handle_key = attrs.pop("target_port", "") or attrs.pop("target_handle_key", "")

        edge = {
            "source": source,
            "target": target,
            "source_node_uuid": source,
            "target_node_uuid": target,
            "source_handle_key": source_handle_key,
            "source_handle_io": attrs.pop("source_handle_io", "source"),
            "target_handle_key": target_handle_key,
            "target_handle_io": attrs.pop("target_handle_io", "target"),
            **attrs,
        }
        self.edges.append(edge)

    def _materialize_wiring_into_inputs(
        self,
        obj: Any,
        inputs: Dict[str, Any],
        variable_sources: Dict[str, Dict[str, Any]],
        target_node_id: str,
        base_path: List[str],
    ):
        has_var = False

        def walk(node: Any, path: List[str]):
            nonlocal has_var
            if isinstance(node, dict):
                if "__var__" in node:
                    has_var = True
                    varname = node["__var__"]
                    placeholder = f"${{{varname}}}"
                    src = variable_sources.get(varname)
                    if src:
                        key = ".".join(path)  # e.g. "params.foo.bar.0"
                        inputs[key] = {"node": src["node_id"], "output": src.get("output_name", "result")}
                        self.add_edge(
                            str(src["node_id"]),
                            target_node_id,
                            source_handle_io=src.get("output_name", "result"),
                            target_handle_io=key,
                        )
                    return placeholder
                return {k: walk(v, path + [k]) for k, v in node.items()}
            if isinstance(node, list):
                return [walk(v, path + [str(i)]) for i, v in enumerate(node)]
            return node

        replaced = walk(obj, base_path[:])
        return replaced, has_var

    def add_workflow_node(
        self,
        node_id: int,
        *,
        device_key: Optional[str] = None,  # 实例名，如 "ser"
        resource_name: Optional[str] = None,  # registry key（原 device_class）
        module: Optional[str] = None,
        template_name: Optional[str] = None,  # 动作/模板名（原 action_key）
        params: Dict[str, Any],
        variable_sources: Dict[str, Dict[str, Any]],
        add_ready_if_no_vars: bool = True,
        prev_node_id: Optional[int] = None,
        **extra_attrs,
    ) -> None:
        """添加工作流节点：params 单层；自动变量连线与 ready 串联；支持附加属性"""
        node_id_str = str(node_id)
        inputs: Dict[str, Any] = {}

        params, has_var = self._materialize_wiring_into_inputs(
            params, inputs, variable_sources, node_id_str, base_path=["params"]
        )

        if add_ready_if_no_vars and not has_var:
            last_id = str(prev_node_id) if prev_node_id is not None else "-1"
            inputs["ready"] = {"node": int(last_id), "output": "ready"}
            self.add_edge(last_id, node_id_str, source_handle_io="ready", target_handle_io="ready")

        node_obj = {
            "device_key": device_key,
            "resource_name": resource_name,  # ✅ 新名字
            "module": module,
            "template_name": template_name,  # ✅ 新名字
            "params": params,
            "inputs": inputs,
        }
        node_obj.update(extra_attrs or {})
        self.add_node(node_id_str, parameters=node_obj)

    # 顺序工作流导出（连线在 inputs，不返回 edges）
    def to_dict(self) -> List[Dict[str, Any]]:
        result = []
        for node_id, attrs in self.nodes.items():
            node = {"uuid": node_id}
            params = dict(attrs.get("parameters", {}) or {})
            flat = {k: v for k, v in attrs.items() if k != "parameters"}
            flat.update(params)
            node.update(flat)
            result.append(node)
        return sorted(result, key=lambda n: int(n["uuid"]) if str(n["uuid"]).isdigit() else n["uuid"])

    # node-link 导出（含 edges）
    def to_node_link_dict(self) -> Dict[str, Any]:
        nodes_list = []
        for node_id, attrs in self.nodes.items():
            node_attrs = attrs.copy()
            params = node_attrs.pop("parameters", {}) or {}
            node_attrs.update(params)
            nodes_list.append({"uuid": node_id, **node_attrs})
        return {
            "directed": True,
            "multigraph": False,
            "graph": {},
            "nodes": nodes_list,
            "edges": self.edges,
            "links": self.edges,
        }


def refactor_data(
    data: List[Dict[str, Any]],
    action_resource_mapping: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    """统一的数据重构函数，根据操作类型自动选择模板

    Args:
        data: 原始步骤数据列表
        action_resource_mapping: action 到 resource_name 的映射字典，可选
    """
    refactored_data = []

    # 定义操作映射，包含生物实验和有机化学的所有操作
    OPERATION_MAPPING = {
        # 生物实验操作
        "transfer_liquid": "transfer_liquid",
        "transfer": "transfer",
        "incubation": "incubation",
        "move_labware": "move_labware",
        "oscillation": "oscillation",
        # 有机化学操作
        "HeatChillToTemp": "HeatChillProtocol",
        "StopHeatChill": "HeatChillStopProtocol",
        "StartHeatChill": "HeatChillStartProtocol",
        "HeatChill": "HeatChillProtocol",
        "Dissolve": "DissolveProtocol",
        "Transfer": "TransferProtocol",
        "Evaporate": "EvaporateProtocol",
        "Recrystallize": "RecrystallizeProtocol",
        "Filter": "FilterProtocol",
        "Dry": "DryProtocol",
        "Add": "AddProtocol",
    }

    UNSUPPORTED_OPERATIONS = ["Purge", "Wait", "Stir", "ResetHandling"]

    for step in data:
        operation = step.get("action")
        if not operation or operation in UNSUPPORTED_OPERATIONS:
            continue

        # 处理重复操作
        if operation == "Repeat":
            times = step.get("times", step.get("parameters", {}).get("times", 1))
            sub_steps = step.get("steps", step.get("parameters", {}).get("steps", []))
            for i in range(int(times)):
                sub_data = refactor_data(sub_steps, action_resource_mapping)
                refactored_data.extend(sub_data)
            continue

        # 获取模板名称
        template_name = OPERATION_MAPPING.get(operation)
        if not template_name:
            # 自动推断模板类型
            if operation.lower() in ["transfer", "incubation", "move_labware", "oscillation"]:
                template_name = f"biomek-{operation}"
            else:
                template_name = f"{operation}Protocol"

        # 获取 resource_name
        resource_name = f"device.{operation.lower()}"
        if action_resource_mapping:
            resource_name = action_resource_mapping.get(operation, resource_name)

        # 获取步骤编号，生成 name 字段
        step_number = step.get("step_number")
        name = f"Step {step_number}" if step_number is not None else None

        # 创建步骤数据
        step_data = {
            "template_name": template_name,
            "resource_name": resource_name,
            "description": step.get("description", step.get("purpose", f"{operation} operation")),
            "lab_node_type": "Device",
            "param": step.get("parameters", step.get("action_args", {})),
            "footer": f"{template_name}-{resource_name}",
        }
        if name:
            step_data["name"] = name
        refactored_data.append(step_data)

    return refactored_data


def build_protocol_graph(
    labware_info: List[Dict[str, Any]],
    protocol_steps: List[Dict[str, Any]],
    workstation_name: str,
    action_resource_mapping: Optional[Dict[str, str]] = None,
) -> WorkflowGraph:
    """统一的协议图构建函数，根据设备类型自动选择构建逻辑

    Args:
        labware_info: labware 信息字典
        protocol_steps: 协议步骤列表
        workstation_name: 工作站名称
        action_resource_mapping: action 到 resource_name 的映射字典，可选
    """
    G = WorkflowGraph()
    resource_last_writer = {}

    protocol_steps = refactor_data(protocol_steps, action_resource_mapping)
    # 有机化学&移液站协议图构建
    WORKSTATION_ID = workstation_name

    # 为所有labware创建资源节点
    res_index = 0
    for labware_id, item in labware_info.items():
        # item_id = item.get("id") or item.get("name", f"item_{uuid.uuid4()}")
        node_id = str(uuid.uuid4())

        # 判断节点类型
        if "Rack" in str(labware_id) or "Tip" in str(labware_id):
            lab_node_type = "Labware"
            description = f"Prepare Labware: {labware_id}"
            liquid_type = []
            liquid_volume = []
        elif item.get("type") == "hardware" or "reactor" in str(labware_id).lower():
            if "reactor" not in str(labware_id).lower():
                continue
            lab_node_type = "Sample"
            description = f"Prepare Reactor: {labware_id}"
            liquid_type = []
            liquid_volume = []
        else:
            lab_node_type = "Reagent"
            description = f"Add Reagent to Flask: {labware_id}"
            liquid_type = [labware_id]
            liquid_volume = [1e5]

        res_index += 1
        G.add_node(
            node_id,
            template_name="create_resource",
            resource_name="host_node",
            name=f"Res {res_index}",
            description=description,
            lab_node_type=lab_node_type,
            footer="create_resource-host_node",
            param={
                "res_id": labware_id,
                "device_id": WORKSTATION_ID,
                "class_name": "container",
                "parent": WORKSTATION_ID,
                "bind_locations": {"x": 0.0, "y": 0.0, "z": 0.0},
                "liquid_input_slot": [-1],
                "liquid_type": liquid_type,
                "liquid_volume": liquid_volume,
                "slot_on_deck": "",
            },
        )
        resource_last_writer[labware_id] = f"{node_id}:labware"

    last_control_node_id = None

    # 处理协议步骤
    for step in protocol_steps:
        node_id = str(uuid.uuid4())
        G.add_node(node_id, **step)

        # 控制流
        if last_control_node_id is not None:
            G.add_edge(last_control_node_id, node_id, source_port="ready", target_port="ready")
        last_control_node_id = node_id

        # 物料流
        params = step.get("param", {})
        input_resources_possible_names = [
            "vessel",
            "to_vessel",
            "from_vessel",
            "reagent",
            "solvent",
            "compound",
            "sources",
            "targets",
        ]

        for target_port in input_resources_possible_names:
            resource_name = params.get(target_port)
            if resource_name and resource_name in resource_last_writer:
                source_node, source_port = resource_last_writer[resource_name].split(":")
                G.add_edge(source_node, node_id, source_port=source_port, target_port=target_port)

        output_resources = {
            "vessel_out": params.get("vessel"),
            "from_vessel_out": params.get("from_vessel"),
            "to_vessel_out": params.get("to_vessel"),
            "filtrate_out": params.get("filtrate_vessel"),
            "reagent": params.get("reagent"),
            "solvent": params.get("solvent"),
            "compound": params.get("compound"),
            "sources_out": params.get("sources"),
            "targets_out": params.get("targets"),
        }

        for source_port, resource_name in output_resources.items():
            if resource_name:
                resource_last_writer[resource_name] = f"{node_id}:{source_port}"

    return G


def draw_protocol_graph(protocol_graph: WorkflowGraph, output_path: str):
    """
    (辅助功能) 使用 networkx 和 matplotlib 绘制协议工作流图，用于可视化。
    """
    if not protocol_graph:
        print("Cannot draw graph: Graph object is empty.")
        return

    G = nx.DiGraph()

    for node_id, attrs in protocol_graph.nodes.items():
        label = attrs.get("description", attrs.get("template_name", node_id[:8]))
        G.add_node(node_id, label=label, **attrs)

    for edge in protocol_graph.edges:
        G.add_edge(edge["source"], edge["target"])

    plt.figure(figsize=(20, 15))
    try:
        pos = nx.nx_agraph.graphviz_layout(G, prog="dot")
    except Exception:
        pos = nx.shell_layout(G)  # Fallback layout

    node_labels = {node: data["label"] for node, data in G.nodes(data=True)}
    nx.draw(
        G,
        pos,
        with_labels=False,
        node_size=2500,
        node_color="skyblue",
        node_shape="o",
        edge_color="gray",
        width=1.5,
        arrowsize=15,
    )
    nx.draw_networkx_labels(G, pos, labels=node_labels, font_size=8, font_weight="bold")

    plt.title("Chemical Protocol Workflow Graph", size=15)
    plt.savefig(output_path, dpi=300, bbox_inches="tight")
    plt.close()
    print(f"  - Visualization saved to '{output_path}'")


COMPASS = {"n", "e", "s", "w", "ne", "nw", "se", "sw", "c"}


def _is_compass(port: str) -> bool:
    return isinstance(port, str) and port.lower() in COMPASS


def draw_protocol_graph_with_ports(protocol_graph, output_path: str, rankdir: str = "LR"):
    """
    使用 Graphviz 端口语法绘制协议工作流图。
    - 若边上的 source_port/target_port 是 compass（n/e/s/w/...），直接用 compass。
    - 否则自动为节点创建 record 形状并定义命名端口 <portname>。
    最终由 PyGraphviz 渲染并输出到 output_path（后缀决定格式，如 .png/.svg/.pdf）。
    """
    if not protocol_graph:
        print("Cannot draw graph: Graph object is empty.")
        return

    # 1) 先用 networkx 搭建有向图，保留端口属性
    G = nx.DiGraph()
    for node_id, attrs in protocol_graph.nodes.items():
        label = attrs.get("description", attrs.get("template_name", node_id[:8]))
        # 保留一个干净的“中心标签”，用于放在 record 的中间槽
        G.add_node(node_id, _core_label=str(label), **{k: v for k, v in attrs.items() if k not in ("label",)})

    edges_data = []
    in_ports_by_node = {}  # 收集命名输入端口
    out_ports_by_node = {}  # 收集命名输出端口

    for edge in protocol_graph.edges:
        u = edge["source"]
        v = edge["target"]
        sp = edge.get("source_handle_key") or edge.get("source_port")
        tp = edge.get("target_handle_key") or edge.get("target_port")

        # 记录到图里（保留原始端口信息）
        G.add_edge(u, v, source_handle_key=sp, target_handle_key=tp)
        edges_data.append((u, v, sp, tp))

        # 如果不是 compass，就按“命名端口”先归类，等会儿给节点造 record
        if sp and not _is_compass(sp):
            out_ports_by_node.setdefault(u, set()).add(str(sp))
        if tp and not _is_compass(tp):
            in_ports_by_node.setdefault(v, set()).add(str(tp))

    # 2) 转为 AGraph，使用 Graphviz 渲染
    A = to_agraph(G)
    A.graph_attr.update(rankdir=rankdir, splines="true", concentrate="false", fontsize="10")
    A.node_attr.update(
        shape="box", style="rounded,filled", fillcolor="lightyellow", color="#999999", fontname="Helvetica"
    )
    A.edge_attr.update(arrowsize="0.8", color="#666666")

    # 3) 为需要命名端口的节点设置 record 形状与 label
    #    左列 = 输入端口；中间 = 核心标签；右列 = 输出端口
    for n in A.nodes():
        node = A.get_node(n)
        core = G.nodes[n].get("_core_label", n)

        in_ports = sorted(in_ports_by_node.get(n, []))
        out_ports = sorted(out_ports_by_node.get(n, []))

        # 如果该节点涉及命名端口，则用 record；否则保留原 box
        if in_ports or out_ports:

            def port_fields(ports):
                if not ports:
                    return " "  # 必须留一个空槽占位
                # 每个端口一个小格子，<p> name
                return "|".join(f"<{re.sub(r'[^A-Za-z0-9_:.|-]', '_', p)}> {p}" for p in ports)

            left = port_fields(in_ports)
            right = port_fields(out_ports)

            # 三栏：左(入) | 中(节点名) | 右(出)
            record_label = f"{{ {left} | {core} | {right} }}"
            node.attr.update(shape="record", label=record_label)
        else:
            # 没有命名端口：普通盒子，显示核心标签
            node.attr.update(label=str(core))

    # 4) 给边设置 headport / tailport
    #    - 若端口为 compass：直接用 compass（e.g., headport="e"）
    #    - 若端口为命名端口：使用在 record 中定义的 <port> 名（同名即可）
    for u, v, sp, tp in edges_data:
        e = A.get_edge(u, v)

        # Graphviz 属性：tail 是源，head 是目标
        if sp:
            if _is_compass(sp):
                e.attr["tailport"] = sp.lower()
            else:
                # 与 record label 中 <port> 名一致；特殊字符已在 label 中做了清洗
                e.attr["tailport"] = re.sub(r"[^A-Za-z0-9_:.|-]", "_", str(sp))

        if tp:
            if _is_compass(tp):
                e.attr["headport"] = tp.lower()
            else:
                e.attr["headport"] = re.sub(r"[^A-Za-z0-9_:.|-]", "_", str(tp))

        # 可选：若想让边更贴边缘，可设置 constraint/spline 等
        # e.attr["arrowhead"] = "vee"

    # 5) 输出
    A.draw(output_path, prog="dot")
    print(f"  - Port-aware workflow rendered to '{output_path}'")


# ---------------- Registry Adapter ----------------


class RegistryAdapter:
    """根据 module 的类名（冒号右侧）反查 registry 的 resource_name（原 device_class），并抽取参数顺序"""

    def __init__(self, device_registry: Dict[str, Any]):
        self.device_registry = device_registry or {}
        self.module_class_to_resource = self._build_module_class_index()

    def _build_module_class_index(self) -> Dict[str, str]:
        idx = {}
        for resource_name, info in self.device_registry.items():
            module = info.get("module")
            if isinstance(module, str) and ":" in module:
                cls = module.split(":")[-1]
                idx[cls] = resource_name
                idx[cls.lower()] = resource_name
        return idx

    def resolve_resource_by_classname(self, class_name: str) -> Optional[str]:
        if not class_name:
            return None
        return self.module_class_to_resource.get(class_name) or self.module_class_to_resource.get(class_name.lower())

    def get_device_module(self, resource_name: Optional[str]) -> Optional[str]:
        if not resource_name:
            return None
        return self.device_registry.get(resource_name, {}).get("module")

    def get_actions(self, resource_name: Optional[str]) -> Dict[str, Any]:
        if not resource_name:
            return {}
        return (self.device_registry.get(resource_name, {}).get("class", {}).get("action_value_mappings", {})) or {}

    def get_action_schema(self, resource_name: Optional[str], template_name: str) -> Optional[Json]:
        return (self.get_actions(resource_name).get(template_name) or {}).get("schema")

    def get_action_goal_default(self, resource_name: Optional[str], template_name: str) -> Json:
        return (self.get_actions(resource_name).get(template_name) or {}).get("goal_default", {}) or {}

    def get_action_input_keys(self, resource_name: Optional[str], template_name: str) -> List[str]:
        schema = self.get_action_schema(resource_name, template_name) or {}
        goal = (schema.get("properties") or {}).get("goal") or {}
        props = goal.get("properties") or {}
        required = goal.get("required") or []
        return list(dict.fromkeys(required + list(props.keys())))
