"""统一 Backend Scheduler。

WorkflowService 持有 Workflow/Task/节点运行/attempt 事实，本服务在每轮 reconcile 中
同时计算 DAG 就绪性、完整动作/物料锁集合和库存 reservation。只有资源请求进入
``held`` 后才会下发执行；attempt 终态先落 Workflow 事实，再释放资源并重算。

两级身份：DAG 节点键 = 节点运行（``workflow_node_run.uuid``，稳定）；执行器
``job_id``、资源申请 owner、库存 reservation 都以当前 attempt（``workflow_node_job.uuid``）
为键。``retry`` 决策由 store 在同一事务里追加新 attempt，调度器拿到 ``next_job`` 后
为它重新申请资源并下发，DAG 节点不终结。
"""

from __future__ import annotations

import asyncio
import logging
import math
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from copy import deepcopy
from typing import Any, Awaitable, Dict, Optional
from uuid import UUID, uuid5

from unilabos.client.materials.core import MaterialsHTTPError
from unilabos.protocol.materials import InventoryMutation
from unilabos.protocol.runtime.loop import (
    LoopCondition,
    LoopSpec,
    compare as compare_condition,
    loop_context,
    parse_loop_spec,
    substitute_loop_placeholders,
)
from unilabos.protocol.utils.workflow_hierarchy import HierarchyError, lift_edge
from unilabos.registry.action_policy import SUCCESS_TYPE_CANCELLATION, SUCCESS_TYPE_EXECUTION_UNKNOWN
from unilabos.protocol.materials import (
    InventoryReservationCreate,
    InventoryReservationTransition,
    InventoryTaskReservationCreate,
)
from unilabos.server.services.materials.core import MaterialsServiceError
from unilabos.server.backend.execution_queue import JOB_ORIGIN_LOCAL_SCHEDULER
from unilabos.server.backend.scheduler.payloads import build_job_start_payload
from unilabos.server.backend.scheduler.materials import (
    material_uuids_for_parameters,
)
from unilabos.server.backend.scheduler.models import (
    ActionLockClaim,
    MaterialLockClaim,
    SchedulerResourceRequest,
    SchedulerResourceRequestRecord,
)
from unilabos.server.backend.scheduler.parameters import (
    ParamResolveError,
    json_get_exists,
    json_set,
)
from unilabos.server.backend.scheduler.dag.executor import DagWalk
from unilabos.server.backend.scheduler.dag.models import (
    NODE_KIND_LOOP,
    DagEdge,
    DagNode,
    NodeState,
    TaskDag,
)
from unilabos.server.backend.scheduler.dag.runner import RunBodyFn, TaskDagRunner
from unilabos.server.backend.scheduler.resource_manager import (
    ResourceNotFound,
    SchedulerResourceManager,
)
from unilabos.server.services.runtime.workflow.service import WorkflowService

logger = logging.getLogger(__name__)

_RUN_TERMINAL = {"succeeded", "failed", "skipped", "canceled", "timeout"}


def _run_status_to_state(status: str) -> NodeState:
    """节点运行终态 → DAG 节点态：取消不算失败（不 fail-fast、不进失败决策）。"""

    if status in {"succeeded", "skipped"}:
        return NodeState.SUCCESS
    if status == "canceled":
        return NodeState.CANCELLED
    return NodeState.FAILED
# 上一进程留下、必须由人裁决而不能重放的节点运行状态（与 store 口径一致）
_RUN_NEEDS_RECONCILIATION = frozenset({"execution_unknown", "intervention_required"})
# 同一 attempt 的裁决 id 跨进程稳定：再次重启后前端拿着旧 id 仍能提交
_RECONCILIATION_NAMESPACE = UUID("5b1d3f8a-6c2e-4e0b-9f3a-7d4c2a1e8b60")
_RECONCILIATION_OPTIONS = (
    {
        "action": "retry",
        "label": "重试",
        "description": "创建新的执行 attempt 重新下发该动作",
    },
    {
        "action": "skip",
        "label": "跳过",
        "description": "视为已完成但不产生结果，下游节点继续执行",
    },
    {
        "action": "operator_intervention",
        "label": "替换为成功",
        "description": "人工确认动作已经完成，可附带 result 作为该节点的返回值",
    },
    {
        "action": "abort",
        "label": "标记失败",
        "description": "记为失败，任务按失败收敛",
    },
)


_TIMEOUT_EXCEPTION_TYPES = frozenset({"TimeoutException", "ExecutionTimeoutException"})


def _failure_error_info(return_info: Any) -> Dict[str, Any]:
    """失败 attempt 写入节点运行的 ``error_info`` 条目：超时闸门触发的失败单独成码。"""

    info = (return_info or {}).get("error_info") if isinstance(return_info, dict) else None
    if not isinstance(info, dict):
        return {"code": "action_failed"}
    exception_type = str(info.get("exception_type") or "")
    if exception_type not in _TIMEOUT_EXCEPTION_TYPES:
        return {"code": "action_failed"}
    entry: Dict[str, Any] = {
        "code": "action_timeout",
        "exception_type": exception_type,
        "message": str(info.get("error_message") or ""),
    }
    if info.get("timeout_seconds") is not None:
        entry["timeout_seconds"] = info["timeout_seconds"]
    return entry


class BackendSchedulingError(RuntimeError):
    """A persisted execution plan cannot be mapped to the local executor."""


def allocation_arguments(
    items: Any, *, action_schema: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Dict[str, Any]]:
    """把权威的 InventoryAllocation 列表按需求 key 归并成动作参数值。

    - ``material``：一个需求恰好选出一个物料实例，值是 ResourceSlot 引用形态
      ``{"uuid": material_uuid, ...}``，框架在 send_goal 解析为 PLR 实例；
    - ``lot``（按量计量的 inventory_lot 库存）：同一需求可能按 FIFO 拆到多个 lot，
      值给出合计数量与 lot 明细 ``{"quantity", "unit", "lots": [{"lot_uuid", "quantity"}]}``。
    """

    from unilabos.server.services.materials.quantity import convert_quantity

    fields = (action_schema or {}).get("properties", {}).get("goal", {}).get("properties", {})
    arguments: Dict[str, Dict[str, Any]] = {}
    for item in items:
        if item.kind == "material":
            if "x-unilabos-material-lock" in fields.get(item.key, {}):
                arguments[item.key] = {"uuid": item.material_uuid}
                continue
            arguments[item.key] = {
                "key": item.key,
                "kind": "material",
                "uuid": item.material_uuid,
                "template_uuid": item.template_uuid,
            }
            continue
        current = arguments.setdefault(
            item.key,
            {
                "key": item.key,
                "kind": "lot",
                "template_uuid": item.template_uuid,
                "unit": item.unit,
                "quantity": 0.0,
                "lots": [],
            },
        )
        # 预留记录保持各批次原单位；给动作的合计和明细统一为首个批次单位。
        converted = convert_quantity(float(item.quantity or 0), item.unit, current["unit"])
        if converted is None:
            raise BackendSchedulingError(f"库存需求 {item.key} 的批次单位无法合并")
        current["quantity"] = float(current["quantity"]) + converted[0]
        current["lots"].append({"lot_uuid": item.lot_uuid, "quantity": converted[0]})
    return arguments


class BackendScheduler:
    """持久化 WorkflowTask 的唯一 DAG、资源和库存调度权威。

    同时是本机派发 job 的生命周期 owner（``job_origins``）：执行面把失败 attempt 挂起
    等待决策时通知本调度器，attempt 与节点运行随之进入 ``intervention_required``；
    终态经 finished 监听回到 :meth:`_on_executor_finished`。
    """

    job_origins = frozenset({JOB_ORIGIN_LOCAL_SCHEDULER})

    def __init__(
        self,
        workflow: WorkflowService,
        executor: Any,
        *,
        materials_gateway: Any = None,
        resource_manager: Optional[SchedulerResourceManager] = None,
        materials_need_lock_resolver: Optional[
            Callable[[str, str], list[str]]
        ] = None,
        device_state_reader: Optional[Callable[[str], Mapping[str, Any]]] = None,
    ) -> None:
        self.workflow = workflow
        self.executor = executor
        self.materials_gateway = materials_gateway
        self.resources = resource_manager or SchedulerResourceManager()
        from unilabos.server.services.runtime.workflow.execution_claim import ExecutionClaims

        self._execution_claims = ExecutionClaims(workflow)
        self._authority_epoch = self._execution_claims.activate_authority()
        configure_start = getattr(executor, "configure_execution_authority", None)
        if callable(configure_start):
            configure_start(self._execution_claims)
        # 所有遗留占用先于任何任务恢复，不能由任务遍历顺序决定谁先拿锁。
        for request in self._execution_claims.restore_requests():
            if self.resources.acquire(request).status != "held":
                raise BackendSchedulingError("持久资源声明与内存占用冲突，不能启动调度")
        from unilabos.server.services.runtime.workflow.resource_intervals import ResourceIntervals
        self._intervals = ResourceIntervals(workflow)
        self._intervals.recover()
        self._stopping = False
        self._materials_need_lock_resolver = materials_need_lock_resolver
        # while 循环的设备状态条件：device_id -> {field: value}。缺省从执行适配器的
        # 设备状态投影读（telemetry 最新快照）。
        self._device_state_reader = device_state_reader
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._started = threading.Event()
        self._guard = threading.RLock()
        self._runners: Dict[str, TaskDagRunner] = {}
        # 恢复先核实旧副作用，不能依赖新版本仍能解释整张工作流图。
        self._recovery_waiters: Dict[str, tuple[asyncio.AbstractEventLoop, asyncio.Event]] = {}
        self._scheduled: set[str] = set()
        self._control_wakeups: set[str] = set()
        # 已满足 DAG 依赖、尚未获得单点许可的叶节点；此时不申请动作/物料锁。
        self._waiting_step_nodes: Dict[str, tuple[Dict[str, Any], DagNode]] = {}
        # 以下均以节点运行 uuid（DAG 节点键）为键
        self._run_to_task: Dict[str, str] = {}
        self._run_specs: Dict[str, Dict[str, Any]] = {}
        self._run_context: Dict[str, tuple[Dict[str, Any], DagNode]] = {}
        # 以下以当前 attempt 的 job uuid 为键（资源 owner / 执行器 job_id）
        self._job_runs: Dict[str, str] = {}
        self._waiting_resource_jobs: Dict[str, tuple[Dict[str, Any], DagNode]] = {}
        self._waiting_interval_jobs: Dict[str, tuple[Dict[str, Any], DagNode]] = {}
        # 区间准入未通过时的完整资源申请，仅供 /scheduler/resources 观测排队与阻塞者。
        self._waiting_interval_requests: Dict[str, SchedulerResourceRequest] = {}
        self._dispatched_jobs: set[str] = set()
        # 已建立 durable 人工确认单、但尚未收到决策的 attempt。
        self._manual_confirmation_jobs: set[str] = set()
        # 进程重启后执行态未知 / 决策上下文丢失的 attempt：decision_id -> 待裁决记录。
        # 与执行面的失败决策一起从 /error-decisions 暴露，由本调度器直接收敛。
        self._reconciliation_decisions: Dict[str, Dict[str, Any]] = {}
        self._dispatch_pause_reasons: set[str] = set()
        if self._execution_claims.unclaimed_active_jobs():
            self._dispatch_pause_reasons.add("legacy_execution_reconciliation")
        if self._intervals.unadmitted_active_claims():
            self._dispatch_pause_reasons.add("legacy_resource_reconciliation")
        from .inventory_recovery import InventoryRecovery
        self._inventory_recovery = InventoryRecovery(workflow, materials_gateway, self._authority_epoch)
        from .physical_recovery import PhysicalRecovery
        self._physical_recovery = PhysicalRecovery(workflow, materials_gateway, self._authority_epoch)
        self._physical_retrying = False
        self._reconcile_inventory()
        self.executor.add_job_finished_listener(self._on_executor_finished)
        resolver = getattr(self.workflow, "set_manual_confirmation_resolver", None)
        if callable(resolver):
            resolver(self._on_manual_confirmation_decided)
        resource_listener = getattr(self.workflow, "set_resource_change_listener", None)
        if callable(resource_listener):
            resource_listener(self._on_resource_change)
        controller = getattr(self.workflow, "set_task_controller", None)
        if callable(controller):
            controller(self._on_task_control)
        releaser = getattr(self.workflow, "set_task_resource_releaser", None)
        if callable(releaser):
            releaser(self._release_unlocked_task_resources)
        self._release_confirmed_custody_inventory()

    @property
    def dispatch_paused(self) -> bool:
        with self._guard:
            return bool(self._dispatch_pause_reasons)

    @property
    def dispatch_pause_reasons(self) -> tuple[str, ...]:
        with self._guard:
            return tuple(sorted(self._dispatch_pause_reasons))

    def pause_dispatch(self, *, reason: str = "manual") -> None:
        """按来源暂停新 Job；同一来源重复请求幂等，已派发的 Job 不受影响。"""
        with self._guard:
            self._dispatch_pause_reasons.add(reason)

    def resume_dispatch(self, *, reason: str = "manual") -> None:
        """仅解除本来源的暂停；最后一个来源解除后才重算等待集合。"""
        if reason == "legacy_resource_reconciliation" and self._intervals.unadmitted_active_claims():
            return
        if reason == "legacy_execution_reconciliation" and self._execution_claims.unclaimed_active_jobs():
            return
        with self._guard:
            if reason not in self._dispatch_pause_reasons:
                return
            self._dispatch_pause_reasons.remove(reason)
            if self._dispatch_pause_reasons:
                return
        self._reconcile_resources()

    def active_device_job_ids(self) -> list[str]:
        """已获派发许可到终态落账之间的设备作业，包含执行态待裁决项。

        人工确认没有设备执行，不阻止 runtime 排空；排空也不取消这类等待。
        """
        with self._guard:
            return sorted(
                (self._dispatched_jobs - self._manual_confirmation_jobs)
                | {item["job_uuid"] for item in self._reconciliation_decisions.values()}
            )

    def start(self, *, recover: bool = True) -> None:
        with self._guard:
            if self._thread is not None and self._thread.is_alive():
                return
            if self._stopping:
                self._authority_epoch = self._execution_claims.activate_authority()
            self._stopping = False
            self._started.clear()
            self._thread = threading.Thread(
                target=self._run_loop,
                name="BackendScheduler",
                daemon=True,
            )
            self._thread.start()
        if not self._started.wait(timeout=5):
            raise RuntimeError("backend scheduler event loop did not start")
        if recover:
            for task in self.workflow.list_recoverable_workflow_tasks():
                self.submit(str(task["uuid"]))

    def submit(self, task_uuid: str) -> None:
        """Queue a persisted task; duplicate submissions share one active runner."""

        self.start(recover=False)
        assert self._loop is not None
        with self._guard:
            if task_uuid in self._runners or task_uuid in self._scheduled:
                return
            self._scheduled.add(task_uuid)
        future = asyncio.run_coroutine_threadsafe(self.run_task(task_uuid), self._loop)

        def report(done: Any) -> None:
            with self._guard:
                self._scheduled.discard(task_uuid)
                wake = task_uuid in self._control_wakeups
                self._control_wakeups.discard(task_uuid)
            if done.cancelled():
                return
            try:
                done.result()
            except Exception:  # noqa: BLE001 - task state is persisted by run_task
                logger.exception("workflow task %s execution failed", task_uuid)
            # 创建时的 paused 检查与第一条 step 命令可能交错，不能吞掉这次唤醒。
            if wake:
                self.submit(task_uuid)

        future.add_done_callback(report)

    def _on_task_control(self, task_uuid: str) -> None:
        """命令先落库，再在调度线程唤醒原 DAG；不新建任务或独立单点 job。"""
        self.start(recover=False)
        assert self._loop is not None

        def wake() -> None:
            with self._guard:
                active = task_uuid in self._runners
                if not active and task_uuid in self._scheduled:
                    self._control_wakeups.add(task_uuid)
                    return
                waiting = [entry for entry in self._waiting_step_nodes.values()
                           if str(entry[0]["uuid"]) == task_uuid]
            if not active:
                self.submit(task_uuid)
                return
            try:
                self._refresh_pending_run_params(task_uuid)
            except Exception:
                logger.exception("任务 %s 重载未下发节点参数失败", task_uuid)
            if self._task_cancel_requested(task_uuid):
                with self._guard:
                    runner = self._runners.get(task_uuid)
                if runner is not None:
                    runner.stop_dispatching()
                return
            # pause：排队等锁、尚未下发的节点退出队列，等单步 / 恢复后重新申请。
            try:
                self._gate_waiting_resource_jobs(task_uuid)
            except Exception:
                logger.exception("任务 %s 暂停时撤回排队节点失败", task_uuid)
            for task, node in waiting:
                try:
                    self._start_node(task, node)
                except Exception:
                    logger.exception("单点节点 %s 起跑失败", node.node_id)
                    self._notify_start_failure(node.node_id)

        self._loop.call_soon_threadsafe(wake)

    def _refresh_pending_run_params(self, task_uuid: str) -> None:
        """暂停窗口里入口参数可能已改：尚未下发的节点运行按库内 param 重载基础参数。"""

        lister = getattr(self.workflow, "list_workflow_node_runs", None)
        if not callable(lister):
            return
        runs = lister(task_uuid)
        with self._guard:
            for run in runs:
                spec = self._run_specs.get(str(run.get("uuid")))
                if spec is None or run.get("status") != "pending":
                    continue
                if spec.get("current_job_uuid") in self._dispatched_jobs:
                    continue
                spec["base_param"] = dict(run.get("param") or {})

    async def run_task(self, task_uuid: str) -> Dict[str, NodeState]:
        prepared = self.workflow.prepare_workflow_task_execution(task_uuid)
        # waiting_reconciliation：任务可接管，但上一进程在飞的 attempt 要由人裁决；
        # DAG 照常构建，这些节点在起跑时改为开决策（见 _start_node），不重放。
        if prepared["state"] not in {"ready", "waiting_reconciliation"}:
            return {}
        task = prepared["task"]
        runs = prepared["runs"]
        if any(run["status"] in _RUN_NEEDS_RECONCILIATION for run in runs):
            await self._reconcile_before_planning(task, runs)
            task = self.workflow.get_workflow_task(task_uuid)
            runs = self.workflow.list_workflow_node_runs(task_uuid)
            failed = {run["status"] for run in runs} & {"failed", "timeout", "canceled"}
            if failed:
                for run in runs:
                    if run["status"] not in _RUN_TERMINAL:
                        self.workflow.close_workflow_node_run(str(run["uuid"]), status="canceled")
                self.workflow.finish_workflow_task(
                    task_uuid, status="failed" if "failed" in failed else "timeout" if "timeout" in failed else "canceled",
                    error_info=[{"code": "node_execution_failed"}],
                )
                self._release_unconsumed_task_inventory(task_uuid)
                return {}
            # 全部成功也走正常收尾，保留工作流声明输出的求值与校验。
            # 重试产生的新 pending attempt 仍须经过下方完整计划和库存检查。
        try:
            dag, specs = self._build_dag(task, runs)
            self._reserve_task_inventory(task, specs)
        except Exception as exc:
            # 计划不可执行（设备/动作缺失、库存不足…）是调度的正常业务终态：
            # 任务落 failed + plan_not_executable，不当成进程异常向上抛。
            self._fail_unstarted_task(task_uuid, runs, exc)
            self._release_unconsumed_task_inventory(task_uuid)
            self._settle_task_reconciliation(task_uuid)
            if isinstance(exc, (BackendSchedulingError, MaterialsServiceError, MaterialsHTTPError)):
                logger.warning("workflow task %s cannot start: %s", task_uuid, exc)
            else:
                logger.exception("workflow task %s failed while planning", task_uuid)
            return {}

        completed = [
            str(run["uuid"])
            for run in runs
            if run["status"] in {"succeeded", "skipped"}
        ]
        walk = DagWalk(dag, completed=completed)
        runner = TaskDagRunner(
            dag,
            lambda node: self._start_node(task, node),
            on_node_terminal=self._on_node_terminal,
            on_cancel_remaining=lambda: self._cancel_task(task_uuid),
            on_run_loop=lambda node, run_body: self._run_loop_node(task, node, run_body),
            loop=asyncio.get_running_loop(),
            walk=walk,
        )
        with self._guard:
            if task_uuid in self._runners:
                return {}
            self._runners[task_uuid] = runner
            self._run_specs.update(specs)
            for run_uuid in specs:
                self._run_to_task[run_uuid] = task_uuid
        try:
            if self._task_cancel_requested(task_uuid):
                # 取消命令先于起跑（或重启后接管一个取消中的任务）：一个节点也不派发
                runner.stop_dispatching()
            result = await runner.run()
            for run_uuid, state in result.items():
                self._persist_terminal_if_needed(run_uuid, state)
            task_status = (
                "succeeded"
                if result and all(state == NodeState.SUCCESS for state in result.values())
                else (
                    "failed"
                    if any(state == NodeState.FAILED for state in result.values())
                    else "canceled"
                )
            )
            # 节点输出取节点运行投影 = 当前（重试后的）attempt 结果
            output = {
                str(run["workflow_node_uuid"]): dict(run.get("return_info") or {})
                for run in self.workflow.list_workflow_node_runs(task_uuid)
                if run["status"] in {"succeeded", "skipped"}
            }
            self.workflow.finish_workflow_task(
                task_uuid,
                status=task_status,
                output=output,
                error_info=(
                    []
                    if task_status == "succeeded"
                    else [{"code": "node_execution_failed"}]
                ),
            )
            return result
        finally:
            if not self._stopping:
                self._cleanup_task_resources(task_uuid)
                self._release_unconsumed_task_inventory(task_uuid)
            with self._guard:
                self._runners.pop(task_uuid, None)
                for run_uuid in specs:
                    spec = self._run_specs.pop(run_uuid, {})
                    self._run_to_task.pop(run_uuid, None)
                    self._run_context.pop(run_uuid, None)
                    self._waiting_step_nodes.pop(run_uuid, None)
                    for job_uuid in spec.get("job_uuids", ()):
                        self._job_runs.pop(job_uuid, None)
                        self._waiting_resource_jobs.pop(job_uuid, None)
                        self._waiting_interval_jobs.pop(job_uuid, None)
                        self._dispatched_jobs.discard(job_uuid)
                        self._manual_confirmation_jobs.discard(job_uuid)
                for decision_id, pending in list(self._reconciliation_decisions.items()):
                    if pending["task_uuid"] == task_uuid:
                        self._reconciliation_decisions.pop(decision_id, None)
            # 任务终结（含取消）后不再有待裁决 attempt，控制态不能停留在 waiting_reconciliation
            self._settle_task_reconciliation(task_uuid)

    def stop(self) -> None:
        # 停止调度进程不等于取消实验；在飞尝试与资源声明留给恢复和人工核实。
        with self._guard:
            self._stopping = True
            self._execution_claims.deactivate_authority(self._authority_epoch)
            loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=3)
        remove = getattr(self.executor, "remove_job_finished_listener", None)
        if callable(remove):
            remove(self._on_executor_finished)
        self._thread = None
        self._loop = None

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        loop.create_task(self._inventory_recovery_loop())
        self._started.set()
        try:
            loop.run_forever()
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            loop.close()

    def _reconcile_inventory(self) -> dict[str, Any]:
        report = self._inventory_recovery.reconcile()
        try:
            self._physical_recovery.reconcile_unstarted()
        except Exception as error:
            report["issues"].append({"reason": f"物理占用恢复失败：{error}"})
        with self._guard:
            self._inventory_recovery_report = report
            if report["issues"]:
                self._dispatch_pause_reasons.add("inventory_reconciliation")
            else:
                self._dispatch_pause_reasons.discard("inventory_reconciliation")
        return report

    def inventory_recovery_snapshot(self) -> dict[str, Any]:
        with self._guard:
            return deepcopy(getattr(self, "_inventory_recovery_report", self._inventory_recovery.snapshot()))

    async def _inventory_recovery_loop(self) -> None:
        """终态库存写入失败时持续重试；恢复成功后唤醒原排队动作。"""
        while not self._stopping:
            await asyncio.sleep(5)
            if not self._stopping:
                self._reconcile_resources()

    async def _reconcile_before_planning(
        self, task: Dict[str, Any], runs: list[Dict[str, Any]],
    ) -> None:
        """先独立接管在飞尝试，再解释计划、预留库存或响应取消。

        图格式、驱动注册表或库存服务变化不能证明旧动作已经停止。核实只使用持久
        Claim 和 attempt 身份，既能接迟到结果，也能走原有人工裁决；重试仅追加新
        attempt，等全部旧动作收敛后再经过完整计划检查。
        """
        task_uuid = str(task["uuid"])
        waiter = asyncio.Event()
        recovered = [run for run in runs if run["status"] in _RUN_NEEDS_RECONCILIATION]
        with self._guard:
            self._recovery_waiters[task_uuid] = (asyncio.get_running_loop(), waiter)
            for run in recovered:
                run_uuid = str(run["uuid"])
                job_uuid = str(run["current_job_uuid"])
                self._run_to_task[run_uuid] = task_uuid
                self._job_runs[job_uuid] = run_uuid
                self._run_specs[run_uuid] = {
                    "workflow_node_uuid": str(run["workflow_node_uuid"]),
                    "current_job_uuid": job_uuid,
                    "attempt_no": int(run.get("attempt_count") or 1),
                    "job_uuids": [job_uuid],
                }
        try:
            for run in recovered:
                run_uuid = str(run["uuid"])
                spec = self._run_specs[run_uuid]
                claim = self._execution_claims.get(spec["current_job_uuid"])
                action = (claim["request"].get("current_action") or {}) if claim else {}
                # 旧版本没有 Claim 时只回退到持久快照，用于展示，绝不用于下发。
                snapshot = task.get("workflow_snapshot") or {}
                sources = snapshot.get("nodes", []) if isinstance(snapshot, dict) and not claim else []
                sources = sources if isinstance(sources, list) else []
                source = next((node for node in sources if isinstance(node, dict)
                               and node.get("uuid") == run["workflow_node_uuid"]), {})
                metadata = source.get("meta_data")
                metadata = metadata if isinstance(metadata, dict) else {}
                device_id = action.get("device_id") or metadata.get("target_device_id") or run.get("material_uuid") or "unknown"
                action_name = action.get("action_name") or source.get("action_name") or "unknown"
                node = DagNode(node_id=run_uuid, device_id=str(device_id), action=str(action_name))
                if claim:
                    self._execution_claims.mark_uncertain(spec["current_job_uuid"])
                with self._guard:
                    self._run_context[run_uuid] = (task, node)
                persisted = self._physical_recovery.pending().get(spec["current_job_uuid"])
                if persisted:
                    self._settle_attempt(spec["current_job_uuid"], **{key: persisted[key] for key in ("job_status", "return_info", "error_info", "resolution")})
                    if self.workflow.get_workflow_node_job(spec["current_job_uuid"])["status"] in _RUN_TERMINAL:
                        continue
                self._open_reconciliation_decision(task, node, spec, str(run["status"]))
            if not any(run["status"] in _RUN_NEEDS_RECONCILIATION
                       for run in self.workflow.list_workflow_node_runs(task_uuid)):
                waiter.set()
            await waiter.wait()
        finally:
            with self._guard:
                self._recovery_waiters.pop(task_uuid, None)
                for run in recovered:
                    run_uuid = str(run["uuid"])
                    self._run_specs.pop(run_uuid, None)
                    self._run_to_task.pop(run_uuid, None)
                    self._run_context.pop(run_uuid, None)
                    self._job_runs.pop(str(run["current_job_uuid"]), None)
                for decision_id, pending in list(self._reconciliation_decisions.items()):
                    if pending["task_uuid"] == task_uuid:
                        self._reconciliation_decisions.pop(decision_id, None)
        self._settle_task_reconciliation(task_uuid)

    def _build_dag(
        self,
        task: Dict[str, Any],
        runs: list[Dict[str, Any]],
    ) -> tuple[TaskDag, Dict[str, Dict[str, Any]]]:
        plan = task.get("execution_plan") or {}
        snapshot = task.get("workflow_snapshot") or {}
        snapshot_nodes = {
            str(node["uuid"]): node for node in snapshot.get("nodes", [])
        }
        planned_nodes = {
            str(node["uuid"]): node for node in plan.get("nodes", [])
        }
        runs_by_node = {str(run["workflow_node_uuid"]): run for run in runs}
        scheduler_revision = int(
            (task.get("meta_data") or {}).get("scheduler_revision") or 1
        )
        dag_nodes: Dict[str, DagNode] = {}
        specs: Dict[str, Dict[str, Any]] = {}
        # 执行父级（最近的循环容器），按工作流节点 uuid；来自执行计划
        parents: Dict[str, Optional[str]] = {}
        for workflow_node_uuid, run in runs_by_node.items():
            planned = planned_nodes.get(workflow_node_uuid, {})
            source = snapshot_nodes.get(workflow_node_uuid, {})
            parent = planned.get("parent_uuid")
            parents[workflow_node_uuid] = (
                str(parent) if parent and str(parent) in runs_by_node else None
            )
            run_uuid = str(run["uuid"])
            policy = run.get("execution_policy") or {}
            param = dict(run.get("param") or planned.get("param") or {})
            source_meta = dict(source.get("meta_data") or {})
            if run["executor_kind"] == NODE_KIND_LOOP:
                try:
                    loop_spec = parse_loop_spec(param)
                except ValueError as exc:
                    raise BackendSchedulingError(
                        f"workflow node {workflow_node_uuid}: {exc}"
                    ) from exc
                dag_nodes[run_uuid] = DagNode(
                    node_id=run_uuid,
                    device_id="",
                    action="",
                    kind=NODE_KIND_LOOP,
                    loop_spec=loop_spec.model_dump(mode="json"),
                    action_args=param,
                )
            elif run["executor_kind"] == "condition":
                from unilabos.protocol.runtime.condition import condition_references

                condition_references(param)
                dag_nodes[run_uuid] = DagNode(
                    node_id=run_uuid, device_id="", action="", kind="condition", action_args=param,
                )
            elif run["executor_kind"] != "device_action":
                raise BackendSchedulingError(
                    f"executor_kind {run['executor_kind']!r} is not wired locally"
                )
            else:
                device_id = str(
                    source_meta.get("target_device_id")
                    or run.get("material_uuid")
                    or planned.get("material_uuid")
                    or source.get("material_uuid")
                    or param.get("device_id")
                    or ""
                )
                action = str(source.get("action_name") or param.get("action") or "")
                if not device_id or not action:
                    raise BackendSchedulingError(
                        f"workflow node {workflow_node_uuid} lacks material_uuid/device action"
                    )
                dag_nodes[run_uuid] = DagNode(
                    node_id=run_uuid,
                    device_id=device_id,
                    action=action,
                    action_type=str(source.get("action_type") or ""),
                    action_args=param,
                    always_free=bool(policy.get("always_free")),
                )
            current_job_uuid = str(run["current_job_uuid"])
            current_job = self._current_job_metadata(current_job_uuid)
            attempt_no = int(
                (current_job or {}).get("attempt_no")
                or run.get("attempt_count")
                or 1
            )
            retry_of_job_uuid = (
                (current_job or {}).get("retry_of_job_uuid")
                or run.get("retry_of_job_uuid")
            )
            attempt_trigger = str((current_job or {}).get("trigger") or "initial")
            specs[run_uuid] = {
                "workflow_node_uuid": workflow_node_uuid,
                # 节点显式声明优先；未声明时派发前按注册表 @action(always_free) 解析
                "always_free_policy": policy.get("always_free"),
                # 节点级超时（冻结语义 execution_timeout_seconds / 新增 timeout_seconds，0 = 未声明）
                "execution_policy": dict(policy),
                "base_param": param,
                "param_schema": planned.get("param_schema"),
                "edges": list(plan.get("edges") or []),
                "runs_by_node": {
                    node_uuid: str(node_run["uuid"])
                    for node_uuid, node_run in runs_by_node.items()
                },
                "inventory_requirements": list(
                    planned.get("inventory_requirements") or []
                ),
                "inventory_template": list(planned.get("inventory_requirements") or []),
                "inventory_deferred": bool(planned.get("inventory_deferred")),
                "reserved_material_uuids": [],
                "scheduler_revision": scheduler_revision,
                # 当前 attempt：执行器 job_id / 资源 owner / 库存 reservation 的键
                "current_job_uuid": current_job_uuid,
                "inventory_job_status": run["status"],
                "attempt_no": attempt_no,
                "retry_of_job_uuid": (
                    str(retry_of_job_uuid) if retry_of_job_uuid else None
                ),
                # attempt 为何产生（runtime.v1 execute_job 的一致性校验字段）；
                # 已重试次数：只有 retry 决策追加的 attempt 才算，循环下一轮不算
                "attempt_trigger": attempt_trigger,
                "retry_count": (
                    max(attempt_no - 1, 0) if attempt_trigger == "retry_decision" else 0
                ),
                "job_uuids": [current_job_uuid],
            }
            manual_meta = source_meta.get("manual_confirm")
            if isinstance(manual_meta, dict):
                specs[run_uuid]["manual_confirm"] = dict(manual_meta)
            if run["status"] in _RUN_NEEDS_RECONCILIATION:
                # 上一进程留下的 attempt：起跑时不下发设备，改为向用户开裁决
                specs[run_uuid]["recovered_status"] = str(run["status"])

        run_of = {node_uuid: str(node_run["uuid"]) for node_uuid, node_run in runs_by_node.items()}
        for workflow_node_uuid, parent in parents.items():
            spec = specs[run_of[workflow_node_uuid]]
            spec["parent_run_uuid"] = run_of[parent] if parent else None
            spec["kind"] = str(runs_by_node[workflow_node_uuid]["executor_kind"])
        # 循环节点：循环体（含嵌套）的全部节点运行，每轮为它们追加 attempt
        for workflow_node_uuid in parents:
            chain = parents.get(workflow_node_uuid)
            while chain is not None:
                specs[run_of[chain]].setdefault("body_run_uuids", []).append(
                    run_of[workflow_node_uuid]
                )
                chain = parents.get(chain)

        # 依赖对（工作流节点 uuid）：handle 边 + execution_policy.depends_on（@workflow
        # 声明式步骤等无 handle 数据流的节点用它表达串行），跨循环边界的提升到容器所在层级
        pairs: list[tuple[str, str]] = []
        for edge in plan.get("edges") or []:
            pairs.append((str(edge["source_node_uuid"]), str(edge["target_node_uuid"])))
        for workflow_node_uuid, run in runs_by_node.items():
            depends_on = (run.get("execution_policy") or {}).get("depends_on") or []
            if not isinstance(depends_on, list):
                continue
            for upstream in depends_on:
                pairs.append((str(upstream), workflow_node_uuid))
        edges_by_container: Dict[Optional[str], list[DagEdge]] = {}
        seen_edges: set[tuple[str, str]] = set()
        for source_node, target_node in pairs:
            if source_node not in runs_by_node or target_node not in runs_by_node:
                continue
            try:
                lifted_source, lifted_target = lift_edge(parents, source_node, target_node)
            except HierarchyError as exc:
                raise BackendSchedulingError(str(exc)) from exc
            key = (run_of[lifted_source], run_of[lifted_target])
            if key in seen_edges or key[0] == key[1]:
                continue
            seen_edges.add(key)
            edges_by_container.setdefault(parents.get(lifted_source), []).append(
                DagEdge(source_node_uuid=key[0], target_node_uuid=key[1])
            )

        def build_level(container: Optional[str]) -> TaskDag:
            level_nodes: Dict[str, DagNode] = {}
            for node_uuid, parent in parents.items():
                if parent != container:
                    continue
                node = dag_nodes[run_of[node_uuid]]
                if node.is_loop:
                    node.body = build_level(node_uuid)
                level_nodes[node.node_id] = node
            return TaskDag(
                task_id=str(task["uuid"]),
                notebook_id="",
                server_info={},
                nodes=level_nodes,
                edges=list(edges_by_container.get(container, [])),
            )

        return build_level(None), specs

    def _current_job_metadata(self, job_uuid: str) -> Dict[str, Any]:
        """读取当前 attempt 的 retry 元数据；兼容精简测试替身。"""

        getter = getattr(self.workflow, "get_workflow_node_job", None)
        if not callable(getter):
            return {}
        try:
            value = getter(job_uuid)
        except Exception:  # noqa: BLE001 - 元数据缺失不应阻断 DAG 构建
            return {}
        return value if isinstance(value, dict) else {}

    def _start_node(self, task: Dict[str, Any], node: DagNode) -> None:
        """为节点运行的当前 attempt 申请完整资源集合；``held`` 即下发。

        重启恢复的 execution_unknown / intervention_required attempt 不申请资源也不
        下发：设备可能已经执行过，盲目重放会产生第二次副作用，改为开裁决等用户决定。
        retry 决策追加的新 attempt 再走正常路径。
        """

        spec = self._run_specs[node.node_id]
        local_state = self._run_condition_control(task, node)
        if local_state is not None:
            with self._guard:
                runner = self._runners.get(str(task["uuid"]))
            if runner is not None:
                runner.notify_terminal(node.node_id, local_state)
            return
        recovered_status = spec.pop("recovered_status", None)
        if recovered_status is not None:
            # 重启前在飞的 attempt：设备可能仍停在中间态，裁决前继续占着它的锁，后来的任务排队
            self._hold_recovered_resources(task, node, spec)
            self._open_reconciliation_decision(task, node, spec, recovered_status)
            return
        # 运行模式以权威为准：起跑时是自动、之后被暂停的任务，捕获的 task 快照已过期。
        # 判定与登记在同一把锁里，避免与 step / resume 唤醒交错而漏掉这个节点。
        with self._guard:
            if self._dispatch_gate_closed(task, spec["current_job_uuid"]):
                self._waiting_interval_jobs.pop(spec["current_job_uuid"], None)
                self._waiting_step_nodes[node.node_id] = (task, node)
                return
            self._waiting_step_nodes.pop(node.node_id, None)
        if spec.get("inventory_deferred") and not self._reserve_ready_inventory(task, node):
            with self._guard:
                self._waiting_interval_jobs[spec["current_job_uuid"]] = (task, node)
            return
        args = self._resolve_action_args(node.node_id)
        # InventoryRequirement 是节点上的声明；权威预留后解析出的具体出库内容
        # （物料 uuid / lot 与数量）按需求 key 注入同名动作参数，设备拿到的已是具体引用。
        args.update(spec.get("inventory_allocations") or {})
        from unilabos.registry.typed_contract import validate_action_values

        args = validate_action_values(spec.get("param_schema"), args, managed_resources=True)
        if spec.get("param_schema"):
            from unilabos.server.services.runtime.workflow.run_preflight import validate_resource_values

            validate_resource_values(spec["param_schema"], args, self.materials_gateway)
        parameter_names = self._material_lock_parameters(
            node.device_id,
            node.action,
        )
        material_uuids = set(
            material_uuids_for_parameters(parameter_names, args)
        )
        material_uuids.update(spec.get("reserved_material_uuids") or ())
        from .physical_contract import resolve_physical_contract
        physical = resolve_physical_contract(spec.get("param_schema"), args, node.device_id, self.materials_gateway)
        material_uuids.update(physical.get("material_ids", []))
        spec["physical_contract"] = physical
        spec["resolved_action_args"] = args
        spec["materials_need_lock"] = parameter_names
        spec["always_free"] = self._action_always_free(node, spec)
        job_uuid = spec["current_job_uuid"]
        # 资源申请以当前 attempt 为 owner：acquire 对同一 owner 幂等重放，重试
        # 的新 attempt 必须是新的申请才能重新排队获取。
        from .tenancy_contract import resolve_tenancy_contract
        tenancy = resolve_tenancy_contract(spec.get("param_schema"), args, node.device_id, self.materials_gateway)
        if physical:
            tenancy["device_claims"] = sorted(set(tenancy.get("device_claims", [])) | set(physical["device_ids"]))
            if physical.get("device_tenancy"):
                if tenancy.get("device_tenancy") and tenancy["device_tenancy"] != physical["device_tenancy"]:
                    raise ValueError("搬运托管与显式设备托管合同不一致")
                tenancy["device_tenancy"] = physical["device_tenancy"]
        if self._physical_recovery.blockers(job_uuid, material_uuids, {node.device_id, *tenancy.get("device_claims", [])}):
            with self._guard:
                self._waiting_interval_jobs[job_uuid] = (task, node)
            return
        resource_request = SchedulerResourceRequest(
                **tenancy,
                request_uuid=f"resource:{job_uuid}",
                owner_uuid=job_uuid,
                task_uuid=str(task["uuid"]),
                current_action=ActionLockClaim(
                    device_id=node.device_id,
                    action_name=node.action,
                ),
                always_free=spec["always_free"],
                interval_admission=True,
                material_lock_claims=[
                    MaterialLockClaim(material_uuid=material_uuid)
                    for material_uuid in sorted(material_uuids)
                ],
            )
        prior_claim = self._execution_claims.get(job_uuid)
        if prior_claim and not prior_claim["request"].get("interval_admission"):
            resource_request = resource_request.model_copy(update={"interval_admission": False})
        spec["resource_request"] = resource_request
        with self._guard:
            self._run_context[node.node_id] = (task, node)
            self._job_runs[job_uuid] = node.node_id
        if not self._intervals.admit(resource_request, self._authority_epoch):
            with self._guard:
                self._waiting_interval_jobs[job_uuid] = (task, node)
                self._waiting_interval_requests[job_uuid] = resource_request
            return
        with self._guard:
            self._waiting_interval_jobs.pop(job_uuid, None)
        record = self.resources.acquire(resource_request)
        with self._guard:
            self._run_context[node.node_id] = (task, node)
            self._job_runs[job_uuid] = node.node_id
            self._waiting_resource_jobs[job_uuid] = (task, node)
        if record.status == "held":
            self._dispatch_held_node(task, node)

    def _executor_ready(self) -> bool:
        """执行适配器（HostLink / ROS2 host node）是否已注册；未就绪时不派发。"""

        ready = getattr(self.executor, "host_ready", None)
        return not callable(ready) or bool(ready())

    def resume_pending_dispatches(self) -> None:
        """执行适配器就绪（``publish_host_ready``）：重算等待集合，派发被闸住的节点。"""

        self._reconcile_resources()

    def _dispatch_gate_closed(self, task: Dict[str, Any], job_uuid: str) -> bool:
        """任务被暂停 / 单步且这个 attempt 没有许可时返回 True（自动模式恒为 False）。"""

        claim = getattr(self.workflow, "claim_task_step", None)
        if not callable(claim):
            return task.get("run_mode") == "step"
        return not claim(str(task["uuid"]), job_uuid)

    def _park_if_gated(self, task: Dict[str, Any], node: DagNode, job_uuid: str) -> bool:
        """暂停的任务：排队中的 attempt 退出资源队列，回到等待单步的集合。

        返回 True 表示已停下（不派发）。判定、撤回与登记在同一把锁里，与 step / resume
        唤醒互斥，不会出现刚登记就错过唤醒的节点。
        """

        with self._guard:
            if job_uuid in self._dispatched_jobs:
                return False
            if not self._dispatch_gate_closed(task, job_uuid):
                return False
            if not self.resources.withdraw(job_uuid, reason="task_paused"):
                return False
            self._waiting_resource_jobs.pop(job_uuid, None)
            self._intervals.withdraw(job_uuid)
            self._waiting_step_nodes[node.node_id] = (task, node)
            return True

    def _gate_waiting_resource_jobs(self, task_uuid: str) -> None:
        with self._guard:
            for job_uuid, (task, node) in list(self._waiting_interval_jobs.items()):
                if str(task["uuid"]) == task_uuid and self._dispatch_gate_closed(task, job_uuid):
                    self._waiting_interval_jobs.pop(job_uuid, None)
                    self._waiting_step_nodes[node.node_id] = (task, node)
            queued = [
                (job_uuid, task, node)
                for job_uuid, (task, node) in self._waiting_resource_jobs.items()
                if str(task["uuid"]) == task_uuid
            ]
        for job_uuid, task, node in queued:
            self._park_if_gated(task, node, job_uuid)

    def _dispatch_held_node(self, task: Dict[str, Any], node: DagNode) -> None:
        """下发一个已经持有完整动作/物料集合的节点运行（当前 attempt）。"""

        with self._guard:
            job_uuid = self._run_specs[node.node_id]["current_job_uuid"]
        # 排队期间任务可能被暂停：拿到锁也不派发，撤回申请让给别的任务。
        if self._park_if_gated(task, node, job_uuid):
            return
        with self._guard:
            spec = self._run_specs[node.node_id]
            job_uuid = spec["current_job_uuid"]
            if self._dispatch_pause_reasons or not self._executor_ready():
                # 安静点重启闸门 / 执行适配器尚未就绪（ROS2 host node 在设备初始化
                # 完成后才注册）：节点保持在等待集合并继续持有资源，resume /
                # host ready 后由 _reconcile_resources 原样派发，不产生失败。
                return
            if job_uuid in self._dispatched_jobs:
                return
            record = self.resources.request_for_owner(job_uuid)
            if record.status != "held":
                return
            self._waiting_resource_jobs.pop(job_uuid, None)
            self._dispatched_jobs.add(job_uuid)
            args = dict(spec["resolved_action_args"])
            is_manual_confirmation = node.action.strip().lower() == "manual_confirm"
            if is_manual_confirmation:
                self._manual_confirmation_jobs.add(job_uuid)
        if is_manual_confirmation:
            try:
                # 缺少 key 与显式 [] 必须区分：前者是编辑器未完成配置，
                # 后者表示 unrestricted（任何人可确认）。
                if "assignee_user_ids" not in args:
                    raise BackendSchedulingError(
                        "manual_confirm requires explicit assignee_user_ids; use [] for unrestricted"
                    )
                timeout = args.get("timeout_seconds", 3600)
                if isinstance(timeout, bool) or not isinstance(timeout, int):
                    raise BackendSchedulingError(
                        "manual_confirm timeout_seconds must be an integer"
                    )
                if timeout <= 0:
                    timeout = 3600
                manual_meta = spec.get("manual_confirm") or {}
                label = str(manual_meta.get("label") or "人工确认").strip()
                prompt = str(manual_meta.get("prompt") or "").strip()
                confirmation = self.workflow.open_workflow_manual_confirmation(
                    job_uuid,
                    param=args,
                    description=(f"{label}：{prompt}" if prompt else label),
                    meta_data={
                        "workflow_task_uuid": str(task["uuid"]),
                        "workflow_node_uuid": str(spec["workflow_node_uuid"]),
                        "target_device_id": node.device_id,
                    },
                    timeout_seconds=timeout,
                )
                # 进程恢复时，决策可能已在 scheduler 尚未重新接管期间提交；
                # 对已终态的幂等读回立即收敛，不重复调用设备。
                if confirmation.get("status") != "pending":
                    self._on_manual_confirmation_decided(confirmation)
                return
            except Exception:
                with self._guard:
                    self._manual_confirmation_jobs.discard(job_uuid)
                try:
                    self.resources.cancel_owner(job_uuid, reason="manual_open_failed")
                except ResourceNotFound:
                    pass
                raise
        dispatch_invoked = False
        try:
            self._execution_claims.reserve(spec["resource_request"], authority_epoch=self._authority_epoch)
            material_execution = None
            if spec.get("physical_contract") or spec.get("inventory_reservation_uuid"):
                from .physical_contract import resolve_physical_contract
                from unilabos.protocol.material_execution import MaterialExecutionPrepare
                from unilabos.protocol.base import canonical_hash
                physical = resolve_physical_contract(spec.get("param_schema"), args, node.device_id, self.materials_gateway)
                if physical.get("material_ids", []) != (spec.get("physical_contract") or {}).get("material_ids", []):
                    raise ValueError("等待期间物理资源范围变化，请重新创建尝试")
                claim = self._execution_claims.get(job_uuid)
                material_execution = self.materials_gateway.prepare_material_execution(MaterialExecutionPrepare(
                    job_uuid=job_uuid, task_uuid=str(task["uuid"]), claim_uuid=claim["claim_uuid"], attempt_no=spec["attempt_no"],
                    parameter_hash=canonical_hash(args), fences=claim["fences"], moves=physical.get("moves", []),
                    aliquot=physical.get("aliquot"), safety_material_uuids=physical.get("material_ids", []),
                    safety_device_ids=sorted({node.device_id, *physical.get("device_ids", [])}) if physical else [],
                    reserved_site_uuids=physical.get("reserved_site_uuids", []),
                    inventory_reservation_uuid=spec.get("inventory_reservation_uuid")))
            self.workflow.mark_workflow_node_job_running(job_uuid)
            payload = build_job_start_payload(
                job_id=job_uuid,
                task_id=str(task["uuid"]),
                workflow_id=str(task.get("workflow_uuid") or ""),
                node_id=spec["workflow_node_uuid"],
                device_id=node.device_id,
                action_name=node.action,
                action_type=node.action_type,
                action_args=args,
                materials_need_lock=spec["materials_need_lock"],
                inventory_requirements=spec["inventory_requirements"],
                inventory_reservation_uuid=spec.get(
                    "inventory_reservation_uuid"
                ),
                scheduler_revision=spec["scheduler_revision"],
                node_run_uuid=node.node_id,
                attempt_no=spec["attempt_no"],
                retry_of_job_uuid=spec.get("retry_of_job_uuid"),
                attempt_trigger=str(spec.get("attempt_trigger") or "initial"),
                retry_count=spec.get("retry_count"),
            )
            payload["always_free"] = spec.get("always_free", node.always_free)
            if material_execution is not None:
                payload["material_execution"] = {key: material_execution[key] for key in ("job_uuid", "claim_uuid", "plan_hash")}

            contract = ((spec.get("param_schema") or {}).get("x-unilabos-action-contract") or {}).get("resource_contract")
            if contract:
                payload["resource_contract"] = contract
            timeouts = self._action_timeouts(node, spec, args)
            if timeouts.get("timeout") is not None:
                payload["timeout_seconds"] = float(timeouts["timeout"])
            if timeouts.get("execution_timeout") is not None:
                payload["execution_timeout_seconds"] = float(timeouts["execution_timeout"])
            self._persist_node_run_timeouts(node.node_id, timeouts)
            payload["execution_permit"] = self._execution_claims.seal_dispatch(job_uuid, payload, self._authority_epoch)
            dispatch_invoked = True
            self.executor.dispatch(payload)
        except Exception as error:
            if dispatch_invoked:
                # 执行器可能已经发出动作；通信异常不证明设备没有执行，保留全部资源等核实。
                self._execution_claims.mark_uncertain(job_uuid)
                self._reconcile_lost_attempt(job_uuid, {"uncertainty_reason": f"派发回执不确定：{error}"})
                logger.exception("作业 %s 派发回执不确定，已保留资源并请求核实", job_uuid)
                return
            self.workflow.record_workflow_node_job_terminal(
                job_uuid, status="failed", return_info={},
                error_info=[{"code": "dispatch_not_sent", "message": str(error)}],
            )
            with self._guard:
                self._dispatched_jobs.discard(job_uuid)
            try:
                if self._execution_claims.get(job_uuid) is None:
                    self.resources.cancel_owner(job_uuid, reason="dispatch_failed")
            except ResourceNotFound:
                pass
            raise

    def _reconcile_resources(self) -> None:
        """重算等待集合；只下发本轮已获得完整资源的 attempt。"""

        with self._guard:
            if self._stopping:
                return
        self._reconcile_inventory()
        self._retry_physical_outcomes()
        with self._guard:
            if ("legacy_resource_reconciliation" in self._dispatch_pause_reasons
                    and not self._intervals.unadmitted_active_claims()):
                self._dispatch_pause_reasons.discard("legacy_resource_reconciliation")
            interval_candidates = list(self._waiting_interval_jobs.items())
        for job_uuid, (task, node) in interval_candidates:
            with self._guard:
                if self._waiting_interval_jobs.pop(job_uuid, None) is None:
                    continue
            try:
                self._start_node(task, node)
            except Exception:
                logger.exception("资源区间等待节点 %s 无法继续", job_uuid)
                with self._guard:
                    self._waiting_interval_jobs.pop(job_uuid, None)
                self._notify_start_failure(node.node_id)
        with self._guard:
            if ("legacy_execution_reconciliation" in self._dispatch_pause_reasons
                    and not self._execution_claims.unclaimed_active_jobs()):
                self._dispatch_pause_reasons.discard("legacy_execution_reconciliation")
            candidates = list(self._waiting_resource_jobs.items())
        for job_uuid, (task, node) in candidates:
            try:
                record = self.resources.request_for_owner(job_uuid)
            except ResourceNotFound:
                continue
            if record.status != "held":
                continue
            try:
                self._dispatch_held_node(task, node)
            except Exception:
                logger.exception(
                    "scheduler failed to dispatch promoted job %s",
                    job_uuid,
                )
                self._notify_start_failure(node.node_id)

    def _notify_start_failure(self, run_uuid: str) -> None:
        with self._guard:
            task_uuid = self._run_to_task.get(run_uuid)
            runner = self._runners.get(task_uuid or "")
        if runner is not None:
            runner.notify_terminal(run_uuid, NodeState.FAILED)

    def _material_lock_parameters(
        self,
        device_id: str,
        action_name: str,
    ) -> list[str]:
        resolver = self._materials_need_lock_resolver
        if resolver is None:
            resolver = getattr(
                self.executor,
                "resolve_material_lock_parameters",
                None,
            )
        if not callable(resolver):
            return []
        return list(resolver(device_id, action_name) or [])

    def _action_always_free(self, node: DagNode, spec: Dict[str, Any]) -> bool:
        """节点是否免动作锁：节点 execution_policy 显式声明优先，否则取注册表 ``@action(always_free)``。

        与 ``materials_need_lock`` 一样在派发前解析：此时执行适配器已就绪，注册表副本
        （含 slave 远端设备）完整。
        """

        explicit = spec.get("always_free_policy")
        if explicit is not None:
            return bool(explicit)
        resolver = getattr(self.executor, "resolve_action_always_free", None)
        if not callable(resolver):
            return node.always_free
        return bool(resolver(node.device_id, node.action))

    def _action_timeouts(
        self, node: DagNode, spec: Dict[str, Any], action_args: Dict[str, Any]
    ) -> Dict[str, Any]:
        """派发前解析该 attempt 的硬 / 软超时（秒）。

        节点 ``execution_policy.timeout_seconds`` / ``execution_timeout_seconds``（正整数）
        显式声明优先；否则取注册表 ``@action(timeout / execution_timeout)``，软超时表达式
        用**最终** ``action_args``（含上游 handle 解析结果）求值。任何一步失败都只记录，
        不阻断派发——超时是安全网，不是准入条件。
        """

        resolved: Dict[str, Any] = {
            "timeout": None,
            "execution_timeout": None,
            "execution_timeout_spec": None,
            "source": {},
        }
        policy = spec.get("execution_policy") or {}
        for policy_key, target in (
            ("timeout_seconds", "timeout"),
            ("execution_timeout_seconds", "execution_timeout"),
        ):
            value = policy.get(policy_key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            if value > 0:
                resolved[target] = float(value)
                resolved["source"][target] = "execution_policy"
        if resolved["timeout"] is not None and resolved["execution_timeout"] is not None:
            return resolved
        resolver = getattr(self.executor, "resolve_action_timeouts", None)
        if not callable(resolver):
            return resolved
        try:
            registry = resolver(node.device_id, node.action, action_args) or {}
        except Exception:  # noqa: BLE001 - 注册表解析失败不阻断派发
            logger.exception(
                "failed to resolve action timeouts for %s.%s", node.device_id, node.action
            )
            return resolved
        for key in ("timeout", "execution_timeout"):
            if resolved[key] is None and registry.get(key) is not None:
                resolved[key] = float(registry[key])
                resolved["source"][key] = "registry"
        resolved["execution_timeout_spec"] = registry.get("execution_timeout_spec")
        if registry.get("error"):
            resolved["error"] = registry["error"]
        return resolved

    def _persist_node_run_timeouts(self, run_uuid: str, timeouts: Dict[str, Any]) -> None:
        """把解析出的软超时秒数写回节点运行（冻结字段 ``execution_timeout_seconds``），供前端展示。"""

        seconds = timeouts.get("execution_timeout")
        if seconds is None:
            return
        setter = getattr(self.workflow, "set_workflow_node_run_execution_timeout", None)
        if not callable(setter):
            return
        try:
            setter(run_uuid, int(math.ceil(float(seconds))))
        except Exception:  # noqa: BLE001 - 展示字段写失败不影响派发
            logger.warning(
                "failed to persist execution_timeout_seconds for node run %s", run_uuid
            )

    def _retry_physical_outcomes(self) -> None:
        with self._guard:
            if self._physical_retrying:
                return
            self._physical_retrying = True
        try:
            for job_uuid, outcome in self._physical_recovery.pending().items():
                self._settle_attempt(job_uuid, **{key: outcome[key] for key in ("job_status", "return_info", "error_info", "resolution")})
        finally:
            with self._guard:
                self._physical_retrying = False

    def _release_job_resources(self, job_uuid: str, *, canceled: bool) -> None:
        """释放一个 attempt 持有的资源申请。"""

        self._reconcile_inventory()
        if not self._execution_claims.release_if_terminal(job_uuid):
            return
        self._intervals.release_job(job_uuid)
        try:
            record = self.resources.request_for_owner(job_uuid)
        except ResourceNotFound:
            with self._guard:
                self._waiting_resource_jobs.pop(job_uuid, None)
                self._dispatched_jobs.discard(job_uuid)
                self._manual_confirmation_jobs.discard(job_uuid)
            self._reconcile_resources()
            return
        if record.status not in {"released", "canceled"}:
            if canceled:
                self.resources.cancel_owner(job_uuid, reason="job_canceled")
            else:
                self.resources.release(job_uuid, reason="job_terminal")
        with self._guard:
            self._waiting_resource_jobs.pop(job_uuid, None)
            self._dispatched_jobs.discard(job_uuid)
            self._manual_confirmation_jobs.discard(job_uuid)
        self._reconcile_resources()

    def _cancel_task(self, task_uuid: str) -> None:
        """停掉一个任务：未下发的节点直接收为 CANCELLED，已下发的只请求取消。

        已交给设备的动作继续持有动作锁 / 物料锁，直到执行面回报它真实结束（驱动返回或
        取消后返回）——锁随该 attempt 的终态释放。同步驱动无法被打断，只能等它返回；
        若 Host 在此期间失联，仍按 execution_unknown 交人工核对。
        """

        with self._guard:
            runner = self._runners.get(task_uuid)
            jobs = [
                (job_uuid, run_uuid)
                for job_uuid, run_uuid in self._job_runs.items()
                if self._run_to_task.get(run_uuid) == task_uuid
            ]
            dispatched = [job_uuid for job_uuid, _run in jobs if job_uuid in self._dispatched_jobs]
            queued = [
                (job_uuid, run_uuid)
                for job_uuid, run_uuid in jobs
                if job_uuid not in self._dispatched_jobs and job_uuid in self._waiting_resource_jobs
            ]
            waiting_steps = [
                node_id
                for node_id, (task, _node) in list(self._waiting_step_nodes.items())
                if str(task["uuid"]) == task_uuid
            ]
            for node_id in waiting_steps:
                self._waiting_step_nodes.pop(node_id, None)
            interval_waiters = [(job, node.node_id) for job, (task, node) in self._waiting_interval_jobs.items()
                                if str(task["uuid"]) == task_uuid]
            for job, _node in interval_waiters:
                self._waiting_interval_jobs.pop(job, None)
            manual_jobs = [
                job_uuid
                for job_uuid in self._manual_confirmation_jobs
                if (run_uuid := self._job_runs.get(job_uuid)) is not None
                and self._run_to_task.get(run_uuid) == task_uuid
            ]
        # 排队等锁的 attempt 退出队列；等单步许可的节点直接收口
        for _job, run_uuid in interval_waiters:
            if runner is not None:
                runner.notify_terminal(run_uuid, NodeState.CANCELLED)
        for job_uuid, run_uuid in queued:
            self._intervals.withdraw(job_uuid)
            try:
                if not self.resources.withdraw(job_uuid, reason="task_canceled"):
                    self.resources.cancel_owner(job_uuid, reason="task_canceled")
            except ResourceNotFound:
                pass
            with self._guard:
                self._waiting_resource_jobs.pop(job_uuid, None)
            if runner is not None:
                runner.notify_terminal(run_uuid, NodeState.CANCELLED)
        for node_id in waiting_steps:
            if runner is not None:
                runner.notify_terminal(node_id, NodeState.CANCELLED)
        # 已下发的动作：只请求取消，锁保留到真实终态
        cancel_job = getattr(self.executor, "cancel_job", None)
        for job_uuid in dispatched:
            self._mark_job_cancel_requested(job_uuid)
            if callable(cancel_job):
                try:
                    cancel_job(job_uuid)
                except Exception:  # noqa: BLE001 - 取消请求失败时等动作自然结束
                    logger.exception("failed to request cancel for job %s", job_uuid)
        if dispatched and not callable(cancel_job):
            self.executor.cancel_task(task_uuid)
        for job_uuid in manual_jobs:
            try:
                self.workflow.decide_workflow_manual_confirmation(
                    str(self.workflow.get_manual_confirmation_for_job(job_uuid)["uuid"]),
                    action="cancel",
                    confirmed_by="scheduler",
                    decision_idempotency_key=f"scheduler-cancel:{job_uuid}",
                )
            except Exception:  # noqa: BLE001 - 任务取消仍由 run/job 收敛兜底
                logger.exception("failed to cancel manual confirmation for job %s", job_uuid)

    def _mark_job_cancel_requested(self, job_uuid: str) -> None:
        marker = getattr(self.workflow, "mark_workflow_node_job_cancel_requested", None)
        if not callable(marker):
            return
        try:
            marker(job_uuid)
        except Exception:  # noqa: BLE001 - 展示状态，失败不影响取消
            logger.exception("failed to mark job %s cancel_requested", job_uuid)

    def _task_cancel_requested(self, task_uuid: str) -> bool:
        getter = getattr(self.workflow, "get_workflow_task", None)
        if not callable(getter):
            return False
        try:
            return getter(task_uuid).get("status") == "canceling"
        except Exception:  # noqa: BLE001 - 读取失败按未取消处理
            return False

    def _cleanup_task_resources(self, task_uuid: str) -> None:
        with self._guard:
            job_uuids = [
                job_uuid
                for run_uuid, owner_task_uuid in self._run_to_task.items()
                if owner_task_uuid == task_uuid
                for job_uuid in self._run_specs.get(run_uuid, {}).get("job_uuids", ())
            ]
        for job_uuid in job_uuids:
            self._release_job_resources(job_uuid, canceled=True)

    def _release_unlocked_task_resources(self, task_uuid: str, job_uuids: list[str]) -> None:
        """V1 ``unlock_resources`` 已在库内释放异常终态任务的占用：同步释放内存资源账本。"""

        for job_uuid in job_uuids:
            self._release_job_resources(job_uuid, canceled=True)
        self._cleanup_task_resources(task_uuid)

    def _on_resource_change(self) -> None:
        if self._stopping:
            return
        self._reconcile_resources()
        self._release_confirmed_custody_inventory()

    def _release_confirmed_custody_inventory(self) -> None:
        with self.workflow.write_lock:
            tasks = [row[0] for row in self.workflow.connection.execute(
                "SELECT uuid FROM workflow_task WHERE attention_reason='device_tenancy_released'")]
        for task_uuid in tasks:
            self._release_unconsumed_task_inventory(task_uuid)

    def resource_snapshot(self):
        """返回当前统一动作/物料锁快照，供诊断 API 使用。"""

        from unilabos.server.services.runtime.workflow.resource_loans import ResourceLoans
        from unilabos.server.services.runtime.workflow.device_tenancy import DeviceTenancies
        with self._guard:
            for stale in set(self._waiting_interval_requests) - set(self._waiting_interval_jobs):
                self._waiting_interval_requests.pop(stale, None)
            interval_waiting = dict(self._waiting_interval_requests)
        with self.workflow.write_lock:
            snapshot = self.resources.snapshot()
            # 跨任务独占由持久区间准入先裁决，被拦下的申请尚未进入内存锁队列；
            # 这里补成 waiting 记录，blockers 为当前占用同一锁键的 attempt，保持观测面完整。
            known = {item.owner_uuid for item in snapshot.requests}
            sequence = max(snapshot.sequence, 1)
            waiting = [
                SchedulerResourceRequestRecord(
                    request_uuid=request.request_uuid,
                    owner_uuid=request.owner_uuid,
                    task_uuid=request.task_uuid,
                    identifiers=request.identifiers,
                    status="waiting",
                    blockers=self._intervals.blocking_jobs(request),
                    created_sequence=sequence,
                    updated_sequence=sequence,
                    version=1,
                )
                for job_uuid, request in sorted(interval_waiting.items())
                if job_uuid not in known
            ]
            return snapshot.model_copy(update={"requests": [*snapshot.requests, *waiting],
                "intervals": self._intervals.snapshot(),
                "loans": ResourceLoans.snapshot(self.workflow.connection),
                "tenancies": DeviceTenancies.snapshot(self.workflow.connection)})

    @staticmethod
    def _inventory_command_uuid(task_uuid: str, suffix: str = "") -> str:
        try:
            namespace = UUID(task_uuid)
        except ValueError:
            namespace = UUID("4f632a8d-f5cc-41e5-9471-f37c79dad537")
        return str(uuid5(namespace, f"inventory:{task_uuid}{suffix}"))

    def _reserve_inventory(
        self,
        task: Dict[str, Any],
        targets: list[tuple[str, Dict[str, Any]]],
        *,
        command_suffix: str = "",
        ready: bool = False,
    ) -> None:
        """为若干 (attempt job uuid, spec) 建库存 reservation，all-or-nothing。

        reservation 绑定 attempt 的 job uuid（执行器按 job_id 校验），所以任务启动时
        为每个节点运行的首个 attempt 预留；retry 的新 attempt 再单独预留一次。
        """

        requests = [
            InventoryReservationCreate(
                task_uuid=str(task["uuid"]),
                node_uuid=str(spec["workflow_node_uuid"]),
                job_uuid=job_uuid,
                scheduler_revision=spec["scheduler_revision"],
                requirements=spec["inventory_requirements"],
            )
            for job_uuid, spec in targets
            if spec["inventory_requirements"] and (ready or not spec.get("inventory_deferred"))
            and spec.get("inventory_job_status", "pending") not in _RUN_TERMINAL
        ]
        if not requests:
            return
        if self.materials_gateway is None:
            raise BackendSchedulingError(
                "workflow declares inventory requirements but materials authority "
                "is unavailable"
            )
        from unilabos.server.backend.inventory import ExecutionInventoryCoordinator
        from unilabos.protocol.base import canonical_hash
        task_uuid = str(task["uuid"])
        existing = {item.job_uuid: item for item in self.materials_gateway.list_inventory_reservations(task_uuid=task_uuid)}
        recovered, missing = [], []
        for request in requests:
            reservation = existing.get(request.job_uuid)
            if reservation is None:
                missing.append(request)
                continue
            ExecutionInventoryCoordinator._validate_reservation(reservation, {
                "task_id": task_uuid, "job_id": request.job_uuid, "node_id": request.node_uuid,
                "scheduler_revision": request.scheduler_revision, "inventory_requirements": request.requirements,
            }, allowed_statuses=frozenset({"active"}))
            recovered.append(reservation)
        if missing:
            value = InventoryTaskReservationCreate(task_uuid=task_uuid,
                scheduler_revision=missing[0].scheduler_revision,
                reservations=sorted(missing, key=lambda item: item.job_uuid))
            # 重试、循环和部分节点已完成会改变预留集合；同一集合保持固定幂等身份。
            digest = canonical_hash(value.model_dump(mode="json", exclude_none=False))
            mutation = InventoryMutation(
                command_uuid=self._inventory_command_uuid(task_uuid, ":batch:" + digest + (command_suffix if ready else "")),
                effect_key="inventory.task.reserve", operation="reserve_task_inventory", actor_type="scheduler")
            result = self.materials_gateway.reserve_task_inventory(mutation, value)
            recovered.extend(result.data.reservations)
        specs_by_job = {job_uuid: spec for job_uuid, spec in targets}
        for reservation in recovered:
            spec = specs_by_job.get(reservation.job_uuid)
            if spec is not None:
                spec["inventory_reservation_uuid"] = reservation.reservation_uuid
                spec["reserved_material_uuids"] = sorted(
                    {
                        item.material_uuid
                        for item in reservation.items
                        if item.kind == "material"
                        and item.material_uuid is not None
                    }
                )
                # 权威解析出的出库内容：按需求 key 归并，派发时注入同名动作参数
                spec["inventory_allocations"] = allocation_arguments(
                    reservation.items, action_schema=spec.get("param_schema"),
                )

    def _reserve_ready_inventory(self, task: Dict[str, Any], node: DagNode) -> bool:
        from .attempt_inventory import freeze_inventory, inventory_wait
        from unilabos.protocol.runtime.input_binding import map_input_bindings, literal_input
        from unilabos.protocol.runtime.value_binding import resolve_result_bindings

        spec = self._run_specs[node.node_id]
        job_uuid = spec["current_job_uuid"]
        if spec.get("inventory_reservation_uuid"):
            return True
        def resolve():
            current = self.workflow.get_workflow_task(str(task["uuid"]))
            values = current.get("input") or {}
            def entry(name):
                if name not in values:
                    raise ValueError(f"库存缺少入口参数：{name}")
                return literal_input(values[name])
            requirements = map_input_bindings(spec["inventory_template"], entry)
            return resolve_result_bindings(requirements, lambda source: self._condition_result(spec, source),
                                           read_path=json_get_exists)
        spec["inventory_requirements"] = freeze_inventory(self.workflow, self._authority_epoch, job_uuid, resolve)
        from unilabos.protocol.materials import InventoryRequirement
        existing = self.materials_gateway.list_inventory_reservations(task_uuid=str(task["uuid"]))
        if not any(item.job_uuid == job_uuid for item in existing):
            preview = self.materials_gateway.preview_inventory([
                InventoryRequirement.model_validate(item) for item in spec["inventory_requirements"]])
            if not preview["available"]:
                reason = "；".join(str(item.get("message") or "库存不足") for item in preview["shortages"])
                inventory_wait(self.workflow, self._authority_epoch, job_uuid, reason)
                return False
        generation = (self._current_job_metadata(job_uuid).get("control_data") or {}).get("inventory_generation", 0)
        try:
            self._reserve_inventory(task, [(job_uuid, spec)], ready=True, command_suffix=f":ready:{generation}")
        except Exception as error:
            if getattr(error, "code", None) != "insufficient_inventory":
                raise
            inventory_wait(self.workflow, self._authority_epoch, job_uuid, str(error), advance=True)
            return False
        inventory_wait(self.workflow, self._authority_epoch, job_uuid, None)
        return True

    def _reserve_task_inventory(
        self,
        task: Dict[str, Any],
        specs: Dict[str, Dict[str, Any]],
    ) -> None:
        self._reserve_inventory(
            task,
            [(spec["current_job_uuid"], spec) for spec in specs.values()],
        )

    def _release_unconsumed_task_inventory(self, task_uuid: str) -> None:
        # 持久 attempt 事实决定补扣、释放或隔离；不能把 task 终态等同于全部动作未执行。
        self._reconcile_inventory()

    def _resolve_action_args(self, run_uuid: str) -> Dict[str, Any]:
        spec = self._run_specs[run_uuid]
        target_node = spec["workflow_node_uuid"]
        result: Any = dict(spec["base_param"])
        for edge in spec["edges"]:
            if str(edge.get("target_node_uuid")) != target_node:
                continue
            if edge.get("dependency_only"):
                continue
            source_key = str(edge.get("source_data_key") or "")
            target_key = str(edge.get("target_data_key") or "")
            if not source_key or not target_key:
                continue
            source_run_uuid = spec["runs_by_node"].get(
                str(edge.get("source_node_uuid"))
            )
            if not source_run_uuid:
                raise ParamResolveError("source workflow node run is missing")
            # 节点运行的 return_info 是当前 attempt 的投影：上游若经历过 retry，
            # 这里拿到的就是重试后的结果。
            source_run = self.workflow.get_workflow_node_run(source_run_uuid)
            value: Any = (source_run.get("return_info") or {}).get("return_value")
            exists, value = json_get_exists(value, source_key)
            if not exists:
                raise ParamResolveError(
                    f"value not exist: source data_key {source_key!r}"
                )
            keys = target_key.split("@@@")
            for nested in keys[:-1]:
                exists, value = json_get_exists(value, nested)
                if not exists:
                    raise ParamResolveError(
                        f"value not exist: nested target key {nested!r}"
                    )
            result = json_set(result, keys[-1], value)
        context = self._loop_context_for(run_uuid)
        if context is not None:
            # 循环体节点的 {{loop.index}} / {{loop.iteration}} / {{loop.count}}：取最内层循环的当前轮
            result = substitute_loop_placeholders(result, context)
        from unilabos.protocol.runtime.value_binding import resolve_result_bindings

        result = resolve_result_bindings(result, lambda source: self._condition_result(spec, source),
                                         read_path=json_get_exists)
        return dict(result)

    def _loop_context_for(self, run_uuid: str) -> Optional[Dict[str, Any]]:
        """节点运行所在最内层循环的当前迭代变量；不在循环体内返回 None。"""

        with self._guard:
            parent = self._run_specs.get(run_uuid, {}).get("parent_run_uuid")
            while parent is not None:
                loop_spec = self._run_specs.get(parent, {})
                context = loop_spec.get("loop_context")
                if context is not None:
                    return dict(context)
                parent = loop_spec.get("parent_run_uuid")
        return None

    def _condition_result(self, spec: Dict[str, Any], node_uuid: str) -> Any:
        """条件读取当前节点运行的成功结果；不接受上一轮或被跳过的结果。"""
        run_uuid = spec["runs_by_node"].get(node_uuid)
        if not run_uuid:
            raise ValueError(f"条件引用的节点不存在：{node_uuid}")
        run = self.workflow.get_workflow_node_run(run_uuid)
        info = run.get("return_info") or {}
        if run["status"] != "succeeded" or "return_value" not in info:
            raise ValueError(f"条件引用的节点没有成功结果：{node_uuid}")
        return info["return_value"]

    def _run_condition_control(self, task: Dict[str, Any], node: DagNode) -> Optional[NodeState]:
        """本地判定和跳过不消费单步许可；只有被选择的设备动作申请资源与许可。"""
        from unilabos.protocol.runtime.condition import condition_guards, evaluate_condition, strict_bool

        spec = self._run_specs[node.node_id]
        guards = condition_guards(spec.get("execution_policy") or {})
        if not guards and node.kind != "condition":
            return None
        if self._task_cancelled(task):
            return NodeState.CANCELLED
        status = "succeeded"
        value: Dict[str, Any] = {}
        errors: list[Dict[str, Any]] = []
        try:
            for guard in guards:
                result = self._condition_result(spec, guard["node_uuid"])
                if not isinstance(result, Mapping) or "matched" not in result:
                    raise ValueError("条件节点缺少 matched 结果")
                if strict_bool(result["matched"]) != guard["equals"]:
                    status = "skipped"
                    value = {"reason": "condition_not_selected", "condition_node_uuid": guard["node_uuid"]}
                    break
            if status != "skipped":
                if node.kind != "condition":
                    return None
                self.workflow.mark_workflow_node_job_running(str(spec["current_job_uuid"]))
                current_input = self.workflow.get_workflow_task(str(task["uuid"])).get("input") or {}
                value = {"matched": evaluate_condition(spec["base_param"], lambda key: self._condition_result(spec, key),
                                                       read_input=current_input.__getitem__)}
        except (ValueError, TypeError, KeyError) as exc:
            status = "failed"
            errors = [{"code": "condition_failed", "message": str(exc)}]
        self.workflow.record_workflow_node_job_terminal(
            str(spec["current_job_uuid"]), status=status,
            return_info={"suc": status == "succeeded", "suc_type": "condition", "return_value": value},
            error_info=errors,
        )
        if status == "skipped" and node.is_loop:
            for child in spec.get("body_run_uuids") or []:
                self.workflow.record_workflow_node_job_terminal(
                    str(self._run_specs[child]["current_job_uuid"]), status="skipped",
                    return_info={"suc": False, "suc_type": "condition", "return_value": value}, error_info=[],
                )
        return _run_status_to_state(status)

    # ── 循环容器 ──────────────────────────────────────────────

    async def _run_loop_node(
        self,
        task: Dict[str, Any],
        node: DagNode,
        run_body: RunBodyFn,
    ) -> NodeState:
        """驱动一个循环节点：按 LoopSpec 决定轮数，每轮重臂循环体 attempt 后跑循环体子 DAG。

        - ``for``：固定 ``count`` 轮；``while``：每轮前求值条件，假即结束；达到
          ``max_iterations`` 视为失败（条件永不为假是配置错误，不能无限占用设备）。
        - 循环体某节点失败 / 被取消：循环节点同态收敛，外层 fail-fast 与普通节点一致。
        - 循环节点自身没有执行器 job：attempt 由这里直接 running → 终态，``return_value``
          给出轮数；每轮的迭代变量写在 ``control_data.loop``，供前端展示与下游读取。
        - 重启恢复：从 ``control_data.loop.iteration`` 记录的轮次继续，本轮已成功的循环体
          节点不重跑（DagWalk completed）。
        """

        spec = self._run_specs[node.node_id]
        local_state = self._run_condition_control(task, node)
        if local_state is not None:
            return local_state
        loop_spec = LoopSpec.model_validate(node.loop_spec)
        job_uuid = str(spec["current_job_uuid"])
        body_runs: list[str] = list(spec.get("body_run_uuids") or [])
        body_node_ids = set(node.body.nodes) if node.body is not None else set()

        def resolve_status(status: str, return_value: Dict[str, Any], error: Optional[str]) -> NodeState:
            self._settle_loop_run(job_uuid, status=status, return_value=return_value, error=error)
            return _run_status_to_state(status)

        try:
            self.workflow.mark_workflow_node_job_running(job_uuid)
        except Exception as exc:  # noqa: BLE001 - attempt 已终态等异常按失败收敛
            logger.exception("loop node %s cannot start", spec["workflow_node_uuid"])
            return resolve_status("failed", {"iterations": 0}, f"loop start failed: {exc}")

        iteration = self._recovered_loop_iteration(node.node_id)
        resumed = iteration > 0 or self._loop_body_in_progress(body_runs)
        completed_iterations = iteration
        while True:
            if self._task_cancelled(task):
                return resolve_status("canceled", {"iterations": completed_iterations}, None)
            if resumed:
                # 恢复到一轮的中途：先把这一轮跑完，不重新判定条件
                pass
            elif loop_spec.mode == "for":
                assert loop_spec.count is not None
                if iteration >= loop_spec.count:
                    break
            else:
                if iteration >= loop_spec.max_iterations:
                    return resolve_status(
                        "failed",
                        {"iterations": completed_iterations},
                        f"while 循环达到 max_iterations={loop_spec.max_iterations} 仍未结束"
                        f"（条件：{loop_spec.condition.describe() if loop_spec.condition else ''}）",
                    )
                try:
                    proceed = self._evaluate_loop_condition(task, spec, loop_spec.condition)
                except Exception as exc:  # noqa: BLE001 - 条件不可求值是配置/数据错误
                    logger.warning(
                        "loop node %s condition failed: %s", spec["workflow_node_uuid"], exc
                    )
                    return resolve_status(
                        "failed",
                        {"iterations": completed_iterations},
                        f"while 条件无法求值：{exc}",
                    )
                if not proceed:
                    break
            progress = {
                "mode": loop_spec.mode,
                "count": loop_spec.count,
                "max_iterations": loop_spec.max_iterations if loop_spec.mode == "while" else None,
                "condition": (
                    loop_spec.condition.describe() if loop_spec.condition is not None else None
                ),
            }
            context = loop_context(iteration, loop_spec.count)
            with self._guard:
                spec["loop_context"] = context
            completed: list[str] = []
            if resumed:
                # 恢复：沿用本轮已有的 attempt（在飞的由裁决收敛），已成功的循环体节点不重跑
                completed = self._completed_body_runs(body_runs, body_node_ids)
                resumed = False
            else:
                try:
                    self._begin_loop_iteration(task, node.node_id, iteration, progress, body_runs)
                except Exception as exc:  # noqa: BLE001 - 重臂失败（库存不足等）按失败收敛
                    logger.exception(
                        "loop node %s failed to arm iteration %s",
                        spec["workflow_node_uuid"],
                        iteration,
                    )
                    return resolve_status(
                        "failed",
                        {"iterations": completed_iterations},
                        f"第 {iteration + 1} 轮无法开始：{exc}",
                    )
            result = await run_body(completed)
            for body_run_uuid, state in result.items():
                self._persist_terminal_if_needed(body_run_uuid, state)
            if any(state == NodeState.FAILED for state in result.values()):
                return resolve_status(
                    "failed",
                    {"iterations": completed_iterations, "failed_iteration": iteration + 1},
                    f"第 {iteration + 1} 轮循环体执行失败",
                )
            if any(state != NodeState.SUCCESS for state in result.values()):
                return resolve_status("canceled", {"iterations": completed_iterations}, None)
            iteration += 1
            completed_iterations = iteration
            if loop_spec.interval_seconds > 0:
                await asyncio.sleep(loop_spec.interval_seconds)
        return resolve_status("succeeded", {"iterations": completed_iterations, "mode": loop_spec.mode}, None)

    def _task_cancelled(self, task: Dict[str, Any]) -> bool:
        with self._guard:
            runner = self._runners.get(str(task["uuid"]))
        return runner is None or bool(getattr(runner, "cancelled", False))

    def _recovered_loop_iteration(self, run_uuid: str) -> int:
        """重启恢复时从循环节点运行的 control_data.loop.iteration 续跑。"""

        try:
            run = self.workflow.get_workflow_node_run(run_uuid)
        except Exception:  # noqa: BLE001 - 精简测试替身可能没有该方法
            return 0
        loop_state = (run.get("control_data") or {}).get("loop") or {}
        iteration = loop_state.get("iteration")
        return int(iteration) if isinstance(iteration, int) and iteration > 0 else 0

    def _loop_body_in_progress(self, body_runs: Iterable[str]) -> bool:
        """循环体里已有节点跑完（succeeded/skipped）：说明是恢复而不是首轮起跑。"""

        for run_uuid in body_runs:
            try:
                run = self.workflow.get_workflow_node_run(run_uuid)
            except Exception:  # noqa: BLE001
                return False
            if str(run.get("status") or "") in {"succeeded", "skipped"}:
                return True
        return False

    def _completed_body_runs(self, body_runs: Iterable[str], body_node_ids: set[str]) -> list[str]:
        completed: list[str] = []
        for run_uuid in body_runs:
            if run_uuid not in body_node_ids:
                continue
            try:
                run = self.workflow.get_workflow_node_run(run_uuid)
            except Exception:  # noqa: BLE001
                continue
            if str(run.get("status") or "") in {"succeeded", "skipped"}:
                completed.append(run_uuid)
        return completed

    def _begin_loop_iteration(
        self,
        task: Dict[str, Any],
        loop_run_uuid: str,
        iteration: int,
        progress: Dict[str, Any],
        body_runs: list[str],
    ) -> None:
        """持久化本轮开始：循环体节点运行追加新 attempt，并把调度器簿记切到新 attempt。"""

        outcome = self.workflow.begin_workflow_loop_iteration(
            loop_run_uuid,
            iteration=iteration,
            progress=progress,
            body_run_uuids=body_runs,
        )
        rearmed: list[tuple[str, Dict[str, Any]]] = []
        for run_uuid, next_job in (outcome.get("next_jobs") or {}).items():
            spec = self._run_specs.get(run_uuid)
            if spec is None:
                continue
            with self._guard:
                self._arm_next_attempt(spec, next_job, retry_of=None)
            if spec.get("inventory_requirements"):
                rearmed.append((str(spec["current_job_uuid"]), spec))
        if rearmed:
            # 库存 reservation 绑定 attempt：每轮为循环体重新预留一次（与 retry 同构）
            self._reserve_inventory(
                task,
                rearmed,
                command_suffix=f":loop:{loop_run_uuid}:{iteration}",
            )

    def _arm_next_attempt(
        self, spec: Dict[str, Any], next_job: Dict[str, Any], *, retry_of: Optional[str]
    ) -> None:
        """把 spec 的当前 attempt 切到 store 追加的新 attempt（retry / 循环下一轮共用）。"""

        spec["current_job_uuid"] = str(next_job["uuid"])
        spec["inventory_job_status"] = "pending"
        spec["attempt_no"] = int(next_job.get("attempt_no") or spec["attempt_no"] + 1)
        spec["retry_of_job_uuid"] = (
            str(next_job.get("retry_of_job_uuid") or retry_of) if retry_of else None
        )
        if retry_of:
            spec["attempt_trigger"] = str(next_job.get("trigger") or "retry_decision")
            spec["retry_count"] = int(spec.get("retry_count") or 0) + 1
        else:
            # 循环下一轮：新一轮从零计重试
            spec["attempt_trigger"] = str(next_job.get("trigger") or "loop_iteration")
            spec["retry_count"] = 0
        spec["job_uuids"].append(str(next_job["uuid"]))
        spec.pop("inventory_reservation_uuid", None)
        spec.pop("inventory_allocations", None)
        spec["inventory_requirements"] = list(spec.get("inventory_template") or [])
        spec["reserved_material_uuids"] = []

    def _evaluate_loop_condition(
        self,
        task: Dict[str, Any],
        loop_spec: Dict[str, Any],
        condition: Optional[LoopCondition],
    ) -> bool:
        """while 条件：设备状态字段 / 某节点最近一次成功输出 与 value 比较。"""

        if condition is None:
            return False
        if condition.source == "device_state":
            snapshot = self._read_device_state(str(condition.device_id))
            if condition.field not in snapshot:
                if condition.op == "exists":
                    return False
                raise BackendSchedulingError(
                    f"设备 {condition.device_id} 没有状态字段 {condition.field!r}"
                    f"（已知字段：{sorted(snapshot)}）"
                )
            return compare_condition(condition.op, snapshot[condition.field], condition.value)
        run_uuid = loop_spec["runs_by_node"].get(str(condition.node_uuid))
        if not run_uuid:
            raise BackendSchedulingError(
                f"条件引用的节点 {condition.node_uuid} 不在本任务里"
            )
        run = self.workflow.get_workflow_node_run(run_uuid)
        if str(run.get("status") or "") not in {"succeeded", "skipped"}:
            # 被引用节点还没有产出（首轮之前 / 上轮没轮到它）：继续，让循环体至少跑一轮
            return True
        value: Any = (run.get("return_info") or {}).get("return_value")
        if condition.data_key:
            exists, value = json_get_exists(value, condition.data_key)
            if not exists:
                if condition.op == "exists":
                    return False
                raise BackendSchedulingError(
                    f"节点 {condition.node_uuid} 的返回值里没有 {condition.data_key!r}"
                )
        return compare_condition(condition.op, value, condition.value)

    def _read_device_state(self, device_id: str) -> Dict[str, Any]:
        """设备最新状态字段 -> 值；来源是注入的读取器或执行适配器的设备状态投影。"""

        reader = self._device_state_reader
        if reader is None:
            projection = getattr(self.executor, "device_state", None)
            latest_for = getattr(projection, "latest_for", None)
            if not callable(latest_for):
                raise BackendSchedulingError(
                    "本机没有设备状态投影，无法对设备状态做 while 判断"
                )
            reader = latest_for
        snapshot = reader(device_id) or {}
        values: Dict[str, Any] = {}
        for field, item in dict(snapshot).items():
            # 投影形状 {prop: {"value", "updated_at", ...}}；也接受已经展平的 {prop: value}
            if isinstance(item, Mapping) and "value" in item:
                values[str(field)] = item["value"]
            else:
                values[str(field)] = item
        return values

    def _settle_loop_run(
        self,
        job_uuid: str,
        *,
        status: str,
        return_value: Dict[str, Any],
        error: Optional[str],
    ) -> None:
        """循环节点 attempt 的终态：没有执行器回报，由调度器直接落表。"""

        error_info: list[Dict[str, Any]] = []
        if status == "failed":
            error_info = [{"code": "loop_failed", "message": error or "loop failed"}]
        try:
            self.workflow.record_workflow_node_job_terminal(
                job_uuid,
                status=status,
                return_info={
                    "suc": status == "succeeded",
                    "suc_type": "loop",
                    "return_value": return_value,
                },
                error_info=error_info,
            )
        except Exception:  # noqa: BLE001 - 已终态（取消收敛先到）等情况不再改写
            logger.exception("failed to settle loop attempt %s", job_uuid)

    def _on_manual_confirmation_decided(
        self, confirmation: Dict[str, Any]
    ) -> None:
        """消费 durable 人工决策，将对应 attempt 收敛为节点终态。"""

        status = str(confirmation.get("status") or "pending")
        if status == "pending":
            return
        job_uuid = str(confirmation.get("workflow_node_job_uuid") or "")
        if not job_uuid:
            return
        with self._guard:
            run_uuid = self._job_runs.get(job_uuid)
            task_uuid = self._run_to_task.get(run_uuid or "")
            runner = self._runners.get(task_uuid or "")
        # 决策可以先于新进程接管到达；事实已在 DB，恢复时 _dispatch_held_node
        # 会再次读到终态并调用本方法，此处不凭空修改未知任务。
        if run_uuid is None or task_uuid is None:
            return

        decision_action = str(
            (confirmation.get("meta_data") or {}).get("decision_action") or ""
        ).strip().lower()
        if status == "approved":
            job_status = "skipped" if decision_action == "skip" else "succeeded"
            node_state = NodeState.SUCCESS
        elif status == "rejected":
            job_status = "failed"
            node_state = NodeState.FAILED
        elif status == "timed_out":
            job_status = "timeout"
            node_state = NodeState.FAILED
        elif status == "canceled":
            job_status = "canceled"
            node_state = NodeState.CANCELLED
        else:
            logger.warning(
                "ignore unknown manual confirmation status %s for job %s",
                status,
                job_uuid,
            )
            return

        try:
            outcome = self.workflow.record_workflow_node_job_terminal(
                job_uuid,
                status=job_status,
                return_info={
                    "suc": job_status in {"succeeded", "skipped"},
                    "suc_type": "manual_confirm",
                    "return_value": {
                        "confirmation_uuid": confirmation.get("uuid"),
                        "status": status,
                        "decision_action": decision_action or None,
                        "confirmed_by": confirmation.get("confirmed_by"),
                        "comment": confirmation.get("comment"),
                    },
                },
                error_info=(
                    []
                    if job_status in {"succeeded", "skipped", "canceled"}
                    else [{"code": f"manual_confirmation_{status}"}]
                ),
            )
        except Exception:  # noqa: BLE001 - 保留 durable 决策，等待恢复重试
            logger.exception(
                "failed to settle manual confirmation %s for job %s",
                confirmation.get("uuid"),
                job_uuid,
            )
            return
        self._release_job_resources(
            job_uuid,
            canceled=job_status in {"failed", "timeout", "canceled"},
        )
        run = outcome.get("run") or {}
        if runner is not None and str(run.get("status") or "") in _RUN_TERMINAL:
            runner.notify_terminal(run_uuid, node_state)

    def _on_executor_finished(
        self,
        job_id: str,
        success: bool,
        ret_value: Any,
        suc_type: str = "normal",
        return_info: Optional[Dict[str, Any]] = None,
    ) -> None:
        with self._guard:
            owned = job_id in self._job_runs
        if not owned:
            # 非本调度器派发的 job（如 Backend-controlled 下发的 execution_job）
            return
        if suc_type == SUCCESS_TYPE_EXECUTION_UNKNOWN:
            self._reconcile_lost_attempt(job_id, return_info or {})
            return
        # 真实结果晚于"结果未知"到达（例如重启后的 Host 补发旧事件）：以真实结果收敛并撤掉待裁决。
        with self._guard:
            stale = [
                (decision_id, pending)
                for decision_id, pending in self._reconciliation_decisions.items()
                if pending["job_uuid"] == job_id
            ]
            for decision_id, _pending in stale:
                self._reconciliation_decisions.pop(decision_id, None)
        if success:
            job_status = "skipped" if suc_type == "skip" else "succeeded"
        elif suc_type == SUCCESS_TYPE_CANCELLATION:
            # 执行面被取消（运行时页取消 job / 停机撤单）不是设备失败：attempt 落 canceled，
            # 节点走 CANCELLED（不触发失败决策与重试），任务终态为 canceled 而非 failed。
            job_status = "canceled"
        else:
            job_status = "failed"
        resolution = (
            (return_info or {}).get("error_resolution")
            if isinstance(return_info, dict)
            else None
        )
        self._settle_attempt(
            job_id,
            job_status=job_status,
            return_info={
                "suc": success,
                "suc_type": suc_type,
                "return_value": ret_value,
            },
            error_info=(
                [] if job_status != "failed" else [_failure_error_info(return_info)]
            ),
            resolution=resolution if isinstance(resolution, dict) else None,
        )
        for task_uuid in {pending["task_uuid"] for _decision_id, pending in stale}:
            self._settle_task_reconciliation(task_uuid)

    def _settle_attempt(
        self,
        job_id: str,
        *,
        job_status: str,
        return_info: Dict[str, Any],
        error_info: list[Dict[str, Any]],
        resolution: Optional[Dict[str, Any]],
    ) -> None:
        """attempt 终态收敛的唯一入口：执行器回报与重启后的人工裁决都走这里。"""

        with self._guard:
            run_uuid = self._job_runs.get(job_id)
            task_uuid = self._run_to_task.get(run_uuid or "")
            runner = self._runners.get(task_uuid or "")
            context = self._run_context.get(run_uuid or "")
            spec = self._run_specs.get(run_uuid or "")
        with self._guard:
            recovery_waiter = self._recovery_waiters.get(task_uuid or "")
        if run_uuid is None or task_uuid is None or spec is None or (runner is None and recovery_waiter is None):
            return
        from .physical_recovery import PhysicalSettlementPending
        try:
            self._physical_recovery.settle(job_id, job_status=job_status, return_info=return_info,
                error_info=error_info, resolution=resolution)
        except PhysicalSettlementPending as error:
            self._execution_claims.mark_uncertain(job_id)
            self._reconcile_lost_attempt(job_id, {"uncertainty_reason": str(error)})
            return
        # attempt 终态先落表并投影到节点运行；retry 决策由 store 在同一事务里追加新 attempt
        outcome = self.workflow.record_workflow_node_job_terminal(
            job_id,
            status=job_status,
            return_info=return_info,
            error_info=error_info,
            error_resolution=resolution,
        )
        run = outcome["run"]
        self._release_job_resources(job_id, canceled=False)

        if recovery_waiter is not None:
            # 取消和单个分支失败都不能提前结束其他待核实动作。
            remaining = self.workflow.list_workflow_node_runs(task_uuid)
            if not any(item["status"] in _RUN_NEEDS_RECONCILIATION for item in remaining):
                recovery_waiter[0].call_soon_threadsafe(recovery_waiter[1].set)
            self._reconcile_resources()
            return

        next_job = outcome.get("next_job")
        if next_job is not None and context is not None:
            # retry：store 已在同一事务里追加新 attempt 并把节点运行切回 pending；
            # 这里只需为新 attempt 重新预留库存、申请资源并下发，DAG 节点不终结。
            task, node = context
            with self._guard:
                # store 在 retry 决策事务中会写入 retry_of_job_uuid；缺失时用当前
                # attempt 作为保守兜底，保证 runtime.v1 的 retry 链仍可验证。
                self._arm_next_attempt(spec, next_job, retry_of=job_id)
            logger.info(
                "workflow node %s retrying as attempt %s (job %s -> %s)",
                spec["workflow_node_uuid"],
                spec["attempt_no"],
                job_id,
                next_job["uuid"],
            )
            try:
                self._reserve_inventory(
                    task,
                    [(spec["current_job_uuid"], spec)],
                    command_suffix=f":{spec['current_job_uuid']}",
                )
                self._start_node(task, node)
            except Exception:  # noqa: BLE001 - 新 attempt 起不来按节点失败收敛
                logger.exception(
                    "workflow node %s failed to start retry attempt %s",
                    spec["workflow_node_uuid"],
                    next_job["uuid"],
                )
                self._persist_terminal_if_needed(run_uuid, NodeState.FAILED)
                runner.notify_terminal(run_uuid, NodeState.FAILED)
            return

        if run["status"] in _RUN_TERMINAL:
            runner.notify_terminal(run_uuid, _run_status_to_state(str(run["status"])))

    def publish_job_error_decision_required(self, report: Dict[str, Any]) -> bool:
        """执行面决策桥：本机派发的 attempt 失败并挂起等待决策。"""

        job_uuid = str(report.get("job_id") or "")
        with self._guard:
            owned = job_uuid in self._job_runs
        if not owned:
            return False
        self.workflow.mark_workflow_node_job_decision_pending(job_uuid, report)
        return True

    def publish_job_error_decision_resumed(self, report: Dict[str, Any]) -> bool:
        """执行面决策桥：``execution_timeout`` 决策以 ``wait`` 收敛，或动作在等待期间
        真实完成——attempt 与节点运行从 ``intervention_required`` 收回 ``running``。"""

        job_uuid = str(report.get("job_id") or "")
        with self._guard:
            owned = job_uuid in self._job_runs
        if not owned:
            return False
        self.workflow.mark_workflow_node_job_decision_resumed(
            job_uuid, str(report.get("decision_id") or "")
        )
        return True

    # ── 重启后的执行态裁决 ──────────────────────────────────────

    def _reconcile_lost_attempt(self, job_id: str, return_info: Dict[str, Any]) -> None:
        """执行端在动作进行中重启：attempt 转 execution_unknown 并开裁决。

        与调度权威重启后的恢复同一语义：不重放、不直接判失败，由人选择重试 / 跳过 /
        替换为成功 / 标记失败。该 attempt 已占有的资源保持到裁决收敛——设备实际状态
        未知时不放行其他任务。
        """

        reason = str(
            return_info.get("uncertainty_reason")
            or "execution host restarted while the action was running"
        )
        with self._guard:
            run_uuid = self._job_runs.get(job_id)
            context = self._run_context.get(run_uuid or "")
            spec = self._run_specs.get(run_uuid or "")
        if run_uuid is None or context is None or spec is None or spec.get("current_job_uuid") != job_id:
            return
        if not self.workflow.mark_workflow_node_job_execution_unknown(job_id, reason):
            return
        self._execution_claims.mark_uncertain(job_id)
        self._intervals.release_job(job_id)
        task, node = context
        self._open_reconciliation_decision(task, node, spec, "execution_unknown")

    def _hold_recovered_resources(
        self,
        task: Dict[str, Any],
        node: DagNode,
        spec: Dict[str, Any],
    ) -> None:
        """进程重启后，为待裁决的遗留 attempt 重新登记它的动作锁 / 物料锁。

        新版本以派发前冻结的持久声明为准，启动时已恢复全部占用，不重新解释驱动注册表。
        旧库没有声明的在飞尝试按历史计划展示；核实完成前全局派发闸门保持关闭。
        """

        job_uuid = str(spec["current_job_uuid"])
        claim = self._execution_claims.get(job_uuid)
        if claim is not None:
            self._execution_claims.mark_uncertain(job_uuid)
            return
        material_uuids: set[str] = set()
        try:
            args = self._resolve_action_args(node.node_id)
            args.update(spec.get("inventory_allocations") or {})
            material_uuids = set(
                material_uuids_for_parameters(
                    self._material_lock_parameters(node.device_id, node.action),
                    args,
                )
            )
        except Exception:  # noqa: BLE001 - 物料解析失败不影响设备锁
            logger.warning(
                "恢复的 attempt %s 无法解析物料锁，只保留设备动作锁", job_uuid, exc_info=True
            )
        material_uuids.update(spec.get("reserved_material_uuids") or ())
        try:
            self.resources.acquire(
                SchedulerResourceRequest(
                    request_uuid=f"resource:{job_uuid}",
                    owner_uuid=job_uuid,
                    task_uuid=str(task["uuid"]),
                    current_action=ActionLockClaim(
                        device_id=node.device_id,
                        action_name=node.action,
                    ),
                    always_free=self._action_always_free(node, spec),
                    material_lock_claims=[
                        MaterialLockClaim(material_uuid=material_uuid)
                        for material_uuid in sorted(material_uuids)
                    ],
                )
            )
        except Exception:  # noqa: BLE001 - 占用失败也要能开裁决
            logger.exception("恢复的 attempt %s 重新占用资源失败", job_uuid)

    def _open_reconciliation_decision(
        self,
        task: Dict[str, Any],
        node: DagNode,
        spec: Dict[str, Any],
        recovered_status: str,
    ) -> None:
        """把上一进程留下的 attempt 变成一条待裁决记录，报告形状与执行面的失败决策一致。"""

        job_uuid = str(spec["current_job_uuid"])
        task_uuid = str(task["uuid"])
        try:
            job = self.workflow.get_workflow_node_job(job_uuid)
        except Exception:  # noqa: BLE001 - 读不到明细也要能开裁决
            job = {}
        attempt_no = int(job.get("attempt_no") or spec.get("attempt_no") or 1)
        previous = (job.get("control_data") or {}).get("pending_decision") or {}
        if recovered_status == "execution_unknown":
            exception_type = "ExecutionStateUnknown"
            detail = str(
                job.get("uncertainty_reason")
                or "process restarted with an in-flight workflow node job"
            )
            error_message = (
                f"进程重启时该动作正在执行（attempt {attempt_no}），无法确认设备是否已完成"
                f"：{detail}。请核对设备实际状态后选择重试、跳过、替换为成功或标记失败。"
            )
            category = "execution_state_unknown"
        else:
            exception_type = str(previous.get("exception_type") or "DeviceActionError")
            original = str(previous.get("error_message") or "").strip()
            error_message = "该动作失败后等待决策，进程重启导致原决策上下文丢失，请重新裁决" + (
                f"：{original}" if original else ""
            )
            category = "execution"
        decision_id = str(uuid5(_RECONCILIATION_NAMESPACE, job_uuid))
        report: Dict[str, Any] = {
            "decision_id": decision_id,
            "device_id": node.device_id,
            "action_name": node.action,
            "task_id": task_uuid,
            "job_id": job_uuid,
            "node_id": str(spec["workflow_node_uuid"]),
            "node_run_uuid": node.node_id,
            "exception_type": exception_type,
            "error_message": error_message,
            "traceback": "",
            "options": [dict(option) for option in _RECONCILIATION_OPTIONS],
            "retry_count": max(attempt_no - 1, 0),
            "max_retries": self._action_max_retries(node.device_id, node.action),
            "created_at": time.time(),
            "require_confirmation": True,
            "category": category,
            "severity": "warning",
            "recovered_status": recovered_status,
        }
        physical = self._physical_recovery.get(job_uuid)
        if physical and (physical["request"].get("moves") or physical["request"].get("aliquot")):
            report["category"] = "physical_settlement"
            report["error_message"] = str((job.get("control_data") or {}).get("physical_outcome", {}).get("error") or error_message)
            report["options"] = [option for option in report["options"] if option["action"] in {"operator_intervention", "abort"}]
            report["material_execution"] = physical
        with self._guard:
            self._run_context[node.node_id] = (task, node)
            self._job_runs[job_uuid] = node.node_id
            self._reconciliation_decisions[decision_id] = {
                "report": report,
                "job_uuid": job_uuid,
                "run_uuid": node.node_id,
                "task_uuid": task_uuid,
            }
        self.workflow.mark_workflow_node_job_decision_pending(job_uuid, report)
        logger.info(
            "workflow node %s awaits reconciliation decision %s after restart "
            "(job %s was %s)",
            spec["workflow_node_uuid"],
            decision_id,
            job_uuid,
            recovered_status,
        )

    def _action_max_retries(self, device_id: str, action_name: str) -> int:
        resolver = getattr(self.executor, "resolve_action_error_policy", None)
        policy: Any = {}
        if callable(resolver):
            try:
                policy = resolver(device_id, action_name) or {}
            except Exception:  # noqa: BLE001 - 注册表读取失败退回缺省上限
                policy = {}
        try:
            return int(policy.get("max_retries", 3))
        except (AttributeError, TypeError, ValueError):
            return 3

    def list_reconciliation_decisions(self) -> list[Dict[str, Any]]:
        """待裁决的重启遗留 attempt；与执行面的 ``list_error_decisions`` 同形。"""

        with self._guard:
            return [
                deepcopy(pending["report"])
                for pending in self._reconciliation_decisions.values()
            ]

    def has_reconciliation_decision(self, decision_id: str) -> bool:
        with self._guard:
            return decision_id in self._reconciliation_decisions

    def resolve_reconciliation_decision(
        self, decision_id: str, decision: Dict[str, Any]
    ) -> bool:
        """按用户选择收敛一个重启遗留 attempt；返回 False 表示拒绝或已不在等待。

        retry 由 store 追加新 attempt 正常派发；skip 记 skipped 放行下游；
        operator_intervention 以人工给出的 ``result``（可缺省）记 succeeded；
        abort 记 failed。所有裁决收敛后任务控制态从 waiting_reconciliation 恢复。
        """

        with self._guard:
            pending = self._reconciliation_decisions.get(decision_id)
        if pending is None:
            return False
        report = pending["report"]
        job_uuid = pending["job_uuid"]
        if decision.get("job_id") and str(decision["job_id"]) != job_uuid:
            return False
        if decision.get("device_id") and str(decision["device_id"]) != report["device_id"]:
            return False
        option = decision.get("option")
        if isinstance(option, dict):
            selected = str(option.get("action") or "")
            for key in ("result", "return_value"):
                if key not in decision and key in option:
                    decision[key] = option[key]
        else:
            selected = str(decision.get("action") or option or "")
        if report.get("category") == "physical_settlement" and not str(decision.get("reason") or "").strip():
            return False
        if selected not in {str(item["action"]) for item in report["options"]}:
            return False
        if selected == "retry" and int(report["retry_count"]) >= int(report["max_retries"]):
            logger.warning(
                "retry rejected for reconciliation decision %s: attempt limit %s reached",
                decision_id,
                report["max_retries"],
            )
            return False
        with self._guard:
            if self._reconciliation_decisions.pop(decision_id, None) is None:
                return False

        resolution = {
            "decision_id": decision_id,
            "selected_action": selected,
            "reason": str(decision.get("reason") or ""),
            "scheduler_updated": True,
        }
        error_info: list[Dict[str, Any]] = []
        if selected == "operator_intervention":
            job_status = "succeeded"
            return_info = {
                "suc": True,
                "suc_type": "operator_intervention",
                "return_value": decision.get("result", decision.get("return_value")),
            }
        elif selected == "skip":
            job_status = "skipped"
            return_info = {"suc": True, "suc_type": "skip", "return_value": None}
        else:
            # retry / abort 都先把当前 attempt 记 failed；retry 由 store 在同一事务追加新 attempt
            job_status = "failed"
            return_info = {
                "suc": False,
                "suc_type": "normal",
                "return_value": None,
                "error": report["error_message"],
            }
            error_info = [
                {
                    "code": (
                        "execution_unknown"
                        if report.get("recovered_status") == "execution_unknown"
                        else "action_failed"
                    ),
                    "message": report["error_message"],
                }
            ]
        logger.info(
            "reconciliation decision %s for job %s resolved as %s",
            decision_id,
            job_uuid,
            selected,
        )
        self._settle_attempt(
            job_uuid,
            job_status=job_status,
            return_info=return_info,
            error_info=error_info,
            resolution=resolution,
        )
        self._settle_task_reconciliation(pending["task_uuid"])
        return self.workflow.get_workflow_node_job(job_uuid)["status"] in _RUN_TERMINAL

    def _settle_task_reconciliation(self, task_uuid: str) -> None:
        settle = getattr(self.workflow, "settle_workflow_task_reconciliation", None)
        if not callable(settle):
            return
        try:
            settle(task_uuid)
        except Exception:  # noqa: BLE001 - 控制态恢复失败不影响已落表的 attempt 事实
            logger.exception("failed to settle reconciliation state for task %s", task_uuid)

    def _on_node_terminal(self, run_uuid: str, state: NodeState) -> None:
        self._persist_terminal_if_needed(run_uuid, state)
        with self._guard:
            spec = self._run_specs.get(run_uuid, {})
            job_uuid = spec.get("current_job_uuid")
        if job_uuid:
            self._release_job_resources(
                job_uuid,
                canceled=state is not NodeState.SUCCESS,
            )

    def _persist_terminal_if_needed(self, run_uuid: str, state: NodeState) -> None:
        status = {
            NodeState.SUCCESS: "succeeded",
            NodeState.FAILED: "failed",
            NodeState.CANCELLED: "canceled",
        }.get(state)
        if status is None:
            return
        self.workflow.close_workflow_node_run(run_uuid, status=status)

    def _fail_unstarted_task(
        self,
        task_uuid: str,
        runs: list[Dict[str, Any]],
        error: Exception,
    ) -> None:
        for run in runs:
            if run["status"] not in _RUN_TERMINAL:
                self.workflow.close_workflow_node_run(str(run["uuid"]), status="canceled")
        self.workflow.finish_workflow_task(
            task_uuid,
            status="failed",
            error_info=[
                {"code": "plan_not_executable", "message": str(error)}
            ],
        )


__all__ = ["BackendScheduler", "BackendSchedulingError", "allocation_arguments"]
