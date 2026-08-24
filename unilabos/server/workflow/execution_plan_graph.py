"""执行计划中 CompositeWorkflowInvocation 虚拟节点的平面化。"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID, uuid5

from unilabos.server.workflow.store import StoreConflict


class CompositeExecutionPlanError(StoreConflict):
    """冻结组合边界不能安全转换为叶子动作计划。"""


class CompositeExecutionPlanNormalizer:
    """重写组合调用边界，并把静态实参下推到真实动作。"""

    def flatten_composite_edges(
        self,
        *,
        nodes: Mapping[str, Mapping[str, Any]],
        edges: Sequence[Mapping[str, Any]],
        handles: Mapping[str, Mapping[str, Any]],
    ) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
        flattened = [dict(edge) for edge in edges]
        param_overrides: dict[str, dict[str, Any]] = defaultdict(dict)
        invocations = [
            (identity, node)
            for identity, node in nodes.items()
            if isinstance(_composite(node), Mapping)
        ]
        invocations.sort(
            key=lambda item: _node_depth(item[0], nodes),
            reverse=True,
        )
        invocation_uuids = {identity for identity, _node in invocations}
        # 内层先展开时，外层可能在同一轮重新生成一条指向内层虚拟节点的边。
        # 用 invocation 数量约束的固定点轮次继续消除这些边，避免把虚拟节点
        # 端点遗留给 Job planner 后被静默丢弃。
        for _round in range(len(invocations) + 1):
            for invocation_uuid, invocation in invocations:
                flattened = self._flatten_invocation(
                    invocation_uuid=invocation_uuid,
                    invocation=invocation,
                    edges=flattened,
                    nodes=nodes,
                    handles=handles,
                    param_overrides=param_overrides,
                )
            if not any(
                edge.get("source_node_uuid") in invocation_uuids
                or edge.get("target_node_uuid") in invocation_uuids
                for edge in flattened
            ):
                break
        else:
            raise CompositeExecutionPlanError("嵌套组合边界无法收敛")
        return _deduplicate_edges(flattened), {
            node_uuid: values
            for node_uuid, values in param_overrides.items()
            if values
        }

    def _flatten_invocation(
        self,
        *,
        invocation_uuid: str,
        invocation: Mapping[str, Any],
        edges: Sequence[Mapping[str, Any]],
        nodes: Mapping[str, Mapping[str, Any]],
        handles: Mapping[str, Mapping[str, Any]],
        param_overrides: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        composite = _composite(invocation)
        if not isinstance(composite, Mapping):
            raise CompositeExecutionPlanError("组合调用缺少冻结边界")
        target_mappings = _mapping(composite.get("target_mappings"), "target_mappings")
        source_mappings = _mapping(composite.get("source_mappings"), "source_mappings")
        structural = _mapping(
            composite.get("structural_mappings"),
            "structural_mappings",
        )
        incoming = [
            edge for edge in edges if edge.get("target_node_uuid") == invocation_uuid
        ]
        outgoing = [
            edge for edge in edges if edge.get("source_node_uuid") == invocation_uuid
        ]
        retained = [
            dict(edge)
            for edge in edges
            if edge.get("target_node_uuid") != invocation_uuid
            and edge.get("source_node_uuid") != invocation_uuid
        ]
        incoming_by_handle: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        outgoing_by_handle: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for edge in incoming:
            incoming_by_handle[str(edge.get("target_handle_uuid") or "")].append(edge)
        for edge in outgoing:
            outgoing_by_handle[str(edge.get("source_handle_uuid") or "")].append(edge)

        target_boundaries = _business_boundary_handles(
            handles,
            invocation,
            "target",
        )
        source_boundaries = _business_boundary_handles(
            handles,
            invocation,
            "source",
        )
        if set(target_mappings) != target_boundaries:
            raise CompositeExecutionPlanError("组合输入冻结映射集合不完整")
        if set(source_mappings) != source_boundaries:
            raise CompositeExecutionPlanError("组合输出冻结映射集合不完整")

        generated: list[dict[str, Any]] = []
        compatibility = composite.get("contract_compatibility")
        contract_inputs = (
            compatibility.get("inputs")
            if isinstance(compatibility, Mapping)
            else []
        )
        input_names = {
            str(item.get("handle_uuid") or ""): str(item.get("name") or "")
            for item in contract_inputs
            if isinstance(item, Mapping)
        }
        invocation_param = invocation.get("param")
        invocation_param = (
            dict(invocation_param) if isinstance(invocation_param, Mapping) else {}
        )
        # 外层组合的静态实参可能先下推到这个内层 invocation；在下一轮
        # 固定点处理中把它作为内层有效参数继续下推到叶节点。
        invocation_param.update(param_overrides.pop(invocation_uuid, {}))

        for boundary_uuid, raw_targets in target_mappings.items():
            targets = _items(raw_targets, "target_mappings")
            providers = incoming_by_handle.get(boundary_uuid, [])
            for provider in providers:
                for target in targets:
                    generated.append(
                        _rewired_edge(
                            invocation_uuid=invocation_uuid,
                            label="input",
                            source_node_uuid=str(provider.get("source_node_uuid") or ""),
                            source_handle_uuid=str(provider.get("source_handle_uuid") or ""),
                            target_node_uuid=_node_identity(target, nodes),
                            target_handle_uuid=_handle_identity(
                                target,
                                "target_handle_uuid",
                                handles,
                                nodes=nodes,
                                io_type="target",
                            ),
                        )
                    )
            parameter = input_names.get(boundary_uuid, "")
            if providers:
                for target in targets:
                    node_uuid = _node_identity(target, nodes)
                    handle_uuid = _handle_identity(
                        target,
                        "target_handle_uuid",
                        handles,
                        nodes=nodes,
                        io_type="target",
                    )
                    data_key = _handle_data_key(handles[handle_uuid])
                    if data_key:
                        param_overrides[node_uuid].pop(data_key, None)
            elif parameter in invocation_param and not _binding_reference(
                invocation_param[parameter]
            ):
                for target in targets:
                    node_uuid = _node_identity(target, nodes)
                    handle_uuid = _handle_identity(
                        target,
                        "target_handle_uuid",
                        handles,
                        nodes=nodes,
                        io_type="target",
                    )
                    data_key = _handle_data_key(handles[handle_uuid])
                    if not data_key:
                        raise CompositeExecutionPlanError("组合输入缺少 data_key")
                    param_overrides[node_uuid][data_key] = _plain(
                        invocation_param[parameter]
                    )

        ready_target = _boundary_handle(handles, invocation, "target", "ready")
        ready_source = _boundary_handle(handles, invocation, "source", "ready")
        entry_targets = _items(structural.get("entry_targets"), "entry_targets")
        completion_sources = _items(
            structural.get("completion_sources"),
            "completion_sources",
        )
        if bool(entry_targets) != bool(completion_sources):
            raise CompositeExecutionPlanError("组合结构边界不完整")
        for provider in incoming_by_handle.get(ready_target, []):
            for target in entry_targets:
                generated.append(
                    _rewired_edge(
                        invocation_uuid=invocation_uuid,
                        label="entry",
                        source_node_uuid=str(provider.get("source_node_uuid") or ""),
                        source_handle_uuid=str(provider.get("source_handle_uuid") or ""),
                        target_node_uuid=_node_identity(target, nodes),
                        target_handle_uuid=_handle_identity(
                            target,
                            "target_handle_uuid",
                            handles,
                            nodes=nodes,
                            io_type="target",
                            structural=True,
                        ),
                    )
                )

        for boundary_uuid, raw_mapping in source_mappings.items():
            mapping = _mapping(raw_mapping, "source_mappings")
            consumers = outgoing_by_handle.get(boundary_uuid, [])
            if mapping.get("kind") == "node_output":
                source_node = _node_identity(mapping, nodes)
                source_handle = _handle_identity(
                    mapping,
                    "source_handle_uuid",
                    handles,
                    nodes=nodes,
                    io_type="source",
                )
                for consumer in consumers:
                    generated.append(
                        _rewired_edge(
                            invocation_uuid=invocation_uuid,
                            label="output",
                            source_node_uuid=source_node,
                            source_handle_uuid=source_handle,
                            target_node_uuid=str(consumer.get("target_node_uuid") or ""),
                            target_handle_uuid=str(consumer.get("target_handle_uuid") or ""),
                        )
                    )
                continue
            if mapping.get("kind") != "workflow_input":
                raise CompositeExecutionPlanError("组合输出映射类型无效")
            parameter = str(mapping.get("parameter") or "")
            input_handle = next(
                (identity for identity, name in input_names.items() if name == parameter),
                "",
            )
            providers = incoming_by_handle.get(input_handle, [])
            for provider in providers:
                for consumer in consumers:
                    generated.append(
                        _rewired_edge(
                            invocation_uuid=invocation_uuid,
                            label="passthrough",
                            source_node_uuid=str(provider.get("source_node_uuid") or ""),
                            source_handle_uuid=str(provider.get("source_handle_uuid") or ""),
                            target_node_uuid=str(consumer.get("target_node_uuid") or ""),
                            target_handle_uuid=str(consumer.get("target_handle_uuid") or ""),
                        )
                    )
            if not providers and parameter in invocation_param:
                for consumer in consumers:
                    target_node = str(consumer.get("target_node_uuid") or "")
                    target_handle = str(consumer.get("target_handle_uuid") or "")
                    if target_node not in nodes or target_handle not in handles:
                        raise CompositeExecutionPlanError("组合透传引用未知端点")
                    data_key = _handle_data_key(handles[target_handle])
                    if not data_key:
                        raise CompositeExecutionPlanError("组合透传目标缺少 data_key")
                    param_overrides[target_node][data_key] = _plain(
                        invocation_param[parameter]
                    )

        for consumer in outgoing_by_handle.get(ready_source, []):
            for source in completion_sources:
                generated.append(
                    _rewired_edge(
                        invocation_uuid=invocation_uuid,
                        label="completion",
                        source_node_uuid=_node_identity(source, nodes),
                        source_handle_uuid=_handle_identity(
                            source,
                            "source_handle_uuid",
                            handles,
                            nodes=nodes,
                            io_type="source",
                            structural=True,
                        ),
                        target_node_uuid=str(consumer.get("target_node_uuid") or ""),
                        target_handle_uuid=str(consumer.get("target_handle_uuid") or ""),
                        )
                    )
        if not entry_targets:
            for provider in incoming_by_handle.get(ready_target, []):
                for consumer in outgoing_by_handle.get(ready_source, []):
                    generated.append(
                        _rewired_edge(
                            invocation_uuid=invocation_uuid,
                            label="empty",
                            source_node_uuid=str(
                                provider.get("source_node_uuid") or ""
                            ),
                            source_handle_uuid=str(
                                provider.get("source_handle_uuid") or ""
                            ),
                            target_node_uuid=str(
                                consumer.get("target_node_uuid") or ""
                            ),
                            target_handle_uuid=str(
                                consumer.get("target_handle_uuid") or ""
                            ),
                        )
                    )
        return [*retained, *generated]


def _composite(node: Mapping[str, Any]) -> Mapping[str, Any] | None:
    meta_data = node.get("meta_data")
    unilab = meta_data.get("unilab") if isinstance(meta_data, Mapping) else None
    value = unilab.get("composite") if isinstance(unilab, Mapping) else None
    return value if isinstance(value, Mapping) else None


def _boundary_handle(
    handles: Mapping[str, Mapping[str, Any]],
    invocation: Mapping[str, Any],
    io_type: str,
    key: str,
) -> str:
    template_uuid = invocation.get("workflow_node_template_uuid")
    matches = [
        identity
        for identity, handle in handles.items()
        if handle.get("workflow_node_template_uuid") == template_uuid
        and handle.get("io_type") == io_type
        and handle.get("handle_key") == key
    ]
    if len(matches) != 1:
        raise CompositeExecutionPlanError("组合调用缺少唯一 ready Handle")
    return matches[0]


def _business_boundary_handles(
    handles: Mapping[str, Mapping[str, Any]],
    invocation: Mapping[str, Any],
    io_type: str,
) -> set[str]:
    template_uuid = invocation.get("workflow_node_template_uuid")
    return {
        identity
        for identity, handle in handles.items()
        if handle.get("workflow_node_template_uuid") == template_uuid
        and handle.get("io_type") == io_type
        and handle.get("handle_key") != "ready"
    }


def _rewired_edge(
    *,
    invocation_uuid: str,
    label: str,
    source_node_uuid: str,
    source_handle_uuid: str,
    target_node_uuid: str,
    target_handle_uuid: str,
) -> dict[str, Any]:
    if not all(
        (source_node_uuid, source_handle_uuid, target_node_uuid, target_handle_uuid)
    ):
        raise CompositeExecutionPlanError("组合边界端点不完整")
    name = (
        f"{label}:{source_node_uuid}:{source_handle_uuid}:"
        f"{target_node_uuid}:{target_handle_uuid}"
    )
    return {
        "uuid": str(uuid5(UUID(invocation_uuid), name)),
        "source_node_uuid": source_node_uuid,
        "source_handle_uuid": source_handle_uuid,
        "target_node_uuid": target_node_uuid,
        "target_handle_uuid": target_handle_uuid,
        "meta_data": {"unilab": {"composite_rewired": True}},
    }


def _node_identity(
    item: Mapping[str, Any],
    nodes: Mapping[str, Mapping[str, Any]],
) -> str:
    identity = str(item.get("workflow_node_uuid") or "")
    if identity not in nodes:
        raise CompositeExecutionPlanError("组合映射引用未知节点")
    return identity


def _handle_identity(
    item: Mapping[str, Any],
    field: str,
    handles: Mapping[str, Mapping[str, Any]],
    *,
    nodes: Mapping[str, Mapping[str, Any]],
    io_type: str,
    structural: bool = False,
) -> str:
    identity = str(item.get(field) or "")
    node_identity = _node_identity(item, nodes)
    handle = handles.get(identity)
    node = nodes[node_identity]
    if (
        handle is None
        or handle.get("workflow_node_template_uuid")
        != node.get("workflow_node_template_uuid")
        or handle.get("io_type") != io_type
        or (handle.get("handle_key") == "ready") != structural
    ):
        raise CompositeExecutionPlanError("组合映射引用未知 Handle")
    return identity


def _mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise CompositeExecutionPlanError(f"组合字段 {field} 必须是对象")
    return {str(key): _plain(item) for key, item in value.items()}


def _items(value: Any, field: str) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise CompositeExecutionPlanError(f"组合字段 {field} 必须是数组")
    if any(not isinstance(item, Mapping) for item in value):
        raise CompositeExecutionPlanError(f"组合字段 {field} 包含非法项")
    return [_mapping(item, field) for item in value]


def _handle_data_key(handle: Mapping[str, Any]) -> str:
    return str(handle.get("data_key") or handle.get("handle_key") or "").split(
        "@@@"
    )[-1].strip()


def _binding_reference(value: Any) -> bool:
    return isinstance(value, Mapping) and value.get("kind") in {
        "workflow_input",
        "node_output",
    }


def _node_depth(
    node_uuid: str,
    nodes: Mapping[str, Mapping[str, Any]],
) -> int:
    depth = 0
    seen = {node_uuid}
    parent = nodes[node_uuid].get("parent_uuid")
    while parent is not None and str(parent) in nodes:
        identity = str(parent)
        if identity in seen:
            raise CompositeExecutionPlanError("组合节点父层级存在循环")
        seen.add(identity)
        depth += 1
        parent = nodes[identity].get("parent_uuid")
    return depth


def _deduplicate_edges(edges: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for edge in edges:
        key = (
            str(edge.get("source_node_uuid") or ""),
            str(edge.get("source_handle_uuid") or ""),
            str(edge.get("target_node_uuid") or ""),
            str(edge.get("target_handle_uuid") or ""),
        )
        if not all(key):
            raise CompositeExecutionPlanError("工作流边端点不完整")
        result.setdefault(key, dict(edge))
    return sorted(result.values(), key=lambda item: str(item.get("uuid") or ""))


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


__all__ = [
    "CompositeExecutionPlanError",
    "CompositeExecutionPlanNormalizer",
]
