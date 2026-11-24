"""工作流定义的纯图变换：V1 增量节点 / 连线编辑与按作者层级读写（移植自 V1 ``definition_edit`` / ``definition_hierarchy``）。"""

from __future__ import annotations

import json
import math
from collections import Counter
from copy import deepcopy
from typing import Any, Mapping
from uuid import NAMESPACE_URL, uuid4, uuid5

from unilabos.protocol.runtime.inventory import inventory_deferred, normalize_inventory
from unilabos.protocol.runtime.workflow import (
    normalize_json_object,
    normalize_manual_confirmation,
    validate_uuid,
)
from unilabos.server.services.runtime.workflow.errors import StoreConflict


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


def remap_node_references(value: Any, identity: Mapping[str, str]) -> Any:
    """按身份表改写 ``value`` 里所有指向节点的引用，返回新对象。

    节点身份是 UUID：任何与某个旧节点身份相等的字符串（值或字典键）都是对该节点的引用，
    不按字段逐个列举形状。图里现有的形状：``parent_uuid``（分组 / 循环体 / 组合调用私有子图）、
    连线两端、``execution_policy`` 的 ``depends_on`` 与 ``condition_guards[].node_uuid``、条件与循环
    表达式的 ``{"node_uuid"}``、旧式循环条件 ``condition.node_uuid``、参数 / 循环 ``carry_next`` /
    库存需求里的结果引用 ``{"kind": "node_output", "workflow_node_uuid"}``、组合调用实参的结果引用，
    以及组合调用 ``meta_data.unilab.composite`` 的边界映射与资源作用域成员。
    """

    if isinstance(value, str):
        return identity.get(value, value)
    if isinstance(value, Mapping):
        return {
            identity.get(key, key) if isinstance(key, str) else key: remap_node_references(item, identity)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [remap_node_references(item, identity) for item in value]
    return deepcopy(value)


def duplicate_graph(
    graph: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, str]]:
    """复制完整图：节点与连线换新身份，图里所有指向节点的引用随之改写。

    返回 ``(节点, 连线, 旧身份 → 新身份)``；调用方用同一张表改写图外（例如工作流元数据）的引用。
    """

    identity = {str(node["uuid"]): str(uuid4()) for node in graph["nodes"]}
    nodes: list[dict[str, Any]] = []
    for source in graph["nodes"]:
        node = remap_node_references(source, identity)
        for field in ("create_time", "update_time", "deleted_at", "workflow_uuid"):
            node.pop(field, None)
        nodes.append(node)
    edges: list[dict[str, Any]] = []
    for source in graph["edges"]:
        edge = remap_node_references(source, identity)
        edge["uuid"] = str(uuid4())
        for field in ("create_time", "update_time", "deleted_at"):
            edge.pop(field, None)
        edges.append(edge)
    return nodes, edges, identity


def node_or_ancestor_disabled(node: Mapping[str, Any], nodes: Mapping[str, Mapping[str, Any]]) -> bool:
    """节点自己或任一祖先（分组 / 循环 / 组合调用）被禁用：不进执行计划，需求不预留、不列出。"""

    current: Mapping[str, Any] | None = node
    seen: set[str] = set()
    while current is not None:
        if bool(current.get("disabled", False)):
            return True
        parent_uuid = current.get("parent_uuid")
        if parent_uuid is None:
            return False
        identity = str(parent_uuid)
        if identity in seen:
            raise StoreConflict("workflow node parent hierarchy contains a cycle")
        seen.add(identity)
        current = nodes.get(identity)
    return False


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


#: 节点库存需求 kind → V1 ``target_type``（content 是容器里的当前内容物）
_V1_TARGET_TYPES = {"material": "material", "lot": "lot", "content": "current_substance"}
#: 按件物料在 V1 列表里的数量与单位（V1 只有"数量 + 单位"，按件即 1 件）
_MATERIAL_QUANTITY = (1.0, "件")
#: 数量 / 单位是运行时引用（workflow_input / node_output）时 V1 列表里的占位单位
_DYNAMIC_UNIT = "运行时决定"


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _declared_inventory(node: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    meta = node.get("meta_data") if isinstance(node.get("meta_data"), Mapping) else {}
    return [item for item in meta.get("inventory_requirements") or [] if isinstance(item, Mapping)]


def _listable(item: Mapping[str, Any]) -> bool:
    """V1 列表能表达的需求条目：kind 已知且 key 非空。"""

    return item.get("kind") in _V1_TARGET_TYPES and bool(str(item.get("key") or ""))


def project_inventory_requirements(graph: Mapping[str, Any]) -> list[dict[str, Any]]:
    """节点 ``meta_data.inventory_requirements`` → V1 图根 ``inventory_requirements``（只读投影）。

    节点级需求是调度、预检与绑定的唯一来源；V1 图根列表只给客户端看，每次读取都从节点派生，
    不单独存储。V1 形状没有 kind / 模板 / 批次 / 容器身份，原条目原样放进
    ``meta_data.unilab.inventory``；数量先按调度同一规则规范化（``normalize_inventory``），
    是运行时引用时记 0 并标 ``quantity_dynamic``。

    列哪些条目与执行计划同一规则：禁用节点及其后代不执行，需求不列出；组合调用的私有子图只在子操作
    定义里查看，本层不列出。``requirement_key`` 在同一 key 出现在执行计划多个节点（含私有子图）时写成
    ``<node_uuid>/<key>``：任务绑定按执行计划解析 key，列表给出的 key 必须能直接拿去绑定。
    """

    workflow = graph.get("workflow") if isinstance(graph.get("workflow"), Mapping) else {}
    workflow_uuid = str(workflow.get("uuid") or "")
    nodes = [node for node in graph.get("nodes") or [] if isinstance(node, Mapping)]
    index = {str(node["uuid"]): node for node in nodes}
    executed = [node for node in nodes if not node_or_ancestor_disabled(node, index)]
    key_counts = Counter(
        str(item["key"]) for node in executed for item in _declared_inventory(node) if _listable(item)
    )
    private = private_node_owners(nodes)

    projected: list[dict[str, Any]] = []
    for node in executed:
        node_uuid = str(node["uuid"])
        if node_uuid in private:
            continue
        declared = _declared_inventory(node)
        meta = node.get("meta_data") if isinstance(node.get("meta_data"), Mapping) else {}
        policy = node.get("execution_policy") if isinstance(node.get("execution_policy"), Mapping) else {}
        mode = str(meta.get("inventory_mode") or "task")
        try:
            normalized = normalize_inventory([dict(item) for item in declared])
            deferred = inventory_deferred(normalized, policy=policy, mode=mode)
        except ValueError:
            normalized, deferred = [dict(item) for item in declared], True
        for raw, item in zip(declared, normalized):
            if not _listable(raw):
                continue
            kind = str(item["kind"])
            key = str(item["key"])
            if kind == "material":
                quantity, unit = _MATERIAL_QUANTITY
                dynamic = False
            else:
                number = _finite_number(item.get("quantity"))
                raw_unit = item.get("unit")
                dynamic = number is None or not isinstance(raw_unit, str) or not raw_unit.strip()
                quantity = number if number is not None else 0.0
                unit = raw_unit.strip() if isinstance(raw_unit, str) and raw_unit.strip() else _DYNAMIC_UNIT
            unilab: dict[str, Any] = {
                "inventory": deepcopy(dict(raw)),
                "inventory_mode": mode,
                "deferred": deferred,
                "quantity_dynamic": dynamic,
            }
            source = raw.get("material_uuid")
            if isinstance(source, Mapping) and source.get("kind") == "node_output":
                source_node = str(source.get("workflow_node_uuid") or "")
                if source_node:
                    unilab["material_source_node_uuid"] = source_node
            projected.append(
                {
                    "uuid": str(uuid5(NAMESPACE_URL, f"unilabos://workflow/{workflow_uuid}/{node_uuid}/{key}")),
                    "workflow_uuid": workflow_uuid,
                    "consume_node_uuid": node_uuid,
                    "requirement_key": key if key_counts[key] == 1 else f"{node_uuid}/{key}",
                    "target_type": _V1_TARGET_TYPES[kind],
                    "required_quantity": quantity,
                    "quantity_unit": unit,
                    "allow_split": kind == "lot",
                    "meta_data": {"unilab": unilab},
                }
            )
    return projected


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def is_derived_inventory_requirements(graph: Mapping[str, Any], requirements: Any) -> bool:
    """图根 ``inventory_requirements`` 是不是读图时从节点派生的列表（导出后原样导入的回声）。

    派生列表的每一条都是某个节点上声明的需求原条目（``consume_node_uuid`` + ``meta_data.unilab.inventory``），
    只按这一对比较：``requirement_key``、数量、``deferred`` 等投影字段随投影规则（可见性、禁用、重名、
    规范化）变化，导入时重新派生会和导出时对不上。对不上任何节点条目（手写、改了需求本身）就不是回声。
    投影字段只读，改了也不生效。
    """

    if not isinstance(requirements, list):
        return False
    declared = {
        (str(node.get("uuid") or ""), _canonical(item))
        for node in graph.get("nodes") or []
        if isinstance(node, Mapping)
        for item in _declared_inventory(node)
    }
    for item in requirements:
        if not isinstance(item, Mapping):
            return False
        meta = item.get("meta_data")
        unilab = meta.get("unilab") if isinstance(meta, Mapping) else None
        inventory = unilab.get("inventory") if isinstance(unilab, Mapping) else None
        if not isinstance(inventory, Mapping):
            return False
        if (str(item.get("consume_node_uuid") or ""), _canonical(inventory)) not in declared:
            return False
    return True


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
    "is_derived_inventory_requirements",
    "node_or_ancestor_disabled",
    "patch_node",
    "private_node_owners",
    "project_inventory_requirements",
    "remap_node_references",
    "restore_private_subgraphs",
]
