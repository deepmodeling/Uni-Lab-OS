"""运行时物料检查与既有条件分支之间的合同校验。"""

from collections.abc import Mapping, Sequence
from typing import Any

from unilabos.workflow._execution_plan_graph import ExecutionPlanBuildError, final_target_data_key
from unilabos.workflow.control_expression import (
    ConditionEvaluationError,
    evaluate_condition_expression,
)


def runtime_command_material_edges(
    nodes: Sequence[Mapping[str, Any]],
    edges: Sequence[Mapping[str, Any]],
    handles: Sequence[Mapping[str, Any]] = (),
) -> set[str]:
    """找出本地工作流命令在运行时才产出的物料边。

    命令输出的物料占位符若是同名输入的透传（``material_passthrough_handles``），
    身份在计划期已知；否则（例如读取物料所在容器）只有执行后才知道具体物料，
    下游消费者须按运行时物料处理。返回：这类数据边的 UUID 集合。
    """
    by_uuid = {node["uuid"]: node for node in nodes}
    templates = {str(handle["uuid"]): str(handle.get("template_handle_uuid") or "") for handle in handles}
    found: set[str] = set()
    for edge in edges:
        if edge.get("source_type") != "ResourceSlot" or edge.get("dependency_only"):
            continue
        node = by_uuid.get(edge["source_node_uuid"])
        if node is None or node.get("kind") != "workflow_command":
            continue
        unilab = (node.get("meta_data") or {}).get("unilab") or {}
        passthrough = unilab.get("material_passthrough_handles") or {}
        handle = str(edge.get("source_handle_uuid") or "")
        if templates.get(handle, handle) not in passthrough and handle not in passthrough:
            found.add(str(edge.get("uuid") or id(edge)))
    return found


def runtime_material_parameters(
    nodes: Sequence[Mapping[str, Any]],
    edges: Sequence[Mapping[str, Any]],
    handles: Sequence[Mapping[str, Any]] = (),
) -> dict[str, list[str]]:
    """沿物料来源数据边标记由准入或运行时库存权威解析的参数。

    种子包括物料来源、物料装卸，以及本地命令在运行时才产出的物料边。
    """
    dynamic = {
        node["uuid"]
        for node in nodes
        if node.get("kind") in {"material_source", "material_io"}
        or node.get("runtime_material_source")
    }
    command_edges = runtime_command_material_edges(nodes, edges, handles)
    parameters: dict[str, set[str]] = {}
    for _ in range(len(nodes) + 1):
        previous = len(dynamic)
        for edge in edges:
            if edge.get("source_type") != "ResourceSlot" or edge.get("dependency_only"):
                continue
            if (edge["source_node_uuid"] not in dynamic
                    and str(edge.get("uuid") or id(edge)) not in command_edges):
                continue
            target = edge["target_node_uuid"]
            parameters.setdefault(target, set()).add(final_target_data_key(edge["target_data_key"]))
            dynamic.add(target)
        if len(dynamic) == previous:
            break
    return {node: sorted(keys) for node, keys in parameters.items()}


def validate_replenishment_plan(nodes: Sequence[Mapping[str, Any]]) -> None:
    """执行前证明支持补料的来源有消费 sufficient 的完整条件分支。"""
    for source in nodes:
        if source.get("kind") != "material_source":
            continue
        if not source.get("param", {}).get("supports_replenishment"):
            continue
        handlers = []
        for node in nodes:
            if node.get("kind") != "condition":
                continue
            region = node.get("control_region") or node.get("param") or {}
            bindings = region.get("bindings", {})
            names = [name for name, binding in bindings.items()
                     if binding.get("kind") == "node_result"
                     and binding.get("node_uuid") == source["uuid"]]
            if not names:
                continue
            # 补料的基本判断只依赖本次来源结果；其余业务条件可放入分支内。
            selected = []
            for sufficient in (True, False):
                variables = {name: {"sufficient": sufficient} for name in names}
                branch = None
                try:
                    for candidate in region.get("branches", []):
                        expression = candidate.get("condition")
                        if expression is None or evaluate_condition_expression(expression, variables=variables):
                            branch = candidate
                            break
                except ConditionEvaluationError:
                    branch = None
                selected.append(branch)
            if all(selected) and selected[0] != selected[1] and all(
                branch.get("node_uuids") for branch in selected
            ):
                handlers.append((node, selected[0]))
        if len(handlers) != 1:
            raise ExecutionPlanBuildError(
                "MATERIAL_SHORTAGE_UNHANDLED",
                f"物料来源 {source['uuid']} 启用支持补料后，必须连接一个读取 sufficient 的条件节点，并配置有料和缺料两条分支",
            )
        _, ready_branch = handlers[0]
        for target in source.get("material_binding_targets", []):
            if target["workflow_node_uuid"] not in ready_branch["node_uuids"]:
                raise ExecutionPlanBuildError(
                    "MATERIAL_SHORTAGE_UNHANDLED",
                    f"来源 {source['uuid']} 的物料只能在 sufficient=true 分支使用；补料后应使用补料步骤返回的物料",
                )
        # 以本来源输出作动态 mount 的子来源同样只能放在有料分支：缺料时输出为空。
        for node in nodes:
            binding = node.get("runtime_mount_binding")
            if (
                isinstance(binding, Mapping)
                and binding.get("source_node_uuid") == source["uuid"]
                and node.get("uuid") not in ready_branch["node_uuids"]
            ):
                raise ExecutionPlanBuildError(
                    "MATERIAL_SHORTAGE_UNHANDLED",
                    f"来源 {source['uuid']} 的物料只能在 sufficient=true 分支作为挂载位；"
                    "缺料分支应使用补料步骤返回的物料",
                )
