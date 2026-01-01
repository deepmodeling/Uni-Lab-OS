"""
JSON 工作流转换模块

提供从多种 JSON 格式转换为统一工作流格式的功能。
支持的格式：
1. workflow/reagent 格式
2. steps_info/labware_info 格式
"""

import json
from os import PathLike
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from unilabos.workflow.common import WorkflowGraph, build_protocol_graph
from unilabos.registry.registry import lab_registry


def get_action_handles(resource_name: str, template_name: str) -> Dict[str, List[str]]:
    """
    从 registry 获取指定设备和动作的 handles 配置

    Args:
        resource_name: 设备资源名称，如 "liquid_handler.prcxi"
        template_name: 动作模板名称，如 "transfer_liquid"

    Returns:
        包含 source 和 target handler_keys 的字典:
        {"source": ["sources_out", "targets_out", ...], "target": ["sources", "targets", ...]}
    """
    result = {"source": [], "target": []}

    device_info = lab_registry.device_type_registry.get(resource_name, {})
    if not device_info:
        return result

    action_mappings = device_info.get("class", {}).get("action_value_mappings", {})
    action_config = action_mappings.get(template_name, {})
    handles = action_config.get("handles", {})

    if isinstance(handles, dict):
        # 处理 input handles (作为 target)
        for handle in handles.get("input", []):
            handler_key = handle.get("handler_key", "")
            if handler_key:
                result["source"].append(handler_key)
        # 处理 output handles (作为 source)
        for handle in handles.get("output", []):
            handler_key = handle.get("handler_key", "")
            if handler_key:
                result["target"].append(handler_key)

    return result


def validate_workflow_handles(graph: WorkflowGraph) -> Tuple[bool, List[str]]:
    """
    校验工作流图中所有边的句柄配置是否正确

    Args:
        graph: 工作流图对象

    Returns:
        (is_valid, errors): 是否有效，错误信息列表
    """
    errors = []
    nodes = graph.nodes

    for edge in graph.edges:
        left_uuid = edge.get("source")
        right_uuid = edge.get("target")
        # target_handle_key是target, right的输入节点（入节点）
        # source_handle_key是source, left的输出节点（出节点）
        right_source_conn_key = edge.get("target_handle_key", "")
        left_target_conn_key = edge.get("source_handle_key", "")

        # 获取源节点和目标节点信息
        left_node = nodes.get(left_uuid, {})
        right_node = nodes.get(right_uuid, {})

        left_res_name = left_node.get("resource_name", "")
        left_template_name = left_node.get("template_name", "")
        right_res_name = right_node.get("resource_name", "")
        right_template_name = right_node.get("template_name", "")

        # 获取源节点的 output handles
        left_node_handles = get_action_handles(left_res_name, left_template_name)
        target_valid_keys = left_node_handles.get("target", [])
        target_valid_keys.append("ready")

        # 获取目标节点的 input handles
        right_node_handles = get_action_handles(right_res_name, right_template_name)
        source_valid_keys = right_node_handles.get("source", [])
        source_valid_keys.append("ready")

        # 如果节点配置了 output handles，则 source_port 必须有效
        if not right_source_conn_key:
            node_name = left_node.get("name", left_uuid[:8])
            errors.append(f"源节点 '{node_name}' 的 source_handle_key 为空，" f"应设置为: {source_valid_keys}")
        elif right_source_conn_key not in source_valid_keys:
            node_name = left_node.get("name", left_uuid[:8])
            errors.append(
                f"源节点 '{node_name}' 的 source 端点 '{right_source_conn_key}' 不存在，" f"支持的端点: {source_valid_keys}"
            )

        # 如果节点配置了 input handles，则 target_port 必须有效
        if not left_target_conn_key:
            node_name = right_node.get("name", right_uuid[:8])
            errors.append(f"目标节点 '{node_name}' 的 target_handle_key 为空，" f"应设置为: {target_valid_keys}")
        elif left_target_conn_key not in target_valid_keys:
            node_name = right_node.get("name", right_uuid[:8])
            errors.append(
                f"目标节点 '{node_name}' 的 target 端点 '{left_target_conn_key}' 不存在，"
                f"支持的端点: {target_valid_keys}"
            )

    return len(errors) == 0, errors


# action 到 resource_name 的映射
ACTION_RESOURCE_MAPPING: Dict[str, str] = {
    # 生物实验操作
    "transfer_liquid": "liquid_handler.prcxi",
    "transfer": "liquid_handler.prcxi",
    "incubation": "incubator.prcxi",
    "move_labware": "labware_mover.prcxi",
    "oscillation": "shaker.prcxi",
    # 有机化学操作
    "HeatChillToTemp": "heatchill.chemputer",
    "StopHeatChill": "heatchill.chemputer",
    "StartHeatChill": "heatchill.chemputer",
    "HeatChill": "heatchill.chemputer",
    "Dissolve": "stirrer.chemputer",
    "Transfer": "liquid_handler.chemputer",
    "Evaporate": "rotavap.chemputer",
    "Recrystallize": "reactor.chemputer",
    "Filter": "filter.chemputer",
    "Dry": "dryer.chemputer",
    "Add": "liquid_handler.chemputer",
}


def normalize_steps(data: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    将不同格式的步骤数据规范化为统一格式

    支持的输入格式：
    - action + parameters
    - action + action_args
    - operation + parameters

    Args:
        data: 原始步骤数据列表

    Returns:
        规范化后的步骤列表，格式为 [{"action": str, "parameters": dict, "description": str?, "step_number": int?}, ...]
    """
    normalized = []
    for idx, step in enumerate(data):
        # 获取动作名称（支持 action 或 operation 字段）
        action = step.get("action") or step.get("operation")
        if not action:
            continue

        # 获取参数（支持 parameters 或 action_args 字段）
        raw_params = step.get("parameters") or step.get("action_args") or {}
        params = dict(raw_params)

        # 规范化 source/target -> sources/targets
        if "source" in raw_params and "sources" not in raw_params:
            params["sources"] = raw_params["source"]
        if "target" in raw_params and "targets" not in raw_params:
            params["targets"] = raw_params["target"]

        # 获取描述（支持 description 或 purpose 字段）
        description = step.get("description") or step.get("purpose")

        # 获取步骤编号（优先使用原始数据中的 step_number，否则使用索引+1）
        step_number = step.get("step_number", idx + 1)

        step_dict = {"action": action, "parameters": params, "step_number": step_number}
        if description:
            step_dict["description"] = description

        normalized.append(step_dict)

    return normalized


def normalize_labware(data: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """
    将不同格式的 labware 数据规范化为统一的字典格式

    支持的输入格式：
    - reagent_name + material_name + positions
    - name + labware + slot

    Args:
        data: 原始 labware 数据列表

    Returns:
        规范化后的 labware 字典，格式为 {name: {"slot": int, "labware": str, "well": list, "type": str, "role": str, "name": str}, ...}
    """
    labware = {}
    for item in data:
        # 获取 key 名称（优先使用 reagent_name，其次是 material_name 或 name）
        reagent_name = item.get("reagent_name")
        key = reagent_name or item.get("material_name") or item.get("name")
        if not key:
            continue

        key = str(key)

        # 处理重复 key，自动添加后缀
        idx = 1
        original_key = key
        while key in labware:
            idx += 1
            key = f"{original_key}_{idx}"

        labware[key] = {
            "slot": item.get("positions") or item.get("slot"),
            "labware": item.get("material_name") or item.get("labware"),
            "well": item.get("well", []),
            "type": item.get("type", "reagent"),
            "role": item.get("role", ""),
            "name": key,
        }

    return labware


def convert_from_json(
    data: Union[str, PathLike, Dict[str, Any]],
    workstation_name: str = "PRCXi",
    validate: bool = True,
) -> WorkflowGraph:
    """
    从 JSON 数据或文件转换为 WorkflowGraph

    支持的 JSON 格式：
    1. {"workflow": [...], "reagent": {...}} - 直接格式
    2. {"steps_info": [...], "labware_info": [...]} - 需要规范化的格式

    Args:
        data: JSON 文件路径、字典数据、或 JSON 字符串
        workstation_name: 工作站名称，默认 "PRCXi"
        validate: 是否校验句柄配置，默认 True

    Returns:
        WorkflowGraph: 构建好的工作流图

    Raises:
        ValueError: 不支持的 JSON 格式 或 句柄校验失败
        FileNotFoundError: 文件不存在
        json.JSONDecodeError: JSON 解析失败
    """
    # 处理输入数据
    if isinstance(data, (str, PathLike)):
        path = Path(data)
        if path.exists():
            with path.open("r", encoding="utf-8") as fp:
                json_data = json.load(fp)
        elif isinstance(data, str):
            # 尝试作为 JSON 字符串解析
            json_data = json.loads(data)
        else:
            raise FileNotFoundError(f"文件不存在: {data}")
    elif isinstance(data, dict):
        json_data = data
    else:
        raise TypeError(f"不支持的数据类型: {type(data)}")

    # 根据格式解析数据
    if "workflow" in json_data and "reagent" in json_data:
        # 格式1: workflow/reagent（已经是规范格式）
        protocol_steps = json_data["workflow"]
        labware_info = json_data["reagent"]
    elif "steps_info" in json_data and "labware_info" in json_data:
        # 格式2: steps_info/labware_info（需要规范化）
        protocol_steps = normalize_steps(json_data["steps_info"])
        labware_info = normalize_labware(json_data["labware_info"])
    elif "steps" in json_data and "labware" in json_data:
        # 格式3: steps/labware（另一种常见格式）
        protocol_steps = normalize_steps(json_data["steps"])
        if isinstance(json_data["labware"], list):
            labware_info = normalize_labware(json_data["labware"])
        else:
            labware_info = json_data["labware"]
    else:
        raise ValueError(
            "不支持的 JSON 格式。支持的格式：\n"
            "1. {'workflow': [...], 'reagent': {...}}\n"
            "2. {'steps_info': [...], 'labware_info': [...]}\n"
            "3. {'steps': [...], 'labware': [...]}"
        )

    # 构建工作流图
    graph = build_protocol_graph(
        labware_info=labware_info,
        protocol_steps=protocol_steps,
        workstation_name=workstation_name,
        action_resource_mapping=ACTION_RESOURCE_MAPPING,
    )

    # 校验句柄配置
    if validate:
        is_valid, errors = validate_workflow_handles(graph)
        if not is_valid:
            import warnings

            for error in errors:
                warnings.warn(f"句柄校验警告: {error}")

    return graph


def convert_json_to_node_link(
    data: Union[str, PathLike, Dict[str, Any]],
    workstation_name: str = "PRCXi",
) -> Dict[str, Any]:
    """
    将 JSON 数据转换为 node-link 格式的字典

    Args:
        data: JSON 文件路径、字典数据、或 JSON 字符串
        workstation_name: 工作站名称，默认 "PRCXi"

    Returns:
        Dict: node-link 格式的工作流数据
    """
    graph = convert_from_json(data, workstation_name)
    return graph.to_node_link_dict()


def convert_json_to_workflow_list(
    data: Union[str, PathLike, Dict[str, Any]],
    workstation_name: str = "PRCXi",
) -> List[Dict[str, Any]]:
    """
    将 JSON 数据转换为工作流列表格式

    Args:
        data: JSON 文件路径、字典数据、或 JSON 字符串
        workstation_name: 工作站名称，默认 "PRCXi"

    Returns:
        List: 工作流节点列表
    """
    graph = convert_from_json(data, workstation_name)
    return graph.to_dict()


# 为了向后兼容，保留下划线前缀的别名
_normalize_steps = normalize_steps
_normalize_labware = normalize_labware
