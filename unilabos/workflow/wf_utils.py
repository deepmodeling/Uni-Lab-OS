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


def _convert_to_node_link(workflow_file: str, workflow_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    将非 node-link 格式的工作流数据转换为 node-link 格式

    Args:
        workflow_file: 工作流文件路径（用于日志）
        workflow_data: 原始工作流数据

    Returns:
        node-link 格式的工作流数据
    """
    from unilabos.workflow.convert_from_json import convert_json_to_node_link

    print_status(f"检测到非 node-link 格式，正在转换...", "info")
    node_link_data = convert_json_to_node_link(workflow_data)
    print_status(f"转换完成", "success")
    return node_link_data


def upload_workflow(
    workflow_file: str,
    workflow_name: Optional[str] = None,
    tags: Optional[List[str]] = None,
    published: bool = False,
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

    # 自动检测并转换格式
    if not _is_node_link_format(workflow_data):
        try:
            workflow_data = _convert_to_node_link(workflow_file, workflow_data)
        except Exception as e:
            print_status(f"工作流格式转换失败: {e}", "error")
            return {"code": -1, "message": f"格式转换失败: {e}"}

    # 提取工作流数据
    nodes = workflow_data.get("nodes", [])
    edges = workflow_data.get("edges", [])
    workflow_uuid_val = workflow_data.get("workflow_uuid", str(uuid.uuid4()))
    wf_name_from_file = workflow_data.get("workflow_name", os.path.basename(workflow_file).replace(".json", ""))

    # 确定工作流名称
    final_name = workflow_name or wf_name_from_file

    print_status(f"正在上传工作流: {final_name}", "info")
    print_status(f"  - 节点数量: {len(nodes)}", "info")
    print_status(f"  - 边数量: {len(edges)}", "info")
    print_status(f"  - 标签: {tags or []}", "info")
    print_status(f"  - 发布状态: {published}", "info")

    # 调用 http_client 上传
    result = http_client.workflow_import(
        name=final_name,
        workflow_uuid=workflow_uuid_val,
        workflow_name=final_name,
        nodes=nodes,
        edges=edges,
        tags=tags,
        published=published,
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
        args_dict: 命令行参数字典
    """
    workflow_file = args_dict.get("workflow_file")
    workflow_name = args_dict.get("workflow_name")
    tags = args_dict.get("tags", [])
    published = args_dict.get("published", False)

    if workflow_file:
        upload_workflow(workflow_file, workflow_name, tags, published)
    else:
        print_status("未指定工作流文件路径，请使用 -f/--workflow_file 参数", "error")
