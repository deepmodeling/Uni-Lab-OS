"""协调物料来源解析作业与 gaojing 短期整图预留。"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal

from unilabos.app.scheduler.inventory.domain import (
    InsufficientStock,
    MaterialClaimedByOtherTask,
    MaterialRequirement,
    MaterialSourceAdmissionRequest,
)
from unilabos.app.scheduler.inventory.workflow_quantity import (
    WorkflowQuantityReservationError,
    WorkflowQuantityShortageError,
)
from unilabos.workflow.material_source import MaterialCustodyPolicy
from unilabos.workflow.task_runtime_projection import TaskRuntimeProjection

MaterialSourceResolutionStatus = Literal["not_required", "blocked", "admitted"]


class MaterialSourceResolutionError(RuntimeError):
    """物料来源不能安全完成短期任务物料准入时使用的稳定错误。"""


@dataclass(frozen=True, slots=True)
class MaterialSourceResolution:
    """一次任务级物料来源协调结果。"""

    status: MaterialSourceResolutionStatus
    # ``reason`` 是受阻时可展示的等待原因；运行时来源只因他任务占用而等待时填写。
    reason: str = ""
    blocking_workflow_id: str | None = None


class MaterialSourceResolutionCoordinator:
    """隐藏来源集合校验、整图预留和逐来源结果投影。"""

    def __init__(
        self,
        *,
        inventory: Any,
        projection: TaskRuntimeProjection,
    ) -> None:
        """绑定唯一短期库存权威与任务运行投影。

        参数：``inventory`` 是本地库存权威（Inventory Authority），可为 ``None``；
        ``projection`` 是工作流任务（WorkflowTask）/作业写边界。返回无。异常：
        构造不访问外部状态，缺库存只在任务确有来源时失败关闭。
        """

        # ``_inventory`` 必须与 EdgeScheduler 使用同一实例，禁止第二库存权威。
        self._inventory = inventory
        self._projection = projection

    def reconcile_required_children(
        self,
        task: Mapping[str, Any],
        *,
        read_jobs: Callable[[], Sequence[Mapping[str, Any]]],
    ) -> MaterialSourceResolution:
        """首次锁规划前按真实依赖准入必需子物料，复用库存选择及持久绑定。"""
        plan = task["execution_plan"]
        nodes = {node["uuid"]: node for node in plan["nodes"]}
        conditional = {
            member
            for node in nodes.values() if node.get("kind") == "condition"
            for branch in (node.get("control_region") or {}).get("branches", [])
            for member in branch.get("node_uuids", [])
        }
        predecessors: dict[str, set[str]] = {}
        for edge in plan.get("edges", []):
            predecessors.setdefault(edge["target_node_uuid"], set()).add(edge["source_node_uuid"])

        def required(node: Mapping[str, Any]) -> bool:
            ancestor = node
            seen: set[str] = set()
            while ancestor.get("uuid"):
                identity = ancestor["uuid"]
                preparation = (ancestor.get("meta_data") or {}).get("unilab", {}).get("operation_preparation") or {}
                if (
                    identity in seen or identity in conditional
                    or ancestor.get("kind") == "repeat_until" or preparation.get("deferred")
                ):
                    return False
                seen.add(identity)
                ancestor = nodes.get(ancestor.get("parent_uuid"), {})
            params = node.get("param") or {}
            return (
                node.get("kind") == "material_source"
                and params.get("mode") == "existing"
                and params.get("supports_replenishment") is not True
            )

        pending = {
            identity: node for identity, node in nodes.items()
            if required(node) and node.get("runtime_material_source")
            and (node.get("runtime_mount_binding") or {}).get("source_data_key") == "material"
            and required(nodes.get(node["runtime_mount_binding"]["source_node_uuid"], {}))
        }
        while pending:
            jobs = list(read_jobs())
            completed = {
                job["workflow_node_uuid"] for job in jobs
                if job.get("status") == "succeeded" and job.get("current_attempt", True)
            }
            ready = [
                identity for identity, node in pending.items()
                if (predecessors.get(identity, set()) | {node["runtime_mount_binding"]["source_node_uuid"]}) <= completed
            ]
            if not ready:
                break
            for identity in ready:
                if identity not in completed:
                    result = self.reconcile(task, read_jobs(), runtime_node_uuid=identity)
                    if result.status == "blocked":
                        self._projection.project_material_source_blocked(
                            str(task["uuid"]), reason=result.reason, source_node_ids={identity},
                        )
                        return result
                del pending[identity]
        return MaterialSourceResolution(status="admitted")

    def reconcile(
        self,
        task: Mapping[str, Any],
        jobs: Sequence[Mapping[str, Any]],
        *,
        quantity_allocations: Sequence[Mapping[str, Any]] = (),
        runtime_node_uuid: str | None = None,
        quantity_inventory: Any = None,
    ) -> MaterialSourceResolution:
        """协调一次可重放的短期任务物料准入（TaskMaterialAdmission）。

        参数：``task`` 提供冻结执行计划（ExecutionPlan）和稳定任务身份；``jobs``
        是创建事务持久化的完整工作流节点作业（WorkflowNodeJob）集合。返回：无
        来源、受阻或已准入的闭集结果。异常：计划、作业、选择器或库存装配不完整
        时抛 ``MaterialSourceResolutionError``/``StoreConflict``；只有
        ``InsufficientStock`` 被解释为可重试受阻。
        """

        task_uuid = self._required_text(task.get("uuid"), field="task.uuid")
        plan = task.get("execution_plan")
        if not isinstance(plan, Mapping):
            raise MaterialSourceResolutionError("工作流任务缺少冻结执行计划")
        raw_nodes = plan.get("nodes")
        if not isinstance(raw_nodes, Sequence) or isinstance(raw_nodes, (str, bytes)):
            raise MaterialSourceResolutionError("执行计划节点必须是数组")
        # ``source_nodes`` 是本轮全有或全无预留共同覆盖的全部启用来源责任。
        source_nodes = [
            node
            for node in raw_nodes
            if isinstance(node, Mapping) and node.get("kind") == "material_source"
            and (node.get("uuid") == runtime_node_uuid if runtime_node_uuid
                 else not node.get("runtime_material_source"))
        ]
        if not source_nodes and not quantity_allocations:
            return MaterialSourceResolution(status="not_required")
        self._projection.validate_material_source_context(task_uuid, plan)
        if self._inventory is None:
            raise MaterialSourceResolutionError(
                "工作流声明了物料来源，但本地调度器没有装配库存权威"
            )

        source_node_uuids = {
            self._required_text(node.get("uuid"), field="material_source.uuid")
            for node in source_nodes
        }
        # ``source_jobs`` 证明每个来源都有且只有一个协调器所有的持久执行责任。
        source_jobs = [
            job for job in jobs if job.get("executor_kind") == "material_source"
            and job.get("workflow_node_uuid") in source_node_uuids
            and bool(job.get("current_attempt", True))
        ]
        source_job_nodes = {
            self._required_text(
                job.get("workflow_node_uuid"),
                field="material_source_job.workflow_node_uuid",
            )
            for job in source_jobs
        }
        if (
            len(source_jobs) != len(source_node_uuids)
            or source_job_nodes != source_node_uuids
        ):
            raise MaterialSourceResolutionError("物料来源与解析作业集合不一致")

        admission_requests: list[MaterialSourceAdmissionRequest] = []
        selectors: dict[str, Mapping[str, Any]] = {}
        jobs_by_node = {
            str(job.get("workflow_node_uuid") or ""): job for job in jobs
        }
        for node in source_nodes:
            node_uuid = str(node["uuid"])
            selector, request = self._source_request(node, plan, jobs_by_node)
            selectors[node_uuid] = selector
            admission_requests.append(request)

        try:
            # 库存权威在一个 SQLite 事务内按保管策略完成整组来源准入；任一来源
            # 不足整体回滚，同一任务/来源/attempt 重放保持稳定绑定。
            admit_task = getattr(self._inventory, "admit_task_materials", None)
            if not callable(admit_task):
                raise MaterialSourceResolutionError(
                    "库存权威未实现统一 TaskMaterialAdmission"
                )
            runtime_allocations: list[dict[str, Any]] = []
            def build_quantities(result: Mapping[str, Any]) -> Sequence[Mapping[str, Any]]:
                runtime_allocations.extend(quantity_inventory.prepare_runtime_source_allocations(
                    task, jobs, runtime_node_uuid, result,
                ))
                return runtime_allocations
            options = {"quantity_builder": build_quantities} if runtime_node_uuid and quantity_inventory is not None else {}
            source_attempt = int(source_jobs[0].get("attempt") or 1) if runtime_node_uuid else 1
            if source_attempt > 1:
                # 绑定键记录当前尝试，重试不复用上一尝试已结束的绑定。
                options["attempt"] = source_attempt
            reservation = admit_task(task_uuid, admission_requests, quantity_allocations, **options)
            if runtime_allocations:
                quantity_inventory.persist_runtime_allocations(runtime_allocations)
        except (InsufficientStock, WorkflowQuantityReservationError) as error:
            if runtime_node_uuid and isinstance(error, MaterialClaimedByOtherTask):
                # 匹配物料只是被其他任务占用：不是缺料，不走补料分支，等待释放后重评。
                return MaterialSourceResolution(
                    status="blocked", reason=str(error), blocking_workflow_id=error.blocking_workflow_id
                )
            if runtime_node_uuid:
                if not isinstance(error, (InsufficientStock, WorkflowQuantityShortageError)):
                    raise MaterialSourceResolutionError(f"物料数量检查失败：{error}") from error
                if selectors[runtime_node_uuid].get("supports_replenishment") is True:
                    self._projection.project_material_shortage(str(source_jobs[0]["uuid"]), reason=str(error))
                    return MaterialSourceResolution(status="admitted")
                raise MaterialSourceResolutionError(f"备用物料不足：{error}") from error
            wait_resources_by_node: dict[str, list[dict[str, str]]] = {}
            for node_uuid, selector in selectors.items():
                fixed_uuid = str(selector.get("material_uuid") or "").strip()
                resources = (
                    [{"scope": "material", "material_uuid": fixed_uuid}]
                    if fixed_uuid
                    else []
                )
                wait_resources_by_node[node_uuid] = [
                    dict(resource)
                    for resource in self._inventory.describe_wait_resources(resources)
                ]
            reason = str(error) or "任务所需物料暂不可用"
            if source_nodes:
                self._projection.project_material_source_blocked(
                    task_uuid,
                    reason=reason,
                    wait_resources_by_node=wait_resources_by_node,
                )
            else:
                resources_by_job: dict[str, list[dict[str, str]]] = {}
                for allocation in quantity_allocations:
                    job_uuid = self._required_text(
                        allocation.get("workflow_node_job_uuid"),
                        field="quantity_allocation.workflow_node_job_uuid",
                    )
                    material_uuid = self._required_text(
                        allocation.get("material_uuid"),
                        field="quantity_allocation.material_uuid",
                    )
                    resources_by_job.setdefault(job_uuid, []).extend(
                        dict(resource)
                        for resource in self._inventory.describe_wait_resources(
                            [
                                {
                                    "scope": "material",
                                    "material_uuid": material_uuid,
                                }
                            ]
                        )
                    )
                self._projection.project_quantity_inventory_blocked(
                    task_uuid,
                    reason=reason,
                    wait_resources_by_job=resources_by_job,
                )
            return MaterialSourceResolution(
                status="blocked", reason=reason,
                blocking_workflow_id=(
                    error.blocking_workflow_id if isinstance(error, MaterialClaimedByOtherTask) else None
                ),
            )
        allocations: dict[str, Any] = {}
        allocation_sites: dict[str, Any] = {}
        for result in (reservation,):
            result_allocations = (
                result.get("allocations") if isinstance(result, Mapping) else None
            )
            if not isinstance(result_allocations, Mapping):
                raise MaterialSourceResolutionError("库存解析结果缺少 allocations")
            allocations.update(result_allocations)
            result_sites = result.get("allocation_sites", {})
            if not isinstance(result_sites, Mapping):
                raise MaterialSourceResolutionError(
                    "库存解析结果的 allocation_sites 必须是对象"
                )
            allocation_sites.update(result_sites)
        if not source_nodes:
            self._projection.project_task_material_admission(task_uuid)
            return MaterialSourceResolution(status="admitted")
        bindings: dict[str, dict[str, str | None]] = {}
        for node_uuid, selector in selectors.items():
            fixed_uuid = str(selector.get("material_uuid") or "").strip()
            if fixed_uuid:
                material_uuid = fixed_uuid
            else:
                raw_allocated = (
                    allocations.get(node_uuid)
                )
                if (
                    not isinstance(raw_allocated, Sequence)
                    or isinstance(raw_allocated, (str, bytes))
                    or len(raw_allocated) != 1
                ):
                    raise MaterialSourceResolutionError(
                        "自动物料来源必须取得唯一库存分配结果"
                    )
                material_uuid = self._required_text(
                    raw_allocated[0],
                    field="material_source.allocated_material_uuid",
                )
            node_sites = allocation_sites.get(node_uuid, {})
            if not isinstance(node_sites, Mapping):
                raise MaterialSourceResolutionError("来源库位分配结果必须是对象")
            resolved_site_uuid = str(
                selector.get("site") or node_sites.get(material_uuid) or ""
            ).strip()
            if not resolved_site_uuid and hasattr(
                self._inventory,
                "current_site_uuid",
            ):
                resolved_site_uuid = str(
                    self._inventory.current_site_uuid(material_uuid) or ""
                ).strip()
            bindings[node_uuid] = {
                "uuid": material_uuid,
                "resource_template_uuid": self._required_text(
                    selector.get("resource_template_uuid"),
                    field="material_source.resource_template_uuid",
                ),
                "site_uuid": resolved_site_uuid or None,
                "flow_role": self._required_text(
                    selector.get("flow_role"),
                    field="material_source.flow_role",
                ),
                "custody_policy": self._required_text(
                    selector.get("custody_policy"),
                    field="material_source.custody_policy",
                ),
            }
        if runtime_node_uuid:
            self._projection.project_material_source_admission(task_uuid, bindings, runtime=True)
        else:
            self._projection.project_material_source_admission(task_uuid, bindings)
        return MaterialSourceResolution(status="admitted")

    def _source_request(
        self, node: Mapping[str, Any], plan: Mapping[str, Any], jobs_by_node: Mapping[str, Mapping[str, Any]],
    ) -> tuple[dict[str, Any], MaterialSourceAdmissionRequest]:
        raw_requirements = node.get("material_requirements")
        if not isinstance(raw_requirements, Sequence) or isinstance(
            raw_requirements, (str, bytes)
        ):
            raise MaterialSourceResolutionError("物料来源预留需求必须是数组")
        selector = node.get("param")
        if not isinstance(selector, Mapping):
            raise MaterialSourceResolutionError("物料来源选择器必须是对象")
        selector = deepcopy(dict(selector))
        normalized_requirements = [
            dict(requirement)
            for requirement in raw_requirements
            if isinstance(requirement, Mapping)
        ]
        if selector.get("mount") is None:
            if not node.get("runtime_material_source"):
                raise MaterialSourceResolutionError(
                    "动态 mount 不能进入任务预准入"
                )
            actual_mount_uuid = self._resolve_runtime_mount_uuid(
                node,
                jobs_by_node=jobs_by_node,
            )
            selector["mount"] = {"uuid": actual_mount_uuid}
            for requirement in normalized_requirements:
                requirement["mount_uuid"] = actual_mount_uuid
        requirements = [
            MaterialRequirement.from_dict(requirement)
            for requirement in normalized_requirements
        ]
        if (
            len(requirements) != len(raw_requirements)
            or len(requirements) != 1
        ):
            raise MaterialSourceResolutionError("物料来源必须有且只有一个实例需求")
        resource_template_uuid = self._required_text(
            selector.get("resource_template_uuid"),
            field="material_source.resource_template_uuid",
        )
        custody_policy = self._required_text(
            selector.get("custody_policy"),
            field="material_source.custody_policy",
        )
        if custody_policy not in {member.value for member in MaterialCustodyPolicy}:
            raise MaterialSourceResolutionError("物料来源保管策略不在规范闭集")
        return selector, MaterialSourceAdmissionRequest(
            node_id=str(node["uuid"]),
            resource_template_uuid=resource_template_uuid,
            custody_policy=custody_policy,
            requirement=requirements[0],
            center_source_context=plan.get("center_material_source_context"),
            planned_material_uuid=str(node.get("planned_material_uuid") or ""),
        )

    @classmethod
    def _resolve_runtime_mount_uuid(
        cls,
        source_node: Mapping[str, Any],
        *,
        jobs_by_node: Mapping[str, Mapping[str, Any]],
    ) -> str:
        """从冻结绑定和上游作业结果解析本轮真实挂载容器。"""

        binding = source_node.get("runtime_mount_binding")
        if not isinstance(binding, Mapping):
            raise MaterialSourceResolutionError("动态 mount 缺少冻结数据边绑定")
        source_node_uuid = cls._required_text(
            binding.get("source_node_uuid"),
            field="material_source.runtime_mount_binding.source_node_uuid",
        )
        source_data_key = cls._required_text(
            binding.get("source_data_key"),
            field="material_source.runtime_mount_binding.source_data_key",
        )
        source_job = jobs_by_node.get(source_node_uuid)
        if not isinstance(source_job, Mapping):
            raise MaterialSourceResolutionError("动态 mount 的上游作业不存在")
        result = cls._unwrap_job_return_payload(source_job.get("return_info"))
        value = result.get(source_data_key) if isinstance(result, Mapping) else None
        if value is None and source_data_key == "material":
            value = result
        if not isinstance(value, Mapping):
            raise MaterialSourceResolutionError("动态 mount 的上游 Material 结果无效")
        return cls._required_text(
            value.get("uuid"),
            field="material_source.runtime_mount_uuid",
        )

    @classmethod
    def _unwrap_job_return_payload(cls, return_info: Any) -> Mapping[str, Any] | None:
        """设备动作信封与物料作业扁平结果都归一成业务 payload。"""

        if not isinstance(return_info, Mapping):
            return None
        payload = return_info.get("return_value")
        if isinstance(payload, Mapping):
            return payload
        nested = return_info.get("result")
        if isinstance(nested, Mapping):
            payload = nested.get("return_value")
            if isinstance(payload, Mapping):
                return payload
            raw = nested.get("return_info")
            if isinstance(raw, str) and raw.strip():
                try:
                    decoded = json.loads(raw)
                except json.JSONDecodeError:
                    decoded = None
                if isinstance(decoded, Mapping):
                    payload = decoded.get("return_value")
                    if isinstance(payload, Mapping):
                        return payload
                    return decoded
        return return_info

    def release_terminal_reservations(self, task_uuid: str, *, reason: str) -> None:
        """幂等释放终态任务仍活跃的短期物料预留。

        参数：``task_uuid`` 是已完成准入的工作流任务（WorkflowTask）身份，
        ``reason`` 是稳定终态清理原因。返回：无。异常：库存权威缺失或释放失败时
        原样传播，禁止把仍活跃的预留静默当作已清理。
        """

        if self._inventory is None:
            raise MaterialSourceResolutionError(
                "工作流声明了物料来源，但本地调度器没有装配库存权威"
            )
        self._inventory.release_workflow(task_uuid, reason=reason)

    @staticmethod
    def _required_text(value: Any, *, field: str) -> str:
        """校验协调器使用的必填文本。

        参数：``value`` 是未知边界值；``field`` 是中文诊断路径。返回：去空白
        文本。异常：空值抛 ``MaterialSourceResolutionError``。
        """

        normalized = str(value or "").strip()
        if not normalized:
            raise MaterialSourceResolutionError(f"{field} 不能为空")
        return normalized


__all__ = [
    "MaterialSourceResolution",
    "MaterialSourceResolutionCoordinator",
    "MaterialSourceResolutionError",
    "MaterialSourceResolutionStatus",
]
