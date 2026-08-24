"""
工作流工具模块

提供工作流上传等功能
"""

import json
import os
from typing import Any, Dict, List, Optional, Protocol

from unilabos.utils.banner_print import print_status


class WorkflowUploadClient(Protocol):
    def create_workflow(
        self,
        *,
        name: str,
        tags: list[Any],
        description: Optional[str],
        meta_data: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]: ...

    def save_workflow_graph(
        self,
        workflow_uuid: str,
        *,
        revision: int,
        nodes: list[dict[str, Any]],
        edges: list[dict[str, Any]],
    ) -> dict[str, Any]: ...


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
    from unilabos.server.workflow.convert_from_json import convert_json_to_node_link

    print_status("检测到非 node-link 格式，正在转换...", "info")
    node_link_data = convert_json_to_node_link(workflow_data)
    print_status("转换完成", "success")
    return node_link_data


def upload_workflow(
    client: WorkflowUploadClient,
    workflow_file: str,
    workflow_name: Optional[str] = None,
    tags: Optional[List[str]] = None,
    published: bool = False,
    description: str = "",
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

    Returns:
        Dict: API响应数据
    """
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

    # 从 JSON 文件中提取 description 和 tags（作为 fallback）
    if not description and "description" in workflow_data:
        description = workflow_data["description"]
        print_status("从文件中读取 description", "info")
    if not tags and "tags" in workflow_data:
        tags = workflow_data["tags"]
        print_status(f"从文件中读取 tags: {tags}", "info")

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
    wf_name_from_file = workflow_data.get("workflow_name", os.path.basename(workflow_file).replace(".json", ""))

    # 确定工作流名称
    final_name = workflow_name or wf_name_from_file

    print_status(f"正在上传工作流: {final_name}", "info")
    print_status(f"  - 节点数量: {len(nodes)}", "info")
    print_status(f"  - 边数量: {len(edges)}", "info")
    print_status(f"  - 标签: {tags or []}", "info")
    print_status(f"  - 描述: {description[:50]}{'...' if len(description) > 50 else ''}", "info")
    print_status(f"  - 发布状态: {published}", "info")

    created = client.create_workflow(
        name=final_name,
        tags=tags or [],
        description=description or None,
        meta_data={
            "source": "unilab-workflow-upload",
            "published_requested": bool(published),
            "source_workflow_uuid": workflow_data.get("workflow_uuid"),
        },
    )
    workflow_uuid = str(created["uuid"])
    graph = client.save_workflow_graph(
        workflow_uuid,
        revision=int(created["revision"]),
        nodes=nodes,
        edges=edges,
    )
    detail = {"workflow": created, "graph": graph}
    print_status(f"工作流上传成功！{workflow_uuid}", "success")
    print_status(f"  - UUID: {workflow_uuid}", "info")
    print_status(f"  - 名称: {created.get('name', final_name)}", "info")
    return {"code": 0, "data": detail}


__all__ = ["WorkflowUploadClient", "upload_workflow"]
