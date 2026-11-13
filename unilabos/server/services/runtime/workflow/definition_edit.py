"""工作流定义的纯图变换：V1 增量节点 / 连线编辑与按作者层级读写（移植自 V1 ``definition_edit`` / ``definition_hierarchy``）。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping
from uuid import uuid4

from unilabos.protocol.runtime.workflow import (
    normalize_json_object,
    normalize_manual_confirmation,
    validate_uuid,
)


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


def _manual_confirmation(value: Any) -> dict[str, Any]:
    try:
        return normalize_manual_confirmation(value)
    except ValueError as error:
        raise WorkflowDefinitionInvalid(str(error)) from None


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
        "manual_confirmation": _manual_confirmation(payload.get("manual_confirmation")),
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
        elif field == "manual_confirmation":
            result[field] = _manual_confirmation(value)
        elif field in {
            "pose",
            "param",
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



def private_node_owners(nodes: list[dict[str, Any]]) -> dict[str, str]:
    """返回私有后代到最外层子操作入口的映射，不跨越当前定义边界。"""
    index = {node["uuid"]: node for node in nodes}
    result = {}
    for node in nodes:
        current = node
        seen = {node["uuid"]}
        while current.get("parent_uuid"):
            parent = current["parent_uuid"]
            if parent not in index or parent in seen:
                raise ValueError("工作流定义的父节点不存在或循环")
            current = index[parent]
            seen.add(parent)
            if current.get("meta_data", {}).get("unilab", {}).get("composite"):
                result[node["uuid"]] = parent
    return result


def definition_graph(graph: Mapping[str, Any]) -> dict[str, Any]:
    """返回本层节点和边，保留子操作合同 pin、具名实参及外部输出接线。"""
    owners = private_node_owners(graph["nodes"])
    visible = {node["uuid"] for node in graph["nodes"]} - owners.keys()
    result = deepcopy(dict(graph))
    result["nodes"] = [node for node in result["nodes"] if node["uuid"] in visible]
    result["edges"] = [
        edge
        for edge in result["edges"]
        if edge["source_node_uuid"] in visible and edge["target_node_uuid"] in visible
    ]
    if "inventory_requirements" in result:
        # 子操作的数量需求在其定义中查看；本层只列自己声明的需求。
        result["inventory_requirements"] = [
            item
            for item in result.get("inventory_requirements") or []
            if str(item.get("consume_node_uuid") or "") in visible
            and not ((item.get("meta_data") or {}).get("unilab") or {}).get("composite_origin")
        ]
    templates = {node.get("workflow_node_template_uuid") for node in result["nodes"]}
    result["node_templates"] = [
        item for item in result.get("node_templates", []) if item["uuid"] in templates
    ]
    result["handle_templates"] = [
        item
        for item in result.get("handle_templates", [])
        if item["workflow_node_template_uuid"] in templates
    ]
    unilab = result["workflow"].get("meta_data", {}).get("unilab", {})
    if "resource_scopes" in unilab:
        # 子操作自己的锁区间在其定义中查看；外层锁仍保留操作入口的覆盖范围。
        scopes = []
        for scope in unilab["resource_scopes"]:
            if scope.get("composite_invocation_uuid"):
                continue
            scope["node_uuids"] = list(
                dict.fromkeys(
                    owners.get(uid, uid) for uid in scope.get("node_uuids", [])
                )
            )
            for key in ("entry_node_uuid", "exit_node_uuid"):
                if scope.get(key) in owners:
                    scope[key] = owners[scope[key]]
            scopes.append(scope)
        unilab["resource_scopes"] = scopes
    return result


def restore_private_subgraphs(
    *,
    applied: Mapping[str, Any],
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """公共定义图回写时恢复保留入口的冻结子图，再走完整校验和源码重编译。

    已提供内部节点的既有客户端走原路径；删除某入口会同时删除它的私有子图。
    新入口只能通过正常编译/插入流程建立，不能从其他父工作流复制私有执行身份。
    """
    owners = private_node_owners(applied["nodes"])
    supplied = {node["uuid"] for node in nodes}
    if not owners or supplied & owners.keys():
        return nodes, edges
    retained = {uid for uid, owner in owners.items() if owner in supplied}
    restored_nodes = [
        *nodes,
        *(deepcopy(node) for node in applied["nodes"] if node["uuid"] in retained),
    ]
    available = supplied | retained
    edge_ids = {edge["uuid"] for edge in edges}
    restored_edges = [
        *edges,
        *(
            deepcopy(edge)
            for edge in applied["edges"]
            if edge["uuid"] not in edge_ids
            and edge["source_node_uuid"] in available
            and edge["target_node_uuid"] in available
            and (
                edge["source_node_uuid"] in retained
                or edge["target_node_uuid"] in retained
            )
        ),
    ]
    return restored_nodes, restored_edges


__all__ = [
    "WorkflowDefinitionInvalid",
    "create_edge",
    "definition_graph",
    "create_node",
    "duplicate_graph",
    "duplicate_node",
    "patch_node",
    "private_node_owners",
    "restore_private_subgraphs",
]
