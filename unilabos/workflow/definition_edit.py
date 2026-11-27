"""工作流定义（Workflow Definition）的纯图变换。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping
from uuid import uuid4

from unilabos.workflow.composite_graph_rewrite import remap_control_references
from unilabos.workflow.composite_invocation import _remap_nested_composite_metadata
from unilabos.workflow.models import normalize_json_object, validate_uuid


class WorkflowDefinitionInvalid(ValueError):
    """增量编辑输入不能形成合法的完整工作流图。"""


_STANDALONE_NODE_TYPES = {
    "compute",
    "condition",
    "repeat_until",
    "script",
    "group",
    "tool_call",
}
# Registry templates historically expose ordinary device actions as ``ILab``
# (and a few older aliases), while the workflow planner normalizes those values
# to ``device_action``.  Manual-confirmation wrapping must use the same
# normalization; checking only the planner's canonical spelling made the
# public node-create API reject real device templates.
_DEVICE_ACTION_TEMPLATE_TYPES = frozenset(
    {"device_action", "ilab", "device", "action", "resource_action"}
)
_PATCHABLE_NODE_FIELDS = {
    "parent_uuid",
    "material_uuid",
    "name",
    "pose",
    "param",
    "manual_confirmation",
    "execution_policy",
    "disabled",
    "minimized",
    "script",
    "description",
    "meta_data",
}


def _optional_text(value: Any) -> str | None:
    """规范化可空文本。"""

    if value is None:
        return None
    if not isinstance(value, str):
        raise WorkflowDefinitionInvalid("文本字段必须是字符串或 null")
    normalized = value.strip()
    return normalized or None


def _required_text(value: Any, field: str) -> str:
    """规范化必填非空文本。"""

    normalized = _optional_text(value)
    if normalized is None:
        raise WorkflowDefinitionInvalid(f"{field} 不能为空")
    return normalized


def _optional_uuid(value: Any, field: str) -> str | None:
    """规范化可空 UUID。"""

    if value is None:
        return None
    try:
        return validate_uuid(value)
    except (TypeError, ValueError):
        raise WorkflowDefinitionInvalid(f"{field} 不是有效 UUID") from None


def _template_node(
    payload: Mapping[str, Any],
    template: Mapping[str, Any],
) -> dict[str, Any]:
    """从模板派生不可由调用方伪造的节点字段。"""

    template_type = _required_text(template.get("node_type"), "模板 node_type")
    requested_type = str(payload.get("type") or "").strip()
    # 导出/完整图会带上模板派生的 type。导入时仍以模板为准，不能因为该字段
    # 存在就拒绝；只有把设备动作包成人工确认时才允许覆盖。
    if (
        requested_type.lower() == "manual_confirm"
        and template_type.lower() in _DEVICE_ACTION_TEMPLATE_TYPES
    ):
        template_type = "manual_confirm"
    name = str(payload.get("name") or "").strip()
    if not name:
        name = _required_text(
            template.get("display_name") or template.get("name"),
            "节点 name",
        )
    param = payload.get("param")
    if param is None:
        param = template.get("goal_default") or template.get("goal") or {}
    return {
        "name": name,
        "type": template_type,
        "icon": _optional_text(template.get("icon")),
        "footer": _optional_text(template.get("footer")),
        "action_name": _optional_text(template.get("name")),
        "action_type": _optional_text(template.get("type")),
        "param": normalize_json_object(param),
    }


def create_node(
    *,
    payload: Mapping[str, Any],
    template: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """把节点创建 DTO 转成可提交给完整图保存器的定义。"""

    template_uuid = _optional_uuid(
        payload.get("workflow_node_template_uuid"),
        "workflow_node_template_uuid",
    )
    if template_uuid is None:
        node_type = _required_text(payload.get("type"), "type")
        if node_type not in _STANDALONE_NODE_TYPES:
            raise WorkflowDefinitionInvalid("无模板节点的 type 不受支持")
        derived = {
            "name": _required_text(payload.get("name"), "name"),
            "type": node_type,
            "icon": None,
            "footer": None,
            "action_name": None,
            "action_type": None,
            "param": normalize_json_object(payload.get("param")),
        }
    else:
        if template is None or str(template.get("uuid")) != template_uuid:
            raise WorkflowDefinitionInvalid("工作流节点模板不存在")
        derived = _template_node(payload, template)
    return {
        "uuid": str(uuid4()),
        "workflow_node_template_uuid": template_uuid,
        "parent_uuid": _optional_uuid(payload.get("parent_uuid"), "parent_uuid"),
        "material_uuid": _optional_uuid(payload.get("material_uuid"), "material_uuid"),
        **derived,
        "pose": normalize_json_object(payload.get("pose")),
        "manual_confirmation": normalize_json_object(
            payload.get("manual_confirmation")
        ),
        "execution_policy": normalize_json_object(payload.get("execution_policy")),
        "disabled": bool(payload.get("disabled", False)),
        "minimized": bool(payload.get("minimized", False)),
        "script": _optional_text(payload.get("script")),
        "description": _optional_text(payload.get("description")),
        "meta_data": normalize_json_object(payload.get("meta_data")),
    }


def patch_node(node: Mapping[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    """在独立副本上应用 Backend 允许的节点局部更新字段。"""

    if not patch or set(patch) - _PATCHABLE_NODE_FIELDS:
        raise WorkflowDefinitionInvalid("节点 patch 没有可应用字段")
    result = deepcopy(dict(node))
    for field, value in patch.items():
        if field in {"parent_uuid", "material_uuid"}:
            result[field] = _optional_uuid(value, field)
        elif field == "name":
            result[field] = _required_text(value, field)
        elif field in {
            "pose",
            "param",
            "manual_confirmation",
            "execution_policy",
            "meta_data",
        }:
            result[field] = normalize_json_object(value)
        elif field in {"disabled", "minimized"}:
            if not isinstance(value, bool):
                raise WorkflowDefinitionInvalid(f"{field} 必须是布尔值")
            result[field] = value
        else:
            result[field] = _optional_text(value)
    return result


def duplicate_node(node: Mapping[str, Any], *, name: str | None) -> dict[str, Any]:
    """复制节点定义并生成新身份；连线不随单节点复制。"""

    result = deepcopy(dict(node))
    result["uuid"] = str(uuid4())
    result["name"] = _required_text(
        name if name is not None else f"{node['name']} copy",
        "name",
    )
    for field in ("create_time", "update_time", "deleted_at", "workflow_uuid"):
        result.pop(field, None)
    return result


def create_edge(payload: Mapping[str, Any]) -> dict[str, Any]:
    """构造一条由完整图验证器最终裁决的工作流连线。"""

    return {
        "uuid": str(uuid4()),
        "source_node_uuid": _optional_uuid(payload.get("source_node_uuid"), "source_node_uuid"),
        "target_node_uuid": _optional_uuid(payload.get("target_node_uuid"), "target_node_uuid"),
        "source_handle_uuid": _optional_uuid(payload.get("source_handle_uuid"), "source_handle_uuid"),
        "target_handle_uuid": _optional_uuid(payload.get("target_handle_uuid"), "target_handle_uuid"),
        "description": _optional_text(payload.get("description")),
        "meta_data": normalize_json_object(payload.get("meta_data")),
    }


def duplicate_graph(
    graph: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """复制完整图并重建节点、父子、连线和数量需求身份引用。"""

    node_identity = {str(node["uuid"]): str(uuid4()) for node in graph["nodes"]}
    nodes: list[dict[str, Any]] = []
    for source in graph["nodes"]:
        node = deepcopy(dict(source))
        node["uuid"] = node_identity[str(source["uuid"])]
        parent_uuid = source.get("parent_uuid")
        node["parent_uuid"] = node_identity.get(str(parent_uuid)) if parent_uuid else None
        # 复制必须同步改写循环/条件参数和嵌套组合边界，避免引用原图节点。
        node["param"] = remap_control_references(source.get("param"), node_identity)
        node["meta_data"] = _remap_nested_composite_metadata(
            source.get("meta_data") or {}, node_identity
        )
        for field in ("create_time", "update_time", "deleted_at", "workflow_uuid"):
            node.pop(field, None)
        nodes.append(node)
    edges: list[dict[str, Any]] = []
    for source in graph["edges"]:
        edge = deepcopy(dict(source))
        edge["uuid"] = str(uuid4())
        edge["source_node_uuid"] = node_identity[str(source["source_node_uuid"])]
        edge["target_node_uuid"] = node_identity[str(source["target_node_uuid"])]
        for field in ("create_time", "update_time", "deleted_at"):
            edge.pop(field, None)
        edges.append(edge)
    requirements: list[dict[str, Any]] = []
    for source in graph.get("inventory_requirements", []):
        requirement = deepcopy(dict(source))
        requirement["uuid"] = str(uuid4())
        requirement["consume_node_uuid"] = node_identity[
            str(source["consume_node_uuid"])
        ]
        metadata = requirement.get("meta_data")
        unilab = metadata.get("unilab") if isinstance(metadata, dict) else None
        source_node_uuid = (
            unilab.get("material_source_node_uuid")
            if isinstance(unilab, dict)
            else None
        )
        if source_node_uuid is not None:
            unilab["material_source_node_uuid"] = node_identity[
                str(source_node_uuid)
            ]
        for field in (
            "create_time",
            "update_time",
            "deleted_at",
            "workflow_uuid",
            "sort_order",
        ):
            requirement.pop(field, None)
        requirements.append(requirement)
    return nodes, edges, requirements


__all__ = [
    "WorkflowDefinitionInvalid",
    "create_edge",
    "create_node",
    "duplicate_graph",
    "duplicate_node",
    "patch_node",
]
