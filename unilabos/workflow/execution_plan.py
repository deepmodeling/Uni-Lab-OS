"""从应用工作流图构造唯一、不可变的执行计划（ExecutionPlan）。"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any
from uuid import UUID, uuid4, uuid5

from unilabos.workflow._execution_plan_graph import (
    ExecutionPlanBuildError,
    ExecutionPlanGraphNormalizer,
    executor_kind,
    final_target_data_key,
)
from unilabos.workflow.authoring_material_io import (
    MaterialIOAuthoringError,
    _input_binding,
    validate_material_io_node,
)
from unilabos.workflow.execution_resource_policy import (
    ExecutionResourcePolicyError,
    action_device_resource_params,
    merge_action_resource_policy,
    validate_static_device_tenancy_order,
)
from unilabos.workflow.manual_confirmation import (
    normalize_manual_confirmation_config,
)
from unilabos.workflow.resource_lock_key import device_lock_key, material_lock_key
from unilabos.workflow.resource_lock_plan import (
    RESOURCE_PLAN_CAPABILITY,
    RESOURCE_PLAN_VERSION,
    STATIC_RESOURCE_DAG_CAPABILITY,
    ResourcePlan,
    ResourcePlanError,
    bind_station_resource_plan,
    compile_template_resource_plan,
    reservation_sites,
    resource_plan_for_nodes,
    serialize_resource_plan,
)
from unilabos.workflow.store import StoreConflict

PLAN_VERSION = 1
CONTROL_PLAN_VERSION = 2
CONTROL_PLAN_CAPABILITIES = (
    "condition_expression_v1",
    "control_regions_v1",
)
DYNAMIC_ITERATION_CAPABILITY = "dynamic_iteration_jobs_v1"
RUNTIME_MATERIAL_SOURCE_CAPABILITY = "runtime_material_source_v1"



def _serialize_resource_plan(plan: ResourcePlan) -> dict[str, Any]:
    """序列化会复验延迟资源约束，错误须遵守计划层的公共边界。"""
    try:
        return serialize_resource_plan(plan)
    except ResourcePlanError as error:
        raise ExecutionPlanBuildError(
            "invalid_resource_plan", f"{error.message} ({error.path})",
        ) from error


def _reject_cross_branch_data_edges(
    planned_nodes: Sequence[Mapping[str, Any]],
    planned_edges: Sequence[Mapping[str, Any]],
) -> None:
    """拒绝连接同一条件互斥分支的数据边。

    一次运行只执行条件的一条分支，另一条分支的节点从不产出结果；跨分支
    读取在任何路径上都拿不到值（包括物料和动态 mount）。条件分支的
    ``node_uuids`` 已按合同覆盖全部后代，因此嵌套成员也能按祖先分支判定。
    """

    membership: dict[str, dict[str, int]] = defaultdict(dict)
    for node in planned_nodes:
        if node.get("kind") != "condition":
            continue
        region = node.get("control_region")
        branches = region.get("branches") if isinstance(region, Mapping) else None
        for index, branch in enumerate(branches if isinstance(branches, list) else []):
            members = branch.get("node_uuids") if isinstance(branch, Mapping) else None
            for member in members if isinstance(members, list) else []:
                membership[str(member)][str(node["uuid"])] = index
    for edge in planned_edges:
        if edge.get("dependency_only") is True:
            continue
        source = membership.get(str(edge.get("source_node_uuid")), {})
        target = membership.get(str(edge.get("target_node_uuid")), {})
        for condition_uuid, branch_index in target.items():
            if condition_uuid in source and source[condition_uuid] != branch_index:
                raise ExecutionPlanBuildError(
                    "invalid_execution_graph",
                    f"节点 {edge.get('target_node_uuid')} 读取了条件 {condition_uuid} "
                    f"另一条分支中节点 {edge.get('source_node_uuid')} 的结果；"
                    "互斥分支不会同时执行，请改用本分支或汇合前的结果",
                )

class ExecutionPlanBuilder:
    """隐藏拓扑收敛、运行连接点实例化和短期物料需求投影。"""

    @staticmethod
    def refresh_resource_plan(
        *,
        graph: Mapping[str, Any],
        plan: dict[str, Any],
        jobs: list[dict[str, Any]],
    ) -> None:
        """在任务输入绑定后重新投影动作资源计划。

        首次建计划时，工作流输入尚未写入动作参数；含动态 ResourceSlot 的动作
        因此只能先冻结设备和固定资源。输入冻结后必须用同一份执行图重新计算
        资源区间，确保即将派发的动作申请与任务已冻结的资源计划一致。
        """

        planned_nodes = [
            deepcopy(dict(node))
            for node in plan.get("nodes", [])
            if isinstance(node, Mapping)
        ]
        planned_edges = [
            dict(edge)
            for edge in plan.get("edges", [])
            if isinstance(edge, Mapping)
        ]
        resource_plan = ExecutionPlanBuilder._resource_plan(
            graph=graph,
            planned_nodes=planned_nodes,
            planned_edges=planned_edges,
        )
        if resource_plan is None:
            plan.pop("resource_plan", None)
            for node in plan.get("nodes", []):
                if not isinstance(node, dict):
                    continue
                for field in (
                    "resource_plan_id",
                    "resource_interval_ids",
                    "resource_acquire_set_id",
                ):
                    node.pop(field, None)
            for job in jobs:
                for field in (
                    "resource_plan_id",
                    "resource_interval_ids",
                    "resource_acquire_set_id",
                ):
                    job.pop(field, None)
            return

        plan["resource_plan"] = _serialize_resource_plan(resource_plan)
        capabilities = plan.setdefault("capabilities", [])
        if not isinstance(capabilities, list):
            capabilities = []
            plan["capabilities"] = capabilities
        for capability in resource_plan.capabilities:
            if capability not in capabilities:
                capabilities.append(capability)

        projections = resource_plan_for_nodes(
            resource_plan, [str(node.get("uuid") or "") for node in planned_nodes],
        )
        for node in plan.get("nodes", []):
            if not isinstance(node, dict):
                continue
            node_uuid = str(node.get("uuid") or "")
            projection = projections[node_uuid]
            node["resource_plan_id"] = resource_plan.plan_id
            intervals = projection["intervals"]
            acquire_sets = projection["acquire_sets"]
            if intervals:
                node["resource_interval_ids"] = [
                    str(item["interval_id"]) for item in intervals
                ]
            else:
                node.pop("resource_interval_ids", None)
            if acquire_sets:
                node["resource_acquire_set_id"] = str(
                    acquire_sets[0]["acquire_set_id"]
                )
            else:
                node.pop("resource_acquire_set_id", None)

        node_by_uuid = {
            str(node.get("uuid") or ""): node
            for node in plan.get("nodes", [])
            if isinstance(node, Mapping)
        }
        for job in jobs:
            node = node_by_uuid.get(str(job.get("workflow_node_uuid") or ""))
            if node is None:
                continue
            job["resource_plan_id"] = resource_plan.plan_id
            job["resource_interval_ids"] = list(node.get("resource_interval_ids") or [])
            job["resource_acquire_set_id"] = str(
                node.get("resource_acquire_set_id") or ""
            )

    def build(
        self,
        graph: Mapping[str, Any],
        *,
        run_mode: str,
        target_node_uuid: str | None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """构造冻结执行计划和首次作业集合。

        参数：``graph`` 是冻结应用图，``run_mode`` 是任务运行模式，
        ``target_node_uuid`` 是单节点模式目标。返回：不含实时库存、预留、执行
        占用或物理结果的计划与作业。异常：图、物料来源（MaterialSource）或
        单节点选择非法时抛 ``ExecutionPlanBuildError``/``StoreConflict``。
        """

        nodes = ExecutionPlanGraphNormalizer().flatten_control_members(
            self._index(graph.get("nodes"), "nodes")
        )
        templates = self._index(graph.get("node_templates", []), "node_templates")
        handles = self._index(graph.get("handle_templates", []), "handle_templates")
        edges = self._objects(graph.get("edges", []), "edges")
        # ``kinds`` 是每个冻结节点的规范执行责任；虚拟节点不创建作业。
        kinds = {
            node_uuid: self._planned_executor_kind(
                node,
                template=(templates.get(node.get("workflow_node_template_uuid")) or {}),
            )
            for node_uuid, node in nodes.items()
        }
        from unilabos.workflow.workflow_boundary_binding import (
            WorkflowBoundaryBindingError,
            flatten_control_result_bindings,
        )

        try:
            nodes = {
                node["uuid"]: node
                for node in flatten_control_result_bindings(
                    graph_nodes=list(nodes.values()),
                    graph_edges=graph.get("edges", []),
                    handles=list(handles.values()),
                    planned_node_uuids={
                        key
                        for key, kind in kinds.items()
                        # 物料来源节点由协调器承担，但仍是条件分支/循环的合法成员
                        # （v4 运行时物料来源可位于分支内），不能当作"计划外节点"拒绝。
                        if kind not in {"group", "workflow"}
                        and nodes[key].get("disabled") is not True
                    },
                )
            }
        except WorkflowBoundaryBindingError as error:
            raise ExecutionPlanBuildError(
                "composite_boundary_mapping_invalid", str(error)
            ) from error
        self._validate_control_nesting(nodes=nodes, kinds=kinds)
        # ``material_sources`` 是由协调器承担的物料来源解析作业，不进入设备派发图。
        material_sources = {
            node_uuid: node
            for node_uuid, node in nodes.items()
            if node.get("disabled") is not True and kinds[node_uuid] == "material_source"
        }
        # ``active`` 只包含既有本地调度器能够执行的普通节点；物料来源由任务桥
        # 在普通动作之前统一完成任务物料准入（TaskMaterialAdmission）。组合
        # 组合工作流调用（CompositeWorkflowInvocation）仅保留父图层级与边界映射，
        # 其展开内部节点直接归属父工作流任务（WorkflowTask），自身不创建作业。
        active = {
            node_uuid: node
            for node_uuid, node in nodes.items()
            if node.get("disabled") is not True
            and kinds[node_uuid] not in {"group", "material_source", "workflow"}
        }
        disabled_control_regions = {
            node_uuid
            for node_uuid, node in nodes.items()
            if kinds[node_uuid] in {"condition", "repeat_until"} and node.get("disabled") is True
        }
        for active_uuid in active:
            current = active_uuid
            seen: set[str] = set()
            while current not in seen:
                seen.add(current)
                parent = nodes.get(current, {}).get("parent_uuid")
                if parent in disabled_control_regions:
                    raise ExecutionPlanBuildError(
                        "invalid_control_region",
                        "禁用控制区域仍包含启用的分支或循环体节点",
                    )
                if not isinstance(parent, str):
                    break
                current = parent
            else:
                raise ExecutionPlanBuildError("invalid_control_region", "条件区域父子关系包含环")
        # ``planned_graph_nodes`` 同时保留协调责任与普通执行责任，使来源运行连接点
        # 和来源到首消费动作的直连边成为冻结执行计划（ExecutionPlan）事实。
        planned_graph_nodes = {**material_sources, **active}
        graph_normalizer = ExecutionPlanGraphNormalizer()
        flattened_edges, composite_params = graph_normalizer.flatten_composite_edges(
            nodes=nodes,
            edges=edges,
            handles=handles,
        )
        runtime_handles, runtime_handle_ids = graph_normalizer.runtime_handles(
            active=planned_graph_nodes,
            handles=handles,
        )
        planned_edges = graph_normalizer.contract_edges(
            nodes=nodes,
            active=planned_graph_nodes,
            edges=flattened_edges,
            handles=handles,
            runtime_handle_ids=runtime_handle_ids,
        )
        control_edges = self._condition_control_edges(
            nodes=nodes,
            kinds=kinds,
            active_node_uuids=set(planned_graph_nodes),
        )
        repeat_edges = self._repeat_control_edges(
            nodes=nodes,
            kinds=kinds,
            active_node_uuids=set(planned_graph_nodes),
        )
        planned_edges.extend(control_edges)
        planned_edges.extend(repeat_edges)
        for node_uuid, node in planned_graph_nodes.items():
            if kinds[node_uuid] != "material_io":
                continue
            try:
                param = validate_material_io_node(node)
            except MaterialIOAuthoringError as error:
                raise ExecutionPlanBuildError(error.code, error.message) from error
            incoming_material_edges = [
                edge
                for edge in planned_edges
                if edge.get("target_node_uuid") == node_uuid
                and edge.get("target_data_key") == "material"
                and edge.get("dependency_only") is not True
            ]
            if (
                param["mode"] in {"unload", "refill"}
                and param["material"] is None
                and len(incoming_material_edges) != 1
                and not (_input_binding(node, "material") is not None and not incoming_material_edges)
            ):
                raise ExecutionPlanBuildError(
                    "invalid_material_io",
                    f"{param['mode']} 必须有唯一上游 material 边",
                )
            if param["mode"] == "load" and incoming_material_edges:
                raise ExecutionPlanBuildError(
                    "invalid_material_io",
                    "load 不能接收上游 material 边",
                )
        graph_order = graph_normalizer.topological_order(
            planned_graph_nodes,
            planned_edges,
        )
        # 必需的既有父物料已由库存准入；只允许读取其真实回执中的 material
        # 作为必需子来源的 mount。缺料仍由原有库存门禁失败，不变为可补料来源。
        required_child_sources = {
            str(edge["target_node_uuid"])
            for edge in planned_edges
            if edge.get("dependency_only") is not True
            and edge.get("source_type") == "ResourceSlot"
            and edge.get("target_type") == "ResourceSlot"
            and edge.get("source_data_key") == "material"
            and edge.get("target_data_key") == "mount"
            and kinds.get(str(edge.get("source_node_uuid"))) == "material_source"
            and kinds.get(str(edge.get("target_node_uuid"))) == "material_source"
            and all(
                nodes[str(edge[key])].get("param", {}).get("mode") == "existing"
                and nodes[str(edge[key])].get("param", {}).get("supports_replenishment") is not True
                for key in ("source_node_uuid", "target_node_uuid")
            )
        }
        runtime_source_uuids = required_child_sources | {
            node_uuid
            for node_uuid in material_sources
            if self._material_source_runs_at_runtime(
                node_uuid,
                nodes=nodes,
                kinds=kinds,
            )
        }
        # 来源与普通节点都先遵守冻结图拓扑，再由计划作业序列保证全部协调责任先于
        # 任何物理动作；这样不会让无边来源受创建时间影响而落到动作之后。
        ordered_sources = [node_uuid for node_uuid in graph_order if node_uuid in material_sources]
        ordered = [node_uuid for node_uuid in graph_order if node_uuid in active]
        if run_mode == "single_node":
            if target_node_uuid is None:
                if not graph_order:
                    raise StoreConflict("workflow has no enabled nodes")
                target_node_uuid = graph_order[0]
            if target_node_uuid in material_sources:
                ordered_sources = [target_node_uuid]
                ordered = []
            elif target_node_uuid in active:
                ordered_sources = []
                ordered = [target_node_uuid]
            else:
                raise StoreConflict("single_node target is not enabled")
            planned_edges = []
            runtime_handles = [
                handle for handle in runtime_handles if handle["node_uuid"] == target_node_uuid
            ]

        requirements, material_params, material_binding_targets = self._material_source_inputs(
            nodes=nodes,
            active=active,
            kinds=kinds,
            edges=planned_edges,
            topological_order=graph_order,
            included_source_uuids=set(ordered_sources),
            runtime_source_uuids=runtime_source_uuids,
        )

        planned_nodes: list[dict[str, Any]] = []
        jobs: list[dict[str, Any]] = []
        handles_by_node: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for handle in runtime_handles:
            handles_by_node[handle["node_uuid"]].append(handle)
        # 启动来源保持旧排序；运行时来源与普通动作按真实依赖排序展示。
        startup_sources = []
        for source_uuid in ordered_sources:
            source_node = nodes[source_uuid]
            if source_uuid not in runtime_source_uuids:
                startup_sources.append(source_uuid)
        included = set(ordered_sources) | set(ordered)
        planned_order = [*startup_sources, *(
            node_uuid for node_uuid in graph_order
            if node_uuid in included and node_uuid not in startup_sources
        )]
        for index, node_uuid in enumerate(planned_order):
            node = nodes[node_uuid]
            kind = kinds[node_uuid]
            template = templates.get(node.get("workflow_node_template_uuid")) or {}
            raw_policy = node.get("execution_policy") or {}
            if isinstance(raw_policy, Mapping) and "access_region" in raw_policy:
                raise ExecutionPlanBuildError(
                    "invalid_execution_policy",
                    "access_region 由 PLC 保证，工作流不得声明软件锁",
                )
            try:
                resource_contract = (
                    self._frozen_action_resource_contract(template)
                    if kind
                    in {
                        "device_action",
                        "material_transfer",
                        "manual_confirm",
                    }
                    else {}
                )
                self._require_complete_material_transfer_contract(
                    kind=kind,
                    action_name=str(
                        node.get("action_name") or template.get("name") or ""
                    ).strip(),
                    resource_contract=resource_contract,
                )
                policy = merge_action_resource_policy(
                    resource_contract,
                    raw_policy,
                )
            except ExecutionResourcePolicyError as error:
                raise ExecutionPlanBuildError(
                    "invalid_execution_policy",
                    str(error),
                ) from error
            # ``planned_param`` 是任务提交时冻结的动作输入，不是运行时回退视图。
            planned_param = dict(node.get("param") or {})
            if kind == "material_source" and "custody_policy" not in planned_param:
                # 升级前七字段来源按历史安全语义显式迁移为任务全程独占；冻结
                # 计划从此只发布八字段合同，协调器无需猜测旧图版本。
                planned_param["custody_policy"] = "task_exclusive"
            planned_param.update(composite_params.get(node_uuid, {}))
            # ``fixed_params`` 是固定的 ``existing`` 物料来源（MaterialSource）沿物料占位符链投影的实例引用。
            fixed_params = material_params.get(node_uuid, {})
            planned_param.update(fixed_params)
            node_handles = handles_by_node.get(node_uuid, [])
            # Virtual group/composite nodes do not exist in the execution plan.
            # Keep the nearest executable ancestor so nested repeat templates can
            # validate their ownership without referring to a removed invocation.
            planned_parent = node.get("parent_uuid")
            parent_seen: set[str] = set()
            while planned_parent and planned_parent not in planned_graph_nodes:
                if planned_parent in parent_seen or planned_parent not in nodes:
                    raise ExecutionPlanBuildError("invalid_control_region", "计划父级缺失或成环")
                parent_seen.add(planned_parent)
                planned_parent = nodes[planned_parent].get("parent_uuid")
            planned_node: dict[str, Any] = {
                "uuid": node_uuid,
                "parent_uuid": planned_parent,
                "topological_index": index,
                "kind": kind,
                "param": planned_param,
                "execution_policy": policy,
                "action_resource_contract": resource_contract,
                "inputs": [
                    {
                        "handle_uuid": handle["uuid"],
                        "data_key": final_target_data_key(handle["data_key"]),
                        "type": handle["type"],
                        "required": handle["required"],
                    }
                    for handle in node_handles
                    if handle["io_type"] == "target"
                ],
                "source_handle_uuids": [
                    handle["uuid"] for handle in node_handles if handle["io_type"] == "source"
                ],
            }
            node_metadata = node.get("meta_data")
            node_unilab = (
                node_metadata.get("unilab") if isinstance(node_metadata, Mapping) else None
            )
            if isinstance(node_unilab, Mapping):
                planned_node["meta_data"] = {"unilab": deepcopy(dict(node_unilab))}
            for resource_field in (
                "resource_defaults",
                "resources",
                "branch_id",
                "order_sensitive",
                "physical_hold_resources",
            ):
                if resource_field in node:
                    planned_node[resource_field] = deepcopy(node[resource_field])
            result_name = (
                node_unilab.get("authoring_result_name")
                if isinstance(node_unilab, Mapping)
                else None
            )
            if isinstance(result_name, str) and result_name:
                planned_node["result_name"] = result_name
            carry_bindings = (
                node_unilab.get("carry_bindings") if isinstance(node_unilab, Mapping) else None
            )
            if isinstance(carry_bindings, Mapping) and carry_bindings:
                planned_node["carry_bindings"] = {
                    runtime_handle_ids[(node_uuid, str(handle_uuid))]: deepcopy(dict(binding))
                    for handle_uuid, binding in carry_bindings.items()
                    if (node_uuid, str(handle_uuid)) in runtime_handle_ids
                    and isinstance(binding, Mapping)
                }
            input_bindings = (
                node_unilab.get("input_bindings") if isinstance(node_unilab, Mapping) else None
            )
            if isinstance(input_bindings, Mapping) and input_bindings:
                planned_node["input_bindings"] = {
                    runtime_handle_ids[(node_uuid, str(handle_uuid))]: deepcopy(dict(binding))
                    for handle_uuid, binding in input_bindings.items()
                    if (node_uuid, str(handle_uuid)) in runtime_handle_ids
                    and isinstance(binding, Mapping)
                }
            site_group_bindings = (
                node_unilab.get("site_group_bindings") if isinstance(node_unilab, Mapping) else None
            )
            site_selectors: list[dict[str, Any]] = []
            for handle in node_handles:
                selector = handle.get("site_selector")
                if handle.get("io_type") != "target" or not isinstance(selector, Mapping):
                    continue
                owner_parameter = selector.get("owner")
                if not isinstance(owner_parameter, str) or not owner_parameter.strip():
                    raise ExecutionPlanBuildError(
                        "invalid_site_selector",
                        "库位选择器缺少所属资源参数",
                    )
                template_handle_uuid = str(handle.get("template_handle_uuid") or "")
                raw_group = (
                    site_group_bindings.get(template_handle_uuid)
                    if isinstance(site_group_bindings, Mapping)
                    else None
                )
                descriptor: dict[str, Any] = {
                    "version": 1,
                    "handle_uuid": str(handle["uuid"]),
                    "parameter": final_target_data_key(str(handle["data_key"])),
                    "owner_parameter": owner_parameter,
                    "occupant_parameter": str(selector.get("occupant") or ""),
                }
                if raw_group is not None:
                    if (
                        not isinstance(raw_group, Mapping)
                        or raw_group.get("version") != 1
                        or raw_group.get("owner_parameter") != owner_parameter
                        or raw_group.get("strategy") != "sort_order"
                        or not isinstance(raw_group.get("group_key"), str)
                        or not str(raw_group["group_key"]).strip()
                    ):
                        raise ExecutionPlanBuildError(
                            "invalid_site_group_binding",
                            "命名库位组绑定与动作 SiteSelector 合同不一致",
                        )
                    descriptor.update(
                        {
                            "group_key": str(raw_group["group_key"]),
                            "strategy": "sort_order",
                        }
                    )
                    exact_parameter = raw_group.get("exact_parameter")
                    if exact_parameter is not None:
                        if not isinstance(exact_parameter, str) or not exact_parameter.strip():
                            raise ExecutionPlanBuildError(
                                "invalid_site_group_binding",
                                "命名库位组精确覆盖参数无效",
                            )
                        descriptor["exact_parameter"] = exact_parameter
                site_selectors.append(descriptor)
            if site_selectors:
                planned_node["site_selectors"] = site_selectors
            if kind in {"condition", "repeat_until"}:
                planned_node["control_region"] = deepcopy(planned_param)
            if kind == "workflow_command":
                planned_node["action_name"] = str((node_unilab or {}).get("local_command") or "")
                if not planned_node["action_name"]:
                    raise ExecutionPlanBuildError("invalid_local_command", "本地命令缺少固定服务身份")
                if (
                    planned_node["action_name"] == "unilabos.workflow.native_commands:HumanCommands.form"
                    and "fields" in planned_param
                ):
                    from unilabos.workflow.human_form import normalize_form_fields

                    try:
                        normalize_form_fields(planned_param["fields"])
                    except ValueError as error:
                        raise ExecutionPlanBuildError("invalid_human_form", str(error)) from error
            if kind in {"device_action", "material_transfer"}:
                planned_node.update(self._device_action_contract(node, template=template))
                # ``action_contract`` 来自模板投影保留元数据，而 ``template.schema``
                # 只承载 Backend 规范的 Goal 参数子模式。
                action_contract = self._frozen_action_contract(
                    template,
                    node_uuid=node_uuid,
                )
                planned_node["param_schema"] = action_contract
            elif kind == "manual_confirm":
                if not self._has_fixed_executor_binding(node):
                    raise ExecutionPlanBuildError(
                        "invalid_executor_binding",
                        "人工确认节点必须绑定一个真实设备动作",
                    )
                planned_node.update(self._device_action_contract(node, template=template))
                planned_node["param_schema"] = self._frozen_action_contract(
                    template,
                    node_uuid=node_uuid,
                )
                try:
                    planned_node["manual_confirmation"] = normalize_manual_confirmation_config(
                        node.get("manual_confirmation")
                    )
                except StoreConflict as error:
                    raise ExecutionPlanBuildError(
                        "invalid_manual_confirmation",
                        str(error),
                    ) from error
            elif kind == "material_io":
                try:
                    planned_node["manual_confirmation"] = (
                        normalize_manual_confirmation_config(
                            {
                                "timeout_seconds": planned_param.get(
                                    "timeout_seconds"
                                )
                            }
                        )
                    )
                except StoreConflict as error:
                    raise ExecutionPlanBuildError(
                        "invalid_material_io",
                        str(error),
                    ) from error
            if node.get("material_uuid") is not None:
                planned_node["material_uuid"] = node["material_uuid"]
            if node.get("script") is not None:
                planned_node["script"] = node["script"]
            if (
                kind
                not in {
                    "device_action",
                    "material_transfer",
                    "manual_confirm",
                }
                and template.get("schema") is not None
            ):
                planned_node["param_schema"] = self._frozen_param_schema(
                    template,
                    node_uuid=node_uuid,
                )
            if requirements.get(node_uuid):
                planned_node["material_requirements"] = requirements[node_uuid]
            if material_binding_targets.get(node_uuid):
                planned_node["material_binding_targets"] = material_binding_targets[node_uuid]
            if kind == "material_source":
                runtime_source = node_uuid in runtime_source_uuids
                if runtime_source:
                    if self._has_repeat_ancestor(node_uuid, nodes=nodes, kinds=kinds):
                        raise ExecutionPlanBuildError(
                            "runtime_material_source_in_repeat_unsupported",
                            "运行时物料来源暂不支持放在循环体内；请在循环外选料后传入物料",
                        )
                    planned_node["runtime_material_source"] = True
                if (
                    planned_param.get("mount") is None
                    and not self._has_fixed_material_identity(planned_param)
                ):
                    if node_uuid not in required_child_sources and not self._dynamic_material_source_mount_allowed(
                        node_uuid,
                        nodes=nodes,
                        kinds=kinds,
                    ):
                        raise ExecutionPlanBuildError(
                            "dynamic_material_source_mount_requires_runtime",
                            "动态 mount 只允许 supports_replenishment 或条件区域中的运行时 material_source",
                        )
                    mount_edges = [
                        edge
                        for edge in planned_edges
                        if edge.get("target_node_uuid") == node_uuid
                        and edge.get("target_data_key") == "mount"
                        and edge.get("target_type") == "ResourceSlot"
                        and edge.get("dependency_only") is not True
                    ]
                    if len(mount_edges) != 1:
                        raise ExecutionPlanBuildError(
                            "invalid_material_source_mount_binding",
                            "动态 material_source mount 必须由唯一 Material 数据边提供",
                        )
                    mount_edge = mount_edges[0]
                    planned_node["runtime_mount_binding"] = {
                        "source_node_uuid": str(
                            mount_edge["source_node_uuid"]
                        ),
                        "source_data_key": final_target_data_key(
                            str(mount_edge.get("source_data_key") or "material")
                        ),
                    }
            planned_nodes.append(planned_node)
            if not self._has_repeat_ancestor(
                node_uuid,
                nodes=nodes,
                kinds=kinds,
            ):
                jobs.append(
                    {
                        "uuid": str(uuid4()),
                        "workflow_node_uuid": node_uuid,
                        "topological_index": index,
                        "executor_kind": kind,
                        "execution_policy": policy,
                        "execution_timeout_seconds": (
                            int(planned_param["timeout_seconds"])
                            if kind == "material_io"
                            else 0
                        ),
                        "param": planned_param,
                    }
                )
        _reject_cross_branch_data_edges(planned_nodes, planned_edges)
        try:
            validate_static_device_tenancy_order(planned_nodes)
        except ExecutionResourcePolicyError as error:
            raise ExecutionPlanBuildError(
                "static_resource_deadlock",
                str(error),
            ) from error
        resource_plan = self._resource_plan(
            graph=graph,
            planned_nodes=planned_nodes,
            planned_edges=planned_edges,
        )
        has_repeat_regions = any(kinds[node_uuid] == "repeat_until" for node_uuid in active)
        has_control_regions = bool(control_edges or repeat_edges)
        plan: dict[str, Any] = {
            "version": CONTROL_PLAN_VERSION if has_control_regions else PLAN_VERSION,
            "run_mode": run_mode,
            "nodes": planned_nodes,
            "edges": planned_edges,
            "handles": runtime_handles,
        }
        if has_control_regions:
            plan["capabilities"] = list(CONTROL_PLAN_CAPABILITIES)
            if has_repeat_regions:
                plan["capabilities"].append(DYNAMIC_ITERATION_CAPABILITY)
        if any(node.get("runtime_material_source") for node in planned_nodes):
            from unilabos.workflow.runtime_material_source import validate_replenishment_plan
            validate_replenishment_plan(planned_nodes)
            plan["version"] = CONTROL_PLAN_VERSION
            capabilities = plan.setdefault("capabilities", list(CONTROL_PLAN_CAPABILITIES))
            capabilities.append(RUNTIME_MATERIAL_SOURCE_CAPABILITY)
        if resource_plan is not None:
            plan["resource_plan"] = _serialize_resource_plan(resource_plan)
            if reservation_sites(resource_plan) or any(
                node.get("kind") == "material_source"
                or (node.get("action_resource_contract") or {}).get("transfer")
                or (node.get("action_resource_contract") or {}).get("operate_in_place")
                for node in planned_nodes
            ):
                plan["inventory_resource_binding"] = "pending"
            capabilities = plan.setdefault("capabilities", [])
            for capability in resource_plan.capabilities:
                if capability not in capabilities:
                    capabilities.append(capability)
            projections = resource_plan_for_nodes(resource_plan, [str(node["uuid"]) for node in planned_nodes])
            for node in planned_nodes:
                node["resource_plan_id"] = resource_plan.plan_id
                projection = projections[str(node["uuid"])]
                if projection["intervals"]:
                    node["resource_interval_ids"] = [
                        str(item["interval_id"]) for item in projection["intervals"]
                    ]
                if projection["acquire_sets"]:
                    node["resource_acquire_set_id"] = str(
                        projection["acquire_sets"][0]["acquire_set_id"]
                    )
            planned_by_uuid = {str(node["uuid"]): node for node in planned_nodes}
            for job in jobs:
                node = planned_by_uuid.get(str(job.get("workflow_node_uuid") or ""))
                if node is None:
                    continue
                job["resource_plan_id"] = resource_plan.plan_id
                job["resource_interval_ids"] = list(node.get("resource_interval_ids") or [])
                job["resource_acquire_set_id"] = str(node.get("resource_acquire_set_id") or "")
        if target_node_uuid is not None:
            plan["target_node_uuid"] = target_node_uuid
        return plan, jobs

    @staticmethod
    def _resource_plan(
        *,
        graph: Mapping[str, Any],
        planned_nodes: Sequence[Mapping[str, Any]],
        planned_edges: Sequence[Mapping[str, Any]],
        allow_deferred_inventory: bool = True,
    ) -> Any:
        """按需构造资源计划；没有资源声明的旧图保持原有计划形状。"""

        from unilabos.registry.material_lock_schema import CompiledMaterialLockSchema, compile_material_lock_schema

        # 只复用同一资源计划中内容相同的规则。逐个动作的实际参数仍须校验。
        compiled_schemas: dict[str, CompiledMaterialLockSchema] = {}
        resource_graph = deepcopy(graph)
        resource_graph["nodes"] = [deepcopy(dict(node)) for node in planned_nodes]
        workflow = graph.get("workflow")
        workflow_meta = workflow.get("meta_data") if isinstance(workflow, Mapping) else None
        unilab_meta = workflow_meta.get("unilab") if isinstance(workflow_meta, Mapping) else None
        persisted_bindings = (
            unilab_meta.get("resource_bindings") if isinstance(unilab_meta, Mapping) else None
        )
        if persisted_bindings is not None and not isinstance(persisted_bindings, Mapping):
            raise ExecutionPlanBuildError(
                "invalid_resource_plan",
                "Workflow resource_bindings 必须是对象",
            )
        graph_bindings = graph.get("resource_bindings")
        if graph_bindings is not None and not isinstance(graph_bindings, Mapping):
            raise ExecutionPlanBuildError(
                "invalid_resource_plan",
                "resource_bindings 必须是对象",
            )
        bindings = {
            **dict(persisted_bindings or {}),
            **dict(graph_bindings or {}),
        }
        material_source_binding_targets = {
            (
                str(target.get("workflow_node_uuid") or ""),
                str(target.get("param_key") or ""),
            )
            for source in resource_graph["nodes"]
            for target in source.get("material_binding_targets", ())
            if isinstance(target, Mapping)
        }
        explicit_bindings = set(bindings)
        has_deferred_material_binding = False
        # 搬运端点库位只能由库存解析；首次编译保留模板，准入后用已解析步骤重建。
        has_deferred_transfer_binding = any(
            (node.get("action_resource_contract") or {}).get("transfer_step")
            and not node.get("transfer_step")
            for node in resource_graph["nodes"]
        )
        for node in resource_graph["nodes"]:
            # 动作参数默认只在本次调用内命名。显式绑定沿用工作流名称。
            parameter_aliases: dict[str, str] = {}
            node["resource_parameter_aliases"] = parameter_aliases
            material_uuid = str(node.get("material_uuid") or "")
            if material_uuid and node.get("kind") in {
                "device_action",
                "material_transfer",
                "manual_confirm",
            }:
                alias = f"device:{material_uuid}"
                defaults = list(node.get("resource_defaults") or [])
                defaults.append(alias)
                bindings[alias] = {
                    "instance_uuid": material_uuid,
                    "kind": "device",
                    "canonical_key": device_lock_key(material_uuid),
                }
                # 根/词法声明可以复用已冻结的设备别名，不访问可变运行态。
                local_id = str(node.get("device_id") or "")
                if local_id:
                    bindings.setdefault(local_id, bindings[alias])
                node["resource_defaults"] = list(dict.fromkeys(defaults))
            material_instances = {
                str(req["instance_uuid"])
                for req in node.get("material_requirements", ())
                if isinstance(req, Mapping) and req.get("instance_uuid")
            }
            schema = node.get("param_schema")
            params = node.get("param") or {}
            contract = node.get("action_resource_contract") or {}
            contract_material_instances: set[str] = set()
            for name in action_device_resource_params(contract):
                value = params.get(name)
                if value is None:
                    continue  # 未来输入仍须有显式绑定，派发前再次按最终参数校验。
                raw_uuid = (
                    value.get("uuid") or value.get("material_uuid")
                    if isinstance(value, Mapping)
                    else value
                )
                try:
                    instance_uuid = str(UUID(str(raw_uuid)))
                except (ValueError, TypeError) as error:
                    raise ExecutionPlanBuildError(
                        "invalid_resource_plan", f"设备资源参数 {name} 缺少规范实例 UUID"
                    ) from error
                expected = {
                    "instance_uuid": instance_uuid,
                    "kind": "device",
                    "canonical_key": device_lock_key(instance_uuid),
                }
                alias = name if name in explicit_bindings else f"node:{node['uuid']}:param:{name}"
                supplied = bindings.get(alias)
                if supplied is not None:
                    # 复用计划绑定器规范化角色和实例键，避免建立第二套身份规则。
                    probe = compile_template_resource_plan(
                        {"nodes": [{"uuid": "binding", "resources": [name]}]}
                    )
                    try:
                        actual = (
                            bind_station_resource_plan(probe, {name: supplied})
                            .resources[0]
                            .canonical_key
                        )
                    except ResourcePlanError as error:
                        raise ExecutionPlanBuildError(
                            "invalid_resource_plan", error.message
                        ) from error
                    if actual != expected["canonical_key"]:
                        raise ExecutionPlanBuildError(
                            "invalid_resource_plan", f"资源绑定 {name} 与冻结动作参数实例不一致"
                        )
                bindings[alias] = expected
                parameter_aliases[name] = alias
                node["resource_defaults"] = list(
                    dict.fromkeys([*node.get("resource_defaults", ()), alias])
                )
            for name in _action_material_resource_params(contract):
                alias = name if name in explicit_bindings else f"node:{node['uuid']}:param:{name}"
                parameter_aliases[name] = alias
                value = params.get(name)
                if value is None:
                    if (str(node.get("uuid") or ""), name) in material_source_binding_targets:
                        # 自动 MaterialSource 的具体 UUID 只会在库存准入后写入。
                        # 此时保留符号资源计划，随后由 bind_inventory_resource_plan
                        # 用同一构建器绑定并校验真实物料互斥键。
                        has_deferred_material_binding = True
                    continue
                raw_uuid = (
                    value.get("uuid") or value.get("material_uuid")
                    if isinstance(value, Mapping)
                    else value
                )
                try:
                    instance_uuid = str(UUID(str(raw_uuid)))
                except (ValueError, TypeError) as error:
                    raise ExecutionPlanBuildError(
                        "invalid_resource_plan", f"物料资源参数 {name} 缺少规范实例 UUID"
                    ) from error
                expected = {
                    "instance_uuid": instance_uuid,
                    "kind": "material",
                    "canonical_key": material_lock_key(instance_uuid),
                }
                supplied = bindings.get(alias)
                if supplied is not None:
                    probe = compile_template_resource_plan(
                        {"nodes": [{"uuid": "binding", "resources": [name]}]}
                    )
                    try:
                        actual = (
                            bind_station_resource_plan(probe, {name: supplied})
                            .resources[0]
                            .canonical_key
                        )
                    except ResourcePlanError as error:
                        raise ExecutionPlanBuildError(
                            "invalid_resource_plan", error.message
                        ) from error
                    if actual != expected["canonical_key"]:
                        raise ExecutionPlanBuildError(
                            "invalid_resource_plan", f"资源绑定 {name} 与冻结动作参数实例不一致"
                        )
                bindings[alias] = expected
                contract_material_instances.add(instance_uuid)
                node["resource_defaults"] = list(
                    dict.fromkeys([*node.get("resource_defaults", ()), alias])
                )
            if isinstance(schema, Mapping):
                goal = (schema.get("properties") or {}).get("goal") or {}
                properties = goal.get("properties") or {}
                deferred_site_parameters = {
                    selector["parameter"]
                    for selector in node.get("site_selectors", ())
                    if isinstance(selector, Mapping) and selector.get("parameter")
                }
                # 搬运两端的所属物件（source/target owner）只用来定位端点库位：端点由库位锁保护，
                # 不把整个载架/仓库当成被操作的物料独占，否则载架上其他库位里正被占用的物料会挡住搬运。
                transfer = contract.get("transfer") if isinstance(contract, Mapping) else None
                owner_parameters = {
                    str(transfer.get(field)) for field in ("source_owner_param", "target_owner_param")
                    if isinstance(transfer, Mapping) and transfer.get(field)
                }
                # 库位选择器由库存权威解析；其余静态字段沿用逐字段校验语义。
                static_params = [(key, value) for key, value in params.items()
                                 if key in properties and key not in deferred_site_parameters
                                 and key not in owner_parameters]
                if static_params:
                    partial_goal = {
                        "type": "object",
                        "properties": properties,
                        **({"$defs": goal["$defs"]} if "$defs" in goal else {}),
                        **({"definitions": goal["definitions"]} if "definitions" in goal else {}),
                    }
                    partial_schema = {"properties": {"goal": partial_goal}}
                    # 包含完整约束、引用与锁标记；不按动作名或设备身份猜测等价性。
                    schema_key = json.dumps(partial_schema, sort_keys=True, separators=(",", ":"))
                    compiled = compiled_schemas.get(schema_key)
                    if compiled is None:
                        compiled = compile_material_lock_schema(partial_schema)
                        compiled_schemas[schema_key] = compiled
                    for key, value in static_params:
                        material_instances.update(compiled.material_lock_uuids({key: value}))
            for instance_uuid in sorted(material_instances):
                if instance_uuid in contract_material_instances:
                    # material 角色参数已经用其合同参数名表示同一物理互斥资源；
                    # 不再创建 synthetic alias，避免一次动作产生两个同义资源。
                    continue
                alias = f"material:{instance_uuid}"
                bindings[alias] = {
                    "instance_uuid": instance_uuid,
                    "kind": "material",
                    "canonical_key": material_lock_key(instance_uuid),
                }
                node["resource_defaults"] = list(
                    dict.fromkeys([*node.get("resource_defaults", ()), alias])
                )
        if not _has_resource_declarations(resource_graph, resource_graph["nodes"]):
            return None
        resource_graph["edges"] = [dict(edge) for edge in planned_edges]
        _project_resource_scopes_to_planned_nodes(
            resource_graph,
            planned_node_uuids={str(node["uuid"]) for node in planned_nodes},
        )
        try:
            raw_bindings = bindings or graph.get("resource_bindings")
            template_plan = compile_template_resource_plan(
                resource_graph, _defer_cycle_validation=raw_bindings is not None,
                _defer_transfer_binding=has_deferred_transfer_binding,
            )
            has_site_reservations = bool(reservation_sites(template_plan))
            if allow_deferred_inventory and (
                has_site_reservations or has_deferred_material_binding or has_deferred_transfer_binding
            ):
                return template_plan
            has_named_scope_resources = any(
                scope.kind in {"root", "with"} and scope.resource_ids
                for scope in template_plan.scopes
            )
            if raw_bindings is None and not has_named_scope_resources and not has_site_reservations:
                return template_plan
            raw_concurrency = graph.get("concurrent_resource_plans") or ()
            if not isinstance(raw_concurrency, Sequence) or isinstance(
                raw_concurrency, (str, bytes)
            ):
                raise ResourcePlanError(
                    "invalid_concurrency",
                    "concurrent_resource_plans 必须是资源计划数组",
                )
            return bind_station_resource_plan(
                template_plan,
                raw_bindings or {},
                concurrency=raw_concurrency,
            )
        except ResourcePlanError as error:
            raise ExecutionPlanBuildError(
                "invalid_resource_plan",
                f"{error.message} ({error.path})",
            ) from error

    @staticmethod
    def _validate_control_nesting(
        *,
        nodes: Mapping[str, Mapping[str, Any]],
        kinds: Mapping[str, str],
    ) -> None:
        """统一限制 condition/repeat_until 混合控制区域的最大深度。"""

        control_kinds = {"condition", "repeat_until"}
        for node_uuid, kind in kinds.items():
            if kind not in control_kinds or nodes[node_uuid].get("disabled") is True:
                continue
            depth = 1
            current = node_uuid
            seen: set[str] = set()
            while current not in seen:
                seen.add(current)
                parent = nodes.get(current, {}).get("parent_uuid")
                if not isinstance(parent, str):
                    break
                if kinds.get(parent) in control_kinds:
                    depth += 1
                current = parent
            else:
                raise ExecutionPlanBuildError(
                    "invalid_control_region", "控制区域父子关系包含环"
                )
            if depth > 8:
                raise ExecutionPlanBuildError(
                    "control_nesting_too_deep", "控制区域嵌套深度不能超过 8"
                )

    @staticmethod
    def _has_repeat_ancestor(
        node_uuid: str,
        *,
        nodes: Mapping[str, Mapping[str, Any]],
        kinds: Mapping[str, str],
    ) -> bool:
        """判断冻结节点是否属于任意 RepeatUntil 模板体。"""

        current = node_uuid
        seen: set[str] = set()
        while current not in seen:
            seen.add(current)
            parent = nodes.get(current, {}).get("parent_uuid")
            if not isinstance(parent, str):
                return False
            if kinds.get(parent) == "repeat_until":
                return True
            current = parent
        raise ExecutionPlanBuildError(
            "invalid_control_region", "控制区域父子关系包含环"
        )

    @staticmethod
    def _material_source_runs_at_runtime(
        node_uuid: str,
        *,
        nodes: Mapping[str, Mapping[str, Any]],
        kinds: Mapping[str, str],
    ) -> bool:
        """判断来源是否由运行中控制流触发，而非任务创建时预准入。"""

        node = nodes[node_uuid]
        if (node.get("param") or {}).get("supports_replenishment") is True:
            return True
        parent = node.get("parent_uuid")
        seen: set[str] = set()
        while isinstance(parent, str) and parent in nodes and parent not in seen:
            seen.add(parent)
            if kinds.get(parent) in {"condition", "repeat_until"}:
                return True
            parent = nodes[parent].get("parent_uuid")
        return False

    @staticmethod
    def _has_fixed_material_identity(selector: Mapping[str, Any]) -> bool:
        """已冻结具体物料 UUID 的来源不需要运行时动态 mount。"""

        return bool(str(selector.get("material_uuid") or "").strip())

    @staticmethod
    def _dynamic_material_source_mount_allowed(
        node_uuid: str,
        *,
        nodes: Mapping[str, Mapping[str, Any]],
        kinds: Mapping[str, str],
    ) -> bool:
        """动态 mount 仅允许补料来源或条件分支来源。"""

        node = nodes[node_uuid]
        if (node.get("param") or {}).get("supports_replenishment") is True:
            return True
        parent = node.get("parent_uuid")
        seen: set[str] = set()
        while isinstance(parent, str) and parent in nodes and parent not in seen:
            seen.add(parent)
            if kinds.get(parent) == "condition":
                return True
            parent = nodes[parent].get("parent_uuid")
        return False

    @staticmethod
    def _repeat_control_edges(
        *,
        nodes: Mapping[str, Mapping[str, Any]],
        kinds: Mapping[str, str],
        active_node_uuids: set[str],
    ) -> list[dict[str, Any]]:
        """校验 RepeatUntil 冻结模板并投影无数据的区域边界依赖。"""

        result: list[dict[str, Any]] = []
        seen_pairs: set[tuple[str, str]] = set()
        repeat_uuids = {
            node_uuid
            for node_uuid in active_node_uuids
            if kinds.get(node_uuid) == "repeat_until"
        }

        def is_descendant(node_uuid: str, region_uuid: str) -> bool:
            """只沿冻结 parent_uuid 证明循环模板成员关系。"""

            current = node_uuid
            seen: set[str] = set()
            while current not in seen:
                seen.add(current)
                parent = nodes.get(current, {}).get("parent_uuid")
                if parent == region_uuid:
                    return True
                if not isinstance(parent, str):
                    return False
                current = parent
            raise ExecutionPlanBuildError(
                "invalid_control_region", "循环区域父子关系包含环"
            )

        def add_edge(
            region_uuid: str, source_uuid: str, target_uuid: str, role: str
        ) -> None:
            """按区域身份与语义角色加入确定性的 dependency_only 边。"""

            pair = (source_uuid, target_uuid)
            if pair in seen_pairs:
                return
            seen_pairs.add(pair)
            result.append(
                {
                    "uuid": str(
                        uuid5(
                            UUID(region_uuid),
                            f"repeat-{role}:{source_uuid}:{target_uuid}",
                        )
                    ),
                    "source_node_uuid": source_uuid,
                    "target_node_uuid": target_uuid,
                    "source_handle_uuid": "",
                    "target_handle_uuid": "",
                    "dependency_only": True,
                }
            )

        for region_uuid in sorted(repeat_uuids):
            node = nodes[region_uuid]
            params = node.get("param")
            if not isinstance(params, Mapping):
                raise ExecutionPlanBuildError(
                    "invalid_control_region", "RepeatUntil 参数必须是对象"
                )
            maximum = params.get("max_iterations")
            initial_carry = params.get("initial_carry")
            next_carry = params.get("next_carry")
            until_expression = params.get("until")
            bindings = params.get("bindings")
            members = params.get("node_uuids")
            entries = params.get("entry_node_uuids")
            exits = params.get("exit_node_uuids")
            if (
                isinstance(maximum, bool)
                or not isinstance(maximum, int)
                or maximum < 1
                or not isinstance(initial_carry, Mapping)
                or not isinstance(next_carry, Mapping)
                or set(initial_carry) != set(next_carry)
                or not isinstance(until_expression, Mapping)
                or not isinstance(bindings, Mapping)
                or not isinstance(members, Sequence)
                or isinstance(members, (str, bytes))
                or not members
                or not isinstance(entries, Sequence)
                or isinstance(entries, (str, bytes))
                or not entries
                or not isinstance(exits, Sequence)
                or isinstance(exits, (str, bytes))
                or not exits
            ):
                raise ExecutionPlanBuildError(
                    "invalid_control_region", "RepeatUntil 冻结合同无效"
                )
            member_uuids = {str(value) for value in members}
            descendants = {
                candidate_uuid
                for candidate_uuid in active_node_uuids
                if candidate_uuid != region_uuid
                and is_descendant(candidate_uuid, region_uuid)
            }
            if (
                len(member_uuids) != len(members)
                or member_uuids != descendants
                or not {str(value) for value in entries} <= member_uuids
                or not {str(value) for value in exits} <= member_uuids
            ):
                raise ExecutionPlanBuildError(
                    "invalid_control_region", "循环模板没有完整覆盖区域后代"
                )
            for binding in initial_carry.values():
                if not isinstance(binding, Mapping):
                    raise ExecutionPlanBuildError(
                        "invalid_control_region", "初始 carry 来源必须是对象"
                    )
                if binding.get("kind") == "node_result":
                    source_uuid = str(binding.get("node_uuid") or "")
                    if (
                        source_uuid not in active_node_uuids
                        or source_uuid in member_uuids
                    ):
                        raise ExecutionPlanBuildError(
                            "invalid_control_region", "初始 carry 必须来自循环区域外"
                        )
                    add_edge(region_uuid, source_uuid, region_uuid, "initial-carry")
            for binding in next_carry.values():
                if not isinstance(binding, Mapping):
                    raise ExecutionPlanBuildError(
                        "invalid_control_region", "下一轮 carry 来源必须是对象"
                    )
                if (
                    binding.get("kind") == "node_result"
                    and str(binding.get("node_uuid") or "") not in member_uuids
                ):
                    raise ExecutionPlanBuildError(
                        "invalid_control_region", "下一轮 carry 必须来自当前循环体"
                    )
            for binding in bindings.values():
                if not isinstance(binding, Mapping):
                    raise ExecutionPlanBuildError(
                        "invalid_control_region", "循环条件绑定必须是对象"
                    )
                if (
                    binding.get("kind") == "node_result"
                    and str(binding.get("node_uuid") or "") not in member_uuids
                ):
                    raise ExecutionPlanBuildError(
                        "invalid_control_region", "循环条件节点结果必须来自当前轮"
                    )
            predecessors = params.get("predecessor_node_uuids", [])
            successors = params.get("successor_node_uuids", [])
            if (
                not isinstance(predecessors, Sequence)
                or isinstance(predecessors, (str, bytes))
                or not isinstance(successors, Sequence)
                or isinstance(successors, (str, bytes))
            ):
                raise ExecutionPlanBuildError(
                    "invalid_control_region", "循环区域前驱或后继必须是数组"
                )
            for source_value in predecessors:
                source_uuid = str(source_value)
                if source_uuid not in active_node_uuids or source_uuid in member_uuids:
                    raise ExecutionPlanBuildError(
                        "invalid_control_region", "循环区域前驱引用计划外或区域内节点"
                    )
                add_edge(region_uuid, source_uuid, region_uuid, "predecessor")
            for target_value in entries:
                add_edge(region_uuid, region_uuid, str(target_value), "entry")
            for target_value in successors:
                target_uuid = str(target_value)
                if target_uuid not in active_node_uuids or target_uuid in member_uuids:
                    raise ExecutionPlanBuildError(
                        "invalid_control_region", "循环区域后继引用计划外或区域内节点"
                    )
                add_edge(region_uuid, region_uuid, target_uuid, "successor")
        return result

    @staticmethod
    def _condition_control_edges(
        *,
        nodes: Mapping[str, Mapping[str, Any]],
        kinds: Mapping[str, str],
        active_node_uuids: set[str],
    ) -> list[dict[str, Any]]:
        """把条件区域到分支成员投影为无数据依赖边，禁止内部节点提前执行。"""

        result: list[dict[str, Any]] = []
        seen_targets: set[tuple[str, str]] = set()
        condition_uuids = {
            node_uuid
            for node_uuid in active_node_uuids
            if kinds.get(node_uuid) == "condition"
        }

        def is_descendant(node_uuid: str, region_uuid: str) -> bool:
            """只沿冻结 parent_uuid 证明区域成员关系。"""

            current = node_uuid
            seen: set[str] = set()
            while current not in seen:
                seen.add(current)
                parent = nodes.get(current, {}).get("parent_uuid")
                if parent == region_uuid:
                    return True
                if not isinstance(parent, str):
                    return False
                current = parent
            raise ExecutionPlanBuildError(
                "invalid_control_region", "条件区域父子关系包含环"
            )

        for node_uuid in condition_uuids:
            depth = 1
            current = node_uuid
            seen: set[str] = set()
            while current not in seen:
                seen.add(current)
                parent = nodes.get(current, {}).get("parent_uuid")
                if not isinstance(parent, str):
                    break
                if parent in condition_uuids:
                    depth += 1
                current = parent
            else:
                raise ExecutionPlanBuildError(
                    "invalid_control_region", "条件区域父子关系包含环"
                )
            if depth > 8:
                raise ExecutionPlanBuildError(
                    "control_nesting_too_deep", "条件区域嵌套深度不能超过 8"
                )

        for region_uuid, node in nodes.items():
            if (
                kinds.get(region_uuid) != "condition"
                or region_uuid not in active_node_uuids
            ):
                continue
            params = node.get("param")
            branches = params.get("branches") if isinstance(params, Mapping) else None
            if (
                not isinstance(branches, Sequence)
                or isinstance(branches, (str, bytes))
                or not branches
            ):
                raise ExecutionPlanBuildError(
                    "invalid_control_region", "条件区域缺少有序分支"
                )
            bindings = params.get("bindings", {}) if isinstance(params, Mapping) else {}
            if not isinstance(bindings, Mapping):
                raise ExecutionPlanBuildError(
                    "invalid_control_region", "条件变量绑定必须是对象"
                )
            from unilabos.workflow.workflow_spec_compiler import WorkflowSpecCompiler

            if set(bindings) != WorkflowSpecCompiler._condition_variable_names(params):
                # 与调度编译同一规则：表达式里的变量必须恰好都有绑定。
                raise ExecutionPlanBuildError(
                    "invalid_control_region", f"条件变量与绑定不一致：{region_uuid}"
                )
            region_descendants = {
                candidate_uuid
                for candidate_uuid in active_node_uuids
                if candidate_uuid != region_uuid
                and is_descendant(candidate_uuid, region_uuid)
            }
            declared_members: set[str] = set()
            for binding in bindings.values():
                if not isinstance(binding, Mapping):
                    raise ExecutionPlanBuildError(
                        "invalid_control_region", "条件变量绑定必须是对象"
                    )
                if binding.get("kind") not in {"node_result", "workflow_input"}:
                    # 与调度编译同一规则：条件变量只能来自节点结果或工作流输入。
                    raise ExecutionPlanBuildError(
                        "invalid_control_region", f"条件变量绑定类型无效：{region_uuid}"
                    )
                if binding.get("kind") != "node_result":
                    continue
                source_uuid = str(binding.get("node_uuid") or "")
                if source_uuid not in active_node_uuids or source_uuid == region_uuid:
                    raise ExecutionPlanBuildError(
                        "invalid_control_region", "条件结果绑定引用计划外节点"
                    )
                pair = (source_uuid, region_uuid)
                if pair not in seen_targets:
                    seen_targets.add(pair)
                    result.append(
                        {
                            "uuid": str(
                                uuid5(
                                    UUID(region_uuid),
                                    f"condition-source:{source_uuid}",
                                )
                            ),
                            "source_node_uuid": source_uuid,
                            "target_node_uuid": region_uuid,
                            "source_handle_uuid": "",
                            "target_handle_uuid": "",
                            "dependency_only": True,
                        }
                    )
            predecessors = params.get("predecessor_node_uuids", [])
            if not isinstance(predecessors, Sequence) or isinstance(
                predecessors, (str, bytes)
            ):
                raise ExecutionPlanBuildError(
                    "invalid_control_region", "条件顺序前驱必须是数组"
                )
            for source_value in predecessors:
                source_uuid = str(source_value)
                if source_uuid not in active_node_uuids or source_uuid == region_uuid:
                    raise ExecutionPlanBuildError(
                        "invalid_control_region", "条件顺序前驱引用计划外节点"
                    )
                pair = (source_uuid, region_uuid)
                if pair in seen_targets:
                    continue
                seen_targets.add(pair)
                result.append(
                    {
                        "uuid": str(
                            uuid5(
                                UUID(region_uuid),
                                f"condition-predecessor:{source_uuid}",
                            )
                        ),
                        "source_node_uuid": source_uuid,
                        "target_node_uuid": region_uuid,
                        "source_handle_uuid": "",
                        "target_handle_uuid": "",
                        "dependency_only": True,
                    }
                )
            for branch in branches:
                if not isinstance(branch, Mapping):
                    raise ExecutionPlanBuildError(
                        "invalid_control_region", "条件分支必须是对象"
                    )
                entries = branch.get("entry_node_uuids")
                exits = branch.get("exit_node_uuids")
                members = branch.get("node_uuids")
                if (
                    not isinstance(entries, Sequence)
                    or isinstance(entries, (str, bytes))
                    or not entries
                    or not isinstance(exits, Sequence)
                    or isinstance(exits, (str, bytes))
                    or not exits
                    or not isinstance(members, Sequence)
                    or isinstance(members, (str, bytes))
                    or not members
                ):
                    raise ExecutionPlanBuildError(
                        "invalid_control_region", "条件分支成员或边界无效"
                    )
                member_uuids = {str(value) for value in members}
                if len(member_uuids) != len(members):
                    raise ExecutionPlanBuildError(
                        "invalid_control_region", "条件分支成员不能重复"
                    )
                if (
                    not member_uuids <= region_descendants
                    or declared_members & member_uuids
                    or not {str(value) for value in entries} <= member_uuids
                    or not {str(value) for value in exits} <= member_uuids
                ):
                    raise ExecutionPlanBuildError(
                        "invalid_control_region", "条件分支成员不属于对应控制区域"
                    )
                declared_members.update(member_uuids)
                # 实验操作的结构入口未必包含内部独立条件；所有成员都必须
                # 等外层选路完成，不能先求值再被外层分支要求跳过。
                for target_value in dict.fromkeys([*entries, *members]):
                    target_uuid = str(target_value)
                    if (
                        target_uuid not in active_node_uuids
                        or target_uuid == region_uuid
                    ):
                        raise ExecutionPlanBuildError(
                            "invalid_control_region", "条件分支入口引用计划外节点"
                        )
                    pair = (region_uuid, target_uuid)
                    if pair in seen_targets:
                        continue
                    seen_targets.add(pair)
                    result.append(
                        {
                            "uuid": str(
                                uuid5(
                                    UUID(region_uuid),
                                    f"condition-entry:{target_uuid}",
                                )
                            ),
                            "source_node_uuid": region_uuid,
                            "target_node_uuid": target_uuid,
                            "source_handle_uuid": "",
                            "target_handle_uuid": "",
                            "dependency_only": True,
                        }
                    )
            if declared_members != region_descendants:
                raise ExecutionPlanBuildError(
                    "invalid_control_region", "条件分支没有完整覆盖区域后代"
                )
        return result

    @staticmethod
    def _planned_executor_kind(
        node: Mapping[str, Any],
        *,
        template: Mapping[str, Any],
    ) -> str:
        """确定冻结计划中的真实执行责任。

        参数：应用节点与对应节点模板。返回：普通设备动作、物料转移或其他规范
        执行种类。异常：未知节点类型沿用 ``executor_kind`` 的稳定失败。模板声明
        的 ``meta_data.unilab.executor_kind`` 优先用于区分仍以 ILab 节点展示的
        受信物料转移动作。
        """

        # ``manual_confirm`` 是工作流节点对底层设备动作模板施加的运行包装，
        # 因此必须优先于模板自身的 ``device_action`` 执行种类。
        if str(node.get("type") or "").strip().lower() == "manual_confirm":
            return "manual_confirm"
        template_metadata = template.get("meta_data")
        template_unilab = (
            template_metadata.get("unilab")
            if isinstance(template_metadata, Mapping)
            else None
        )
        explicit_kind = str(
            template.get("executor_kind")
            or (
                template_unilab.get("executor_kind")
                if isinstance(template_unilab, Mapping)
                else ""
            )
            or ""
        ).strip()
        if explicit_kind == "material_transfer":
            return explicit_kind
        return executor_kind(str(template.get("node_type") or node.get("type") or ""))

    @staticmethod
    def _has_fixed_executor_binding(node: Mapping[str, Any]) -> bool:
        """判断人工确认节点是否包装了一个固定设备动作。"""

        metadata = node.get("meta_data")
        unilab = metadata.get("unilab") if isinstance(metadata, Mapping) else None
        binding = (
            unilab.get("executor_binding") if isinstance(unilab, Mapping) else None
        )
        return (
            isinstance(binding, Mapping)
            and binding.get("mode") == "fixed"
            and bool(str(binding.get("device_id") or "").strip())
        )

    def _material_source_inputs(
        self,
        *,
        nodes: Mapping[str, Mapping[str, Any]],
        active: Mapping[str, Mapping[str, Any]],
        kinds: Mapping[str, str],
        edges: Sequence[Mapping[str, Any]],
        topological_order: Sequence[str],
        included_source_uuids: set[str],
        runtime_source_uuids: set[str],
    ) -> tuple[
        dict[str, list[dict[str, Any]]],
        dict[str, dict[str, dict[str, str]]],
        dict[str, list[dict[str, str]]],
    ]:
        """投影 ``existing`` 物料来源（MaterialSource）的静态准入输入。

        参数：完整节点、活动节点、执行种类、平面计划边与拓扑顺序来自同一应用
        图；``included_source_uuids`` 限定本次运行范围真正包含的来源。返回：遗留
        库存预留需求、仅固定来源可提前写入的最终物料引用参数，以及自动来源成功
        准入后的作业参数目标；它不是任务物料预留。异常：create_new、选择器 UUID
        非法或物料流分叉/循环时抛稳定计划错误；范围外来源不参与校验。
        """

        # ``outgoing`` 保留每条物料边的目标节点与最终参数键。
        outgoing: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for edge in edges:
            if (
                edge.get("dependency_only") is not True
                and edge.get("source_type") == "ResourceSlot"
                and edge.get("target_type") == "ResourceSlot"
            ):
                outgoing[str(edge["source_node_uuid"])].append(
                    (
                        str(edge["target_node_uuid"]),
                        final_target_data_key(str(edge.get("target_data_key") or "")),
                    )
                )
        order = {node_uuid: index for index, node_uuid in enumerate(topological_order)}
        requirements: dict[str, list[dict[str, Any]]] = defaultdict(list)
        # ``material_params`` 把具体物料身份绑定到首消费动作的参数键。
        material_params: dict[str, dict[str, dict[str, str]]] = defaultdict(dict)
        # ``binding_targets`` 只描述运行时绑定应写到哪里，不包含库存选择结果。
        binding_targets: dict[str, list[dict[str, str]]] = defaultdict(list)
        target_owners: dict[tuple[str, str], str] = {}
        for source_uuid, node in nodes.items():
            if (
                source_uuid not in included_source_uuids
                or kinds[source_uuid] != "material_source"
                or node.get("disabled") is True
            ):
                continue
            selector = node.get("param")
            if not isinstance(selector, Mapping):
                raise ExecutionPlanBuildError(
                    "invalid_material_source_selector", "物料来源选择器必须是对象"
                )
            if selector.get("mode") != "existing":
                raise ExecutionPlanBuildError(
                    "unsupported_material_source_mode",
                    "短期调度桥只接受固定的 existing 物料来源",
                )
            if self._has_fixed_material_identity(selector):
                material_uuid = str(selector.get("material_uuid") or "")
                material_uuid = self._selector_uuid(
                    material_uuid,
                    code="invalid_material_uuid",
                    message="物料来源 UUID 非法",
                )
                requirement = {
                    "template_id": self._selector_uuid(
                        selector.get("resource_template_uuid"),
                        code="invalid_material_source_selector",
                        message="物料来源资源模板 UUID 非法",
                    ),
                    "instance_uuid": material_uuid,
                }
            else:
                material_uuid = ""
                mount = selector.get("mount")
                if mount is None:
                    if source_uuid not in runtime_source_uuids:
                        raise ExecutionPlanBuildError(
                            "dynamic_material_source_mount_requires_runtime",
                            "动态 mount 不能进入任务预准入",
                        )
                    mount_uuid = ""
                elif not isinstance(mount, Mapping):
                    raise ExecutionPlanBuildError(
                        "invalid_material_source_selector",
                        "自动物料来源缺少挂载点",
                    )
                else:
                    mount_uuid = self._selector_uuid(
                        mount.get("uuid"),
                        code="invalid_material_source_selector",
                        message="物料来源挂载点 UUID 非法",
                    )
                template_uuid = self._selector_uuid(
                    selector.get("resource_template_uuid"),
                    code="invalid_material_source_selector",
                    message="物料来源资源模板 UUID 非法",
                )
                site_uuid = ""
                if selector.get("site") is not None:
                    site_uuid = self._selector_uuid(
                        selector.get("site"),
                        code="invalid_material_source_selector",
                        message="物料来源库位 UUID 非法",
                    )
                raw_slot_uuids = selector.get("slot_range") or []
                if not isinstance(raw_slot_uuids, Sequence) or isinstance(
                    raw_slot_uuids, (str, bytes)
                ):
                    raise ExecutionPlanBuildError(
                        "invalid_material_source_selector",
                        "物料来源库位范围必须是数组",
                    )
                slot_uuids = [
                    self._selector_uuid(
                        value,
                        code="invalid_material_source_selector",
                        message="物料来源库位范围 UUID 非法",
                    )
                    for value in raw_slot_uuids
                ]
                if site_uuid and slot_uuids:
                    raise ExecutionPlanBuildError(
                        "invalid_material_source_selector",
                        "物料来源不能同时指定库位和库位范围",
                    )
                requirement = {
                    "template_id": template_uuid,
                    "mount_uuid": mount_uuid,
                    "site_uuid": site_uuid,
                    "slot_uuids": slot_uuids,
                }
                if selector.get("search_descendants"):
                    requirement["search_descendants"] = True
            # 短期遗留预留（inventory_reservation）归属来源协调责任；普通设备
            # 动作只消费已经准入的稳定物料引用，不再次取得任务级预留。
            requirements[source_uuid].append(requirement)
            consumer_bindings = self._device_consumer_bindings(
                source_uuid,
                active=active,
                kinds=kinds,
                outgoing=outgoing,
                order=order,
            )
            for consumer, param_key in consumer_bindings:
                if not param_key:
                    raise ExecutionPlanBuildError(
                        "invalid_execution_graph",
                        "物料占位符目标连接点缺少参数键",
                    )
                target_key = (consumer, param_key)
                existing_owner = target_owners.get(target_key)
                if existing_owner is not None and existing_owner != source_uuid:
                    raise ExecutionPlanBuildError(
                        "invalid_execution_graph",
                        "多个 existing 物料来源冲突写入同一动作参数",
                    )
                target_owners[target_key] = source_uuid
                binding_targets[source_uuid].append(
                    {"workflow_node_uuid": consumer, "param_key": param_key}
                )
                if material_uuid:
                    material_params[consumer][param_key] = {"uuid": material_uuid}
        return dict(requirements), dict(material_params), dict(binding_targets)

    @staticmethod
    def _selector_uuid(value: Any, *, code: str, message: str) -> str:
        """把选择器身份规范为非 nil UUID。

        参数：``value`` 是冻结选择器字段；``code``/``message`` 是稳定计划诊断。
        返回：规范小写 UUID。异常：空值、非法值或 nil UUID 抛计划错误。
        """

        try:
            identity = UUID(str(value or ""))
        except ValueError as exc:
            raise ExecutionPlanBuildError(code, message) from exc
        if identity.int == 0:
            raise ExecutionPlanBuildError(code, message)
        return str(identity)

    @staticmethod
    def _device_consumer_bindings(
        source_uuid: str,
        *,
        active: Mapping[str, Mapping[str, Any]],
        kinds: Mapping[str, str],
        outgoing: Mapping[str, Sequence[tuple[str, str]]],
        order: Mapping[str, int],
    ) -> list[tuple[str, str]]:
        """沿物料占位符（ResourceSlot）链寻找首层设备动作集合。

        参数：来源 UUID、活动节点、执行种类、带目标参数键的邻接表和稳定拓扑
        排名描述一条冻结物料链。返回：按拓扑稳定排序的首层启用
        ``device_action`` UUID 及其物料参数键；复合工作流展开形成隐式透传时，
        同一来源可直接绑定多个严格有序消费者。异常：循环时抛计划错误。
        """

        pending = [source_uuid]
        visited: set[str] = set()
        consumers: list[tuple[str, str]] = []
        while pending:
            current = pending.pop(0)
            if current in visited:
                raise ExecutionPlanBuildError(
                    "material_flow_not_linear", "物料占位符链含循环"
                )
            visited.add(current)
            targets = list(outgoing.get(current, ()))
            targets.sort(
                key=lambda target: (
                    order.get(target[0], len(order)),
                    target[0],
                    target[1],
                )
            )
            for target_uuid, target_param_key in targets:
                # 加液、搬运这类物料转移动作与本地工作流命令同样是物料占位符的消费者。
                if target_uuid in active and kinds[target_uuid] in {
                    "device_action",
                    "material_transfer",
                    "workflow_command",
                }:
                    binding = (target_uuid, target_param_key)
                    if binding not in consumers:
                        consumers.append(binding)
                    continue
                if kinds.get(target_uuid) == "material_source":
                    # 下游 material_source 接管后续消费。父级只负责选盒/mount，
                    # 不能再穿过子级来源去争写同一动作参数。
                    continue
                pending.append(target_uuid)
        return consumers

    @staticmethod
    def _frozen_action_contract(
        template: Mapping[str, Any], *, node_uuid: str
    ) -> dict[str, Any]:
        """从节点模板保留元数据冻结完整动作合同（Action Contract）。

        参数：``template`` 是应用图冻结的工作流节点模板，``node_uuid`` 是诊断
        使用的动作节点身份。返回：与模板容器隔离的完整动作 Schema envelope。
        异常：保留元数据、完整合同或 ``properties.goal`` 缺失/非对象时抛
        ``ExecutionPlanBuildError``；禁止回退实时注册表或 Goal 子模式。
        """

        # ``metadata``/``unilab`` 定位模板投影保留的 Uni-Lab 执行合同边界。
        metadata = template.get("meta_data")
        unilab = metadata.get("unilab") if isinstance(metadata, Mapping) else None
        # ``contract`` 是本工作流任务（WorkflowTask）唯一可冻结的完整动作合同。
        contract = (
            unilab.get("action_contract_schema")
            if isinstance(unilab, Mapping)
            else None
        )
        properties = (
            contract.get("properties") if isinstance(contract, Mapping) else None
        )
        # ``goal_schema`` 只用于证明完整合同能被动作物料锁编译器安全消费。
        goal_schema = (
            properties.get("goal") if isinstance(properties, Mapping) else None
        )
        if not isinstance(goal_schema, Mapping):
            raise ExecutionPlanBuildError(
                "invalid_action_contract",
                f"设备动作模板缺少完整动作合同：{node_uuid}",
            )
        return deepcopy(dict(contract))

    @staticmethod
    def _frozen_param_schema(
        template: Mapping[str, Any], *, node_uuid: str
    ) -> dict[str, Any]:
        """冻结非设备节点的参数 Schema，并兼容 Backend 的 JSON 文本格式。

        参数：``template`` 是应用图冻结的节点模板，``node_uuid`` 是诊断使用的
        节点身份。返回：与模板容器隔离的 JSON Schema 对象。异常：Schema 文本
        不是合法 JSON，或解码后不是对象时抛 ``ExecutionPlanBuildError``。
        """

        schema = template.get("schema")
        if isinstance(schema, str):
            try:
                schema = json.loads(schema)
            except json.JSONDecodeError as error:
                raise ExecutionPlanBuildError(
                    "invalid_param_schema",
                    f"节点参数 Schema 不是合法 JSON：{node_uuid}",
                ) from error
        if not isinstance(schema, Mapping):
            raise ExecutionPlanBuildError(
                "invalid_param_schema",
                f"节点参数 Schema 必须是对象：{node_uuid}",
            )
        return deepcopy(dict(schema))

    @staticmethod
    def _frozen_action_resource_contract(
        template: Mapping[str, Any],
    ) -> dict[str, Any]:
        """读取节点模板内由 AST 编译的动作资源合同。

        参数：``template`` 是任务创建时引用的动作节点模板。返回：与模板隔离的
        ``ActionResourceContract`` 字典；动作未声明资源语义时返回空字典。异常：
        完整动作 Schema 的扩展形状损坏时抛 ``ExecutionPlanBuildError``，禁止运行期
        回查可变注册表猜测资源。
        """

        metadata = template.get("meta_data")
        unilab = metadata.get("unilab") if isinstance(metadata, Mapping) else None
        schema = (
            unilab.get("action_contract_schema")
            if isinstance(unilab, Mapping)
            else None
        )
        extension = (
            schema.get("x-unilabos-action-contract")
            if isinstance(schema, Mapping)
            else None
        )
        if extension is None:
            return {}
        if not isinstance(extension, Mapping):
            raise ExecutionPlanBuildError(
                "invalid_action_resource_contract",
                "动作合同扩展必须是对象",
            )
        resource_contract = extension.get("resource_contract")
        if resource_contract is None:
            return {}
        if not isinstance(resource_contract, Mapping):
            raise ExecutionPlanBuildError(
                "invalid_action_resource_contract",
                "动作资源合同必须是对象",
            )
        return deepcopy(dict(resource_contract))

    @staticmethod
    def _require_complete_material_transfer_contract(
        *,
        kind: str,
        action_name: str,
        resource_contract: Mapping[str, Any],
    ) -> None:
        """禁止机械臂物料转移以空合同进入执行计划。

        参数：``kind`` 是冻结执行责任，``action_name`` 是动作名，
        ``resource_contract`` 是 AST 编译后的声明式资源合同。返回：合同包含完整
        ``transfer`` 映射时无返回值。``transfer_resource`` 只把已到位物料改挂到
        目标库位，不占用夹爪。异常：机械臂物料转移缺少映射时抛
        ``ExecutionPlanBuildError``。
        """

        if kind != "material_transfer" or action_name == "transfer_resource":
            return
        transfer = resource_contract.get("transfer")
        required_text = (
            "material_param",
            "target_owner_param",
            "gripper_site_role",
        )
        if (
            not isinstance(transfer, Mapping)
            or any(
                not str(transfer.get(field) or "").strip() for field in required_text
            )
            or not (
                str(transfer.get("target_site_uuid_param") or "").strip()
                or str(transfer.get("target_site_name_param") or "").strip()
            )
        ):
            raise ExecutionPlanBuildError(
                "missing_material_transfer_resource_contract",
                "物料转移动作必须声明物料、目标设备、目标库位和机械臂夹爪角色",
            )

    @staticmethod
    def _device_action_contract(
        node: Mapping[str, Any],
        *,
        template: Mapping[str, Any],
    ) -> dict[str, Any]:
        """冻结设备动作执行器和动作合同。

        参数：``node`` 是应用图设备动作节点。返回：固定执行器身份，或只含资源
        模板的动态设备选择器，以及动作名与规范动作类型。异常：固定绑定非法、
        动态模板身份缺失或动作合同不完整时失败关闭。
        """

        metadata = node.get("meta_data")
        unilab = metadata.get("unilab") if isinstance(metadata, Mapping) else None
        binding = (
            unilab.get("executor_binding") if isinstance(unilab, Mapping) else None
        )
        device_id = ""
        if isinstance(binding, Mapping) and binding.get("mode") == "fixed":
            device_id = str(binding.get("device_id") or "").strip()
        if binding is not None and not device_id:
            raise ExecutionPlanBuildError(
                "invalid_executor_binding",
                "设备动作固定执行器绑定非法",
            )
        device_selector: dict[str, str] = {}
        if not device_id:
            resource_template_uuid = str(
                template.get("resource_template_uuid") or ""
            ).strip()
            try:
                resource_template_uuid = str(UUID(resource_template_uuid))
            except ValueError as error:
                raise ExecutionPlanBuildError(
                    "invalid_device_selector",
                    "动态设备选择器缺少合法资源模板 UUID",
                ) from error
            device_selector = {
                "mode": "resource_template",
                "resource_template_uuid": resource_template_uuid,
            }
        action_type = str(node.get("action_type") or "").strip()
        frozen_template_type = str(template.get("type") or "").strip()
        if not action_type and frozen_template_type.startswith("UniLabJsonCommand"):
            action_type = frozen_template_type
        template_metadata = template.get("meta_data")
        template_unilab = (
            template_metadata.get("unilab")
            if isinstance(template_metadata, Mapping)
            else None
        )
        return {
            "device_id": device_id,
            "device_selector": device_selector,
            "action_name": node.get("action_name"),
            "action_type": action_type or "UniLabJsonCommand",
            "always_free": bool(
                template_unilab.get("always_free", False)
                if isinstance(template_unilab, Mapping)
                else False
            ),
        }

    @staticmethod
    def _index(value: Any, field: str) -> dict[str, Mapping[str, Any]]:
        """按 UUID 索引对象序列。

        参数：``value`` 是边界数组，``field`` 是诊断字段。返回：身份映射。
        异常：非数组、非对象、空身份或重复身份时抛计划错误。
        """

        objects = ExecutionPlanBuilder._objects(value, field)
        result: dict[str, Mapping[str, Any]] = {}
        for item in objects:
            identity = str(item.get("uuid") or "")
            if not identity or identity in result:
                raise ExecutionPlanBuildError(
                    "invalid_execution_graph", f"{field} 身份缺失或重复"
                )
            result[identity] = item
        return result

    @staticmethod
    def _objects(value: Any, field: str) -> list[Mapping[str, Any]]:
        """校验对象数组。

        参数：``value`` 是边界值，``field`` 是诊断字段。返回：对象列表。
        异常：类型不符时抛 ``ExecutionPlanBuildError``。
        """

        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            raise ExecutionPlanBuildError(
                "invalid_execution_graph", f"{field} 必须是数组"
            )
        if any(not isinstance(item, Mapping) for item in value):
            raise ExecutionPlanBuildError(
                "invalid_execution_graph", f"{field} 成员必须是对象"
            )
        return list(value)


def _has_resource_declarations(
    graph: Mapping[str, Any],
    planned_nodes: Sequence[Mapping[str, Any]],
) -> bool:
    """判断冻结图是否显式进入资源计划语义。"""

    if any(graph.get(key) for key in ("resources", "resource_scopes")):
        return True
    workflow = graph.get("workflow")
    workflow_meta = workflow.get("meta_data") if isinstance(workflow, Mapping) else None
    unilab_meta = workflow_meta.get("unilab") if isinstance(workflow_meta, Mapping) else None
    if isinstance(unilab_meta, Mapping) and any(
        unilab_meta.get(key) for key in ("resources", "resource_scopes")
    ):
        return True
    for node in planned_nodes:
        if node.get("resource_defaults") or node.get("resources") or node.get("transfer_step"):
            return True
        metadata = node.get("meta_data")
        node_unilab = metadata.get("unilab") if isinstance(metadata, Mapping) else None
        if isinstance(node_unilab, Mapping) and node_unilab.get("resource_defaults"):
            return True
        contract = node.get("action_resource_contract")
        if isinstance(contract, Mapping):
            if (
                contract.get("resource_aliases")
                or contract.get("required_device_params")
                or contract.get("transfer_step")
                or contract.get("operate_in_place")
            ):
                return True
            transfer = contract.get("transfer")
            if isinstance(transfer, Mapping) and any(
                transfer.get(field) for field in ("motion_resource_roles", "tool_resource_roles")
            ):
                return True
            if contract.get("version") == 2 and contract.get("resource_params"):
                return True
    return False


def _action_material_resource_params(contract: Mapping[str, Any]) -> tuple[str, ...]:
    """返回 v2 动作合同中明确声明为物料资源的参数名。"""

    resource_params = contract.get("resource_params", ())
    if not isinstance(resource_params, Sequence) or isinstance(
        resource_params, (str, bytes)
    ):
        return ()
    return tuple(
        dict.fromkeys(
            str(item["param"])
            for item in resource_params
            if isinstance(item, Mapping)
            and item.get("role") == "material"
            and str(item.get("param") or "")
        )
    )


def _project_resource_scopes_to_planned_nodes(
    graph: dict[str, Any],
    *,
    planned_node_uuids: set[str],
) -> None:
    """去掉只存在于作者图中的展示节点，保留执行节点的资源边界。

    作者 AST 的 scope 成员可以包含 Group/Condition 等结构节点，而执行计划只
    携带实际作业节点。此 seam 使用已知 planned UUID 做安全投影；未知业务 UUID
    不会被静默创造，过滤后为空的作用域仍交给资源计划模块报错。
    """

    def project(raw_scopes: Any) -> Any:
        if not isinstance(raw_scopes, list):
            return raw_scopes
        projected: list[Any] = []
        for raw_scope in raw_scopes:
            if not isinstance(raw_scope, Mapping):
                projected.append(raw_scope)
                continue
            scope = dict(raw_scope)
            raw_members = scope.get("node_uuids")
            if isinstance(raw_members, Sequence) and not isinstance(
                raw_members, (str, bytes)
            ):
                members = [
                    str(node_uuid)
                    for node_uuid in raw_members
                    if str(node_uuid) in planned_node_uuids
                ]
                if raw_members and members:
                    scope["node_uuids"] = members
                    if str(scope.get("entry_node_uuid") or "") not in planned_node_uuids:
                        scope["entry_node_uuid"] = members[0]
                    if str(scope.get("exit_node_uuid") or "") not in planned_node_uuids:
                        scope["exit_node_uuid"] = members[-1]
            projected.append(scope)
        return projected

    if isinstance(graph.get("resource_scopes"), list):
        graph["resource_scopes"] = project(graph["resource_scopes"])
    workflow = graph.get("workflow")
    if not isinstance(workflow, Mapping):
        return
    workflow_meta = workflow.get("meta_data")
    if not isinstance(workflow_meta, Mapping):
        return
    unilab_meta = workflow_meta.get("unilab")
    if not isinstance(unilab_meta, Mapping):
        return
    raw_scopes = unilab_meta.get("resource_scopes")
    if isinstance(raw_scopes, list):
        unilab_meta["resource_scopes"] = project(raw_scopes)


__all__ = [
    "CONTROL_PLAN_CAPABILITIES",
    "CONTROL_PLAN_VERSION",
    "PLAN_VERSION",
    "RESOURCE_PLAN_CAPABILITY",
    "RESOURCE_PLAN_VERSION",
    "STATIC_RESOURCE_DAG_CAPABILITY",
    "ExecutionPlanBuildError",
    "ExecutionPlanBuilder",
]
