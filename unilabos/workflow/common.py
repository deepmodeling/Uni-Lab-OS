"""
工作流转换模块 - JSON 到 WorkflowGraph 的转换流程

==================== 输入格式 (JSON) ====================

{
    "workflow": [
        {"action": "transfer_liquid", "action_args": {"sources": "cell_lines", "targets": "Liquid_1", "asp_vol": 100.0, "dis_vol": 74.75, ...}},
        ...
    ],
    "reagent": {
        "cell_lines": {"slot": 4, "well": ["A1", "A3", "A5"], "labware": "DRUG + YOYO-MEDIA"},
        "Liquid_1": {"slot": 1, "well": ["A4", "A7", "A10"], "labware": "rep 1"},
        ...
    }
}

==================== 转换步骤 ====================

第一步: 按 slot 去重创建 create_resource 节点（创建板子）
--------------------------------------------------------------------------------
- 首先创建一个 Group 节点（type="Group", minimized=true），用于包含所有 create_resource 节点
- 遍历所有 reagent，按 slot 去重，为每个唯一的 slot 创建一个板子
- 所有 create_resource 节点的 parent_uuid 指向 Group 节点，minimized=true
- 生成参数:
    res_id: plate_slot_{slot}
    device_id: /PRCXI
    class_name: PRCXI_BioER_96_wellplate
    parent: /PRCXI/PRCXI_Deck
    slot_on_deck: "{slot}"
- 输出端口: labware（用于连接 set_liquid_from_plate）
- 控制流: create_resource 之间通过 ready 端口串联

示例: slot=1, slot=4 -> 创建 1 个 Group + 2 个 create_resource 节点

第二步: 为每个 reagent 创建 set_liquid_from_plate 节点（设置液体）
--------------------------------------------------------------------------------
- 首先创建一个 Group 节点（type="Group", minimized=true），用于包含所有 set_liquid_from_plate 节点
- 遍历所有 reagent，为每个试剂创建 set_liquid_from_plate 节点
- 所有 set_liquid_from_plate 节点的 parent_uuid 指向 Group 节点，minimized=true
- 生成参数:
    plate: []（通过连接传递，来自 create_resource 的 labware）
    well_names: ["A1", "A3", "A5"]（来自 reagent 的 well 数组）
    liquid_names: ["cell_lines", "cell_lines", "cell_lines"]（与 well 数量一致）
    volumes: [1e5, 1e5, 1e5]（与 well 数量一致，默认体积）
- 输入连接: create_resource (labware) -> set_liquid_from_plate (input_plate)
- 输出端口: output_wells（用于连接 transfer_liquid）
- 控制流: set_liquid_from_plate 连接在所有 create_resource 之后，通过 ready 端口串联

第三步: 解析 workflow，创建 transfer_liquid 等动作节点
--------------------------------------------------------------------------------
- 遍历 workflow 数组，为每个动作创建步骤节点
- 参数重命名: asp_vol -> asp_vols, dis_vol -> dis_vols, asp_flow_rate -> asp_flow_rates, dis_flow_rate -> dis_flow_rates
- 参数扩展: 根据 targets 的 wells 数量，将单值扩展为数组
    例: asp_vol=100.0, targets 有 3 个 wells -> asp_vols=[100.0, 100.0, 100.0]
- 连接处理: 如果 sources/targets 已通过 set_liquid_from_plate 连接，参数值改为 []
- 输入连接: set_liquid_from_plate (output_wells) -> transfer_liquid (sources_identifier / targets_identifier)
- 输出端口: sources_out, targets_out（用于连接下一个 transfer_liquid）

==================== 连接关系图 ====================

控制流 (ready 端口串联):
    - create_resource 之间: 无 ready 连接
    - set_liquid_from_plate 之间: 无 ready 连接
    - create_resource 与 set_liquid_from_plate 之间: 无 ready 连接
    - transfer_liquid 之间: 通过 ready 端口串联
        transfer_liquid_1 -> transfer_liquid_2 -> transfer_liquid_3 -> ...

物料流:
    [create_resource] --labware--> [set_liquid_from_plate] --output_wells--> [transfer_liquid] --sources_out/targets_out--> [下一个 transfer_liquid]
          (slot=1)                    (cell_lines)           (input_plate)     (sources_identifier)                          (sources_identifier)
          (slot=4)                    (Liquid_1)                               (targets_identifier)                          (targets_identifier)

==================== 端口映射 ====================

create_resource:
    输出: labware

set_liquid_from_plate:
    输入: input_plate
    输出: output_plate, output_wells

transfer_liquid:
    输入: sources -> sources_identifier, targets -> targets_identifier
    输出: sources -> sources_out, targets -> targets_out

==================== 设备名配置 (device_name) ====================

每个节点都有 device_name 字段，指定在哪个设备上执行:
- create_resource: device_name = "host_node"（固定）
- set_liquid_from_plate: device_name = "PRCXI"（可配置，见 DEVICE_NAME_DEFAULT）
- transfer_liquid 等动作: device_name = "PRCXI"（可配置，见 DEVICE_NAME_DEFAULT）

==================== 校验规则 ====================

- 检查 sources/targets 是否在 reagent 中定义
- 检查 sources 和 targets 的 wells 数量是否匹配
- 检查参数数组长度是否与 wells 数量一致
- 如有问题，在 footer 中添加 [WARN: ...] 标记
"""

import re
import uuid

import networkx as nx
from networkx.drawing.nx_agraph import to_agraph
import matplotlib.pyplot as plt
from typing import Dict, List, Any, Tuple, Optional

Json = Dict[str, Any]


# ==================== 默认配置 ====================

# 设备名配置
DEVICE_NAME_HOST = "host_node"  # create_resource 固定在 host_node 上执行
DEVICE_NAME_DEFAULT = "PRCXI"  # transfer_liquid, set_liquid_from_plate 等动作的默认设备名

# 节点类型
NODE_TYPE_DEFAULT = "ILab"  # 所有节点的默认类型

# create_resource 节点默认参数
CREATE_RESOURCE_DEFAULTS = {
    "device_id": "/PRCXI",
    "parent_template": "/PRCXI/PRCXI_Deck",
    "class_name": "PRCXI_BioER_96_wellplate",
}

# 默认液体体积 (uL)
DEFAULT_LIQUID_VOLUME = 1e5

# 参数重命名映射：单数 -> 复数（用于 transfer_liquid 等动作）
PARAM_RENAME_MAPPING = {
    "asp_vol": "asp_vols",
    "dis_vol": "dis_vols",
    "asp_flow_rate": "asp_flow_rates",
    "dis_flow_rate": "dis_flow_rates",
}


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
    labware_info: Dict[str, Dict[str, Any]],
    protocol_steps: List[Dict[str, Any]],
    workstation_name: str,
    action_resource_mapping: Optional[Dict[str, str]] = None,
    labware_defs: Optional[List[Dict[str, Any]]] = None,
) -> WorkflowGraph:
    """统一的协议图构建函数，根据设备类型自动选择构建逻辑

    Args:
        labware_info: reagent 信息字典，格式为 {name: {slot, well}, ...}，用于 set_liquid 和 well 查找
        protocol_steps: 协议步骤列表
        workstation_name: 工作站名称
        action_resource_mapping: action 到 resource_name 的映射字典，可选
        labware_defs: labware 定义列表，格式为 [{"name": "...", "slot": "1", "type": "lab_xxx"}, ...]
    """
    G = WorkflowGraph()
    resource_last_writer = {}  # reagent_name -> "node_id:port"
    slot_to_create_resource = {}  # slot -> create_resource node_id

    protocol_steps = refactor_data(protocol_steps, action_resource_mapping)

    # ==================== 第一步：按 slot 创建 create_resource 节点 ====================
    # 创建 Group 节点，包含所有 create_resource 节点
    group_node_id = str(uuid.uuid4())
    G.add_node(
        group_node_id,
        name="Resources Group",
        type="Group",
        parent_uuid="",
        lab_node_type="Device",
        template_name="",
        resource_name="",
        footer="",
        minimized=True,
        param=None,
    )

    # 直接使用 JSON 中的 labware 定义，每个 slot 一条记录，type 即 class_name
    res_index = 0
    for lw in (labware_defs or []):
        slot = str(lw.get("slot", ""))
        if not slot or slot in slot_to_create_resource:
            continue  # 跳过空 slot 或已处理的 slot

        lw_name = lw.get("name", f"slot {slot}")
        lw_type = lw.get("type", CREATE_RESOURCE_DEFAULTS["class_name"])
        res_id = f"plate_slot_{slot}"

        res_index += 1
        node_id = str(uuid.uuid4())
        G.add_node(
            node_id,
            template_name="create_resource",
            resource_name="host_node",
            name=lw_name,
            description=f"Create {lw_name}",
            lab_node_type="Labware",
            footer="create_resource-host_node",
            device_name=DEVICE_NAME_HOST,
            type=NODE_TYPE_DEFAULT,
            parent_uuid=group_node_id,
            minimized=True,
            param={
                "res_id": res_id,
                "device_id": CREATE_RESOURCE_DEFAULTS["device_id"],
                "class_name": lw_type,
                "parent": CREATE_RESOURCE_DEFAULTS["parent_template"],
                "bind_locations": {"x": 0.0, "y": 0.0, "z": 0.0},
                "slot_on_deck": slot,
            },
        )
        slot_to_create_resource[slot] = node_id

    # ==================== 第二步：为每个 reagent 创建 set_liquid_from_plate 节点 ====================
    # 创建 Group 节点，包含所有 set_liquid_from_plate 节点
    set_liquid_group_id = str(uuid.uuid4())
    G.add_node(
        set_liquid_group_id,
        name="SetLiquid Group",
        type="Group",
        parent_uuid="",
        lab_node_type="Device",
        template_name="",
        resource_name="",
        footer="",
        minimized=True,
        param=None,
    )

    set_liquid_index = 0

    for labware_id, item in labware_info.items():
        # 跳过 Tip/Rack 类型
        if "Rack" in str(labware_id) or "Tip" in str(labware_id):
            continue
        if item.get("type") == "hardware":
            continue

        slot = str(item.get("slot", ""))
        wells = item.get("well", [])
        if not wells or not slot:
            continue

        # res_id 不能有空格
        res_id = str(labware_id).replace(" ", "_")
        well_count = len(wells)

        node_id = str(uuid.uuid4())
        set_liquid_index += 1

        G.add_node(
            node_id,
            template_name="set_liquid_from_plate",
            resource_name="liquid_handler.prcxi",
            name=f"SetLiquid {set_liquid_index}",
            description=f"Set liquid: {labware_id}",
            lab_node_type="Reagent",
            footer="set_liquid_from_plate-liquid_handler.prcxi",
            device_name=DEVICE_NAME_DEFAULT,
            type=NODE_TYPE_DEFAULT,
            parent_uuid=set_liquid_group_id,  # 指向 Group 节点
            minimized=True,  # 折叠显示
            param={
                "plate": [],  # 通过连接传递
                "well_names": wells,  # 孔位名数组，如 ["A1", "A3", "A5"]
                "liquid_names": [res_id] * well_count,
                "volumes": [DEFAULT_LIQUID_VOLUME] * well_count,
            },
        )

        # set_liquid_from_plate 之间不需要 ready 连接

        # 物料流：create_resource 的 labware -> set_liquid_from_plate 的 input_plate
        create_res_node_id = slot_to_create_resource.get(slot)
        if create_res_node_id:
            G.add_edge(create_res_node_id, node_id, source_port="labware", target_port="input_plate")

        # set_liquid_from_plate 的输出 output_wells 用于连接 transfer_liquid
        resource_last_writer[labware_id] = f"{node_id}:output_wells"

    # transfer_liquid 之间通过 ready 串联，从 None 开始
    last_control_node_id = None

    # 端口名称映射：JSON 字段名 -> 实际 handle key
    INPUT_PORT_MAPPING = {
        "sources": "sources_identifier",
        "targets": "targets_identifier",
        "vessel": "vessel",
        "to_vessel": "to_vessel",
        "from_vessel": "from_vessel",
        "reagent": "reagent",
        "solvent": "solvent",
        "compound": "compound",
    }

    OUTPUT_PORT_MAPPING = {
        "sources": "sources_out",  # 输出端口是 xxx_out
        "targets": "targets_out",  # 输出端口是 xxx_out
        "vessel": "vessel_out",
        "to_vessel": "to_vessel_out",
        "from_vessel": "from_vessel_out",
        "filtrate_vessel": "filtrate_out",
        "reagent": "reagent",
        "solvent": "solvent",
        "compound": "compound",
    }

    # 需要根据 wells 数量扩展的参数列表（复数形式）
    EXPAND_BY_WELLS_PARAMS = ["asp_vols", "dis_vols", "asp_flow_rates", "dis_flow_rates"]

    # 处理协议步骤
    for step in protocol_steps:
        node_id = str(uuid.uuid4())
        params = step.get("param", {}).copy()  # 复制一份，避免修改原数据
        connected_params = set()  # 记录被连接的参数
        warnings = []  # 收集警告信息

        # 参数重命名：单数 -> 复数
        for old_name, new_name in PARAM_RENAME_MAPPING.items():
            if old_name in params:
                params[new_name] = params.pop(old_name)

        # 处理输入连接
        for param_key, target_port in INPUT_PORT_MAPPING.items():
            resource_name = params.get(param_key)
            if resource_name and resource_name in resource_last_writer:
                source_node, source_port = resource_last_writer[resource_name].split(":")
                G.add_edge(source_node, node_id, source_port=source_port, target_port=target_port)
                connected_params.add(param_key)
            elif resource_name and resource_name not in resource_last_writer:
                # 资源名在 labware_info 中不存在
                warnings.append(f"{param_key}={resource_name} 未找到")

        # 获取 targets 对应的 wells 数量，用于扩展参数
        targets_name = params.get("targets")
        sources_name = params.get("sources")
        targets_wells_count = 1
        sources_wells_count = 1

        if targets_name and targets_name in labware_info:
            target_wells = labware_info[targets_name].get("well", [])
            targets_wells_count = len(target_wells) if target_wells else 1
        elif targets_name:
            warnings.append(f"targets={targets_name} 未在 reagent 中定义")

        if sources_name and sources_name in labware_info:
            source_wells = labware_info[sources_name].get("well", [])
            sources_wells_count = len(source_wells) if source_wells else 1
        elif sources_name:
            warnings.append(f"sources={sources_name} 未在 reagent 中定义")

        # 检查 sources 和 targets 的 wells 数量是否匹配
        if targets_wells_count != sources_wells_count and targets_name and sources_name:
            warnings.append(f"wells 数量不匹配: sources={sources_wells_count}, targets={targets_wells_count}")

        # 使用 targets 的 wells 数量来扩展参数
        wells_count = targets_wells_count

        # 扩展单值参数为数组（根据 targets 的 wells 数量）
        for expand_param in EXPAND_BY_WELLS_PARAMS:
            if expand_param in params:
                value = params[expand_param]
                # 如果是单个值，扩展为数组
                if not isinstance(value, list):
                    params[expand_param] = [value] * wells_count
                # 如果已经是数组但长度不对，记录警告
                elif len(value) != wells_count:
                    warnings.append(f"{expand_param} 数量({len(value)})与 wells({wells_count})不匹配")

        # 如果 sources/targets 已通过连接传递，将参数值改为空数组
        for param_key in connected_params:
            if param_key in params:
                params[param_key] = []

        # 更新 step 的 param、footer、device_name 和 type
        step_copy = step.copy()
        step_copy["param"] = params
        step_copy["device_name"] = DEVICE_NAME_DEFAULT  # 动作节点使用默认设备名
        step_copy["type"] = NODE_TYPE_DEFAULT  # 节点类型

        # 如果有警告，修改 footer 添加警告标记（警告放前面）
        if warnings:
            original_footer = step.get("footer", "")
            step_copy["footer"] = f"[WARN: {'; '.join(warnings)}] {original_footer}"

        G.add_node(node_id, **step_copy)

        # 控制流
        if last_control_node_id is not None:
            G.add_edge(last_control_node_id, node_id, source_port="ready", target_port="ready")
        last_control_node_id = node_id

        # 处理输出：更新 resource_last_writer
        for param_key, output_port in OUTPUT_PORT_MAPPING.items():
            resource_name = step.get("param", {}).get(param_key)  # 使用原始参数值
            if resource_name:
                resource_last_writer[resource_name] = f"{node_id}:{output_port}"

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
