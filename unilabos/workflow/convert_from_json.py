"""
JSON 工作流转换模块

将 workflow/reagent 格式的 JSON 转换为统一工作流格式。

输入格式:
{
    "workflow": [
        {"action": "...", "action_args": {...}},
        ...
    ],
    "reagent": {
        "reagent_name": {"slot": int, "well": [...], "labware": "..."},
        ...
    }
}
"""

import json
from os import PathLike
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from unilabos.workflow.common import WorkflowGraph, build_protocol_graph
from unilabos.registry.registry import lab_registry


# ==================== 字段映射配置 ====================

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

# action_args 字段到 parameters 字段的映射
# 格式: {"old_key": "new_key"}, 仅映射需要重命名的字段
ARGS_FIELD_MAPPING: Dict[str, str] = {
    # 如果需要字段重命名，在这里配置
    # "old_field_name": "new_field_name",
}

# 默认工作站名称
DEFAULT_WORKSTATION = "PRCXI"


# ==================== 核心转换函数 ====================


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
        for handle in handles.get("input", []):
            handler_key = handle.get("handler_key", "")
            if handler_key:
                result["source"].append(handler_key)
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
        right_source_conn_key = edge.get("target_handle_key", "")
        left_target_conn_key = edge.get("source_handle_key", "")

        left_node = nodes.get(left_uuid, {})
        right_node = nodes.get(right_uuid, {})

        left_res_name = left_node.get("resource_name", "")
        left_template_name = left_node.get("template_name", "")
        right_res_name = right_node.get("resource_name", "")
        right_template_name = right_node.get("template_name", "")

        left_node_handles = get_action_handles(left_res_name, left_template_name)
        target_valid_keys = left_node_handles.get("target", [])
        target_valid_keys.append("ready")

        right_node_handles = get_action_handles(right_res_name, right_template_name)
        source_valid_keys = right_node_handles.get("source", [])
        source_valid_keys.append("ready")

        # 验证目标节点（right）的输入端口
        if not right_source_conn_key:
            node_name = right_node.get("name", right_uuid[:8])
            errors.append(f"目标节点 '{node_name}' 的输入端口 (target_handle_key) 为空，应设置为: {source_valid_keys}")
        elif right_source_conn_key not in source_valid_keys:
            node_name = right_node.get("name", right_uuid[:8])
            errors.append(
                f"目标节点 '{node_name}' 的输入端口 '{right_source_conn_key}' 不存在，支持的输入端口: {source_valid_keys}"
            )

        # 验证源节点（left）的输出端口
        if not left_target_conn_key:
            node_name = left_node.get("name", left_uuid[:8])
            errors.append(f"源节点 '{node_name}' 的输出端口 (source_handle_key) 为空，应设置为: {target_valid_keys}")
        elif left_target_conn_key not in target_valid_keys:
            node_name = left_node.get("name", left_uuid[:8])
            errors.append(
                f"源节点 '{node_name}' 的输出端口 '{left_target_conn_key}' 不存在，支持的输出端口: {target_valid_keys}"
            )

    return len(errors) == 0, errors


def normalize_workflow_steps(workflow: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    将 workflow 格式的步骤数据规范化

    输入格式:
        [{"action": "...", "action_args": {...}}, ...]

    输出格式:
        [{"action": "...", "parameters": {...}, "step_number": int}, ...]

    Args:
        workflow: workflow 数组

    Returns:
        规范化后的步骤列表
    """
    normalized = []
    for idx, step in enumerate(workflow):
        action = step.get("action")
        if not action:
            continue

        # 获取参数: action_args
        raw_params = step.get("action_args", {})
        params = {}

        # 应用字段映射
        for key, value in raw_params.items():
            mapped_key = ARGS_FIELD_MAPPING.get(key, key)
            params[mapped_key] = value

        step_dict = {
            "action": action,
            "parameters": params,
            "step_number": idx + 1,
        }

        # 保留描述字段
        if "description" in step:
            step_dict["description"] = step["description"]

        normalized.append(step_dict)

    return normalized


def convert_from_json(
    data: Union[str, PathLike, Dict[str, Any]],
    workstation_name: str = DEFAULT_WORKSTATION,
    validate: bool = True,
) -> WorkflowGraph:
    """
    从 JSON 数据或文件转换为 WorkflowGraph

    JSON 格式:
        {"workflow": [...], "reagent": {...}}

    Args:
        data: JSON 文件路径、字典数据、或 JSON 字符串
        workstation_name: 工作站名称，默认 "PRCXi"
        validate: 是否校验句柄配置，默认 True

    Returns:
        WorkflowGraph: 构建好的工作流图

    Raises:
        ValueError: 不支持的 JSON 格式
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
            json_data = json.loads(data)
        else:
            raise FileNotFoundError(f"文件不存在: {data}")
    elif isinstance(data, dict):
        json_data = data
    else:
        raise TypeError(f"不支持的数据类型: {type(data)}")

    # 校验格式
    if "workflow" not in json_data or "reagent" not in json_data:
        raise ValueError(
            "不支持的 JSON 格式。请使用标准格式:\n"
            '{"workflow": [{"action": "...", "action_args": {...}}, ...], '
            '"reagent": {"name": {"slot": int, "well": [...], "labware": "..."}, ...}}'
        )

    # 提取数据
    workflow = json_data["workflow"]
    reagent = json_data["reagent"]

    # 规范化步骤数据
    protocol_steps = normalize_workflow_steps(workflow)

    # reagent 已经是字典格式，直接使用
    labware_info = reagent

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
    workstation_name: str = DEFAULT_WORKSTATION,
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
    workstation_name: str = DEFAULT_WORKSTATION,
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
