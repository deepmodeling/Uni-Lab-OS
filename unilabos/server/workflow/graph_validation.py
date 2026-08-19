"""冻结 Backend 全图 PUT 的本地语义校验。"""

from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Mapping

from unilabos.server.workflow.json_codec import encode_json, strict_json_equal
from unilabos.server.workflow.models import WorkflowEdgeWrite, WorkflowNodeWrite

_MAX_SCHEMA_DEPTH = 64
_MAX_TIMEOUT_SECONDS = (2**63 - 1) // 1_000_000_000


class GraphValidationError(ValueError):
    """提交的全图不满足冻结 Backend 语义。"""


class MissingTemplateError(GraphValidationError):
    """节点引用的模板不在当前 OS 模板目录中。"""


def validate_graph(
    *,
    nodes: List[WorkflowNodeWrite],
    edges: List[WorkflowEdgeWrite],
    templates: Mapping[str, Dict[str, Any]],
    handles: Mapping[str, Dict[str, Any]],
    effective_params: Mapping[str, Dict[str, Any]],
    workflow_meta_data: Mapping[str, Any],
    node_meta_data: Mapping[str, Dict[str, Any]],
) -> None:
    """在写事务内校验一份完整替换图。"""

    node_by_uuid = {node.uuid: node for node in nodes}
    edge_by_uuid = {edge.uuid: edge for edge in edges}
    if len(node_by_uuid) != len(nodes):
        raise GraphValidationError("工作流节点 UUID 重复")
    if len(edge_by_uuid) != len(edges):
        raise GraphValidationError("工作流边 UUID 重复")

    for node in nodes:
        template_uuid = node.workflow_node_template_uuid
        if template_uuid is not None and template_uuid not in templates:
            raise MissingTemplateError(f"工作流节点模板 {template_uuid} 不存在")
        if node.parent_uuid is not None and node.parent_uuid not in node_by_uuid:
            raise GraphValidationError("父节点不在提交的完整图中")
    _validate_parent_cycles(nodes)

    for edge in edges:
        if edge.source_node_uuid == edge.target_node_uuid:
            raise GraphValidationError("节点不能连接到自身")
        if (
            edge.source_node_uuid not in node_by_uuid
            or edge.target_node_uuid not in node_by_uuid
        ):
            raise GraphValidationError("边引用了提交图以外的节点")
        _validate_edge_handle(
            node_by_uuid[edge.source_node_uuid],
            edge.source_handle_uuid,
            "source",
            handles,
        )
        _validate_edge_handle(
            node_by_uuid[edge.target_node_uuid],
            edge.target_handle_uuid,
            "target",
            handles,
        )

    bindings_by_node = {
        node.uuid: _validated_input_bindings(
            node,
            node_meta_data[node.uuid],
            workflow_meta_data,
            handles,
        )
        for node in nodes
    }
    enabled = {
        node.uuid: node
        for node in nodes
        if not node.disabled and _node_kind(node, templates) != "group"
    }
    enabled_edges: List[WorkflowEdgeWrite] = []
    incoming: Dict[tuple[str, str], str] = {}
    connected_inputs: Dict[tuple[str, str], str] = {}
    available_data_keys: Dict[str, List[str]] = defaultdict(list)
    for edge in edges:
        if edge.source_node_uuid not in enabled or edge.target_node_uuid not in enabled:
            continue
        source_handle = handles[edge.source_handle_uuid]
        target_handle = handles[edge.target_handle_uuid]
        if not _handle_types_compatible(
            source_handle.get("type"),
            target_handle.get("type"),
        ):
            raise GraphValidationError("边两端 Handle 类型不兼容")
        target_input = (edge.target_node_uuid, edge.target_handle_uuid)
        if target_input in incoming:
            raise GraphValidationError("同一目标 Handle 只能有一条入边")
        incoming[target_input] = edge.uuid
        enabled_edges.append(edge)
        if not _dependency_only(source_handle):
            connected_inputs[target_input] = edge.uuid
            available_data_keys[edge.target_node_uuid].append(
                _handle_data_key(target_handle)
            )

    _validate_edge_cycles(enabled, enabled_edges)
    for node_uuid, node in enabled.items():
        param = effective_params[node_uuid]
        bindings = bindings_by_node[node_uuid]
        for handle_uuid in bindings:
            available_data_keys[node_uuid].append(
                _handle_data_key(handles[handle_uuid])
            )
        template_uuid = node.workflow_node_template_uuid
        if template_uuid is not None:
            schema = _parse_schema(templates[template_uuid].get("schema"))
            if schema is not None:
                _validate_schema_value(
                    schema,
                    param,
                    root=schema,
                    path="$",
                    ignore_required=True,
                    depth=0,
                )
                _validate_required_properties(
                    schema,
                    param,
                    root=schema,
                    path="",
                    available={
                        _final_target_data_key(key)
                        for key in available_data_keys[node_uuid]
                        if _final_target_data_key(key)
                    },
                    depth=0,
                )
        _validate_required_handles(
            node,
            param,
            handles.values(),
            connected_inputs,
            bindings,
        )
        _validate_execution_policy(node.execution_policy)
        if _node_kind(node, templates) == "device_action":
            if node.material_uuid is None:
                raise GraphValidationError("设备动作节点必须绑定 material_uuid")


def _validate_parent_cycles(nodes: Iterable[WorkflowNodeWrite]) -> None:
    parents = {
        node.uuid: node.parent_uuid for node in nodes if node.parent_uuid is not None
    }
    for start in parents:
        visited: set[str] = set()
        current: str | None = start
        while current is not None:
            if current in visited:
                raise GraphValidationError("父子关系形成循环")
            visited.add(current)
            current = parents.get(current)


def _validate_edge_handle(
    node: WorkflowNodeWrite,
    handle_uuid: str,
    io_type: str,
    handles: Mapping[str, Dict[str, Any]],
) -> None:
    template_uuid = node.workflow_node_template_uuid
    if template_uuid is None:
        raise GraphValidationError("有连线的节点必须引用节点模板")
    handle = handles.get(handle_uuid)
    if (
        handle is None
        or handle.get("workflow_node_template_uuid") != template_uuid
        or handle.get("io_type") != io_type
    ):
        raise GraphValidationError("Handle 不属于节点模板或方向错误")


def _node_kind(
    node: WorkflowNodeWrite,
    templates: Mapping[str, Dict[str, Any]],
) -> str:
    raw_kind = node.type
    if node.workflow_node_template_uuid is not None:
        raw_kind = templates[node.workflow_node_template_uuid].get(
            "node_type",
            "",
        )
    aliases = {
        "device": "device_action",
        "device_action": "device_action",
        "resource_action": "device_action",
        "ilab": "device_action",
        "compute": "compute",
        "condition": "condition",
        "script": "script",
        "py_script": "script",
        "group": "group",
        "tool_call": "tool_call",
        "manual_confirm": "manual_confirm",
    }
    kind = aliases.get(str(raw_kind).strip().lower())
    if kind is None:
        raise GraphValidationError(f"不支持的节点执行类型 {raw_kind!r}")
    return kind


def _validate_edge_cycles(
    enabled: Mapping[str, WorkflowNodeWrite],
    edges: Iterable[WorkflowEdgeWrite],
) -> None:
    indegree = {node_uuid: 0 for node_uuid in enabled}
    outgoing: Dict[str, List[str]] = defaultdict(list)
    for edge in edges:
        indegree[edge.target_node_uuid] += 1
        outgoing[edge.source_node_uuid].append(edge.target_node_uuid)
    ready = [node_uuid for node_uuid, degree in indegree.items() if degree == 0]
    visited = 0
    while ready:
        current = ready.pop()
        visited += 1
        for target in outgoing[current]:
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
    if visited != len(enabled):
        raise GraphValidationError("工作流图形成循环")


def _handle_types_compatible(source: Any, target: Any) -> bool:
    source_type = str(source or "").strip().lower()
    target_type = str(target or "").strip().lower()
    return (
        source_type == target_type
        or source_type in {"", "any"}
        or target_type in {"", "any"}
    )


def _handle_data_key(handle: Mapping[str, Any]) -> str:
    return str(handle.get("data_key") or handle.get("handle_key") or "").strip()


def _final_target_data_key(data_key: str) -> str:
    return data_key.split("@@@")[-1].strip()


def _dependency_only(handle: Mapping[str, Any]) -> bool:
    if str(handle.get("handle_key") or "").strip().lower() == "ready":
        return True
    data_source = str(handle.get("data_source") or "").strip()
    return bool(data_source) and data_source.lower() != "executor"


def _validate_required_handles(
    node: WorkflowNodeWrite,
    param: Mapping[str, Any],
    handles: Iterable[Dict[str, Any]],
    incoming: Mapping[tuple[str, str], str],
    bindings: Mapping[str, Dict[str, Any]],
) -> None:
    template_uuid = node.workflow_node_template_uuid
    if template_uuid is None:
        return
    for handle in handles:
        if (
            handle.get("workflow_node_template_uuid") != template_uuid
            or handle.get("io_type") != "target"
        ):
            continue
        data_key = _final_target_data_key(_handle_data_key(handle))
        has_default = data_key in param and param[data_key] is not None
        has_edge = (node.uuid, str(handle["uuid"])) in incoming
        has_binding = str(handle["uuid"]) in bindings
        provider_count = sum((has_default, has_edge, has_binding))
        if provider_count > 1:
            raise GraphValidationError(f"输入 {data_key!r} 存在多个 Provider")
        if handle.get("required") and provider_count != 1:
            raise GraphValidationError(f"缺少必填输入 {data_key!r}")
        if has_default and not _declared_type_matches(
            param[data_key],
            handle.get("type"),
        ):
            raise GraphValidationError(f"输入 {data_key!r} 的类型不正确")


def _validated_input_bindings(
    node: WorkflowNodeWrite,
    meta_data: Mapping[str, Any],
    workflow_meta_data: Mapping[str, Any],
    handles: Mapping[str, Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    unilab = meta_data.get("unilab", {})
    if not isinstance(unilab, dict):
        raise GraphValidationError("Node meta_data.unilab 必须是对象")
    raw_bindings = unilab.get("input_bindings", {})
    if not isinstance(raw_bindings, dict):
        raise GraphValidationError("input_bindings 必须是对象")
    if not raw_bindings:
        return {}
    if node.workflow_node_template_uuid is None:
        raise GraphValidationError("无模板节点不能声明 input_bindings")

    workflow_unilab = workflow_meta_data.get("unilab", {})
    if not isinstance(workflow_unilab, dict):
        raise GraphValidationError("Workflow meta_data.unilab 必须是对象")
    input_contract = workflow_unilab.get("input_contract", {})
    if not isinstance(input_contract, dict):
        raise GraphValidationError("input_contract 必须是对象")
    parameters = input_contract.get("parameters", [])
    if not isinstance(parameters, list):
        raise GraphValidationError("input_contract.parameters 必须是数组")
    parameter_names = [
        item.get("name")
        for item in parameters
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    ]

    result: Dict[str, Dict[str, Any]] = {}
    for handle_uuid, raw_binding in raw_bindings.items():
        handle = handles.get(handle_uuid)
        if (
            handle is None
            or handle.get("workflow_node_template_uuid")
            != node.workflow_node_template_uuid
            or handle.get("io_type") != "target"
        ):
            raise GraphValidationError("input_binding 未引用本节点的目标 Handle")
        if not isinstance(raw_binding, dict):
            raise GraphValidationError("input_binding 必须是对象")
        parameter = raw_binding.get("parameter")
        if not isinstance(parameter, str) or not parameter:
            raise GraphValidationError("input_binding.parameter 无效")
        if parameter_names.count(parameter) != 1:
            raise GraphValidationError("input_binding 必须唯一引用 Workflow 参数")
        source = raw_binding.get("source")
        if source is not None and source != "workflow_input":
            raise GraphValidationError("input_binding.source 无效")
        result[handle_uuid] = dict(raw_binding)
    return result


def _validate_execution_policy(policy: Mapping[str, Any]) -> None:
    if "execution_timeout_seconds" not in policy:
        return
    value = policy["execution_timeout_seconds"]
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > _MAX_TIMEOUT_SECONDS
    ):
        raise GraphValidationError("execution_timeout_seconds 必须是非负整数")


def _parse_schema(raw_schema: Any) -> Any:
    if raw_schema is None or str(raw_schema).strip() == "":
        return None
    try:
        schema = json.loads(str(raw_schema))
    except (TypeError, ValueError) as exc:
        raise GraphValidationError("节点参数 JSON Schema 无效") from exc
    if not isinstance(schema, (dict, bool)):
        raise GraphValidationError("节点参数 JSON Schema 必须是对象或布尔值")
    return schema


def _resolve_ref(root: Any, reference: str) -> Any:
    if reference == "#":
        return root
    if not reference.startswith("#/"):
        raise GraphValidationError("仅支持本地 JSON Schema 引用")
    current = root
    for raw_part in reference[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, dict) or part not in current:
            raise GraphValidationError("JSON Schema 引用不存在")
        current = current[part]
    return current


def _validate_schema_value(
    schema: Any,
    value: Any,
    *,
    root: Any,
    path: str,
    ignore_required: bool,
    depth: int,
) -> None:
    if depth > _MAX_SCHEMA_DEPTH:
        raise GraphValidationError("JSON Schema 校验深度超限")
    if schema is True:
        return
    if schema is False or not isinstance(schema, dict):
        raise GraphValidationError(f"{path} 不满足 JSON Schema")
    if "$ref" in schema:
        _validate_schema_value(
            _resolve_ref(root, schema["$ref"]),
            value,
            root=root,
            path=path,
            ignore_required=ignore_required,
            depth=depth + 1,
        )
    for child in schema.get("allOf", []):
        _validate_schema_value(
            child,
            value,
            root=root,
            path=path,
            ignore_required=ignore_required,
            depth=depth + 1,
        )
    for keyword in ("anyOf", "oneOf"):
        if keyword not in schema:
            continue
        matches = 0
        for child in schema[keyword]:
            try:
                _validate_schema_value(
                    child,
                    value,
                    root=root,
                    path=path,
                    ignore_required=ignore_required,
                    depth=depth + 1,
                )
            except GraphValidationError:
                continue
            matches += 1
        if matches == 0 or (keyword == "oneOf" and matches != 1):
            raise GraphValidationError(f"{path} 不满足 {keyword}")
    if "not" in schema:
        try:
            _validate_schema_value(
                schema["not"],
                value,
                root=root,
                path=path,
                ignore_required=ignore_required,
                depth=depth + 1,
            )
        except GraphValidationError:
            pass
        else:
            raise GraphValidationError(f"{path} 命中禁止的 JSON Schema")

    declared_types = schema.get("type")
    if declared_types is not None:
        if not isinstance(declared_types, list):
            declared_types = [declared_types]
        if not any(_json_type_matches(value, item) for item in declared_types):
            raise GraphValidationError(f"{path} 的类型不满足 JSON Schema")
    if "const" in schema and not _json_equal(value, schema["const"]):
        raise GraphValidationError(f"{path} 不等于 JSON Schema const")
    if "enum" in schema and not any(
        _json_equal(value, item) for item in schema["enum"]
    ):
        raise GraphValidationError(f"{path} 不在 JSON Schema enum 中")

    if isinstance(value, dict):
        if len(value) < schema.get("minProperties", 0):
            raise GraphValidationError(f"{path} 少于 minProperties")
        if "maxProperties" in schema and len(value) > schema["maxProperties"]:
            raise GraphValidationError(f"{path} 多于 maxProperties")
        if not ignore_required:
            for name in schema.get("required", []):
                if name not in value:
                    raise GraphValidationError(f"{path} 缺少必填属性 {name!r}")
        properties = schema.get("properties", {})
        for name, child_value in value.items():
            if name in properties:
                _validate_schema_value(
                    properties[name],
                    child_value,
                    root=root,
                    path=f"{path}.{name}",
                    ignore_required=ignore_required,
                    depth=depth + 1,
                )
            elif schema.get("additionalProperties") is False:
                raise GraphValidationError(f"{path} 含未声明属性 {name!r}")
            elif isinstance(schema.get("additionalProperties"), dict):
                _validate_schema_value(
                    schema["additionalProperties"],
                    child_value,
                    root=root,
                    path=f"{path}.{name}",
                    ignore_required=ignore_required,
                    depth=depth + 1,
                )
    if isinstance(value, list) and "items" in schema:
        for index, child_value in enumerate(value):
            _validate_schema_value(
                schema["items"],
                child_value,
                root=root,
                path=f"{path}[{index}]",
                ignore_required=ignore_required,
                depth=depth + 1,
            )
    _validate_scalar_constraints(schema, value, path)


def _validate_scalar_constraints(
    schema: Mapping[str, Any],
    value: Any,
    path: str,
) -> None:
    if isinstance(value, str):
        length = len(value)
        if length < schema.get("minLength", 0):
            raise GraphValidationError(f"{path} 短于 minLength")
        if "maxLength" in schema and length > schema["maxLength"]:
            raise GraphValidationError(f"{path} 长于 maxLength")
        if "pattern" in schema:
            try:
                matched = re.search(schema["pattern"], value)
            except re.error as exc:
                raise GraphValidationError("JSON Schema pattern 无效") from exc
            if matched is None:
                raise GraphValidationError(f"{path} 不匹配 pattern")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            raise GraphValidationError(f"{path} 少于 minItems")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise GraphValidationError(f"{path} 多于 maxItems")
        if schema.get("uniqueItems") and len(
            {_canonical_json(item) for item in value}
        ) != len(value):
            raise GraphValidationError(f"{path} 含重复数组项")
    if _is_number(value):
        if "minimum" in schema and value < schema["minimum"]:
            raise GraphValidationError(f"{path} 小于 minimum")
        if "maximum" in schema and value > schema["maximum"]:
            raise GraphValidationError(f"{path} 大于 maximum")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            raise GraphValidationError(f"{path} 不大于 exclusiveMinimum")
        if "exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"]:
            raise GraphValidationError(f"{path} 不小于 exclusiveMaximum")
        if "multipleOf" in schema:
            quotient = value / schema["multipleOf"]
            if not math.isclose(quotient, round(quotient)):
                raise GraphValidationError(f"{path} 不是 multipleOf 的倍数")


def _validate_required_properties(
    schema: Any,
    value: Any,
    *,
    root: Any,
    path: str,
    available: set[str],
    depth: int,
) -> None:
    if depth > _MAX_SCHEMA_DEPTH or isinstance(schema, bool):
        return
    if not isinstance(schema, dict):
        return
    if "$ref" in schema:
        _validate_required_properties(
            _resolve_ref(root, schema["$ref"]),
            value,
            root=root,
            path=path,
            available=available,
            depth=depth + 1,
        )
    for child in schema.get("allOf", []):
        _validate_required_properties(
            child,
            value,
            root=root,
            path=path,
            available=available,
            depth=depth + 1,
        )
    for keyword in ("anyOf", "oneOf"):
        if keyword not in schema:
            continue
        failures = 0
        for child in schema[keyword]:
            try:
                _validate_required_properties(
                    child,
                    value,
                    root=root,
                    path=path,
                    available=available,
                    depth=depth + 1,
                )
            except GraphValidationError:
                failures += 1
        if failures == len(schema[keyword]):
            raise GraphValidationError(f"{path or '$'} 缺少必填属性")
    object_value = value if isinstance(value, dict) else {}
    for name in schema.get("required", []):
        child_path = f"{path}.{name}" if path else name
        if name not in object_value and child_path not in available:
            raise GraphValidationError(f"缺少 JSON Schema 必填属性 {child_path!r}")
    for name, child_schema in schema.get("properties", {}).items():
        if name not in object_value:
            continue
        child_path = f"{path}.{name}" if path else name
        _validate_required_properties(
            child_schema,
            object_value[name],
            root=root,
            path=child_path,
            available=available,
            depth=depth + 1,
        )


def _json_type_matches(value: Any, declared_type: Any) -> bool:
    expected = str(declared_type).strip().lower()
    if expected == "null":
        return value is None
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return (
            isinstance(value, int)
            and not isinstance(value, bool)
            or isinstance(value, float)
            and math.isfinite(value)
            and value.is_integer()
        )
    if expected == "number":
        return _is_number(value)
    if expected == "string":
        return isinstance(value, str)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    return False


def _declared_type_matches(value: Any, declared_type: Any) -> bool:
    expected = str(declared_type or "").strip().lower()
    if value is None or expected in {"", "any", "default"}:
        return True
    aliases = {
        "float": "number",
        "double": "number",
        "int": "integer",
        "bool": "boolean",
        "list": "array",
        "map": "object",
    }
    normalized = aliases.get(expected, expected)
    if normalized not in {
        "null",
        "boolean",
        "integer",
        "number",
        "string",
        "array",
        "object",
    }:
        return True
    return _json_type_matches(value, normalized)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _canonical_json(value: Any) -> bytes:
    return encode_json(value, sort_keys=True)


def _json_equal(left: Any, right: Any) -> bool:
    return strict_json_equal(left, right)


__all__ = [
    "GraphValidationError",
    "MissingTemplateError",
    "validate_graph",
]
