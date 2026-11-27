"""把执行计划（ExecutionPlan）纯编译为旧调度器工作流规格。"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from unilabos.app.scheduler.inventory.domain import MaterialRequirement
from unilabos.app.scheduler.models import (
    Handle,
    RepeatUntilRegion,
    WorkflowEdge,
    WorkflowNode,
    WorkflowSpec,
)
from unilabos.utils.tracing import normalize_trace_context
from unilabos.workflow._workflow_spec_snapshot import (
    WorkflowSpecCompilationError,
    canonical_uuid,
    index_jobs,
    index_objects,
    mapping,
    mapping_sequence,
)
from unilabos.workflow.execution_plan import (
    CONTROL_PLAN_CAPABILITIES,
    CONTROL_PLAN_VERSION,
    DYNAMIC_ITERATION_CAPABILITY,
    PLAN_VERSION,
    RUNTIME_MATERIAL_SOURCE_CAPABILITY,
)
from unilabos.workflow.resource_lock_plan import (
    RESOURCE_PLAN_CAPABILITY,
    SCHEDULER_RESOURCE_ORDER_CAPABILITY,
    STATIC_RESOURCE_DAG_CAPABILITY,
    ResourcePlanError,
    deserialize_resource_plan,
    normalize_execution_resource_plan,
)


class WorkflowSpecCompiler:
    """封装冻结执行计划到旧调度输入的全部确定性转换。"""

    def compile(
        self,
        task_snapshot: Mapping[str, Any],
        jobs: Sequence[Mapping[str, Any]],
    ) -> WorkflowSpec:
        """编译已持久化身份的工作流任务（WorkflowTask）。

        参数：``task_snapshot`` 提供任务身份和唯一运行静态输入
        ``execution_plan``，``jobs`` 提供已有作业身份与最终参数。返回：纯
        ``WorkflowSpec``。异常：计划、身份、执行器合同或端点非法时抛闭集编译
        错误，且不读取库存（Inventory）、作业执行占用
        （JobExecutionClaim）或设备实时状态。
        """

        return self._compile(task_snapshot, jobs, inventory_admission=False)

    def validate_inventory_admission(
        self,
        task_snapshot: Mapping[str, Any],
        jobs: Sequence[Mapping[str, Any]],
    ) -> bool:
        """检查库存准入所需的冻结计划，只返回是否需要库存。"""
        spec = self._compile(task_snapshot, jobs, inventory_admission=True)
        return bool(spec.material_requirements_by_node())

    def _compile(
        self,
        task_snapshot: Mapping[str, Any],
        jobs: Sequence[Mapping[str, Any]],
        *,
        inventory_admission: bool,
    ) -> WorkflowSpec:
        task = mapping(task_snapshot, "invalid_task_snapshot", "task_snapshot")
        # ``task_uuid`` 是旧调度运行复用的工作流任务稳定身份。
        task_uuid = canonical_uuid(task.get("uuid"), "invalid_task_identity", "task_snapshot.uuid")
        audit_snapshot = task.get("workflow_snapshot")
        if audit_snapshot is not None:
            mapping(
                audit_snapshot,
                "invalid_workflow_snapshot",
                "task_snapshot.workflow_snapshot",
            )
        plan = mapping(
            task.get("execution_plan"),
            "invalid_execution_plan",
            "task_snapshot.execution_plan",
        )
        plan = normalize_execution_resource_plan(plan)
        version = plan.get("version")
        if isinstance(version, bool) or version not in {
            PLAN_VERSION,
            CONTROL_PLAN_VERSION,
        }:
            raise WorkflowSpecCompilationError(
                "invalid_execution_plan",
                f"执行计划版本必须是 {PLAN_VERSION} 或 {CONTROL_PLAN_VERSION}",
            )
        capability_set: set[str] = set()
        if version == CONTROL_PLAN_VERSION:
            capabilities = plan.get("capabilities")
            if not isinstance(capabilities, list):
                raise WorkflowSpecCompilationError(
                    "unsupported_execution_plan_capability",
                    "控制执行计划能力声明不完整",
                )
            required_capabilities = set(CONTROL_PLAN_CAPABILITIES)
            capability_set = set(str(item) for item in capabilities)
            allowed_extras = {
                DYNAMIC_ITERATION_CAPABILITY,
                RUNTIME_MATERIAL_SOURCE_CAPABILITY,
                RESOURCE_PLAN_CAPABILITY,
                STATIC_RESOURCE_DAG_CAPABILITY,
                SCHEDULER_RESOURCE_ORDER_CAPABILITY,
            }
            if (
                not required_capabilities <= capability_set
                or capability_set - required_capabilities - allowed_extras
            ):
                raise WorkflowSpecCompilationError(
                    "unsupported_execution_plan_capability",
                    "控制执行计划能力声明不完整",
                )
            if DYNAMIC_ITERATION_CAPABILITY in capability_set and not any(
                str(item.get("kind") or "") == "repeat_until"
                for item in plan.get("nodes", [])
                if isinstance(item, Mapping)
            ):
                raise WorkflowSpecCompilationError(
                    "unsupported_execution_plan_capability",
                    "动态迭代能力声明与计划节点不一致",
                )
        raw_resource_plan = plan.get("resource_plan")
        resource_plan = None
        if raw_resource_plan is not None:
            try:
                resource_plan = deserialize_resource_plan(raw_resource_plan)
            except ResourcePlanError as error:
                raise WorkflowSpecCompilationError(
                    "invalid_resource_plan",
                    f"资源计划无效：{error.message}",
                ) from error
            pending_inventory = (
                inventory_admission
                and plan.get("inventory_resource_binding") == "pending"
                and resource_plan.binding_state == "template"
            )
            if resource_plan.binding_state != "bound" and not pending_inventory:
                raise WorkflowSpecCompilationError(
                    "resource_plan_unbound",
                    "资源计划必须在任务编译前绑定到具体工站实例",
                )
            order_proofs = {STATIC_RESOURCE_DAG_CAPABILITY, SCHEDULER_RESOURCE_ORDER_CAPABILITY}
            if not pending_inventory and order_proofs.isdisjoint(resource_plan.capabilities):
                raise WorkflowSpecCompilationError(
                    "invalid_resource_plan",
                    "资源计划缺少静态无环证明或调度顺序要求",
                )
            raw_plan_capabilities = plan.get("capabilities")
            if not isinstance(raw_plan_capabilities, list) or RESOURCE_PLAN_CAPABILITY not in {
                str(item) for item in raw_plan_capabilities
            }:
                raise WorkflowSpecCompilationError(
                    "unsupported_execution_plan_capability",
                    "执行计划缺少资源区间能力声明",
                )
        elif isinstance(plan.get("capabilities"), list):
            plan_capabilities = {str(item) for item in plan["capabilities"]}
            if RESOURCE_PLAN_CAPABILITY in plan_capabilities or (
                {STATIC_RESOURCE_DAG_CAPABILITY, SCHEDULER_RESOURCE_ORDER_CAPABILITY}.intersection(plan_capabilities)
            ):
                raise WorkflowSpecCompilationError(
                    "invalid_resource_plan",
                    "执行计划声明了资源计划能力但缺少 resource_plan",
                )
        raw_nodes = mapping_sequence(
            plan.get("nodes"),
            "invalid_execution_plan",
            "task_snapshot.execution_plan.nodes",
        )
        if resource_plan is not None:
            interval_ids = {item.interval_id for item in resource_plan.intervals}
            acquire_set_ids = {item.acquire_set_id for item in resource_plan.acquire_sets}
            for index, raw_node in enumerate(raw_nodes):
                raw_interval_ids = raw_node.get("resource_interval_ids") or []
                if not isinstance(raw_interval_ids, list) or any(
                    not isinstance(value, str) or value not in interval_ids
                    for value in raw_interval_ids
                ):
                    raise WorkflowSpecCompilationError(
                        "invalid_resource_plan",
                        f"计划节点 resource_interval_ids 无效：{index}",
                    )
                acquire_set_id = str(raw_node.get("resource_acquire_set_id") or "")
                if acquire_set_id and acquire_set_id not in acquire_set_ids:
                    raise WorkflowSpecCompilationError(
                        "invalid_resource_plan",
                        f"计划节点 resource_acquire_set_id 无效：{index}",
                    )
                node_plan_id = str(raw_node.get("resource_plan_id") or "")
                if node_plan_id and node_plan_id != resource_plan.plan_id:
                    raise WorkflowSpecCompilationError(
                        "invalid_resource_plan",
                        f"计划节点 resource_plan_id 不匹配：{index}",
                    )
        raw_handles = mapping_sequence(
            plan.get("handles", []),
            "invalid_execution_plan",
            "task_snapshot.execution_plan.handles",
        )
        raw_edges = mapping_sequence(
            plan.get("edges", []),
            "invalid_execution_plan",
            "task_snapshot.execution_plan.edges",
        )
        nodes, ordered_node_uuids = index_objects(
            raw_nodes,
            identity_code="invalid_node_identity",
            duplicate_code="duplicate_node_identity",
            field="execution_plan.nodes",
        )
        handles, ordered_handle_uuids = index_objects(
            raw_handles,
            identity_code="invalid_handle_identity",
            duplicate_code="duplicate_handle_identity",
            field="execution_plan.handles",
        )
        jobs_by_node = index_jobs(jobs, nodes=nodes)
        repeat_members = self._repeat_members(nodes)
        compiled_nodes = self._compile_nodes(
            ordered_node_uuids=ordered_node_uuids,
            nodes=nodes,
            handles=handles,
            jobs_by_node=jobs_by_node,
            task_input=(
                task.get("input")
                if isinstance(task.get("input"), Mapping)
                else (
                    task.get("normalized_input")
                    if isinstance(task.get("normalized_input"), Mapping)
                    else {}
                )
            ),
            control_enabled=version == CONTROL_PLAN_VERSION,
            repeat_member_uuids=repeat_members,
        )
        if any(node.executor_kind == "material_source" for node in compiled_nodes):
            if RUNTIME_MATERIAL_SOURCE_CAPABILITY not in capability_set:
                raise WorkflowSpecCompilationError("unsupported_execution_plan_capability", "运行时物料来源缺少能力声明")
            from unilabos.workflow.runtime_material_source import validate_replenishment_plan
            validate_replenishment_plan(list(nodes.values()))
        from unilabos.workflow.runtime_material_source import (
            runtime_command_material_edges,
            runtime_material_parameters,
        )
        if any(
            node.executor_kind in {"material_source", "material_io"}
            for node in compiled_nodes
        ) or runtime_command_material_edges(list(nodes.values()), plan["edges"], plan.get("handles") or ()):
            dynamic_parameters = runtime_material_parameters(
                list(nodes.values()), plan["edges"], plan.get("handles") or ()
            )
            for compiled_node in compiled_nodes:
                compiled_node.runtime_material_parameters = dynamic_parameters.get(compiled_node.id, [])
        top_level_nodes = [node for node in compiled_nodes if node.id not in repeat_members]
        active_node_uuids = {node.id for node in compiled_nodes}
        # ``coordinator_node_uuids`` 在执行计划中保留图身份，但不会进入旧调度器。
        coordinator_node_uuids = {
            node_uuid
            for node_uuid, node in nodes.items()
            if str(node.get("kind") or "").strip()
            in {"material_source", "workflow_input", "workflow_output"}
            and not node.get("runtime_material_source")
        }
        # 这些协调器由提交/数据投影边界结算，本身不跨物理派发边界。即使
        # workflow_output 此刻仍是 pending，也不能成为设备资源的释放屏障。
        # 只投影真实区间成员，避免把无关协调节点扩大成调度器信任输入。
        resource_plan_node_uuids = (
            {
                member
                for interval in resource_plan.intervals
                for member in interval.node_uuids
            }
            if resource_plan is not None
            else set()
        )
        resource_coordinator_node_uuids = sorted(
            coordinator_node_uuids & resource_plan_node_uuids
        )
        compiled_handles = self._compile_handles(
            ordered_handle_uuids=ordered_handle_uuids,
            handles=handles,
            active_node_uuids=active_node_uuids,
            coordinator_node_uuids=coordinator_node_uuids,
            planned_node_uuids=set(nodes),
        )
        compiled_edges = self._compile_edges(
            raw_edges,
            active_node_uuids=active_node_uuids,
            coordinator_node_uuids=coordinator_node_uuids,
            planned_node_uuids=set(nodes),
            handles=handles,
        )
        repeat_regions = self._compile_repeat_regions(
            nodes=nodes,
            compiled_nodes=compiled_nodes,
            compiled_edges=compiled_edges,
            compiled_handles=compiled_handles,
        )
        top_level_node_uuids = {node.id for node in top_level_nodes}
        return WorkflowSpec(
            workflow_id=task_uuid,
            task_id=task_uuid,
            nodes=top_level_nodes,
            edges=[
                edge
                for edge in compiled_edges
                if edge.source_node_id in top_level_node_uuids
                and edge.target_node_id in top_level_node_uuids
            ],
            handles=[
                handle for handle in compiled_handles if handle.node_id in top_level_node_uuids
            ],
            priority=task.get("priority", 1.0),
            submitted_at=self.submitted_at(task),
            lab_id=str(task.get("lab_id") or "").strip(),
            run_mode=str(task.get("run_mode") or plan.get("run_mode") or "normal"),
            trace_context=normalize_trace_context(
                task.get("trace_context")
                if isinstance(task.get("trace_context"), Mapping)
                else None
            ),
            repeat_regions=repeat_regions,
            resource_plan=(
                deepcopy(dict(raw_resource_plan))
                if isinstance(raw_resource_plan, Mapping)
                else None
            ),
            resource_coordinator_node_ids=resource_coordinator_node_uuids,
        )

    @staticmethod
    def submitted_at(task: Mapping[str, Any]) -> float:
        """优先使用持久 Task 创建时间，兼容未带读模型字段的直接编译调用。"""

        normalized = str(task.get("create_time") or "").strip()
        if not normalized:
            return time.time()
        try:
            parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
        except ValueError as error:
            raise WorkflowSpecCompilationError(
                "invalid_task_snapshot",
                "task_snapshot.create_time 不是合法时间",
            ) from error
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()

    def _compile_nodes(
        self,
        *,
        ordered_node_uuids: Sequence[str],
        nodes: Mapping[str, Mapping[str, Any]],
        handles: Mapping[str, Mapping[str, Any]],
        jobs_by_node: Mapping[str, Mapping[str, Any]],
        task_input: Mapping[str, Any],
        control_enabled: bool,
        repeat_member_uuids: set[str],
    ) -> list[WorkflowNode]:
        """编译执行计划中的设备动作节点。

        参数：``ordered_node_uuids`` 保留确定性拓扑顺序，``nodes`` 是计划节点
        索引，``jobs_by_node`` 是持久作业绑定。返回：旧调度节点；协调器所有的
        物料来源解析作业（MaterialSourceResolutionJob）完成校验后跳过。异常：
        未知执行种类、缺作业、空设备或空动作合同时抛稳定编译错误并失败关闭。
        """

        compiled: list[WorkflowNode] = []
        for node_uuid in ordered_node_uuids:
            node = nodes[node_uuid]
            kind = str(node.get("kind") or "").strip()
            job = jobs_by_node.get(node_uuid)
            is_repeat_template = node_uuid in repeat_member_uuids
            if job is None and not is_repeat_template:
                raise WorkflowSpecCompilationError(
                    "missing_workflow_node_job",
                    f"执行计划节点缺少持久作业身份：{node_uuid}",
                )
            if kind in {"material_source", "workflow_input", "workflow_output"}:
                if job is None:
                    raise WorkflowSpecCompilationError(
                        "missing_workflow_node_job",
                        f"协调器计划节点缺少持久作业身份：{node_uuid}",
                    )
                # ``executor_kind`` 明确证明该作业属于协调器，不能伪装成动作节点。
                if str(job.get("executor_kind") or "") != kind:
                    raise WorkflowSpecCompilationError(
                        "unsupported_executor_kind",
                        f"协调器作业执行种类非法：{node_uuid}",
                    )
                if kind == "material_source" and node.get("runtime_material_source"):
                    compiled.append(WorkflowNode(
                        id=node_uuid, job_id=str(job["uuid"]),
                        result_name=str(node.get("result_name") or ""),
                        device_id="scheduler-control", action_name="material_source",
                        executor_kind="material_source", node_type="material_source",
                        param=deepcopy(dict(node.get("param") or {})),
                    ))
                continue
            if kind == "repeat_until":
                if not control_enabled or (job is None and not is_repeat_template):
                    raise WorkflowSpecCompilationError(
                        "unsupported_executor_kind",
                        f"执行计划不能激活 RepeatUntil 作业：{node_uuid}",
                    )
                if (
                    job is not None
                    and str(job.get("executor_kind") or "") != "repeat_until"
                ):
                    raise WorkflowSpecCompilationError(
                        "unsupported_executor_kind",
                        f"RepeatUntil 作业执行种类非法：{node_uuid}",
                    )
                planned_param = node.get("control_region")
                if not isinstance(planned_param, Mapping):
                    raise WorkflowSpecCompilationError(
                        "invalid_execution_plan",
                        f"RepeatUntil 区域冻结参数无效：{node_uuid}",
                    )
                repeat_param = deepcopy(dict(planned_param))
                repeat_variables: dict[str, Any] = {}
                for source_group in (
                    repeat_param.get("initial_carry", {}),
                    repeat_param.get("bindings", {}),
                ):
                    if not isinstance(source_group, Mapping):
                        raise WorkflowSpecCompilationError(
                            "invalid_execution_plan",
                            f"RepeatUntil 输入来源无效：{node_uuid}",
                        )
                    for binding in source_group.values():
                        if (
                            isinstance(binding, Mapping)
                            and binding.get("kind") == "workflow_input"
                        ):
                            parameter = str(binding.get("parameter") or "")
                            if parameter in task_input:
                                repeat_variables[parameter] = deepcopy(
                                    task_input[parameter]
                                )
                repeat_param["variables"] = repeat_variables
                compiled.append(
                    WorkflowNode(
                        id=node_uuid,
                        result_name=str(node.get("result_name") or ""),
                        job_id=(
                            canonical_uuid(
                                job.get("uuid"),
                                "invalid_job_identity",
                                f"jobs[{node_uuid}].uuid",
                            )
                            if job is not None
                            else ""
                        ),
                        device_id="scheduler-control",
                        action_name="repeat_until",
                        action_type="repeat_until",
                        param=repeat_param,
                        executor_kind="repeat_until",
                        node_type="repeat_until",
                    )
                )
                continue
            if kind == "condition":
                if not control_enabled:
                    raise WorkflowSpecCompilationError(
                        "unsupported_executor_kind",
                        f"版本 1 执行计划不支持条件作业：{node_uuid}",
                    )
                if job is not None and str(job.get("executor_kind") or "") != "condition":
                    raise WorkflowSpecCompilationError(
                        "unsupported_executor_kind",
                        f"条件作业执行种类非法：{node_uuid}",
                    )
                job_uuid = (
                    canonical_uuid(
                        job.get("uuid"),
                        "invalid_job_identity",
                        f"jobs[{node_uuid}].uuid",
                    )
                    if job is not None
                    else ""
                )
                planned_param = node.get("control_region")
                public_param = node.get("param")
                job_param = job.get("param", {}) if job is not None else {}
                if (
                    not isinstance(planned_param, Mapping)
                    or not isinstance(public_param, Mapping)
                    or dict(public_param) != dict(planned_param)
                    or not isinstance(job_param, Mapping)
                ):
                    raise WorkflowSpecCompilationError(
                        "invalid_execution_plan",
                        f"条件区域冻结参数不一致：{node_uuid}",
                    )
                # 分支拓扑和表达式只读冻结的 control_region；Job.param 不能覆盖
                # 控制结构，否则会与已经冻结的 dependency_only 边分叉。
                condition_param = deepcopy(dict(planned_param))
                variables = condition_param.get("variables", {})
                bindings = condition_param.get("bindings")
                if not isinstance(variables, Mapping):
                    raise WorkflowSpecCompilationError(
                        "invalid_execution_plan",
                        f"条件变量必须是对象：{node_uuid}",
                    )
                expression_names = self._condition_variable_names(condition_param)
                if (
                    not isinstance(bindings, Mapping)
                    or set(bindings) != expression_names
                ):
                    raise WorkflowSpecCompilationError(
                        "invalid_execution_plan",
                        f"条件变量与绑定不一致：{node_uuid}",
                    )
                condition_variables: dict[str, Any] = {}
                for name, binding in bindings.items():
                    if not isinstance(binding, Mapping):
                        raise WorkflowSpecCompilationError(
                            "invalid_execution_plan",
                            f"条件变量绑定必须是对象：{node_uuid}",
                        )
                    binding_kind = binding.get("kind")
                    if binding_kind == "workflow_input":
                        parameter = str(binding.get("parameter") or "")
                        if parameter in task_input:
                            condition_variables[str(name)] = deepcopy(
                                task_input[parameter]
                            )
                    elif binding_kind != "node_result":
                        raise WorkflowSpecCompilationError(
                            "invalid_execution_plan",
                            f"条件变量绑定类型无效：{node_uuid}",
                        )
                condition_param["variables"] = condition_variables
                compiled.append(
                    WorkflowNode(
                        id=node_uuid,
                        result_name=str(node.get("result_name") or ""),
                        job_id=job_uuid,
                        device_id="scheduler-control",
                        action_name="evaluate_condition",
                        action_type="condition",
                        param=condition_param,
                        executor_kind="condition",
                        node_type="condition",
                    )
                )
                continue
            if kind == "material_io":
                if job is None or str(job.get("executor_kind") or "") != "material_io":
                    raise WorkflowSpecCompilationError(
                        "unsupported_executor_kind",
                        f"material_io 作业执行种类非法：{node_uuid}",
                    )
                planned_param = node.get("param")
                confirmation = node.get("manual_confirmation")
                if (
                    not isinstance(planned_param, Mapping)
                    or not isinstance(confirmation, Mapping)
                    or confirmation.get("timeout_seconds")
                    != planned_param.get("timeout_seconds")
                ):
                    raise WorkflowSpecCompilationError(
                        "invalid_execution_plan",
                        f"material_io 人工确认合同无效：{node_uuid}",
                    )
                job_param = job.get("param") or {}
                if not isinstance(job_param, Mapping):
                    raise WorkflowSpecCompilationError(
                        "invalid_job_param",
                        f"material_io 作业参数必须是对象：{node_uuid}",
                    )
                # 冻结计划里的 material 在来源准入前是 None。来源节点成功后，
                # 实际 UUID 只写在 Job.param.material；来源边随后离开调度图，
                # 这里必须带回该绑定。已在计划中冻结的物料引用不被作业空值覆盖。
                compiled_param = deepcopy(dict(planned_param))
                job_material = job_param.get("material")
                if (
                    not self._is_material_reference(compiled_param.get("material"))
                    and self._is_material_reference(job_material)
                ):
                    compiled_param["material"] = deepcopy(dict(job_material))
                compiled.append(
                    WorkflowNode(
                        id=node_uuid,
                        result_name=str(node.get("result_name") or ""),
                        job_id=canonical_uuid(
                            job.get("uuid"),
                            "invalid_job_identity",
                            f"jobs[{node_uuid}].uuid",
                        ),
                        device_id="scheduler-control",
                        action_name="material_io",
                        action_type="material_io",
                        param=compiled_param,
                        executor_kind="material_io",
                        node_type="material_io",
                        manual_confirmation=deepcopy(dict(confirmation)),
                        resource_plan_id=str(node.get("resource_plan_id") or ""),
                        resource_interval_ids=[
                            str(value)
                            for value in (node.get("resource_interval_ids") or [])
                        ],
                        resource_acquire_set_id=str(
                            node.get("resource_acquire_set_id") or ""
                        ),
                    )
                )
                continue
            if kind not in {"device_action", "material_transfer", "manual_confirm", "workflow_command"}:
                raise WorkflowSpecCompilationError(
                    "unsupported_executor_kind", f"旧调度器不支持执行种类：{kind}"
                )
            if (
                kind == "material_transfer"
                and job is not None
                and str(job.get("executor_kind") or "") != "material_transfer"
            ):
                raise WorkflowSpecCompilationError(
                    "unsupported_executor_kind",
                    f"物料转移作业执行种类非法：{node_uuid}",
                )
            job_uuid = (
                canonical_uuid(
                    job.get("uuid"), "invalid_job_identity", f"jobs[{node_uuid}].uuid"
                )
                if job is not None
                else ""
            )
            device_id = str(node.get("device_id") or "").strip()
            raw_device_selector = node.get("device_selector") or {}
            if not isinstance(raw_device_selector, Mapping):
                raise WorkflowSpecCompilationError(
                    "invalid_device_selector",
                    f"动态设备选择器必须是对象：{node_uuid}",
                )
            device_selector = deepcopy(dict(raw_device_selector))
            dispatches_device_action = kind in {
                "device_action",
                "material_transfer",
                "manual_confirm",
            }
            if dispatches_device_action:
                if not device_id and not device_selector:
                    raise WorkflowSpecCompilationError(
                        "invalid_executor_binding",
                        f"设备动作缺少执行器选择：{node_uuid}",
                    )
            action_name = str(node.get("action_name") or "").strip()
            action_type = str(node.get("action_type") or "").strip()
            if dispatches_device_action:
                if not action_name or not action_type:
                    raise WorkflowSpecCompilationError(
                        "invalid_action_contract", f"设备动作合同不完整：{node_uuid}"
                    )
            planned_param = node.get("param", {})
            if not isinstance(planned_param, Mapping):
                raise WorkflowSpecCompilationError(
                    "invalid_execution_plan", f"计划节点参数必须是对象：{node_uuid}"
                )
            resolved_planned_param = deepcopy(dict(planned_param))
            raw_input_bindings = node.get("input_bindings", {})
            planned_inputs = node.get("inputs", [])
            if not isinstance(raw_input_bindings, Mapping) or not isinstance(
                planned_inputs, Sequence
            ):
                raise WorkflowSpecCompilationError(
                    "invalid_execution_plan", f"计划节点输入绑定无效：{node_uuid}"
                )
            inputs_by_handle = {
                str(item.get("handle_uuid") or ""): str(item.get("data_key") or "")
                for item in planned_inputs
                if isinstance(item, Mapping)
            }
            # 库位输入已经在 Task 创建时冻结为候选 UUID，不能再次从公共输入
            # 注入显式选择器；否则返回原库位时会与 target_site_group 冲突。
            policy = node.get("execution_policy") or {}
            frozen_site_parameters = {
                str(selector.get("parameter") or "")
                for selector in node.get("site_selectors", [])
                if isinstance(selector, Mapping)
                and isinstance(policy.get("target_site_selection"), Mapping)
                and policy.get("target_site_group")
            }
            for handle_uuid, binding in raw_input_bindings.items():
                if not isinstance(binding, Mapping):
                    raise WorkflowSpecCompilationError(
                        "invalid_execution_plan", f"工作流输入绑定无效：{node_uuid}"
                    )
                parameter = str(binding.get("parameter") or "")
                data_key = inputs_by_handle.get(str(handle_uuid), "")
                if parameter not in task_input or not data_key:
                    raise WorkflowSpecCompilationError(
                        "invalid_execution_plan", f"工作流输入绑定无法解析：{node_uuid}"
                    )
                if data_key not in frozen_site_parameters:
                    resolved_planned_param[data_key] = deepcopy(task_input[parameter])
            job_param = job.get("param", {}) if job is not None else {}
            if not isinstance(job_param, Mapping):
                raise WorkflowSpecCompilationError(
                    "invalid_job_param", f"作业最终参数必须是对象：{job_uuid}"
                )
            requirements = self._material_requirements(node, node_uuid=node_uuid)
            param_schema = (
                self._param_schema(node, node_uuid=node_uuid)
                if dispatches_device_action
                else None
            )
            raw_node_metadata = node.get("meta_data")
            raw_unilab_metadata = (
                raw_node_metadata.get("unilab")
                if isinstance(raw_node_metadata, Mapping)
                and isinstance(raw_node_metadata.get("unilab"), Mapping)
                else {}
            )
            raw_passthrough_handles = raw_unilab_metadata.get(
                "material_passthrough_handles", {}
            )
            # 发布合同记录的是模板 Handle UUID，而执行计划为每个节点物化
            # 新的运行 Handle UUID。必须在编译边界完成映射，否则运行态 DAG
            # 找不到透传端口，后继节点会被错误地标成参数待定。
            runtime_handle_by_template = {
                str(raw_handle.get("template_handle_uuid")): str(raw_handle.get("uuid"))
                for raw_handle in handles.values()
                if str(raw_handle.get("node_uuid") or raw_handle.get("node_id") or "")
                == node_uuid
                and str(raw_handle.get("template_handle_uuid") or "").strip()
                and str(raw_handle.get("uuid") or "").strip()
            }

            def runtime_handle_uuid(value: Any) -> str:
                key = str(value or "").strip()
                return runtime_handle_by_template.get(key, key)

            passthrough_handles = (
                {
                    runtime_handle_uuid(output_uuid): runtime_handle_uuid(input_uuid)
                    for output_uuid, input_uuid in raw_passthrough_handles.items()
                    if str(output_uuid).strip() and str(input_uuid).strip()
                }
                if isinstance(raw_passthrough_handles, Mapping)
                else {}
            )
            compiled.append(
                WorkflowNode(
                    id=node_uuid,
                    result_name=str(node.get("result_name") or ""),
                    job_id=job_uuid,
                    device_id=device_id,
                    device_material_uuid=str(node.get("material_uuid") or "").strip(),
                    device_selector=device_selector,
                    action_name=action_name,
                    action_type=action_type,
                    param=self._merge_final_param(resolved_planned_param, job_param),
                    param_schema=param_schema,
                    executor_kind=kind,
                    execution_policy=deepcopy(dict(node.get("execution_policy") or {})),
                    action_resource_contract=deepcopy(
                        dict(node.get("action_resource_contract") or {})
                    ),
                    node_type=(
                        "manual_confirm" if kind == "manual_confirm" else "ILab"
                    ),
                    manual_confirmation=deepcopy(
                        dict(node.get("manual_confirmation") or {})
                    ),
                    disabled=False,
                    always_free=bool(node.get("always_free", False)),
                    material_requirements=requirements,
                    carry_bindings=deepcopy(dict(node.get("carry_bindings") or {})),
                    resource_plan_id=str(node.get("resource_plan_id") or ""),
                    resource_interval_ids=[
                        str(value)
                        for value in (node.get("resource_interval_ids") or [])
                    ],
                    resource_acquire_set_id=str(
                        node.get("resource_acquire_set_id") or ""
                    ),
                    material_passthrough_handles=passthrough_handles,
                )
            )
        return compiled

    @staticmethod
    def _repeat_members(nodes: Mapping[str, Mapping[str, Any]]) -> set[str]:
        """返回所有冻结 RepeatUntil 区域拥有的模板节点身份。"""

        result: set[str] = set()
        for node in nodes.values():
            if str(node.get("kind") or "") != "repeat_until":
                continue
            region = node.get("control_region")
            members = region.get("node_uuids") if isinstance(region, Mapping) else None
            if not isinstance(members, Sequence) or isinstance(members, (str, bytes)):
                raise WorkflowSpecCompilationError(
                    "invalid_execution_plan", "RepeatUntil 模板成员必须是数组"
                )
            result.update(str(value) for value in members)
        return result

    @staticmethod
    def _compile_repeat_regions(
        *,
        nodes: Mapping[str, Mapping[str, Any]],
        compiled_nodes: Sequence[WorkflowNode],
        compiled_edges: Sequence[WorkflowEdge],
        compiled_handles: Sequence[Handle],
    ) -> dict[str, RepeatUntilRegion]:
        """把计划中的冻结循环体从顶层 DAG 分离成惰性物化模板。"""

        compiled_by_id = {node.id: node for node in compiled_nodes}
        handle_by_uuid = {handle.uuid: handle for handle in compiled_handles}
        repeat_uuids = {
            node_uuid
            for node_uuid, node in nodes.items()
            if str(node.get("kind") or "") == "repeat_until"
        }

        def nearest_repeat_owner(node_uuid: str) -> str | None:
            visited: set[str] = set()
            parent = nodes[node_uuid].get("parent_uuid")
            while isinstance(parent, str) and parent and parent not in visited:
                if parent in repeat_uuids:
                    return parent
                visited.add(parent)
                candidate = nodes.get(parent)
                parent = candidate.get("parent_uuid") if candidate is not None else None
            return None

        owner_by_node = {
            node_uuid: nearest_repeat_owner(node_uuid) for node_uuid in nodes
        }

        def is_descendant_of(node_uuid: str, region_uuid: str) -> bool:
            visited: set[str] = set()
            current: str | None = node_uuid
            while current is not None and current not in visited:
                visited.add(current)
                parent = nodes[current].get("parent_uuid")
                if parent == region_uuid:
                    return True
                current = parent if isinstance(parent, str) and parent in nodes else None
            return False

        all_regions: dict[str, RepeatUntilRegion] = {}
        for region_uuid, planned_node in nodes.items():
            if str(planned_node.get("kind") or "") != "repeat_until":
                continue
            control_region = planned_node.get("control_region")
            raw_members = (
                control_region.get("node_uuids")
                if isinstance(control_region, Mapping)
                else None
            )
            if not isinstance(raw_members, Sequence) or isinstance(
                raw_members, (str, bytes)
            ):
                raise WorkflowSpecCompilationError(
                    "invalid_execution_plan", "RepeatUntil 模板成员必须是数组"
                )
            declared_members = {str(value) for value in raw_members}
            if not declared_members <= set(compiled_by_id):
                raise WorkflowSpecCompilationError(
                    "invalid_execution_plan", "RepeatUntil 模板引用未知节点"
                )
            member_uuids = {
                node_uuid
                for node_uuid, owner_uuid in owner_by_node.items()
                if owner_uuid == region_uuid
            }
            if not member_uuids or any(
                not is_descendant_of(node_uuid, region_uuid)
                for node_uuid in declared_members
            ):
                raise WorkflowSpecCompilationError(
                    "invalid_execution_plan", "RepeatUntil 模板成员归属无效"
                )
            all_regions[region_uuid] = RepeatUntilRegion(
                control_node_id=region_uuid,
                nodes=[
                    deepcopy(compiled_by_id[node_uuid])
                    for node_uuid in compiled_by_id
                    if node_uuid in member_uuids
                ],
                edges=[
                    deepcopy(edge)
                    for edge in compiled_edges
                    if edge.source_node_id in member_uuids
                    and edge.target_node_id in member_uuids
                ],
                handles=[
                    deepcopy(handle)
                    for handle in compiled_handles
                    if handle.node_id in member_uuids
                ],
                inbound_bindings=WorkflowSpecCompiler._inbound_value_bindings(
                    member_uuids=member_uuids,
                    compiled_edges=compiled_edges,
                    handle_by_uuid=handle_by_uuid,
                ),
            )
        for child_uuid, child_region in all_regions.items():
            owner_uuid = owner_by_node.get(child_uuid)
            if owner_uuid is not None:
                all_regions[owner_uuid].repeat_regions[child_uuid] = child_region
        return {
            region_uuid: region
            for region_uuid, region in all_regions.items()
            if owner_by_node.get(region_uuid) is None
        }

    @staticmethod
    def _inbound_value_bindings(
        *,
        member_uuids: set[str],
        compiled_edges: Sequence[WorkflowEdge],
        handle_by_uuid: Mapping[str, Handle],
    ) -> dict[str, list[dict[str, Any]]]:
        """收集从区域外节点指向循环体动作的传参边。

        参数：``member_uuids`` 是本区域模板成员，``compiled_edges`` 是全部计划边，
        ``handle_by_uuid`` 覆盖全部可派发节点的连接点。返回：按目标模板节点索引
        的绑定列表；过滤规则与 ``WorkflowRun`` 的父节点传参边完全一致（来源必须是
        执行器输出、两端都不是 ready、都带数据键），只表达顺序的边不收。异常：无。
        """

        bindings: dict[str, list[dict[str, Any]]] = {}
        for edge in compiled_edges:
            if edge.target_node_id not in member_uuids or edge.source_node_id in member_uuids:
                continue
            source = handle_by_uuid.get(edge.source_handle_uuid)
            target = handle_by_uuid.get(edge.target_handle_uuid)
            if source is None or target is None:
                continue
            if (
                source.data_source != "executor"
                or source.handle_key == "ready"
                or source.data_key == ""
            ):
                continue
            if target.handle_key == "ready" or target.data_key == "":
                continue
            bindings.setdefault(edge.target_node_id, []).append(
                {
                    "source_node_id": edge.source_node_id,
                    "source_data_key": source.data_key,
                    "target_data_key": target.data_key,
                }
            )
        return bindings

    @staticmethod
    def _condition_variable_names(param: Mapping[str, Any]) -> set[str]:
        """收集条件分支表达式中所有结构化变量名。"""

        result: set[str] = set()

        def visit(value: Any) -> None:
            if isinstance(value, Mapping):
                if set(value) == {"var"} and isinstance(value.get("var"), str):
                    result.add(str(value["var"]))
                    return
                for child in value.values():
                    visit(child)
            elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                for child in value:
                    visit(child)

        visit(param.get("branches"))
        return result

    @staticmethod
    def _material_requirements(
        node: Mapping[str, Any], *, node_uuid: str
    ) -> list[MaterialRequirement]:
        """读取计划已冻结的短期物料需求。

        参数：``node`` 是设备动作计划，``node_uuid`` 是诊断身份。返回：
        遗留库存预留（inventory_reservation）入口需要的需求值对象；
        它不是任务物料预留（TaskMaterialReservation）。异常：结构非法时
        抛计划错误；不在编译时重新遍历物料来源（MaterialSource）或查询库存。
        """

        raw_requirements = mapping_sequence(
            node.get("material_requirements", []),
            "invalid_execution_plan",
            f"execution_plan.nodes[{node_uuid}].material_requirements",
        )
        return [
            MaterialRequirement.from_dict(requirement)
            for requirement in raw_requirements
        ]

    @staticmethod
    def _param_schema(
        node: Mapping[str, Any], *, node_uuid: str
    ) -> dict[str, Any] | None:
        """隔离执行计划冻结的动作参数 Schema。

        参数：``node`` 是设备动作计划，``node_uuid`` 是诊断身份。
        返回：可独立使用的冻结动作合同（Action Contract）。异常：
        标准执行计划（ExecutionPlan）中的 Schema 缺失、为空或非对象时，
        以 `invalid_action_contract` 稳定编译错误失败关闭。
        """

        raw_schema = node.get("param_schema")
        if raw_schema is None or not isinstance(raw_schema, Mapping):
            raise WorkflowSpecCompilationError(
                "invalid_action_contract",
                f"设备动作参数 Schema 必须是对象：{node_uuid}",
            )
        return deepcopy(dict(raw_schema))

    @classmethod
    def _merge_final_param(
        cls,
        planned: Mapping[str, Any],
        job: Mapping[str, Any],
    ) -> dict[str, Any]:
        """合并计划参数与作业最终参数并保护稳定物料引用。

        参数：``planned`` 是创建任务时冻结的参数，``job`` 是参数解析器产出的最终
        参数。返回：作业值优先的隔离对象，但作业不得把计划中的 ``{"uuid":
        ...}`` 物料引用或非空物料引用列表删除或替换为空值。异常：
        无；其他值按作业结果覆盖。
        """

        merged: dict[str, Any] = dict(planned)
        for key, job_value in job.items():
            planned_value = planned.get(key)
            if cls._is_material_reference(planned_value):
                if not cls._is_material_reference(job_value):
                    continue
                merged[key] = dict(job_value)
                continue
            if cls._is_material_reference_list(planned_value):
                if not cls._is_material_reference_list(job_value):
                    continue
                merged[key] = [dict(item) for item in job_value]
                continue
            if isinstance(planned_value, Mapping) and isinstance(job_value, Mapping):
                merged[key] = cls._merge_final_param(planned_value, job_value)
                continue
            merged[key] = job_value
        return merged

    @staticmethod
    def _is_material_reference(value: Any) -> bool:
        """判断值是否是稳定物料引用。

        参数：``value`` 是任意参数值。返回：仅含有非空 ``uuid`` 语义的对象为
        真；额外展示字段不影响稳定身份。异常：无。
        """

        return isinstance(value, Mapping) and bool(str(value.get("uuid") or "").strip())

    @classmethod
    def _is_material_reference_list(cls, value: Any) -> bool:
        """判断值是否为非空物料引用列表。

        参数：``value`` 是任意参数值。返回：仅当值是非空数组，且每项
        都是带非空 UUID 的物料引用时为真。异常：无。
        """

        return (
            isinstance(value, Sequence)
            and not isinstance(value, (str, bytes))
            and bool(value)
            and all(cls._is_material_reference(item) for item in value)
        )

    @staticmethod
    def _compile_handles(
        *,
        ordered_handle_uuids: Sequence[str],
        handles: Mapping[str, Mapping[str, Any]],
        active_node_uuids: set[str],
        coordinator_node_uuids: set[str],
        planned_node_uuids: set[str],
    ) -> list[Handle]:
        """编译节点作用域运行连接点（Handle）。

        参数：身份顺序、连接点索引、可派发节点、协调器节点及全部计划节点来自
        同一计划。返回：只含旧调度节点的连接点。异常：拥有者非法或不属于计划时
        抛编译错误，禁止模板身份碰撞。
        """

        compiled: list[Handle] = []
        for handle_uuid in ordered_handle_uuids:
            raw_handle = handles[handle_uuid]
            owner_uuid = canonical_uuid(
                raw_handle.get("node_uuid", raw_handle.get("node_id")),
                "invalid_handle_identity",
                f"execution_plan.handles[{handle_uuid}].node_uuid",
            )
            if owner_uuid not in planned_node_uuids:
                raise WorkflowSpecCompilationError(
                    "edge_handle_identity_mismatch",
                    f"运行连接点引用计划外节点：{owner_uuid}",
                )
            if owner_uuid in coordinator_node_uuids:
                continue
            if owner_uuid not in active_node_uuids:
                raise WorkflowSpecCompilationError(
                    "edge_handle_identity_mismatch",
                    f"运行连接点引用不可派发节点：{owner_uuid}",
                )
            compiled.append(
                Handle(
                    uuid=handle_uuid,
                    data_source=str(raw_handle.get("data_source") or "").strip(),
                    handle_key=str(raw_handle.get("handle_key") or "").strip(),
                    data_key=str(raw_handle.get("data_key") or "").strip(),
                    node_id=owner_uuid,
                    io_type=str(raw_handle.get("io_type") or "").strip(),
                    value_type=str(
                        raw_handle.get("type", raw_handle.get("value_type", "")) or ""
                    ).strip(),
                )
            )
        return compiled

    @staticmethod
    def _compile_edges(
        raw_edges: Sequence[Mapping[str, Any]],
        *,
        active_node_uuids: set[str],
        coordinator_node_uuids: set[str],
        planned_node_uuids: set[str],
        handles: Mapping[str, Mapping[str, Any]],
    ) -> list[WorkflowEdge]:
        """编译执行计划的直接依赖与虚拟旁路依赖。

        参数：``raw_edges`` 是计划边；可派发、协调器和全部计划节点集合划定旧调度
        边界；``handles`` 是节点作用域端点索引。返回：排除协调器边的旧调度边。
        异常：节点或端点引用不一致时抛闭集错误；``dependency_only`` 边允许空
        连接点且不传数据。
        """

        compiled: list[WorkflowEdge] = []
        for index, edge in enumerate(raw_edges):
            edge_uuid = canonical_uuid(
                edge.get("uuid"),
                "invalid_edge_identity",
                f"execution_plan.edges[{index}].uuid",
            )
            source_uuid = canonical_uuid(
                edge.get("source_node_uuid"),
                "invalid_edge_identity",
                f"execution_plan.edges[{index}].source_node_uuid",
            )
            target_uuid = canonical_uuid(
                edge.get("target_node_uuid"),
                "invalid_edge_identity",
                f"execution_plan.edges[{index}].target_node_uuid",
            )
            if (
                source_uuid not in planned_node_uuids
                or target_uuid not in planned_node_uuids
            ):
                raise WorkflowSpecCompilationError(
                    "edge_node_identity_mismatch", "计划边引用计划外节点"
                )
            # 物料来源边是任务物料绑定（TaskMaterialBinding）的冻结审计事实；
            # 参数已经在准入前写入首消费者，旧调度器不得再次调度协调器端点。
            if (
                source_uuid in coordinator_node_uuids
                or target_uuid in coordinator_node_uuids
            ):
                continue
            if (
                source_uuid not in active_node_uuids
                or target_uuid not in active_node_uuids
            ):
                raise WorkflowSpecCompilationError(
                    "edge_node_identity_mismatch", "计划边引用不可派发节点"
                )
            dependency_only = edge.get("dependency_only") is True
            source_handle_uuid, source_handle = WorkflowSpecCompiler._edge_handle(
                edge,
                field="source",
                edge_index=index,
                node_uuid=source_uuid,
                handles=handles,
                optional=dependency_only,
            )
            target_handle_uuid, target_handle = WorkflowSpecCompiler._edge_handle(
                edge,
                field="target",
                edge_index=index,
                node_uuid=target_uuid,
                handles=handles,
                optional=dependency_only,
            )
            compiled.append(
                WorkflowEdge(
                    uuid=edge_uuid,
                    source_node_id=source_uuid,
                    target_node_id=target_uuid,
                    source_handle_uuid=source_handle_uuid,
                    target_handle_uuid=target_handle_uuid,
                    source_handle_key=str(
                        (source_handle or {}).get("handle_key") or ""
                    ),
                    target_handle_key=str(
                        (target_handle or {}).get("handle_key") or ""
                    ),
                )
            )
        return compiled

    @staticmethod
    def _edge_handle(
        edge: Mapping[str, Any],
        *,
        field: str,
        edge_index: int,
        node_uuid: str,
        handles: Mapping[str, Mapping[str, Any]],
        optional: bool,
    ) -> tuple[str, Mapping[str, Any] | None]:
        """读取并校验计划边的一个运行连接点。

        参数：``edge`` 是计划边，``field`` 是 ``source``/``target``，
        ``edge_index`` 是诊断序号，``node_uuid`` 是预期拥有者，``handles`` 是
        端点索引，``optional`` 表示纯依赖边可为空。返回：端点 UUID 与对象。
        异常：身份缺失、端点不存在或拥有者不匹配时抛编译错误。
        """

        raw_uuid = str(edge.get(f"{field}_handle_uuid") or "").strip()
        if optional and not raw_uuid:
            return "", None
        handle_uuid = canonical_uuid(
            raw_uuid,
            "invalid_edge_identity",
            f"execution_plan.edges[{edge_index}].{field}_handle_uuid",
        )
        handle = handles.get(handle_uuid)
        if handle is None or str(handle.get("node_uuid") or "") != node_uuid:
            raise WorkflowSpecCompilationError(
                "edge_handle_identity_mismatch", "计划边端点与节点作用域不一致"
            )
        return handle_uuid, handle


__all__ = ["WorkflowSpecCompilationError", "WorkflowSpecCompiler"]
