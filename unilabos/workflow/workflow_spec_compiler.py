"""把执行计划（ExecutionPlan）纯编译为旧调度器工作流规格。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

from unilabos.app.scheduler.inventory.domain import MaterialRequirement
from unilabos.app.scheduler.models import (
    Handle,
    WorkflowEdge,
    WorkflowNode,
    WorkflowSpec,
)
from unilabos.workflow._workflow_spec_snapshot import (
    WorkflowSpecCompilationError,
    canonical_uuid,
    index_jobs,
    index_objects,
    mapping,
    mapping_sequence,
)
from unilabos.workflow.execution_plan import PLAN_VERSION


class WorkflowSpecCompiler:
    """封装版本化执行计划到旧调度输入的全部确定性转换。"""

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

        task = mapping(task_snapshot, "invalid_task_snapshot", "task_snapshot")
        # ``task_uuid`` 是旧调度运行复用的工作流任务稳定身份。
        task_uuid = canonical_uuid(
            task.get("uuid"), "invalid_task_identity", "task_snapshot.uuid"
        )
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
        version = plan.get("version")
        if isinstance(version, bool) or version != PLAN_VERSION:
            raise WorkflowSpecCompilationError(
                "invalid_execution_plan",
                f"执行计划版本必须是 {PLAN_VERSION}",
            )
        raw_nodes = mapping_sequence(
            plan.get("nodes"),
            "invalid_execution_plan",
            "task_snapshot.execution_plan.nodes",
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
        compiled_nodes = self._compile_nodes(
            ordered_node_uuids=ordered_node_uuids,
            nodes=nodes,
            jobs_by_node=jobs_by_node,
        )
        active_node_uuids = {node.id for node in compiled_nodes}
        # ``coordinator_node_uuids`` 在执行计划中保留图身份，但不会进入旧调度器。
        coordinator_node_uuids = {
            node_uuid
            for node_uuid, node in nodes.items()
            if str(node.get("kind") or "").strip() == "material_source"
        }
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
        return WorkflowSpec(
            workflow_id=task_uuid,
            task_id=task_uuid,
            nodes=compiled_nodes,
            edges=compiled_edges,
            handles=compiled_handles,
            priority=task.get("priority", 1.0),
            lab_id=str(task.get("lab_id") or "").strip(),
            run_mode=str(task.get("run_mode") or plan.get("run_mode") or "normal"),
        )

    def _compile_nodes(
        self,
        *,
        ordered_node_uuids: Sequence[str],
        nodes: Mapping[str, Mapping[str, Any]],
        jobs_by_node: Mapping[str, Mapping[str, Any]],
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
            if job is None:
                raise WorkflowSpecCompilationError(
                    "missing_workflow_node_job",
                    f"执行计划节点缺少持久作业身份：{node_uuid}",
                )
            if kind == "material_source":
                # ``executor_kind`` 明确证明该作业属于协调器，不能伪装成动作节点。
                if str(job.get("executor_kind") or "") != "material_source":
                    raise WorkflowSpecCompilationError(
                        "unsupported_executor_kind",
                        f"物料来源作业执行种类非法：{node_uuid}",
                    )
                continue
            if kind != "device_action":
                raise WorkflowSpecCompilationError(
                    "unsupported_executor_kind", f"旧调度器不支持执行种类：{kind}"
                )
            job_uuid = canonical_uuid(
                job.get("uuid"), "invalid_job_identity", f"jobs[{node_uuid}].uuid"
            )
            device_id = str(node.get("device_id") or "").strip()
            if not device_id:
                raise WorkflowSpecCompilationError(
                    "invalid_executor_binding", f"设备动作缺少固定执行器：{node_uuid}"
                )
            action_name = str(node.get("action_name") or "").strip()
            action_type = str(node.get("action_type") or "").strip()
            if not action_name or not action_type:
                raise WorkflowSpecCompilationError(
                    "invalid_action_contract", f"设备动作合同不完整：{node_uuid}"
                )
            planned_param = node.get("param", {})
            if not isinstance(planned_param, Mapping):
                raise WorkflowSpecCompilationError(
                    "invalid_execution_plan", f"计划节点参数必须是对象：{node_uuid}"
                )
            job_param = job.get("param", {})
            if not isinstance(job_param, Mapping):
                raise WorkflowSpecCompilationError(
                    "invalid_job_param", f"作业最终参数必须是对象：{job_uuid}"
                )
            requirements = self._material_requirements(node, node_uuid=node_uuid)
            param_schema = self._param_schema(node, node_uuid=node_uuid)
            compiled.append(
                WorkflowNode(
                    id=node_uuid,
                    job_id=job_uuid,
                    device_id=device_id,
                    action_name=action_name,
                    action_type=action_type,
                    param=self._merge_final_param(planned_param, job_param),
                    param_schema=param_schema,
                    node_type="ILab",
                    disabled=False,
                    material_requirements=requirements,
                    material_outputs=[
                        dict(output)
                        for output in (node.get("material_outputs") or [])
                        if isinstance(output, Mapping)
                    ],
                    material_preconditions=[
                        dict(item)
                        for item in (node.get("material_preconditions") or [])
                        if isinstance(item, Mapping)
                    ],
                    material_effects=[
                        dict(item)
                        for item in (node.get("material_effects") or [])
                        if isinstance(item, Mapping)
                    ],
                )
            )
        return compiled

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
