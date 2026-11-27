"""把标准工作流任务（WorkflowTask）委托给既有本地调度器。"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from unilabos.app.scheduler.dispatch import CommittedJobOutcome
from unilabos.app.scheduler.inventory.dispatch_admission import (
    AliquotDispatchCondition,
    DispatchAdmissionConflict,
    DispatchAdmissionRequest,
    DispatchFence,
    DispatchResource,
    InventoryRelocationCondition,
    OperateInPlaceCondition,
    TransferDispatchCondition,
)
from unilabos.app.scheduler.inventory.material_io import (
    MaterialIOError,
    canonicalize_load_reagent,
    canonicalize_refill_reagent,
    material_io_change_set,
)
from unilabos.app.scheduler.inventory.station_resource import (
    MaterialTransferCommand,
    StationResourceError,
)
from unilabos.app.scheduler.material_source_resolution import (
    MaterialSourceResolutionCoordinator,
)
from unilabos.app.scheduler.models import DispatchedJob
from unilabos.app.scheduler.resource_wait_policy import (
    is_temporary_resource_condition,
)
from unilabos.app.scheduler.service import EdgeScheduler
from unilabos.workflow.dispatch_permit_saga import (
    freeze_projected_dispatch_permit,
)
from unilabos.workflow.error_control import StationExecutionControl
from unilabos.workflow.execution_lock_lease import (
    list_execution_locks,
    wait_resource_from_execution_lock,
    wait_resource_identity,
)
from unilabos.workflow.execution_restart_recovery import (
    EXECUTION_PROCESS_RESTARTED,
    TASK_ABORTED_BY_RUNTIME_RESTART,
    fail_task_awaiting_restart_cleanup,
)
from unilabos.workflow.manual_confirmation import ManualConfirmationStore
from unilabos.workflow.material_aliquot_settlement import MaterialAliquotSettlement
from unilabos.workflow.material_transfer_settlement import (
    InventoryFactConflict,
    MaterialTransferSettlement,
)
from unilabos.workflow.physical_settlement_policy import (
    MATERIAL_CONTENT_RECONCILIATION_REQUIRED,
    MATERIAL_TRANSFER_RECONCILIATION_REQUIRED,
    QUANTITY_RECONCILIATION_REQUIRED,
)
from unilabos.workflow.quantity_inventory import WorkflowQuantityInventory
from unilabos.workflow.resource_lock_key import device_lock_key
from unilabos.workflow.store import StoreConflict, StoreNotFound, WorkflowStore, utc_now
from unilabos.workflow.task_runtime_projection import (
    CLEANUP_STATUSES_SETTLEABLE_AFTER_TERMINAL,
    TaskRuntimeProjection,
)
from unilabos.workflow.workflow_spec_compiler import WorkflowSpecCompiler

logger = logging.getLogger(__name__)

_DEFAULT_CANCEL_ACK_TIMEOUT_SECONDS = 10.0
_DEFAULT_CANCEL_COMPLETE_TIMEOUT_SECONDS = 60.0

# ``submit_workflow`` 会同步执行第一次重排，因此桥必须知道异常发生在调度器
# 建立运行之前，还是已经进入了持久派发/本地控制回调。后者不能把工作流库中
# 尚未派发的 Task/Job 伪造为 canceled，否则同一任务既不能安全重试，也会丢失
# 原始 pending 事实。
_SUBMISSION_PHASE_SCHEDULER = "scheduler_submit"
_SUBMISSION_PHASE_PRE_DISPATCH = "pre_dispatch"
_SUBMISSION_PHASE_LOCAL_CONTROL = "local_control"


def _operate_condition_mappings(value: Any) -> tuple[dict[str, str], ...]:
    """规范旧单项与新多项派发条件，禁止丢失任一物料事实。"""
    raw = [value] if isinstance(value, Mapping) else value
    fields = {"material_uuid", "site_owner_material_uuid", "site_uuid", "device_material_uuid"}
    if not isinstance(raw, (list, tuple)) or not raw:
        raise StoreConflict("派发原位操作条件集合无效")
    result = []
    for item in raw:
        if (
            not isinstance(item, Mapping)
            or set(item) != fields
            or any(not str(item[field] or "").strip() for field in fields)
        ):
            raise StoreConflict("派发原位操作条件字段不完整")
        result.append({field: str(item[field]).strip() for field in fields})
    if len({item["material_uuid"] for item in result}) != len(result):
        raise StoreConflict("派发原位操作物料身份重复")
    if len({item["device_material_uuid"] for item in result}) != 1:
        raise StoreConflict("派发原位操作物料不属于同一设备")
    return tuple(result)


def _merge_param_patch(
    current: Mapping[str, Any],
    patch: Mapping[str, Any],
) -> dict[str, Any]:
    """递归合并用户对暂停 Job 的参数修改，数组和标量按新值替换。"""

    merged = deepcopy(dict(current))
    for key, value in patch.items():
        previous = merged.get(key)
        if isinstance(previous, Mapping) and isinstance(value, Mapping):
            merged[key] = _merge_param_patch(previous, value)
        else:
            merged[key] = deepcopy(value)
    return merged


def _retained_interval_ids_for_result(
    task: Mapping[str, Any],
    job: Mapping[str, Any],
    completed_nodes: Sequence[str | Mapping[str, Any]] = (),
) -> tuple[str, ...]:
    """返回正常连续持有或异常闩锁后仍须保留的资源区间。"""

    control_data = job.get("control_data")
    if not isinstance(control_data, Mapping):
        return ()
    raw_ids = control_data.get("resource_interval_ids")
    if not isinstance(raw_ids, (list, tuple, set, frozenset)):
        return ()
    interval_ids = {str(value).strip() for value in raw_ids if str(value).strip()}
    if not interval_ids:
        return ()
    execution_plan = task.get("execution_plan")
    if not isinstance(execution_plan, Mapping):
        return ()
    from unilabos.workflow.resource_lock_plan import retained_resource_interval_ids

    jobs = completed_nodes if completed_nodes and isinstance(completed_nodes[0], Mapping) else ()
    if jobs:
        return retained_resource_interval_ids(
            execution_plan,
            interval_ids,
            job,
            jobs,
        )
    from unilabos.workflow.resource_lock_plan import continuing_resource_interval_ids

    return continuing_resource_interval_ids(
        execution_plan,
        interval_ids,
        str(job.get("workflow_node_uuid") or ""),
        completed_nodes,
        current_completed=job.get("status") == "succeeded",
    )


class TaskSchedulerBridgeError(RuntimeError):
    """工作流任务不能安全进入本地调度器时使用的稳定桥接错误。"""

    def __init__(self, message: str, *, reason_code: str | None = None) -> None:
        """保留可展示的业务说明，以及库存权威已有的可选具体错误码。

        参数：``message`` 是原有桥接业务说明；``reason_code`` 仅在下层已有
        结构化错误码时提供。返回无；不访问或修改任务、库存和占用状态。
        """

        super().__init__(message)
        self.reason_code = reason_code


class TaskSchedulerBridge:
    """隐藏任务编译、短期物料门禁、生命周期投影和监听器清理。"""

    def __init__(
        self,
        store: WorkflowStore,
        *,
        scheduler: EdgeScheduler,
        compiler: WorkflowSpecCompiler | None = None,
        projection: TaskRuntimeProjection | None = None,
        cancel_ack_timeout_seconds: float = _DEFAULT_CANCEL_ACK_TIMEOUT_SECONDS,
        cancel_complete_timeout_seconds: float = (
            _DEFAULT_CANCEL_COMPLETE_TIMEOUT_SECONDS
        ),
        clock: Callable[[], datetime] | None = None,
        timer_factory: Callable[..., Any] = threading.Timer,
        time_scheduling_enabled: bool | None = None,
    ) -> None:
        """装配唯一工作流任务调度桥（TaskSchedulerBridge）。

        参数：``store`` 是标准任务/作业写权威；``scheduler`` 是既有本地调度器；
        ``compiler`` 把冻结执行计划（ExecutionPlan）转换为遗留调度规格；
        ``projection`` 把调度生命周期写回标准事实；两个取消超时分别约束执行器
        受理与设备终态等待。返回无；初始化会只读恢复此前受阻的准入任务。异常：
        读取恢复事实或超时参数转换失败时原样传播；库存服务只能从
        ``scheduler.inventory_service`` 读取，不能另行注入。
        ``clock`` 与 ``timer_factory`` 是时间边界；生产默认使用 UTC 系统时间和
        ``threading.Timer``，隔离测试可注入手动推进实现而不真实等待。
        """

        # ``_store`` 是本桥唯一的工作流任务（WorkflowTask）持久事实来源。
        self._store = store
        # ``_scheduler`` 同时持有唯一允许复用的本地库存权威（Inventory Authority）。
        self._scheduler = scheduler
        # 由生产装配显式开启，直接构造仍使用既有兼容语义；不读取测试环境。
        self._time_scheduling_enabled = (
            bool(getattr(scheduler, "time_scheduling_enabled", False))
            if time_scheduling_enabled is None else time_scheduling_enabled
        )
        self.error_control = StationExecutionControl(store, lock=scheduler.execution_control_lock)
        self.error_control.time_scheduling_enabled = self._time_scheduling_enabled
        scheduler.bind_station_control(self.error_control)
        self._compiler = compiler or WorkflowSpecCompiler()
        self._projection = projection or TaskRuntimeProjection(store)
        if self._time_scheduling_enabled:
            from unilabos.app.scheduler.timing_store import TimingObservationStore
            with store.transaction() as connection:
                TimingObservationStore(connection).ensure_schema()
            with store.read() as connection:
                TimingObservationStore(connection).replay_into(scheduler._estimator)
        self._max_in_flight_jobs = int(getattr(scheduler, "max_in_flight_jobs", 100))
        self._max_active_tasks = int(getattr(scheduler, "max_active_tasks", 500))
        self._max_tasks_per_workflow = int(
            getattr(scheduler, "max_tasks_per_workflow", 100)
        )
        self._cancel_ack_timeout_seconds = max(
            0.01,
            float(cancel_ack_timeout_seconds),
        )
        self._cancel_complete_timeout_seconds = max(
            self._cancel_ack_timeout_seconds,
            float(cancel_complete_timeout_seconds),
        )
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._timer_factory = timer_factory
        self._attention_notifier: Callable[[], None] | None = None
        from unilabos.workflow.human_commands import HumanInteractionStore
        self.human_interactions = HumanInteractionStore(store)
        self._human_timers: dict[str, Any] = {}
        self._cancel_timers: dict[str, Any] = {}
        self._cancel_timer_lock = threading.RLock()
        self._manual_confirmations = ManualConfirmationStore(store)
        self._manual_deadline_timer: Any | None = None
        self._manual_timer_lock = threading.RLock()
        # 关闭时要等待已进入的超时收敛回调完成，避免回调在
        # 外部关闭 SQLite 后继续投影终态。
        self._manual_callback_lock = threading.RLock()
        # ``_material_sources`` 复用调度器持有的同一库存权威，先于任何普通派发
        # 协调整个任务的物料来源解析作业（MaterialSourceResolutionJob）。
        self._material_sources = MaterialSourceResolutionCoordinator(
            inventory=scheduler.inventory_service,
            projection=self._projection,
        )
        inventory_store = getattr(scheduler.inventory_service, "store", None)
        self._quantity_inventory = (
            WorkflowQuantityInventory(store, scheduler.inventory_service)
            if inventory_store is not None
            else None
        )
        # 只串行人工更正入口；普通结果回调不取得此锁，避免与 Scheduler 锁倒序。
        self._quantity_reconciliation_lock = threading.RLock()
        self._material_transfer_settlement = MaterialTransferSettlement(
            scheduler.station_resource_inventory
        )
        self._material_aliquot_settlement = MaterialAliquotSettlement(
            scheduler.station_resource_inventory
        )
        # ``_task_by_job`` 只过滤本桥提交到共享调度器的作业，不承担持久恢复。
        self._task_by_job: dict[str, str] = {}
        # ``_submitted_tasks`` 标识仍可进行准入重试（AdmissionRetry）的本地运行。
        self._submitted_tasks: set[str] = set()
        # ``_submission_phases`` 只覆盖一次同步 submit 的短生命周期；回调可能在
        # EdgeScheduler 持有其内部锁时发生，使用独立 RLock 避免并发提交/失败收敛
        # 读取到半更新阶段。
        self._submission_phase_lock = threading.RLock()
        self._submission_phases: dict[str, str] = {}
        # 进入本地控制或派发准入后失败时，Edge 运行要保留 canceled 快照供诊断，
        # 但持久 Task/Job 仍是 pending。该集合是唯一允许下次 submit 丢弃的
        # scheduler 占位来源，不能按“同 UUID + canceled”猜测并删除其他运行。
        self._retryable_scheduler_runs: set[str] = set()
        # ``_admission_pending_tasks`` 从持久准入事实恢复；内存集合仅作本轮调度索引，
        # 进程重启不会丢失仍需重试的任务身份。
        self._admission_pending_tasks: set[str] = set(
            TaskRuntimeProjection(store).list_blocked_material_tasks()
        )
        # Workflow 终态与 Edge/Inventory 清理跨越多个权威，不能依赖下一次
        # restart 通知仍携带相同 Job UUID。首次清理失败后在本进程内保留 Task
        # 身份；持久跨进程补偿仍由 ``_recover_terminal_inventory_cleanup`` 承担。
        self._runtime_restart_cleanup_pending_tasks: set[str] = set()
        self._closed = False
        scheduler.add_admission_retry_listener(self._retry_pending_admissions)
        scheduler.bind_dispatch_admission_authority(self._on_job_pre_dispatch)
        scheduler.bind_manual_continuation_authority(
            self._on_manual_continuation_dispatching
        )
        scheduler.add_job_execution_wait_listener(self._on_job_execution_wait)
        scheduler.add_job_dispatch_accepted_listener(self._on_job_dispatch_accepted)
        scheduler.add_job_dispatch_uncertain_listener(self._on_job_dispatch_uncertain)
        scheduler.add_job_cancel_accepted_listener(self._on_job_cancel_accepted)
        scheduler.add_job_cancel_uncertain_listener(self._on_job_cancel_uncertain)
        scheduler.add_job_cancel_no_send_listener(self._on_job_cancel_no_send)
        scheduler.add_job_feedback_listener(self._on_job_feedback)
        scheduler.add_job_outcome_listener(self._on_job_outcome)
        scheduler.add_job_finished_listener(self._on_job_finished)
        scheduler.add_job_settled_listener(self._on_job_settled)
        scheduler.add_local_control_listener(self._on_local_control_evaluated)
        scheduler.add_execution_process_restarted_listener(
            self._on_execution_process_restarted
        )
        scheduler.add_execution_connection_lost_listener(
            self._on_execution_connection_lost
        )
        scheduler.set_drain_blocker_provider(self.active_or_uncertain_job_ids)
        scheduler.set_workflow_inventory_release_guard(self._prepare_inventory_release)
        release_guard = getattr(scheduler.inventory_service, "set_workflow_release_guard", None)
        if release_guard is not None:
            release_guard(self._prepare_inventory_release)

    def set_attention_notifier(self, notifier: Callable[[], None] | None) -> None:
        """为事项变化绑定已有的轻量 WSS 快照通知。"""

        self._attention_notifier = notifier
        self.error_control.set_attention_notifier(notifier)

    def preflight_transport_node(self, node: Mapping[str, Any]) -> Mapping[str, Any]:
        """车辆候选复用正式派发的实时注册、健康和未知命令校验。"""
        return self._scheduler.preflight_device_target(node)

    def _notify_attention_changed(self) -> None:
        if self._attention_notifier is not None:
            try:
                self._attention_notifier()
            except Exception:
                # 通知失败不能把已提交事实伪装成业务失败。
                logger.exception("中心事项快照唤醒失败，保留已提交事实")

    def prepare_inventory_allocations(
        self,
        connection: Any,
        *,
        graph: Mapping[str, Any],
        prepared: Any,
        task_uuid: str,
        bindings: list[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        """在 Task 首次写入前准备数量型库存分配。

        参数：``connection`` 是工作流创建事务；``graph/prepared`` 是同一冻结图
        和执行计划；``task_uuid`` 是运行身份；``bindings`` 是 HTTP 显式绑定。
        返回：既有 ``workflow_inventory_allocation`` 表的待插入行。异常：本次执行
        有逻辑库存需求但未装配库存权威时关闭式失败；具体校验错误原样传播。
        """

        active_requirements = [
            requirement
            for requirement in prepared.workflow_snapshot.get(
                "inventory_requirements",
                [],
            )
            if isinstance(requirement, Mapping)
            and str(requirement.get("consume_node_uuid"))
            in prepared.planned_node_uuids
        ]
        if self._quantity_inventory is None:
            if active_requirements or bindings:
                raise StoreConflict("工作流数量型库存未装配本地库存权威")
            return []
        return self._quantity_inventory.prepare_task_allocations(
            connection,
            graph=graph,
            prepared=prepared,
            task_uuid=task_uuid,
            bindings=bindings,
            defer_inventory_reservation=True,
        )

    def preflight_material_sources(self, prepared: Any, *, task_uuid: str = "") -> list[dict[str, Any]]:
        """来源预检使用与正式准入同一库存权威，只读解析已知候选。"""
        from unilabos.app.scheduler.inventory.source_preflight import material_source_checks

        inventory = self._scheduler.inventory_service
        if inventory is None:
            from unilabos.workflow.run_preflight import unavailable_material_source_checks

            return unavailable_material_source_checks(prepared.execution_plan)
        return material_source_checks(inventory, prepared.execution_plan, task_uuid=task_uuid)

    def preflight_batch_material_sources(self, plans: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """整批来源在同一只读库存锁内试分配，保留单项报告的独立语义。"""
        from unilabos.app.scheduler.inventory.source_preflight import batch_material_source_checks

        inventory = self._scheduler.inventory_service
        return batch_material_source_checks(inventory, plans) if inventory is not None else []

    def preflight_material_outputs(
        self, graph: Mapping[str, Any], *, prepared: Any = None,
        run_mode: str = "normal", target_node_uuid: str | None = None,
    ) -> list[dict[str, Any]]:
        """中心候选输出复用同一库存权威的只读检查，不取得来源或交接占用。"""
        from unilabos.workflow.material_output_preflight import material_output_checks

        return material_output_checks(graph, prepared=prepared,
            inventory_store=getattr(self._scheduler.inventory_service, "store", None),
            run_mode=run_mode, target_node_uuid=target_node_uuid)

    def preflight_inventory_allocations(
        self, *, graph: Mapping[str, Any], prepared: Any,
        bindings: list[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        """规范候选绑定并检查单份库存余量，保持既有单份预检语义。"""

        allocations = self.prepare_preflight_inventory_allocations(
            graph=graph, prepared=prepared, bindings=bindings,
        )
        self.check_preflight_inventory_allocations(allocations)
        return allocations

    def prepare_preflight_inventory_allocations(
        self,
        *,
        graph: Mapping[str, Any],
        prepared: Any,
        bindings: list[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        """只读规范候选的数量库存绑定，供单份和整批余量检查复用。

        参数：图、冻结候选输入和显式库存绑定与正式提交相同。返回：已验证分配
        预览。异常：存在活动需求但本地库存权威未装配，绑定不合法时失败关闭。
        本方法不创建任务、预留、台账或 Outbox。
        """

        active_requirements = [
            requirement
            for requirement in prepared.workflow_snapshot.get(
                "inventory_requirements",
                [],
            )
            if isinstance(requirement, Mapping)
            and str(requirement.get("consume_node_uuid"))
            in prepared.planned_node_uuids
        ]
        if self._quantity_inventory is None:
            if active_requirements or bindings:
                raise StoreConflict("工作流数量型库存未装配本地库存权威")
            return []
        return self._quantity_inventory.prepare_preflight_allocations(
            graph=graph,
            prepared=prepared,
            bindings=bindings,
        )

    def check_preflight_inventory_allocations(
        self, allocations: list[Mapping[str, Any]],
    ) -> None:
        """复用同一库存权威检查整批候选总量，不创建执行任务或资源占用。"""

        if self._quantity_inventory is None:
            if allocations:
                raise StoreConflict("工作流数量型库存未装配本地库存权威")
            return
        self._quantity_inventory.check_preflight_allocations(allocations)

    def discard_uncommitted_inventory(self, task_uuid: str) -> None:
        """补偿未提交 Task 在库存权威中留下的数量预留。

        参数：``task_uuid`` 是创建事务最终回滚的任务身份。返回无。异常：库存补偿
        失败原样传播并由 API 报告内部错误；未装配数量库存或重复调用均安全无写入。
        """

        if self._quantity_inventory is None:
            return
        self._quantity_inventory.discard_uncommitted_task(task_uuid)

    def list_task_inventory_consumptions(self, task_uuid: str) -> list[dict[str, Any]]:
        """返回任务的数量型库存消费事实；未装配库存时返回空数组。"""

        if self._quantity_inventory is None:
            return []
        return self._quantity_inventory.list_task_consumptions(task_uuid)

    def list_task_material_events(
        self, task_uuid: str, job_uuids: set[str], *, since_ms: int = 0
    ) -> list[dict[str, Any]]:
        """返回任务的物料变化台账；未装配库存时返回空数组。"""

        if self._quantity_inventory is None:
            return []
        return self._quantity_inventory.list_task_material_events(
            task_uuid, job_uuids, since_ms=since_ms
        )

    def list_job_inventory_consumptions(self, job_uuid: str) -> list[dict[str, Any]]:
        """返回作业的数量型库存消费事实；未装配库存时返回空数组。"""

        if self._quantity_inventory is None:
            return []
        return self._quantity_inventory.list_job_consumptions(job_uuid)

    def list_reagent_inventory_consumptions(
        self, reagent_uuid: str
    ) -> list[dict[str, Any]]:
        """返回试剂实例的数量型库存消费谱系；未装配库存时返回空数组。"""

        if self._quantity_inventory is None:
            return []
        return self._quantity_inventory.list_reagent_consumptions(reagent_uuid)

    def submit(self, task: Mapping[str, Any]) -> dict[str, Any]:
        """提交已经持久化的工作流任务（WorkflowTask）。

        参数：``task`` 是创建事务返回的标准任务投影。返回：调度同步推进后的标准
        任务/作业聚合。异常：桥关闭、任务身份非法、冻结计划编译失败、缺少库存权威
        或派发前投影冲突时失败关闭；失败不会创建新的任务/作业身份。
        """
        serial = getattr(self._scheduler, "serial_queue_enabled", False)
        with ExitStack() as admission:
            if serial:
                admission.enter_context(self._scheduler.execution_control_lock)
            return self._submit(task, release_admission=admission.close if serial else None)

    def _submit(
        self, task: Mapping[str, Any], *, release_admission: Callable[[], None] | None
    ) -> dict[str, Any]:
        if self._closed:
            raise TaskSchedulerBridgeError("工作流任务调度桥已经关闭")
        # ``task_uuid`` 是本地调度运行、遗留预留和标准任务共用的稳定身份。
        task_uuid = self._required_text(task.get("uuid"), field="task.uuid")
        if self._store.execution_held(task_uuid):
            raise StoreConflict("原任务处于灾备隔离，禁止重新提交调度")
        if self._store.center_cancellation_requested(task_uuid):
            self._finish_serial_admission(task_uuid)
            return self._aggregate(task_uuid)
        # 上一次在本桥回调中失败时保留了一个 canceled 的 Edge 快照；只有本桥
        # 自己登记过的占位才允许在重试前丢弃，避免误删同 UUID 的其他调度运行。
        self._discard_retryable_scheduler_run(task_uuid)
        if task_uuid in self._submitted_tasks:
            return self._aggregate(task_uuid)
        if task_uuid in self._admission_pending_tasks:
            return self._aggregate(task_uuid)
        jobs: list[dict[str, Any]] = []
        admission_attempted = False
        registered = False
        try:
            persisted_task = self._store.get_task(task_uuid)
            # ``jobs`` 是创建事务已经确定的工作流节点作业（WorkflowNodeJob）集合。
            jobs = self._store.list_jobs(task_uuid)
            plan = persisted_task.get("execution_plan") or {}
            pending_binding = plan.get("inventory_resource_binding") == "pending"
            if pending_binding:
                requires_inventory = self._compiler.validate_inventory_admission(persisted_task, jobs)
            else:
                requires_inventory = bool(self._compiler.compile(persisted_task, jobs).material_requirements_by_node())
            if requires_inventory and self._scheduler.inventory_service is None:
                raise TaskSchedulerBridgeError(
                    "工作流声明了物料需求，但本地调度器没有装配库存权威"
                )
            if release_admission is not None:
                if persisted_task["status"] != "pending":
                    return self._aggregate(task_uuid)
                if not self._scheduler.serial_admission_turn(
                    task_uuid, WorkflowSpecCompiler.submitted_at(persisted_task)
                ):
                    self._admission_pending_tasks.add(task_uuid)
                    return self._aggregate(task_uuid)
            # 物料来源解析必须先提交完整短期预留和逐来源结果；受阻时不注册普通
            # 动作，也不让任何作业越过物理派发边界。
            allocation_reader = getattr(
                self._quantity_inventory,
                "task_allocations",
                None,
            )
            quantity_allocations = (
                allocation_reader(task_uuid) if callable(allocation_reader) else ()
            )
            # 从进入 reconcile 起就必须按“可能已经提交预留”处理异常：库存事务
            # 返回后仍有结果校验和绑定投影，任一步失败都要幂等补偿。
            admission_attempted = True
            material_resolution = self._material_sources.reconcile(
                persisted_task,
                jobs,
                quantity_allocations=quantity_allocations,
            )
            if material_resolution.status == "blocked":
                self._admission_pending_tasks.add(task_uuid)
                blocker = material_resolution.blocking_workflow_id
                if blocker and release_admission is not None:
                    self._scheduler.advance_serial_admission(task_uuid, blocker)
                return self._aggregate(task_uuid)
            if pending_binding:
                child_resolution = self._material_sources.reconcile_required_children(
                    persisted_task, read_jobs=lambda: self._store.list_jobs(task_uuid),
                )
                if child_resolution.status == "blocked":
                    self._admission_pending_tasks.add(task_uuid)
                    return self._aggregate(task_uuid)
            self._admission_pending_tasks.discard(task_uuid)
            # 自动物料来源（MaterialSource）的准入结果已原子写入既有动作作业参数；
            # 重新读取同一作业身份后再编译，禁止派发准入前的空参数快照。
            jobs = self._store.list_jobs(task_uuid)
            if pending_binding:
                self._projection.bind_inventory_resource_plan(
                    task_uuid, self._scheduler.station_resource_inventory,
                )
                persisted_task = self._store.get_task(task_uuid)
            spec = self._compiler.compile(persisted_task, jobs)
            if not spec.nodes:
                # 仅来源任务没有普通作业可触发调度器终态清理；协调器必须在返回成功
                # 前幂等释放仍活跃的短期预留，不能让测试或调用方承担内部清理。
                self._material_sources.release_terminal_reservations(
                    task_uuid,
                    reason="workflow_succeeded",
                )
                self._finish_serial_admission(task_uuid)
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
            self._begin_submission_phase(task_uuid)
            execution_mode = str(
                persisted_task.get("execution_mode")
                or persisted_task.get("run_mode")
                or "normal"
            )
            dispatch_gate = str(
                persisted_task.get("dispatch_gate")
                or (
                    "paused"
                    if execution_mode in {"switching_to_step", "step"}
                    else "active"
                )
            )
            if execution_mode == "normal" and dispatch_gate == "active":
                # 保持既有调度器替身/扩展只接收 spec 的兼容性；默认控制态
                # 与旧 submit 合同完全一致。
                submission = self._scheduler.submit_workflow(spec)
            else:
                submission = self._scheduler.submit_workflow(
                    spec,
                    execution_mode=execution_mode,
                    dispatch_gate=dispatch_gate,
                )
            if release_admission is not None:
                # 入图以后队首身份已固定。在锁外等派发，保留提交失败的原有补偿。
                self._scheduler.wake_serial_queue(release_admission=release_admission)
                snapshot = self._scheduler.workflow_snapshot(task_uuid)
                if snapshot is not None:
                    submission["state"] = snapshot["state"]
            # 调度器返回即表示同步控制/派发回调全部成功；后续 Trace 或首次
            # 状态投影异常不应被误判为“回调内部失败”。
            self._finish_submission_phase(task_uuid)
            self._project_scheduler_trace_context(
                task_uuid,
                submission.get("trace_context"),
            )
            # ``scheduler_state`` 是内部等料或运行状态；投影层负责限制 wire 状态。
            scheduler_state = self._required_text(
                submission.get("state"), field="scheduler.state"
            )
            aggregate = self._projection.project_submission(task_uuid, scheduler_state)
            return aggregate
        except Exception as error:
            self._finish_serial_admission(task_uuid)
            submission_phase = self._finish_submission_phase(task_uuid)
            if self._crossed_dispatch_boundary(jobs):
                raise TaskSchedulerBridgeError(
                    "工作流任务派发结果不确定，已保留在途执行等待明确结果"
                ) from error
            if admission_attempted or registered:
                self._cancel_failed_submission(
                    task_uuid,
                    jobs,
                    retain_pending=registered
                    and submission_phase
                    in {
                        _SUBMISSION_PHASE_PRE_DISPATCH,
                        _SUBMISSION_PHASE_LOCAL_CONTROL,
                    },
                )
            if isinstance(error, TaskSchedulerBridgeError):
                raise
            raise TaskSchedulerBridgeError(
                "工作流任务无法安全提交到本地调度器"
            ) from error

    def _finish_serial_admission(self, task_uuid: str) -> None:
        if getattr(self._scheduler, "serial_queue_enabled", False):
            if self._scheduler.discard_serial_admission(task_uuid):
                self._admission_pending_tasks.discard(task_uuid)
                self._scheduler.wake_serial_queue()

    def retry_admission(self, task_uuid: str) -> dict[str, Any]:
        """对同一待处理任务触发准入重试（AdmissionRetry）。

        参数：``task_uuid`` 是此前已提交但因物料不足等待的稳定任务身份。返回：
        重排后的标准任务/作业聚合。异常：桥关闭、未知任务或调度重排失败时传播；
        本操作绝不创建新任务、作业或执行尝试身份。
        """

        if self._closed:
            raise TaskSchedulerBridgeError("工作流任务调度桥已经关闭")
        normalized_uuid = self._required_text(task_uuid, field="task_uuid")
        admission = TaskRuntimeProjection(self._store).get_material_admission(
            normalized_uuid
        )
        if normalized_uuid in self._admission_pending_tasks or (
            admission is not None and admission.get("status") == "blocked"
        ):
            # 显式准入重试复用同一持久任务/作业身份；先移除内存标记，让 ``submit``
            # 真正重做整图预留，若仍受阻会原样重新登记。
            self._admission_pending_tasks.discard(normalized_uuid)
            return self.submit(self._store.get_task(normalized_uuid))
        if normalized_uuid not in self._submitted_tasks:
            raise TaskSchedulerBridgeError("工作流任务尚未提交到本地调度器")
        self._scheduler.reschedule()
        return self._aggregate(normalized_uuid)

    def reschedule(self) -> None:
        """在人工释放持久执行锁后唤醒共享调度器。"""

        if self._closed:
            raise TaskSchedulerBridgeError("工作流任务调度桥已经关闭")
        self._scheduler.reschedule()

    def decide_error(self, decision_id: str, request: dict[str, Any], *, idempotency_key: str, operator_id: str) -> dict[str, Any]:
        """应用已保存的人工决定；任一步失败保留原命令供同身份补偿。"""
        accepted = self.error_control.decide(decision_id, request, idempotency_key=idempotency_key, operator_id=operator_id)
        return self.apply_error_command(accepted)

    def apply_error_command(self, accepted: dict[str, Any]) -> dict[str, Any]:
        from unilabos.app.scheduler.inventory.domain import InventoryError
        try:
            return self._apply_error_command(accepted)
        except (InventoryError, StationResourceError) as error:
            if accepted["result"].get("action") not in {"inventory_correction", "resource_disposition"}:
                return {**self.error_control.get_command(accepted["command_uuid"]), "application_error": str(error)}
            return self.error_control.reject_command(accepted["command_uuid"], str(error))
        except Exception as error:
            # 控制意图已落盘；应用暂时失败不能伪装成请求未受理。
            logger.exception("已受理错误处置命令待恢复：%s", accepted["command_uuid"])
            return {**self.error_control.get_command(accepted["command_uuid"]), "application_error": str(error)}

    def _apply_error_command(self, accepted: dict[str, Any]) -> dict[str, Any]:
        if accepted["status"] != "PENDING":
            return accepted
        result = accepted["result"]
        task_uuid = result["source_task_uuid"]
        with self.error_control.lock:
            current = self.error_control.get_command(accepted["command_uuid"])
            if current["status"] != "PENDING":
                return current
            self.error_control.validate_pending_decision(accepted["command_uuid"])
            current = self.error_control.get_command(accepted["command_uuid"])
            if not current["result"].get("application_effects_applied"):
                if result["action"] == "retry_current_node":
                    occupancies = result.get("retry_occupancies", [])
                    if result.get("retry_previous_uncertainty"):
                        inventory = self._scheduler.station_resource_inventory
                        if inventory is not None:
                            # 重试只释放来源 Claim；共享 Fence 下仍运行的其他 Job 保持占用。
                            for claim_uuid in sorted({item["claim_uuid"] for item in occupancies}):
                                inventory.transition_dispatch_permit(claim_uuid, target_state="released")
                        self.error_control.apply_resource_facts({
                            **accepted, "result": {**result, "occupancies": occupancies},
                        })
                        self.error_control.record_operator_retry(accepted)
                        self._scheduler.release_error_occupancies(occupancies, include_shared=False)
                    job = self._store.get_job(result["retry_job_uuid"])
                    self._task_by_job[job["uuid"]] = task_uuid
                    node_id = job["control_data"].get("runtime_node_id") or job["workflow_node_uuid"]
                    self._scheduler.retry_node(task_uuid, node_id, job["uuid"])
                elif result["action"] == "inventory_correction":
                    inventory = self._scheduler.station_resource_inventory
                    if inventory is None:
                        return self.error_control.reject_command(accepted["command_uuid"], "库存权威未装配")
                    inventory.correct_inventory(accepted["command_uuid"], result)
                elif result["action"] == "resource_disposition":
                    inventory = self._scheduler.station_resource_inventory
                    if inventory is not None:
                        inventory.release_error_occupancies(result["occupancies"])
                    self.error_control.apply_resource_facts(accepted)
                    self._scheduler.release_error_occupancies(result["occupancies"])
                elif result["action"] == "stop_manual_action":
                    self.stop_execution(result["execution_job_uuid"], command_uuid=accepted["command_uuid"], reason=result["reason"])
                else:
                    self.cancel(task_uuid, command_uuid=accepted["command_uuid"], reason="error_decision")
                self.error_control.mark_decision_effects_applied(accepted["command_uuid"])
            applied = self.error_control.mark_applied(accepted["command_uuid"])
        self._scheduler.reschedule()
        return applied

    def error_inventory(self) -> dict[str, Any]:
        inventory = self._scheduler.station_resource_inventory
        if inventory is None:
            raise StoreConflict("库存权威未装配")
        return inventory.error_handling_inventory()

    def apply_auxiliary(self, operation: str, request: dict[str, Any], *, idempotency_key: str, operator_id: str) -> dict[str, Any]:
        return self.apply_error_command(self.error_control.accept_auxiliary(operation, request,
            idempotency_key=idempotency_key, operator_id=operator_id))

    def settle_canceled_before_start(self, job_uuid: str) -> None:
        job = self._store.get_job(job_uuid)
        if job["status"] == "canceled" and job["start_state"] == "NOT_STARTED":
            self._scheduler.settle_canceled_before_start(job_uuid)

    def stop_execution(self, job_uuid: str, *, command_uuid: str, reason: str) -> None:
        """只停止指定执行 attempt，其他历史未知或新 attempt 不级联停止。"""
        job = self._store.get_job(job_uuid)
        if job["status"] in {"succeeded", "failed", "timeout", "canceled"}:
            return
        now = self._clock()
        self._projection.project_cancel_requested(job["workflow_task_uuid"],
            command_uuid=command_uuid, reason=reason, execution_job_uuid=job_uuid,
            ack_deadline_at=self._format_time(now + timedelta(seconds=self._cancel_ack_timeout_seconds)),
            complete_deadline_at=self._format_time(now + timedelta(seconds=self._cancel_complete_timeout_seconds)))
        self._scheduler.cancel_workflow(job["workflow_task_uuid"], execution_job_uuid=job_uuid,
            cancel_current=job.get("current_attempt", True))
        updated = self._store.get_job(job_uuid)
        if updated["status"] == "cancel_requested":
            self._schedule_cancel_timeout(updated)

    def unlock_resources(
        self,
        task_uuid: str,
        *,
        command_uuid: str,
        reason: str,
    ) -> dict[str, Any]:
        """在操作员已确认现场安全后整组释放异常终态 Task 资源。

        参数：Task/Command 身份和现场处置说明均由应用服务验证。
        返回各类释放计数与最终清理状态。异常：任务非异常终态，或仍有
        已派发/运行中的 Job、
        库存权威不支持整组释放或持久事实冲突时抛出稳定桥接错误。

        跨库无法使用单一 SQLite 事务；因此严格按库存 Permit → 任务级
        预留 → 工作流持久锁 → Scheduler 内存的单向、幂等顺序释放。
        中途失败只会留下过度约束，不会让内存先于持久门禁放行。
        """

        if self._closed:
            raise TaskSchedulerBridgeError("工作流任务调度桥已经关闭")
        normalized_uuid = self._required_text(task_uuid, field="task_uuid")
        normalized_command_uuid = self._required_text(
            command_uuid,
            field="command_uuid",
        )
        normalized_reason = self._required_text(reason, field="reason")
        task = self._store.get_task(normalized_uuid)
        jobs = self._store.list_jobs(normalized_uuid)
        if task.get("status") not in {"failed", "canceled", "timeout"}:
            raise TaskSchedulerBridgeError("任务尚未进入异常终态")
        if self._pending_quantity_consumption(normalized_uuid):
            raise TaskSchedulerBridgeError("任务仍有数量待对账，不能人工释放全部资源")
        if any(job.get("status") in {"dispatched", "running"} for job in jobs):
            raise TaskSchedulerBridgeError("任务仍有已派发或运行中的作业")

        try:
            station_inventory = self._scheduler.station_resource_inventory
            if (
                station_inventory is None
                and self._scheduler.physical_dispatch_enabled
            ):
                raise TaskSchedulerBridgeError(
                    "物理调度未装配库存权威，拒绝人工释放"
                )
            if station_inventory is not None:
                release_permits = getattr(
                    station_inventory,
                    "release_task_dispatch_permits",
                    None,
                )
                if not callable(release_permits):
                    raise TaskSchedulerBridgeError(
                        "库存权威不支持终态任务整组释放"
                    )
                release_permits(task_uuid=normalized_uuid)
            if self._quantity_inventory is not None:
                self._quantity_inventory.release_task(
                    normalized_uuid,
                    reason="operator_resource_unlock",
                )
            if any(job.get("executor_kind") == "material_source" for job in jobs):
                self._material_sources.release_terminal_reservations(
                    normalized_uuid,
                    reason="operator_resource_unlock",
                )
            result = self._projection.release_operator_confirmed_task_resources(
                normalized_uuid,
                command_uuid=normalized_command_uuid,
                reason=normalized_reason,
            )
            self._scheduler.release_terminal_workflow_resources(normalized_uuid)
        except TaskSchedulerBridgeError:
            raise
        except (StoreConflict, StoreNotFound, ValueError) as error:
            raise TaskSchedulerBridgeError(str(error)) from error

        for job in jobs:
            job_uuid = str(job.get("uuid") or "")
            self._task_by_job.pop(job_uuid, None)
            self._cancel_cancel_timer(job_uuid)
            self._cancel_manual_confirmation_timer(job_uuid)
        self._submitted_tasks.discard(normalized_uuid)
        self._admission_pending_tasks.discard(normalized_uuid)
        with self._submission_phase_lock:
            self._retryable_scheduler_runs.discard(normalized_uuid)
        try:
            self._scheduler.reschedule()
        except Exception:
            # 全部资源释放事实已单向提交；下次调度轮会重新发现等待者。
            logger.warning("人工释放任务资源后调度器唤醒失败", exc_info=True)
        return result

    def step(
        self,
        task_uuid: str,
        *,
        target_node_uuid: str | None = None,
    ) -> dict[str, Any]:
        """让已经暂停的任务只派发一个就绪节点。

        当前若是自动运行，先切回单步再派发。在途 Job 尚未结束时不改变模式，
        调用方可以在它结束后再次单步。
        """

        if self._closed:
            raise TaskSchedulerBridgeError("工作流任务调度桥已经关闭")
        normalized_uuid = self._required_text(task_uuid, field="task_uuid")
        # ``_submitted_tasks`` 只是桥接层的监听路由账本，不是运行权威。工作区
        # 重组或监听器世代切换后它可能暂时缺少仍由同一个 EdgeScheduler 持有的
        # 运行；单步准入必须由调度器自身验证任务存在、单步模式和暂停状态。
        try:
            task = self._store.get_task(normalized_uuid)
            execution_mode = str(
                task.get("execution_mode") or task.get("run_mode") or "normal"
            )
            if execution_mode == "normal":
                if (
                    task.get("control_status") != "paused"
                    or str(task.get("dispatch_gate") or "active") != "paused"
                ):
                    raise ValueError("task_is_not_paused_step")
                state = self._scheduler.step_state(normalized_uuid)
                if int(state.get("in_flight_job_count") or 0):
                    raise ValueError(
                        f"workflow {normalized_uuid} step is still in progress"
                    )
                if state.get("execution_mode") != "normal":
                    raise ValueError(
                        f"workflow {normalized_uuid} is not in normal mode"
                    )
                self.switch_to_step(normalized_uuid)
            return self._scheduler.step_workflow(
                normalized_uuid,
                target_node_id=target_node_uuid,
            )
        except ValueError as error:
            raise TaskSchedulerBridgeError(str(error)) from error

    def step_state(self, task_uuid: str) -> dict[str, Any]:
        """返回标准 Step UI 使用的后端权威候选集合。"""

        if self._closed:
            raise TaskSchedulerBridgeError("工作流任务调度桥已经关闭")
        normalized_uuid = self._required_text(task_uuid, field="task_uuid")
        try:
            return self._scheduler.step_state(normalized_uuid)
        except ValueError as error:
            raise TaskSchedulerBridgeError(str(error)) from error

    def cancel(
        self,
        task_uuid: str,
        *,
        command_uuid: str | None = None,
        reason: str = "task_canceled",
    ) -> dict[str, Any]:
        """持久化取消请求并请求本地执行器安全停止设备作业。

        参数：``task_uuid`` 是已创建任务身份；``command_uuid`` 是公开幂等控制命令
        身份，直接调用时自动生成。返回取消受理后的任务/作业聚合。异常：桥关闭、
        任务已有在途作业但缺少调度器快照，或投影冲突时抛稳定错误。

        未发送作业同步取消；设备在途作业保持 ``cancel_requested`` 和执行锁，等待
        执行器受理及明确终态。取消请求成功不等于设备已经安全停止。
        """
        if getattr(self._scheduler, "serial_queue_enabled", False):
            with self._scheduler.execution_control_lock:
                return self._cancel(task_uuid, command_uuid=command_uuid, reason=reason)
        return self._cancel(task_uuid, command_uuid=command_uuid, reason=reason)

    def _cancel(
        self, task_uuid: str, *, command_uuid: str | None, reason: str
    ) -> dict[str, Any]:
        if self._closed:
            raise TaskSchedulerBridgeError("工作流任务调度桥已经关闭")
        normalized_uuid = self._required_text(task_uuid, field="task_uuid")
        normalized_command_uuid = self._required_text(
            command_uuid or str(uuid4()),
            field="command_uuid",
        )
        now = self._clock()
        ack_deadline = now + timedelta(seconds=self._cancel_ack_timeout_seconds)
        complete_deadline = now + timedelta(
            seconds=self._cancel_complete_timeout_seconds
        )
        scheduler_snapshot = self._scheduler.workflow_snapshot(normalized_uuid)
        admission = self._projection.get_material_admission(normalized_uuid)
        admission_pending = normalized_uuid in self._admission_pending_tasks or (
            admission is not None and admission.get("status") == "blocked"
        )
        if scheduler_snapshot is None and not admission_pending:
            # 已持久取消的 Task 可以在进程重启后幂等重放同一取消命令；此时
            # 内存 DAG 已不存在，但仍须继续走投影与库存释放，最终让工站控制
            # 命令从 PENDING 收口为 APPLIED。领取后恢复但尚未派发的 pending
            # Task 也可直接取消；存在在途作业时不得假定设备已安全停止。
            try:
                persisted_status = str(
                    self._store.get_task(normalized_uuid).get("status") or ""
                )
                undispatched = persisted_status == "pending" and all(
                    job["status"] in {"pending", "succeeded", "skipped", "canceled"}
                    for job in self._store.list_jobs(normalized_uuid)
                )
            except StoreNotFound:
                persisted_status = ""
                undispatched = False
            if persisted_status != "canceled" and not undispatched:
                raise TaskSchedulerBridgeError("工作流任务尚未提交到本地调度器")
        self._projection.project_cancel_requested(
            normalized_uuid,
            command_uuid=normalized_command_uuid,
            ack_deadline_at=self._format_time(ack_deadline),
            complete_deadline_at=self._format_time(complete_deadline),
            reason=reason,
        )
        # 取消投影已提交，未发送人工确认的关闭不会再经过设备终态回调。
        self._notify_attention_changed()
        # 外部 Task Cancel 可能关闭当前最早的人工确认；立即重排唯一计时器，
        # 不让已关闭的 deadline 长时间占据唤醒槽。
        self._schedule_manual_confirmation_timeout()
        if scheduler_snapshot is not None and not self._scheduler.cancel_workflow(
            normalized_uuid
        ):
            raise TaskSchedulerBridgeError("工作流任务尚未提交到本地调度器")
        self._store.stop_debug(normalized_uuid)
        self._admission_pending_tasks.discard(normalized_uuid)
        aggregate = self._aggregate(normalized_uuid)
        for job in aggregate["jobs"]:
            if job.get("status") == "cancel_requested":
                self._schedule_cancel_timeout(job)
        if aggregate["task"]["status"] == "canceled":
            aggregate = self._release_canceled_inventory(
                aggregate,
                reason="workflow_canceled",
            )
            self._submitted_tasks.discard(normalized_uuid)
            self._finish_serial_admission(normalized_uuid)
        return aggregate

    def pause(self, task_uuid: str) -> dict[str, Any]:
        """暂停普通 Task 的后续派发，不改变执行模式或中断在途 Job。"""

        if self._closed:
            raise TaskSchedulerBridgeError("工作流任务调度桥已经关闭")
        normalized_uuid = self._required_text(task_uuid, field="task_uuid")
        try:
            current = self._store.get_task(normalized_uuid)
            execution_mode = str(
                current.get("execution_mode") or current.get("run_mode") or "normal"
            )
            if execution_mode != "normal":
                raise ValueError("任务不在普通执行模式")
            # 先落持久闸门，再切内存闸门；两者之间即使进程退出，恢复也不会
            # 把已经暂停的普通 Task 重新派发。
            task = self._store.set_task_dispatch_gate(
                normalized_uuid,
                dispatch_gate="paused",
            )
            if normalized_uuid in self._admission_pending_tasks:
                return {"task": task, "scheduler": None}
            if self._scheduler.workflow_snapshot(normalized_uuid) is None:
                if task.get("status") == "pending":
                    return {"task": task, "scheduler": None}
                raise ValueError("工作流任务尚未提交到本地调度器")
            result = self._scheduler.pause_workflow(normalized_uuid)
            task = self._store.get_task(normalized_uuid)
            return {"task": task, "scheduler": result}
        except (StoreConflict, ValueError) as error:
            raise TaskSchedulerBridgeError(str(error)) from error

    def update_pending_job_param(
        self,
        task_uuid: str,
        job_uuid: str,
        param: Mapping[str, Any],
    ) -> dict[str, Any]:
        """修改暂停任务中尚未派发 Job 的参数并同步内存调度运行态。

        参数：``task_uuid`` 与 ``job_uuid`` 是稳定任务/作业身份；``param`` 是
        顶层 JSON 对象补丁，未传字段保留原值。返回更新后的 Task/Job 投影。异常：
        任务未暂停、作业已离开 pending、身份不属于同一任务或参数不合法时抛
        ``TaskSchedulerBridgeError``；在途作业绝不会被改写。
        """

        if not isinstance(param, Mapping):
            raise TaskSchedulerBridgeError("作业参数必须是对象")
        if self._closed:
            raise TaskSchedulerBridgeError("工作流任务调度桥已经关闭")
        normalized_task_uuid = self._required_text(task_uuid, field="task_uuid")
        normalized_job_uuid = self._required_text(job_uuid, field="job_uuid")
        with self._scheduler.execution_control_lock:
            try:
                task = self._store.get_task(normalized_task_uuid)
                job = self._store.get_job(normalized_job_uuid)
                if str(job.get("workflow_task_uuid")) != normalized_task_uuid:
                    raise StoreConflict("作业不属于目标任务")
                if (
                    str(task.get("dispatch_gate") or "active") != "paused"
                    or str(task.get("execution_mode") or task.get("run_mode") or "normal") != "normal"
                    or str(task.get("control_status") or "active") != "paused"
                ):
                    raise StoreConflict("任务必须先暂停才能修改参数")
                if not bool(job.get("current_attempt", True)):
                    raise StoreConflict("历史作业 attempt 不能修改参数")
                if str(job.get("status")) != "pending":
                    raise StoreConflict("作业已经离开待运行状态，不能修改参数")
                merged_param = _merge_param_patch(job.get("param") or {}, param)
                updated_job = self._store.update_pending_job_param(
                    normalized_task_uuid,
                    normalized_job_uuid,
                    merged_param,
                )
                if self._scheduler.workflow_snapshot(normalized_task_uuid) is not None:
                    try:
                        self._scheduler.update_pending_job_param(
                            normalized_task_uuid,
                            str(job.get("workflow_node_uuid") or ""),
                            normalized_job_uuid,
                            merged_param,
                        )
                    except Exception:
                        # 数据库和内存运行态必须保持一致；暂停门禁仍阻止任何新
                        # 派发，因此可以安全地把持久参数恢复为旧值。
                        self._store.update_pending_job_param(
                            normalized_task_uuid,
                            normalized_job_uuid,
                            job.get("param") or {},
                        )
                        raise
                return {
                    "task": self._store.get_task(normalized_task_uuid),
                    "job": updated_job,
                }
            except (StoreConflict, StoreNotFound, ValueError) as error:
                raise TaskSchedulerBridgeError(str(error)) from error

    def resume(self, task_uuid: str) -> dict[str, Any]:
        """恢复普通 Task，或把稳定暂停的 step Task 切换为自动调度。"""

        if self._closed:
            raise TaskSchedulerBridgeError("工作流任务调度桥已经关闭")
        normalized_uuid = self._required_text(task_uuid, field="task_uuid")
        try:
            task = self._store.get_task(normalized_uuid)
            execution_mode = str(
                task.get("execution_mode") or task.get("run_mode") or "normal"
            )
            if execution_mode == "step":
                if (
                    task.get("control_status") != "paused"
                    or task.get("dispatch_gate") != "paused"
                ):
                    raise ValueError("单步任务当前不在可恢复的暂停状态")
                # 调试恢复必须先让 Scheduler 放行本次重排；防御性派发门禁会
                # 允许 step 模式的显式单步/自动继续派发。
                result = self._scheduler.continue_automatic(normalized_uuid)
                task = self._store.get_task(normalized_uuid)
                if task.get("status") not in {
                    "succeeded",
                    "failed",
                    "canceled",
                    "timeout",
                }:
                    task = self._store.set_task_execution_mode(
                        normalized_uuid,
                        execution_mode="normal",
                        control_status="active",
                        dispatch_gate="active",
                    )
                return {"task": task, "scheduler": result}
            if execution_mode != "normal":
                raise ValueError("任务不在可恢复的普通执行模式")
            # 先落持久闸门，确保 Scheduler 产生的首个新 Job 能通过同一门禁。
            task = self._store.set_task_dispatch_gate(
                normalized_uuid,
                dispatch_gate="active",
            )
            if self._scheduler.workflow_snapshot(normalized_uuid) is None:
                if normalized_uuid in self._admission_pending_tasks or task.get("status") == "pending":
                    self._admission_pending_tasks.discard(normalized_uuid)
                    return self.submit(task)
                raise ValueError("工作流任务尚未提交到本地调度器")
            result = self._scheduler.resume_workflow(normalized_uuid)
            return {"task": self._store.get_task(normalized_uuid), "scheduler": result}
        except (StoreConflict, ValueError) as error:
            raise TaskSchedulerBridgeError(str(error)) from error

    def switch_to_step(self, task_uuid: str) -> dict[str, Any]:
        """把普通 Task 安全切换为单步，排空当前在途 Job。"""

        if self._closed:
            raise TaskSchedulerBridgeError("工作流任务调度桥已经关闭")
        normalized_uuid = self._required_text(task_uuid, field="task_uuid")
        try:
            task = self._store.get_task(normalized_uuid)
            execution_mode = str(
                task.get("execution_mode") or task.get("run_mode") or "normal"
            )
            if execution_mode != "normal":
                raise ValueError("任务不在普通执行模式")
            previous_control_status = str(task.get("control_status") or "active")
            previous_dispatch_gate = str(task.get("dispatch_gate") or "active")
            # 先把持久事实标记为切换中。WorkflowStore 在同一写事务内检查
            # 其他活动任务，随后所有普通任务创建都会看到这个闸门；这样调度器
            # 排空在途 Job 的时间窗口不会重新允许第二个调试任务进入。
            self._store.set_task_execution_mode(
                normalized_uuid,
                execution_mode="switching_to_step",
                control_status="paused",
                dispatch_gate="paused",
            )
            try:
                result = self._scheduler.switch_to_step(normalized_uuid)
            except Exception:
                # 调度器未能接管时恢复原来的普通执行事实，避免留下永久调试闸门。
                try:
                    self._store.set_task_execution_mode(
                        normalized_uuid,
                        execution_mode="normal",
                        control_status=previous_control_status,
                        dispatch_gate=previous_dispatch_gate,
                    )
                except (StoreConflict, StoreNotFound):
                    logger.exception("单步切换失败后恢复任务执行模式失败")
                raise
            task = self._store.set_task_execution_mode(
                normalized_uuid,
                execution_mode=str(result["execution_mode"]),
                control_status="paused",
                dispatch_gate="paused",
            )
            return {"task": task, "scheduler": result}
        except (StoreConflict, ValueError) as error:
            raise TaskSchedulerBridgeError(str(error)) from error

    def decide_manual_confirmation(
        self,
        job_uuid: str,
        *,
        action: str,
    ) -> dict[str, Any]:
        """以 Job UUID 决定人工确认；批准继续同一 Job，拒绝取消 Task。"""

        if self._closed:
            raise TaskSchedulerBridgeError("工作流任务调度桥已经关闭")
        normalized_job_uuid = self._required_text(job_uuid, field="job_uuid")
        try:
            normalized_action = self._required_text(action, field="action").lower()
            aggregate, created = (
                self._projection.project_manual_confirmation_decision(
                    normalized_job_uuid,
                    action=normalized_action,
                    decided_at=self._format_time(self._clock()),
                )
            )
            if created:
                # 决定已提交；计时器重排或后续调度失败也不能遗漏事项关闭通知。
                self._notify_attention_changed()
            self._cancel_manual_confirmation_timer(normalized_job_uuid)
            task_uuid = self._required_text(
                aggregate["task"].get("uuid"), field="task.uuid"
            )
            if normalized_action == "approve":
                decided_job = next(
                    job
                    for job in aggregate["jobs"]
                    if str(job.get("uuid")) == normalized_job_uuid
                )
                # 决定与后续调度分属两个组合根步骤。同一 approve 重放时，若
                # 持久 Job 仍停在待派发态，必须补做继续动作；已越过派发边界或
                # 已终态则只返回当前事实，避免重复物理下发。
                if (
                    (
                        decided_job.get("status") == "pending"
                        and decided_job.get("executor_kind") == "device_action"
                    )
                    or (
                        decided_job.get("status") == "running"
                        and decided_job.get("executor_kind") == "material_io"
                    )
                ):
                    with self._scheduler.execution_control_lock:
                        self._scheduler.resolve_manual_confirmation(
                            normalized_job_uuid,
                            approved=True,
                        )
                        if decided_job.get("executor_kind") == "material_io":
                            self._execute_material_io(normalized_job_uuid)
                return self._aggregate(task_uuid)
            if aggregate["task"].get("status") in {
                "succeeded",
                "failed",
                "canceled",
                "timeout",
            }:
                if aggregate["task"].get("status") == "canceled":
                    return self._release_canceled_inventory(
                        aggregate,
                        reason="manual_confirmation_rejected",
                    )
                return self._aggregate(task_uuid)
            # 同一 reject 在取消步骤失败后可补做 Task Cancel；取消已经进入终态
            # 时由上方直接返回，不生成新的取消命令。
            return self.cancel(
                task_uuid,
                reason="manual_confirmation_rejected",
            )
        except (StoreConflict, ValueError) as error:
            raise TaskSchedulerBridgeError(str(error)) from error

    def request_uncertain_resolution(
        self,
        job_uuid: str,
        *,
        reason: str,
        device_command_id: str,
    ) -> dict[str, Any]:
        """创建 Edge UNKNOWN 处置命令并保持执行锁到提交证明到达。"""

        if self._closed:
            raise TaskSchedulerBridgeError("工作流任务调度桥已经关闭")
        normalized_job = self._required_text(job_uuid, field="job_uuid")
        normalized_reason = self._required_text(reason, field="reason")
        try:
            result = self._scheduler.request_uncertain_resolution(
                normalized_job,
                reason=normalized_reason,
            )
            command_uuid = self._required_text(
                result.get("command_uuid"),
                field="resolution.command_uuid",
            )
            aggregate = self._projection.project_uncertain_resolution_requested(
                normalized_job,
                command_uuid=command_uuid,
                reason=normalized_reason,
                device_command_id=str(device_command_id or "").strip(),
            )
            return {
                "job": next(
                    job for job in aggregate["jobs"] if job["uuid"] == normalized_job
                ),
                "resolution_command_uuid": command_uuid,
                "pending_edge_confirmation": True,
                "created": bool(result.get("created", True)),
            }
        except (StoreConflict, StoreNotFound, ValueError) as error:
            raise TaskSchedulerBridgeError(str(error)) from error

    def reconcile_job_quantity(
        self,
        job_uuid: str,
        *,
        consumptions: Sequence[Mapping[str, Any]],
        reason: str,
    ) -> dict[str, Any]:
        """凭已保存的设备成功证明更正数量，随后只重放结果、不重新执行动作。"""

        with self._quantity_reconciliation_lock:
            return self._reconcile_job_quantity(
                job_uuid, consumptions=consumptions, reason=reason
            )

    def _reconcile_job_quantity(
        self,
        job_uuid: str,
        *,
        consumptions: Sequence[Mapping[str, Any]],
        reason: str,
    ) -> dict[str, Any]:
        """串行保存请求和跨库结算，并以库存已接受请求校验响应丢失后的重放。"""

        if self._closed or self._quantity_inventory is None:
            raise TaskSchedulerBridgeError("当前模式没有可用的数量结算权威")
        reason = self._required_text(reason, field="reason")
        job = self._store.get_job(job_uuid)
        task_uuid = str(job["workflow_task_uuid"])
        pending = any(
            row["task_uuid"] == task_uuid and row["job_uuid"] == job_uuid
            for row in self._quantity_inventory.pending_consumption_reconciliations()
        )
        outcome = self._projection.stage_quantity_reconciliation(
            job_uuid, consumptions=consumptions, reason=reason, can_replace=pending
        )
        committed = CommittedJobOutcome(**outcome)
        if pending:
            self._quantity_inventory.reconcile_consumption(
                task_uuid=task_uuid, job_uuid=job_uuid, consumptions=consumptions, reason=reason
            )
        else:
            committed_consumptions = self._quantity_inventory.list_job_consumptions(job_uuid)
            request = {"consumptions": [dict(item) for item in consumptions], "reason": reason}
            if not committed_consumptions or any(
                ((item.get("meta_data") or {}).get("reconciliation") or {}).get("request") != request
                for item in committed_consumptions
            ):
                raise StoreConflict("作业没有已提交的同一数量更正，不能直接完成结果")
        self._complete_reconciled_quantity_result(job_uuid, committed)
        # 同一任务可能在首个失败意图后又收到并行作业结果。只重放这些已保存的
        # 成功证据，让每个作业分别结算；失败项仍保持待对账，不能提前释放预留。
        for sibling in self._store.list_jobs(task_uuid):
            if sibling["uuid"] == job_uuid or sibling.get("uncertainty_reason") != QUANTITY_RECONCILIATION_REQUIRED:
                continue
            evidence = (sibling.get("control_data") or {}).get("quantity_reconciliation") or {}
            saved = evidence.get("committed_outcome")
            if isinstance(saved, dict):
                try:
                    self._complete_reconciled_quantity_result(
                        sibling["uuid"], CommittedJobOutcome(**saved)
                    )
                except (ValueError, StoreConflict, sqlite3.IntegrityError):
                    logger.exception("并行作业数量仍需对账：%s", sibling["uuid"])
        if not self._pending_quantity_consumption(task_uuid):
            if self._quantity_restart_pending(task_uuid):
                self._complete_deferred_quantity_restart(task_uuid)
            elif self._scheduler.workflow_snapshot(task_uuid) is None:
                task = self._store.get_task(task_uuid)
                if task["status"] in {"pending", "running", "canceling"}:
                    # 同进程重建只因数量核对暂缓还原，核对完成后仍复用原任务。
                    self._recover_running_task(task)
                    self.reschedule()
        return self._store.get_job(job_uuid)

    def _complete_reconciled_quantity_result(
        self, job_uuid: str, outcome: CommittedJobOutcome
    ) -> None:
        """存在原调度运行时完成其在途节点；重启后只恢复持久结果投影。"""

        if job_uuid in self._scheduler.snapshot().get("inflight_jobs", {}):
            self._scheduler.on_job_outcome(job_uuid, outcome)
        else:
            self._replay_persisted_job_outcome(job_uuid, outcome)

    def settle_failed_material_transfer(
        self,
        job_uuid: str,
        *,
        actual_change_set: Mapping[str, Any],
        reason: str,
    ) -> dict[str, Any]:
        """提交失败转运的实际物料位置并释放物理占用。

        参数：``job_uuid`` 是等待物料位置对账的失败作业；
        ``actual_change_set`` 必须完整声明物料、实际父资源以及实际库位 UUID 或
        名称；``reason`` 是操作员说明。返回：保持失败主状态、已完成物理结算的
        作业。异常：身份、库存事实、停止证明或幂等载荷冲突时抛桥接错误，且不会
        提前释放库存 Claim/Fence。
        """

        if self._closed:
            raise TaskSchedulerBridgeError("工作流任务调度桥已经关闭")
        normalized_job = self._required_text(job_uuid, field="job_uuid")
        normalized_reason = self._required_text(reason, field="reason")
        inventory = self._scheduler.station_resource_inventory
        if inventory is None:
            raise TaskSchedulerBridgeError("物料位置对账缺少工站库存权威")
        try:
            persisted_job = self._store.get_job(normalized_job)
            expected_change = persisted_job.get("expected_change_set")
            control = persisted_job.get("control_data")
            if (
                isinstance(expected_change, Mapping)
                and expected_change.get("kind") == "material_content_aliquot"
            ):
                receipts = actual_change_set.get("receipts")
                if (
                    persisted_job.get("status") not in {"failed", "canceled", "timeout"}
                    or persisted_job.get("uncertainty_reason")
                    != MATERIAL_CONTENT_RECONCILIATION_REQUIRED
                    or not isinstance(receipts, list)
                    or not isinstance(control, Mapping)
                    or not isinstance(control.get("physical_settlement"), Mapping)
                    or not control["physical_settlement"].get("execution_stopped")
                ):
                    raise StoreConflict("失败分装缺少完整目标回执或设备停止证明")
                claim = self._projection.get_execution_claim(normalized_job)
                if claim is None:
                    raise StoreConflict(f"失败分装作业缺少库存 Claim：{normalized_job}")
                settled = self._material_aliquot_settlement.settle_success(
                    job=persisted_job,
                    execution_claim=claim,
                    receipts=receipts,
                )
                change_set = {
                    "kind": "material_content_aliquot",
                    "source_material_uuid": expected_change.get("source_material_uuid"),
                    "target_material_uuids": list(
                        expected_change.get("target_material_uuids") or []
                    ),
                    "receipts": [dict(item) for item in receipts],
                    "inventory_result": dict(settled or {}),
                }
                aggregate = self._projection.project_failed_job_inventory_reconciled(
                    normalized_job,
                    actual_change_set=change_set,
                    reason=normalized_reason,
                )
                inventory.transition_dispatch_permit(
                    str(claim["claim_uuid"]), target_state="released"
                )
                self._finish_settled_terminal_task(normalized_job)
                return next(
                    item for item in aggregate["jobs"] if item["uuid"] == normalized_job
                )
            change_set = self._normalize_actual_material_change_set(actual_change_set)
            if (
                persisted_job.get("status") not in {"failed", "canceled", "timeout"}
                or persisted_job.get("uncertainty_reason")
                != MATERIAL_TRANSFER_RECONCILIATION_REQUIRED
                or not isinstance(expected_change, Mapping)
                or expected_change.get("kind") != "material_transfer"
                or change_set["material_uuid"] != expected_change.get("material_uuid")
                or not isinstance(control, Mapping)
                or not isinstance(control.get("physical_settlement"), Mapping)
                or not control["physical_settlement"].get("execution_stopped")
            ):
                raise StoreConflict("失败转运缺少匹配的预期变化或设备停止证明")
            claim = self._projection.get_execution_claim(normalized_job)
            if claim is None:
                raise StoreConflict(f"失败转运作业缺少库存 Claim：{normalized_job}")
            settled_material = inventory.settle_material_transfer(
                MaterialTransferCommand(
                    material_uuid=change_set["material_uuid"],
                    target_owner_material_uuid=change_set["target_owner_material_uuid"],
                    target_site_uuid=change_set.get("target_site_uuid", ""),
                    target_site_name=change_set.get("target_site_name", ""),
                    actor="physical_settlement",
                    causation_id=normalized_job,
                    effect_uuid=str(persisted_job.get("dispatch_effect_uuid") or ""),
                    claim_uuid=str(claim["claim_uuid"]),
                    job_uuid=normalized_job,
                    attempt=int(persisted_job.get("attempt") or 0),
                    parameter_hash=str(
                        persisted_job.get("dispatch_parameter_hash") or ""
                    ),
                    expected_change_set=dict(expected_change),
                    fences=tuple(
                        DispatchFence(
                            lock_key=str(fence["lock_key"]),
                            fencing_token=int(fence["fencing_token"]),
                        )
                        for fence in claim.get("fences", [])
                    ),
                )
            )
            change_set["inventory_result"] = dict(settled_material)
            aggregate = self._projection.project_failed_job_inventory_reconciled(
                normalized_job,
                actual_change_set=change_set,
                reason=normalized_reason,
            )
            inventory.transition_dispatch_permit(
                str(claim["claim_uuid"]),
                target_state="released",
            )
            self._finish_settled_terminal_task(normalized_job)
            return next(
                job for job in aggregate["jobs"] if job["uuid"] == normalized_job
            )
        except StationResourceError as error:
            raise TaskSchedulerBridgeError(
                error.message, reason_code=error.code
            ) from error
        except (
            StoreConflict,
            StoreNotFound,
            ValueError,
        ) as error:
            raise TaskSchedulerBridgeError(str(error)) from error

    def recover_active_tasks(
        self, *, execution_process_restarted: bool = False
    ) -> list[dict[str, Any]]:
        """同进程调度重建可恢复路由；执行进程重启只终结旧任务，不恢复动作。"""
        if self._closed:
            raise TaskSchedulerBridgeError("工作流任务调度桥已经关闭")
        if self._store.evidence_only():
            return []
        recovered: list[dict[str, Any]] = []
        with self._scheduler.execution_control_lock:
            self.error_control.recovering = True
            try:
                inventory = self._scheduler.station_resource_inventory
                if inventory is not None and not self._time_scheduling_enabled:
                    inventory.release_unprojected_dispatch_permits(
                        known_claim_uuids=self._projection.list_active_execution_claim_uuids()
                    )
                self._recover_inventory_commands()
                if execution_process_restarted:
                    self._scheduler.on_execution_process_restarted(())
                else:
                    self._recover_quantity_consumptions()
                    self._register_active_recovery_routes()
                    for task in self._store.list_execution_restart_candidates():
                        task_uuid = str(task["uuid"])
                        self._replay_saved_quantity_outcomes(task_uuid)
                        if self._pending_quantity_consumption(task_uuid):
                            continue
                        if self._quantity_restart_pending(task_uuid):
                            self._complete_deferred_quantity_restart(task_uuid)
                            continue
                        aggregate = self._aggregate(task_uuid)
                        if aggregate["task"]["status"] in {"succeeded", "failed", "canceled", "timeout"} and all(
                            job["status"] not in {"pending", "dispatched", "running", "cancel_requested"}
                            for job in aggregate["jobs"]
                        ):
                            continue
                        self._mark_inventory_claims_uncertain(aggregate)
                        if task_uuid not in self._submitted_tasks:
                            result = self._recover_running_task(aggregate["task"])
                            if result is not None:
                                recovered.append(result)
                    self._scheduler.replay_persisted_edge_projections(
                        feedback_listener=self._on_job_feedback,
                        outcome_listener=self._replay_persisted_job_outcome,
                        finished_listener=self._replay_persisted_job_finished,
                    )
                    # 回执可能已结束取消；只恢复仍在等待的原截止时间，不重发停止。
                    for job_uuid in tuple(self._task_by_job):
                        job = self._store.get_job(job_uuid)
                        if job["status"] == "cancel_requested":
                            self._schedule_cancel_timeout(job)
                    self._recover_terminal_inventory_cleanup()
                    self._scheduler.restore_not_started_dispatches()
                    for command in self.error_control.pending_commands():
                        self.apply_error_command(command)
            except Exception:
                # 还原失败不开放部分恢复的调度器；修复后可幂等重入。
                logger.exception("工站执行状态恢复失败，保持恢复屏障")
                raise
            else:
                self.error_control.recovering = False
        self._restore_human_timeouts()
        self.reschedule()
        return recovered

    def _recover_inventory_commands(self) -> None:
        """库存提交与工作流投影之间中断时，只补齐已提交的本地作业结果。"""
        from unilabos.workflow.inventory_commands import committed_inventory_command_result

        for task in self._store.list_execution_restart_candidates():
            task_uuid = str(task["uuid"])
            for job in self._store.list_jobs(task_uuid):
                if job["executor_kind"] != "workflow_command" or job["status"] != "pending":
                    continue
                result = committed_inventory_command_result(
                    self._store, self._scheduler.inventory_service, job["uuid"], task_uuid=task_uuid
                )
                if result is None:
                    continue
                aggregate = self._projection.project_local_control_evaluation(
                    job_uuid=job["uuid"], selected_branch=None, skipped_job_uuids=[], local_command_result=result,
                )
                self._reconcile_inventory_resource_intervals(task_uuid=task_uuid, aggregate=aggregate)
                self._settle_local_control_task(task_uuid, aggregate)

    def _recover_quantity_consumptions(self) -> None:
        """先补齐跨库消费意图，无法结算的作业保留原预留与人工对账原因。"""
        if self._quantity_inventory is None:
            return
        self._quantity_inventory.recover_pending()
        for pending in self._quantity_inventory.pending_consumption_reconciliations():
            try:
                self._mark_quantity_reconciliation(
                    pending["job_uuid"], reason=pending["reason"]
                )
            except (ValueError, StoreConflict, StoreNotFound, sqlite3.IntegrityError):
                logger.exception("数量对账记录无法恢复，保留原预留：%s", pending["job_uuid"])

    def _replay_saved_quantity_outcomes(self, task_uuid: str) -> None:
        """库存更正已提交但结果未投影时，只回放已保存的设备成功证明。"""
        for job in self._store.list_jobs(task_uuid):
            if job.get("uncertainty_reason") != QUANTITY_RECONCILIATION_REQUIRED:
                continue
            evidence = (job.get("control_data") or {}).get("quantity_reconciliation") or {}
            saved = evidence.get("committed_outcome")
            if isinstance(saved, dict):
                self._replay_persisted_job_outcome(job["uuid"], CommittedJobOutcome(**saved))

    def _quantity_restart_pending(self, task_uuid: str) -> bool:
        """读取延迟重启的持久证据，不能把新桥的空内存当作没有发生过重启。"""
        with self._store.read() as connection:
            return connection.execute(
                "SELECT 1 FROM workflow_node_job WHERE workflow_task_uuid=? "
                "AND deleted_at IS NULL AND "
                "json_extract(control_data,'$.quantity_reconciliation.runtime_restart_pending')=1 LIMIT 1",
                (task_uuid,),
            ).fetchone() is not None

    def _defer_quantity_restart(self, task_uuid: str) -> None:
        """立即失败任务，延迟清理已成功待结算作业的证据及资源占用。"""
        with self._store.transaction() as connection:
            connection.execute(
                "UPDATE workflow_node_job SET control_data=json_set(control_data, "
                "'$.quantity_reconciliation.runtime_restart_pending',json('true'), "
                "'$.quantity_reconciliation.runtime_restart_observed_at',COALESCE("
                "json_extract(control_data,'$.quantity_reconciliation.runtime_restart_observed_at'),?)) "
                "WHERE workflow_task_uuid=? AND deleted_at IS NULL AND uncertainty_reason=?",
                (utc_now(), task_uuid, QUANTITY_RECONCILIATION_REQUIRED),
            )
            if not self._quantity_restart_pending(task_uuid):
                raise StoreConflict("数量待对账任务缺少可保存重启事实的作业")
            fail_task_awaiting_restart_cleanup(connection, task_uuid=task_uuid)
        self._runtime_restart_cleanup_pending_tasks.add(task_uuid)

    def _complete_deferred_quantity_restart(self, task_uuid: str) -> None:
        """数量更正后收口实际重启的旧任务；持久完成之前始终保留重试标记。"""
        if self._pending_quantity_consumption(task_uuid):
            return
        aggregate = self._projection.project_execution_process_restarted(task_uuid)
        self.error_control.close_restarted_task(task_uuid)
        self._release_runtime_restart_resources(aggregate or self._aggregate(task_uuid))
        with self._store.transaction() as connection:
            connection.execute(
                "UPDATE workflow_node_job SET control_data=json_set(control_data, "
                "'$.quantity_reconciliation.runtime_restart_pending',json('false')) "
                "WHERE workflow_task_uuid=? AND deleted_at IS NULL AND "
                "json_extract(control_data,'$.quantity_reconciliation.runtime_restart_pending')=1",
                (task_uuid,),
            )
        self._runtime_restart_cleanup_pending_tasks.discard(task_uuid)

    def _recover_terminal_inventory_cleanup(self) -> None:
        """补齐终态 Task 在进程退出前未完成的库存释放与清理提交。

        参数与返回均为空。扫描成功、失败、取消和超时任务；仍存在 dispatched、
        running 或 cancel_requested 作业时保留全部占用。安全
        任务先幂等释放数量与实例物料预留，异常终态最后才写 cleanup settled；
        任一释放失败原样传播，禁止把未完成清理伪装成已结算。
        """

        unsafe_job_statuses = {
            "dispatched",
            "running",
            "cancel_requested",
        }
        for status in ("succeeded", "failed", "canceled", "timeout"):
            page = 1
            while True:
                task_page = self._store.list_tasks(
                    page=page,
                    page_size=200,
                    status=status,
                )
                for task in task_page["items"]:
                    task_uuid = str(task["uuid"])
                    if self._pending_quantity_consumption(task_uuid):
                        continue
                    if self._quantity_restart_pending(task_uuid):
                        self._complete_deferred_quantity_restart(task_uuid)
                        continue
                    jobs = self._store.list_jobs(task_uuid)
                    from unilabos.workflow.resource_lock_plan import (
                        failed_explicit_resource_interval_ids,
                    )

                    failure_latched = bool(
                        failed_explicit_resource_interval_ids(
                            task.get("execution_plan", {}),
                            jobs,
                        )
                    )
                    restart_cleanup_required = (
                        self._requires_runtime_restart_cleanup(task, jobs)
                    )
                    if restart_cleanup_required:
                        self._release_runtime_restart_resources(
                            {"task": task, "jobs": jobs}
                        )
                        continue
                    uncertain_jobs = [
                        job
                        for job in jobs
                        if str(job.get("uncertainty_reason") or "").strip()
                    ]
                    if uncertain_jobs:
                        self._mark_inventory_claims_uncertain(
                            {"task": task, "jobs": jobs}
                        )
                        continue
                    if any(
                        str(job.get("status")) in unsafe_job_statuses for job in jobs
                    ):
                        continue
                    reason = f"workflow_{status}_recovery"
                    if self._quantity_inventory is not None and not failure_latched:
                        self._quantity_inventory.release_task(
                            task_uuid,
                            reason=reason,
                        )
                    if not failure_latched and any(
                        job.get("executor_kind") == "material_source" for job in jobs
                    ):
                        self._material_sources.release_terminal_reservations(
                            task_uuid,
                            reason=reason,
                        )
                    if status == "succeeded":
                        release = getattr(self._scheduler.inventory_service, "release_workflow", None)
                        if callable(release):
                            release(task_uuid, reason=reason)
                    if (
                        status != "succeeded"
                        and not failure_latched
                        and task.get("cleanup_status")
                        in CLEANUP_STATUSES_SETTLEABLE_AFTER_TERMINAL
                    ):
                        self._projection.project_cleanup_settled(task_uuid)
                if page * 200 >= int(task_page["total"]):
                    break
                page += 1

    def _mark_inventory_claims_uncertain(
        self,
        aggregate: Mapping[str, Any],
    ) -> None:
        """把等待物理结算作业的库存 Claim 统一冻结为 uncertain。

        参数：``aggregate`` 是同一任务的持久 Task/Job 聚合。返回无；只处理带
        ``uncertainty_reason`` 且已有工作流 Claim 的作业。异常：物理部署缺少库存
        权威、Claim 或状态转换失败时原样传播；记录型干跑允许没有库存副本。
        """

        inventory = self._scheduler.station_resource_inventory
        jobs = aggregate.get("jobs")
        if not isinstance(jobs, list):
            raise StoreConflict("任务聚合缺少作业数组")
        for job in jobs:
            if not isinstance(job, Mapping):
                continue
            uncertainty_reason = str(job.get("uncertainty_reason") or "").strip()
            if not uncertainty_reason:
                # 同一终态任务通常还包含 workflow_input、本地控制和此前已成功的
                # 作业；这些作业从未签发物理 Claim，恢复时不得把它们误判为损坏。
                continue
            job_uuid = self._required_text(job.get("uuid"), field="job.uuid")
            claim = self._projection.get_execution_claim(job_uuid)
            if claim is None:
                # 错误处置手动通道本来不签发 Claim；仅恢复结果路由，保留待核对事实。
                # 重试的 workflow 通道即使保留手动来源元数据，仍须持有自己的 Claim。
                if job.get("execution_source") == "error_manual":
                    continue
                if self._scheduler.physical_dispatch_enabled:
                    raise StoreConflict(f"等待物理结算的作业缺少库存 Claim：{job_uuid}")
                continue
            if claim["state"] == "released":
                continue
            if inventory is None:
                if self._scheduler.physical_dispatch_enabled:
                    raise StoreConflict("物理部署缺少库存 DispatchPermit 权威")
                continue
            inventory.transition_dispatch_permit(
                str(claim["claim_uuid"]),
                target_state="uncertain",
            )

    def _register_active_recovery_routes(self) -> None:
        """从持久任务重建结果重放所需的稳定路由。"""

        for status in ("running", "pending", "canceling"):
            page = 1
            while True:
                task_page = self._store.list_tasks(
                    page=page,
                    page_size=200,
                    status=status,
                )
                for task in task_page["items"]:
                    task_uuid = self._required_text(task.get("uuid"), field="task.uuid")
                    for job in self._store.list_jobs(task_uuid):
                        if job.get("status") in {
                            "dispatched",
                            "running",
                            "cancel_requested",
                        }:
                            job_uuid = self._required_text(
                                job.get("uuid"), field="job.uuid"
                            )
                            self._task_by_job[job_uuid] = task_uuid
                if page * 200 >= int(task_page["total"]):
                    break
                page += 1

    def _replay_persisted_job_finished(
        self,
        job_uuid: str,
        success: bool,
        ret_value: Any,
        suc_type: str,
    ) -> None:
        """把已提交 Edge 结果先投影终态，再收敛任务清理。"""

        if self._scheduler.has_inflight_job(job_uuid):
            self._scheduler.on_job_finished(job_uuid, success, ret_value, suc_type)
            return
        self._on_job_finished(job_uuid, success, ret_value, suc_type)
        self._on_job_settled(job_uuid, success, ret_value, suc_type)

    def _replay_persisted_job_outcome(
        self,
        job_uuid: str,
        outcome: CommittedJobOutcome,
    ) -> None:
        """把已提交保真结果投影终态并收敛任务清理。

        参数：``job_uuid`` 是持久作业身份；``outcome`` 是 Edge 发件箱重放的完整
        终态证据。返回无。异常：工作流投影或清理失败向上传播，结果保持待投影；
        本方法只做投递重放（DeliveryReplay），不会再次派发物理作业。
        """

        try:
            if self._scheduler.has_inflight_job(job_uuid):
                self._scheduler.on_job_outcome(job_uuid, outcome)
                return
            self._on_job_outcome(job_uuid, outcome)
        except (ValueError, StoreConflict, sqlite3.IntegrityError):
            job = self._store.get_job(job_uuid)
            evidence = (job.get("control_data") or {}).get("quantity_reconciliation") or {}
            if (
                job.get("uncertainty_reason") != QUANTITY_RECONCILIATION_REQUIRED
                or evidence.get("committed_outcome") != outcome.as_dict()
            ):
                raise
            # 只隔离有同一成功证明的数量异常，其他结果或存储错误仍须上抛。
            logger.exception("库存消费等待人工对账，保留执行占用：%s", job_uuid)
            return
        success = outcome.outcome == "succeeded"
        ret_value = outcome.return_info.get("return_value", outcome.return_info)
        self._on_job_settled(
            job_uuid,
            success,
            ret_value,
            "normal" if success else outcome.outcome,
        )

    def _on_execution_process_restarted(self, job_uuids: tuple[str, ...]) -> None:
        """确认执行进程更换后终结全部活动任务，清理失败可幂等重入。"""
        self.error_control.recovering = True
        inventory_store = getattr(self._scheduler.inventory_service, "store", None)
        if inventory_store is not None:
            # 软件 Claim 清理前保留已发出车辆预占的物理责任，禁止重派。
            from unilabos.app.scheduler.inventory.transport_allocation import TransportAllocations
            TransportAllocations(inventory_store).recover_execution_restart()
        self._recover_inventory_commands()
        self._recover_quantity_consumptions()
        # 事件中的 Job 列表仅是执行端观测，不得遗漏暂停、排队或节点间隙的任务。
        candidates = {
            task["uuid"] for task in self._store.list_execution_restart_candidates()
        }
        candidates.update(self._runtime_restart_cleanup_pending_tasks)
        with self._store.read() as conn:
            candidates.update(
                row[0] for row in conn.execute(
                    "SELECT DISTINCT j.workflow_task_uuid FROM workflow_node_job j "
                    "JOIN workflow_task t ON t.uuid=j.workflow_task_uuid "
                    "WHERE j.deleted_at IS NULL AND t.deleted_at IS NULL AND "
                    "json_extract(j.control_data,'$.quantity_reconciliation.runtime_restart_pending')=1"
                )
            )
            candidates.update(
                row[0]
                for row in conn.execute(
                    "SELECT uuid FROM workflow_task WHERE status='failed' "
                    "AND cleanup_status='pending' AND deleted_at IS NULL AND ("
                    "EXISTS (SELECT 1 FROM json_each(workflow_task.error_info) "
                    "WHERE json_extract(value,'$.code')='execution_process_restarted') OR "
                    "EXISTS (SELECT 1 FROM workflow_node_job j WHERE j.workflow_task_uuid=workflow_task.uuid "
                    "AND json_extract(j.control_data,'$.runtime_interruption.reason')='execution_process_restarted'))"
                )
            )
        for task_uuid in sorted(candidates):
            self._replay_saved_quantity_outcomes(task_uuid)
            if self._pending_quantity_consumption(task_uuid):
                self._defer_quantity_restart(task_uuid)
                continue
            if self._quantity_restart_pending(task_uuid):
                self._complete_deferred_quantity_restart(task_uuid)
                continue
            self._runtime_restart_cleanup_pending_tasks.add(task_uuid)
            aggregate = self._projection.project_execution_process_restarted(task_uuid)
            self.error_control.close_restarted_task(task_uuid)
            self._release_runtime_restart_resources(
                aggregate or self._aggregate(task_uuid)
            )
            self._runtime_restart_cleanup_pending_tasks.discard(task_uuid)
        # 旧 Edge 账本可能仍引用已删除的 Job；整站重启后这些路由同样失效。
        for job_uuid, task_uuid in tuple(self._task_by_job.items()):
            if task_uuid not in self._runtime_restart_cleanup_pending_tasks:
                self._task_by_job.pop(job_uuid, None)
                self._cancel_cancel_timer(job_uuid)
                self._cancel_manual_confirmation_timer(job_uuid)
        self.error_control.finish_runtime_restart()
        self.error_control.recovering = False

    def _on_execution_connection_lost(
        self,
        job_uuids: tuple[str, ...],
    ) -> None:
        """连接中断不能证明进程退出；暂停新动作并等待重连。"""

        for job_uuid in job_uuids or tuple(self.active_or_uncertain_job_ids()):
            try:
                job = self._store.get_job(job_uuid)
            except StoreNotFound:
                continue
            if job.get("start_state") == "NOT_STARTED":
                continue
            if job.get("uncertainty_reason") == QUANTITY_RECONCILIATION_REQUIRED:
                continue
            if job["status"] in {"dispatched", "running", "cancel_requested"}:
                self.error_control.report_error(
                    job_uuid,
                    phase="execution_unknown",
                    message="执行连接中断，动作结果待核对",
                )
                if job.get("uncertainty_reason") != "execution_connection_lost":
                    self._transition_inventory_claim(job_uuid, target_state="uncertain")
                    self._projection.project_execution_attention(job_uuid, reason="execution_connection_lost")
        self._scheduler.restore_not_started_dispatches()

    def _release_runtime_restart_resources(self, aggregate: Mapping[str, Any]) -> None:
        """释放中断任务的软件执行占用；不请求或推断实际库存对账。"""
        task_uuid = str(aggregate["task"]["uuid"])
        jobs = aggregate["jobs"]
        if self._pending_quantity_consumption(task_uuid):
            return
        inventory = self._scheduler.station_resource_inventory
        for job in jobs:
            job_uuid = str(job["uuid"])
            claim = self._projection.get_execution_claim(job_uuid)
            if claim is not None and inventory is not None:
                inventory.transition_dispatch_permit(
                    str(claim["claim_uuid"]), target_state="released"
                )
            self._task_by_job.pop(job_uuid, None)
            self._cancel_cancel_timer(job_uuid)
            self._cancel_manual_confirmation_timer(job_uuid)
        release_permits = getattr(inventory, "release_task_dispatch_permits", None)
        if callable(release_permits):
            # 覆盖库存 Permit 已创建、工作流 Claim 尚未提交的崩溃窗口。
            release_permits(task_uuid=task_uuid)
        if self._quantity_inventory is not None:
            self._quantity_inventory.release_task(task_uuid, reason="runtime_restarted")
        if any(job.get("executor_kind") == "material_source" for job in jobs):
            self._material_sources.release_terminal_reservations(
                task_uuid, reason="runtime_restarted"
            )
        # 包括未投递和未 ACK 的旧命令，避免新执行进程重新领取它们。
        self._scheduler.fail_restarted_jobs([str(job["uuid"]) for job in jobs])
        with self._store.transaction() as conn:
            conn.execute(
                "UPDATE workflow_task SET cleanup_status='settled', attention_reason=NULL, "
                "update_time=? WHERE uuid=? AND cleanup_status!='settled'",
                (utc_now(), task_uuid),
            )
        self._submitted_tasks.discard(task_uuid)
        self._admission_pending_tasks.discard(task_uuid)
        self._finish_serial_admission(task_uuid)

    def _recover_running_task(self, task: Mapping[str, Any]) -> dict[str, Any] | None:
        """以有效 attempt 还原节点，历史在途执行只恢复结果路由，不重新发送。"""
        task_uuid = self._required_text(task.get("uuid"), field="task.uuid")
        jobs = self._store.list_jobs(task_uuid)
        serial = getattr(self._scheduler, "serial_queue_enabled", False)
        plan = task.get("execution_plan") or {}
        if task["status"] == "pending" and plan.get("inventory_resource_binding") == "pending":
            # 等待轮次的任务可能仍只有模板资源计划；先恢复准入，再绑定工站实例。
            self._compiler.validate_inventory_admission(task, jobs)
            return self._restore_pending_admission(task_uuid, task, serial=serial)

        spec = self._compiler.compile(task, jobs)
        compiled_node_ids = {node.id for node in spec.nodes}
        pending_static_sources = [
            job
            for job in jobs
            if job.get("executor_kind") == "material_source"
            and bool(job.get("current_attempt", True))
            and job.get("status") == "pending"
            and str(job.get("workflow_node_uuid") or "") not in compiled_node_ids
        ]
        admission = self._projection.get_material_admission(task_uuid) if serial else None
        awaiting_serial_admission = (
            serial
            and task["status"] == "pending"
            and all(job["status"] == "pending" for job in jobs)
            and (admission is None or admission.get("status") != "admitted")
        )
        if pending_static_sources or awaiting_serial_admission:
            # 静态来源还在 pending，说明首次准入没通过。来源节点不在调度图里，
            # 直接恢复会让下游加液失去缺料屏障。留在准入重试，补料后再提交。
            return self._restore_pending_admission(task_uuid, task, serial=serial)
        restored_jobs = []
        handoffs = []
        completed = {}
        confirmations = {item["workflow_node_job_uuid"]: item for item in self._manual_confirmations.list_by_task(task_uuid)}
        with self._store.read() as conn:
            active_owners = {str(row["workflow_node_job_uuid"]) for row in conn.execute(
                "SELECT DISTINCT workflow_node_job_uuid FROM execution_lock_lease WHERE workflow_task_uuid=? AND state IN ('reserved','running')", (task_uuid,))}
        for job in jobs:
            job_uuid = str(job["uuid"])
            if task["status"] in {"pending", "running"} and job.get("current_attempt", True) and job["status"] in {"failed", "timeout"}:
                self.error_control.report_error(job_uuid, phase="execution", message="恢复未完成处置的节点错误")
            job["awaiting_error_decision"] = self.error_control.awaits_decision(job_uuid)
            self._task_by_job[job_uuid] = task_uuid
            data = job.get("control_data") or {}
            node_id = str(data.get("runtime_node_id") or job["workflow_node_uuid"])
            node = next((item for item in spec.nodes if item.id == node_id), None)
            payload = data.get("dispatch_payload") or {}
            confirmation = confirmations.get(job_uuid, {})
            approved = confirmation.get("status") == "approved"
            job["manual_continuation_pending"] = approved and job["status"] == "pending" and bool(payload)
            if (job.get("status") in {"dispatched", "running", "cancel_requested"} or job["manual_continuation_pending"]) and job["executor_kind"] not in {"condition", "repeat_until"}:
                restored_jobs.append(DispatchedJob(
                    job_id=job_uuid, workflow_id=task_uuid, node_id=node_id,
                    device_action_key=(f"/devices/{payload['device_id']}/{payload['action']}" if payload else (node.device_action_key if node else "")),
                    device_id=str(payload.get("device_id") or (node.device_id if node else "")),
                    device_material_uuid=str((data.get("actual_executor") or {}).get("material_uuid") or job.get("material_uuid") or (node.device_material_uuid if node else "")),
                    action_name=str(payload.get("action") or (node.action_name if node else "")),
                    dispatch_credentials={key: payload[key] for key in ("attempt", "command_uuid", "claim_uuid", "fences", "effect_uuid", "parameter_hash", "expected_change_set") if key in payload},
                    resolved_args=dict(job.get("param") or {}),
                    manual_confirmation_approved=approved,
                    manual_action_dispatched=(
                        approved
                        and job["status"] != "pending"
                        and job["executor_kind"] != "material_io"
                    ),
                    operator_released_keys=set(data.get("operator_released_keys") or []),
                    resource_lock_keys={str(item["lock_key"]) for item in data.get("execution_locks", [])} - set(data.get("operator_released_keys") or []),
                    active_resource_lock_keys=set(data.get("active_resource_lock_keys") or []) - set(data.get("operator_released_keys") or []),
                    resource_interval_ids=list(data.get("resource_interval_ids") or []),
                ))
            if job.get("current_attempt", True) and job["status"] == "succeeded" and not job["awaiting_error_decision"]:
                if node is not None:
                    completed[node_id] = job.get("return_info") or {}
            if (job["status"] in {"succeeded", "failed", "timeout"} and not job.get("uncertainty_reason")
                    and job_uuid in active_owners and data.get("resource_interval_ids") and data.get("resource_interval_ids_by_lock")):
                handoffs.append({"node_id": node_id, "job_id": job_uuid,
                    "resource_interval_ids": data["resource_interval_ids"],
                    "operator_released_keys": data.get("operator_released_keys") or [],
                    "resource_interval_ids_by_lock": data["resource_interval_ids_by_lock"]})
        restored = self._scheduler.restore_workflow(
            spec,
            completed,
            restored_jobs,
            {},
            handoffs,
            execution_facts=jobs,
            execution_mode=str(
                task.get("execution_mode") or task.get("run_mode") or "normal"
            ),
            dispatch_gate=str(
                task.get("dispatch_gate")
                or (
                    "paused"
                    if str(task.get("execution_mode") or task.get("run_mode") or "normal")
                    in {"switching_to_step", "step"}
                    else "active"
                )
            ),
        )
        self._submitted_tasks.add(task_uuid)
        for job in jobs:
            confirmation_status = confirmations.get(
                str(job["uuid"]), {}
            ).get("status")
            if (
                job.get("executor_kind") == "material_io"
                and job.get("status") == "running"
                and confirmation_status == "approved"
            ):
                self._scheduler.resolve_manual_confirmation(
                    str(job["uuid"]), approved=True
                )
                self._execute_material_io(str(job["uuid"]))
            elif confirmation_status == "pending":
                self._schedule_manual_confirmation_timeout(str(job["uuid"]))
        self._project_scheduler_trace_context(task_uuid, restored.get("trace_context"))
        return self._aggregate(task_uuid)

    def _restore_pending_admission(
        self, task_uuid: str, task: Mapping[str, Any], *, serial: bool
    ) -> dict[str, Any]:
        """重建等待准入的任务索引和串行顺序。"""
        self._admission_pending_tasks.add(task_uuid)
        if serial:
            self._scheduler.serial_admission_turn(task_uuid, WorkflowSpecCompiler.submitted_at(task))
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
        # 规划和库存唤醒可能持有本桥回调；先在锁外等调度循环静默，再关闭运行库。
        self._scheduler.close()
        with self._scheduler.execution_control_lock:
            if self._closed:
                return
            self._closed = True
            for timer in list(self._human_timers.values()):
                timer.cancel()
            self._human_timers.clear()
            self._scheduler.remove_admission_retry_listener(self._retry_pending_admissions)
            self._scheduler.unbind_dispatch_admission_authority(self._on_job_pre_dispatch)
            self._scheduler.unbind_manual_continuation_authority(
                self._on_manual_continuation_dispatching
            )
            self._scheduler.remove_job_execution_wait_listener(self._on_job_execution_wait)
            self._scheduler.remove_job_dispatch_accepted_listener(
                self._on_job_dispatch_accepted
            )
            self._scheduler.remove_job_dispatch_uncertain_listener(
                self._on_job_dispatch_uncertain
            )
            self._scheduler.remove_job_cancel_accepted_listener(
                self._on_job_cancel_accepted
            )
            self._scheduler.remove_job_cancel_uncertain_listener(
                self._on_job_cancel_uncertain
            )
            self._scheduler.remove_job_cancel_no_send_listener(self._on_job_cancel_no_send)
            self._scheduler.remove_job_feedback_listener(self._on_job_feedback)
            self._scheduler.remove_job_outcome_listener(self._on_job_outcome)
            self._scheduler.remove_job_finished_listener(self._on_job_finished)
            self._scheduler.remove_job_settled_listener(self._on_job_settled)
            self._scheduler.remove_local_control_listener(self._on_local_control_evaluated)
            self._scheduler.remove_execution_process_restarted_listener(
                self._on_execution_process_restarted
            )
            self._scheduler.remove_execution_connection_lost_listener(
                self._on_execution_connection_lost
            )
            self._scheduler.set_drain_blocker_provider(None)
            self._scheduler.set_workflow_inventory_release_guard(None)
            self._task_by_job.clear()
            self._submitted_tasks.clear()
            if getattr(self._scheduler, "serial_queue_enabled", False):
                for task_uuid in self._admission_pending_tasks:
                    self._scheduler.discard_serial_admission(task_uuid)
            self._admission_pending_tasks.clear()
            self._runtime_restart_cleanup_pending_tasks.clear()
        with self._cancel_timer_lock:
            timers = tuple(self._cancel_timers.values())
            self._cancel_timers.clear()
        for timer in timers:
            timer.cancel()
        with self._manual_timer_lock:
            manual_timer = self._manual_deadline_timer
            self._manual_deadline_timer = None
        if manual_timer is not None:
            manual_timer.cancel()
        # 与在途超时回调建立关闭栅栏；新回调会看到 ``_closed``
        # 并立即返回，已进入的回调则在此完成持久化后再退出。
        with self._manual_callback_lock:
            pass

    def active_or_uncertain_job_ids(self) -> set[str]:
        """返回阻止调度器安全排空的持久作业身份。

        参数：无。返回：已经派发、运行、请求取消，或业务已失败但仍持有不确定
        执行占用的 Job UUID 集合；已完整准入的人工确认同样阻止排空，内部物料来源
        解析除外。异常：持久库读取失败原样传播，使排空关闭式失败，禁止把读取失败
        解释为空闲。
        """

        return {
            self._required_text(job_uuid, field="job.uuid")
            for job_uuid in self._store.list_drain_blocking_job_ids()
        }

    def _retry_pending_admissions(self) -> None:
        """按持久顺序重试明确处于物料来源准入等待的 Task。

        参数：无。返回无；每个工作流任务（WorkflowTask）只在本轮重试一次，仍
        受阻时由 ``submit`` 重新登记。异常：存储、准入或调度失败原样传播，禁止
        在公开重排失败时继续派发其他普通动作。
        """

        if getattr(self._scheduler, "serial_queue_enabled", False):
            task_uuid = self._scheduler.serial_admission_candidate()
            if task_uuid is None:
                return
            try:
                task = self._store.get_task(task_uuid)
            except StoreNotFound:
                self._finish_serial_admission(task_uuid)
                return
            if task["status"] != "pending" or self._store.execution_held(task_uuid):
                # 中心取消、超时和隔离也可能绕过本桥直接结束排队任务。
                self._finish_serial_admission(task_uuid)
                return
            self._admission_pending_tasks.discard(task_uuid)
            self.submit(task)
            return

        pending = set(self._admission_pending_tasks)
        if not pending:
            return
        for task in self._store.list_recoverable_tasks(statuses=("pending",)):
            task_uuid = self._required_text(task.get("uuid"), field="task.uuid")
            if task_uuid not in pending:
                continue
            self._admission_pending_tasks.discard(task_uuid)
            self.submit(task)

    def _on_job_pre_dispatch(self, dispatching: dict[str, Any]) -> bool:
        """在物理派发前取得库存 Permit 并提交标准派发意图。

        参数：``dispatching`` 是既有调度器即将越过执行边界的作业摘要。返回：
        标准作业在库存事务中复验全部条件、取得 Claim/Fence，并把同一 Permit
        投影到工作流库后为真；资源竞争保持 ``pending``
        时为假。异常：缺库存权威、请求损坏或跨库投影冲突关闭式阻止物理派发。
        """

        if self._store.evidence_only():
            return False
        job_uuid = str(dispatching.get("job_id") or "")
        task_uuid = self._task_by_job.get(job_uuid)
        if task_uuid is None:
            raise StoreConflict(f"派发作业不属于当前持久调度权威：{job_uuid}")
        if self._store.execution_held(task_uuid):
            return False
        persisted_task = self._store.get_task(task_uuid)
        transport = persisted_task.get("workflow_snapshot", {}).get("workflow", {}).get("meta_data", {}).get("center_transport_binding")
        if transport is not None:
            guard = getattr(self, "transport_dispatch_guard", None)
            if guard is None or not guard(transport):
                return False
        execution_mode = str(
            persisted_task.get("execution_mode")
            or persisted_task.get("run_mode")
            or "normal"
        )
        dispatch_gate = str(persisted_task.get("dispatch_gate") or "active")
        if dispatch_gate != "active" and execution_mode != "step":
            # 普通暂停和 normal→step 排空都必须在最后一道持久准入门禁再次
            # 复验，防止暂停命令与重排线程并发时让新 Job 越过物理边界。
            return False
        # 先登记阶段再校验其余派发摘要；任一校验、库存准入或持久投影异常都
        # 属于“已进入派发回调”的失败，不能走早期提交的终止/释放语义。
        self._mark_submission_phase(task_uuid, _SUBMISSION_PHASE_PRE_DISPATCH)
        dispatch_task_uuid = self._required_text(
            dispatching.get("workflow_id"), field="dispatching.workflow_id"
        )
        if dispatch_task_uuid != task_uuid:
            raise StoreConflict(f"派发作业与任务身份不一致：{job_uuid}")
        resolved_args = dispatching.get("resolved_args")
        if not isinstance(resolved_args, Mapping):
            raise StoreConflict(f"派发作业缺少最终解析参数：{job_uuid}")
        execution_locks = dispatching.get("execution_locks")
        if not isinstance(execution_locks, list):
            raise StoreConflict(f"派发作业缺少执行锁集合：{job_uuid}")
        # 等待图是 WorkflowStore 的只读权威，不要求测试或扩展注入的生命周期
        # Projection 同时实现诊断查询接口。
        wait_graph = TaskRuntimeProjection(self._store).get_execution_wait_graph()
        if wait_graph.get("deadlock_detected"):
            self._projection.project_execution_lock_wait(
                task_uuid=task_uuid,
                job_uuid=job_uuid,
                execution_locks=[],
                wait_code="execution_deadlock_detected",
                wait_message="检测到工站作业等待环，已冻结新的物理派发",
                max_active_tasks=self._max_active_tasks,
                max_tasks_per_workflow=self._max_tasks_per_workflow,
            )
            return False
        raw_candidates = dispatching.get("dispatch_candidates")
        dispatch_candidates: list[Mapping[str, Any]] = []
        if raw_candidates is not None:
            if (
                not isinstance(raw_candidates, list)
                or len(raw_candidates) < 2
                or any(not isinstance(item, Mapping) for item in raw_candidates)
            ):
                raise StoreConflict(f"派发候选集合无效：{job_uuid}")
            dispatch_candidates = list(raw_candidates)
        device_tenancy = dispatching.get("device_tenancy")
        if device_tenancy is not None and not isinstance(device_tenancy, Mapping):
            raise StoreConflict(f"派发作业设备托管转换不是对象：{job_uuid}")
        raw_operate = dispatching.get("operate_in_place_condition")
        required_device_tenancy = None
        if raw_operate is not None:
            conditions = _operate_condition_mappings(raw_operate)
            requirements = [
                {
                    "material_uuid": item["material_uuid"],
                    "device_lock_key": device_lock_key(item["device_material_uuid"]),
                }
                for item in conditions
            ]
            required_device_tenancy = requirements[0] if len(requirements) == 1 else requirements
        actual_executor = dispatching.get("actual_executor")
        if actual_executor is not None and not isinstance(actual_executor, Mapping):
            raise StoreConflict(f"派发作业实际执行器不是对象：{job_uuid}")
        foreign_use = self._foreign_unfinished_use_of_unload(
            task_uuid=task_uuid, job_uuid=job_uuid, resolved_args=resolved_args
        )
        if foreign_use is not None:
            blocking_task_uuid, blocking_job_uuid = foreign_use
            # 卸下的物料仍有其他任务未完成的作业要用（技术方案 7.9）：等对方用完再卸。
            self._projection.project_execution_lock_wait(
                task_uuid=task_uuid,
                job_uuid=job_uuid,
                execution_locks=[],
                blocking_task_uuid=blocking_task_uuid,
                blocking_job_uuid=blocking_job_uuid,
                wait_code="task_material_claimed",
                wait_message="要卸下的物料仍有其他任务未完成的作业在使用，等待其完成",
                max_active_tasks=self._max_active_tasks,
                max_tasks_per_workflow=self._max_tasks_per_workflow,
            )
            dispatching["serial_blocking_workflow_id"] = blocking_task_uuid
            return False
        reserve_deferred = getattr(
            self._quantity_inventory, "reserve_deferred_runtime_requirements", None
        )
        if callable(reserve_deferred):
            try:
                # 来源缺料后由补料分支提供容器的数量需求，在越过物理边界前补做预留；
                # 容器不足或无法唯一确定时本节点失败，不能等设备动完才发现。
                reserve_deferred(task_uuid=task_uuid, job_uuid=job_uuid)
            except StoreConflict as error:
                raise DispatchAdmissionConflict(str(error)) from error
        inventory_authority = self._scheduler.station_resource_inventory
        permit = None
        permit_committed = False
        permit_projected = False
        if inventory_authority is None and self._scheduler.physical_dispatch_enabled:
            raise StoreConflict("物理派发未装配库存 DispatchPermit 权威")
        if inventory_authority is not None:
            try:
                if dispatch_candidates:
                    requests: list[DispatchAdmissionRequest] = []
                    for candidate in dispatch_candidates:
                        candidate_args = candidate.get("resolved_args")
                        candidate_locks = candidate.get("execution_locks")
                        candidate_transfer = candidate.get("transfer_dispatch_condition")
                        if (
                            not isinstance(candidate_args, Mapping)
                            or not isinstance(candidate_locks, list)
                            or not isinstance(candidate_transfer, Mapping)
                        ):
                            raise StoreConflict(f"派发候选字段不完整：{job_uuid}")
                        candidate_dispatching = dict(dispatching)
                        candidate_dispatching.update(candidate)
                        requests.append(
                            self._dispatch_admission_request(
                                dispatching=candidate_dispatching,
                                task_uuid=task_uuid,
                                job_uuid=job_uuid,
                                resolved_args=candidate_args,
                                execution_locks=candidate_locks,
                            )
                        )
                    decision = inventory_authority.acquire_dispatch_permit_candidates(
                        tuple(requests)
                    )
                    selected_index = decision.selected_candidate_index
                    if decision.acquired and not (0 <= selected_index < len(dispatch_candidates)):
                        raise StoreConflict(f"库存权威返回了非法派发候选索引：{job_uuid}")
                    if decision.acquired:
                        selected = dispatch_candidates[selected_index]
                        dispatching.update(selected)
                        resolved_args = selected["resolved_args"]
                        execution_locks = selected["execution_locks"]
                else:
                    request = self._dispatch_admission_request(
                        dispatching=dispatching,
                        task_uuid=task_uuid,
                        job_uuid=job_uuid,
                        resolved_args=resolved_args,
                        execution_locks=execution_locks,
                    )
                    decision = inventory_authority.acquire_dispatch_permit(request)
            except StationResourceError as error:
                if not is_temporary_resource_condition(error.code):
                    raise
                self._projection.project_execution_lock_wait(
                    task_uuid=task_uuid,
                    job_uuid=job_uuid,
                    execution_locks=[],
                    wait_code=error.code,
                    wait_message=error.message,
                    wait_resources=error.resources,
                    max_active_tasks=self._max_active_tasks,
                    max_tasks_per_workflow=self._max_tasks_per_workflow,
                )
                return False
            if not decision.acquired:
                self._projection.project_execution_lock_wait(
                    task_uuid=task_uuid,
                    job_uuid=job_uuid,
                    execution_locks=execution_locks,
                    blocking_task_uuid=(decision.blocking_task_uuid or None),
                    blocking_job_uuid=(decision.blocking_job_uuid or None),
                    wait_code=decision.wait_code,
                    wait_message=decision.wait_message,
                    max_active_tasks=self._max_active_tasks,
                    max_tasks_per_workflow=self._max_tasks_per_workflow,
                )
                return False
            permit = decision.permit
            assert permit is not None
        try:
            projection_kwargs: dict[str, Any] = {}
            if required_device_tenancy is not None:
                projection_kwargs["required_device_tenancy"] = required_device_tenancy
                projection_kwargs["native_operate_condition"] = deepcopy(raw_operate)
                projection_kwargs["inventory_authority"] = inventory_authority
            for field in (
                "resource_plan_id",
                "resource_interval_ids",
                "resource_acquire_set_id",
                "resource_interval_ids_by_lock",
            ):
                value = dispatching.get(field)
                if value:
                    projection_kwargs[field] = value
            for source, target in (
                ("resource_preheld_lock_keys", "preheld_lock_keys"),
                ("resource_preheld_job_uuids", "preheld_job_uuids"),
            ):
                value = dispatching.get(source)
                if value:
                    projection_kwargs[target] = value
            if "manual_confirmation" in dispatching:
                manual_config = dispatching.get("manual_confirmation")
                if not isinstance(manual_config, Mapping):
                    raise StoreConflict(f"人工确认配置必须是对象：{job_uuid}")
                projection_kwargs.update(
                    {
                        "manual_confirmation_config": manual_config,
                        "projected_at": self._format_time(self._clock()),
                    }
                )
            aggregate = self._projection.project_pre_dispatch(
                task_uuid=task_uuid,
                job_uuid=job_uuid,
                resolved_param=resolved_args,
                execution_locks=execution_locks,
                device_tenancy=device_tenancy,
                actual_executor=actual_executor,
                dispatch_permit=(permit.as_mapping() if permit is not None else None),
                dispatch_payload=dispatching.get("dispatch_payload"),
                max_active_tasks=self._max_active_tasks,
                max_tasks_per_workflow=self._max_tasks_per_workflow,
                max_in_flight_jobs=self._max_in_flight_jobs,
                aging_interval_seconds=self._scheduler.aging_interval_seconds,
                **projection_kwargs,
            )
            projected_job = next(
                (job for job in aggregate["jobs"] if job["uuid"] == job_uuid),
                None,
            )
            if projected_job is None:
                raise StoreConflict(f"派发作业投影后消失：{job_uuid}")
            is_manual_waiting = (
                projected_job.get("executor_kind")
                in {"manual_confirm", "material_io"}
                and projected_job.get("status") == "running"
            )
            if projected_job["status"] != "dispatched" and not is_manual_waiting:
                if inventory_authority is not None and permit is not None:
                    inventory_authority.transition_dispatch_permit(
                        permit.claim_uuid,
                        target_state="released",
                    )
                return False
            # 工作流库已持久化派发意图和 Claim。从这一行起，
            # 即使库存库的 reserved 确认返回异常，也不能再把
            # prepared Permit 当成“未投影”释放。
            permit_projected = True
            if inventory_authority is not None and permit is not None:
                inventory_authority.transition_dispatch_permit(
                    permit.claim_uuid,
                    target_state="reserved",
                )
                preheld_jobs = tuple(
                    sorted(
                        {
                            str(value)
                            for value in (dispatching.get("resource_preheld_job_uuids") or [])
                            if str(value).strip()
                        }
                    )
                )
                preheld_keys = tuple(
                    sorted(
                        {
                            str(value)
                            for value in (dispatching.get("resource_preheld_lock_keys") or [])
                            if str(value).strip()
                        }
                    )
                )
                retry_ancestors = self.error_control.retry_ancestor_ids(job_uuid)
                preheld_jobs = tuple(
                    identity for identity in preheld_jobs
                    if (previous := self._store.get_job(identity))["status"] == "succeeded"
                    or (identity in retry_ancestors and previous["status"] in {"failed", "timeout"}
                        and not previous.get("uncertainty_reason"))
                )
                if preheld_jobs:
                    release_preheld = getattr(
                        inventory_authority,
                        "release_preheld_dispatch_claims",
                        None,
                    )
                    if not callable(release_preheld):
                        raise StoreConflict("库存权威不支持安全的连续 Claim 交接")
                    release_preheld(
                        task_uuid=task_uuid,
                        job_uuids=preheld_jobs,
                        lock_keys=preheld_keys,
                    )
                permit_committed = True
        except BaseException:
            if inventory_authority is not None and permit is not None and not permit_committed:
                if permit_projected:
                    freeze_projected_dispatch_permit(
                        inventory=inventory_authority,
                        projection=self._projection,
                        job_uuid=job_uuid,
                        claim_uuid=permit.claim_uuid,
                    )
                else:
                    inventory_authority.transition_dispatch_permit(
                        permit.claim_uuid,
                        target_state="released",
                    )
            raise
        if projected_job["status"] == "dispatched" or is_manual_waiting:
            claim = self._projection.get_execution_claim(job_uuid)
            if claim is None:
                raise StoreConflict(f"派发作业缺少持久 Claim：{job_uuid}")
            if permit is not None and str(claim["claim_uuid"]) != permit.claim_uuid:
                raise StoreConflict(f"工作流 Claim 与库存 Permit 不一致：{job_uuid}")
            dispatching.update(
                {
                    "attempt": int(projected_job["attempt"]),
                    "command_uuid": str(projected_job["edge_command_uuid"]),
                    "claim_uuid": str(claim["claim_uuid"]),
                    "fences": [dict(fence) for fence in claim["fences"]],
                    "effect_uuid": (
                        permit.effect_uuid
                        if permit is not None
                        else str(projected_job.get("dispatch_effect_uuid") or "")
                    ),
                    "parameter_hash": (
                        permit.parameter_hash
                        if permit is not None
                        else str(projected_job.get("dispatch_parameter_hash") or "")
                    ),
                    "expected_change_set": (
                        dict(permit.expected_change_set)
                        if permit is not None
                        else dict(projected_job.get("expected_change_set") or {})
                    ),
                }
            )
        if (
            projected_job.get("executor_kind")
            in {"manual_confirm", "material_io"}
            and projected_job.get("status") == "running"
        ):
            self._schedule_manual_confirmation_timeout(job_uuid)
            self._notify_attention_changed()
        return projected_job["status"] == "dispatched" or is_manual_waiting

    def _dispatch_admission_request(
        self,
        *,
        dispatching: Mapping[str, Any],
        task_uuid: str,
        job_uuid: str,
        resolved_args: Mapping[str, Any],
        execution_locks: list[Mapping[str, Any]],
    ) -> DispatchAdmissionRequest:
        """从最终动作参数和完整资源集构造不可变库存准入请求。

        参数：派发摘要、Task/Job 身份、最终参数和全部执行锁。返回：带确定性
        ``effect_uuid``、参数哈希、预期 ChangeSet 与可选转运条件的请求。异常：
        JSON 参数、锁字段或转运条件损坏时抛 ``StoreConflict``，不得猜测默认值。
        """

        try:
            parameter_bytes = json.dumps(
                resolved_args,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeError) as error:
            raise StoreConflict(f"派发最终参数不能稳定哈希：{job_uuid}") from error
        parameter_hash = "sha256:" + hashlib.sha256(parameter_bytes).hexdigest()
        job = self._store.get_job(job_uuid)
        attempt = int(job["attempt"])
        effect_uuid = str(
            uuid5(
                NAMESPACE_URL,
                f"unilabos-dispatch:{job_uuid}:{attempt}:{parameter_hash}",
            )
        )
        resources: list[DispatchResource] = []
        for raw in execution_locks:
            if not isinstance(raw, Mapping):
                raise StoreConflict("派发执行锁必须是对象")
            resources.append(
                DispatchResource(
                    lock_key=str(raw.get("lock_key") or ""),
                    scope=str(raw.get("scope") or ""),
                    material_uuid=str(raw.get("material_uuid") or ""),
                    site_uuid=str(raw.get("site_uuid") or ""),
                )
            )
        raw_transfer = dispatching.get("transfer_dispatch_condition")
        raw_relocation = dispatching.get("inventory_relocation_condition")
        raw_site_selection = dispatching.get("site_selection")
        raw_operate = dispatching.get("operate_in_place_condition")
        raw_aliquot = dispatching.get("aliquot_dispatch_condition")
        transfer = None
        operate_in_place = None
        aliquot = None
        inventory_relocation = None
        expected_change_set: dict[str, Any] = {"kind": "no_inventory_change"}
        if dispatching.get("executor_kind") == "material_io":
            operation, request_values = self._material_io_request_values(
                job,
                resolved_args,
            )
            expected_change_set = material_io_change_set(
                operation,
                {"operation": operation, **request_values},
            )
        if raw_transfer is not None:
            if not isinstance(raw_transfer, Mapping):
                raise StoreConflict("派发转运条件必须是对象")
            required = {
                "material_uuid",
                "source_owner_material_uuid",
                "source_site_uuid",
                "target_owner_material_uuid",
                "target_site_uuid",
                "executor_material_uuid",
                "gripper_site_uuid",
            }
            if set(raw_transfer) != required or any(
                not str(raw_transfer[field] or "").strip() for field in required
            ):
                raise StoreConflict("派发转运条件字段不完整")
            transfer = TransferDispatchCondition(
                **{field: str(raw_transfer[field]).strip() for field in required},
                allow_held_material=bool(dispatching.get("transfer_place_step")),
            )
            expected_change_set = {
                "kind": "material_transfer",
                "material_uuid": transfer.material_uuid,
                "source_site_uuid": transfer.source_site_uuid,
                "target_site_uuid": transfer.target_site_uuid,
            }
            if raw_site_selection is not None:
                if not isinstance(raw_site_selection, Mapping):
                    raise StoreConflict("派发库位选择审计信息必须是对象")
                required_selection = {
                    "version",
                    "owner_material_uuid",
                    "group_key",
                    "requested_reference",
                    "strategy",
                    "site_uuids",
                    "fingerprint",
                }
                if set(raw_site_selection) != required_selection:
                    raise StoreConflict("派发库位选择审计字段不完整")
                selection_site_uuids = raw_site_selection.get("site_uuids")
                selection_group = str(raw_site_selection.get("group_key") or "")
                requested_reference = str(raw_site_selection.get("requested_reference") or "")
                if (
                    raw_site_selection.get("version") != 1
                    or str(raw_site_selection.get("strategy") or "") != "sort_order"
                    or not str(raw_site_selection.get("owner_material_uuid") or "")
                    or not (selection_group or requested_reference)
                    or not str(raw_site_selection.get("fingerprint") or "")
                    or not isinstance(selection_site_uuids, list)
                    or not selection_site_uuids
                    or transfer.target_site_uuid not in selection_site_uuids
                ):
                    raise StoreConflict("派发库位选择审计信息非法")
                expected_change_set["site_selection"] = {
                    "group_key": selection_group,
                    "requested_reference": requested_reference,
                    "strategy": "sort_order",
                    "fingerprint": str(raw_site_selection["fingerprint"]),
                    "selected_site_uuid": transfer.target_site_uuid,
                }
        if raw_operate is not None:
            if transfer is not None:
                raise StoreConflict("同一作业不能同时声明转运和原位操作")
            operate_conditions = tuple(
                OperateInPlaceCondition(**item) for item in _operate_condition_mappings(raw_operate)
            )
            operate_in_place = operate_conditions[0] if len(operate_conditions) == 1 else operate_conditions
        if raw_aliquot is not None:
            if not isinstance(raw_aliquot, Mapping) or set(raw_aliquot) != {
                "source_material_uuid",
                "target_material_uuids",
            }:
                raise StoreConflict("派发分装条件字段不完整")
            source_uuid = str(raw_aliquot.get("source_material_uuid") or "").strip()
            raw_targets = raw_aliquot.get("target_material_uuids")
            if (
                not source_uuid
                or not isinstance(raw_targets, list)
                or not raw_targets
                or any(not str(value or "").strip() for value in raw_targets)
            ):
                raise StoreConflict("派发分装来源或目标集合非法")
            if transfer is not None or operate_in_place is not None:
                raise StoreConflict("同一作业不能同时声明分装、转运或原位操作")
            targets = tuple(str(value).strip() for value in raw_targets)
            aliquot = AliquotDispatchCondition(
                source_material_uuid=source_uuid,
                target_material_uuids=targets,
            )
            expected_change_set = {
                "kind": "material_content_aliquot",
                "source_material_uuid": source_uuid,
                "target_material_uuids": list(targets),
            }
        if raw_relocation is not None:
            if not isinstance(raw_relocation, Mapping):
                raise StoreConflict("库存改挂条件必须是对象")
            required_relocation = {
                "material_uuid",
                "source_owner_material_uuid",
                "source_site_uuid",
                "target_owner_material_uuid",
                "target_site_uuid",
            }
            if set(raw_relocation) != required_relocation or any(
                not str(raw_relocation[field] or "").strip() for field in required_relocation
            ):
                raise StoreConflict("库存改挂条件字段不完整")
            if transfer is not None or operate_in_place is not None or aliquot is not None:
                raise StoreConflict("同一作业不能同时声明库存改挂和机械臂转运")
            inventory_relocation = InventoryRelocationCondition(
                **{field: str(raw_relocation[field]).strip() for field in required_relocation}
            )
            expected_change_set = {
                "kind": "material_transfer",
                "material_uuid": inventory_relocation.material_uuid,
                "source_site_uuid": inventory_relocation.source_site_uuid,
                "target_site_uuid": inventory_relocation.target_site_uuid,
            }
        reserved_targets = []
        custody_endpoints = []
        task = self._store.get_task(task_uuid)
        resource_plan = (task.get("execution_plan") or {}).get("resource_plan") or {}
        by_alias = {r["alias"]: r["canonical_key"] for r in resource_plan.get("resources", [])}
        preheld_keys = set(dispatching.get("resource_preheld_lock_keys") or ())
        for pair in (resource_plan.get("metadata") or {}).get("transfers", []):
            is_pick = pair["pick_node_uuid"] == job["workflow_node_uuid"]
            key = by_alias.get(pair["target_site"], "")
            owner_key = by_alias.get(pair["target_resource"], "")
            # 冻结的拆分搬运合同证明这是预持有的放料端点。中间动作继续沿用
            # 该范围，库存准入仍核实原租约和前驱身份，不能扩大物料使用权限。
            if not is_pick and not {key, owner_key} <= preheld_keys:
                continue
            resource = next(
                (r for r in resources if r.lock_key == key and r.scope == "material_site"), None
            )
            if resource is None:
                raise StoreConflict("拆分搬运目标 Site 未绑定到完整执行资源集")
            custody_endpoints.append(resource.site_uuid)
            if is_pick:
                reserved_targets.append(resource.site_uuid)
        return DispatchAdmissionRequest(
            effect_uuid=effect_uuid,
            material_input_receipt=self._store.center_material_input_receipt(task_uuid),
            task_uuid=task_uuid,
            job_uuid=job_uuid,
            attempt=attempt,
            parameter_hash=parameter_hash,
            expected_change_set=expected_change_set,
            resources=tuple(resources),
            reserved_target_site_uuids=tuple(reserved_targets),
            custody_endpoint_site_uuids=tuple(custody_endpoints),
            shared_scope_lock_keys=tuple(dispatching.get("resource_shared_scope_keys") or ()),
            preheld_lock_keys=tuple(
                sorted(
                    {
                        str(value)
                        for value in (dispatching.get("resource_preheld_lock_keys") or [])
                        if str(value).strip()
                    }
                )
            ),
            preheld_job_uuids=tuple(
                sorted(
                    {
                        str(value)
                        for value in (dispatching.get("resource_preheld_job_uuids") or [])
                        if str(value).strip()
                    }
                )
            ),
            transfer=transfer,
            operate_in_place=operate_in_place,
            aliquot=aliquot,
            inventory_relocation=inventory_relocation,
        )

    @staticmethod
    def _material_io_request_values(
        job: Mapping[str, Any],
        resolved_args: Mapping[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        """把最终节点参数规范为 MaterialIOService 的稳定效果请求。"""

        operation = str(resolved_args.get("mode") or "").strip().lower()
        workflow_id = str(job.get("workflow_task_uuid") or "").strip()
        control_data = job.get("control_data")
        runtime_node_id = (
            str(control_data.get("runtime_node_id") or "").strip()
            if isinstance(control_data, Mapping)
            else ""
        )
        node_id = runtime_node_id or str(job.get("workflow_node_uuid") or "").strip()
        attempt = int(job.get("attempt") or 0)
        if not workflow_id or not node_id or attempt <= 0:
            raise StoreConflict("Material I/O 作业效果身份不完整")

        def resource_uuid(value: Any, field: str) -> str:
            if not isinstance(value, Mapping):
                raise StoreConflict(f"Material I/O {field} 必须是 ResourceSlot")
            identity = str(value.get("uuid") or "").strip()
            if not identity:
                raise StoreConflict(f"Material I/O {field}.uuid 不能为空")
            return identity

        if operation == "load":
            template_uuid = str(
                resolved_args.get("resource_template_uuid") or ""
            ).strip()
            site_name = str(resolved_args.get("site") or "").strip()
            site_uuid = str(resolved_args.get("site_uuid") or "").strip()
            if not template_uuid or not (site_uuid or site_name):
                raise StoreConflict("Material I/O load 缺少模板或目标 Site")
            request_values = {
                "template_uuid": template_uuid,
                "mount_material_uuid": resource_uuid(
                    resolved_args.get("mount"), "mount"
                ),
                "workflow_id": workflow_id,
                "node_id": node_id,
                "attempt": attempt,
            }
            if site_uuid:
                request_values["site_uuid"] = site_uuid
            if site_name:
                request_values["site"] = site_name
            supplied_name = str(resolved_args.get("name") or "").strip()
            if supplied_name:
                request_values["name"] = supplied_name
            barcode = str(resolved_args.get("barcode") or "").strip()
            if barcode:
                request_values["barcode"] = barcode
            if resolved_args.get("reagent") is not None:
                try:
                    request_values["reagent"] = canonicalize_load_reagent(
                        resolved_args.get("reagent")
                    )
                except MaterialIOError as error:
                    raise StoreConflict(error.message) from error
            return operation, request_values
        if operation == "unload":
            disposition = str(resolved_args.get("disposition") or "").strip().lower()
            scope = str(resolved_args.get("scope") or "").strip().lower()
            if not disposition or not scope:
                raise StoreConflict("Material I/O unload 缺少 disposition 或 scope")
            return operation, {
                "material_uuid": resource_uuid(
                    resolved_args.get("material"), "material"
                ),
                "disposition": disposition,
                "scope": scope,
                "workflow_id": workflow_id,
                "node_id": node_id,
                "attempt": attempt,
            }
        if operation == "refill":
            request_values = {
                "material_uuid": resource_uuid(
                    resolved_args.get("material"), "material"
                ),
                "mount_material_uuid": resource_uuid(
                    resolved_args.get("mount"), "mount"
                ),
                "workflow_id": workflow_id,
                "node_id": node_id,
                "attempt": attempt,
            }
            site_name = str(resolved_args.get("site") or "").strip()
            site_uuid = str(resolved_args.get("site_uuid") or "").strip()
            if site_uuid:
                request_values["site_uuid"] = site_uuid
            if site_name:
                request_values["site"] = site_name
            try:
                request_values["reagent"] = canonicalize_refill_reagent(
                    resolved_args.get("reagent")
                )
            except MaterialIOError as error:
                raise StoreConflict(error.message) from error
            return operation, request_values
        raise StoreConflict("Material I/O mode 必须是 load、unload 或 refill")

    def _execute_material_io(self, job_uuid: str) -> None:
        """幂等执行已批准的本地 Material I/O，再走标准作业完成监听链。"""

        job = self._store.get_job(job_uuid)
        if (
            job.get("executor_kind") != "material_io"
            or job.get("status") != "running"
        ):
            return
        confirmation = self._manual_confirmations.get_by_job(job_uuid)
        if confirmation.get("status") != "approved":
            raise StoreConflict("Material I/O 尚未获得人工批准")
        service = getattr(self._scheduler.inventory_service, "material_io", None)
        if service is None:
            raise StoreConflict("Material I/O 未装配本地库存服务")
        claim = self._projection.get_execution_claim(job_uuid)
        if claim is None:
            raise StoreConflict("Material I/O 缺少库存 DispatchPermit")
        operation, request_values = self._material_io_request_values(
            job,
            job.get("param") or {},
        )
        permit = {
            "effect_uuid": str(job.get("dispatch_effect_uuid") or ""),
            "claim_uuid": str(claim["claim_uuid"]),
            "task_uuid": str(job["workflow_task_uuid"]),
            "job_uuid": job_uuid,
            "attempt": int(job["attempt"]),
            "parameter_hash": str(job.get("dispatch_parameter_hash") or ""),
            "expected_change_set": dict(job.get("expected_change_set") or {}),
            "fences": {
                str(fence["lock_key"]): int(fence["fencing_token"])
                for fence in claim["fences"]
            },
        }
        try:
            if operation == "load":
                result = service.settle_load(permit=permit, **request_values)
            elif operation == "unload":
                result = service.settle_unload(permit=permit, **request_values)
            else:
                result = service.settle_refill(permit=permit, **request_values)
        except MaterialIOError as error:
            self._scheduler.on_job_finished(
                job_uuid,
                False,
                {"code": error.code, "message": error.message},
                "material_io_failed",
            )
            return
        self._scheduler.on_job_finished(
            job_uuid,
            True,
            result,
            "material_io",
        )

    def _on_job_execution_wait(self, waiting: Mapping[str, Any]) -> None:
        """把调度循环先命中的内存占用投影为持久等待事实。"""

        job_uuid = str(waiting.get("job_id") or "")
        task_uuid = self._task_by_job.get(job_uuid)
        if task_uuid is None:
            return
        dispatch_task_uuid = self._required_text(
            waiting.get("workflow_id"), field="waiting.workflow_id"
        )
        if dispatch_task_uuid != task_uuid:
            raise StoreConflict(f"等待作业与任务身份不一致：{job_uuid}")
        execution_locks = waiting.get("execution_locks")
        if not isinstance(execution_locks, list):
            raise StoreConflict(f"等待作业缺少执行锁集合：{job_uuid}")
        candidate_site_uuids = waiting.get("candidate_site_uuids")
        if candidate_site_uuids is not None and not isinstance(
            candidate_site_uuids, list
        ):
            raise StoreConflict(f"等待作业候选库位集合不是数组：{job_uuid}")
        raw_wait_resources = waiting.get("wait_resources")
        if raw_wait_resources is not None and not isinstance(raw_wait_resources, list):
            raise StoreConflict(f"等待作业资源集合不是数组：{job_uuid}")
        # Scheduler 为诊断会附带 local_device_id、device_name、wait_code 等字段；
        # 持久化的工作流等待合同只保存资源范围和稳定身份，避免内部诊断字段
        # 直接穿透到严格的 WorkflowTask 投影并把正常等待误判为提交失败。
        wait_resources: list[Any] = []
        for resource in raw_wait_resources or []:
            if not isinstance(resource, Mapping):
                # 保留非法元素，让持久化投影按既有合同拒绝，而不是静默丢弃。
                wait_resources.append(resource)
                continue
            wait_resources.append(
                {
                    key: str(resource[key]).strip()
                    for key in (
                        "scope",
                        "lock_key",
                        "device_id",
                        "material_uuid",
                        "site_uuid",
                    )
                    if key in resource and resource[key] not in (None, "")
                }
            )
        wait_resources.extend(
            {
                "scope": "material_site",
                "site_uuid": self._required_text(
                    site_uuid,
                    field="waiting.candidate_site_uuids",
                ),
            }
            for site_uuid in candidate_site_uuids or []
        )
        known_resource_identities = {
            identity
            for resource in wait_resources
            if (identity := wait_resource_identity(resource)) is not None
        }
        for execution_lock in execution_locks:
            if not isinstance(execution_lock, Mapping):
                raise StoreConflict(f"等待作业执行锁必须是对象：{job_uuid}")
            resource = wait_resource_from_execution_lock(execution_lock)
            identity = (
                wait_resource_identity(resource)
                if resource is not None
                else None
            )
            if resource is not None and identity not in known_resource_identities:
                wait_resources.append(resource)
                if identity is not None:
                    known_resource_identities.add(identity)
        inventory = self._scheduler.station_resource_inventory
        if inventory is not None:
            wait_resources = [
                dict(resource)
                for resource in inventory.describe_wait_resources(wait_resources)
            ]
        self._projection.project_execution_lock_wait(
            task_uuid=task_uuid,
            job_uuid=job_uuid,
            execution_locks=execution_locks,
            blocking_task_uuid=(
                str(waiting["blocking_workflow_id"])
                if waiting.get("blocking_workflow_id")
                else None
            ),
            blocking_job_uuid=(
                str(waiting["blocking_job_id"])
                if waiting.get("blocking_job_id")
                else None
            ),
            wait_code=(str(waiting["wait_code"]) if waiting.get("wait_code") else None),
            wait_message=(
                str(waiting["wait_message"]) if waiting.get("wait_message") else None
            ),
            wait_resources=wait_resources,
            max_active_tasks=self._max_active_tasks,
            max_tasks_per_workflow=self._max_tasks_per_workflow,
            include_aggregate=False,
        )

    def _on_job_dispatch_accepted(self, job_uuid: str) -> None:
        """把库存 Claim 与工作流投影一起推进为 running。

        参数：``job_uuid`` 是执行适配器已明确接受的作业。返回：无。异常：库存
        Claim 缺失或任一持久转换失败时原样传播，调用方会转入不确定对账。
        """

        if job_uuid not in self._task_by_job:
            return
        if self._store.get_job(job_uuid)["status"] not in {"dispatched", "running", "cancel_requested"}:
            return
        self._transition_inventory_claim(job_uuid, target_state="running")
        self._projection.project_dispatch_accepted(job_uuid)

    def _on_manual_continuation_dispatching(self, job_uuid: str) -> None:
        """在人工批准后复验双库 Permit，再提交同一 Job 的派发意图。"""

        if self._store.evidence_only():
            raise StoreConflict("灾备模式禁止人工继续派发，执行资格尚未核实")
        if job_uuid not in self._task_by_job:
            raise StoreConflict(f"人工确认继续作业不属于当前运行：{job_uuid}")
        if self._store.execution_held(self._task_by_job[job_uuid]):
            raise StoreConflict("原任务处于灾备隔离，禁止人工继续物理派发")
        job = self._store.get_job(job_uuid)
        claim = self._projection.require_dispatchable_execution_claim(job_uuid)
        inventory_authority = self._scheduler.station_resource_inventory
        if inventory_authority is None:
            if self._scheduler.physical_dispatch_enabled:
                raise StoreConflict("人工确认物理派发缺少库存 Permit 权威")
        else:
            validator = getattr(
                inventory_authority,
                "validate_active_dispatch_permit",
                None,
            )
            if not callable(validator):
                raise StoreConflict("人工确认物理派发缺少库存 Permit 复验能力")
            validator(
                effect_uuid=str(job.get("dispatch_effect_uuid") or ""),
                claim_uuid=str(claim["claim_uuid"]),
                task_uuid=str(job["workflow_task_uuid"]),
                job_uuid=job_uuid,
                attempt=int(job["attempt"]),
                parameter_hash=str(job.get("dispatch_parameter_hash") or ""),
                expected_change_set=job.get("expected_change_set", {}),
                resource_keys=claim["resource_keys"],
                fences={
                    str(fence["lock_key"]): int(fence["fencing_token"])
                    for fence in claim["fences"]
                },
            )
        self._projection.project_manual_continuation_dispatching(
            job_uuid,
            dispatched_at=self._format_time(self._clock()),
        )

    def _on_job_dispatch_uncertain(self, job_uuid: str, reason: str) -> None:
        """让库存与工作流 Claim 同时冻结为物理不确定。

        参数：``job_uuid`` 是派发接受结果不明的作业；``reason`` 是稳定原因。
        返回：无。异常：库存或工作流持久化失败原样传播，不能释放任何资源。
        """

        if job_uuid not in self._task_by_job:
            return
        self._transition_inventory_claim(job_uuid, target_state="uncertain")
        self._projection.project_execution_attention(job_uuid, reason=reason)
        self.error_control.report_error(job_uuid, phase="dispatch", message=reason)
        self._notify_attention_changed()

    def _on_job_cancel_accepted(self, job_uuid: str) -> None:
        """记录本地执行器已受理取消并切换到设备终态截止时间。

        参数：``job_uuid`` 是在途作业身份。返回无。异常：持久投影冲突传播给
        执行边界，防止受理事实被静默丢失。终态先于受理回调到达时幂等忽略。
        """

        if job_uuid not in self._task_by_job:
            return
        job = self._store.get_job(job_uuid)
        if job.get("status") in {"succeeded", "failed", "canceled", "timeout"}:
            self._cancel_cancel_timer(job_uuid)
            return
        aggregate = self._projection.project_cancel_accepted(job_uuid)
        accepted_job = next(
            job for job in aggregate["jobs"] if str(job.get("uuid")) == job_uuid
        )
        self._schedule_cancel_timeout(accepted_job)

    def _on_job_cancel_uncertain(self, job_uuid: str, reason: str) -> None:
        """让取消拒绝或能力缺失的作业保持运行并等待物理对账。"""

        with self._scheduler.execution_control_lock:
            if job_uuid not in self._task_by_job:
                return
            self._cancel_cancel_timer(job_uuid)
            job = self._store.get_job(job_uuid)
            if job.get("status") in {"succeeded", "failed", "canceled", "timeout"}:
                return
            self.error_control.report_error(
                job_uuid,
                phase="cancel_unknown",
                message=reason,
                event_key=f"cancel_unknown:{job.get('cancel_command_uuid') or 'unassigned'}",
            )
            self._transition_inventory_claim(job_uuid, target_state="uncertain")
            self._projection.project_execution_attention(job_uuid, reason=reason)
            self._notify_attention_changed()

    def _on_job_cancel_no_send(self, job_uuid: str) -> None:
        """用 No-send Proof 立即结算一个尚未执行的取消作业。"""

        if job_uuid not in self._task_by_job:
            return
        self._cancel_cancel_timer(job_uuid)
        job = self._store.get_job(job_uuid)
        if job.get("status") != "cancel_requested":
            return
        if str(job.get("uncertainty_reason") or "").strip():
            # 已收到过结果未知的作业不可能“未发送”：保留占用与预留，等待对账或
            # 正式错误处置，不能按未发送证明释放。
            logger.warning("忽略结果未知作业的未发送取消证明：%s", job_uuid)
            return
        task_uuid = self._required_text(
            job.get("workflow_task_uuid"),
            field="job.workflow_task_uuid",
        )
        self._project_job_result(
            task_uuid=task_uuid,
            job_uuid=job_uuid,
            scheduler_state="canceled",
            return_info={"cancel_reason": "local_no_send_proof"},
            error_info=[],
            manual_confirmation_status=None,
        )

    def _transition_inventory_claim(
        self,
        job_uuid: str,
        *,
        target_state: str,
    ) -> None:
        """按工作流审计 Claim 身份推进库存权威生命周期。

        参数：``job_uuid`` 是工作流节点作业；``target_state`` 是库存 Claim 目标
        状态。返回：无。异常：物理部署缺库存权威、Claim 缺失或转换非法时抛
        ``StoreConflict``/库存异常；干跑模式没有库存 Claim 时直接返回。
        """

        inventory_authority = self._scheduler.station_resource_inventory
        claim = self._projection.get_execution_claim(job_uuid)
        if self.error_control.is_manual(job_uuid):
            return
        if inventory_authority is None:
            if self._scheduler.physical_dispatch_enabled:
                raise StoreConflict("物理作业缺少库存 DispatchPermit 权威")
            return
        if claim is None:
            raise StoreConflict(f"作业缺少库存 Claim 投影：{job_uuid}")
        inventory_authority.transition_dispatch_permit(
            str(claim["claim_uuid"]),
            target_state=target_state,
        )

    def _schedule_cancel_timeout(self, job: Mapping[str, Any]) -> None:
        """按持久截止时间为一个取消中作业安排单次本地检查。

        参数：``job`` 是最新持久作业投影。返回无。异常：缺少或损坏截止时间时
        立即让主状态恢复为 running 并进入物理对账，避免取消状态永久悬挂。受理前使用 ACK 截止时间，受理后
        使用设备完成截止时间。
        """

        job_uuid = self._required_text(job.get("uuid"), field="job.uuid")
        accepted = bool(job.get("cancel_accepted_at"))
        field = "cancel_complete_deadline_at" if accepted else "cancel_ack_deadline_at"
        raw_deadline = job.get(field)
        try:
            deadline = self._parse_time(raw_deadline)
        except (TypeError, ValueError):
            self._on_job_cancel_uncertain(
                job_uuid,
                f"invalid_{field}",
            )
            return
        delay = max(
            0.0,
            (deadline - self._clock()).total_seconds(),
        )
        timer = self._timer_factory(
            delay,
            self._on_cancel_timeout,
            kwargs={
                "job_uuid": job_uuid,
                "expected_accepted": accepted,
            },
        )
        timer.daemon = True
        with self._cancel_timer_lock:
            previous = self._cancel_timers.pop(job_uuid, None)
            self._cancel_timers[job_uuid] = timer
        if previous is not None:
            previous.cancel()
        timer.start()

    def _on_cancel_timeout(
        self,
        *,
        job_uuid: str,
        expected_accepted: bool,
    ) -> None:
        """把到期且仍未收敛的取消作业冻结为运行中物理不确定事实。

        参数：``job_uuid`` 是作业身份；``expected_accepted`` 标识本计时器观察的是
        受理阶段还是设备终态阶段。返回无。异常：存储关闭或并发终态只记录日志，
        不释放任何执行占用。
        """

        with self._cancel_timer_lock:
            self._cancel_timers.pop(job_uuid, None)
        if self._closed:
            return
        try:
            with self._scheduler.execution_control_lock:
                if self._closed:
                    return
                job = self._store.get_job(job_uuid)
                if job.get("status") != "cancel_requested":
                    return
                accepted = bool(job.get("cancel_accepted_at"))
                if accepted != expected_accepted:
                    return
                reason = (
                    "local_cancel_completion_timeout"
                    if accepted
                    else "local_cancel_acceptance_timeout"
                )
                self._on_job_cancel_uncertain(job_uuid, reason)
        except Exception:
            logger.exception("本地取消超时检查失败：%s", job_uuid)

    def _cancel_cancel_timer(self, job_uuid: str) -> None:
        """幂等取消一个作业的本地截止时间计时器。"""

        with self._cancel_timer_lock:
            timer = self._cancel_timers.pop(job_uuid, None)
        if timer is not None:
            timer.cancel()

    def _schedule_manual_confirmation_timeout(self, _job_uuid: str = "") -> None:
        """只为全库最早截止时间保留一个可唤醒计时器。"""

        with self._manual_timer_lock:
            previous = self._manual_deadline_timer
            self._manual_deadline_timer = None
            if previous is not None:
                previous.cancel()
            confirmation = self._manual_confirmations.next_pending_deadline()
            if confirmation is None or self._closed:
                return
            deadline = self._parse_time(confirmation["deadline_at"])
            delay = max(0.0, (deadline - self._clock()).total_seconds())
            timer = self._timer_factory(
                delay,
                self._on_manual_confirmation_timeout,
            )
            timer.daemon = True
            self._manual_deadline_timer = timer
            timer.start()

    def _on_manual_confirmation_timeout(self) -> None:
        """批量收敛当前所有到期确认，再安排下一条权威截止时间。"""

        with self._manual_callback_lock:
            with self._manual_timer_lock:
                self._manual_deadline_timer = None
            if self._closed:
                return
            now = self._format_time(self._clock())
            for confirmation in self._manual_confirmations.list_due(now):
                job_uuid = str(confirmation["workflow_node_job_uuid"])
                try:
                    aggregate, created = (
                        self._projection.project_manual_confirmation_timeout(
                            job_uuid,
                            decided_at=now,
                        )
                    )
                    if created:
                        self._notify_attention_changed()
                        self.cancel(
                            str(aggregate["task"]["uuid"]),
                            reason="manual_confirmation_timeout",
                        )
                except (StoreConflict, TaskSchedulerBridgeError):
                    continue
                except Exception:
                    logger.exception("人工确认超时收敛失败：%s", job_uuid)
            self._schedule_manual_confirmation_timeout()

    def _cancel_manual_confirmation_timer(self, job_uuid: str) -> None:
        """决定变更后重排唯一人工确认截止计时器。"""

        del job_uuid
        self._schedule_manual_confirmation_timeout()

    @staticmethod
    def _parse_time(value: Any) -> datetime:
        """解析持久 UTC 时间；非法或无时区输入抛 ``ValueError``。"""

        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("截止时间缺少时区")
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _format_time(value: datetime) -> str:
        """把带时区时间规范为本地工作流库使用的 UTC RFC3339 文本。"""

        if value.tzinfo is None:
            raise ValueError("截止时间缺少时区")
        return (
            value.astimezone(timezone.utc)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )

    def _on_job_feedback(self, job_uuid: str, sample: Mapping[str, Any]) -> None:
        """把 Edge 已提交反馈投影为工作流作业有序历史。"""

        # 反馈可能在进程重启后的重放阶段到达；持久作业表才是归属权威，不能用
        # 进程内 ``_task_by_job`` 过滤，否则恢复中的合法反馈会被静默丢弃。
        self._projection.project_feedback(
            job_uuid=job_uuid,
            sequence=sample.get("sequence"),
            feedback_type=sample.get("feedback_type"),
            data=sample.get("data") or {},
            observed_at=sample.get("observed_at"),
            idempotency_key=sample.get("idempotency_key"),
        )

    def _execute_native_command(self, event: Mapping[str, Any], task_uuid: str) -> dict[str, Any]:
        """通过框架命令模块复用 OS 库存权威。"""
        from unilabos.workflow.inventory_commands import execute_inventory_command

        return execute_inventory_command(
            self._store, self._scheduler.inventory_service, event, task_uuid=task_uuid
        )

    def _schedule_human_timeout(self, request: Mapping[str, Any]) -> None:
        """依据持久截止时间恢复等待；暂停门禁不影响人工请求超时。"""
        job_uuid = request["job_uuid"]
        if job_uuid in self._human_timers:
            return
        deadline = self._parse_time(request["deadline_at"])
        if deadline <= self._clock():
            if self.human_interactions.expire(request["uuid"], now=self._clock()):
                self._notify_attention_changed()
            return

        def wake() -> None:
            if self._closed:
                return
            try:
                with self._scheduler.execution_control_lock:
                    if self._closed:
                        return
                    self._human_timers.pop(job_uuid, None)
                    changed = self.human_interactions.expire(request["uuid"], now=self._clock())
                    if not changed and self._clock() < deadline:
                        self._schedule_human_timeout(request)
                if changed:
                    self._notify_attention_changed()
                self._scheduler._wake_reconcile(wait=False)
            except Exception:
                logger.exception("人工交互超时收口失败")

        timer = self._timer_factory(max(0.01, (deadline - self._clock()).total_seconds()), wake)
        timer.daemon = True
        self._human_timers[job_uuid] = timer
        timer.start()

    def _restore_human_timeouts(self) -> None:
        """独立恢复人工交互计时器，包括暂停任务及已经过期的请求。"""
        from unilabos.workflow.human_commands import pending_human_interactions

        with self._scheduler.execution_control_lock:
            with self._store.read() as connection:
                requests = pending_human_interactions(connection)
            for request in requests:
                self._schedule_human_timeout(request)

    def reply_human_interaction(self, identity: str, result: dict[str, Any]) -> dict[str, Any]:
        # Serialize reply with cancellation and dispatch, then wake outside that lock.
        with self._scheduler.execution_control_lock:
            request = self.human_interactions.reply(identity, result, now=self._clock())
        self._notify_attention_changed()
        self._scheduler._wake_reconcile(wait=False)
        return request

    def _on_local_control_evaluated(
        self, event: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        """把条件选择与未选分支作为无物理副作用的标准作业事实落盘。"""

        job_uuid = self._required_text(event.get("job_id"), field="control.job_id")
        task_uuid = self._task_by_job.get(job_uuid)
        if task_uuid is None:
            return
        if event.get("control_type") == "material_source" and not event.get("error"):
            try:
                task = self._store.get_task(task_uuid)
                resolution = self._material_sources.reconcile(
                    task, self._store.list_jobs(task_uuid),
                    runtime_node_uuid=str(event["node_id"]),
                    quantity_inventory=self._quantity_inventory,
                )
                if resolution.status == "blocked":
                    # 物料被其他任务占用：来源作业保持等待并写明原因，释放后下一轮重评。
                    # 原因只写一次；重复写会追加失效事件、立刻再唤醒重排而空转。
                    current_reason = self._store.get_job(job_uuid).get("wait_reason") or {}
                    pending = {**event, "pending": True, "serial_blocking_workflow_id": resolution.blocking_workflow_id}
                    if isinstance(current_reason, Mapping) and current_reason.get("code") == "task_material_claimed":
                        return pending
                    self._projection.project_execution_lock_wait(
                        task_uuid=task_uuid,
                        job_uuid=job_uuid,
                        execution_locks=[],
                        wait_code="task_material_claimed",
                        wait_message=resolution.reason or "物料正被其他任务占用，等待释放",
                        max_active_tasks=self._max_active_tasks,
                        max_tasks_per_workflow=self._max_tasks_per_workflow,
                    )
                    return pending
                result = self._store.get_job(job_uuid)["return_info"]
                self._task_by_job.pop(job_uuid, None)
                return {**event, "result": result}
            except Exception as error:
                self.error_control.report_error(job_uuid, phase="prepare", message=str(error))
                return {**event, "error": "material_source_check_failed", "message": str(error)}
        if event.get("error"):
            self.error_control.report_error(job_uuid, phase="prepare", message=str(event.get("message") or event["error"]))
            return dict(event)
        # 条件和循环回调发生在设备动作之前，也可能在投影事务中失败；保留
        # 该阶段让 submit 的失败收敛与设备派发回调一致。
        self._mark_submission_phase(task_uuid, _SUBMISSION_PHASE_LOCAL_CONTROL)
        if event.get("control_type") == "workflow_command":
            try:
                from unilabos.workflow.human_commands import PREFIX
                if str(event.get("command_name") or "").startswith(PREFIX):
                    request = self.human_interactions.evaluate(event, task_uuid, now=self._clock())
                    # 请求或超时已经提交，随后再唤醒 WSS 读取快照。
                    self._notify_attention_changed()
                    if request["status"] == "pending":
                        self._schedule_human_timeout(request)
                        return {**event, "pending": True}
                    timer = self._human_timers.pop(job_uuid, None)
                    if timer is not None:
                        timer.cancel()
                    if request["status"] != "answered":
                        raise ValueError(f"人工交互已结束：{request['status']}")
                    result = request["result"]
                else:
                    result = self._execute_native_command(event, task_uuid)
            except (ValueError, KeyError, RuntimeError) as error:
                self.error_control.report_error(job_uuid, phase="prepare", message=str(error))
                return {**event, "error": "local_command_failed", "message": str(error)}
            except Exception as error:
                from unilabos.app.scheduler.inventory.domain import InventoryError
                if not isinstance(error, InventoryError):
                    raise
                self.error_control.report_error(job_uuid, phase="prepare", message=str(error))
                return {**event, "error": error.code, "message": str(error)}
            aggregate = self._projection.project_local_control_evaluation(
                job_uuid=job_uuid, selected_branch=None, skipped_job_uuids=[], local_command_result=result,
            )
            self._reconcile_inventory_resource_intervals(task_uuid=task_uuid, aggregate=aggregate)
            self._task_by_job.pop(job_uuid, None)
            self._settle_local_control_task(task_uuid, aggregate)
            return {**event, "result": result}
        if event.get("control_type") == "repeat_until":
            phase = str(event.get("phase") or "")
            iteration_index = event.get("iteration_index")
            if isinstance(iteration_index, bool) or not isinstance(
                iteration_index, int
            ):
                raise TaskSchedulerBridgeError("循环控制缺少合法轮次索引")
            if phase == "materialize":
                raw_jobs = event.get("iteration_jobs")
                if not isinstance(raw_jobs, list) or any(
                    not isinstance(item, Mapping) for item in raw_jobs
                ):
                    raise TaskSchedulerBridgeError("循环轮次作业结构非法")
                try:
                    self._projection.materialize_repeat_iteration(
                        control_job_uuid=job_uuid,
                        iteration_index=iteration_index,
                        control_path=self._required_text(
                            event.get("node_id"), field="control.node_id"
                        ),
                        jobs=raw_jobs,
                    )
                except StoreConflict as error:
                    self.error_control.report_error(job_uuid, phase="prepare", message=str(error))
                    return {**event, "error": "repeat_iteration_materialization_failed", "message": str(error)}
                for item in raw_jobs:
                    iteration_job_uuid = self._required_text(
                        item.get("job_uuid"), field="control.iteration_jobs[].job_uuid"
                    )
                    self._task_by_job[iteration_job_uuid] = task_uuid
                return
            if phase != "evaluate":
                raise TaskSchedulerBridgeError("循环控制阶段非法")
            carry = event.get("carry")
            next_carry = event.get("next_carry")
            if not isinstance(carry, Mapping) or (
                next_carry is not None and not isinstance(next_carry, Mapping)
            ):
                raise TaskSchedulerBridgeError("循环控制 carry 结构非法")
            raw_skipped_jobs = event.get("skipped_jobs", [])
            if not isinstance(raw_skipped_jobs, list) or any(
                not isinstance(item, Mapping) for item in raw_skipped_jobs
            ):
                raise TaskSchedulerBridgeError("循环结算 skipped_jobs 结构非法")
            aggregate = self._projection.project_repeat_evaluation(
                control_job_uuid=job_uuid,
                iteration_index=iteration_index,
                condition_result=(
                    event.get("condition_result")
                    if type(event.get("condition_result")) is bool
                    else None
                ),
                carry=carry,
                next_carry=next_carry,
                error_code=(str(event["error"]) if event.get("error") else None),
                error_message=(str(event["message"]) if event.get("message") else None),
                skipped_job_uuids=[
                    self._required_text(
                        item.get("job_id"),
                        field="control.skipped_jobs[].job_id",
                    )
                    for item in raw_skipped_jobs
                ],
            )
            self._reconcile_inventory_resource_intervals(
                task_uuid=task_uuid,
                aggregate=aggregate,
            )
            if event.get("error") or event.get("condition_result") is True:
                self._task_by_job.pop(job_uuid, None)
            return
        skipped_jobs = event.get("skipped_jobs")
        if not isinstance(skipped_jobs, list):
            raise TaskSchedulerBridgeError("条件结算缺少 skipped_jobs")
        skipped_job_uuids = [
            self._required_text(
                item.get("job_id"),
                field="control.skipped_jobs[].job_id",
            )
            for item in skipped_jobs
            if isinstance(item, Mapping)
        ]
        if len(skipped_job_uuids) != len(skipped_jobs):
            raise TaskSchedulerBridgeError("条件结算 skipped_jobs 结构非法")
        aggregate = self._projection.project_local_control_evaluation(
            job_uuid=job_uuid,
            selected_branch=(
                str(event["selected_branch"])
                if event.get("selected_branch") is not None
                else None
            ),
            skipped_job_uuids=skipped_job_uuids,
            error_code=(str(event["error"]) if event.get("error") else None),
            error_message=(str(event["message"]) if event.get("message") else None),
        )
        self._reconcile_inventory_resource_intervals(
            task_uuid=task_uuid,
            aggregate=aggregate,
        )
        self._task_by_job.pop(job_uuid, None)
        for skipped_uuid in skipped_job_uuids:
            self._task_by_job.pop(skipped_uuid, None)
        self._settle_local_control_task(task_uuid, aggregate)

    def _settle_local_control_task(self, task_uuid: str, aggregate: Mapping[str, Any]) -> None:
        """本地节点终结任务时沿用统一的预留释放和失败占用规则。"""
        terminal_status = aggregate["task"]["status"]
        if terminal_status == "succeeded":
            if self._quantity_inventory is not None:
                self._quantity_inventory.release_task(
                    task_uuid,
                    reason="workflow_succeeded",
                )
            if any(
                job.get("executor_kind") == "material_source"
                for job in aggregate["jobs"]
            ):
                self._material_sources.release_terminal_reservations(
                    task_uuid,
                    reason="workflow_succeeded",
                )
            release = getattr(self._scheduler.inventory_service, "release_workflow", None)
            if callable(release):
                release(task_uuid, reason="workflow_succeeded")
        elif terminal_status == "failed":
            from unilabos.workflow.resource_lock_plan import (
                failed_explicit_resource_interval_ids,
            )

            failure_latched = bool(
                failed_explicit_resource_interval_ids(
                    aggregate["task"].get("execution_plan", {}),
                    aggregate["jobs"],
                )
            )
            scheduler_snapshot = self._scheduler.snapshot()
            active_for_task = any(
                inflight.get("workflow_id") == task_uuid
                for inflight in scheduler_snapshot.get("inflight_jobs", {}).values()
            )
            if not active_for_task and not failure_latched:
                if self._quantity_inventory is not None:
                    self._quantity_inventory.release_task(
                        task_uuid,
                        reason="workflow_failed",
                    )
                if any(
                    job.get("executor_kind") == "material_source"
                    for job in aggregate["jobs"]
                ):
                    self._material_sources.release_terminal_reservations(
                        task_uuid,
                        reason="workflow_failed",
                    )
                self._projection.project_cleanup_settled(task_uuid)
        if task_uuid not in self._task_by_job.values():
            self._submitted_tasks.discard(task_uuid)

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
        persisted_job = self._store.get_job(job_uuid)
        if self._was_aborted_by_runtime_restart(persisted_job):
            logger.info("忽略 runtime 重启后迟到的旧式作业完成通知：%s", job_uuid)
            return
        # ``return_info`` 保持标准对象字段；标量结果使用明确包装键。
        return_info = (
            dict(ret_value)
            if isinstance(ret_value, Mapping)
            else ({"return_value": ret_value} if ret_value is not None else {})
        )
        # ``error_info`` 只在明确失败时记录稳定代码与人工决策来源。
        error_info: list[dict[str, Any]] = []
        canceled = not success and suc_type == "canceled"
        if not success and not canceled and suc_type == "material_io_failed" and isinstance(ret_value, Mapping):
            # material_io 不派发设备；直接展示库存拒绝的具体原因，避免误导操作员。
            error_info = [
                {
                    "code": str(ret_value.get("code") or "material_io_failed"),
                    "message": str(ret_value.get("message") or "物料上下料结算失败"),
                    "suc_type": suc_type,
                }
            ]
        elif not success and not canceled:
            error_info = [
                {
                    "code": "legacy_edge_scheduler_action_failed",
                    "message": "设备动作执行失败",
                    "suc_type": suc_type,
                }
            ]
        # 旧式完成回调同样必须结算：material_io、原生命令与公开 finish 接口都经过
        # 这里，跳过会让转运、分装和声明用量静默不入账。
        inventory_reconciliation = (
            self._settle_successful_job_inventory(
                task_uuid=task_uuid,
                job_uuid=job_uuid,
                aliquot_receipts=(),
                consumptions=(),
                outcome=None,
            )
            if success
            else None
        )
        self._project_job_result(
            task_uuid=task_uuid,
            job_uuid=job_uuid,
            scheduler_state=(
                "success" if success else ("canceled" if canceled else "failed")
            ),
            return_info=return_info,
            error_info=error_info,
            timing_sample_allowed=success and suc_type == "normal",
            manual_confirmation_status=(
                "timed_out" if suc_type == "manual_confirmation_timeout" else None
            ),
            inventory_reconciliation=inventory_reconciliation,
        )

    def _on_job_outcome(
        self,
        job_uuid: str,
        outcome: CommittedJobOutcome,
    ) -> None:
        """把 Edge HTTP 保真结果投影为标准任务/作业终态。

        参数：``job_uuid`` 是稳定作业身份；``outcome`` 保留 Backend wire 终态、
        返回值与错误证据。返回无。异常：身份路由或持久投影冲突向上传播；结果不会
        被降级成旧 ``success/suc_type`` 组合，也不会触发新的物理执行。
        """

        task_uuid = self._task_by_job.get(job_uuid)
        if task_uuid is None:
            # 重启恢复会先把已经越过物理边界的 Task 标为失败，因此它不再进入
            # 活动 DAG 路由表；设备停止证明仍可能在稍后到达。此时必须从持久
            # WorkflowNodeJob 恢复归属，不能静默丢弃证明并永久持有 Claim。
            try:
                persisted_route = self._store.get_job(job_uuid)
            except StoreNotFound:
                return
            task_uuid = str(
                persisted_route.get("workflow_task_uuid") or ""
            ).strip()
            if not task_uuid:
                return
        persisted_job = self._store.get_job(job_uuid)
        if self._was_aborted_by_runtime_restart(persisted_job):
            # runtime 重启已经为这次执行冻结了失败/取消事实，并释放了旧
            # Claim/Fence。旧执行进程随后到达的结果不再具备提交权，必须丢弃，
            # 否则会与重启事实形成不可变结果冲突，甚至错误结算物料。
            logger.info("忽略 runtime 重启后迟到的作业结果：%s", job_uuid)
            return
        if outcome.unknown_command_ids:
            self._transition_inventory_claim(job_uuid, target_state="uncertain")
            if not (self._time_scheduling_enabled and persisted_job.get("uncertainty_reason")):
                self._projection.project_execution_attention(
                    job_uuid,
                    reason=(
                        "edge_reported_unknown_commands:"
                        + ",".join(outcome.unknown_command_ids)
                    ),
                )
            self.error_control.report_error(job_uuid, phase="execution", message="执行结果未知：" + ",".join(outcome.unknown_command_ids))
            return
        if (
            persisted_job.get("status") == "failed"
            and str(persisted_job.get("uncertainty_reason") or "").strip()
        ):
            claim = self._projection.get_execution_claim(job_uuid)
            aggregate = self._projection.project_failed_job_execution_stopped(
                job_uuid,
                outcome=outcome.outcome,
                return_info=outcome.return_info,
                error_info=outcome.error_info,
            )
            settled_job = next(
                item for item in aggregate["jobs"] if item["uuid"] == job_uuid
            )
            if not settled_job.get("uncertainty_reason"):
                inventory = self._scheduler.station_resource_inventory
                if inventory is not None and claim is not None:
                    inventory.transition_dispatch_permit(
                        str(claim["claim_uuid"]),
                        target_state="released",
                    )
                elif self._scheduler.physical_dispatch_enabled:
                    raise StoreConflict(f"失败作业物理结算缺少库存 Claim：{job_uuid}")
            self._cancel_cancel_timer(job_uuid)
            self._cancel_manual_confirmation_timer(job_uuid)
            if not settled_job.get("uncertainty_reason"):
                self._finish_settled_terminal_task(job_uuid)
            return
        scheduler_state = {
            "succeeded": "success",
            "failed": "failed",
            "canceled": "canceled",
            "timeout": "timeout",
        }.get(outcome.outcome)
        if scheduler_state is None:
            raise StoreConflict(f"不支持的 Edge 作业终态：{outcome.outcome}")
        inventory_reconciliation = None
        if outcome.outcome == "succeeded":
            inventory_reconciliation = self._settle_successful_job_inventory(
                task_uuid=task_uuid,
                job_uuid=job_uuid,
                aliquot_receipts=outcome.material_aliquot_receipts,
                consumptions=outcome.inventory_consumptions,
                outcome=outcome,
            )
        self._project_job_result(
            task_uuid=task_uuid,
            job_uuid=job_uuid,
            scheduler_state=scheduler_state,
            return_info=outcome.return_info,
            error_info=outcome.error_info,
            manual_confirmation_status=None,
            inventory_reconciliation=inventory_reconciliation,
        )

    def _foreign_unfinished_use_of_unload(
        self,
        *,
        task_uuid: str,
        job_uuid: str,
        resolved_args: Mapping[str, Any],
    ) -> tuple[str, str] | None:
        """返回仍要使用待卸物料（含子物料）的其他任务未完成作业。

        只看对方作业参数里是否引用这些物料；对方同样只是卸下这件物料的作业不算
        使用，避免两个任务都只想卸废盒时互相等待。非卸料作业直接返回 ``None``。
        """

        if str(resolved_args.get("mode") or "") != "unload":
            return None
        material = resolved_args.get("material")
        material_uuid = str(
            (material.get("uuid") if isinstance(material, Mapping) else material) or ""
        ).strip()
        material_io = getattr(self._scheduler.inventory_service, "material_io", None)
        if not material_uuid or material_io is None:
            return None
        if self._store.get_job(job_uuid).get("executor_kind") != "material_io":
            return None
        identities = material_io.material_subtree_uuids(material_uuid)
        if not identities:
            return None
        clauses = " OR ".join("j.param LIKE ?" for _ in identities)
        with self._store.read() as connection:
            rows = connection.execute(
                f"""SELECT j.uuid, j.workflow_task_uuid, j.executor_kind, j.param
                FROM workflow_node_job AS j
                JOIN workflow_task AS t ON t.uuid = j.workflow_task_uuid
                WHERE j.deleted_at IS NULL AND t.deleted_at IS NULL
                  AND j.workflow_task_uuid <> ?
                  AND j.status IN ('pending', 'dispatched', 'running', 'cancel_requested')
                  AND t.status IN ('pending', 'running', 'canceling')
                  AND ({clauses})
                ORDER BY j.create_time, j.uuid""",
                (task_uuid, *(f"%{identity}%" for identity in identities)),
            ).fetchall()
        for row in rows:
            if row["executor_kind"] == "material_io":
                try:
                    param = json.loads(row["param"] or "{}")
                except ValueError:
                    param = {}
                if isinstance(param, Mapping) and param.get("mode") == "unload":
                    continue
            return str(row["workflow_task_uuid"]), str(row["uuid"])
        if getattr(self._scheduler, "serial_queue_enabled", False):
            from unilabos.app.scheduler.inventory.source_preflight import material_source_checks

            for queued in self._store.list_recoverable_tasks(statuses=("pending",)):
                queued_uuid = str(queued["uuid"])
                if queued_uuid == task_uuid or queued_uuid not in self._admission_pending_tasks:
                    continue
                # 后排来源尚无绑定，复用只读试分配识别卸料依赖，不提前占用库存。
                checks = material_source_checks(
                    self._scheduler.inventory_service, queued["execution_plan"], task_uuid=queued_uuid
                )
                if any(check["blocking"] for check in checks):
                    continue
                source_ids = {
                    check["node_uuid"] for check in checks
                    if check["code"] == "material_source_ready"
                    and check["details"].get("material_uuid") in identities
                }
                for job in self._store.list_jobs(queued_uuid) if source_ids else ():
                    if job["status"] == "pending" and job["workflow_node_uuid"] in source_ids:
                        return queued_uuid, str(job["uuid"])
        return None

    def _settle_successful_job_inventory(
        self,
        *,
        task_uuid: str,
        job_uuid: str,
        aliquot_receipts: Sequence[Mapping[str, Any]],
        consumptions: Sequence[Mapping[str, Any]],
        outcome: CommittedJobOutcome | None,
    ) -> Mapping[str, Any] | None:
        """在成功终态落库前结算物料转运、分装与声明的数量消费。

        Edge 保真回执与旧式完成回调共用这一处，任何成功通道都不能跳过库存结算。
        参数：任务/作业身份、分装回执、实际用量（为空时按预留量结算）以及可选的
        原始回执。返回：库存事实冲突时的对账详情，否则 ``None``。异常：结算失败
        原样传播；数量结算失败时先冻结为数量对账再抛出。
        """

        inventory_reconciliation = None
        task = self._store.get_task(task_uuid)
        claim = self._projection.get_execution_claim(job_uuid)
        try:
            self._material_transfer_settlement.settle_success(
                job=self._store.get_job(job_uuid),
                execution_plan=(
                    task.get("execution_plan")
                    if isinstance(task.get("execution_plan"), Mapping)
                    else None
                ),
                execution_claim=claim,
            )
        except InventoryFactConflict as error:
            inventory_reconciliation = error.details
        self._material_aliquot_settlement.settle_success(
            job=self._store.get_job(job_uuid),
            execution_claim=claim,
            receipts=aliquot_receipts,
        )
        if self._quantity_inventory is not None:
            reserve_deferred = getattr(
                self._quantity_inventory, "reserve_deferred_runtime_requirements", None
            )
            try:
                if callable(reserve_deferred):
                    reserve_deferred(task_uuid=task_uuid, job_uuid=job_uuid)
            except StoreConflict as error:
                # 补料分支的消费容器无法确定：冻结为数量对账，不能静默不扣量。
                self._mark_quantity_reconciliation(
                    job_uuid, reason=str(error), outcome=outcome
                )
                raise
            try:
                self._quantity_inventory.consume_successful_job(
                    task_uuid=task_uuid,
                    job_uuid=job_uuid,
                    consumptions=consumptions,
                )
            except (ValueError, StoreConflict, sqlite3.IntegrityError) as error:
                if self._pending_quantity_consumption(task_uuid):
                    self._mark_quantity_reconciliation(
                        job_uuid, reason=str(error), outcome=outcome
                    )
                raise
        return inventory_reconciliation

    def _prepare_inventory_release(self, task_uuid: str) -> bool:
        """成功产物先取得独立库存保护，再释放旧 Task 的其余来源与设备资源。"""
        inventory = self._scheduler.inventory_service
        inventory_store = getattr(inventory, "store", None)
        if inventory_store is None:
            return True
        try:
            task = self._store.get_task(task_uuid)
        except StoreNotFound:
            from unilabos.persistence.inspection import table_exists
            # 通用库存也服务于未创建本地 Task 的外部预留；不能误把其释放当成 Task 收尾。
            # 已知（含软删）Task、遗留 Job 或中心绑定仍失败关闭，不能按外部预留放行。
            with self._store.read() as c:
                if c.execute("SELECT 1 FROM workflow_task WHERE uuid=?", (task_uuid,)).fetchone() or c.execute(
                    "SELECT 1 FROM workflow_node_job WHERE workflow_task_uuid=? LIMIT 1", (task_uuid,)
                ).fetchone():
                    return False
                if table_exists(c, "center_invocation_inbox") and c.execute(
                    "SELECT 1 FROM center_invocation_inbox WHERE local_task_uuid=?", (task_uuid,)
                ).fetchone():
                    return False
            return self._quantity_inventory is None or not any(
                item["task_uuid"] == task_uuid
                for item in self._quantity_inventory.pending_consumption_reconciliations()
            )
        if self._pending_quantity_consumption(task_uuid):
            return False
        if task["status"] not in {"succeeded", "failed", "canceled", "timeout"}:
            return False
        from unilabos.app.scheduler.inventory.transport_allocation import TransportAllocations
        TransportAllocations(inventory_store).hold(task_uuid, uncertain=task["status"] != "succeeded")
        if task["status"] != "succeeded":
            return True
        from unilabos.app.scheduler.inventory.outbound import admit_dynamic_job_outputs, protect_outputs
        cursor = ""
        while True:
            sources = self._store.list_center_material_exports(after_invocation_uuid=cursor)
            for source in sources:
                if source["local_task_uuid"] == task_uuid:
                    # 仅恢复仍等待原产物证据的出站责任。已经提交 outcome 或交接完成
                    # 的历史任务不能重新取得旧物料；普通本地任务也不创建出站准入。
                    for job in self._store.list_jobs(task_uuid):
                        if job.get("status") == "succeeded":
                            admit_dynamic_job_outputs(inventory, task=task, job=job, return_info=job.get("return_info") or {})
                    protect_outputs(inventory_store, invocation_uuid=source["invocation_uuid"],
                                    task_uuid=task_uuid, outputs=source["outputs"])
                    return True
            if len(sources) < 100:
                break
            cursor = sources[-1]["invocation_uuid"]
        # 已冻结证据表示保护已经建立；无合法结果的中心物料任务必须继续核实。
        contexts = self._store.center_material_attention_contexts()
        return not any(item["local_task_uuid"] == task_uuid and item.get("pending_reason") in
                       {"material_evidence_required", "material_output_invalid"} for item in contexts)

    def _pending_quantity_consumption(self, task_uuid: str) -> bool:
        """判断任务是否有尚未结算的实际用量，禁止任何收尾路径释放占用。"""

        return self._quantity_inventory is not None and (any(
            item["task_uuid"] == task_uuid
            for item in self._quantity_inventory.pending_consumption_reconciliations()
        ) or any(
            job.get("uncertainty_reason") == QUANTITY_RECONCILIATION_REQUIRED
            for job in self._store.list_jobs(task_uuid)
        ))

    def _mark_quantity_reconciliation(
        self,
        job_uuid: str,
        *,
        reason: str,
        outcome: CommittedJobOutcome | None = None,
    ) -> None:
        """冻结库存和工作流 Claim，保留原始成功结果供显式数量更正。"""

        self._projection.project_quantity_consumption_attention(
            job_uuid, reason=reason,
            outcome=outcome.as_dict() if outcome is not None else None,
        )
        if self._projection.get_execution_claim(job_uuid) is not None:
            self._transition_inventory_claim(job_uuid, target_state="uncertain")
        elif self._scheduler.physical_dispatch_enabled:
            raise StoreConflict(f"数量对账作业缺少库存 Claim：{job_uuid}")
        self._cancel_cancel_timer(job_uuid)

    @staticmethod
    def _requires_runtime_restart_cleanup(
        task: Mapping[str, Any],
        jobs: Sequence[Mapping[str, Any]],
    ) -> bool:
        """判断终态聚合是否仍属于 runtime restart 跨权威补偿。"""

        task_errors = task.get("error_info")
        task_was_aborted = isinstance(task_errors, list) and any(
            isinstance(item, Mapping)
            and item.get("code") == EXECUTION_PROCESS_RESTARTED
            for item in task_errors
        )
        return task_was_aborted or any(
            TaskSchedulerBridge._was_aborted_by_runtime_restart(job)
            for job in jobs
        )

    @staticmethod
    def _was_aborted_by_runtime_restart(job: Mapping[str, Any]) -> bool:
        """判断 Job 终态是否由 runtime 重启策略冻结。"""

        control_data = job.get("control_data")
        if isinstance(control_data, Mapping):
            interruption = control_data.get("runtime_interruption")
            if (
                isinstance(interruption, Mapping)
                and interruption.get("reason") == EXECUTION_PROCESS_RESTARTED
            ):
                return True
        error_info = job.get("error_info")
        if not isinstance(error_info, list):
            return False
        return any(
            isinstance(item, Mapping)
            and item.get("code")
            in {
                EXECUTION_PROCESS_RESTARTED,
                TASK_ABORTED_BY_RUNTIME_RESTART,
            }
            for item in error_info
        )

    def _project_job_result(
        self,
        *,
        task_uuid: str,
        job_uuid: str,
        scheduler_state: str,
        return_info: Mapping[str, Any],
        error_info: list[Any],
        manual_confirmation_status: str | None,
        inventory_reconciliation: Mapping[str, Any] | None = None,
        timing_sample_allowed: bool = True,
    ) -> None:
        """提交一次标准作业结果并完成桥接层终态收尾。

        参数：任务/作业身份、规范调度终态、返回对象、错误数组和可选人工确认
        关闭状态。返回无。异常：持久结果冲突或物料来源释放失败原样传播；计时器
        仅在结果提交成功后取消，相同投递依赖投影层幂等处理。
        """

        claim = self._projection.get_execution_claim(job_uuid)
        if scheduler_state in {"failed", "timeout"}:
            self.error_control.report_error(job_uuid, phase="execution", message=str(error_info or return_info))
        timing_sample = None
        if (self._time_scheduling_enabled and timing_sample_allowed
                and scheduler_state == "success" and not error_info
                and not inventory_reconciliation and manual_confirmation_status is None):
            timing_sample = self._scheduler.capture_timing_observation(job_uuid)
        if scheduler_state == "success" and self.error_control.awaits_decision(job_uuid):
            # 同一作业先报 UNKNOWN、后收到明确成功时，先关掉那条未知错误。
            # 否则成功投影会因 error_waiting 跳过任务终态，后继也无法继续。
            self.error_control.accept_proved_success(job_uuid)
        if scheduler_state == "success" and getattr(self._scheduler.inventory_service, "store", None) is not None:
            from unilabos.app.scheduler.inventory.outbound import admit_dynamic_job_outputs
            try:
                admit_dynamic_job_outputs(self._scheduler.inventory_service, task=self._store.get_task(task_uuid),
                                          job=self._store.get_job(job_uuid), return_info=dict(return_info))
            except ValueError as error:
                # 物理执行成功不因产物准入失败被改写；独立结果补报会保留可恢复诊断。
                logger.warning("运行产物独占准入待核实 task=%s job=%s reason=%s", task_uuid, job_uuid, error)
        aggregate = self._projection.project_job_finished(
            job_uuid=job_uuid,
            scheduler_state=scheduler_state,
            return_info=return_info,
            error_info=error_info,
            manual_confirmation_status=manual_confirmation_status,
            **({"inventory_reconciliation": inventory_reconciliation} if inventory_reconciliation else {}),
            **({"timing_sample": timing_sample} if timing_sample is not None else {}),
        )
        if self._time_scheduling_enabled:
            from unilabos.app.scheduler.timing_store import TimingObservationStore
            with self._store.read() as connection:
                saved_sample = TimingObservationStore(connection).get(job_uuid)
            spec = (self._scheduler._estimator._spec_for(saved_sample.device_action_key)
                    if saved_sample is not None else None)
            if (saved_sample is not None and saved_sample.spec_version == spec.version
                    and saved_sample.timing_boundary == spec.timing_boundary):
                # 只能学习已与执行事实一起提交的原始样本；消息重投不会重新计时。
                self._scheduler._estimator.observe(saved_sample.device_action_key,
                    saved_sample.actual_s, saved_sample.params, device_id=saved_sample.device_id,
                    action_version=saved_sample.action_version, outcome=saved_sample.outcome,
                    observation_id=saved_sample.observation_id, calibration=saved_sample.calibration)
        settled_job = next(item for item in aggregate["jobs"] if item["uuid"] == job_uuid)
        continuing_interval_ids = _retained_interval_ids_for_result(
            aggregate["task"],
            settled_job,
            aggregate["jobs"],
        )
        inventory_claim_state = (
            "uncertain" if str(settled_job.get("uncertainty_reason") or "").strip() else "released"
        )
        inventory_authority = self._scheduler.station_resource_inventory
        if inventory_authority is not None and claim is not None:
            if continuing_interval_ids and inventory_claim_state != "uncertain":
                control_data = settled_job.get("control_data")
                interval_map = (
                    control_data.get("resource_interval_ids_by_lock", {})
                    if isinstance(control_data, Mapping)
                    else {}
                )
                keep_lock_keys = tuple(
                    sorted(
                        str(lock_key)
                        for lock_key, raw_ids in interval_map.items()
                        if isinstance(raw_ids, (list, tuple, set, frozenset))
                        and set(str(value) for value in raw_ids) & set(continuing_interval_ids)
                    )
                )
                if not keep_lock_keys:
                    raise StoreConflict("连续区间结果缺少可保留的物理资源映射")
                retain = getattr(
                    inventory_authority,
                    "retain_dispatch_permit_resources",
                    None,
                )
                if not callable(retain):
                    raise StoreConflict("库存权威不支持连续区间的部分 Claim 释放")
                # 走到这里说明结果已明确（无 uncertainty_reason）。先 UNKNOWN、后证明
                # 成功的作业，其保留资源要从 uncertain 收敛回 running，后继才能继承。
                retain(
                    str(claim["claim_uuid"]),
                    keep_lock_keys=keep_lock_keys,
                    proved_result=True,
                )
            else:
                inventory_authority.transition_dispatch_permit(
                    str(claim["claim_uuid"]),
                    target_state=inventory_claim_state,
                )
        elif self._scheduler.physical_dispatch_enabled and not self.error_control.is_manual(job_uuid):
            raise StoreConflict(f"物理作业结果缺少库存 Claim：{job_uuid}")
        self._reconcile_inventory_resource_intervals(
            task_uuid=task_uuid,
            aggregate=aggregate,
            exclude_job_uuid=job_uuid,
        )
        self._cancel_cancel_timer(job_uuid)
        self._cancel_manual_confirmation_timer(job_uuid)
        terminal_status = aggregate["task"]["status"]
        if terminal_status == "succeeded" and not settled_job.get("uncertainty_reason") and any(
            job.get("executor_kind") == "material_source" for job in aggregate["jobs"]
        ):
            # 成功表示最后一个动作已完成，可立即释放来源层短期预留。失败任务必须
            # 等所有在途设备动作结算，由 ``_on_job_settled`` 推进 cleanup_status。
            self._material_sources.release_terminal_reservations(
                task_uuid,
                reason=f"workflow_{terminal_status}",
            )
        if terminal_status == "succeeded" and not settled_job.get("uncertainty_reason") and self._quantity_inventory is not None:
            self._quantity_inventory.release_task(
                task_uuid,
                reason="workflow_succeeded",
            )
        self._notify_attention_changed()

    def _reconcile_inventory_resource_intervals(
        self,
        *,
        task_uuid: str,
        aggregate: Mapping[str, Any],
        exclude_job_uuid: str = "",
    ) -> None:
        """按最新投影释放历史物理 Job 已结束区间的库存 Claim。"""

        inventory_authority = self._scheduler.station_resource_inventory
        if inventory_authority is None:
            return
        jobs = aggregate["jobs"]
        for previous in jobs:
            if (
                previous["uuid"] == exclude_job_uuid
                or previous["status"]
                not in {"succeeded", "failed", "canceled", "timeout"}
                or previous.get("uncertainty_reason")
            ):
                continue
            previous_control = previous.get("control_data") or {}
            interval_map = previous_control.get(
                "resource_interval_ids_by_lock", {}
            )
            if not previous_control.get("resource_interval_ids") or not interval_map:
                continue
            if self._projection.get_execution_claim(previous["uuid"]) is None:
                continue
            remaining = set(
                _retained_interval_ids_for_result(
                    aggregate["task"], previous, jobs
                )
            )
            keep = {
                key
                for key, interval_ids in interval_map.items()
                if remaining & set(interval_ids)
            }
            release_keys = tuple(sorted(set(interval_map) - keep))
            if release_keys:
                inventory_authority.release_preheld_dispatch_claims(
                    task_uuid=task_uuid,
                    job_uuids=(previous["uuid"],),
                    lock_keys=release_keys,
                )

    def _on_job_settled(
        self,
        job_uuid: str,
        success: bool,
        ret_value: Any,
        suc_type: str,
    ) -> None:
        """在调度 DAG 已结算当前节点后推进调试与异常终态清理。

        参数：``job_uuid`` 是已结算作业身份；其余参数是调度器完成事实，本方法不
        改写结果载荷。返回无。异常：调试推进或清理投影冲突原样传播，避免仍在途
        的物料被静默释放。
        """

        task_uuid = self._task_by_job.get(job_uuid)
        if task_uuid is None:
            return
        # 正常完成回调来自当前调度运行；重启后的 Edge 结果重放则可能只有持久
        # Job→Task 路由而没有对应内存运行。记录这一区别，用于在释放持久派发
        # 容量后主动唤醒其他已经恢复、仍在调度器中等待的作业。
        replayed_without_runtime = self._scheduler.workflow_snapshot(task_uuid) is None
        scheduler_runtime = self._scheduler.workflow_snapshot(task_uuid)
        if scheduler_runtime is not None:
            persisted_task = self._store.get_task(task_uuid)
            runtime_mode = str(
                scheduler_runtime.get("execution_mode")
                or persisted_task.get("execution_mode")
                or persisted_task.get("run_mode")
                or "normal"
            )
            if (
                runtime_mode == "step"
                and persisted_task.get("execution_mode") == "switching_to_step"
                and persisted_task.get("status")
                not in {"succeeded", "failed", "canceled", "timeout"}
            ):
                self._store.set_task_execution_mode(
                    task_uuid,
                    execution_mode="step",
                    control_status="paused",
                    dispatch_gate="paused",
                )
        # ``continue`` 只在下一个节点没有断点时复用既有单步派发原语；断点或
        # 显式 ``step`` 会先创建新 Hold，绝不越过物理派发边界。
        debug_action = self._store.advance_debug_after_job_finished(task_uuid)
        if debug_action.get("type") == "step":
            next_node_uuid = self._required_text(
                debug_action.get("workflow_node_uuid"),
                field="debug.workflow_node_uuid",
            )
            try:
                self._scheduler.step_workflow(
                    task_uuid,
                    target_node_id=next_node_uuid,
                )
            except ValueError as error:
                raise TaskSchedulerBridgeError(str(error)) from error
        self._task_by_job.pop(job_uuid, None)
        aggregate = self._aggregate(task_uuid)
        if aggregate["task"]["status"] in {"failed", "canceled", "timeout"}:
            scheduler_snapshot = self._scheduler.snapshot()
            active_for_task = any(
                inflight.get("workflow_id") == task_uuid
                for inflight in scheduler_snapshot.get("inflight_jobs", {}).values()
            )
            unsettled_jobs = [
                job for job in aggregate["jobs"] if str(job.get("uncertainty_reason") or "").strip()
            ]
            if not active_for_task and not unsettled_jobs:
                retained_intervals = any(
                    _retained_interval_ids_for_result(aggregate["task"], job, aggregate["jobs"])
                    for job in aggregate["jobs"]
                )
                if retained_intervals:
                    # 投影会记录待人工交接；不得先释放任务级物料预留。
                    self._projection.project_cleanup_settled(task_uuid)
                else:
                    if self._quantity_inventory is not None:
                        self._quantity_inventory.release_task(
                            task_uuid,
                            reason=f"workflow_{aggregate['task']['status']}",
                        )
                    if any(
                        job.get("executor_kind") == "material_source" for job in aggregate["jobs"]
                    ):
                        self._material_sources.release_terminal_reservations(
                            task_uuid,
                            reason=f"workflow_{aggregate['task']['status']}",
                        )
                    # ``settled`` 是两类库存预留均已安全释放后的最终承诺。任何释放
                    # 失败都会保留非 settled 状态，供恢复扫描以同一身份幂等重试。
                    self._projection.project_cleanup_settled(task_uuid)
        if task_uuid not in self._task_by_job.values():
            self._submitted_tasks.discard(task_uuid)
        if replayed_without_runtime:
            # Edge 发件箱重放发生在旧调度运行已经丢失之后；此时不会经过
            # ``EdgeScheduler._on_job_finished`` 尾部的自动重排，需要显式唤醒
            # 其他已恢复运行。容量仍由数据库状态判定，不依赖这个内存触发器。
            self._scheduler.reschedule()

    def _finish_settled_terminal_task(self, job_uuid: str) -> None:
        """在显式物理结算完成后推进父任务资源清理。

        参数：``job_uuid`` 是刚刚清除不确定事实的失败作业。返回无；复用标准
        settled 回调以统一释放任务级预留和调度容量；Task 真正 settled 后同步
        释放其全部 Inventory Permit，包括曾为该 Job 提供 preheld 连续锁的
        provider。异常原样传播，跨库失败由终态恢复扫描幂等补偿。
        """

        task_uuid = str(self._store.get_job(job_uuid)["workflow_task_uuid"])
        self._task_by_job.setdefault(job_uuid, task_uuid)
        self._on_job_settled(job_uuid, False, None, "physical_settlement")
        if self._store.get_task(task_uuid).get("cleanup_status") != "settled":
            return
        inventory = self._scheduler.station_resource_inventory
        if inventory is None:
            return
        for job in self._store.list_jobs(task_uuid):
            candidate_job_uuid = self._required_text(
                job.get("uuid"), field="job.uuid"
            )
            if candidate_job_uuid == job_uuid:
                # 调用方在进入本收尾前已经释放刚结算 Job 的 Inventory Permit。
                continue
            claim = self._projection.get_execution_claim(
                candidate_job_uuid
            )
            if claim is not None:
                inventory.transition_dispatch_permit(
                    str(claim["claim_uuid"]),
                    target_state="released",
                )

    @staticmethod
    def _normalize_actual_material_change_set(
        value: Mapping[str, Any],
    ) -> dict[str, Any]:
        """验证操作员提交的实际物料位置，拒绝模糊或额外字段。"""

        if not isinstance(value, Mapping):
            raise ValueError("实际物料位置必须是对象")
        allowed = {
            "kind",
            "material_uuid",
            "target_owner_material_uuid",
            "target_site_uuid",
            "target_site_name",
        }
        if set(value) - allowed:
            raise ValueError("实际物料位置包含未定义字段")
        normalized = {field: str(value.get(field) or "").strip() for field in allowed}
        if normalized["kind"] != "material_transfer":
            raise ValueError("实际变化类型必须是 material_transfer")
        if (
            not normalized["material_uuid"]
            or not normalized["target_owner_material_uuid"]
        ):
            raise ValueError("实际物料位置缺少物料或父资源 UUID")
        if bool(normalized["target_site_uuid"]) == bool(normalized["target_site_name"]):
            raise ValueError("实际物料位置必须且只能指定库位 UUID 或名称")
        return {key: item for key, item in normalized.items() if item}

    def _aggregate(self, task_uuid: str) -> dict[str, Any]:
        """读取一个标准任务/作业聚合。

        参数：``task_uuid`` 是父任务稳定身份。返回：当前任务投影和有序作业列表。
        异常：任务不存在时传播工作流存储（WorkflowStore）异常。
        """

        confirmations = {
            item["workflow_node_job_uuid"]: item
            for item in self._manual_confirmations.list_by_task(task_uuid)
        }
        return {
            "task": self._store.get_task(task_uuid),
            "jobs": [
                {
                    **job,
                    **(
                        {"manual_confirmation": confirmations[job["uuid"]]}
                        if job["uuid"] in confirmations
                        else {}
                    ),
                }
                for job in self._store.list_jobs(task_uuid)
            ],
        }

    def _project_scheduler_trace_context(
        self,
        task_uuid: str,
        trace_context: Any,
    ) -> None:
        """把 Scheduler 根 span carrier 写入 Task，关闭观测时保持业务 no-op。"""

        if not isinstance(trace_context, Mapping) or not trace_context.get(
            "traceparent"
        ):
            return
        projector = getattr(self._projection, "project_trace_context", None)
        if not callable(projector):
            logger.warning(
                "任务运行投影未实现 Trace Context 接口，忽略 task=%s",
                task_uuid,
            )
            return
        try:
            projector(task_uuid, trace_context)
        except Exception:  # noqa: BLE001 - 观测元数据失败不得改变物理调度结论
            logger.exception(
                "工作流任务 Trace Context 持久化失败，业务继续 task=%s",
                task_uuid,
            )

    def _begin_submission_phase(self, task_uuid: str) -> None:
        """登记一次即将调用 Edge ``submit_workflow`` 的阶段。"""

        with self._submission_phase_lock:
            self._submission_phases[task_uuid] = _SUBMISSION_PHASE_SCHEDULER

    def _mark_submission_phase(self, task_uuid: str, phase: str) -> None:
        """把同步提交标记为已经进入某个调度回调。

        回调也可能来自稍后的公开重排；此时没有活动 submit 阶段，标记操作保持
        no-op，避免把正常运行期的投影异常误判为提交回滚。
        """

        if phase not in {
            _SUBMISSION_PHASE_PRE_DISPATCH,
            _SUBMISSION_PHASE_LOCAL_CONTROL,
        }:
            raise ValueError(f"非法工作流提交阶段：{phase}")
        with self._submission_phase_lock:
            if task_uuid in self._submission_phases:
                self._submission_phases[task_uuid] = phase

    def _finish_submission_phase(self, task_uuid: str) -> str | None:
        """取出并清除一次提交阶段，返回其最后已知阶段。"""

        with self._submission_phase_lock:
            return self._submission_phases.pop(task_uuid, None)

    def _discard_retryable_scheduler_run(self, task_uuid: str) -> None:
        """只丢弃本桥此前保留的 canceled Edge 占位。

        参数：``task_uuid`` 是待重试任务身份。返回无；占位不存在或已被其他
        清理路径移除时幂等返回。异常：调度器拒绝丢弃（例如意外出现在途作业）
        时保留本桥标记并向调用方传播，禁止带着可能冲突的 UUID 再次提交。
        """

        with self._submission_phase_lock:
            if task_uuid not in self._retryable_scheduler_runs:
                return
            try:
                self._scheduler.discard_workflow(task_uuid)
            except Exception as error:
                raise TaskSchedulerBridgeError(
                    "上一次失败提交的本地调度占位仍无法安全清理"
                ) from error
            # ``False`` 表示调度器中已没有该运行（例如外部恢复清理已完成），
            # 对本桥标记而言同样是安全的幂等结果。
            self._retryable_scheduler_runs.discard(task_uuid)

    def _cancel_failed_submission(
        self,
        task_uuid: str,
        jobs: list[dict[str, Any]],
        *,
        retain_pending: bool = False,
    ) -> None:
        """封闭一次未越过执行边界的失败提交。

        参数：``task_uuid`` 是旧调度运行身份；``jobs`` 是本次路由的持久作业集合；
        ``retain_pending`` 表示异常已进入派发准入或本地控制回调，此时只取消
        Edge 内存运行并保留其 canceled 快照，持久 Task/Job 继续保持 pending，供
        同一桥重试。返回无；尽力取消遗留内存运行并清除监听路由，原始异常由调用
        方保留。早期 scheduler submit 失败则继续把任务投影为 canceled、释放来源
        预留并完成 cleanup。
        """

        canceled = False
        try:
            canceled = bool(self._scheduler.cancel_workflow(task_uuid))
        except Exception:  # 清理失败不能覆盖原始安全错误
            logger.exception("失败的工作流任务提交无法取消遗留调度运行")

        # 无论是否保留快照，桥接路由都必须先撤掉，防止失败运行在后续共享重排
        # 中继续回调到已经失效的持久提交上下文。
        self._submitted_tasks.discard(task_uuid)
        self._admission_pending_tasks.discard(task_uuid)
        for job in jobs:
            self._task_by_job.pop(str(job.get("uuid") or ""), None)

        if retain_pending:
            # 只有确认 cancel 调用找到了运行，才登记为本桥拥有的可重试占位；若
            # 调度器在 submit 前就失败且没有创建运行，下一次 submit 无需 discard。
            if canceled:
                with self._submission_phase_lock:
                    self._retryable_scheduler_runs.add(task_uuid)
            return

        try:
            self._scheduler.discard_workflow(task_uuid)
        except Exception:  # 清理失败不能覆盖原始安全错误
            logger.exception("失败的工作流任务提交无法丢弃遗留调度运行")
        aggregate = self._projection.project_canceled(task_uuid)
        try:
            self._release_canceled_inventory(
                aggregate,
                reason="workflow_submission_failed",
            )
        except Exception:  # 清理重试由 required 状态恢复；不得覆盖提交原始异常
            logger.exception("失败提交的任务库存预留暂未释放，等待启动恢复重试")

    def _release_canceled_inventory(
        self,
        aggregate: dict[str, Any],
        *,
        reason: str,
    ) -> dict[str, Any]:
        """幂等释放能够证明安全终止的取消任务库存。

        参数：``aggregate`` 是取消后的 Task/Job 聚合；``reason`` 是库存审计原因。
        返回：释放后聚合。``required`` 先释放再提交 ``settled``；历史 ``settled``
        仍重放幂等释放以修复旧版本提前结算留下的活动预留。``requires_attention``
        等不能证明安全的状态保持全部占用，等待人工物理对账。
        """

        task = aggregate.get("task")
        jobs = aggregate.get("jobs")
        if not isinstance(task, dict) or not isinstance(jobs, list):
            raise StoreConflict("取消任务聚合缺少 Task 或 Jobs")
        if task.get("status") != "canceled":
            raise StoreConflict("只能释放已取消任务的库存预留")
        cleanup_status = str(task.get("cleanup_status") or "")
        if cleanup_status not in {"required", "settled"}:
            return aggregate
        inventory = self._scheduler.station_resource_inventory
        for job in jobs:
            if not isinstance(job, Mapping):
                continue
            job_uuid = str(job.get("uuid") or "")
            if not job_uuid:
                continue
            claim = self._projection.get_execution_claim(job_uuid)
            if claim is not None and inventory is not None:
                inventory.transition_dispatch_permit(
                    str(claim["claim_uuid"]),
                    target_state="released",
                )
        if self._quantity_inventory is not None:
            self._quantity_inventory.release_task(
                str(task["uuid"]),
                reason=reason,
            )
        if any(job.get("executor_kind") == "material_source" for job in jobs):
            self._material_sources.release_terminal_reservations(
                str(task["uuid"]),
                reason=reason,
            )
        if cleanup_status == "required":
            return self._projection.project_cleanup_settled(str(task["uuid"]))
        return aggregate

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
        return bool(
            persisted_statuses
            & {
                "dispatched",
                "running",
                "cancel_requested",
            }
        )

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
