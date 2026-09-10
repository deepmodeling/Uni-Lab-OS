"""工作流任务（WorkflowTask）输入解析与冻结执行计划（ExecutionPlan）绑定。"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from unilabos.workflow.json_codec import clone_json
from unilabos.workflow.models import validate_uuid
from unilabos.workflow.schema import (
    WorkflowSchemaError,
    normalize_value,
    parse_value_schema,
)
from unilabos.workflow.workflow_io import (
    WorkflowIOValidationError,
    validate_workflow_graph_io,
)


class TaskInputError(ValueError):
    """任务输入无法在任何持久写入前形成唯一冻结解释。"""


ResourceSlotResolver = Callable[[str], Mapping[str, Any] | None]


@dataclass(frozen=True)
class PreparedTaskInput:
    """同一工作流快照中已规范化并绑定的任务创建事实。"""

    workflow_snapshot: dict[str, Any]
    resolved_input: dict[str, Any]
    execution_plan: dict[str, Any]
    jobs: list[dict[str, Any]]


def prepare_task_input(
    *,
    graph: Mapping[str, Any],
    raw_input: Mapping[str, Any],
    execution_plan: Mapping[str, Any],
    jobs: Sequence[Mapping[str, Any]],
    resource_resolver: ResourceSlotResolver | None = None,
) -> PreparedTaskInput:
    """在持久写入前解析任务输入并绑定活动计划节点。

    参数：``graph`` 是当前应用图，``raw_input`` 是请求输入，
    ``execution_plan`` 与 ``jobs`` 来自同一图的计划构造。返回：彼此独立的冻结
    快照、规范输入、计划和首次作业。异常：合同、值、绑定或提供者不唯一时抛
        ``TaskInputError``；物料占位符（ResourceSlot）必须由物料权威唯一解析。
    """

    if not isinstance(raw_input, Mapping) or any(
        not isinstance(key, str) for key in raw_input
    ):
        raise TaskInputError("工作流任务输入必须是字符串键对象")
    try:
        snapshot = clone_json(dict(graph))
        plan = clone_json(dict(execution_plan))
        prepared_jobs = clone_json(list(jobs))
        supplied = clone_json(dict(raw_input))
        validated = validate_workflow_graph_io(snapshot)
        resolved = _resolve_values(
            validated.input_contract.to_dict()["parameters"],
            supplied,
            resource_resolver=resource_resolver,
        )
        _bind_active_plan(
            plan=plan,
            jobs=prepared_jobs,
            input_bindings=validated.input_bindings,
            resolved_input=resolved,
        )
    except TaskInputError:
        raise
    except (
        TypeError,
        ValueError,
        WorkflowIOValidationError,
        WorkflowSchemaError,
    ) as exc:
        raise TaskInputError("工作流任务输入或绑定无效") from exc
    return PreparedTaskInput(
        workflow_snapshot=snapshot,
        resolved_input=resolved,
        execution_plan=plan,
        jobs=prepared_jobs,
    )


def _resolve_values(
    parameters: Sequence[Mapping[str, Any]],
    supplied: Mapping[str, Any],
    *,
    resource_resolver: ResourceSlotResolver | None,
) -> dict[str, Any]:
    """按闭合输入合同解析请求值并填入合同默认值。

    参数：``parameters`` 是已验证的有序参数声明，``supplied`` 是独立请求对象。
    返回：按合同顺序排列的规范输入。异常：未知、缺失、类型不符或含物料占位符
    （ResourceSlot）时抛 ``TaskInputError``。
    """

    declared = {str(parameter["name"]) for parameter in parameters}
    if any(name not in declared for name in supplied):
        raise TaskInputError("工作流任务输入包含未声明参数")
    resolved: dict[str, Any] = {}
    for parameter in parameters:
        name = str(parameter["name"])
        schema = parse_value_schema(parameter["schema"])
        if name in supplied:
            value = supplied[name]
        elif parameter["required"]:
            raise TaskInputError("工作流任务缺少必填输入")
        else:
            value = parameter["default"]
        try:
            normalized = normalize_value(schema, value)
            resolved[name] = _resolve_resource_slot_values(
                schema.to_dict(),
                normalized,
                resource_resolver=resource_resolver,
            )
        except WorkflowSchemaError as exc:
            raise TaskInputError("工作流任务输入值不符合 Schema") from exc
    return resolved


def _resolve_resource_slot_values(
    schema: Mapping[str, Any],
    value: Any,
    *,
    resource_resolver: ResourceSlotResolver | None,
) -> Any:
    """以物料权威解析 Schema 中的每个 ResourceSlot，并保留最小 UUID 值。"""

    if "anyOf" in schema:
        if value is None:
            return None
        members = schema.get("anyOf")
        if not isinstance(members, list) or not members:
            raise TaskInputError("工作流任务 ResourceSlot Schema 无效")
        base = members[0]
        if not isinstance(base, Mapping):
            raise TaskInputError("工作流任务 ResourceSlot Schema 无效")
        return _resolve_resource_slot_values(
            base,
            value,
            resource_resolver=resource_resolver,
        )
    if schema.get("$slot") == "ResourceSlot":
        if resource_resolver is None:
            raise TaskInputError("工作流任务缺少物料权威解析器")
        if not isinstance(value, Mapping) or not isinstance(value.get("uuid"), str):
            raise TaskInputError("工作流任务 ResourceSlot 值无效")
        material_uuid = validate_uuid(value["uuid"])
        try:
            material = resource_resolver(material_uuid)
        except Exception as exc:  # noqa: BLE001 - 解析器错误统一关闭为任务输入失败
            raise TaskInputError("工作流任务物料解析失败") from exc
        if not isinstance(material, Mapping):
            raise TaskInputError("工作流任务引用的物料不存在")
        try:
            resolved_uuid = validate_uuid(str(material.get("uuid") or ""))
            template_uuid = validate_uuid(
                str(material.get("resource_template_uuid") or "")
            )
        except (TypeError, ValueError):
            raise TaskInputError("工作流任务物料权威缺少稳定身份") from None
        if resolved_uuid != material_uuid:
            raise TaskInputError("工作流任务物料权威返回了不一致身份")
        allowed = schema.get("allowed_resource_template_uuids")
        if allowed is not None and template_uuid not in allowed:
            raise TaskInputError("工作流任务物料模板不符合输入约束")
        return {"uuid": resolved_uuid}
    if schema.get("type") == "array":
        items = schema.get("items")
        if not isinstance(items, Mapping) or not isinstance(value, list):
            raise TaskInputError("工作流任务 ResourceSlot 数组无效")
        return [
            _resolve_resource_slot_values(
                items,
                item,
                resource_resolver=resource_resolver,
            )
            for item in value
        ]
    return value


def _bind_active_plan(
    *,
    plan: dict[str, Any],
    jobs: list[dict[str, Any]],
    input_bindings: Mapping[str, Mapping[str, Mapping[str, str]]],
    resolved_input: Mapping[str, Any],
) -> None:
    """把已解析输入绑定到活动计划节点与对应首次作业。

    参数：``plan``/``jobs`` 是独立可修改副本，``input_bindings`` 是公共校验器
    产出的节点绑定，``resolved_input`` 是规范值。返回：无，原地完成冻结绑定。
    异常：静态值、图边和工作流输入同时提供，或必填目标无提供者时抛
    ``TaskInputError``。
    """

    plan_nodes = _indexed_objects(plan.get("nodes"), key="uuid", label="计划节点")
    plan_handles = _indexed_objects(
        plan.get("handles"),
        key="uuid",
        label="计划连接点",
    )
    jobs_by_node = _indexed_objects(
        jobs,
        key="workflow_node_uuid",
        label="计划作业节点",
    )
    incoming = _incoming_edges(_object_list(plan.get("edges"), label="计划边"))
    for handle_uuid, handle in plan_handles.items():
        if handle.get("io_type") != "target":
            continue
        node_uuid = str(handle.get("node_uuid") or "")
        node = plan_nodes.get(node_uuid)
        job = jobs_by_node.get(node_uuid)
        if node is None or job is None:
            raise TaskInputError("计划连接点未归属唯一活动作业")
        template_handle_uuid = str(handle.get("template_handle_uuid") or "")
        binding = input_bindings.get(node_uuid, {}).get(template_handle_uuid)
        input_projection = next(
            (
                item
                for item in _object_list(node.get("inputs"), label="计划节点输入")
                if item.get("handle_uuid") == handle_uuid
            ),
            None,
        )
        if input_projection is None:
            raise TaskInputError("计划目标连接点缺少节点输入投影")
        data_key = str(input_projection.get("data_key") or "")
        if not data_key:
            raise TaskInputError("计划目标连接点缺少参数键")
        node_param = node.get("param")
        job_param = job.get("param")
        if not isinstance(node_param, dict) or not isinstance(job_param, dict):
            raise TaskInputError("计划节点或作业参数不是对象")
        incoming_edges = incoming.get(handle_uuid, [])
        static_provider = data_key in node_param and node_param[data_key] is not None
        # 固定 existing 物料来源（MaterialSource）在计划构建时把同一条物料边
        # 解析出的具体引用预投影到动作参数；它与该边是一个提供者，不能重复计数。
        if static_provider and any(
            _is_prebound_material_edge(
                edge=edge,
                plan_nodes=plan_nodes,
                target_data_key=data_key,
                target_param=node_param,
            )
            for edge in incoming_edges
        ):
            static_provider = False
        provider_count = (
            int(static_provider) + len(incoming_edges) + int(binding is not None)
        )
        if provider_count > 1:
            raise TaskInputError("计划目标输入存在多个提供者")
        if bool(input_projection.get("required")) and provider_count == 0:
            raise TaskInputError("计划必填目标输入没有提供者")
        if binding is None:
            continue
        parameter = binding["parameter"]
        if parameter not in resolved_input:
            raise TaskInputError("计划输入绑定引用未解析参数")
        node_param[data_key] = clone_json(resolved_input[parameter])
        job_param[data_key] = clone_json(resolved_input[parameter])


def _incoming_edges(
    edges: Sequence[Mapping[str, Any]],
) -> dict[str, list[Mapping[str, Any]]]:
    """按目标运行连接点索引提供值的计划边。

    参数：``edges`` 是已验证为对象的冻结计划边。返回：排除纯依赖边后按目标
    连接点 UUID 分组的边列表。异常：本函数不抛异常；缺失目标身份的边保留在
    空字符串组，并由后续必填提供者校验失败关闭。
    """

    result: dict[str, list[Mapping[str, Any]]] = {}
    for edge in edges:
        if edge.get("dependency_only") is True:
            continue
        result.setdefault(str(edge.get("target_handle_uuid") or ""), []).append(edge)
    return result


def _is_prebound_material_edge(
    *,
    edge: Mapping[str, Any],
    plan_nodes: Mapping[str, Mapping[str, Any]],
    target_data_key: str,
    target_param: Mapping[str, Any],
) -> bool:
    """识别已由同一物料边预投影的固定物料引用。

    参数：``edge`` 是目标输入的计划边，``plan_nodes`` 是活动计划节点索引，
    ``target_data_key``/``target_param`` 定位动作参数。返回：来源确为固定
    existing 物料来源（MaterialSource），且动作参数等于该来源 UUID 引用时为真。
    异常：不抛异常；任何形状或语义不匹配都返回假并继续按独立静态提供者计数。
    """

    if (
        edge.get("source_type") != "ResourceSlot"
        or edge.get("target_type") != "ResourceSlot"
    ):
        return False
    source = plan_nodes.get(str(edge.get("source_node_uuid") or ""))
    if source is None or source.get("kind") != "material_source":
        return False
    source_param = source.get("param")
    if not isinstance(source_param, Mapping) or source_param.get("mode") != "existing":
        return False
    material_uuid = source_param.get("material_uuid")
    return isinstance(material_uuid, str) and target_param.get(target_data_key) == {
        "uuid": material_uuid
    }


def _indexed_objects(
    raw: Any,
    *,
    key: str,
    label: str,
) -> dict[str, dict[str, Any]]:
    """把对象列表按必填字符串字段建立唯一索引。

    参数：``raw`` 是候选列表，``key`` 是身份字段，``label`` 用于中文诊断。
    返回：保持原对象引用的索引。异常：形状、身份或唯一性不合法时抛
    ``TaskInputError``。
    """

    result: dict[str, dict[str, Any]] = {}
    for item in _object_list(raw, label=label):
        identity = item.get(key)
        if not isinstance(identity, str) or not identity or identity in result:
            raise TaskInputError(f"{label}身份无效或重复")
        result[identity] = item
    return result


def _object_list(raw: Any, *, label: str) -> list[dict[str, Any]]:
    """把不受信任值收窄为对象列表。

    参数：``raw`` 是候选 JSON 值，``label`` 是中文诊断名称。返回：原对象列表。
    异常：不是列表或成员不是对象时抛 ``TaskInputError``。
    """

    if not isinstance(raw, list) or any(not isinstance(item, dict) for item in raw):
        raise TaskInputError(f"{label}必须是对象列表")
    return raw


__all__ = [
    "PreparedTaskInput",
    "ResourceSlotResolver",
    "TaskInputError",
    "prepare_task_input",
]
