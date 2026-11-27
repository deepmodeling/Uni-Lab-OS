"""组合工作流两条展开路径共享的纯图重写规则。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any
from uuid import UUID, uuid5

_CONTROL_NODE_TYPES = frozenset({"condition", "repeat_until"})
_NODE_REFERENCE_KEYS = frozenset(
    {
        "node_uuid",
        "workflow_node_uuid",
        "control_region_uuid",
        "node_uuids",
        "entry_node_uuids",
        "exit_node_uuids",
        "predecessor_node_uuids",
        "successor_node_uuids",
    }
)


class CompositeGraphRewriteError(RuntimeError):
    """组合图重写无法安全完成时返回的中立结构化异常。"""

    def __init__(self, code: str, path: str, message: str | None = None) -> None:
        """保存稳定错误码、JSON Pointer 路径与可选说明。"""

        self.code = code
        self.path = path
        self.message = message
        super().__init__(code)


def remap_control_references(
    value: Any,
    node_uuid_map: Mapping[str, str],
    *,
    key: str | None = None,
) -> Any:
    """按字段语义重映射控制节点内部的节点和区域身份。"""

    if isinstance(value, list):
        return [
            remap_control_references(item, node_uuid_map, key=key) for item in value
        ]
    if isinstance(value, Mapping):
        return {
            str(name): remap_control_references(item, node_uuid_map, key=str(name))
            for name, item in value.items()
        }
    if key in _NODE_REFERENCE_KEYS and isinstance(value, str):
        return node_uuid_map.get(value, value)
    return deepcopy(value)


def control_workflow_input_parameters(
    nodes: Sequence[Mapping[str, Any]],
) -> set[str]:
    """返回仅由结构化控制区域引用的工作流输入名称。"""

    result: set[str] = set()

    def visit(value: Any) -> None:
        """递归收集一个控制参数值引用的工作流输入。"""

        if isinstance(value, Mapping):
            if value.get("kind") == "workflow_input" and isinstance(
                value.get("parameter"), str
            ):
                result.add(str(value["parameter"]))
            for child in value.values():
                visit(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                visit(child)

    for node in nodes:
        node_type = str(node.get("type") or "")
        node_node_type = str(node.get("node_type") or "")
        if (
            node_type not in _CONTROL_NODE_TYPES
            and node_node_type not in _CONTROL_NODE_TYPES
        ):
            continue
        visit(node.get("param"))
    return result


def materialize_control_arguments(
    nodes: Sequence[dict[str, Any]],
    *,
    keyword_arguments: Mapping[str, object],
    source_handles: Sequence[Mapping[str, Any]] = (),
) -> None:
    """把组合实参固化到条件和重复控制区域。"""

    providers: set[str] = set()

    def control_argument(argument: Mapping[str, Any]) -> dict[str, Any]:
        matches = [
            h
            for h in source_handles
            if h.get("uuid") == argument.get("source_handle_uuid")
            and h.get("io_type") == "source"
        ]
        if (
            len(matches) != 1
            or not matches[0].get("data_key")
            or not argument.get("workflow_node_uuid")
        ):
            raise CompositeGraphRewriteError(
                "composite_boundary_mapping_invalid",
                "/keyword_arguments",
                "控制参数缺少唯一的上游结果连接点",
            )
        providers.add(str(argument["workflow_node_uuid"]))
        return {
            "kind": "node_result",
            "node_uuid": argument["workflow_node_uuid"],
            "result_path": [matches[0]["data_key"]],
        }

    for node in nodes:
        providers.clear()
        node_type = str(node.get("type") or "")
        node_node_type = str(node.get("node_type") or "")
        if (
            node_type not in _CONTROL_NODE_TYPES
            and node_node_type not in _CONTROL_NODE_TYPES
        ):
            continue
        params = node.get("param")
        if not isinstance(params, dict):
            continue
        bindings = params.get("bindings")
        literal_replacements: dict[str, Any] = {}
        if isinstance(bindings, dict):
            for variable, binding in list(bindings.items()):
                if not isinstance(binding, Mapping):
                    continue
                if (
                    binding.get("kind") != "workflow_input"
                    or not isinstance(binding.get("parameter"), str)
                    or binding["parameter"] not in keyword_arguments
                ):
                    continue
                argument = keyword_arguments[str(binding["parameter"])]
                if isinstance(argument, Mapping):
                    kind = argument.get("kind")
                    if kind == "workflow_input" and isinstance(
                        argument.get("parameter"), str
                    ):
                        bindings[str(variable)] = {
                            "kind": "workflow_input",
                            "parameter": str(argument["parameter"]),
                        }
                        continue
                    if kind == "node_output":
                        bindings[str(variable)] = control_argument(argument)
                        continue
                literal_replacements[str(variable)] = _plain_json(argument)
                bindings.pop(variable, None)

        for key in ("branches", "until"):
            if key in params and literal_replacements:
                params[key] = _replace_control_expression_variable(
                    params[key],
                    literal_replacements,
                )
        for key in ("initial_carry", "next_carry"):
            if key in params:
                params[key] = _materialize_control_binding_value(
                    params[key],
                    keyword_arguments,
                    control_argument,
                )
        if providers:
            params["predecessor_node_uuids"] = list(
                dict.fromkeys(
                    [*params.get("predecessor_node_uuids", []), *sorted(providers)]
                )
            )


def _replace_control_expression_variable(
    value: Any,
    replacements: Mapping[str, Any],
) -> Any:
    """把控制表达式中的变量替换为调用方提供的字面量。"""

    if isinstance(value, Mapping):
        variable = value.get("var")
        if (
            set(value) == {"var"}
            and isinstance(variable, str)
            and variable in replacements
        ):
            return {"lit": _plain_json(replacements[variable])}
        return {
            str(key): _replace_control_expression_variable(child, replacements)
            for key, child in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [
            _replace_control_expression_variable(child, replacements) for child in value
        ]
    return deepcopy(value)


def _materialize_control_binding_value(
    value: Any,
    keyword_arguments: Mapping[str, object],
    control_argument,
) -> Any:
    """递归固化控制区域携带值中的工作流输入来源。"""

    if isinstance(value, Mapping):
        if (
            value.get("kind") == "workflow_input"
            and isinstance(value.get("parameter"), str)
            and value["parameter"] in keyword_arguments
        ):
            argument = keyword_arguments[str(value["parameter"])]
            if isinstance(argument, Mapping):
                kind = argument.get("kind")
                if kind == "workflow_input" and isinstance(
                    argument.get("parameter"), str
                ):
                    return {
                        "kind": "workflow_input",
                        "parameter": str(argument["parameter"]),
                    }
                if kind == "node_output":
                    return control_argument(argument)
            return {"kind": "literal", "value": _plain_json(argument)}
        return {
            str(key): _materialize_control_binding_value(
                child, keyword_arguments, control_argument
            )
            for key, child in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [
            _materialize_control_binding_value(
                child, keyword_arguments, control_argument
            )
            for child in value
        ]
    return deepcopy(value)


def _plain_json(value: Any) -> Any:
    """递归复制冻结 JSON 容器为普通字典和列表。"""

    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(item) for item in value]
    return value


def project_experiment_operation_resource_scopes(
    workflow: Mapping[str, Any],
    *,
    invocation_uuid: str,
    node_uuid_map: Mapping[str, str],
) -> tuple[dict[str, Any], ...]:
    """把实验操作的词法资源作用域投影到本次调用身份空间。"""

    meta_data = workflow.get("meta_data")
    if meta_data is not None and not isinstance(meta_data, Mapping):
        raise CompositeGraphRewriteError(
            "composite_boundary_mapping_invalid",
            "/child/workflow/meta_data",
            "实验操作元数据必须是对象",
        )
    unilab = meta_data.get("unilab") if isinstance(meta_data, Mapping) else None
    if unilab is not None and not isinstance(unilab, Mapping):
        raise CompositeGraphRewriteError(
            "composite_boundary_mapping_invalid",
            "/child/workflow/meta_data/unilab",
            "实验操作 unilab 元数据必须是对象",
        )
    raw_scopes = unilab.get("resource_scopes") if isinstance(unilab, Mapping) else None
    if raw_scopes is None:
        return ()
    if not isinstance(raw_scopes, Sequence) or isinstance(raw_scopes, (str, bytes)):
        raise CompositeGraphRewriteError(
            "composite_boundary_mapping_invalid",
            "/child/workflow/meta_data/unilab/resource_scopes",
            "实验操作资源作用域必须是数组",
        )
    try:
        namespace = UUID(invocation_uuid)
    except (AttributeError, TypeError, ValueError):
        raise CompositeGraphRewriteError(
            "composite_boundary_mapping_invalid",
            "/invocation_uuid",
        ) from None

    scope_ids: list[str] = []
    raw_by_id: dict[str, Mapping[str, Any]] = {}
    for index, raw_scope in enumerate(raw_scopes):
        path = f"/child/workflow/meta_data/unilab/resource_scopes/{index}"
        if not isinstance(raw_scope, Mapping):
            raise CompositeGraphRewriteError(
                "composite_boundary_mapping_invalid",
                path,
                "实验操作资源作用域必须是对象",
            )
        scope_id = str(raw_scope.get("scope_id") or "").strip()
        if not scope_id or scope_id in raw_by_id:
            raise CompositeGraphRewriteError(
                "composite_boundary_mapping_invalid",
                f"{path}/scope_id",
                "实验操作资源作用域身份缺失或重复",
            )
        scope_ids.append(scope_id)
        raw_by_id[scope_id] = raw_scope

    projected_ids = {
        scope_id: f"resource-scope-{uuid5(namespace, f'unilabos:composite-resource-scope:v1:{scope_id}')}"
        for scope_id in scope_ids
    }
    result: list[dict[str, Any]] = []
    for index, scope_id in enumerate(scope_ids):
        raw_scope = raw_by_id[scope_id]
        path = f"/child/workflow/meta_data/unilab/resource_scopes/{index}"
        raw_members = raw_scope.get("node_uuids")
        if not isinstance(raw_members, Sequence) or isinstance(
            raw_members,
            (str, bytes),
        ):
            raise CompositeGraphRewriteError(
                "composite_boundary_mapping_invalid",
                f"{path}/node_uuids",
                "实验操作资源作用域节点必须是数组",
            )
        members = _mapped_node_uuids(
            raw_members,
            node_uuid_map,
            path=f"{path}/node_uuids",
        )
        entry = "" if raw_scope.get("kind") == "reservation" else _mapped_node_uuid(
            raw_scope.get("entry_node_uuid"),
            node_uuid_map,
            path=f"{path}/entry_node_uuid",
        )
        exit_node = "" if raw_scope.get("kind") == "reservation" else _mapped_node_uuid(
            raw_scope.get("exit_node_uuid"),
            node_uuid_map,
            path=f"{path}/exit_node_uuid",
        )
        raw_parent = raw_scope.get("parent_scope_id")
        parent_scope_id: str | None = None
        if raw_parent is not None:
            parent_scope_id = projected_ids.get(str(raw_parent))
            if parent_scope_id is None:
                raise CompositeGraphRewriteError(
                    "composite_boundary_mapping_invalid",
                    f"{path}/parent_scope_id",
                    "实验操作资源作用域引用了未知父作用域",
                )
        projected = deepcopy(dict(raw_scope))
        projected.update(
            {
                "scope_id": projected_ids[scope_id],
                "parent_scope_id": parent_scope_id,
                "entry_node_uuid": entry,
                "exit_node_uuid": exit_node,
                "node_uuids": members,
                "composite_invocation_uuid": invocation_uuid,
                "source_scope_id": scope_id,
                "source_workflow_uuid": str(workflow.get("uuid") or ""),
            }
        )
        result.append(projected)
    return tuple(result)


def merge_expanded_resource_scopes(
    direct_scopes: Sequence[Mapping[str, Any]],
    *,
    nested_invocations: Sequence[
        tuple[str, Sequence[str], Sequence[Mapping[str, Any]]]
    ],
) -> tuple[dict[str, Any], ...]:
    """合并直接与嵌套作用域，并扩大覆盖组合调用的父作用域成员。"""

    scopes = [deepcopy(dict(scope)) for scope in direct_scopes]
    for invocation_uuid, expanded_members, _nested_scopes in nested_invocations:
        members = [str(item) for item in expanded_members]
        for scope in scopes:
            raw_scope_members = scope.get("node_uuids")
            if not isinstance(raw_scope_members, Sequence) or isinstance(
                raw_scope_members,
                (str, bytes),
            ):
                continue
            expanded: list[str] = []
            for node_uuid in raw_scope_members:
                replacements = (
                    members if str(node_uuid) == invocation_uuid else [str(node_uuid)]
                )
                for replacement in replacements:
                    if replacement not in expanded:
                        expanded.append(replacement)
            scope["node_uuids"] = expanded

    result = list(scopes)
    for invocation_uuid, _expanded_members, nested_scopes in nested_invocations:
        parent_scope_id = _deepest_covering_scope_id(direct_scopes, invocation_uuid)
        for raw_scope in nested_scopes:
            scope = deepcopy(dict(raw_scope))
            if scope.get("parent_scope_id") is None and parent_scope_id is not None:
                scope["parent_scope_id"] = parent_scope_id
            result.append(scope)
    scope_ids = [str(scope.get("scope_id") or "") for scope in result]
    if any(not scope_id for scope_id in scope_ids) or len(scope_ids) != len(
        set(scope_ids)
    ):
        raise CompositeGraphRewriteError(
            "composite_boundary_mapping_invalid",
            "/child/workflow/meta_data/unilab/resource_scopes",
            "展开后的实验操作资源作用域身份缺失或重复",
        )
    return tuple(result)


def _deepest_covering_scope_id(
    scopes: Sequence[Mapping[str, Any]],
    invocation_uuid: str,
) -> str | None:
    """返回覆盖组合调用节点的最深直接作用域身份。"""

    by_id = {str(scope.get("scope_id")): scope for scope in scopes}

    def depth(scope: Mapping[str, Any]) -> int:
        """计算作用域在当前直接作用域集合中的父链深度。"""

        result = 0
        current = scope
        visited: set[str] = set()
        while current.get("parent_scope_id") is not None:
            parent_id = str(current["parent_scope_id"])
            if parent_id in visited or parent_id not in by_id:
                break
            visited.add(parent_id)
            result += 1
            current = by_id[parent_id]
        return result

    candidates = [
        scope
        for scope in scopes
        if invocation_uuid in {str(item) for item in scope.get("node_uuids", ())}
    ]
    if not candidates:
        return None
    return str(
        max(
            candidates,
            key=lambda scope: (depth(scope), str(scope.get("scope_id") or "")),
        )["scope_id"]
    )


def _mapped_node_uuids(
    raw_node_uuids: Sequence[object],
    node_uuid_map: Mapping[str, str],
    *,
    path: str,
) -> list[str]:
    """重映射一个作用域的完整成员集合。"""

    result: list[str] = []
    for raw_uuid in raw_node_uuids:
        mapped = _mapped_node_uuid(raw_uuid, node_uuid_map, path=path)
        if mapped not in result:
            result.append(mapped)
    if not result:
        raise CompositeGraphRewriteError(
            "composite_boundary_mapping_invalid",
            path,
            "实验操作资源作用域不能为空",
        )
    return result


def _mapped_node_uuid(
    raw_uuid: object,
    node_uuid_map: Mapping[str, str],
    *,
    path: str,
) -> str:
    """重映射一个必须属于子图的节点身份。"""

    if not isinstance(raw_uuid, str) or raw_uuid not in node_uuid_map:
        raise CompositeGraphRewriteError(
            "composite_boundary_mapping_invalid",
            path,
            "实验操作资源作用域引用了未知节点",
        )
    return str(node_uuid_map[raw_uuid])


__all__ = [
    "CompositeGraphRewriteError",
    "control_workflow_input_parameters",
    "materialize_control_arguments",
    "merge_expanded_resource_scopes",
    "project_experiment_operation_resource_scopes",
    "remap_control_references",
]
