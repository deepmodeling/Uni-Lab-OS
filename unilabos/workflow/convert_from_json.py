"""
JSON 工作流转换模块

将 workflow/reagent/labware 格式的 JSON 转换为统一工作流格式。

输入格式:
{
    "labware": [
        {"name": "...", "slot": "1", "type": "lab_xxx"},
        ...
    ],
    "workflow": [
        {"action": "...", "action_args": {...}},
        ...
    ],
    "reagent": {
        "reagent_name": {"slot": int, "well": [...]},
        ...
    }
}
"""

import json
import warnings
from os import PathLike
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from unilabos.workflow.common import DEFAULT_TARGET_DEVICE, WorkflowGraph, build_protocol_graph
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


def _load_json_data(data: Union[str, PathLike, Dict[str, Any]]) -> Dict[str, Any]:
    """统一加载 JSON 输入。

    支持三种形态：
    1. ``str`` / ``PathLike`` 指向磁盘文件 → ``json.load``
    2. ``str``（非文件路径）→ ``json.loads`` 解析为 dict
    3. ``dict`` → 直接返回

    抽出此 helper 是为了让 :func:`convert_from_json` 和
    :func:`convert_json_to_workflow_envelope` 都能复用，
    后者需要在传给 :func:`convert_from_json` **之前**先读出顶层
    ``metadata`` 段，而 :func:`convert_from_json` 自身的 schema 校验
    不感知 ``metadata`` 字段。
    """
    if isinstance(data, (str, PathLike)):
        path = Path(data)
        if path.exists():
            with path.open("r", encoding="utf-8") as fp:
                return json.load(fp)
        if isinstance(data, str):
            return json.loads(data)
        raise FileNotFoundError(f"文件不存在: {data}")
    if isinstance(data, dict):
        return data
    raise TypeError(f"不支持的数据类型: {type(data)}")


def convert_from_json(
    data: Union[str, PathLike, Dict[str, Any]],
    workstation_name: str = DEFAULT_WORKSTATION,
    validate: bool = True,
    preserve_tip_rack_incoming_class: bool = False,
    target_device: str = DEFAULT_TARGET_DEVICE,
    target_model: Optional[str] = None,
) -> WorkflowGraph:
    """
    从 JSON 数据或文件转换为 WorkflowGraph

    JSON 格式:
        {"workflow": [...], "reagent": {...}}

    Args:
        data: JSON 文件路径、字典数据、或 JSON 字符串
        workstation_name: 工作站名称，默认 "PRCXi"
        validate: 是否校验句柄配置，默认 True
        preserve_tip_rack_incoming_class: True（默认）时仅 tip_rack 不跑模板、按传入类名/labware；其它载体仍自动匹配。
            False 时全部走模板。JSON 根 ``preserve_tip_rack_incoming_class`` 可覆盖此参数。
        target_device: P6.1 新增。目标仪器名（厂商粒度，如 ``prcxi`` / ``beckman`` / ``tecan``）。
            决定查 ``labware_mapping.yaml`` 中 ``target_devices.<target_device>.rules`` 段；未声明
            的名字由 loader 自动 fallback 到固定段 ``target_devices.default``。默认 ``"prcxi"``。
        target_model: P6.1.1 新增。同厂商内的目标型号名（如 ``"9320"`` / ``"4040"``）；
            决定 ``target_devices.<target_device>.models.<target_model>`` 段的 ``slot_remap`` /
            ``rules`` 覆盖。``None`` 表示走厂商级配置。

    Returns:
        WorkflowGraph: 构建好的工作流图

    Raises:
        ValueError: 不支持的 JSON 格式
        FileNotFoundError: 文件不存在
        json.JSONDecodeError: JSON 解析失败
    """
    json_data = _load_json_data(data)

    # 校验格式（``metadata`` 段为 P5 新增可选顶层字段，不参与校验）
    if "workflow" not in json_data or "reagent" not in json_data:
        raise ValueError(
            "不支持的 JSON 格式。请使用标准格式:\n"
            '{"labware": [...], "workflow": [...], "reagent": {...}}'
        )

    # 提取数据
    workflow = json_data["workflow"]
    reagent = json_data["reagent"]
    labware_defs = json_data.get("labware", [])  # 新的 labware 定义列表

    # 规范化步骤数据
    protocol_steps = normalize_workflow_steps(workflow)

    # reagent 已经是字典格式，用于 set_liquid 和 well 数量查找
    labware_info = reagent

    preserve = preserve_tip_rack_incoming_class
    if "preserve_tip_rack_incoming_class" in json_data:
        preserve = bool(json_data["preserve_tip_rack_incoming_class"])

    # 构建工作流图
    graph = build_protocol_graph(
        labware_info=labware_info,
        protocol_steps=protocol_steps,
        workstation_name=workstation_name,
        action_resource_mapping=ACTION_RESOURCE_MAPPING,
        labware_defs=labware_defs,
        preserve_tip_rack_incoming_class=preserve,
        target_device=target_device,
        target_model=target_model,
    )

    # 校验句柄配置
    if validate:
        # 句柄校验依赖 registry 中各设备动作的端口定义（action_value_mappings.handles）。
        # wf / workflow_upload 走轻量 HTTP 客户端路径时从不调用 build_registry()，
        # 此时 device_type_registry 为空表，校验只认硬加的 "ready" 端口，导致每条数据边
        # 都误报「端口不存在，支持的端口: ['ready']」。这里按需构建一次（空表才建，幂等；
        # 完整 unilab 启动已建表时自动跳过，避免触发 setup() 的重复调用告警）。
        import warnings

        if not lab_registry.device_type_registry:
            try:
                from unilabos.registry.registry import build_registry

                build_registry()
            except Exception as exc:  # 构建失败降级为原行为，绝不让上传因建表异常而崩
                warnings.warn(f"注册表构建失败，跳过句柄校验: {exc}")

        if lab_registry.device_type_registry:
            is_valid, errors = validate_workflow_handles(graph)
            if not is_valid:
                for error in errors:
                    warnings.warn(f"句柄校验警告: {error}")

    return graph


def convert_json_to_node_link(
    data: Union[str, PathLike, Dict[str, Any]],
    workstation_name: str = DEFAULT_WORKSTATION,
    preserve_tip_rack_incoming_class: bool = False,
    target_device: str = DEFAULT_TARGET_DEVICE,
    target_model: Optional[str] = None,
) -> Dict[str, Any]:
    """
    将 JSON 数据转换为 node-link 格式的字典

    Args:
        data: JSON 文件路径、字典数据、或 JSON 字符串
        workstation_name: 工作站名称，默认 "PRCXi"
        target_device: P6.1 新增，目标仪器名；透传给 :func:`convert_from_json`。
        target_model: P6.1.1 新增，同厂商内的型号名；透传给 :func:`convert_from_json`。

    Returns:
        Dict: node-link 格式的工作流数据
    """
    graph = convert_from_json(
        data,
        workstation_name,
        preserve_tip_rack_incoming_class=preserve_tip_rack_incoming_class,
        target_device=target_device,
        target_model=target_model,
    )
    return graph.to_node_link_dict()


def convert_json_to_workflow_list(
    data: Union[str, PathLike, Dict[str, Any]],
    workstation_name: str = DEFAULT_WORKSTATION,
    preserve_tip_rack_incoming_class: bool = True,
    target_device: str = DEFAULT_TARGET_DEVICE,
    target_model: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    将 JSON 数据转换为工作流列表格式

    Args:
        data: JSON 文件路径、字典数据、或 JSON 字符串
        workstation_name: 工作站名称，默认 "PRCXi"
        target_device: P6.1 新增，目标仪器名；透传给 :func:`convert_from_json`。
        target_model: P6.1.1 新增，同厂商内的型号名；透传给 :func:`convert_from_json`。

    Returns:
        List: 工作流节点列表
    """
    graph = convert_from_json(
        data,
        workstation_name,
        preserve_tip_rack_incoming_class=preserve_tip_rack_incoming_class,
        target_device=target_device,
        target_model=target_model,
    )
    return graph.to_dict()


# ==================== P5 — Workflow envelope ====================


def convert_json_to_workflow_envelope(
    data: Union[str, PathLike, Dict[str, Any]],
    *,
    target_lab_uuid: str = "",
    workflow_uuid: str = "",
    workflow_name: Optional[str] = None,
    name: Optional[str] = None,
    tags: Optional[List[str]] = None,
    workstation_name: str = DEFAULT_WORKSTATION,
    preserve_tip_rack_incoming_class: bool = False,
    target_device: str = DEFAULT_TARGET_DEVICE,
    target_model: Optional[str] = None,
) -> Dict[str, Any]:
    """把 transfer_actions JSON 转换为带「外壳」的 Cloud Lab 上传格式。

    与 :func:`convert_json_to_node_link` 的差异：本函数在 ``nodes / edges``
    之外补齐了前端 / Cloud 上传接口期望的顶层字段
    （``target_lab_uuid`` / ``name`` / ``data.workflow_uuid`` /
    ``data.workflow_name`` / ``data.tags``），并保持 ``nodes / edges`` 字节级
    与 :func:`convert_json_to_node_link` 完全一致。

    参数优先级（自顶向下取首个非空）：

    1. 显式传入：``workflow_name`` / ``tags`` / ``name``。
    2. 输入 JSON 顶层 ``metadata`` 段：``metadata.workflow_name`` /
       ``metadata.tags``（由 Stage 2 ``export_transfer_actions`` 写入）。
    3. 回退：空字符串 / 空列表，并打 :mod:`warnings` warning。

    UUID 类字段（``target_lab_uuid`` / ``workflow_uuid``）**不**自动生成；
    缺省保留空字符串，由调用方（前端 / 上传接口）写入。这样转换器输出
    的同一份协议是字节稳定的，便于 batch diff 与回归。

    Args:
        data: JSON 文件路径、字典数据、或 JSON 字符串。
            支持 P5 新增的顶层 ``metadata`` 字段，缺失时 fallback 空。
        target_lab_uuid: 目标实验台 UUID；默认空字符串。
        workflow_uuid: 工作流 UUID；默认空字符串（后端持久化时生成）。
        workflow_name: 工作流名称；缺省时取 ``metadata.workflow_name``。
        name: 列表页面展示标题；缺省时镜像 ``workflow_name``。
        tags: 工作流标签；缺省时取 ``metadata.tags``。
        workstation_name: 透传给 :func:`convert_from_json`。
        preserve_tip_rack_incoming_class: 透传给 :func:`convert_from_json`。
        target_device: P6.1 新增，目标仪器名；透传给 :func:`convert_from_json`。
        target_model: P6.1.1 新增，同厂商内的型号名；透传给 :func:`convert_from_json`。

    Returns:
        外壳化的 dict::

            {
                "target_lab_uuid": str,
                "name": str,
                "data": {
                    "workflow_uuid": str,
                    "workflow_name": str,
                    "tags": List[str],
                    "nodes": [...],
                    "edges": [...]
                }
            }
    """
    json_data = _load_json_data(data)

    # 1) 解析 P5 新增的顶层 metadata 段
    meta = json_data.get("metadata") if isinstance(json_data, dict) else None
    if not isinstance(meta, dict):
        meta = {}

    resolved_name = workflow_name if workflow_name else str(meta.get("workflow_name") or "")
    if tags is None:
        meta_tags = meta.get("tags")
        resolved_tags: List[str] = list(meta_tags) if isinstance(meta_tags, (list, tuple)) else []
    else:
        resolved_tags = list(tags)

    if not resolved_name:
        warnings.warn(
            "convert_json_to_workflow_envelope: workflow_name 为空，"
            "请检查 transfer_actions JSON 的 metadata.workflow_name 或显式传入 workflow_name"
        )
    if not resolved_tags:
        warnings.warn(
            "convert_json_to_workflow_envelope: tags 为空，"
            "请检查 README.md 的 ## Categories 段或显式传入 tags"
        )

    # 2) 复用 convert_from_json 构图（metadata 段对图构建透明）
    graph = convert_from_json(
        json_data,
        workstation_name,
        preserve_tip_rack_incoming_class=preserve_tip_rack_incoming_class,
        target_device=target_device,
        target_model=target_model,
    )
    node_link = graph.to_node_link_dict()

    # 3) 组装外壳；name 默认镜像 workflow_name，显式传入时覆盖
    return {
        "target_lab_uuid": target_lab_uuid,
        "name": name if name is not None else resolved_name,
        "data": {
            "workflow_uuid": workflow_uuid,
            "workflow_name": resolved_name,
            "tags": resolved_tags,
            "nodes": node_link.get("nodes", []),
            "edges": node_link.get("edges", []),
        },
    }
