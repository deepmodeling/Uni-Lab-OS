"""冻结后端（Backend）全图 PUT 的本地语义校验。"""

from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from collections.abc import Sequence
from copy import deepcopy
from typing import Any, Dict, Iterable, List, Mapping

from unilabos.workflow.authoring_material_io import (
    MaterialIOAuthoringError,
    validate_material_io_node,
)
from unilabos.workflow.execution_resource_policy import (
    ExecutionResourcePolicyError,
    normalize_execution_resource_policy,
)
from unilabos.workflow.json_codec import encode_json, strict_json_equal
from unilabos.workflow.material_graph_validation import (
    MaterialGraphValidationError,
    validate_material_graph,
)
from unilabos.workflow.material_selector import (
    MaterialSelectorError,
    validate_material_source_node,
)
from unilabos.workflow.models import (
    WorkflowEdgeWrite,
    WorkflowNodeWrite,
    validate_json_value,
)
from unilabos.workflow.workflow_io import (
    WorkflowIOValidationError,
    validate_workflow_io,
)

_MAX_SCHEMA_DEPTH = 64
_MAX_TIMEOUT_SECONDS = (2**63 - 1) // 1_000_000_000


class GraphValidationError(ValueError):
    """提交的全图不满足冻结后端（Backend）语义。"""


class CodedGraphValidationError(GraphValidationError):
    """带稳定领域错误码的全图校验失败。"""

    def __init__(self, code: str, message: str):
        """保存机器码和中文诊断。

        参数说明：``code`` 是服务可透传的机器码，``message`` 解释具体约束。
        返回：无；构造全图校验异常。
        """

        super().__init__(message)
        self.code = code


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
    validate_workflow_io_contract: bool = False,
) -> None:
    """在写事务内校验一份完整替换的工作流图（Workflow Graph）。

    参数说明：``nodes`` 与 ``edges`` 是待替换的完整节点和边；``templates`` 与
    ``handles`` 是其引用的活动节点模板和连接点（Handle）模板；
    ``effective_params`` 是合并保留字段后的节点实参；``workflow_meta_data`` 与
    ``node_meta_data`` 是事务内实际工作流和节点元数据；
    ``validate_workflow_io_contract`` 为 ``True`` 时启用唯一公共工作流输入/输出
    （Workflow I/O）合同，为 ``False`` 时保留旧调用方兼容语义。返回：无；全部
    不变量成立才正常返回。异常：缺少模板抛出 ``MissingTemplateError``；物料来源
    （MaterialSource）、物料流线性（MaterialFlowLinearity）或资源模板兼容
    （ResourceTemplate Compatibility）抛出 ``CodedGraphValidationError``；其余图、
    连接点或工作流输入/输出错误抛出 ``GraphValidationError``。
    """

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
        if _node_kind(node, templates) == "material_source":
            try:
                validate_material_source_node(
                    {
                        "material_uuid": node.material_uuid,
                        "param": effective_params[node.uuid],
                    }
                )
            except MaterialSelectorError as error:
                raise CodedGraphValidationError(
                    error.code,
                    error.message,
                ) from error
        if _node_kind(node, templates) == "material_io":
            try:
                validate_material_io_node(
                    {
                        "param": effective_params[node.uuid],
                        "meta_data": node.meta_data,
                    }
                )
            except MaterialIOAuthoringError as error:
                raise GraphValidationError(error.message) from error
        if _node_kind(node, templates) == "condition" and not node.disabled:
            _validate_condition_branches(node, node_by_uuid, effective_params)
    _validate_parent_cycles(nodes)
    for node in nodes:
        parent = node_by_uuid.get(node.parent_uuid) if node.parent_uuid else None
        if (
            node.disabled
            and parent is not None
            and not parent.disabled
            and _node_kind(parent, templates) in {"condition", "repeat_until"}
        ):
            # 启用的控制区域不会执行被禁用的成员，执行计划必然拒绝；要跳过整段
            # 流程应禁用整个控制区域。
            raise CodedGraphValidationError(
                "invalid_control_region",
                f"控制区域成员 {node.uuid} 已禁用；如需跳过请禁用整个控制区域",
            )

    validated_io = None
    if validate_workflow_io_contract:
        try:
            validated_io = validate_workflow_io(
                nodes=node_by_uuid,
                handles=handles,
                workflow_meta_data=workflow_meta_data,
                node_meta_data=node_meta_data,
            )
        except WorkflowIOValidationError as exc:
            raise GraphValidationError("工作流输入/输出合同无效") from exc

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

    try:
        validate_material_graph(
            nodes=nodes,
            edges=edges,
            templates=templates,
            handles=handles,
            effective_params=effective_params,
            validated_workflow_io=validated_io,
        )
    except MaterialGraphValidationError as error:
        raise CodedGraphValidationError(error.code, error.message) from error

    bindings_by_node = (
        dict(validated_io.input_bindings)
        if validated_io is not None
        else {
            node.uuid: _validated_input_bindings(
                node,
                node_meta_data[node.uuid],
                workflow_meta_data,
                handles,
            )
            for node in nodes
        }
    )
    bindings_by_node = {
        node_uuid: dict(bindings) for node_uuid, bindings in bindings_by_node.items()
    }
    for node in nodes:
        carry_bindings = _validated_carry_bindings(
            node,
            node_meta_data[node.uuid],
            handles,
            nodes=node_by_uuid,
            templates=templates,
        )
        overlap = set(bindings_by_node[node.uuid]) & set(carry_bindings)
        if overlap:
            raise GraphValidationError("同一目标 Handle 不能同时绑定输入和 carry")
        bindings_by_node[node.uuid].update(carry_bindings)
        site_group_bindings = _validated_site_group_bindings(
            node,
            node_meta_data[node.uuid],
            handles,
            workflow_meta_data,
        )
        overlap = set(bindings_by_node[node.uuid]) & set(site_group_bindings)
        if overlap:
            raise GraphValidationError(
                "同一目标 Handle 不能同时绑定输入、carry 和命名库位组"
            )
        bindings_by_node[node.uuid].update(site_group_bindings)
    enabled = {
        node.uuid: node
        for node in nodes
        if not node.disabled and _node_kind(node, templates) != "group"
    }
    _validate_replenishment_branches(enabled, templates, effective_params)
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
        enabled_edges.append(edge)
        if _dependency_only(source_handle):
            continue
        if target_input in incoming:
            raise GraphValidationError("同一目标 Handle 只能有一条数据入边")
        incoming[target_input] = edge.uuid
        connected_inputs[target_input] = edge.uuid
        available_data_keys[edge.target_node_uuid].append(
            _handle_data_key(target_handle)
        )

    _project_composite_boundary_inputs(
        nodes=enabled,
        ancestry_nodes=node_by_uuid,
        handles=handles,
        effective_params=effective_params,
        node_meta_data=node_meta_data,
        bindings_by_node=bindings_by_node,
        connected_inputs=connected_inputs,
        available_data_keys=available_data_keys,
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
        # 工作流调用模板的 ``schema`` 是发布合同 envelope，不是节点 ``param``
        # 的动作输入 JSON Schema；业务输入仍由边界连接点（Handle）校验。
        if template_uuid is not None and _node_kind(node, templates) != "workflow":
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
                metadata = node_meta_data.get(node.uuid, {})
                unilab = (
                    metadata.get("unilab") if isinstance(metadata, Mapping) else None
                )
                binding = (
                    unilab.get("executor_binding")
                    if isinstance(unilab, Mapping)
                    else None
                )
                if binding is not None:
                    raise GraphValidationError("固定设备动作节点必须绑定 material_uuid")
                if (
                    template_uuid is None
                    or not str(
                        templates[template_uuid].get("resource_template_uuid") or ""
                    ).strip()
                ):
                    raise GraphValidationError("动态设备动作节点缺少设备类型")


def _project_composite_boundary_inputs(
    *,
    nodes: Mapping[str, WorkflowNodeWrite],
    ancestry_nodes: Mapping[str, WorkflowNodeWrite],
    handles: Mapping[str, Dict[str, Any]],
    effective_params: Mapping[str, Dict[str, Any]],
    node_meta_data: Mapping[str, Dict[str, Any]],
    bindings_by_node: Mapping[str, Mapping[str, Mapping[str, str]]],
    connected_inputs: Dict[tuple[str, str], str],
    available_data_keys: Dict[str, List[str]],
) -> None:
    """把组合调用边界已提供的值投影到展开内部目标。

    参数：已启用节点、完整父子层级、目录连接点（Handle）、实参、元数据、输入绑定
    和已连线事实。返回：无；原地补充内部目标的可用键。异常：
    边界映射不闭合、跨调用层级或引用错误连接点时抛出
    ``GraphValidationError``。
    """

    for invocation_uuid, invocation in nodes.items():
        metadata = node_meta_data.get(invocation_uuid, {})
        unilab = metadata.get("unilab") if isinstance(metadata, Mapping) else None
        composite = unilab.get("composite") if isinstance(unilab, Mapping) else None
        mappings = (
            composite.get("target_mappings") if isinstance(composite, Mapping) else None
        )
        if mappings is None:
            continue
        if not isinstance(mappings, Mapping):
            raise GraphValidationError("组合工作流目标映射必须是对象")
        for boundary_handle_uuid, targets in mappings.items():
            boundary_handle = (
                handles.get(boundary_handle_uuid)
                if isinstance(boundary_handle_uuid, str)
                else None
            )
            if (
                boundary_handle is None
                or boundary_handle.get("workflow_node_template_uuid")
                != invocation.workflow_node_template_uuid
                or boundary_handle.get("io_type") != "target"
                or not isinstance(targets, list)
            ):
                raise GraphValidationError("组合工作流目标映射边界无效")
            boundary_key = _handle_data_key(boundary_handle)
            provided = (
                (invocation_uuid, boundary_handle_uuid) in connected_inputs
                or boundary_handle_uuid in bindings_by_node.get(invocation_uuid, {})
                or boundary_key in effective_params.get(invocation_uuid, {})
            )
            if not provided:
                continue
            for target in targets:
                if not isinstance(target, Mapping):
                    raise GraphValidationError("组合工作流内部目标映射无效")
                target_node_uuid = target.get("workflow_node_uuid")
                target_handle_uuid = target.get("target_handle_uuid")
                target_node = (
                    nodes.get(target_node_uuid)
                    if isinstance(target_node_uuid, str)
                    else None
                )
                target_handle = (
                    handles.get(target_handle_uuid)
                    if isinstance(target_handle_uuid, str)
                    else None
                )
                if (
                    target_node is None
                    or target_handle is None
                    or not _is_composite_descendant(
                        target_node_uuid,
                        invocation_uuid,
                        # 展示分组不执行，但仍是调用子树中的真实父节点。
                        ancestry_nodes,
                    )
                    or target_handle.get("workflow_node_template_uuid")
                    != target_node.workflow_node_template_uuid
                    or target_handle.get("io_type") != "target"
                ):
                    raise GraphValidationError("组合工作流内部目标越过调用边界")
                target_input = (target_node_uuid, target_handle_uuid)
                target_key = _handle_data_key(target_handle)
                if (
                    target_input in connected_inputs
                    or target_handle_uuid in bindings_by_node.get(target_node_uuid, {})
                    or target_key in effective_params.get(target_node_uuid, {})
                ):
                    continue
                connected_inputs[target_input] = f"composite:{invocation_uuid}"
                available_data_keys[target_node_uuid].append(target_key)


def _is_composite_descendant(
    node_uuid: str,
    invocation_uuid: str,
    nodes: Mapping[str, WorkflowNodeWrite],
) -> bool:
    """判断映射目标是否位于调用节点的任意深度子树内。

    RepeatUntil/condition 会在展开图中形成额外的控制层级。组合参数仍然
    只能进入当前调用节点的私有子树，但不应被错误限制为必须是直接子节点。
    参数沿父引用向上追溯，遇到调用根即成功；缺失父节点或循环则失败关闭。
    """

    current = node_uuid
    visited: set[str] = set()
    while current not in visited:
        if current == invocation_uuid:
            return True
        visited.add(current)
        node = nodes.get(current)
        if node is None or node.parent_uuid is None:
            return False
        current = str(node.parent_uuid)
    return False


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


def _validate_replenishment_branches(
    enabled: Mapping[str, WorkflowNodeWrite],
    templates: Mapping[str, Dict[str, Any]],
    effective_params: Mapping[str, Dict[str, Any]],
) -> None:
    """保存前复用计划构建的补料规则，拒绝缺少有料/缺料两条非空分支的图。

    参数说明：``enabled`` 是参与执行的节点，其余为模板与合并后的节点实参。
    返回：无。异常：规则不成立抛出 ``CodedGraphValidationError``，保证无效图
    不会覆盖当前有效修订，而不是等到计划构建、预检或建任务时才被拒绝。
    """

    projected = [
        {"uuid": uuid, "kind": kind, "param": effective_params[uuid]}
        for uuid, node in enabled.items()
        if (kind := _node_kind(node, templates)) in {"material_source", "condition"}
    ]
    if not any(
        item["kind"] == "material_source"
        and item["param"].get("supports_replenishment") is True
        for item in projected
    ):
        return
    # 函数内导入：计划图模块在导入期依赖 store，而 store 依赖本模块。
    from unilabos.workflow._execution_plan_graph import ExecutionPlanBuildError
    from unilabos.workflow.runtime_material_source import validate_replenishment_plan

    try:
        validate_replenishment_plan(projected)
    except ExecutionPlanBuildError as error:
        raise CodedGraphValidationError(
            "material_shortage_unhandled", str(error)
        ) from error
    except (AttributeError, KeyError, TypeError) as error:
        # 条件 bindings/branches 形状损坏时给业务错误，不能冒成 500。
        raise CodedGraphValidationError(
            "invalid_control_region", f"条件节点的绑定或分支结构无效：{error}"
        ) from error


def _node_kind(
    node: WorkflowNodeWrite,
    templates: Mapping[str, Dict[str, Any]],
) -> str:
    """把节点或模板 wire 类型规范为本地执行分类。

    参数说明：``node`` 是候选工作流节点（WorkflowNode），``templates`` 是
    当前最小节点模板投影。返回：公共校验使用的执行分类；未知类型关闭失败。
    异常：模板缺失或类型不受支持时抛出 ``GraphValidationError``。
    """

    # ``raw_kind`` 优先采用节点模板的权威类型，节点字段仅兼容无模板旧图。
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
        "repeat_until": "repeat_until",
        "script": "script",
        "py_script": "script",
        "group": "group",
        "tool_call": "tool_call",
        "manual_confirm": "manual_confirm",
        "material_io": "material_io",
        "material_source": "material_source",
        "workflow_command": "workflow_command",
        # 组合工作流调用（CompositeWorkflowInvocation）是创作层层级节点；
        # 运行时仍只执行其静态展开的叶动作，不创建嵌套工作流任务（WorkflowTask）。
        "workflow": "workflow",
    }
    kind = aliases.get(str(raw_kind).strip().lower())
    if kind is None:
        raise GraphValidationError(f"不支持的节点执行类型 {raw_kind!r}")
    return kind


def _nonempty_id_list(value: Any) -> bool:
    return (
        isinstance(value, Sequence)
        and not isinstance(value, (str, bytes))
        and len(value) > 0
    )


def _condition_descendants(
    region_uuid: str,
    nodes: Mapping[str, WorkflowNodeWrite],
) -> set[str]:
    children: dict[str, list[str]] = defaultdict(list)
    for node in nodes.values():
        if node.parent_uuid is not None:
            children[node.parent_uuid].append(node.uuid)
    found: set[str] = set()
    pending = list(children.get(region_uuid, []))
    while pending:
        current = pending.pop()
        if current in found:
            continue
        found.add(current)
        pending.extend(children.get(current, []))
    return found


def _validate_condition_branches(
    node: WorkflowNodeWrite,
    nodes: Mapping[str, WorkflowNodeWrite],
    effective_params: Mapping[str, Dict[str, Any]],
) -> None:
    """拒绝空的条件分支，避免无效控制区域覆盖当前图。"""

    params = effective_params.get(node.uuid) or {}
    branches = params.get("branches") if isinstance(params, Mapping) else None
    if not _nonempty_id_list(branches):
        raise CodedGraphValidationError("invalid_control_region", "条件区域缺少有序分支")
    descendants = _condition_descendants(node.uuid, nodes)
    declared: set[str] = set()
    for branch in branches:
        if not isinstance(branch, Mapping):
            raise CodedGraphValidationError("invalid_control_region", "条件分支必须是对象")
        entries = branch.get("entry_node_uuids")
        exits = branch.get("exit_node_uuids")
        members = branch.get("node_uuids")
        if not (
            _nonempty_id_list(entries)
            and _nonempty_id_list(exits)
            and _nonempty_id_list(members)
        ):
            raise CodedGraphValidationError("invalid_control_region", "条件分支成员或边界无效")
        member_uuids = [str(value) for value in members]
        member_set = set(member_uuids)
        disabled_members = sorted(
            uuid for uuid in member_set if uuid in nodes and nodes[uuid].disabled
        )
        if disabled_members:
            # 启用的条件区域不会执行被禁用的成员，执行计划必然拒绝；要跳过整段
            # 流程应禁用整个条件区域。
            raise CodedGraphValidationError(
                "invalid_control_region",
                f"条件分支成员 {disabled_members[0]} 已禁用；如需跳过请禁用整个条件区域",
            )
        if len(member_set) != len(member_uuids):
            raise CodedGraphValidationError("invalid_control_region", "条件分支成员不能重复")
        if (
            not member_set <= descendants
            or declared & member_set
            or not {str(value) for value in entries} <= member_set
            or not {str(value) for value in exits} <= member_set
        ):
            raise CodedGraphValidationError("invalid_control_region", "条件分支成员不属于对应控制区域")
        declared.update(member_set)


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
    # 已发布工作流边界的业务输出使用 ``result``；它与普通
    # 动作的 ``executor`` 输出一样承载值，不能被降级为纯依赖边。
    return bool(data_source) and data_source.lower() not in {"executor", "result"}


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


def _validated_carry_bindings(
    node: WorkflowNodeWrite,
    meta_data: Mapping[str, Any],
    handles: Mapping[str, Dict[str, Any]],
    *,
    nodes: Mapping[str, WorkflowNodeWrite],
    templates: Mapping[str, Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """校验动作输入只引用其祖先 RepeatUntil 区域中的 carry 键。"""

    unilab = meta_data.get("unilab", {})
    raw_bindings = (
        unilab.get("carry_bindings", {}) if isinstance(unilab, Mapping) else {}
    )
    if not isinstance(raw_bindings, dict):
        raise GraphValidationError("carry_bindings 必须是对象")
    result: Dict[str, Dict[str, Any]] = {}
    for handle_uuid, raw_binding in raw_bindings.items():
        handle = handles.get(handle_uuid)
        if (
            handle is None
            or handle.get("workflow_node_template_uuid")
            != node.workflow_node_template_uuid
            or handle.get("io_type") != "target"
        ):
            raise GraphValidationError("carry_binding 未引用本节点的目标 Handle")
        if not isinstance(raw_binding, dict):
            raise GraphValidationError("carry_binding 必须是对象")
        region_uuid = raw_binding.get("control_region_uuid")
        key = raw_binding.get("key")
        region = nodes.get(region_uuid) if isinstance(region_uuid, str) else None
        if region is None or _node_kind(region, templates) != "repeat_until":
            raise GraphValidationError("carry_binding 未引用 RepeatUntil 区域")
        current = node.parent_uuid
        seen: set[str] = set()
        while (
            isinstance(current, str) and current not in seen and current != region_uuid
        ):
            seen.add(current)
            parent = nodes.get(current)
            current = parent.parent_uuid if parent is not None else None
        if current != region_uuid:
            raise GraphValidationError("carry_binding 只能引用祖先循环区域")
        params = region.param if isinstance(region.param, Mapping) else {}
        initial_carry = params.get("initial_carry")
        if (
            not isinstance(key, str)
            or not isinstance(initial_carry, Mapping)
            or key not in initial_carry
        ):
            raise GraphValidationError("carry_binding 引用了未知 carry 键")
        result[handle_uuid] = dict(raw_binding)
    return result


def _validated_site_group_bindings(
    node: WorkflowNodeWrite,
    meta_data: Mapping[str, Any],
    handles: Mapping[str, Dict[str, Any]],
    workflow_meta_data: Mapping[str, Any],
) -> Dict[str, Dict[str, Any]]:
    """校验命名库位组是本动作 SiteSelector 目标连接点的唯一 Provider。"""

    unilab = meta_data.get("unilab", {})
    raw_bindings = (
        unilab.get("site_group_bindings", {}) if isinstance(unilab, Mapping) else {}
    )
    if not isinstance(raw_bindings, dict):
        raise GraphValidationError("site_group_bindings 必须是对象")
    if raw_bindings and node.workflow_node_template_uuid is None:
        raise GraphValidationError("无模板节点不能声明 site_group_bindings")
    result: Dict[str, Dict[str, Any]] = {}
    for handle_uuid, raw_binding in raw_bindings.items():
        handle = handles.get(handle_uuid)
        if (
            handle is None
            or handle.get("workflow_node_template_uuid")
            != node.workflow_node_template_uuid
            or handle.get("io_type") != "target"
        ):
            raise GraphValidationError("site_group_binding 未引用本节点的目标 Handle")
        required_fields = {
            "version",
            "group_key",
            "owner_parameter",
            "strategy",
        }
        if not isinstance(raw_binding, dict) or not (
            required_fields <= set(raw_binding)
            and set(raw_binding) <= required_fields | {"exact_parameter"}
        ):
            raise GraphValidationError("site_group_binding 字段非法")
        group_key = raw_binding.get("group_key")
        owner_parameter = raw_binding.get("owner_parameter")
        if (
            raw_binding.get("version") != 1
            or raw_binding.get("strategy") != "sort_order"
            or not isinstance(group_key, str)
            or not group_key.strip()
            or group_key != group_key.strip()
            or not isinstance(owner_parameter, str)
            or not owner_parameter.strip()
        ):
            raise GraphValidationError("site_group_binding 选择策略非法")
        handle_meta = handle.get("meta_data")
        handle_unilab = (
            handle_meta.get("unilab") if isinstance(handle_meta, Mapping) else None
        )
        selector = (
            handle_unilab.get("site_selector")
            if isinstance(handle_unilab, Mapping)
            else None
        )
        if (
            not isinstance(selector, Mapping)
            or selector.get("version") != 1
            or selector.get("owner") != owner_parameter
        ):
            raise GraphValidationError("site_group_binding 与 SiteSelector 合同不一致")
        exact_parameter = raw_binding.get("exact_parameter")
        if exact_parameter is not None:
            workflow_unilab = workflow_meta_data.get("unilab", {})
            input_contract = (
                workflow_unilab.get("input_contract", {})
                if isinstance(workflow_unilab, Mapping)
                else {}
            )
            parameters = (
                input_contract.get("parameters", [])
                if isinstance(input_contract, Mapping)
                else []
            )
            parameter_names = [
                item.get("name")
                for item in parameters
                if isinstance(item, Mapping) and isinstance(item.get("name"), str)
            ]
            if (
                not isinstance(exact_parameter, str)
                or parameter_names.count(exact_parameter) != 1
            ):
                raise GraphValidationError(
                    "site_group_binding.exact_parameter 未唯一引用 Workflow 参数"
                )
        result[handle_uuid] = dict(raw_binding)
    return result


def _validate_execution_policy(policy: Mapping[str, Any]) -> None:
    """校验节点执行期限、完整资源声明并拒绝 PLC 访问区域锁。

    参数：``policy`` 是应用图节点的公开执行策略。返回无。异常：超时非法或
    声明 ``access_region`` 时抛 ``GraphValidationError``；机械臂碰撞区域互斥
    属于 PLC 安全责任，不得形成 Scheduler 资源作用域。
    """

    if "access_region" in policy:
        raise GraphValidationError("access_region 由 PLC 保证，工作流不得声明软件锁")
    try:
        normalized = normalize_execution_resource_policy(policy)
    except ExecutionResourcePolicyError as error:
        raise GraphValidationError(str(error)) from error
    timeout = normalized.get("execution_timeout_seconds")
    if timeout is not None and timeout > _MAX_TIMEOUT_SECONDS:
        raise GraphValidationError("execution_timeout_seconds 超出 int64 纳秒范围")


def _parse_schema(raw_schema: Any) -> Any:
    """把模板存储表示解析为与调用方分离的节点参数 JSON Schema。

    参数说明：``raw_schema`` 是模板投影写入的字典、布尔值或既有 JSON 字符串。
    返回：独立的 JSON Schema 对象、布尔值或缺省 ``None``。异常：容器、JSON
    编码或根类型无效时抛出 ``GraphValidationError``，不得按 Python repr 解码。
    """

    if raw_schema is None or isinstance(raw_schema, str) and not raw_schema.strip():
        return None
    try:
        schema = (
            json.loads(raw_schema)
            if isinstance(raw_schema, str)
            else deepcopy(raw_schema)
        )
        validate_json_value(schema)
    except (TypeError, ValueError, RecursionError) as exc:
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
    declared_type = schema.get("type")
    if value is None and (
        declared_type == "null"
        or (
            isinstance(declared_type, list)
            and "null" in declared_type
        )
    ):
        # JSON Schema 的 ``required`` 只约束对象实例；可空对象取 null 时，
        # 不能继续把其 properties 当作已存在对象检查嵌套必填字段。
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
