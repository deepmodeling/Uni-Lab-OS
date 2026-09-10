"""把标准工作流任务（WorkflowTask）委托给既有本地调度器。"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from unilabos.app.scheduler.dag_state import WorkflowRun
from unilabos.app.scheduler.material_source_resolution import (
    MaterialSourceResolutionCoordinator,
)
from unilabos.app.scheduler.service import EdgeScheduler
from unilabos.workflow.store import StoreConflict, WorkflowStore
from unilabos.workflow.task_runtime_projection import TaskRuntimeProjection
from unilabos.workflow.workflow_spec_compiler import WorkflowSpecCompiler

logger = logging.getLogger(__name__)


class TaskSchedulerBridgeError(RuntimeError):
    """工作流任务不能安全进入本地调度器时使用的稳定桥接错误。"""


class TaskSchedulerBridge:
    """隐藏任务编译、短期物料门禁、生命周期投影和监听器清理。"""

    def __init__(
        self,
        store: WorkflowStore,
        *,
        scheduler: EdgeScheduler,
        compiler: WorkflowSpecCompiler | None = None,
        projection: TaskRuntimeProjection | None = None,
    ) -> None:
        """装配唯一工作流任务调度桥（TaskSchedulerBridge）。

        参数：``store`` 是标准任务/作业写权威；``scheduler`` 是既有本地调度器；
        ``compiler`` 把冻结执行计划（ExecutionPlan）转换为遗留调度规格；
        ``projection`` 把调度生命周期写回标准事实。返回无。异常：构造不访问
        数据库；库存服务只能从 ``scheduler.inventory_service`` 读取，不能另行注入。
        """

        # ``_store`` 是本桥唯一的工作流任务（WorkflowTask）持久事实来源。
        self._store = store
        # ``_scheduler`` 同时持有唯一允许复用的本地库存权威（Inventory Authority）。
        self._scheduler = scheduler
        self._compiler = compiler or WorkflowSpecCompiler()
        self._projection = projection or TaskRuntimeProjection(store)
        # ``_material_sources`` 复用调度器持有的同一库存权威，先于任何普通派发
        # 协调整个任务的物料来源解析作业（MaterialSourceResolutionJob）。
        self._material_sources = MaterialSourceResolutionCoordinator(
            inventory=scheduler.inventory_service,
            projection=self._projection,
        )
        # ``_task_by_job`` 只过滤本桥提交到共享调度器的作业，不承担持久恢复。
        self._task_by_job: dict[str, str] = {}
        # ``_submitted_tasks`` 标识仍可进行准入重试（AdmissionRetry）的本地运行。
        self._submitted_tasks: set[str] = set()
        # ``_admission_pending_tasks`` 只保存尚未交给旧调度器的受阻任务身份。
        self._admission_pending_tasks: set[str] = set()
        self._closed = False
        scheduler.add_admission_retry_listener(self._retry_pending_admissions)
        scheduler.add_job_pre_dispatch_listener(self._on_job_pre_dispatch)
        scheduler.add_job_finished_listener(self._on_job_finished)

    def submit(self, task: Mapping[str, Any]) -> dict[str, Any]:
        """提交已经持久化的工作流任务（WorkflowTask）。

        参数：``task`` 是创建事务返回的标准任务投影。返回：调度同步推进后的标准
        任务/作业聚合。异常：桥关闭、任务身份非法、冻结计划编译失败、缺少库存权威
        或派发前投影冲突时失败关闭；失败不会创建新的任务/作业身份。
        """

        if self._closed:
            raise TaskSchedulerBridgeError("工作流任务调度桥已经关闭")
        # ``task_uuid`` 是本地调度运行、遗留预留和标准任务共用的稳定身份。
        task_uuid = self._required_text(task.get("uuid"), field="task.uuid")
        if task_uuid in self._submitted_tasks:
            return self._aggregate(task_uuid)
        if task_uuid in self._admission_pending_tasks:
            return self._aggregate(task_uuid)
        jobs: list[dict[str, Any]] = []
        registered = False
        try:
            persisted_task = self._store.get_task(task_uuid)
            # ``jobs`` 是创建事务已经确定的工作流节点作业（WorkflowNodeJob）集合。
            jobs = self._store.list_jobs(task_uuid)
            spec = self._compiler.compile(persisted_task, jobs)
            if (
                spec.material_requirements_by_node()
                and self._scheduler.inventory_service is None
            ):
                raise TaskSchedulerBridgeError(
                    "工作流声明了物料需求，但本地调度器没有装配库存权威"
                )
            # 物料来源解析必须先提交完整短期预留和逐来源结果；受阻时不注册普通
            # 动作，也不让任何作业越过物理派发边界。
            material_resolution = self._material_sources.reconcile(
                persisted_task,
                jobs,
            )
            if material_resolution.status == "blocked":
                self._admission_pending_tasks.add(task_uuid)
                return self._aggregate(task_uuid)
            self._admission_pending_tasks.discard(task_uuid)
            # 自动物料来源（MaterialSource）的准入结果已原子写入既有动作作业参数；
            # 重新读取同一作业身份后再编译，禁止派发准入前的空参数快照。
            jobs = self._store.list_jobs(task_uuid)
            spec = self._compiler.compile(persisted_task, jobs)
            if not spec.nodes:
                # 仅来源任务没有普通作业可触发调度器终态清理；协调器必须在返回成功
                # 前消费数量预留并释放仍活跃的短期预留，不能让测试或调用方承担
                # 内部清理。
                self._material_sources.consume_terminal_reservations(
                    task_uuid,
                    [
                        str(job.get("workflow_node_uuid") or "")
                        for job in jobs
                        if job.get("executor_kind") == "material_source"
                    ],
                )
                self._material_sources.release_terminal_reservations(
                    task_uuid,
                    reason="workflow_succeeded",
                )
                return self._aggregate(task_uuid)

            dispatch_job_uuids = {node.job_id for node in spec.nodes}
            for job in jobs:
                # ``job_uuid`` 是监听器回调与标准持久作业之间的稳定路由身份。
                job_uuid = self._required_text(job.get("uuid"), field="jobs[].uuid")
                if job_uuid not in dispatch_job_uuids:
                    continue
                self._task_by_job[job_uuid] = task_uuid
            self._submitted_tasks.add(task_uuid)
            registered = True
            submission = self._scheduler.submit_workflow(spec)
            # ``scheduler_state`` 是内部等料或运行状态；投影层负责限制 wire 状态。
            scheduler_state = self._required_text(
                submission.get("state"), field="scheduler.state"
            )
            return self._projection.project_submission(task_uuid, scheduler_state)
        except Exception as error:
            if self._crossed_dispatch_boundary(jobs):
                raise TaskSchedulerBridgeError(
                    "工作流任务派发结果不确定，已保留在途执行等待明确结果"
                ) from error
            if registered:
                self._cancel_failed_submission(task_uuid, jobs)
            if isinstance(error, TaskSchedulerBridgeError):
                raise
            raise TaskSchedulerBridgeError(
                "工作流任务无法安全提交到本地调度器"
            ) from error

    def retry_admission(self, task_uuid: str) -> dict[str, Any]:
        """对同一待处理任务触发准入重试（AdmissionRetry）。

        参数：``task_uuid`` 是此前已提交但因物料不足等待的稳定任务身份。返回：
        重排后的标准任务/作业聚合。异常：桥关闭、未知任务或调度重排失败时传播；
        本操作绝不创建新任务、作业或执行尝试身份。
        """

        if self._closed:
            raise TaskSchedulerBridgeError("工作流任务调度桥已经关闭")
        normalized_uuid = self._required_text(task_uuid, field="task_uuid")
        if normalized_uuid in self._admission_pending_tasks:
            # 显式准入重试复用同一持久任务/作业身份；先移除内存标记，让 ``submit``
            # 真正重做整图预留，若仍受阻会原样重新登记。
            self._admission_pending_tasks.discard(normalized_uuid)
            return self.submit(self._store.get_task(normalized_uuid))
        if normalized_uuid not in self._submitted_tasks:
            raise TaskSchedulerBridgeError("工作流任务尚未提交到本地调度器")
        self._scheduler.reschedule()
        return self._aggregate(normalized_uuid)

    def step(
        self,
        task_uuid: str,
        *,
        target_node_uuid: str | None = None,
    ) -> dict[str, Any]:
        """让已经提交且暂停的单步任务只派发一个就绪节点。"""

        if self._closed:
            raise TaskSchedulerBridgeError("工作流任务调度桥已经关闭")
        normalized_uuid = self._required_text(task_uuid, field="task_uuid")
        if normalized_uuid not in self._submitted_tasks:
            raise TaskSchedulerBridgeError("工作流任务尚未提交到本地调度器")
        try:
            return self._scheduler.step_workflow(
                normalized_uuid,
                target_node_id=target_node_uuid,
            )
        except ValueError as error:
            raise TaskSchedulerBridgeError(str(error)) from error

    def cancel(self, task_uuid: str) -> dict[str, Any]:
        """取消一个活动任务并原子收敛标准任务/作业状态。"""

        if self._closed:
            raise TaskSchedulerBridgeError("工作流任务调度桥已经关闭")
        normalized_uuid = self._required_text(task_uuid, field="task_uuid")
        task = self._store.get_task(normalized_uuid)
        if task.get("status") == "canceled":
            return self._aggregate(normalized_uuid)
        if task.get("status") not in {"pending", "running"}:
            raise TaskSchedulerBridgeError("工作流任务已经结束，不能取消")

        if normalized_uuid in self._submitted_tasks:
            if not self._scheduler.cancel_workflow(normalized_uuid):
                raise TaskSchedulerBridgeError("本地调度器未找到活动工作流任务")
        self._admission_pending_tasks.discard(normalized_uuid)
        aggregate = self._projection.project_cancellation(normalized_uuid)
        if any(
            job.get("executor_kind") == "material_source"
            for job in aggregate["jobs"]
        ):
            self._material_sources.release_terminal_reservations(
                normalized_uuid,
                reason="workflow_canceled",
            )
        for job in aggregate["jobs"]:
            self._task_by_job.pop(str(job.get("uuid") or ""), None)
        self._submitted_tasks.discard(normalized_uuid)
        return aggregate

    def recover_active_tasks(self) -> list[dict[str, Any]]:
        """恢复没有结果不明作业的活动工作流任务（WorkflowTask）。

        参数：无。返回：已恢复任务的标准聚合列表；包括运行中任务和尚未执行
        首步的 ``pending + paused`` 单步任务。已成功作业仅
        恢复 DAG 返回值，待处理作业才可派发；发现 ``dispatched`` 或
        ``running`` 作业时跳过该任务，禁止重放结果不明的物理动作。
        异常：冻结计划或持久事实不一致时记录后跳过，不阻止其他
        可证明安全的任务恢复。
        """

        if self._closed:
            raise TaskSchedulerBridgeError("工作流任务调度桥已经关闭")
        recovered: list[dict[str, Any]] = []
        for status in ("running", "pending"):
            page = 1
            while True:
                task_page = self._store.list_tasks(
                    page=page,
                    page_size=200,
                    status=status,
                )
                tasks = task_page["items"]
                for task in tasks:
                    if status == "pending" and not (
                        task.get("run_mode") == "step"
                        and task.get("control_status") == "paused"
                    ):
                        continue
                    try:
                        aggregate = self._recover_running_task(task)
                    except Exception:  # noqa: BLE001 - 单任务损坏不影响其他恢复
                        logger.exception(
                            "活动工作流任务无法安全恢复：%s",
                            task.get("uuid"),
                        )
                        continue
                    if aggregate is not None:
                        recovered.append(aggregate)
                if page * 200 >= int(task_page["total"]):
                    break
                page += 1
        return recovered

    def _recover_running_task(
        self,
        task: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        """恢复单个可证明无结果不明作业的运行中任务。"""

        task_uuid = self._required_text(task.get("uuid"), field="task.uuid")
        jobs = self._store.list_jobs(task_uuid)
        uncertain_jobs = [
            job for job in jobs if job.get("status") in {"dispatched", "running"}
        ]
        if uncertain_jobs:
            logger.error(
                "工作流任务 %s 存在 %d 个结果不明作业，禁止自动恢复",
                task_uuid,
                len(uncertain_jobs),
            )
            return None
        source_jobs = [
            job for job in jobs if job.get("executor_kind") == "material_source"
        ]
        ordinary_jobs = [
            job for job in jobs if job.get("executor_kind") != "material_source"
        ]
        if any(job.get("status") != "succeeded" for job in source_jobs):
            raise TaskSchedulerBridgeError("运行中任务存在未完成的物料来源作业")
        if source_jobs:
            # 旧冻结计划可能只记录第一个物理消费者；幂等重放同一准入结果会从
            # 计划边补齐复合工作流隐式透传的其他待处理动作参数，不再次查询或
            # 占用库存（Inventory）。
            bindings: dict[str, Mapping[str, str]] = {}
            for source_job in source_jobs:
                return_info = source_job.get("return_info")
                material = (
                    return_info.get("material")
                    if isinstance(return_info, Mapping)
                    else None
                )
                if not isinstance(material, Mapping):
                    raise TaskSchedulerBridgeError("物料来源成功作业缺少绑定结果")
                source_node_uuid = self._required_text(
                    source_job.get("workflow_node_uuid"),
                    field="material_source_job.workflow_node_uuid",
                )
                bindings[source_node_uuid] = material
            self._projection.project_material_source_admission(task_uuid, bindings)
            jobs = self._store.list_jobs(task_uuid)
            ordinary_jobs = [
                job
                for job in jobs
                if job.get("executor_kind") != "material_source"
            ]
        if any(
            job.get("status") not in {"pending", "succeeded"}
            for job in ordinary_jobs
        ):
            raise TaskSchedulerBridgeError("运行中任务包含不可恢复的作业终态")
        pending_jobs = [job for job in ordinary_jobs if job.get("status") == "pending"]
        if not pending_jobs:
            raise TaskSchedulerBridgeError("运行中任务没有待处理作业")

        spec = self._compiler.compile(task, jobs)
        plan = task.get("execution_plan")
        raw_nodes = plan.get("nodes") if isinstance(plan, Mapping) else None
        if not isinstance(raw_nodes, list):
            raise TaskSchedulerBridgeError("执行计划节点必须是数组")
        nodes_by_uuid = {
            str(node.get("uuid") or ""): node
            for node in raw_nodes
            if isinstance(node, Mapping)
        }
        jobs_by_node = {
            str(job.get("workflow_node_uuid") or ""): job
            for job in ordinary_jobs
        }
        completed_results: dict[str, Any] = {}
        recovery_run = WorkflowRun(spec)
        for node in spec.nodes:
            job = jobs_by_node.get(node.id)
            if job is None or job.get("status") != "succeeded":
                continue
            # 按冻结拓扑顺序重建当时最终参数；这只读取已成功
            # 父节点事实，不派发任何动作。
            resolved_param = recovery_run.resolve_params(node.id)
            recovered_result = self._recover_simulated_action_return(
                job,
                nodes_by_uuid.get(node.id, {}),
                resolved_param=resolved_param,
            )
            completed_results[node.id] = recovered_result
            recovery_run.mark_finished(node.id, recovered_result)
        for job in pending_jobs:
            job_uuid = self._required_text(job.get("uuid"), field="job.uuid")
            self._task_by_job[job_uuid] = task_uuid
        self._submitted_tasks.add(task_uuid)
        try:
            self._scheduler.restore_workflow(spec, completed_results)
        except Exception:
            if not self._crossed_dispatch_boundary(jobs):
                self._submitted_tasks.discard(task_uuid)
                for job in pending_jobs:
                    self._task_by_job.pop(str(job.get("uuid") or ""), None)
            raise
        return self._aggregate(task_uuid)

    @staticmethod
    def _recover_simulated_action_return(
        job: Mapping[str, Any],
        plan_node: Mapping[str, Any],
        *,
        resolved_param: Mapping[str, Any] | None = None,
    ) -> Any:
        """为模拟动作回执重建同名输入的物料透传并兼容历史标记。"""

        return_info = job.get("return_info")
        if not isinstance(return_info, Mapping) or not (
            return_info.get("action_mode") == "simulate"
            or return_info.get("test_mode") is True
        ):
            return deepcopy(return_info)
        recovered = deepcopy(dict(return_info))
        param = resolved_param if resolved_param is not None else job.get("param")
        param_schema = plan_node.get("param_schema")
        properties = (
            param_schema.get("properties")
            if isinstance(param_schema, Mapping)
            else None
        )
        result_schema = (
            properties.get("result") if isinstance(properties, Mapping) else None
        )
        result_properties = (
            result_schema.get("properties")
            if isinstance(result_schema, Mapping)
            else None
        )
        if isinstance(param, Mapping) and isinstance(result_properties, Mapping):
            for output_key in result_properties:
                if output_key not in recovered and output_key in param:
                    recovered[output_key] = deepcopy(param[output_key])
        return recovered

    def close(self) -> None:
        """幂等注销本桥的调度生命周期监听器。

        参数：无。返回：无；重复调用不重复注销，关闭后清除仅用于回调过滤的内存
        路由，但不修改任何持久任务、作业或物料预留事实。
        """

        if self._closed:
            return
        self._closed = True
        self._scheduler.remove_admission_retry_listener(
            self._retry_pending_admissions
        )
        self._scheduler.remove_job_pre_dispatch_listener(self._on_job_pre_dispatch)
        self._scheduler.remove_job_finished_listener(self._on_job_finished)
        self._task_by_job.clear()
        self._submitted_tasks.clear()
        self._admission_pending_tasks.clear()

    def _retry_pending_admissions(self) -> None:
        """重试全部尚未注册到旧调度器的物料来源准入。

        参数：无。返回无；每个工作流任务（WorkflowTask）只在本轮重试一次，仍
        受阻时由 ``submit`` 重新登记。异常：存储、准入或调度失败原样传播，禁止
        在公开重排失败时继续派发其他普通动作。
        """

        for task_uuid in tuple(self._admission_pending_tasks):
            self._admission_pending_tasks.discard(task_uuid)
            self.submit(self._store.get_task(task_uuid))

    def _on_job_pre_dispatch(self, dispatching: Mapping[str, Any]) -> None:
        """在物理派发前提交标准作业派发意图。

        参数：``dispatching`` 是既有调度器即将越过执行边界的作业摘要。返回无；
        非本桥作业忽略，持久转换冲突原样抛出并阻止执行适配器调用。
        """

        job_uuid = str(dispatching.get("job_id") or "")
        task_uuid = self._task_by_job.get(job_uuid)
        if task_uuid is None:
            return
        dispatch_task_uuid = self._required_text(
            dispatching.get("workflow_id"), field="dispatching.workflow_id"
        )
        if dispatch_task_uuid != task_uuid:
            raise StoreConflict(f"派发作业与任务身份不一致：{job_uuid}")
        resolved_args = dispatching.get("resolved_args")
        if not isinstance(resolved_args, Mapping):
            raise StoreConflict(f"派发作业缺少最终解析参数：{job_uuid}")
        self._projection.project_pre_dispatch(
            task_uuid=task_uuid,
            job_uuid=job_uuid,
            resolved_param=resolved_args,
        )

    def _on_job_finished(
        self,
        job_uuid: str,
        success: bool,
        ret_value: Any,
        suc_type: str,
    ) -> None:
        """把既有调度器明确结果投影为标准任务/作业终态。

        参数：``job_uuid`` 是稳定作业身份；``success`` 是明确成功标志；
        ``ret_value`` 是设备结果；``suc_type`` 是遗留人工处理分类。返回无；非本桥
        作业忽略，投影冲突传播给调度器记录，绝不触发物理重做。
        """

        task_uuid = self._task_by_job.get(job_uuid)
        if task_uuid is None:
            return
        # ``return_info`` 保持标准对象字段；标量结果使用明确包装键。
        return_info = (
            dict(ret_value)
            if isinstance(ret_value, Mapping)
            else ({"return_value": ret_value} if ret_value is not None else {})
        )
        # ``error_info`` 只在明确失败时记录稳定代码与人工决策来源。
        error_info: list[dict[str, Any]] = []
        if not success:
            error_info = [
                {
                    "code": "legacy_edge_scheduler_action_failed",
                    "message": "设备动作执行失败",
                    "suc_type": suc_type,
                }
            ]
        aggregate = self._projection.project_job_finished(
            job_uuid=job_uuid,
            scheduler_state="success" if success else "failed",
            return_info=return_info,
            error_info=error_info,
        )
        terminal_status = aggregate["task"]["status"]
        source_jobs = [
            job
            for job in aggregate["jobs"]
            if job.get("executor_kind") == "material_source"
        ]
        if terminal_status == "succeeded" and source_jobs:
            # 来源协调作业不越过普通设备 DAG；成功终态时才正式消费其中
            # 的 lot 数量预留，确保设备动作失败前仍可释放库存。
            self._material_sources.consume_terminal_reservations(
                task_uuid,
                [str(job.get("workflow_node_uuid") or "") for job in source_jobs],
            )
        if terminal_status in {"succeeded", "failed"} and source_jobs:
            # 物料来源解析作业不进入旧调度 DAG，因此其任务级短期预留不会被普通
            # 节点逐一消费；业务任务明确成功或失败后由本桥按同一任务身份幂等释放。
            self._material_sources.release_terminal_reservations(
                task_uuid,
                reason=f"workflow_{terminal_status}",
            )
        self._task_by_job.pop(job_uuid, None)
        if task_uuid not in self._task_by_job.values():
            self._submitted_tasks.discard(task_uuid)

    def _aggregate(self, task_uuid: str) -> dict[str, Any]:
        """读取一个标准任务/作业聚合。

        参数：``task_uuid`` 是父任务稳定身份。返回：当前任务投影和有序作业列表。
        异常：任务不存在时传播工作流存储（WorkflowStore）异常。
        """

        return {
            "task": self._store.get_task(task_uuid),
            "jobs": self._store.list_jobs(task_uuid),
        }

    def _cancel_failed_submission(
        self,
        task_uuid: str,
        jobs: list[dict[str, Any]],
    ) -> None:
        """封闭一次未越过执行边界的失败提交。

        参数：``task_uuid`` 是旧调度运行身份；``jobs`` 是本次路由的持久作业集合。
        返回无；尽力取消遗留内存运行并清除监听路由，原始异常由调用方保留。
        """

        try:
            self._scheduler.cancel_workflow(task_uuid)
        except Exception:  # 清理失败不能覆盖原始安全错误
            logger.exception("失败的工作流任务提交无法清理遗留调度运行")
        self._submitted_tasks.discard(task_uuid)
        self._admission_pending_tasks.discard(task_uuid)
        for job in jobs:
            self._task_by_job.pop(str(job.get("uuid") or ""), None)

    def _crossed_dispatch_boundary(self, jobs: list[dict[str, Any]]) -> bool:
        """判断标准作业是否已经越过持久派发边界。

        参数：``jobs`` 是本次提交的既有工作流节点作业（WorkflowNodeJob）集合。
        返回：任一作业已为 ``dispatched`` 或 ``running`` 时为真。异常：存储读取
        故障视为不能证明未派发，保守返回真并禁止取消或清除回调路由。
        """

        try:
            # ``persisted_statuses`` 是物理派发前投影提交后的标准状态集合。
            persisted_statuses = {
                self._store.get_job(str(job.get("uuid") or ""))["status"]
                for job in jobs
            }
        except Exception:  # noqa: BLE001 - 无法证明未派发时必须保守保留在途事实
            return True
        return bool(persisted_statuses & {"dispatched", "running"})

    @staticmethod
    def _required_text(value: Any, *, field: str) -> str:
        """校验桥接必填文本。

        参数：``value`` 是未知输入，``field`` 是稳定诊断字段。返回：去空白文本。
        异常：空值抛出 ``TaskSchedulerBridgeError``。
        """

        normalized = str(value or "").strip()
        if not normalized:
            raise TaskSchedulerBridgeError(f"{field} 不能为空")
        return normalized


__all__ = ["TaskSchedulerBridge", "TaskSchedulerBridgeError"]
