"""工作流任务（WorkflowTask）输入解析与冻结执行计划（ExecutionPlan）绑定。"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4, uuid5

from unilabos.workflow.execution_plan import ExecutionPlanBuilder
from unilabos.workflow.json_codec import clone_json
from unilabos.workflow.models import validate_uuid
from unilabos.workflow.schema import (
    WorkflowSchemaError,
    normalize_value,
    parse_value_schema,
)
from unilabos.workflow.workflow_boundary_binding import WorkflowBoundaryBindingError, flatten_output_bindings
from unilabos.workflow.workflow_io import (
    ValidatedWorkflowIO,
    WorkflowIOValidationError,
    validate_workflow_graph_io,
)


class TaskInputError(ValueError):
    """任务输入无法在任何持久写入前形成唯一冻结解释。"""


ResourceSlotResolver = Callable[[str], Mapping[str, Any] | None]
SiteSelectionResolver = Callable[[Mapping[str, Any]], Mapping[str, Any]]


@dataclass(frozen=True)
class PreparedTaskInput:
    """同一工作流快照中已规范化并绑定的任务创建事实。"""

    workflow_snapshot: dict[str, Any]
    resolved_input: dict[str, Any]
    execution_plan: dict[str, Any]
    jobs: list[dict[str, Any]]

    @property
    def planned_node_uuids(self) -> frozenset[str]:
        """返回本次冻结计划中的全部节点身份，包括惰性控制流后代。"""

        return frozenset(
            str(node.get("uuid"))
            for node in self.execution_plan.get("nodes", [])
            if isinstance(node, Mapping)
        )


def prepare_task_input(
    *,
    graph: Mapping[str, Any],
    raw_input: Mapping[str, Any],
    execution_plan: Mapping[str, Any],
    jobs: Sequence[Mapping[str, Any]],
    resource_resolver: ResourceSlotResolver | None = None,
    site_selection_resolver: SiteSelectionResolver | None = None,
    require_output_boundary: bool = False,
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
        _bind_material_source_sites(
            snapshot=snapshot, plan=plan, jobs=prepared_jobs,
            resolved_input=resolved, resolver=site_selection_resolver,
        )
        _bind_inventory_requirement_quantities(
            snapshot=snapshot,
            resolved_input=resolved,
        )
        _bind_plan_inputs(
            plan=plan,
            jobs=prepared_jobs,
            input_bindings=validated.input_bindings,
            resolved_input=resolved,
        )
        _freeze_site_selections(
            plan=plan,
            jobs=prepared_jobs,
            resolver=site_selection_resolver,
            resolved_input=resolved,
        )
        ExecutionPlanBuilder.refresh_resource_plan(
            graph=snapshot,
            plan=plan,
            jobs=prepared_jobs,
        )
        output_bindings = flatten_output_bindings(
            graph_nodes=snapshot.get("nodes", []),
            graph_edges=snapshot.get("edges", []),
            output_bindings=validated.output_bindings,
            planned_node_uuids={
                str(node.get("uuid") or "")
                for node in plan.get("nodes", [])
                if isinstance(node, Mapping)
            },
        )
        _add_boundary_jobs(
            snapshot=snapshot,
            plan=plan,
            jobs=prepared_jobs,
            resolved_input=resolved,
            workflow_io=validated,
            output_bindings=output_bindings,
            require_output_boundary=require_output_boundary,
        )
    except TaskInputError:
        raise
    except WorkflowBoundaryBindingError as exc:
        raise TaskInputError(f"工作流输出绑定无效：{exc}") from exc
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


def _bind_inventory_requirement_quantities(
    *,
    snapshot: dict[str, Any],
    resolved_input: Mapping[str, Any],
) -> None:
    """把创作期数量绑定解析为本次 Task 的冻结库存需求。

    参数：``snapshot`` 是即将持久化的独立图快照；``resolved_input`` 已按工作流
    输入合同补齐默认值并完成类型校验。返回无并原地替换快照需求。异常：绑定
    元数据损坏、数量非数值或为负时抛 ``TaskInputError``；动态数量为零表示本次
    任务不启用该需求，不会伪造零数量预留。
    """

    if "inventory_requirements" not in snapshot:
        return
    raw_requirements = snapshot["inventory_requirements"]
    if not isinstance(raw_requirements, list):
        raise TaskInputError("工作流数量库存需求必须是数组")
    requirements: list[dict[str, Any]] = []
    for raw in raw_requirements:
        if not isinstance(raw, Mapping):
            raise TaskInputError("工作流数量库存需求必须是对象")
        requirement = clone_json(dict(raw))
        metadata = requirement.get("meta_data")
        unilab = metadata.get("unilab") if isinstance(metadata, Mapping) else None
        binding = (
            unilab.get("quantity_binding") if isinstance(unilab, Mapping) else None
        )
        if binding is None:
            requirements.append(requirement)
            continue
        if not isinstance(binding, Mapping):
            raise TaskInputError("工作流数量绑定无效")
        kind = binding.get("kind")
        if kind == "workflow_input":
            parameter = binding.get("parameter")
            if not isinstance(parameter, str) or parameter not in resolved_input:
                raise TaskInputError("工作流数量绑定引用了不存在的输入")
            raw_quantity = resolved_input[parameter]
        elif kind == "literal":
            raw_quantity = binding.get("value")
        else:
            raise TaskInputError("工作流数量绑定类型无效")
        scale = unilab.get("quantity_scale", 1.0)
        if (
            isinstance(raw_quantity, bool)
            or not isinstance(raw_quantity, (int, float))
            or isinstance(scale, bool)
            or not isinstance(scale, (int, float))
        ):
            raise TaskInputError("工作流数量必须是有限非负数")
        quantity = float(raw_quantity) * float(scale)
        if not math.isfinite(quantity) or quantity < 0 or float(scale) <= 0:
            raise TaskInputError("工作流数量必须是有限非负数")
        if quantity == 0:
            continue
        requirement["required_quantity"] = quantity
        requirements.append(requirement)
    snapshot["inventory_requirements"] = requirements


def _add_boundary_jobs(
    *,
    snapshot: Mapping[str, Any],
    plan: dict[str, Any],
    jobs: list[dict[str, Any]],
    resolved_input: Mapping[str, Any],
    workflow_io: ValidatedWorkflowIO,
    output_bindings: Mapping[str, Mapping[str, Any]],
    require_output_boundary: bool = False,
) -> None:
    """为工作流输入/输出合同编译正式纯数据边界节点作业。

    参数：冻结图、执行计划、作业、规范输入与已验证 I/O 合同属于同一修订；本
    函数原地增加至多一个输入和一个输出节点；需要持久结果时空输出合同也建立
    正式边界，等待其他 Job 全部成功后产生真实空结果。返回无。异常：工作流身份非法时
    由 UUID 解析抛出并使任务创建整体零写入。
    """

    namespace = UUID(str(snapshot["workflow"]["uuid"]))
    input_parameters = workflow_io.input_contract.to_dict()["parameters"]
    output_descriptors = workflow_io.output_contract.to_dict()["outputs"]
    input_node_uuid = str(uuid5(namespace, "workflow-input-boundary"))
    output_node_uuid = str(uuid5(namespace, "workflow-output-boundary"))
    existing_nodes = list(plan.get("nodes", []))
    existing_jobs = list(jobs)
    offset = 1 if input_parameters else 0
    for index, node in enumerate(existing_nodes, start=offset):
        node["topological_index"] = index
    for index, job in enumerate(existing_jobs, start=offset):
        job["topological_index"] = index

    input_node: dict[str, Any] | None = None
    input_job: dict[str, Any] | None = None
    if input_parameters:
        input_node = {
            "uuid": input_node_uuid,
            "topological_index": 0,
            "kind": "workflow_input",
            "param": {},
            "execution_policy": {},
            "action_resource_contract": {},
        }
        input_job = {
            "uuid": str(uuid4()),
            "workflow_node_uuid": input_node_uuid,
            "topological_index": 0,
            "executor_kind": "workflow_input",
            "execution_policy": {},
            "execution_timeout_seconds": 0,
            "param": {},
            "status": "succeeded",
            "return_info": clone_json(dict(resolved_input)),
        }

    output_node: dict[str, Any] | None = None
    output_job: dict[str, Any] | None = None
    if output_descriptors or require_output_boundary:
        output_index = len(existing_nodes) + offset
        frozen_output_bindings = {
            name: dict(binding) for name, binding in output_bindings.items()
        }
        output_node = {
            "uuid": output_node_uuid,
            "topological_index": output_index,
            "kind": "workflow_output",
            "param": {},
            "execution_policy": {},
            "action_resource_contract": {},
            "output_bindings": frozen_output_bindings,
        }
        output_job = {
            "uuid": str(uuid4()),
            "workflow_node_uuid": output_node_uuid,
            "topological_index": output_index,
            "executor_kind": "workflow_output",
            "execution_policy": {},
            "execution_timeout_seconds": 0,
            "param": {},
            "status": "pending",
            "return_info": {},
        }
        for output_name, binding in frozen_output_bindings.items():
            if binding["kind"] == "literal":
                if not binding.get("completion_node_uuids"):
                    raise TaskInputError("固定输出缺少组合操作完成边界")
                continue
            source_node_uuid = (
                input_node_uuid
                if binding["kind"] == "workflow_input"
                else str(binding["workflow_node_uuid"])
            )
            plan.setdefault("edges", []).append(
                {
                    "uuid": str(
                        uuid5(
                            namespace,
                            f"workflow-output-dependency:{output_name}",
                        )
                    ),
                    "source_node_uuid": source_node_uuid,
                    "target_node_uuid": output_node_uuid,
                    "source_handle_uuid": "",
                    "target_handle_uuid": "",
                    "dependency_only": True,
                }
            )
        for output_name, binding in frozen_output_bindings.items():
            for completion_node in binding.get("completion_node_uuids", []):
                plan.setdefault("edges", []).append({
                    "uuid": str(uuid5(namespace, f"workflow-output-completion:{output_name}:{completion_node}")),
                    "source_node_uuid": completion_node, "target_node_uuid": output_node_uuid,
                    "source_handle_uuid": "", "target_handle_uuid": "", "dependency_only": True,
                })
    plan["nodes"] = [
        *([input_node] if input_node is not None else []),
        *existing_nodes,
        *([output_node] if output_node is not None else []),
    ]
    jobs[:] = [
        *([input_job] if input_job is not None else []),
        *existing_jobs,
        *([output_job] if output_job is not None else []),
    ]


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
            label = str(parameter.get("title") or name)
            field = f"{label} ({name})" if label != name else name
            raise TaskInputError(f"工作流任务缺少必填输入：{field}")
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
        except Exception as exc:
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


def _bind_material_source_sites(
    *, snapshot: dict[str, Any], plan: dict[str, Any], jobs: list[dict[str, Any]],
    resolved_input: Mapping[str, Any], resolver: SiteSelectionResolver | None,
) -> None:
    """把启动库位冻结到来源准入需求；库存权威随后检查类型并原子预留物料。"""
    plan_nodes = {node["uuid"]: node for node in plan["nodes"]}
    jobs_by_node = {job["workflow_node_uuid"]: job for job in jobs}
    for source in snapshot["nodes"]:
        binding = (source.get("meta_data") or {}).get("unilab", {}).get("material_source_site_binding")
        if binding is None or source["uuid"] not in plan_nodes:
            continue
        parameter = binding.get("parameter")
        reference = (
            resolved_input.get(parameter)
            if isinstance(parameter, str)
            else binding.get("reference")
        )
        if not isinstance(reference, str) or not reference.strip():
            raise TaskInputError(
                f"请选择来源库位：{parameter or binding.get('reference') or ''}"
            )
        if resolver is None:
            raise TaskInputError("启动库位选择缺少库存权威解析器")
        selector = source["param"]
        mount = selector.get("mount")
        if not isinstance(mount, Mapping) or not isinstance(
            mount.get("uuid"), str
        ):
            raise TaskInputError("静态来源库位解析需要已冻结的 mount UUID")
        try:
            resolution = resolver({
                "version": 1, "owner_material_uuid": mount["uuid"],
                "occupant_material_uuid": "", "group_key": "",
                "exact_site_reference": reference, "strategy": "sort_order",
            })
            site_uuids = resolution["site_uuids"]
            if not isinstance(site_uuids, list) or len(site_uuids) != 1:
                raise ValueError("来源库位必须唯一")
            site_uuid = validate_uuid(site_uuids[0])
            owners = resolution.get("owner_material_uuids") or []
            site_owner = str(owners[0]) if len(owners) == 1 and owners[0] else mount["uuid"]
        except Exception as exc:
            raise TaskInputError(
                f"来源库位解析失败：{parameter or 'static'} / {reference}"
            ) from exc
        allowed = selector.get("slot_range")
        if allowed is not None and site_uuid not in allowed:
            raise TaskInputError(f"来源库位不在工作流允许范围：{reference}")
        node = plan_nodes[source["uuid"]]
        requirements = node.get("material_requirements", [])
        if len(requirements) != 1:
            raise TaskInputError("启动库位来源必须有且只有一个物料需求")
        requirements[0].update(site_uuid=site_uuid, slot_uuids=[])
        # 启动库位是上层物料的地址（台面位号、料架层）时，库位在载架上：挂载物料一并换成载架。
        if site_owner != mount["uuid"]:
            requirements[0]["mount_uuid"] = site_owner
        # 应用图保持不变；任务快照与计划使用同一具体库位，供数量预留和来源准入消费。
        for target in (selector, node["param"], jobs_by_node[source["uuid"]]["param"]):
            target.update(site=site_uuid, slot_range=None)
            if site_owner != mount["uuid"] and isinstance(target.get("mount"), Mapping):
                target["mount"] = {**target["mount"], "uuid": site_owner}
        source["meta_data"]["unilab"].pop("material_source_site_binding")


def _bind_plan_inputs(
    *,
    plan: dict[str, Any],
    jobs: list[dict[str, Any]],
    input_bindings: Mapping[str, Mapping[str, Mapping[str, str]]],
    resolved_input: Mapping[str, Any],
) -> None:
    """把已解析输入绑定到计划节点与已存在的首次作业。

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
    group_provider_by_handle = {
        str(selector.get("handle_uuid") or "")
        for node in plan_nodes.values()
        for selector in (
            node.get("site_selectors")
            if isinstance(node.get("site_selectors"), list)
            else []
        )
        if isinstance(selector, Mapping) and selector.get("group_key")
    }
    carry_provider_by_handle = {
        str(handle_uuid)
        for node in plan_nodes.values()
        for handle_uuid in (
            node.get("carry_bindings")
            if isinstance(node.get("carry_bindings"), Mapping)
            else {}
        )
    }
    for handle_uuid, handle in plan_handles.items():
        if handle.get("io_type") != "target":
            continue
        node_uuid = str(handle.get("node_uuid") or "")
        node = plan_nodes.get(node_uuid)
        if node is None:
            raise TaskInputError("计划连接点未归属唯一活动作业")
        job = jobs_by_node.get(node_uuid)
        # RepeatUntil 的后代节点是“每轮作业模板”，不会在 Task 创建阶段生成
        # 首轮 Job；它们的冻结参数会在调度器物化每一轮时复制。普通节点仍必须
        # 在同一事务中拥有唯一 Job，不能用该例外掩盖计划损坏。
        deferred_job_template = job is None and _is_repeat_template_node(
            node_uuid,
            plan_nodes=plan_nodes,
        )
        if job is None and not deferred_job_template:
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
        if not isinstance(node_param, dict):
            raise TaskInputError("计划节点参数不是对象")
        job_param = job.get("param") if job is not None else None
        if job is not None and not isinstance(job_param, dict):
            raise TaskInputError("计划作业参数不是对象")
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
            int(static_provider)
            + len(incoming_edges)
            + int(binding is not None)
            + int(handle_uuid in group_provider_by_handle)
            + int(handle_uuid in carry_provider_by_handle)
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
        if isinstance(job_param, dict):
            job_param[data_key] = clone_json(resolved_input[parameter])


def _is_repeat_template_node(
    node_uuid: str,
    *,
    plan_nodes: Mapping[str, Mapping[str, Any]],
) -> bool:
    """判断计划节点是否属于 RepeatUntil 的动态作业模板。

    参数：``node_uuid`` 是待判断节点身份，``plan_nodes`` 是同一执行计划节点索引。
    返回：节点存在 RepeatUntil 祖先且自身不是控制区域时为真。异常：父节点缺失或
    父子关系成环时抛 ``TaskInputError``，避免把损坏的计划当作可延迟作业。
    """

    template_node = plan_nodes.get(node_uuid)
    if template_node is None:
        raise TaskInputError("计划节点父子关系引用未知节点")
    template_kind = str(template_node.get("kind") or "")
    current = node_uuid
    visited: set[str] = set()
    while current not in visited:
        visited.add(current)
        node = plan_nodes.get(current)
        if node is None:
            raise TaskInputError("计划节点父子关系引用未知节点")
        parent_uuid = node.get("parent_uuid")
        if not isinstance(parent_uuid, str) or not parent_uuid:
            return False
        parent = plan_nodes.get(parent_uuid)
        if parent is None:
            raise TaskInputError("计划节点父子关系引用未知父节点")
        if str(parent.get("kind") or "") == "repeat_until":
            return template_kind not in {
                "condition",
                "repeat_until",
            }
        current = parent_uuid
    raise TaskInputError("计划节点父子关系包含环")


def _record_deferred_site_group(
    *,
    node: dict[str, Any],
    job: dict[str, Any] | None,
    parameter: str,
    owner_parameter: str,
    group_key: str,
) -> None:
    """所属物料尚未选出时，只把命名库位组键记到执行策略。

    参数：``node`` / ``job`` 是计划节点和对应作业；``parameter`` 是目标孔参数名；
    ``owner_parameter`` 是所属物料参数名；``group_key`` 是作者写下的组键。返回：
    无。异常：执行策略不是对象时抛 ``TaskInputError``。
    """

    marker = {
        "version": 1,
        "parameter": parameter,
        "owner_parameter": owner_parameter,
        "group_key": group_key,
        "strategy": "sort_order",
    }
    policy = node.get("execution_policy")
    if not isinstance(policy, dict):
        raise TaskInputError("计划库位选择节点执行策略不是对象")
    existing = policy.get("deferred_site_group")
    if existing is not None and existing != marker:
        raise TaskInputError("运行时库位组与既有执行策略冲突")
    policy["deferred_site_group"] = marker
    if job is None:
        return
    job_policy = job.get("execution_policy")
    if not isinstance(job_policy, dict):
        raise TaskInputError("计划库位选择作业执行策略不是对象")
    job_policy["deferred_site_group"] = clone_json(marker)


def _freeze_site_selections(
    *,
    plan: dict[str, Any],
    jobs: list[dict[str, Any]],
    resolver: SiteSelectionResolver | None,
    resolved_input: Mapping[str, Any],
) -> None:
    """把节点库位选择声明解析成 Task 代际冻结的具体候选 UUID。

    参数：``plan`` 与 ``jobs`` 已完成工作流输入绑定；``resolver`` 是库存权威的
    只读任务准入端口。返回：原位写入计划节点与已存在 Job 的冻结执行策略。异常：
    选择器、父资源或库存回执不完整时抛 ``TaskInputError``，调用方不得创建任务。
    """

    plan_nodes = _indexed_objects(plan.get("nodes"), key="uuid", label="计划节点")
    jobs_by_node = _indexed_objects(
        jobs,
        key="workflow_node_uuid",
        label="计划作业节点",
    )
    for node_uuid, node in plan_nodes.items():
        raw_selectors = node.get("site_selectors")
        if raw_selectors is None:
            continue
        if not isinstance(raw_selectors, list) or any(
            not isinstance(item, Mapping) for item in raw_selectors
        ):
            raise TaskInputError("计划库位选择器必须是对象列表")
        job = jobs_by_node.get(node_uuid)
        deferred_job_template = job is None and _is_repeat_template_node(
            node_uuid,
            plan_nodes=plan_nodes,
        )
        if job is None and not deferred_job_template:
            raise TaskInputError("计划库位选择器未归属唯一活动作业")
        node_param = node.get("param")
        if not isinstance(node_param, dict):
            raise TaskInputError("计划库位选择器节点参数不是对象")
        job_param = job.get("param") if job is not None else None
        if job is not None and not isinstance(job_param, dict):
            raise TaskInputError("计划库位选择器作业参数不是对象")
        for raw_selector in raw_selectors:
            parameter = str(raw_selector.get("parameter") or "").strip()
            owner_parameter = str(raw_selector.get("owner_parameter") or "").strip()
            group_key = str(raw_selector.get("group_key") or "").strip()
            if not parameter or not owner_parameter:
                raise TaskInputError("计划库位选择器字段不完整")
            raw_owner = node_param.get(owner_parameter)
            # 所属物料还没有具体 UUID（运行时物料来源尚未选出盒子）时，不能在提交
            # 时把库位组冻成某一只盒子的孔。命名组只记下组键，派发拿到所属物料后
            # 再按 sort_order 选第一个能放的孔。作者写了具体孔名时仍把名字留在参数
            # 里。精确孔必须留在库位组约束内，所属物料未定时不能跳过该校验。
            # 已经是具体 UUID 的仍在提交时冻结；有值但不是 UUID 的仍拒绝。
            if raw_owner is None:
                exact_parameter = str(raw_selector.get("exact_parameter") or "").strip()
                if exact_parameter:
                    raise TaskInputError("所属物料尚未确定时不能指定精确库位")
                if group_key:
                    _record_deferred_site_group(
                        node=node,
                        job=job,
                        parameter=parameter,
                        owner_parameter=owner_parameter,
                        group_key=group_key,
                    )
                    node_param.pop(parameter, None)
                    if isinstance(job_param, dict):
                        job_param.pop(parameter, None)
                continue
            if not isinstance(raw_owner, Mapping) or not isinstance(raw_owner.get("uuid"), str):
                raise TaskInputError("目标库位所属资源没有冻结 UUID")
            try:
                owner_material_uuid = validate_uuid(raw_owner["uuid"])
            except (TypeError, ValueError):
                raise TaskInputError("目标库位所属资源 UUID 非法") from None
            raw_occupant = node_param.get(str(raw_selector.get("occupant_parameter") or ""))
            occupant_material_uuid = ""
            if raw_occupant is not None:
                if not isinstance(raw_occupant, Mapping) or not isinstance(
                    raw_occupant.get("uuid"), str
                ):
                    raise TaskInputError("待放物料没有冻结 UUID")
                try:
                    occupant_material_uuid = validate_uuid(raw_occupant["uuid"])
                except (TypeError, ValueError):
                    raise TaskInputError("待放物料 UUID 非法") from None
            exact_parameter = str(raw_selector.get("exact_parameter") or "").strip()
            if exact_parameter:
                if exact_parameter not in resolved_input:
                    raise TaskInputError("命名库位组精确覆盖参数没有解析值")
                exact_reference = resolved_input[exact_parameter]
                if exact_reference not in (None, "") and not isinstance(exact_reference, str):
                    raise TaskInputError("精确库位覆盖参数必须是字符串")
            else:
                exact_reference = node_param.get(parameter)
            if not group_key and exact_reference in (None, ""):
                continue
            if resolver is None:
                raise TaskInputError("工作流任务缺少库位选择权威解析器")
            request = {
                "version": 1,
                "owner_material_uuid": owner_material_uuid,
                "occupant_material_uuid": occupant_material_uuid,
                "group_key": group_key,
                "exact_site_reference": (
                    "" if exact_reference in (None, "") else str(exact_reference)
                ),
                "strategy": "sort_order",
            }
            try:
                resolution = resolver(request)
            except Exception as exc:
                raise TaskInputError("工作流任务库位选择解析失败") from exc
            raw_site_uuids = resolution.get("site_uuids")
            if (
                not isinstance(raw_site_uuids, Sequence)
                or isinstance(raw_site_uuids, (str, bytes))
                or not raw_site_uuids
            ):
                raise TaskInputError("库位选择权威没有返回候选 UUID")
            try:
                site_uuids = [validate_uuid(str(value)) for value in raw_site_uuids]
            except (TypeError, ValueError):
                raise TaskInputError("库位选择权威返回了非法 UUID") from None
            if len(set(site_uuids)) != len(site_uuids):
                raise TaskInputError("库位选择权威返回了重复 UUID")
            # 普通动作与 OS 命令接收一个具体库位 UUID；只有转运动作拥有派发时
            # 从候选组选孔的执行器。不能把普通动作的必填参数删成转运专用策略。
            resource_contract = node.get("action_resource_contract") or {}
            if node.get("kind") in {"device_action", "workflow_command"} and not resource_contract.get("transfer") and not group_key:
                if len(site_uuids) != 1:
                    raise TaskInputError("普通动作库位输入必须选择一个具体库位")
                node_param[parameter] = site_uuids[0]
                if isinstance(job_param, dict):
                    job_param[parameter] = site_uuids[0]
                bindings = node.get("input_bindings")
                if isinstance(bindings, dict):
                    bindings.pop(str(raw_selector.get("handle_uuid") or ""), None)
                continue
            policy = node.get("execution_policy")
            if not isinstance(policy, dict):
                raise TaskInputError("计划库位选择节点执行策略不是对象")
            job_policy = job.get("execution_policy") if job is not None else None
            if job is not None and not isinstance(job_policy, dict):
                raise TaskInputError("计划库位选择作业执行策略不是对象")
            existing = policy.get("target_site_group")
            if existing is not None and list(existing) != site_uuids:
                raise TaskInputError("库位选择与既有执行策略冲突")
            selection = {
                "version": 1,
                "owner_material_uuid": owner_material_uuid,
                "group_key": group_key,
                "requested_reference": request["exact_site_reference"],
                "strategy": "sort_order",
                "site_uuids": site_uuids,
                "fingerprint": str(resolution.get("fingerprint") or ""),
            }
            policy["target_site_group"] = site_uuids
            policy["target_site_selection"] = selection
            if isinstance(job_policy, dict):
                job_policy["target_site_group"] = clone_json(site_uuids)
                job_policy["target_site_selection"] = clone_json(selection)
            # 设备驱动只在派发时接收最终选中的规范库位名；Task 快照不把人类
            # 引用误当成物理动作参数，也避免与冻结候选组形成双选择器。
            node_param.pop(parameter, None)
            if isinstance(job_param, dict):
                job_param.pop(parameter, None)
            selector_handle_uuid = str(raw_selector.get("handle_uuid") or "").strip()
            input_bindings = node.get("input_bindings")
            # 源码字面量库位没有工作流输入绑定，因此计划节点不会携带
            # ``input_bindings``；它已由库存权威冻结成候选 UUID，无需再删除。
            # 只有字段存在但形状损坏时才关闭失败，避免把损坏计划当成字面量。
            if input_bindings is None:
                continue
            if not selector_handle_uuid:
                raise TaskInputError("计划库位选择器缺少可冻结的输入连接点")
            if not isinstance(input_bindings, dict):
                raise TaskInputError("计划库位选择器输入绑定不是对象")
            input_bindings.pop(selector_handle_uuid, None)


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
    "SiteSelectionResolver",
    "TaskInputError",
    "prepare_task_input",
]


def freeze_scope_device_bindings(graph: Mapping[str, Any], resolver: ResourceSlotResolver | None) -> dict[str, Any]:
    """把显式 scope 中的部署设备别名冻结到已知执行器实例，不猜测命名资源。"""
    from copy import deepcopy

    from unilabos.workflow.resource_lock_key import device_lock_key
    result = deepcopy(dict(graph))
    if resolver is None:
        return result
    devices = {str(node.get("material_uuid") or "") for node in result.get("nodes", ())
               if node.get("material_uuid") and node.get("action_name")}
    metadata = result.get("workflow", {}).get("meta_data", {}).get("unilab", {})
    bindings = result.setdefault("resource_bindings", {})
    persisted = metadata.get("resource_bindings", {})
    for scope in [*result.get("resource_scopes", ()), *metadata.get("resource_scopes", ())]:
        for alias in scope.get("resources", ()):
            if not isinstance(alias, str) or alias in bindings or alias in persisted or alias.startswith("/"):
                continue
            material = resolver(alias)
            identity = str((material or {}).get("uuid") or "")
            if identity in devices:
                bindings[alias] = {"instance_uuid": identity, "kind": "device", "canonical_key": device_lock_key(identity)}
    return result
