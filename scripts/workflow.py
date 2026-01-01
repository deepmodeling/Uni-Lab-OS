import json
import logging
import traceback
import uuid
from typing import Any, Dict, List

import networkx as nx
import matplotlib.pyplot as plt
import requests

logger = logging.getLogger(__name__)


class SimpleGraph:
    """简单的有向图实现，用于构建工作流图"""

    def __init__(self):
        self.nodes = {}
        self.edges = []

    def add_node(self, node_id, **attrs):
        """添加节点"""
        self.nodes[node_id] = attrs

    def add_edge(self, source, target, **attrs):
        """添加边"""
        # edge = {"source": source, "target": target, **attrs}
        edge = {
            "source": source, "target": target,
            "source_node_uuid": source,
            "target_node_uuid": target,
            "source_handle_io": "source",
            "target_handle_io": "target",
            **attrs
        }
        self.edges.append(edge)

    def to_dict(self):
        """转换为工作流图格式"""
        nodes_list = []
        for node_id, attrs in self.nodes.items():
            node_attrs = attrs.copy()
            params = node_attrs.pop("parameters", {}) or {}
            node_attrs.update(params)
            nodes_list.append({"id": node_id, **node_attrs})

        return {
            "directed": True,
            "multigraph": False,
            "graph": {},
            "nodes": nodes_list,
            "edges": self.edges,
            "links": self.edges,
        }


def extract_json_from_markdown(text: str) -> str:
    """从markdown代码块中提取JSON"""
    text = text.strip()
    if text.startswith("```json\n"):
        text = text[8:]
    if text.startswith("```\n"):
        text = text[4:]
    if text.endswith("\n```"):
        text = text[:-4]
    return text






def create_workflow(
    steps_info: str,
    labware_info: str,
    workflow_name: str = "Generated Workflow",
    workstation_name: str = "workstation",
    workflow_description: str = "Auto-generated workflow from protocol",
) -> Dict[str, Any]:
    """
    创建工作流，输入数据已经是统一的字典格式

    Args:
        steps_info: 步骤信息 (JSON字符串，已经是list of dict格式)
        labware_info: 实验器材和试剂信息 (JSON字符串，已经是list of dict格式)
        workflow_name: 工作流名称
        workflow_description: 工作流描述

    Returns:
        创建结果，包含工作流UUID和详细信息
    """
    try:
        # 直接解析JSON数据
        steps_info_clean = extract_json_from_markdown(steps_info)
        labware_info_clean = extract_json_from_markdown(labware_info)

        steps_data = json.loads(steps_info_clean)
        labware_data = json.loads(labware_info_clean)

        # 统一处理所有数据
        protocol_graph = build_protocol_graph(labware_data, steps_data, workstation_name=workstation_name)

        # 检测协议类型（用于标签）
        protocol_type = "bio" if any("biomek" in step.get("template", "") for step in refactored_steps) else "organic"

        # 转换为工作流格式
        data = protocol_graph.to_dict()

        # 转换节点格式
        for i, node in enumerate(data["nodes"]):
            description = node.get("description", "")
            onode = {
                "template": node.pop("template"),
                "id": node["id"],
                "lab_node_type": node.get("lab_node_type", "Device"),
                "name": description or f"Node {i + 1}",
                "params": {"default": node},
                "handles": {},
            }

            # 处理边连接
            for edge in data["links"]:
                if edge["source"] == node["id"]:
                    source_port = edge.get("source_port", "output")
                    if source_port not in onode["handles"]:
                        onode["handles"][source_port] = {"type": "source"}

                if edge["target"] == node["id"]:
                    target_port = edge.get("target_port", "input")
                    if target_port not in onode["handles"]:
                        onode["handles"][target_port] = {"type": "target"}

            data["nodes"][i] = onode

        # 发送到API创建工作流
        api_secret = configs.Lab.Key
        if not api_secret:
            return {"error": "API SecretKey is not configured", "success": False}

        # Step 1: 创建工作流
        workflow_url = f"{configs.Lab.Api}/api/v1/workflow/"
        headers = {
            "Content-Type": "application/json",
        }
        params = {"secret_key": api_secret}

        graph_data = {"name": workflow_name, **data}

        logger.info(f"Creating workflow: {workflow_name}")
        response = requests.post(
            workflow_url, params=params, json=graph_data, headers=headers, timeout=configs.Lab.Timeout
        )
        response.raise_for_status()

        workflow_info = response.json()

        if workflow_info.get("code") != 0:
            error_msg = f"API returned an error: {workflow_info.get('msg', 'Unknown Error')}"
            logger.error(error_msg)
            return {"error": error_msg, "success": False}

        workflow_uuid = workflow_info.get("data", {}).get("uuid")
        if not workflow_uuid:
            return {"error": "Failed to get workflow UUID from response", "success": False}

        # Step 2: 添加到模板库（可选）
        try:
            library_url = f"{configs.Lab.Api}/api/flociety/vs/workflows/library/"
            lib_payload = {
                "workflow_uuid": workflow_uuid,
                "title": workflow_name,
                "description": workflow_description,
                "labels": [protocol_type.title(), "Auto-generated"],
            }

            library_response = requests.post(
                library_url, params=params, json=lib_payload, headers=headers, timeout=configs.Lab.Timeout
            )
            library_response.raise_for_status()

            library_info = library_response.json()
            logger.info(f"Workflow added to library: {library_info}")

            return {
                "success": True,
                "workflow_uuid": workflow_uuid,
                "workflow_info": workflow_info.get("data"),
                "library_info": library_info.get("data"),
                "protocol_type": protocol_type,
                "message": f"Workflow '{workflow_name}' created successfully",
            }

        except Exception as e:
            # 即使添加到库失败，工作流创建仍然成功
            logger.warning(f"Failed to add workflow to library: {str(e)}")
            return {
                "success": True,
                "workflow_uuid": workflow_uuid,
                "workflow_info": workflow_info.get("data"),
                "protocol_type": protocol_type,
                "message": f"Workflow '{workflow_name}' created successfully (library addition failed)",
            }

    except requests.exceptions.RequestException as e:
        error_msg = f"Network error when calling API: {str(e)}"
        logger.error(error_msg)
        return {"error": error_msg, "success": False}
    except json.JSONDecodeError as e:
        error_msg = f"JSON parsing error: {str(e)}"
        logger.error(error_msg)
        return {"error": error_msg, "success": False}
    except Exception as e:
        error_msg = f"An unexpected error occurred: {str(e)}"
        logger.error(error_msg)
        logger.error(traceback.format_exc())
        return {"error": error_msg, "success": False}
