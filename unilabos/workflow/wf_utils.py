"""
工作流工具模块

提供工作流上传等功能
"""

import json
import os
import uuid
from typing import Any, Dict, List, Optional

from unilabos.utils.banner_print import print_status


def _is_node_link_format(data: Dict[str, Any]) -> bool:
    """检查数据是否为 node-link 格式"""
    return "nodes" in data and "edges" in data


def _convert_to_node_link(
    workflow_file: str,
    workflow_data: Dict[str, Any],
    *,
    target_device: str = "prcxi",
    target_model: Optional[str] = None,
) -> Dict[str, Any]:
    """
    将非 node-link 格式的工作流数据转换为 node-link 格式

    Args:
        workflow_file: 工作流文件路径（用于日志）
        workflow_data: 原始工作流数据
        target_device: P6.1 新增，目标仪器名；透传给 :func:`convert_json_to_node_link`。
        target_model: P6.1.1 新增，同厂商内的型号名；透传给 :func:`convert_json_to_node_link`。

    Returns:
        node-link 格式的工作流数据
    """
    from unilabos.workflow.convert_from_json import convert_json_to_node_link

    model_hint = f" target_model={target_model}" if target_model else ""
    print_status(
        f"检测到非 node-link 格式，正在转换（target_device={target_device}{model_hint}）...",
        "info",
    )
    node_link_data = convert_json_to_node_link(
        workflow_data, target_device=target_device, target_model=target_model
    )
    print_status(f"转换完成", "success")
    return node_link_data


def upload_workflow(
    workflow_file: str,
    workflow_name: Optional[str] = None,
    tags: Optional[List[str]] = None,
    published: bool = False,
    description: str = "",
    target_device: str = "prcxi",
    target_model: Optional[str] = None,
) -> Dict[str, Any]:
    """
    上传工作流到服务器

    支持的输入格式：
    1. node-link 格式: {"nodes": [...], "edges": [...]}
    2. workflow/reagent 格式: {"workflow": [...], "reagent": {...}}
    3. steps_info/labware_info 格式: {"steps_info": [...], "labware_info": [...]}
    4. steps/labware 格式: {"steps": [...], "labware": [...]}

    Args:
        workflow_file: 工作流文件路径（JSON格式）
        workflow_name: 工作流名称，如果不提供则从文件中读取或使用文件名
        tags: 工作流标签列表，默认为空列表
        published: 是否发布工作流，默认为False
        description: 工作流描述，发布时使用
        target_device: P6.1 新增，目标仪器名（厂商粒度，如 ``prcxi`` / ``beckman`` / ``tecan``）。
            决定查 ``labware_mapping.yaml`` 中 ``target_devices.<target_device>.rules`` 段；未声明
            的名字由 loader 自动 fallback 到固定段 ``target_devices.default``。默认 ``"prcxi"``。
        target_model: P6.1.1 新增，同厂商内的型号名（如 ``"9320"`` / ``"4040"``）；
            决定 ``target_devices.<target_device>.models.<target_model>`` 段的 ``slot_remap`` /
            ``rules`` 覆盖。``None`` 表示走厂商级配置。

    Returns:
        Dict: API响应数据
    """
    # 延迟导入，避免在配置文件加载之前初始化 http_client
    from unilabos.app.web import http_client

    if not os.path.exists(workflow_file):
        print_status(f"工作流文件不存在: {workflow_file}", "error")
        return {"code": -1, "message": f"文件不存在: {workflow_file}"}

    # 读取工作流文件
    try:
        with open(workflow_file, "r", encoding="utf-8") as f:
            workflow_data = json.load(f)
    except json.JSONDecodeError as e:
        print_status(f"工作流文件JSON解析失败: {e}", "error")
        return {"code": -1, "message": f"JSON解析失败: {e}"}

    # P5：先把原始 transfer_actions JSON 的顶层 metadata 段抠出来，避免后续
    # _convert_to_node_link 转换后丢失 metadata.workflow_name / metadata.tags。
    # 兼容：旧 node-link 文件没有 metadata 段时为空 dict。
    orig_metadata = workflow_data.get("metadata") if isinstance(workflow_data, dict) else None
    if not isinstance(orig_metadata, dict):
        orig_metadata = {}

    # 从 JSON 文件中提取 description 和 tags（作为 fallback）
    # tags fallback 链：CLI 显式 > metadata.tags（P5）> 顶层 tags（旧字段）> 空列表
    if not description and "description" in workflow_data:
        description = workflow_data["description"]
        print_status(f"从文件中读取 description", "info")
    if not tags:
        meta_tags = orig_metadata.get("tags")
        if isinstance(meta_tags, (list, tuple)) and meta_tags:
            tags = list(meta_tags)
            print_status(f"从 metadata.tags 读取 tags: {tags}", "info")
        elif "tags" in workflow_data:
            tags = workflow_data["tags"]
            print_status(f"从文件顶层读取 tags: {tags}", "info")

    # 自动检测并转换格式
    if not _is_node_link_format(workflow_data):
        try:
            workflow_data = _convert_to_node_link(
                workflow_file,
                workflow_data,
                target_device=target_device,
                target_model=target_model,
            )
        except Exception as e:
            print_status(f"工作流格式转换失败: {e}", "error")
            return {"code": -1, "message": f"格式转换失败: {e}"}

    # 提取工作流数据
    nodes = workflow_data.get("nodes", [])
    edges = workflow_data.get("edges", [])
    workflow_uuid_val = workflow_data.get("workflow_uuid", str(uuid.uuid4()))

    # 工作流名称 fallback 链（优先级自顶向下，取首个非空）：
    #   1. CLI 显式 -n/--workflow_name
    #   2. P5 顶层 metadata.workflow_name（transfer_actions JSON 主路径）
    #   3. 转换后 workflow_data 顶层 workflow_name（旧 node-link 形态遗留字段）
    #   4. 文件名（去 .json 后缀）兜底
    meta_wf_name = str(orig_metadata.get("workflow_name") or "").strip()
    legacy_top_name = str(workflow_data.get("workflow_name") or "").strip()
    fallback_filename = os.path.basename(workflow_file).replace(".json", "")
    wf_name_from_file = meta_wf_name or legacy_top_name or fallback_filename

    # 确定工作流名称
    final_name = workflow_name or wf_name_from_file
    if not workflow_name:
        if meta_wf_name:
            print_status(f"使用 metadata.workflow_name: {meta_wf_name}", "info")
        elif legacy_top_name:
            print_status(f"使用文件顶层 workflow_name（旧字段）: {legacy_top_name}", "info")
        else:
            print_status(
                f"metadata.workflow_name 与顶层 workflow_name 均为空，回退到文件名: {fallback_filename}",
                "warning",
            )

    print_status(f"正在上传工作流: {final_name}", "info")
    print_status(f"  - 节点数量: {len(nodes)}", "info")
    print_status(f"  - 边数量: {len(edges)}", "info")
    print_status(f"  - 标签: {tags or []}", "info")
    print_status(f"  - 描述: {description[:50]}{'...' if len(description) > 50 else ''}", "info")
    print_status(f"  - 发布状态: {published}", "info")
    print_status(f"  - 目标仪器: {target_device}", "info")
    if target_model:
        print_status(f"  - 目标型号: {target_model}", "info")

    # 调用 http_client 上传
    result = http_client.workflow_import(
        name=final_name,
        workflow_uuid=workflow_uuid_val,
        workflow_name=final_name,
        nodes=nodes,
        edges=edges,
        tags=tags,
        published=published,
        description=description,
    )

    if result.get("code") == 0:
        data = result.get("data", {})
        print_status(f"工作流上传成功！{data}", "success")
        print_status(f"  - UUID: {data.get('uuid', 'N/A')}", "info")
        print_status(f"  - 名称: {data.get('name', 'N/A')}", "info")
    else:
        print_status(f"工作流上传失败: {result.get('message', '未知错误')}", "error")

    return result


def handle_workflow_upload_command(args_dict: Dict[str, Any]) -> None:
    """
    处理 workflow_upload 子命令

    Args:
        args_dict: 命令行参数字典；
            - P6.1 新增 ``target_device`` key（缺省 ``"prcxi"``）。
            - P6.1.1 新增 ``target_model`` key（缺省 ``None``）。
    """
    workflow_file = args_dict.get("workflow_file")
    workflow_name = args_dict.get("workflow_name")
    tags = args_dict.get("tags", [])
    published = args_dict.get("published", False)
    description = args_dict.get("description", "")
    target_device = args_dict.get("target_device") or "prcxi"
    target_model = args_dict.get("target_model") or None

    if workflow_file:
        upload_workflow(
            workflow_file,
            workflow_name,
            tags,
            published,
            description,
            target_device=target_device,
            target_model=target_model,
        )
    else:
        print_status("未指定工作流文件路径，请使用 -f/--workflow_file 参数", "error")
