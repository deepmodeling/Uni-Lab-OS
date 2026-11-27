"""本地调度器（EdgeScheduler）：Edge 侧执行态推进的唯一入口。

重排触发点（硬性约定，二者都强制全量 reschedule）：

1. **每个工作流提交**（``submit_workflow``）
2. **每个子 action 完成**（``on_job_finished``，含成功/失败）

每次 reschedule：

    收集所有 RUNNING 工作流的 ready 节点
      → TaskOrderer 排序（本地 stub 或 HTTP 调 uni-lab-scheduler）
      → 按序下发；动作键或设备级互斥键被占用的节点跳过，等下一次触发
      → 下发前解析父节点传参（gjson/sjson + ``@@@`` 语义）

不做一次性拓扑序：ready 集合每次触发点都重新计算、重新排序。

物料衔接（注入本地库存服务（InventoryService）时启用；spec 无物料字段则行为完全不变）：

- submit：汇总 DAG 全部物料需求，入队前 all-or-nothing 预留；
  不足 → workflow 置 ``waiting_for_material``，不进入执行队列，每次重排重试预留
- 节点下发前只持有预留，不提前扣减库存
- 明确成功结果提交时：预留 → FIFO lot 消费 + 实例 deploy（幂等键
  workflow:node:attempt）
- 失败、取消或跳过不扣减；工作流终态释放剩余 active 预留（依据 DB，不依赖内存）

动作物料锁与库位锁（Action Material Lock / Site Lock）：

- 下发前校验最终参数，并从规范动作 Schema 提取物料 UUID
- ``transfer_resource`` 的 ``site_uuid`` 是独立可选参数；优先按稳定 UUID 查库位，
  未提供时再按 ``mount_resource.uuid + site`` 名称解析
- 整物料锁覆盖其所有子库位；同一库位串行，同一物料下不同库位可并行
- 实体型物料需求的 ``instance_uuid`` 自动并入同一物料锁键
- job 完成 / 工作流取消时释放
"""

from __future__ import annotations

import logging
import threading
import time
import uuid as uuid_mod
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from unilabos.app.scheduler.timing_store import TimingObservation

from unilabos.app.scheduler.dag_state import WorkflowRun
from unilabos.app.scheduler.device_target import (
    DeviceTargetUnavailable,
    ResolvedDeviceTarget,
)
from unilabos.app.scheduler.dispatch import (
    CancelDispatchState,
    CommittedJobOutcome,
    Dispatcher,
    DispatchPayload,
    RecordingDispatcher,
    build_job_start_payload,
)
from unilabos.app.scheduler.estimation import DurationEstimator
from unilabos.app.scheduler.inventory.dispatch_admission import DispatchAdmissionConflict
from unilabos.app.scheduler.inventory.domain import InsufficientStock
from unilabos.app.scheduler.inventory.material_io import MaterialIOError
from unilabos.app.scheduler.inventory.station_resource import (
    StationResourceError,
    StationResourceInventory,
)
from unilabos.app.scheduler.models import (
    DispatchedJob,
    NodeState,
    ReadyTask,
    RepeatUntilRegion,
    WorkflowNode,
    WorkflowSpec,
    WorkflowState,
    priority_weight,
)
from unilabos.app.scheduler.ordering import (
    OrderingContext,
    StableLocalOrderer,
    TaskOrderer,
)
from unilabos.app.scheduler.param_resolver import ParamResolveError
from unilabos.app.scheduler.resource_lock import (
    canonical_resource_lock_scope,
    conflicting_resource_lock_keys,
    material_lock_key,
    narrow_runtime_owner_lock_to_site,
    normalize_resource_lock_keys,
    site_lock_key,
)
from unilabos.app.scheduler.resource_wait_policy import (
    is_temporary_resource_condition,
)
from unilabos.app.scheduler.serial_scheduling import SerialWorkflowQueue
from unilabos.app.scheduler.site_target import (
    ResolvedSiteTarget,
    SiteTargetResolutionError,
    resolve_site_target,
)
from unilabos.app.scheduler.transfer_resource_set import (
    TransferResourceSetError,
    resolve_transfer_resource_set,
)
from unilabos.registry.material_lock_schema import (
    MaterialLockSchemaError,
    compile_material_lock_schema,
)
from unilabos.utils.tracing import (
    DetachedSpan,
    add_event,
    extract_trace_context,
    span,
    start_detached_span,
    submit_with_context,
)
from unilabos.workflow.execution_resource_policy import (
    ExecutionResourcePolicyError,
    resolve_execution_resource_policy,
)
from unilabos.workflow.resource_lock_key import device_lock_key, parse_canonical_resource_lock_key
from unilabos.workflow.resource_lock_plan import (
    SCHEDULER_RESOURCE_ORDER_CAPABILITY,
    ResourcePlan,
    deserialize_resource_plan,
    resource_plan_for_node,
    validate_station_resource_plans,
)

logger = logging.getLogger(__name__)
# ``launch`` installs ``LaunchLogger`` as the process-wide logger class and
# defaults each new logger to ``propagate=False``.  This module does not attach
# a private handler, so policy failures would otherwise fall through to
# ``logging.lastResort`` and bypass the application's root handler (and pytest's
# ``caplog``).  Scheduler diagnostics belong to the normal application log
# stream, so explicitly restore propagation here.
logger.propagate = True


def _merge_param_patch(
    current: Mapping[str, Any],
    patch: Mapping[str, Any],
) -> dict[str, Any]:
    """递归合并暂停任务的参数修改，数组和标量按修改值替换。"""

    merged = deepcopy(dict(current))
    for key, value in patch.items():
        previous = merged.get(key)
        if isinstance(previous, Mapping) and isinstance(value, Mapping):
            merged[key] = _merge_param_patch(previous, value)
        else:
            merged[key] = deepcopy(value)
    return merged

_RECONCILE_EXECUTOR = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix="station-scheduler-reconcile",
)
_RECONCILE_THREAD = threading.local()


def _run_reconcile(
    function: Callable[[], list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """在唯一调度循环线程执行一次被唤醒的重排。"""

    _RECONCILE_THREAD.active = True
    try:
        return function()
    finally:
        _RECONCILE_THREAD.active = False


def _log_background_reconcile_failure(future: Future[Any]) -> None:
    """记录回调重入时无法同步等待的后台重排异常。"""

    try:
        future.result()
    except BaseException:
        logger.exception("[EdgeScheduler] 后台重排失败")


_DEFAULT_MAX_IN_FLIGHT_JOBS = 100
_DEFAULT_MAX_ACTIVE_TASKS = 500
_DEFAULT_MAX_TASKS_PER_WORKFLOW = 100


def _bound_resource_lock_key(resource: Mapping[str, Any]) -> str:
    """把 bound 计划资源投影为当前调度器使用的规范互斥键。"""

    canonical_key = str(resource.get("canonical_key") or "").strip()
    if canonical_key.startswith("/"):
        return canonical_key
    kind = str(resource.get("kind") or "").strip().lower()
    instance_uuid = str(resource.get("instance_uuid") or "").strip()
    if kind in {"device", "motion", "tool", "robot", "rail"} and instance_uuid:
        return device_lock_key(instance_uuid)
    if kind in {"material", "container", "sample"} and instance_uuid:
        return material_lock_key(instance_uuid)
    if kind in {"site", "material_site"} and canonical_key:
        return canonical_key
    if canonical_key.startswith(("material/", "/devices/")):
        return canonical_key
    return canonical_key


class ExecutionPolicyError(ValueError):
    """冻结节点执行策略无法安全转成持久调度声明。"""


def _resource_argument_uuid(value: Any, *, argument_name: str) -> str:
    """从动作物料引用中读取并规范化稳定 UUID。

    参数：``value`` 可以是规范 ``ResourceSlot`` 字典或内部 PLR 富对象；
    ``argument_name`` 用于错误说明。返回：规范小写 UUID 字符串。异常：字段缺失
    或格式非法时抛 ``SiteTargetResolutionError``，防止库位锁退化为名称猜测。
    """

    raw_uuid: Any = None
    if isinstance(value, Mapping):
        raw_uuid = value.get("uuid") or value.get("unilabos_uuid")
    else:
        raw_uuid = getattr(value, "unilabos_uuid", None) or getattr(
            value,
            "uuid",
            None,
        )
    try:
        return str(uuid_mod.UUID(str(raw_uuid)))
    except (AttributeError, TypeError, ValueError) as error:
        raise SiteTargetResolutionError(
            "invalid_resource_uuid",
            f"{argument_name} 缺少合法 uuid",
        ) from error


def _device_key_from_strict_action_key(action_key: Any) -> str | None:
    """从严格动作级忙碌键提取设备级内存互斥键。

    参数：``action_key`` 是外部忙碌提供者返回的候选键。
    返回：仅当输入严格符合 ``/devices/{device_id}/{action_name}`` 且设备、动作
    均非空时返回 ``/devices/{device_id}``；其他输入返回 ``None``。
    异常：不主动抛出异常；非字符串和歧义路径一律不解析，避免误扩大互斥范围。

    该转换只桥接既有动作级内存事实，不产生持久作业执行占用
    （JobExecutionClaim）或栅栏（Fence）。
    """

    if not isinstance(action_key, str):
        return None
    path_parts = action_key.split("/")
    if (
        len(path_parts) != 4
        or path_parts[0] != ""
        or path_parts[1] != "devices"
        or not path_parts[2]
        or not path_parts[3]
    ):
        return None
    try:
        return device_lock_key(path_parts[2])
    except ValueError:
        return None


class EdgeScheduler:
    def __init__(
        self,
        orderer: TaskOrderer | None = None,
        dispatcher: Dispatcher | None = None,
        external_busy_keys: set[str] | None = None,
        busy_key_provider: Callable[[], set[str]] | None = None,
        workflow_state_listener: Callable[[str, str], None] | None = None,
        inventory: Any = None,
        station_resources: StationResourceInventory | None = None,
        device_target_resolver: (
            Callable[[Mapping[str, Any], str, set[str]], ResolvedDeviceTarget] | None
        ) = None,
        estimator: DurationEstimator | None = None,
        timeline_capacity: int = 400,
        monitor: Any = None,
        history: Any = None,
        max_in_flight_jobs: int = _DEFAULT_MAX_IN_FLIGHT_JOBS,
        max_active_tasks: int = _DEFAULT_MAX_ACTIVE_TASKS,
        max_tasks_per_workflow: int = _DEFAULT_MAX_TASKS_PER_WORKFLOW,
        clock: Callable[[], float] = time.time,
        serial_queue: bool = False,
    ):
        """装配本地执行态调度器（Scheduler）。

        Args:
            orderer: 对已就绪任务进行稳定排序的策略。
            dispatcher: 把作业（Job）提交给执行器的适配器。
            external_busy_keys: 启动时已知的外部设备占用键。
            busy_key_provider: 实时读取设备占用键的函数。
            workflow_state_listener: 工作流（Workflow）终态通知函数。
            inventory: 本地库存（Inventory）预留、消费和释放服务。
            station_resources: 设备、库位（Site）与转运事实的窄库存接口；生产
                组合根显式注入，隔离测试可从 ``InventoryService`` 读取同一接口。
            device_target_resolver: 按冻结设备类型从当前 Edge 注册选择实例的函数。
            estimator: 动作预计时长计算器。
            timeline_capacity: 内存时间线最多保留的作业数量。
            monitor: 实时监控事件输出适配器。
            history: 遗留工作流执行历史存储。
            max_in_flight_jobs: 全局尚未收敛的在途作业上限。
            max_active_tasks: 全局运行中工作流任务上限。
            max_tasks_per_workflow: 同一工作流定义的运行任务上限。
            clock: 调度时间来源；测试可注入手动时钟，生产默认使用系统时间。
            serial_queue: 一次仅推进一个 Task；卸料等待时可先推进相关用料任务。
        """

        for field, value in (
            ("max_in_flight_jobs", max_in_flight_jobs),
            ("max_active_tasks", max_active_tasks),
            ("max_tasks_per_workflow", max_tasks_per_workflow),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{field} 必须是正整数")
        if max_tasks_per_workflow > max_active_tasks:
            raise ValueError("max_tasks_per_workflow 不能大于 max_active_tasks")

        self._clock = clock
        self._serial_queue = serial_queue
        self._serial_workflows = SerialWorkflowQueue()
        self._serial_admissions: dict[str, float] = {}
        self._orderer = orderer or StableLocalOrderer(clock=clock)
        self._dispatcher = dispatcher or RecordingDispatcher()
        self._lock = threading.RLock()

        self._workflows: dict[str, WorkflowRun] = {}
        # workflow_id -> 本次单步命令唯一允许派发的节点。只在一次重排期间存在。
        self._step_targets: dict[str, str] = {}
        # 单步打开的人工提示在批准后继续同一个节点，直至派发或终态。
        # job_id -> DispatchedJob（完成回调路由 + 资源锁）
        self._inflight: dict[str, DispatchedJob] = {}
        # 外部注入的锁（例如 DeviceActionManager 已占用的设备），可选
        self._external_busy_keys = external_busy_keys if external_busy_keys is not None else set()
        # 实时锁视图提供者（微后端 busy_device_action_keys），可选
        self._busy_key_provider = busy_key_provider
        # 工作流终态通知（success/failed/canceled 各通知一次；锁外触发）
        self._workflow_state_listener = workflow_state_listener
        self._notified_workflows: set[str] = set()
        self._reschedule_count = 0
        # 可选 InventoryService（duck-typed：reserve_workflow / consume_reservation /
        # quarantine_reservation / release_workflow）；None = 物料衔接整体关闭
        self._inventory = inventory
        # 工站资源读取只依赖公开窄接口，调度器不得再穿透 InventoryService.store。
        self._station_resources = station_resources
        if (
            self._station_resources is None
            and inventory is not None
            and hasattr(type(inventory), "station_resources")
        ):
            candidate_station_resources = getattr(inventory, "station_resources", None)
            if candidate_station_resources is not None:
                self._station_resources = candidate_station_resources
        # 有物料需求的 workflow（其余 workflow 不产生任何 inventory 调用）
        self._material_workflows: set[str] = set()
        self._device_target_resolver = device_target_resolver
        # job_id -> 该作业（Job）持有的物料与库位锁键；完成或取消时释放。
        self._job_resource_locks: dict[str, set[str]] = {}
        # （工作流、区间）→ 已完成 Job 之后仍保留的具体锁键；这是内存连续持有
        # 投影，持久桥在 Lease 元数据中镜像同一所有权。
        self._interval_resource_holders: dict[tuple[str, str], set[str]] = {}
        self._interval_resource_holder_jobs: dict[tuple[str, str], set[str]] = {}
        # holder 只描述“当前可继承的所有权”，opened 另行记录区间是否已越过
        # 首个物理派发边界。二者分离后，重启丢失 handoff 时不能把一个已经
        # 开始的连续区间误当成全新申请，从而掩盖资源占用断裂。
        self._opened_resource_intervals: set[tuple[str, str]] = set()
        self._resource_plans: dict[str, ResourcePlan] = {}
        # 时长预估器（declared / historical / auto 三种 mode，内含两种计算模式）
        self._estimator = estimator or DurationEstimator()
        # 泳道图时间线：已完结 job 的起止记录（环形缓冲）
        self._timeline: deque[dict[str, Any]] = deque(maxlen=timeline_capacity)
        # 实时监控总线（duck-typed emit(channel, type, data)）；None = 关闭
        self._monitor = monitor
        self._material_monitor_listener_id: int | None = None
        # 物料监听与 submit/finish 必须共用同一轮重排 Future，否则预留/结算
        # 事件会先抢走派发，调用方等到的下一轮返回空 dispatched。
        self._reconcile_wakeup_lock = threading.Lock()
        self._reconcile_condition = threading.Condition(self._reconcile_wakeup_lock)
        self._active_reschedule_threads: dict[int, int] = {}
        self._close_lock = threading.RLock()
        self._closed = False
        self._pending_reconcile: Future[Any] | None = None
        self._reconcile_generation = 0
        add_listener = getattr(monitor, "add_listener", None)
        if callable(add_listener):
            self._material_monitor_listener_id = add_listener(
                self._on_material_changed,
                {"material"},
            )
        # 工作流执行历史（WorkflowHistoryStore，独立 SQLite）；None = 不落盘
        self._history = history
        # 容量裁决由标准 Task/Job 持久状态完成；调度器只公开同一组配置，禁止
        # 另建进程内计数权威。默认值与 Backend 主线保持一致。
        self._max_in_flight_jobs = max_in_flight_jobs
        self._max_active_tasks = max_active_tasks
        self._max_tasks_per_workflow = max_tasks_per_workflow
        # 排空（DRAIN）只停止新的设备作业派发；已经派发的作业仍可回传结果并
        # 完成结算。状态只属于调度器权威，Workspace Host 通过 HTTP 查询。
        self._draining = False
        self._station_control: Any = None
        # 标准 Task/Job 持久层可能保留重启后不允许重放的物理不确定作业；提供者
        # 只返回身份，排空状态在读取时合并，不复制或改写持久权威。
        self._drain_blocker_provider: Callable[[], set[str]] | None = None
        self._workflow_inventory_release_guard: Callable[[str], bool] | None = None
        # 长生命周期根 span：workflow → action/job。只保存上下文/句柄，不保存 payload。
        self._workflow_spans: dict[str, DetachedSpan] = {}
        self._job_spans: dict[str, DetachedSpan] = {}
        # 物理派发只有一个持久准入权威。单一绑定防止普通观察器伪造 Permit，
        # 也避免多个数据库权威以未定义顺序分别取得部分资源。
        self._dispatch_admission_authority: Callable[[dict[str, Any]], bool] | None = None
        self._manual_continuation_authority: Callable[[str], None] | None = None
        self._job_execution_wait_listeners: list[Callable[[dict[str, Any]], None]] = []
        self._job_dispatch_accepted_listeners: list[Callable[[str], None]] = []
        self._job_dispatch_uncertain_listeners: list[Callable[[str, str], None]] = []
        self._job_cancel_accepted_listeners: list[Callable[[str], None]] = []
        self._job_cancel_uncertain_listeners: list[Callable[[str, str], None]] = []
        self._job_cancel_no_send_listeners: list[Callable[[str], None]] = []
        self._job_feedback_listeners: list[Callable[[str, dict[str, Any]], None]] = []
        self._job_finished_listeners: list[Callable[[str, bool, Any, str], None]] = []
        self._job_outcome_listeners: list[Callable[[str, CommittedJobOutcome], None]] = []
        self._job_settled_listeners: list[Callable[[str, bool, Any, str], None]] = []
        # 观察者返回 None；唯一持久投影监听器可返回经事务确认的替代决定。
        self._local_control_listeners: list[Callable[[dict[str, Any]], dict[str, Any] | None]] = []
        self._execution_process_restarted_listeners: list[Callable[[tuple[str, ...]], None]] = []
        self._execution_connection_lost_listeners: list[
            Callable[[tuple[str, ...]], None]
        ] = []
        # 准入重试监听器把尚未注册为旧调度运行的来源受阻任务接到同一个公开
        # 重排触发点；监听器本身仍由工作流任务桥拥有。
        self._admission_retry_listeners: list[Callable[[], None]] = []

    @property
    def inventory_service(self) -> Any:
        """返回本地调度器持有的库存服务（InventoryService）。

        参数：无。返回：同一库存权威（Inventory Authority）实例；未装配时为
        ``None``。该只读属性只供组合与桥接层验证和复用，禁止替换权威。
        """

        return self._inventory

    @property
    def serial_queue_enabled(self) -> bool:
        return self._serial_queue

    def serial_admission_turn(self, workflow_id: str, submitted_at: float) -> bool:
        """先登记持久 Task 再判定队首，物料准入和派发共用同一队列。"""
        if not self._serial_queue:
            return True
        with self._lock:
            self._serial_admissions[workflow_id] = submitted_at
            return (
                not self._draining
                and self._select_serial_workflow() == workflow_id
                and (self._station_control is None or self._station_control.allows_task(workflow_id))
            )

    def discard_serial_admission(self, workflow_id: str) -> bool:
        """清除未入执行图的任务；调用方在库存清理后唤醒下一轮。"""
        with self._lock:
            return self._serial_admissions.pop(workflow_id, None) is not None

    def serial_admission_candidate(self) -> str | None:
        """返回当前可以尝试准入的排队任务身份，供重排监听器使用。"""
        with self._lock:
            if self._draining or getattr(self._station_control, "recovering", False):
                return None
            current = self._select_serial_workflow()
            return current if current in self._serial_admissions else None

    def advance_serial_admission(self, workflow_id: str, blocker: str) -> None:
        """允许已入图并持有旧库存预留的阻塞任务完成。"""
        with self._lock:
            if not self._serial_queue or self._inflight or self._draining or self._select_serial_workflow() != workflow_id:
                return
            run = self._workflows.get(blocker)
            if run is None or run.state in self._TERMINAL_STATES:
                return
            if self._serial_workflows.advance(self._active_workflow_submit_times(), blocker, set()):
                self._wake_reconcile(wait=False)

    def wake_serial_queue(self, *, release_admission: Callable[[], None] | None = None) -> None:
        """同步等待已登记任务的重排，调度回调内则交给下一轮处理。"""
        self._wake_reconcile(release_before_wait=release_admission)

    @property
    def time_scheduling_enabled(self) -> bool:
        """开源版的时间排程能力，保留供公共 API 使用。"""
        return False

    def capture_timing_observation(self, job_id: str) -> TimingObservation | None:
        """在结果首次提交前捕获同一计时边界的完整样本；推断和人工等待不采样。"""
        from unilabos.app.scheduler.timing import DEFAULT_BOUNDARY, finite_seconds
        from unilabos.app.scheduler.timing_store import TimingObservation

        with self._lock:
            job = self._inflight.get(job_id)
            if job is None or not job.timing_origin_observed:
                return None
            run = self._workflows.get(job.workflow_id)
            node = run.node(job.node_id) if run is not None else None
            if (run is None or node is None or node.is_manual_confirm()
                    or run.state.value != "running" or run.dispatch_gate != "active"):
                return None
            if self._station_control is not None and (
                not self._station_control.is_current(job_id)
                or self._station_control.awaits_decision(job_id)
                or self._station_control.is_manual(job_id)
            ):
                return None
            spec = self._estimator._spec_for(job.device_action_key)
            if spec.timing_boundary != DEFAULT_BOUNDARY:
                # 当前只有派发与可观察结果时间，没有真实物理起止证据。
                return None
            actual_s = finite_seconds(self._clock() - job.dispatched_at)
            if actual_s is None:
                return None
            return TimingObservation(
                observation_id=job_id, device_action_key=job.device_action_key,
                actual_s=actual_s, params=deepcopy(job.resolved_args),
                device_id=job.device_material_uuid or job.device_id,
                spec_version=spec.version, timing_boundary=spec.timing_boundary,
            )

    def time_schedule_snapshot(self) -> dict[str, Any]:
        """保留时间排程查询接口，公开版报告该能力已禁用。"""
        return {"enabled": False, "algorithm": "stable-ready-order"}

    @property
    def execution_control_lock(self) -> Any:
        """派发与错误控制共用短临界区，防止暂停后同轮候选继续开始。"""
        return self._lock

    def bind_station_control(self, control: Any) -> None:
        with self._lock:
            self._station_control = control
            bind = getattr(self._dispatcher, "bind_execution_control", None)
            if callable(bind):
                bind(control, self._notify_job_dispatch_accepted)

    def restore_not_started_dispatches(self) -> None:
        """按原身份修复未开始出站意图；执行端仍需通过实时开始屏障。"""
        with self._lock:
            restore = getattr(self._dispatcher, "restore_dispatch", None)
            if self._station_control is None or not callable(restore):
                return
            for payload in self._station_control.recovery_dispatch_payloads():
                restore(DispatchPayload(payload))

    def has_inflight_job(self, job_uuid: str) -> bool:
        with self._lock:
            return job_uuid in self._inflight

    def retry_node(self, task_uuid: str, node_id: str, job_uuid: str) -> None:
        """应用持久新 attempt；本方法本身不解除工站暂停。"""
        with self._lock:
            run = self._workflows.get(task_uuid)
            if run is None:
                raise ValueError("重试任务尚未恢复到调度器")
            run.retry_node(node_id, job_uuid)

    @property
    def station_resource_inventory(self) -> StationResourceInventory | None:
        """返回调度器使用的工站资源库存窄接口。

        参数：无。返回：设备、库位（Site）与转运事实的唯一读取/结算接口；未
        装配库存时为 ``None``。异常：不访问外部状态，不主动抛出异常。
        """

        return self._station_resources

    @property
    def physical_dispatch_enabled(self) -> bool:
        """返回当前执行适配器是否会越过真实物理派发边界。

        参数：无。返回：记录型干跑适配器为假，其余适配器为真。异常：无。该值
        只用于组合根强制装配库存准入权威，不能作为动作级安全判断。
        """

        return not isinstance(self._dispatcher, RecordingDispatcher)

    @property
    def aging_interval_seconds(self) -> float:
        """返回内存排序与持久资源队列共享的优先级老化周期。"""

        return self._orderer.aging_interval_seconds

    @property
    def max_in_flight_jobs(self) -> int:
        """返回全局在途作业容量。"""

        return self._max_in_flight_jobs

    @property
    def max_active_tasks(self) -> int:
        """返回全局运行任务容量。"""

        return self._max_active_tasks

    @property
    def max_tasks_per_workflow(self) -> int:
        """返回同一工作流定义的运行任务容量。"""

        return self._max_tasks_per_workflow

    def _emit_monitor(
        self, channel: str, event_type: str, data: dict[str, Any]
    ) -> None:
        if self._monitor is None:
            return
        try:
            self._monitor.emit(channel, event_type, data)
        except Exception:  # noqa: BLE001 - 监控故障不影响调度
            pass

    def _on_material_changed(self, _event: Mapping[str, Any]) -> None:
        """库存提交后异步唤醒等待任务，重新检查库位/物料前置条件。"""

        try:
            # 库存事件可能正由任务完成/结算线程发布，而该线程仍持有调度锁。
            # 只投递、不等待；与 submit/finish 的 ``_wake_reconcile`` 合并到
            # 同一 Future，避免调用方等到空派发，也避免同步等待死锁。
            self._wake_reconcile(wait=False)
        except Exception:
            logger.exception("[EdgeScheduler] material change reconcile failed")

    def close(self) -> None:
        """停止新重排并等待已进入的回调，完成后调用方才能关闭数据库。

        等待 Future 时不持调度锁。调度回调内部不能同步等待自身，
        此时先关闭新准入并明确报错，由外部生命周期所有者重试关闭。
        """
        with self._reconcile_wakeup_lock:
            inside_reschedule = threading.get_ident() in self._active_reschedule_threads
            if inside_reschedule or self._owns_scheduler_lock() or bool(getattr(_RECONCILE_THREAD, "active", False)):
                self._closed = True
                raise RuntimeError("必须在调度回调与调度锁之外等待调度器关闭")
        with self._close_lock:
            with self._reconcile_wakeup_lock:
                self._closed = True
                pending = self._pending_reconcile
            remove_listener = getattr(self._monitor, "remove_listener", None)
            listener_id = self._material_monitor_listener_id
            if callable(remove_listener) and listener_id is not None:
                remove_listener(listener_id)
            self._material_monitor_listener_id = None
            if pending is not None:
                try:
                    pending.result()
                except Exception:
                    # Future 已结束即已越过关闭栅栏；记录原异常，不重复执行回调。
                    logger.exception("关闭时最后一轮调度未成功完成")
            # 公开 reschedule 也可来自 API 线程；准入重试在调度锁外，须单独等待。
            with self._reconcile_condition:
                self._reconcile_condition.wait_for(lambda: not self._active_reschedule_threads)
            with self._lock:
                pass

    def _safe_history(self, method: str, *args: Any, **kwargs: Any) -> None:
        """写执行历史；持久化故障不影响调度。"""
        if self._history is None:
            return
        try:
            getattr(self._history, method)(*args, **kwargs)
        except Exception:
            logger.exception("[EdgeScheduler] history.%s failed", method)

    def set_workflow_state_listener(self, listener: Callable[[str, str], None]) -> None:
        """替换工作流终态监听器；参数 ``listener`` 接收工作流身份和旧状态值。"""

        self._workflow_state_listener = listener

    def add_admission_retry_listener(self, listener: Callable[[], None]) -> None:
        """注册公开重排前的准入重试（AdmissionRetry）监听器。

        参数：``listener`` 负责重试尚未注册到旧调度器的持久任务。返回无；监听器
        异常会关闭失败并阻止本轮旧调度重排。
        """

        self._admission_retry_listeners.append(listener)

    def remove_admission_retry_listener(self, listener: Callable[[], None]) -> None:
        """移除准入重试（AdmissionRetry）监听器。

        参数：``listener`` 必须是此前注册的同一回调。返回无；重复移除保持幂等。
        """

        self._admission_retry_listeners = [
            current
            for current in self._admission_retry_listeners
            if current != listener
        ]

    def bind_dispatch_admission_authority(
        self,
        authority: Callable[[dict[str, Any]], bool],
    ) -> None:
        """绑定唯一持久派发准入权威。

        参数：``authority`` 必须在一个权威事务中复验条件、取得 Claim/Fence 并
        把完整 DispatchPermit 写回派发摘要。返回无。异常：重复绑定或传入不可
        调用对象时抛 ``ExecutionPolicyError``；不允许观察器共享此安全接缝。
        """

        if not callable(authority):
            raise ExecutionPolicyError("持久派发准入权威必须可调用")
        if self._dispatch_admission_authority is not None:
            raise ExecutionPolicyError("持久派发准入权威已经绑定")
        self._dispatch_admission_authority = authority

    def unbind_dispatch_admission_authority(
        self,
        authority: Callable[[dict[str, Any]], bool],
    ) -> None:
        """解绑同一持久准入权威，非当前绑定不得改变安全配置。

        参数：``authority`` 是组合根此前绑定的同一回调。返回无。异常：身份不
        匹配时抛 ``ExecutionPolicyError``，避免错误组件卸掉仍在使用的权威。
        """

        if self._dispatch_admission_authority != authority:
            raise ExecutionPolicyError("解绑的持久派发准入权威身份不匹配")
        self._dispatch_admission_authority = None

    def bind_manual_continuation_authority(
        self,
        authority: Callable[[str], None],
    ) -> None:
        """绑定批准后把同一 Job 推进到物理派发意图的持久权威。"""

        if not callable(authority) or self._manual_continuation_authority is not None:
            raise ExecutionPolicyError("人工确认继续派发权威已经绑定或不可调用")
        self._manual_continuation_authority = authority

    def unbind_manual_continuation_authority(
        self,
        authority: Callable[[str], None],
    ) -> None:
        """只允许组合根解绑此前注册的同一权威。"""

        if self._manual_continuation_authority != authority:
            raise ExecutionPolicyError("人工确认继续派发权威身份不匹配")
        self._manual_continuation_authority = None

    def add_job_execution_wait_listener(
        self,
        listener: Callable[[dict[str, Any]], None],
    ) -> None:
        """注册执行资源忙等待监听器，使内存阻塞也留下持久等待事实。"""

        self._job_execution_wait_listeners.append(listener)

    def remove_job_execution_wait_listener(
        self,
        listener: Callable[[dict[str, Any]], None],
    ) -> None:
        """幂等移除执行资源忙等待监听器。"""

        self._job_execution_wait_listeners = [
            current
            for current in self._job_execution_wait_listeners
            if current != listener
        ]

    def add_job_dispatch_accepted_listener(
        self,
        listener: Callable[[str], None],
    ) -> None:
        """注册执行适配器接受作业后的持久确认监听器。"""

        self._job_dispatch_accepted_listeners.append(listener)

    def remove_job_dispatch_accepted_listener(
        self,
        listener: Callable[[str], None],
    ) -> None:
        """幂等移除执行接受监听器。"""

        self._job_dispatch_accepted_listeners = [
            current
            for current in self._job_dispatch_accepted_listeners
            if current != listener
        ]

    def add_job_dispatch_uncertain_listener(
        self,
        listener: Callable[[str, str], None],
    ) -> None:
        """注册派发边界异常后的物理不确定监听器。"""

        self._job_dispatch_uncertain_listeners.append(listener)

    def remove_job_dispatch_uncertain_listener(
        self,
        listener: Callable[[str, str], None],
    ) -> None:
        """幂等移除物理不确定监听器。"""

        self._job_dispatch_uncertain_listeners = [
            current
            for current in self._job_dispatch_uncertain_listeners
            if current != listener
        ]

    def add_job_cancel_accepted_listener(
        self,
        listener: Callable[[str], None],
    ) -> None:
        """注册本地执行器明确接受取消后的持久投影监听器。"""

        self._job_cancel_accepted_listeners.append(listener)

    def remove_job_cancel_accepted_listener(
        self,
        listener: Callable[[str], None],
    ) -> None:
        """幂等移除本地取消受理监听器。"""

        self._job_cancel_accepted_listeners = [
            current
            for current in self._job_cancel_accepted_listeners
            if current != listener
        ]

    def add_job_cancel_uncertain_listener(
        self,
        listener: Callable[[str, str], None],
    ) -> None:
        """注册取消不能确认时的物理不确定监听器。"""

        self._job_cancel_uncertain_listeners.append(listener)

    def remove_job_cancel_uncertain_listener(
        self,
        listener: Callable[[str, str], None],
    ) -> None:
        """幂等移除取消物理不确定监听器。"""

        self._job_cancel_uncertain_listeners = [
            current
            for current in self._job_cancel_uncertain_listeners
            if current != listener
        ]

    def add_job_cancel_no_send_listener(
        self,
        listener: Callable[[str], None],
    ) -> None:
        """注册可证明未越过物理边界的取消监听器。"""

        self._job_cancel_no_send_listeners.append(listener)

    def remove_job_cancel_no_send_listener(
        self,
        listener: Callable[[str], None],
    ) -> None:
        """幂等移除未发送取消监听器。"""

        self._job_cancel_no_send_listeners = [
            current
            for current in self._job_cancel_no_send_listeners
            if current != listener
        ]

    def add_job_finished_listener(
        self,
        listener: Callable[[str, bool, Any, str], None],
    ) -> None:
        """注册作业完成监听器。

        参数：``listener`` 接收 Job UUID、成功标记、返回值和旧异常决策类型。返回
        无；用于把旧调度结果投影回标准工作流节点作业（WorkflowNodeJob）。
        """

        self._job_finished_listeners.append(listener)

    def add_job_outcome_listener(
        self,
        listener: Callable[[str, CommittedJobOutcome], None],
    ) -> None:
        """注册 Backend-shaped 不可变结果监听器。

        参数：``listener`` 接收作业 UUID 与完整终态证据。返回无。异常：无；实际
        回调异常在结果投影时向上传播，调度器会保留在途状态供投递重放。
        """

        self._job_outcome_listeners.append(listener)

    def add_job_feedback_listener(
        self,
        listener: Callable[[str, dict[str, Any]], None],
    ) -> None:
        """注册 Edge 已持久反馈的标准工作流投影监听器。"""

        self._job_feedback_listeners.append(listener)

    def remove_job_feedback_listener(
        self,
        listener: Callable[[str, dict[str, Any]], None],
    ) -> None:
        """幂等移除作业反馈监听器。"""

        self._job_feedback_listeners = [
            current for current in self._job_feedback_listeners if current != listener
        ]

    def remove_job_finished_listener(
        self,
        listener: Callable[[str, bool, Any, str], None],
    ) -> None:
        """移除作业完成监听器；参数 ``listener`` 必须是此前注册的同一回调。"""

        self._job_finished_listeners = [
            current for current in self._job_finished_listeners if current != listener
        ]

    def remove_job_outcome_listener(
        self,
        listener: Callable[[str, CommittedJobOutcome], None],
    ) -> None:
        """幂等移除不可变结果监听器。

        参数：``listener`` 是此前注册的同一回调。返回无。异常：无；未注册时保持
        原集合不变。
        """

        self._job_outcome_listeners = [
            current for current in self._job_outcome_listeners if current != listener
        ]

    def add_execution_process_restarted_listener(
        self,
        listener: Callable[[tuple[str, ...]], None],
    ) -> None:
        """注册动作执行进程重启后的工作流投影监听器。"""

        self._execution_process_restarted_listeners.append(listener)

    def remove_execution_process_restarted_listener(
        self,
        listener: Callable[[tuple[str, ...]], None],
    ) -> None:
        """幂等移除动作执行进程重启监听器。"""

        self._execution_process_restarted_listeners = [
            current
            for current in self._execution_process_restarted_listeners
            if current != listener
        ]

    def on_execution_process_restarted(
        self,
        job_uuids: tuple[str, ...],
    ) -> None:
        """终止受动作进程重启影响的本地 DAG。

        参数：``job_uuids`` 是动作账本判定已越过派发边界、但无法确认终态的
        作业；空集合仍代表承载这些任务的 Runtime 已崩溃。返回无。异常：持久
        投影监听器异常原样传播。持久失败事实提交后移除在途作业与本地资源锁，
        使旧 Fence 失效，后继节点不再派发。
        """

        affected = tuple(dict.fromkeys(str(job_uuid) for job_uuid in job_uuids))
        with self._lock:
            if self._closed:
                raise RuntimeError("调度器已关闭，执行重启事实须保留待恢复")
            for listener in tuple(self._execution_process_restarted_listeners):
                listener(affected)
            affected_workflow_ids = {
                workflow_id
                for workflow_id, run in self._workflows.items()
                if run.state
                in {
                    WorkflowState.PENDING,
                    WorkflowState.RUNNING,
                    WorkflowState.PAUSED,
                    WorkflowState.WAITING_MATERIAL,
                    WorkflowState.CANCELING,
                }
            } | {job.workflow_id for job in self._inflight.values()}
            for workflow_id in affected_workflow_ids:
                run = self._workflows[workflow_id]
                run.abort_execution_process()
            aborted_job_ids = [
                job_uuid
                for job_uuid, job in self._inflight.items()
                if job.workflow_id in affected_workflow_ids
            ]
            for job_uuid in aborted_job_ids:
                job = self._inflight.pop(job_uuid, None)
                if job is not None:
                    self._record_interval_handoff(job, success=False)
                self._job_resource_locks.pop(job_uuid, None)
                action_trace = self._job_spans.pop(job_uuid, None)
                if action_trace is not None:
                    action_trace.event(
                        "action.runtime_restarted",
                        {"workflow.job.uuid": job_uuid},
                    )
                    action_trace.end()
                if job is not None:
                    self._record_timeline(
                        job,
                        success=False,
                        suc_type="execution_process_restarted",
                        state="failed",
                    )
            for workflow_id in affected_workflow_ids:
                self._clear_interval_holders(
                    workflow_id,
                    preserve_explicit=False,
                )
            notifications = self._collect_terminal_notifications()
        self._fire_notifications(notifications)

    def add_execution_connection_lost_listener(
        self, listener: Callable[[tuple[str, ...]], None]
    ) -> None:
        self._execution_connection_lost_listeners.append(listener)

    def remove_execution_connection_lost_listener(
        self, listener: Callable[[tuple[str, ...]], None]
    ) -> None:
        self._execution_connection_lost_listeners = [
            item
            for item in self._execution_connection_lost_listeners
            if item != listener
        ]

    def on_execution_connection_lost(self, job_uuids: tuple[str, ...]) -> None:
        """断线只打开待确认状态，不冒充运行进程重启。"""
        with self._lock:
            if self._closed:
                raise RuntimeError("调度器已关闭，连接中断事实须保留待恢复")
            for listener in tuple(self._execution_connection_lost_listeners):
                listener(job_uuids)

    def fail_restarted_jobs(self, job_uuids: Sequence[str]) -> list[str]:
        """工作流已失败并释放锁后，把 Edge 账本中的重启占用收成 failed。

        参数：``job_uuids`` 是工作流侧已经失败的作业。返回：执行器实际收口的
        作业身份；没有 Edge 账本能力时为空列表。异常：执行器收口故障原样传播。
        """

        method = getattr(self._dispatcher, "fail_restarted_jobs", None)
        if not callable(method):
            return []
        failed = method(tuple(str(job_uuid) for job_uuid in job_uuids))
        return list(failed or [])

    def replay_persisted_edge_projections(
        self,
        *,
        feedback_listener: Callable[[str, dict[str, Any]], None],
        outcome_listener: Callable[[str, CommittedJobOutcome], None] | None = None,
        finished_listener: Callable[[str, bool, Any, str], None] | None = None,
    ) -> dict[str, int]:
        """把 Edge 本地已提交证据直接重放给持久工作流投影。

        进程重启后内存 DAG 尚未恢复，因此不绕经
        ``on_job_finished`` 的内存在途作业查找。无持久 Edge 发件箱的
        执行后端返回空计数。
        """

        replay = getattr(self._dispatcher, "replay_pending_projections", None)
        if not callable(replay):
            return {"feedback": 0, "outcomes": 0}
        result = replay(
            feedback_listener=feedback_listener,
            outcome_listener=outcome_listener,
            finished_listener=finished_listener,
        )
        if not isinstance(result, Mapping):
            raise ValueError("Edge 重放返回值必须是对象")
        return {
            "feedback": int(result.get("feedback", 0)),
            "outcomes": int(result.get("outcomes", 0)),
        }

    def add_job_settled_listener(
        self,
        listener: Callable[[str, bool, Any, str], None],
    ) -> None:
        """注册 DAG 节点结算监听器；仅在完成事实持久化并更新内存 DAG 后调用。"""

        self._job_settled_listeners.append(listener)

    def remove_job_settled_listener(
        self,
        listener: Callable[[str, bool, Any, str], None],
    ) -> None:
        """幂等移除节点结算监听器。"""

        self._job_settled_listeners = [
            current for current in self._job_settled_listeners if current != listener
        ]

    def add_local_control_listener(
        self,
        listener: Callable[[dict[str, Any]], dict[str, Any] | None],
    ) -> None:
        """注册调度器本地条件结算监听器。"""

        self._local_control_listeners.append(listener)

    def remove_local_control_listener(
        self,
        listener: Callable[[dict[str, Any]], dict[str, Any] | None],
    ) -> None:
        """幂等移除调度器本地条件结算监听器。"""

        self._local_control_listeners = [
            current for current in self._local_control_listeners if current != listener
        ]

    def _notify_job_pre_dispatch(self, dispatching: dict[str, Any]) -> bool:
        """调用唯一持久准入权威；参数是即将越过执行边界的作业摘要。

        返回：权威完成门禁并补入 Command、Claim、Fence、效果身份与参数哈希时
        为真，资源等待时为假。异常：物理执行适配器未装配唯一权威或权威内部
        失败时抛 ``ExecutionPolicyError``/原始异常；内建记录适配器仅生成隔离
        干跑凭据，不会触达设备，也不能用于生产物理派发。
        """

        authority = self._dispatch_admission_authority
        if authority is None:
            if not isinstance(self._dispatcher, RecordingDispatcher):
                raise ExecutionPolicyError("持久派发准入权威未装配")
            # ``dry_run_identity`` 只让记录适配器走完调度观察路径，不代表持久占用。
            dry_run_identity = str(dispatching.get("job_id") or "")
            dispatching.update(
                {
                    "attempt": 1,
                    "command_uuid": str(
                        uuid_mod.uuid5(
                            uuid_mod.NAMESPACE_URL,
                            "unilabos-dry-run-command:" + dry_run_identity,
                        )
                    ),
                    "claim_uuid": str(
                        uuid_mod.uuid5(
                            uuid_mod.NAMESPACE_URL,
                            "unilabos-dry-run-claim:" + dry_run_identity,
                        )
                    ),
                    "fences": [],
                    "effect_uuid": str(
                        uuid_mod.uuid5(
                            uuid_mod.NAMESPACE_URL,
                            "unilabos-dry-run-effect:" + dry_run_identity,
                        )
                    ),
                    "parameter_hash": "dry-run",
                    "expected_change_set": {"kind": "dry_run"},
                }
            )
            return True
        return authority(dispatching)

    def _notify_job_execution_wait(self, waiting: dict[str, Any]) -> None:
        """在内存设备锁或动作物料锁先命中时同步持久化等待顺序。"""

        for listener in tuple(self._job_execution_wait_listeners):
            listener(dict(waiting))

    def _notify_job_dispatch_accepted(self, job_id: str) -> None:
        """在执行适配器返回接受后同步推进持久作业与租约。"""

        with self._lock:
            if self._closed:
                raise RuntimeError("调度器已关闭，开始回执须保留待恢复")
            for listener in tuple(self._job_dispatch_accepted_listeners):
                listener(job_id)

    def _notify_job_dispatch_uncertain(self, job_id: str, reason: str) -> None:
        """在派发意图已提交但适配器未明确返回时记录物理不确定事实。"""

        for listener in tuple(self._job_dispatch_uncertain_listeners):
            listener(job_id, reason)

    def _notify_job_cancel_accepted(self, job_id: str) -> None:
        """记录执行器已接受取消；该事实不释放在途作业或资源锁。"""

        with self._lock:
            if self._closed:
                raise RuntimeError("调度器已关闭，取消回执须保留待恢复")
            for listener in tuple(self._job_cancel_accepted_listeners):
                listener(job_id)

    def _notify_job_cancel_uncertain(self, job_id: str, reason: str) -> None:
        """把取消拒绝、能力缺失或边界异常通知为物理不确定事实。"""

        with self._lock:
            if self._closed:
                raise RuntimeError("调度器已关闭，取消不确定事实须保留待恢复")
            for listener in tuple(self._job_cancel_uncertain_listeners):
                listener(job_id, reason)

    def _notify_job_cancel_no_send(self, job_id: str) -> None:
        """通知持久层该作业可证明从未越过物理执行边界。"""

        for listener in tuple(self._job_cancel_no_send_listeners):
            listener(job_id)

    def _notify_job_finished(
        self,
        job_id: str,
        success: bool,
        ret_value: Any,
        suc_type: str,
    ) -> None:
        """在清理本地在途状态前通知一次完成事实。

        参数分别是作业身份、成功标记、设备返回值和旧异常决策类型。返回无；投影
        失败向上抛出，使同一完成事实可以投递重放（DeliveryReplay）；调用方不得
        在全部监听器确认前释放在途作业或动作物料锁（Action Material Lock）。
        """

        for listener in tuple(self._job_finished_listeners):
            listener(job_id, success, ret_value, suc_type)

    def _notify_job_outcome(
        self,
        job_id: str,
        outcome: CommittedJobOutcome,
    ) -> None:
        """在释放在途状态前投递完整不可变结果。

        参数：``job_id`` 是作业身份；``outcome`` 是 Edge 已提交终态与证据。返回
        无。异常：任一持久投影失败时原样传播，调用方不得释放在途作业和占用。
        """

        for listener in tuple(self._job_outcome_listeners):
            listener(job_id, outcome)

    def on_job_feedback(self, job_id: str, sample: dict[str, Any]) -> None:
        """把 Edge 已提交反馈同步投递给工作流持久投影。"""

        with self._lock:
            if self._closed:
                raise RuntimeError("调度器已关闭，反馈须保留待恢复")
            for listener in tuple(self._job_feedback_listeners):
                listener(job_id, dict(sample))

    def _notify_job_settled(
        self,
        job_id: str,
        success: bool,
        ret_value: Any,
        suc_type: str,
    ) -> None:
        """在持久完成事实和 DAG 结算都成立后通知后继控制逻辑。"""

        for listener in tuple(self._job_settled_listeners):
            listener(job_id, success, ret_value, suc_type)

    def _notify_local_control(self, evaluation: dict[str, Any]) -> dict[str, Any]:
        """同步投影本地控制事实，并返回权威确认或改写后的决定。"""

        effective = deepcopy(evaluation)
        for listener in tuple(self._local_control_listeners):
            projected = listener(deepcopy(effective))
            if isinstance(projected, dict):
                effective = deepcopy(projected)
        return effective

    # ── 触发点 1：任务进来 ────────────────────────────────────

    def submit_workflow(
        self,
        spec: WorkflowSpec,
        *,
        execution_mode: str | None = None,
        dispatch_gate: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            if self._closed:
                raise RuntimeError("调度器已关闭，不能接收新工作流")
            if (
                spec.workflow_id in self._workflows
                or spec.workflow_id in self._workflow_spans
            ):
                raise ValueError(f"workflow {spec.workflow_id} already submitted")
            workflow_trace = start_detached_span(
                "workflow.task.run",
                attributes={
                    "workflow.uuid": spec.workflow_id,
                    "workflow.task.uuid": spec.task_id,
                    "lab.id": spec.lab_id,
                    "workflow.plan.node_count": len(spec.nodes),
                    "workflow.priority": str(spec.priority),
                },
                parent_context=extract_trace_context(
                    getattr(spec, "trace_context", {})
                ),
            )
            # 先登记 span 也充当 submit 占位，避免并发同 ID 覆盖对方的追踪句柄。
            self._workflow_spans[spec.workflow_id] = workflow_trace
        try:
            with (
                workflow_trace.activate(),
                span(
                    "workflow.task.submit",
                    attributes={
                        "workflow.uuid": spec.workflow_id,
                        "workflow.task.uuid": spec.task_id,
                    },
                ),
            ):
                result = self._submit_workflow(
                    spec,
                    execution_mode=execution_mode,
                    dispatch_gate=dispatch_gate,
                )
                trace_context = getattr(workflow_trace, "trace_context", None)
                result["trace_context"] = (
                    trace_context() if callable(trace_context) else {}
                )
                return result
        except BaseException as exc:
            workflow_trace.fail(exc)
            workflow_trace.end()
            self._workflow_spans.pop(spec.workflow_id, None)
            raise

    def _submit_workflow(
        self,
        spec: WorkflowSpec,
        *,
        execution_mode: str | None = None,
        dispatch_gate: str | None = None,
    ) -> dict[str, Any]:
        """提交工作流并立即重排。返回本次下发结果。

        带物料需求时：入队前整 DAG all-or-nothing 预留；不足则置
        ``waiting_for_material``（不进入执行队列，后续每次重排自动重试）。
        """
        with self._lock:
            if spec.workflow_id in self._workflows:
                raise ValueError(f"workflow {spec.workflow_id} already submitted")
            self._validate_station_admission(spec)
            run = WorkflowRun(
                spec,
                execution_mode=execution_mode,
                dispatch_gate=dispatch_gate,
            )  # 构图 + 环检测，失败直接抛
            self._workflows[spec.workflow_id] = run
            self._serial_admissions.pop(spec.workflow_id, None)

            requirements = spec.material_requirements_by_node()
            if requirements and run.state is not WorkflowState.SUCCESS:
                if self._inventory is None:
                    logger.warning(
                        "[EdgeScheduler] workflow %s declares materials but no inventory "
                        "service wired; proceeding without reservation",
                        spec.workflow_id,
                    )
                else:
                    self._material_workflows.add(spec.workflow_id)
                    waiting_for_turn = self._serial_queue and self._select_serial_workflow() != spec.workflow_id
                    if waiting_for_turn:
                        run.state = WorkflowState.PENDING
                    elif not self._try_reserve(run):
                        run.state = WorkflowState.WAITING_MATERIAL

            logger.info(
                "[EdgeScheduler] workflow %s submitted (%d nodes, state=%s), reschedule",
                spec.workflow_id,
                len(spec.nodes),
                run.state.value,
            )
            self._emit_monitor(
                "scheduler",
                "workflow_submitted",
                {
                    "workflow_id": spec.workflow_id,
                    "nodes": len(spec.nodes),
                    "state": run.state.value,
                    "priority": str(spec.priority),
                },
            )
            self._safe_history("record_submitted", spec, run.state.value)
        # 首次提交与设备完成后的后续推进必须进入同一个串行调度循环。API 工作
        # 线程只登记 WorkflowRun 并唤醒循环，不能在请求线程直接完成资源判定和
        # 物理派发，否则并发 Task 会让调度线程亲和性失效。为保持既有返回合同，
        # 当前调用方仍等待该串行轮次完成，但等待不占用 Scheduler 执行线程。
        dispatched = self._wake_reconcile()
        with self._lock:
            notifications = self._collect_terminal_notifications()
            state = run.state.value
        self._fire_notifications(notifications)
        return {
            "workflow_id": spec.workflow_id,
            "state": state,
            "execution_mode": run.execution_mode,
            "dispatch_gate": run.dispatch_gate,
            "dispatched": dispatched,
        }

    def step_workflow(
        self,
        workflow_id: str,
        target_node_id: str | None = None,
    ) -> dict[str, Any]:
        """让暂停的单步工作流只派发一个就绪节点，随后立即恢复暂停。

        参数：``workflow_id`` 是已提交运行身份；``target_node_id`` 可指定本次
        必须放行的就绪节点，空值按稳定图顺序选择第一个。返回本轮派发摘要。
        异常：未知任务、非单步任务、非暂停状态或目标尚未就绪时抛 ``ValueError``。
        """

        with self._lock:
            run = self._workflows.get(workflow_id)
            if run is None:
                raise ValueError(f"workflow {workflow_id} not found")
            if run.execution_mode != "step":
                raise ValueError(f"workflow {workflow_id} is not in step mode")
            if self._serial_queue and self._select_serial_workflow() != workflow_id:
                raise ValueError(f"工作流 {workflow_id} 尚未轮到执行")
            if run.state is not WorkflowState.PAUSED:
                raise ValueError(f"workflow {workflow_id} is not paused")
            if self._workflow_in_flight_count_locked(workflow_id):
                raise ValueError(f"workflow {workflow_id} step is still in progress")

            ready_nodes = self._step_ready_nodes_locked(run)
            if target_node_id is None:
                if len(ready_nodes) > 1:
                    raise ValueError("multiple step targets require explicit selection")
                selected = ready_nodes[0] if ready_nodes else None
            else:
                selected = next(
                    (node for node in ready_nodes if node.id == target_node_id),
                    None,
                )
            if selected is None:
                run.state = WorkflowState.PAUSED
                raise ValueError("step target is not ready: resource_order 排程或前置资源尚未就绪")

            self._step_targets[workflow_id] = selected.id
            try:
                # READY 候选确认后才临时打开同一调度循环；finally 会在本步的
                # 本地控制结算或动作派发完成后重新关闭闸门。
                run.state = WorkflowState.RUNNING
                dispatched = self._reschedule_locked()
            finally:
                self._step_targets.pop(workflow_id, None)
                if run.state is WorkflowState.RUNNING:
                    run.state = WorkflowState.PAUSED
            self._advance_step_repeat_transitions_locked(run)
            notifications = self._collect_terminal_notifications()
            if self._serial_queue:
                dispatched = [item for item in dispatched if item["workflow_id"] == workflow_id]
            result = {
                "workflow_id": workflow_id,
                "state": run.state.value,
                "dispatched": dispatched,
            }
        self._fire_notifications(notifications)
        return result

    def step_state(self, workflow_id: str) -> dict[str, Any]:
        """返回单步控制所需的后端权威候选与在途状态。"""

        with self._lock:
            run = self._workflows.get(workflow_id)
            if run is None:
                raise ValueError(f"workflow {workflow_id} not found")
            in_flight = self._workflow_in_flight_count_locked(workflow_id)
            can_select = (
                run.execution_mode == "step"
                and run.state is WorkflowState.PAUSED
                and in_flight == 0
                and (not self._serial_queue or self._select_serial_workflow() == workflow_id)
            )
            candidates = self._step_ready_nodes_locked(run) if can_select else []
            return {
                "workflow_id": workflow_id,
                "state": run.state.value,
                "execution_mode": run.execution_mode,
                "in_flight_job_count": in_flight,
                "requires_selection": len(candidates) > 1,
                "can_step": bool(candidates),
                "blocked_candidates": [],
                "candidates": [
                    {
                        "node_id": node.id,
                        "executor_kind": node.executor_kind,
                        "node_type": node.node_type,
                        "device_id": node.device_id,
                        "action_name": node.action_name,
                    }
                    for node in candidates
                ],
            }

    def switch_to_step(self, workflow_id: str) -> dict[str, Any]:
        """停止新的自动派发，并在当前 Task 的在途 Job 排空后进入单步。"""

        with self._lock:
            run = self._workflows.get(workflow_id)
            if run is None:
                raise ValueError(f"workflow {workflow_id} not found")
            if run.execution_mode != "normal":
                raise ValueError(f"workflow {workflow_id} is not in normal mode")
            if run.state in self._TERMINAL_STATES:
                raise ValueError(f"workflow {workflow_id} is terminal")
            run.execution_mode = "switching_to_step"
            run.dispatch_gate = "paused"
            run.state = WorkflowState.PAUSED
            self._complete_step_transition_locked(run)
            return self.step_state(workflow_id)

    def continue_automatic(self, workflow_id: str) -> dict[str, Any]:
        """从稳定单步暂停态恢复同一 WorkflowRun 的自动调度。"""

        with self._lock:
            run = self._workflows.get(workflow_id)
            if run is None:
                raise ValueError(f"workflow {workflow_id} not found")
            if run.execution_mode != "step":
                raise ValueError(f"workflow {workflow_id} is not in step mode")
            if run.state is not WorkflowState.PAUSED:
                raise ValueError(f"workflow {workflow_id} is not paused")
            if self._workflow_in_flight_count_locked(workflow_id):
                raise ValueError(f"workflow {workflow_id} step is still in progress")
            run.execution_mode = "normal"
            run.dispatch_gate = "active"
            run.state = WorkflowState.RUNNING
            dispatched = self._reschedule_locked()
            notifications = self._collect_terminal_notifications()
            result = {
                "workflow_id": workflow_id,
                "state": run.state.value,
                "execution_mode": run.execution_mode,
                "dispatch_gate": run.dispatch_gate,
                "dispatched": dispatched,
            }
        self._fire_notifications(notifications)
        return result

    def _workflow_in_flight_count_locked(self, workflow_id: str) -> int:
        """统计指定 WorkflowRun 尚未结算的 Job；调用方必须持有调度锁。"""

        return sum(
            job.workflow_id == workflow_id for job in self._inflight.values()
        )

    def _step_ready_nodes_locked(self, run: WorkflowRun) -> list[WorkflowNode]:
        """在不打开派发闸门的前提下读取一个暂停运行的 DAG 候选。"""

        previous_state = run.state
        run.state = WorkflowState.RUNNING
        try:
            return list(run.ready_nodes())
        finally:
            run.state = previous_state

    def _commit_prepared_local_control_locked(
        self,
        run: WorkflowRun,
        evaluation: Mapping[str, Any],
    ) -> dict[str, Any]:
        """先持久化一个本地控制决定，再提交同一份内存 DAG 事实。"""

        control_node = run.node(str(evaluation["node_id"]))
        event = {
            **evaluation,
            "workflow_id": run.spec.workflow_id,
            "job_id": control_node.job_id,
            "skipped_jobs": [
                {
                    "node_id": node_id,
                    "job_id": run.node(node_id).job_id,
                }
                for node_id in evaluation.get("skipped_node_ids", ())
            ],
        }
        event = self._notify_local_control(event)
        if event.get("pending"):
            return event
        if event.get("error") and self._station_control is not None:
            run.hold_error(str(event["node_id"]))
        else:
            run.commit_local_control(event)
        self._release_completed_interval_holders(run.spec.workflow_id)
        return event

    def _advance_step_repeat_transitions_locked(self, run: WorkflowRun) -> None:
        """自动结算 Step 循环轮末 until，并在 false 时惰性物化下一轮。

        RepeatUntil 容器首次进入仍是一个用户 Step，循环体中的可见节点也逐个
        放行；这里只跨过不发送设备命令的轮末判断，以及该判断为 false 时紧邻
        的下一轮物化。新的独立循环容器或条件节点不会被顺带消费。
        """

        if (
            run.execution_mode != "step"
            or run.state is not WorkflowState.PAUSED
            or self._workflow_in_flight_count_locked(run.spec.workflow_id)
        ):
            return
        previous_state = run.state
        run.state = WorkflowState.RUNNING
        allow_continuation_materialization = False
        try:
            while run.state is WorkflowState.RUNNING:
                evaluation = run.prepare_local_control()
                if evaluation is None or evaluation.get("control_type") != "repeat_until":
                    break
                phase = str(evaluation.get("phase") or "")
                if phase == "materialize" and not allow_continuation_materialization:
                    break
                if phase not in {"evaluate", "materialize"}:
                    break
                event = self._commit_prepared_local_control_locked(run, evaluation)
                if phase == "materialize":
                    break
                allow_continuation_materialization = (
                    not event.get("error")
                    and event.get("condition_result") is False
                )
        finally:
            if run.state is WorkflowState.RUNNING:
                run.state = previous_state

    def _complete_step_transition_locked(self, run: WorkflowRun) -> None:
        """在最后一个在途 Job 结算后原子完成 normal→step 排空。"""

        if (
            run.execution_mode == "switching_to_step"
            and self._workflow_in_flight_count_locked(run.spec.workflow_id) == 0
        ):
            run.execution_mode = "step"
            run.dispatch_gate = "paused"
            if run.state not in self._TERMINAL_STATES:
                run.state = WorkflowState.PAUSED

    def pause_workflow(self, workflow_id: str) -> dict[str, Any]:
        """暂停普通任务的后续派发，不改变其执行模式或在途 Job。"""

        with self._lock:
            run = self._workflows.get(workflow_id)
            if run is None:
                raise ValueError(f"workflow {workflow_id} not found")
            if run.execution_mode != "normal":
                raise ValueError(f"workflow {workflow_id} is not in normal mode")
            if run.state in self._TERMINAL_STATES:
                raise ValueError(f"workflow {workflow_id} is terminal")
            run.dispatch_gate = "paused"
            return {
                "workflow_id": workflow_id,
                "state": run.state.value,
                "execution_mode": run.execution_mode,
                "dispatch_gate": run.dispatch_gate,
                "in_flight_job_count": self._workflow_in_flight_count_locked(
                    workflow_id
                ),
                "dispatched": [],
            }

    def update_pending_job_param(
        self,
        workflow_id: str,
        node_id: str,
        job_id: str,
        param: Mapping[str, Any],
    ) -> dict[str, Any]:
        """更新暂停任务中尚未派发节点的参数，并使内存运行态立即生效。

        参数：``workflow_id``、``node_id`` 和 ``job_id`` 是同一冻结任务中的稳定
        身份；``param`` 是已经合并好的最终参数对象。返回旧参数与新参数的副本，
        供桥接层在持久化失败时回滚。异常：任务不存在、任务未暂停、节点已经进入
        在途/终态或身份不一致时抛 ``ValueError``。此入口只修改动作参数，不改变
        DAG 节点、边、资源计划或作业身份。
        """

        if not isinstance(param, Mapping):
            raise ValueError("作业参数必须是对象")
        with self._lock:
            run = self._workflows.get(workflow_id)
            if run is None:
                raise ValueError(f"workflow {workflow_id} not found")
            if run.dispatch_gate != "paused" or run.execution_mode != "normal":
                raise ValueError("任务必须先暂停才能修改参数")
            if job_id in self._inflight:
                raise ValueError("作业已经下发，不能修改参数")
            node = run.node(node_id)
            if node is None:
                raise ValueError(f"workflow node {node_id} not found")
            if node.job_id and node.job_id != job_id:
                raise ValueError("作业与工作流节点身份不一致")
            state = run.node_state(node_id)
            if state not in {NodeState.PENDING, NodeState.READY}:
                raise ValueError("作业已经离开待运行状态，不能修改参数")
            previous = deepcopy(node.param)
            node.param = _merge_param_patch(node.param, param)
            return {"previous": previous, "param": deepcopy(node.param)}

    def resume_workflow(self, workflow_id: str) -> dict[str, Any]:
        """恢复普通任务的后续派发，继续复用同一 WorkflowRun。"""

        with self._lock:
            run = self._workflows.get(workflow_id)
            if run is None:
                raise ValueError(f"workflow {workflow_id} not found")
            if run.execution_mode != "normal":
                raise ValueError(f"workflow {workflow_id} is not in normal mode")
            if run.state in self._TERMINAL_STATES:
                raise ValueError(f"workflow {workflow_id} is terminal")
            run.dispatch_gate = "active"
            if run.state is WorkflowState.PAUSED:
                run.state = WorkflowState.RUNNING
            dispatched = self._reschedule_locked()
            notifications = self._collect_terminal_notifications()
            result = {
                "workflow_id": workflow_id,
                "state": run.state.value,
                "execution_mode": run.execution_mode,
                "dispatch_gate": run.dispatch_gate,
                "in_flight_job_count": self._workflow_in_flight_count_locked(
                    workflow_id
                ),
                "dispatched": dispatched,
            }
        self._fire_notifications(notifications)
        return result

    def restore_workflow(
        self,
        spec: WorkflowSpec,
        completed_results: dict[str, Any],
        restored_jobs: Sequence[DispatchedJob] = (),
        skipped_nodes: Mapping[str, str] | None = None,
        restored_interval_handoffs: Sequence[Mapping[str, Any]] = (),
        execution_facts: Sequence[Mapping[str, Any]] = (),
        execution_mode: str | None = None,
        dispatch_gate: str | None = None,
    ) -> dict[str, Any]:
        """从持久成功事实恢复一个未终态工作流（Workflow）。

        参数：``spec`` 是原任务冻结规格；``completed_results`` 按节点
        UUID 提供已持久成功的返回值。返回：恢复后状态与本轮新派
        发摘要。异常：未知完成节点、重复运行或派发失败原样传播；
        已完成节点只恢复 DAG 状态，绝不重放设备动作；
        ``restored_interval_handoffs`` 是成功 Job 持久化的连续区间元数据，
        仅用于重建内存所有权，不会重复发送设备命令。
        """

        skipped_nodes = dict(skipped_nodes or {})
        completed_node_ids = set(completed_results)
        known_node_ids = {node.id for node in spec.nodes if not node.disabled}
        unknown_node_ids = (completed_node_ids | set(skipped_nodes)) - known_node_ids
        if unknown_node_ids:
            raise ValueError(
                f"workflow {spec.workflow_id} has unknown completed nodes: "
                f"{sorted(unknown_node_ids)}"
            )
        with self._lock:
            if spec.workflow_id in self._workflows or spec.workflow_id in self._workflow_spans:
                raise ValueError(f"workflow {spec.workflow_id} already submitted")
            workflow_trace = start_detached_span(
                "workflow.task.run",
                attributes={
                    "workflow.uuid": spec.workflow_id,
                    "workflow.task.uuid": spec.task_id,
                    "workflow.plan.node_count": len(spec.nodes),
                    "workflow.recovered.node_count": len(completed_results),
                },
                parent_context=extract_trace_context(spec.trace_context),
            )
            self._workflow_spans[spec.workflow_id] = workflow_trace
        reconcile_started = False
        had_material_reservation = False
        try:
            with workflow_trace.activate(), self._lock:
                self._validate_station_admission(spec)
                run = WorkflowRun(
                    spec,
                    execution_mode=execution_mode,
                    dispatch_gate=dispatch_gate,
                )
                self._workflows[spec.workflow_id] = run
                self._serial_admissions.pop(spec.workflow_id, None)
                # 在途作业仍按原路径恢复预留，其余任务等成为队首再申请库存。
                defer_reservation = self._serial_queue and not restored_jobs
                requirements = spec.material_requirements_by_node()
                if requirements and self._inventory is not None and run.state is not WorkflowState.SUCCESS:
                    self._material_workflows.add(spec.workflow_id)
                    if not defer_reservation and not self._try_reserve(run):
                        run.state = WorkflowState.WAITING_MATERIAL
                for node in spec.nodes if not execution_facts else ():
                    if node.id in completed_results:
                        run.mark_finished(node.id, completed_results[node.id])
                    elif node.id in skipped_nodes:
                        run.mark_skipped(node.id, reason=skipped_nodes[node.id])
                if execution_facts:
                    run.restore_execution_facts(execution_facts)
                self._restore_interval_handoffs(
                    spec,
                    completed_results,
                    restored_interval_handoffs,
                    execution_facts=execution_facts,
                )
                for restored_job in restored_jobs:
                    node = run.node(restored_job.node_id)
                    if execution_facts:
                        self._inflight[restored_job.job_id] = restored_job
                        if restored_job.resource_lock_keys:
                            self._job_resource_locks[restored_job.job_id] = set(restored_job.resource_lock_keys)
                        if node is not None and restored_job.job_id == node.job_id:
                            self._open_resource_intervals_for_job(run, restored_job)
                        continue
                    if node is None or not node.waits_for_manual_confirmation():
                        raise ValueError("只允许恢复尚未越过设备边界的人工确认作业")
                    self._validate_restored_manual_job_identity(spec, node, restored_job)
                    if restored_job.node_id in completed_results:
                        raise ValueError("已完成节点不能同时恢复为人工确认等待")
                    if restored_job.job_id in self._inflight:
                        raise ValueError("恢复的人工确认作业身份重复")
                    self._refresh_job_active_use(
                        run,
                        node,
                        restored_job,
                        require_all_plan_keys=True,
                    )
                    conflict = self._inflight_job_conflict(restored_job)
                    if conflict is not None:
                        other_job, active_conflicts, device_conflict = conflict
                        reasons = sorted(active_conflicts)
                        if device_conflict:
                            reasons.append(
                                device_lock_key(
                                    restored_job.device_material_uuid
                                    or restored_job.device_id
                                )
                            )
                        raise ExecutionPolicyError(
                            "恢复的人工确认作业与在途作业资源冲突："
                            f"{restored_job.job_id}->{other_job.job_id}:"
                            + ",".join(reasons)
                        )
                    run.mark_dispatched(restored_job.node_id)
                    self._inflight[restored_job.job_id] = restored_job
                    self._open_resource_intervals_for_job(run, restored_job)
                    if restored_job.resource_lock_keys:
                        self._job_resource_locks[restored_job.job_id] = set(
                            restored_job.resource_lock_keys
                        )
                if (
                    defer_reservation
                    and spec.workflow_id in self._material_workflows
                    and run.state in {WorkflowState.RUNNING, WorkflowState.PAUSED}
                ):
                    run.state = WorkflowState.PENDING
                logger.info(
                    "[EdgeScheduler] workflow %s restored (%d/%d nodes completed)",
                    spec.workflow_id,
                    len(completed_results),
                    len(spec.nodes),
                )
                self._emit_monitor(
                    "scheduler",
                    "workflow_restored",
                    {
                        "workflow_id": spec.workflow_id,
                        "completed_nodes": len(completed_results),
                        "nodes": len(spec.nodes),
                        "state": run.state.value,
                    },
                )
                reconcile_started = True
                dispatched = self._reschedule_locked()
                notifications = self._collect_terminal_notifications()
            self._fire_notifications(notifications)
            result = {
                "workflow_id": spec.workflow_id,
                "state": run.state.value,
                "execution_mode": run.execution_mode,
                "dispatch_gate": run.dispatch_gate,
                "dispatched": dispatched,
            }
            result["trace_context"] = workflow_trace.trace_context()
            return result
        except BaseException as exc:
            if not reconcile_started:
                with self._lock:
                    self._workflows.pop(spec.workflow_id, None)
                    self._clear_interval_holders(spec.workflow_id)
                    self._resource_plans.pop(spec.workflow_id, None)
                    self._step_targets.pop(spec.workflow_id, None)
                    had_material_reservation = (
                        spec.workflow_id in self._material_workflows
                    )
                    self._material_workflows.discard(spec.workflow_id)
                    self._notified_workflows.discard(spec.workflow_id)
                    for job_id, job in tuple(self._inflight.items()):
                        if job.workflow_id != spec.workflow_id:
                            continue
                        self._inflight.pop(job_id, None)
                        self._job_resource_locks.pop(job_id, None)
                        action_trace = self._job_spans.pop(job_id, None)
                        if action_trace is not None:
                            action_trace.end()
                if had_material_reservation:
                    self._safe_inventory_call(
                        "release_workflow",
                        spec.workflow_id,
                        reason="workflow_restore_rejected",
                    )
            workflow_trace.fail(exc)
            workflow_trace.end()
            self._workflow_spans.pop(spec.workflow_id, None)
            raise

    def _restore_interval_handoffs(
        self,
        spec: WorkflowSpec,
        completed_results: Mapping[str, Any],
        handoffs: Sequence[Mapping[str, Any]],
        *, execution_facts: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        """从已持久化成功 Job 的区间元数据恢复内存连续所有权。"""

        plan = self._resource_plan_for_spec(spec)
        if plan is None:
            if handoffs:
                raise ExecutionPolicyError("恢复的连续资源交接缺少冻结资源计划")
            return
        intervals = {str(item.interval_id): item for item in plan.intervals}
        successful = {str(node_id) for node_id in completed_results}
        facts_by_job = {str(job["uuid"]): job for job in execution_facts}
        resources = {item.resource_id: item for item in plan.resources}
        run = self._workflows[spec.workflow_id]

        def interval_is_settled(interval: Any) -> bool:
            return all(
                self._resource_interval_member_completed(run, member)
                for member in interval.node_uuids
            )

        physical_completed = {
            node.id
            for node in spec.nodes
            if node.id in successful and self._is_physical_resource_node(node)
        }
        opened = {
            (spec.workflow_id, interval_id)
            for interval_id, interval in intervals.items()
            if physical_completed & set(interval.node_uuids)
            and not interval_is_settled(interval)
        }
        restored_holders: dict[tuple[str, str], set[str]] = {}
        restored_holder_jobs: dict[tuple[str, str], set[str]] = {}

        for interval_id, interval in intervals.items():
            if interval.resource_id not in resources:
                raise ExecutionPolicyError(
                    "恢复的连续资源交接引用缺失资源：" + interval_id
                )
        for raw in handoffs:
            if not isinstance(raw, Mapping):
                raise ExecutionPolicyError("恢复的连续资源交接记录必须是对象")
            node_id = str(raw.get("node_id") or "").strip()
            job_id = str(raw.get("job_id") or "").strip()
            fact = facts_by_job.get(job_id)
            if execution_facts:
                if (fact is None or fact["status"] not in {"succeeded", "failed", "timeout"}
                        or fact.get("uncertainty_reason") or node_id != str((fact.get("control_data") or {}).get("runtime_node_id") or fact["workflow_node_uuid"])):
                    raise ExecutionPolicyError("恢复的连续资源所有权缺少明确结束的执行事实")
                template_node_id = str(fact["workflow_node_uuid"])
            else:
                if not node_id or not job_id or node_id not in successful:
                    raise ExecutionPolicyError("恢复的连续资源交接缺少成功 Job 身份")
                template_node_id = node_id
            released_keys = set(raw.get("operator_released_keys") or [])

            raw_interval_ids = raw.get("resource_interval_ids")
            if not isinstance(raw_interval_ids, Sequence) or isinstance(
                raw_interval_ids, (str, bytes)
            ):
                raise ExecutionPolicyError("恢复的连续资源交接区间集合非法")
            interval_ids = {str(value).strip() for value in raw_interval_ids}
            if not interval_ids or "" in interval_ids:
                raise ExecutionPolicyError("恢复的连续资源交接区间集合非法")
            for interval_id in interval_ids:
                interval = intervals.get(interval_id)
                if interval is None:
                    raise ExecutionPolicyError(
                        "恢复的连续资源交接引用未计划区间：" + interval_id
                    )
                if template_node_id not in interval.node_uuids:
                    raise ExecutionPolicyError(
                        f"恢复的连续资源交接节点不属于区间：{node_id}/{interval_id}"
                    )
            expected_node_interval_ids = {
                interval_id
                for interval_id, interval in intervals.items()
                if template_node_id in interval.node_uuids
            }
            if interval_ids != expected_node_interval_ids:
                raise ExecutionPolicyError("恢复的连续资源交接区间集合与冻结计划不一致")

            raw_by_lock = raw.get("resource_interval_ids_by_lock")
            if not isinstance(raw_by_lock, Mapping):
                raise ExecutionPolicyError("恢复的连续资源交接锁映射非法")
            actual_by_lock: dict[str, set[str]] = {}
            for raw_key, raw_ids in raw_by_lock.items():
                lock_key = str(raw_key).strip()
                if (
                    not lock_key
                    or not isinstance(raw_ids, Sequence)
                    or isinstance(raw_ids, (str, bytes))
                ):
                    raise ExecutionPolicyError("恢复的连续资源交接锁映射非法")
                mapped_interval_ids = {str(value).strip() for value in raw_ids}
                if not mapped_interval_ids or "" in mapped_interval_ids:
                    raise ExecutionPolicyError("恢复的连续资源交接锁映射非法")
                for interval_id in mapped_interval_ids:
                    interval = intervals.get(interval_id)
                    if interval is None:
                        raise ExecutionPolicyError(
                            "恢复的连续资源交接锁映射引用未计划区间：" + interval_id
                        )
                    if interval_id not in interval_ids:
                        raise ExecutionPolicyError(
                            "恢复的连续资源交接锁映射引用未声明区间：" + interval_id
                        )
                    resource = resources[interval.resource_id]
                    expected_lock_key = _bound_resource_lock_key(
                        {
                            "canonical_key": resource.canonical_key,
                            "kind": resource.kind,
                            "instance_uuid": resource.instance_uuid,
                        }
                    )
                    if not expected_lock_key or lock_key != expected_lock_key:
                        raise ExecutionPolicyError(
                            "恢复的连续资源交接锁键偏离冻结计划：" + lock_key
                        )
                actual_by_lock.setdefault(lock_key, set()).update(mapped_interval_ids)

            active_interval_ids = {
                interval_id
                for interval_id in interval_ids
                if not interval_is_settled(intervals[interval_id])
            }
            expected_active_by_lock: dict[str, set[str]] = {}
            for interval_id in active_interval_ids:
                interval = intervals[interval_id]
                resource = resources[interval.resource_id]
                lock_key = _bound_resource_lock_key(
                    {
                        "canonical_key": resource.canonical_key,
                        "kind": resource.kind,
                        "instance_uuid": resource.instance_uuid,
                    }
                )
                if not lock_key:
                    raise ExecutionPolicyError(
                        "恢复的连续资源交接资源缺少规范锁键：" + interval_id
                    )
                if lock_key not in released_keys:
                    expected_active_by_lock.setdefault(lock_key, set()).add(interval_id)
            actual_active_by_lock = {
                lock_key: mapped_ids & active_interval_ids
                for lock_key, mapped_ids in actual_by_lock.items()
                if mapped_ids & active_interval_ids and lock_key not in released_keys
            }
            if actual_active_by_lock != expected_active_by_lock:
                raise ExecutionPolicyError("恢复的连续资源交接锁映射与冻结计划不一致")

            for lock_key, active_ids in expected_active_by_lock.items():
                for interval_id in active_ids:
                    key = (spec.workflow_id, interval_id)
                    opened.add(key)
                    restored_holders.setdefault(key, set()).add(lock_key)
                    restored_holder_jobs.setdefault(key, set()).add(job_id)

        self._opened_resource_intervals.update(opened)
        for key, lock_keys in restored_holders.items():
            self._interval_resource_holders.setdefault(key, set()).update(lock_keys)
        for key, job_ids in restored_holder_jobs.items():
            self._interval_resource_holder_jobs.setdefault(key, set()).update(job_ids)

    def _try_reserve(self, run: WorkflowRun) -> bool:
        """尝试整 DAG 预留；不足返回 False（幂等，可反复重试）。"""
        try:
            self._inventory.reserve_workflow(
                run.spec.workflow_id, run.spec.material_requirements_by_node()
            )
            return True
        except InsufficientStock as exc:
            logger.info(
                "[EdgeScheduler] workflow %s waiting for material: %s",
                run.spec.workflow_id,
                exc,
            )
            return False

    # ── 触发点 2：子 action 完成 ──────────────────────────────

    def on_job_outcome(
        self,
        job_id: str,
        outcome: CommittedJobOutcome,
    ) -> dict[str, Any]:
        """按 Backend-shaped 结果结算本地作业（Job）。

        参数：``job_id`` 是稳定作业身份；``outcome`` 保留 Edge 已提交的成功、
        失败、取消或超时终态及证据。返回：本轮工作流状态和后续派发摘要。异常：
        持久投影或库存结算失败原样传播；不会把失败类终态压缩成普通失败。
        """

        if outcome.unknown_command_ids:
            # 结果不明不是失败终态。作业继续保持 running，同时保留 Scheduler
            # 的在途身份和动作资源占用，等待设备或人工完成物理对账。
            with self._lock:
                if self._closed:
                    raise RuntimeError("调度器已关闭，不确定结果须保留待恢复")
                if job_id not in self._inflight:
                    logger.warning("[EdgeScheduler] unknown Job outcome: %s", job_id)
                    return {"dispatched": []}
                self._notify_job_outcome(job_id, outcome)
                return {
                    "state": "running",
                    "dispatched": [],
                }

        success = outcome.outcome == "succeeded"
        ret_value = outcome.return_info.get(
            "return_value",
            outcome.return_info,
        )
        return self._finish_job_with_trace(
            job_id,
            success,
            ret_value,
            "normal" if success else outcome.outcome,
            committed_outcome=outcome,
        )

    def on_job_finished(
        self,
        job_id: str,
        success: bool,
        ret_value: Any = None,
        suc_type: str = "normal",
    ) -> dict[str, Any]:
        """结算旧执行适配器的四参数完成回调。

        参数：作业身份、成功标记、返回值与旧异常分类。返回：工作流状态和后续派发
        摘要。异常：投影或库存结算失败原样传播。新 Edge HTTP 路径应使用
        :meth:`on_job_outcome`，本方法只保留旧执行适配器兼容。
        """

        return self._finish_job_with_trace(job_id, success, ret_value, suc_type)

    def _finish_job_with_trace(
        self,
        job_id: str,
        success: bool,
        ret_value: Any,
        suc_type: str,
        *,
        committed_outcome: CommittedJobOutcome | None = None,
    ) -> dict[str, Any]:
        """在同一追踪边界内完成保真或旧式结果结算。

        参数：前四项是旧调度结果；``committed_outcome`` 存在时是优先投影的完整
        Edge 证据。返回结算摘要。异常：结算错误向上传播；追踪句柄始终关闭。
        """

        action_trace = self._job_spans.get(job_id)
        if action_trace is None:
            return self._on_job_finished(
                job_id,
                success,
                ret_value,
                suc_type,
                committed_outcome=committed_outcome,
            )
        try:
            with action_trace.activate():
                add_event(
                    "action.result",
                    {
                        "workflow.job.uuid": job_id,
                        "action.success": success,
                        "action.success.type": suc_type,
                    },
                    span=action_trace.span,
                )
                if not success:
                    action_trace.error("action execution failed")
                return self._on_job_finished(
                    job_id,
                    success,
                    ret_value,
                    suc_type,
                    committed_outcome=committed_outcome,
                )
        finally:
            action_trace.end()
            self._job_spans.pop(job_id, None)

    def _on_job_finished(
        self,
        job_id: str,
        success: bool,
        ret_value: Any = None,
        suc_type: str = "normal",
        *,
        committed_outcome: CommittedJobOutcome | None = None,
    ) -> dict[str, Any]:
        """作业（Job）完成回调：写回结果、清理依赖并强制重排。

        ``suc_type`` 来自设备侧异常决策（registry.action_policy）：
        normal / skip / operator_intervention。skip 表示动作报错后人工选择
        跳过——节点按成功推进，但不把本节点预留结算为库存消耗。
        """
        with self._lock:
            if self._closed:
                # 执行端已保存的回执不能被空监听器列表误确认；重开后按原身份投影。
                raise RuntimeError("调度器已关闭，完成结果须保留待恢复")
            job = self._inflight.get(job_id)
            if job is None:
                logger.warning("[EdgeScheduler] unknown job finished: %s", job_id)
                return {"dispatched": []}

            run = self._workflows.get(job.workflow_id)
            if run is None:
                return {"dispatched": []}

            # 库存只在设备明确成功后结算。扣减失败时保留在途作业、执行占用和
            # 完成投递，禁止出现“作业成功但库存仍未扣减”的公开事实。
            if success and job.workflow_id in self._material_workflows:
                node = next(
                    (
                        candidate
                        for candidate in run.spec.nodes
                        if candidate.id == job.node_id
                    ),
                    None,
                )
                if node is not None and node.material_requirements:
                    self._inventory.consume_reservation(job.workflow_id, job.node_id)
                    if suc_type in {"skip", "user_bypass_error"}:
                        # skip 表示设备动作没有正常完成，物料却可能已进入物理
                        # 过程。先按实际使用结算，再隔离，禁止把数量虚假放回库存。
                        self._inventory.quarantine_reservation(
                            job.workflow_id,
                            job.node_id,
                            reason="device_action_skipped",
                            actor="edge_scheduler",
                            causation_id=job_id,
                        )

            # 标准完成事实必须先持久化；任一监听器失败时保留在途作业与资源锁，
            # 允许设备对同一结果进行投递重放（DeliveryReplay）。库存消费本身也
            # 以相同 workflow/node/attempt 幂等，重放不会重复扣减。
            if committed_outcome is not None:
                self._notify_job_outcome(job_id, committed_outcome)
            else:
                self._notify_job_finished(job_id, success, ret_value, suc_type)
            self._record_interval_handoff(job, success=success)
            self._inflight.pop(job_id, None)
            self._job_resource_locks.pop(job_id, None)

            # 泳道图时间线：记录实际起止 + 喂给历史统计（EMA）+ 历史库落盘
            canceled_by_executor = not success and suc_type == "canceled"
            self._record_timeline(
                job,
                success=success,
                suc_type=suc_type,
                ret_value=ret_value,
                state="canceled" if canceled_by_executor else "",
            )

            if self._station_control is not None and not self._station_control.is_current(job_id):
                pass  # 旧 attempt 的迟到结果仅更新该作业，不能推进新 attempt。
            elif self._station_control is not None and self._station_control.awaits_decision(job_id):
                # 迟到成功仍需用户处置；不能提前放开后继再让重试与后继并行。
                run.hold_error(job.node_id)
            elif success:
                run.mark_finished(job.node_id, ret_value)
            elif canceled_by_executor:
                # 取消请求本身不结算节点；只有执行器返回明确取消终态后，才消费
                # 在途节点并允许后续物理清理释放占用。
                run.mark_canceled(job.node_id)
            else:
                if self._station_control is not None and self._station_control.awaits_decision(job_id):
                    run.hold_error(job.node_id)
                else:
                    run.mark_failed(job.node_id)
                # 失败工作流的未下发节点不再推进；已下发的等它们各自回调
                logger.warning(
                    "[EdgeScheduler] node %s failed, workflow %s stops advancing",
                    job.node_id,
                    job.workflow_id,
                )
            if run.state in {WorkflowState.FAILED, WorkflowState.CANCELED, WorkflowState.TIMEOUT}:
                self._clear_interval_holders(
                    job.workflow_id,
                    preserve_explicit=True,
                )
            elif run.state in {WorkflowState.SUCCESS, WorkflowState.WAITING_MATERIAL}:
                self._clear_interval_holders(job.workflow_id)

            # normal→step 的切换请求在最后一个在途 Job 结算前始终保持排空态；
            # 必须先从 ``_inflight`` 删除当前 Job、再完成切换，保证下一轮重排
            # 看见的是关闭的新派发闸门。
            self._complete_step_transition_locked(run)

            # Step 的用户边界止于可见 body 节点。最后一个 body Job 结算后，
            # until 判断和 false→下一轮物化没有设备动作，必须在同一调度事务内
            # 自动完成，避免暂停态因没有 READY 候选而永久卡住。
            self._advance_step_repeat_transitions_locked(run)

            # 调试器的继续/断点推进必须等当前节点已经写入 WorkflowRun；否则下一
            # 节点仍被依赖判定为未就绪。完成事实监听与 DAG 结算监听明确分阶段。
            self._notify_job_settled(job_id, success, ret_value, suc_type)

            logger.info(
                "[EdgeScheduler] 作业 %s（工作流=%s 节点=%s 成功=%s）已完成，唤醒重排",
                job_id[:8],
                job.workflow_id,
                job.node_id,
                success,
            )
            notifications = self._collect_terminal_notifications()
        self._fire_notifications(notifications)
        dispatched = self._wake_reconcile()
        with self._lock:
            result = {
                "workflow_id": job.workflow_id,
                "workflow_state": run.state.value,
                "dispatched": dispatched,
            }
            post_reconcile_notifications = self._collect_terminal_notifications()
        self._fire_notifications(post_reconcile_notifications)
        return result

    def resolve_manual_confirmation(
        self,
        job_id: str,
        *,
        approved: bool,
        param: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """标记持久决定已经批准，并优先尝试同一 Job 的真实设备动作。"""

        if not approved:
            raise ValueError("拒绝人工确认必须走 Task Cancel 流程")
        with self._lock:
            job = self._inflight.get(job_id)
            if job is None:
                raise ValueError("人工确认对应的作业不在运行中")
            run = self._workflows.get(job.workflow_id)
            node = run.node(job.node_id) if run is not None else None
            if (
                run is None
                or node is None
                or not node.waits_for_manual_confirmation()
            ):
                raise ValueError("作业不是人工确认节点")
            if job.manual_action_dispatched:
                return {
                    "workflow_id": job.workflow_id,
                    "workflow_state": run.state.value,
                    "dispatched": [],
                }
            if param is not None and dict(param) != job.resolved_args:
                raise ValueError("人工确认不支持修改设备动作参数")
            job.manual_confirmation_approved = True
            if node.is_material_io():
                return {
                    "workflow_id": job.workflow_id,
                    "workflow_state": run.state.value,
                    "dispatched": [],
                }
            dispatched = self._try_dispatch_approved_manual_locked(job)
            return {
                "workflow_id": job.workflow_id,
                "workflow_state": run.state.value,
                "dispatched": [dispatched] if dispatched is not None else [],
            }

    def _try_dispatch_approved_manual_locked(
        self,
        job: DispatchedJob,
    ) -> dict[str, Any] | None:
        """复用既有资源与凭据派发已批准人工 Job；设备离线时保持等待。"""

        if not job.manual_confirmation_approved or job.manual_action_dispatched:
            return None
        if self._station_control is not None and not self._station_control.allows_task(job.workflow_id):
            return None
        run = self._workflows.get(job.workflow_id)
        node = run.node(job.node_id) if run is not None else None
        if run is None or node is None:
            raise ValueError("作业不是人工确认节点")
        if node.is_material_io():
            return None
        if not node.is_manual_confirm():
            raise ValueError("作业不是人工确认节点")
        self._refresh_job_active_use(
            run,
            node,
            job,
            require_all_plan_keys=False,
        )
        if self._inflight_job_conflict(job) is not None:
            return None
        if self._device_target_resolver is not None:
            other_busy: set[str] = set(self._external_busy_keys)
            if self._busy_key_provider is not None:
                other_busy |= set(self._busy_key_provider())
            for other_job_id, other in self._inflight.items():
                if other_job_id == job.job_id:
                    continue
                other_busy.add(other.device_action_key)
                other_busy.add(
                    device_lock_key(other.device_material_uuid or other.device_id)
                )
            try:
                selected = self._device_target_resolver(
                    {
                        "mode": "fixed",
                        "local_device_id": job.device_id,
                        "material_uuid": job.device_material_uuid,
                    },
                    node.action_name,
                    other_busy,
                )
            except DeviceTargetUnavailable:
                return None
            if (
                selected.local_device_id != job.device_id
                or selected.material_uuid != job.device_material_uuid
            ):
                raise ValueError("人工确认批准时设备执行身份发生漂移")
        elif self.physical_dispatch_enabled:
            raise ValueError("人工确认批准时缺少当前设备注册状态权威")
        authority = self._manual_continuation_authority
        if authority is None and self.physical_dispatch_enabled:
            raise ExecutionPolicyError("人工确认继续派发权威未装配")
        if authority is not None:
            authority(job.job_id)
        missing_credentials = [
            field
            for field in (
                "attempt",
                "command_uuid",
                "claim_uuid",
                "fences",
                "effect_uuid",
                "parameter_hash",
                "expected_change_set",
            )
            if field not in job.dispatch_credentials
        ]
        if missing_credentials:
            raise ValueError(
                "人工确认继续动作缺少派发凭据："
                + ",".join(sorted(missing_credentials))
            )
        payload = build_job_start_payload(
            job_id=job.job_id,
            task_id=run.spec.task_id,
            workflow_id=job.workflow_id,
            node_id=job.node_id,
            device_id=job.device_id,
            action_name=node.action_name,
            action_type=node.action_type,
            action_args=job.resolved_args,
            always_free=node.always_free,
        )
        payload.update(deepcopy(job.dispatch_credentials))
        job.manual_action_dispatched = True
        try:
            self._dispatcher.dispatch(payload)
            if not getattr(self._dispatcher, "deferred_start", False):
                self._notify_job_dispatch_accepted(job.job_id)
        except BaseException:
            self._notify_job_dispatch_uncertain(
                job.job_id,
                "manual_confirmation_dispatch_acceptance_unknown",
            )
            raise
        return {
            "job_id": job.job_id,
            "workflow_id": job.workflow_id,
            "node_id": job.node_id,
            "device_action_key": job.device_action_key,
        }

    def request_uncertain_resolution(
        self,
        job_id: str,
        *,
        reason: str,
    ) -> dict[str, Any]:
        """请求 Edge 证明未知设备命令已取消；这里只创建命令，不释放占用。"""

        store = getattr(self._dispatcher, "store", None)
        create_resolution = getattr(store, "create_unknown_resolution", None)
        if not callable(create_resolution):
            raise ValueError("当前执行边界不支持 UNKNOWN 处置证明")
        result = create_resolution(job_id, reason=reason)
        if not isinstance(result, Mapping):
            raise ValueError("UNKNOWN 处置命令返回非法")
        return dict(result)

    # ── 重排核心 ─────────────────────────────────────────────

    def _owns_scheduler_lock(self) -> bool:
        """当前线程是否已经持有调度锁。"""

        owned = getattr(self._lock, "_is_owned", None)
        return bool(owned()) if callable(owned) else False

    def _drain_pending_reconcile(self) -> list[dict[str, Any]]:
        """连续重排直到本轮期间到达的物料唤醒和后到提交都被消化。

        参数：无。返回：本 Future 内各轮派发摘要的累加。异常：``reschedule``
        失败原样传播。合并唤醒时用代数记录后到的补料、下料和 submit/finish；
        当前轮若已经查过空队列或已经派发过，代数变化后必须再跑一轮，并把
        各轮派发累加回去，否则调用方会拿到空 ``dispatched``，工作流看起来
        已经提交却不再往下走。
        """

        dispatched: list[dict[str, Any]] = []
        with self._reconcile_wakeup_lock:
            running = self._pending_reconcile
        try:
            while True:
                with self._reconcile_wakeup_lock:
                    if self._closed:
                        return dispatched
                    seen_generation = self._reconcile_generation
                dispatched.extend(self.reschedule())
                with self._reconcile_wakeup_lock:
                    if seen_generation == self._reconcile_generation:
                        return dispatched
        finally:
            with self._reconcile_wakeup_lock:
                if self._pending_reconcile is running:
                    self._pending_reconcile = None

    def _wake_reconcile(
        self, *, wait: bool = True, release_before_wait: Callable[[], None] | None = None
    ) -> list[dict[str, Any]]:
        """只向 Scheduler 调度循环投递唤醒，不在设备回调栈执行重排。

        普通完成回调同步等待这一轮结果以保持现有 API 返回形状；若执行器在调度
        循环线程内同步回调，或当前线程已持有调度锁，则只排队并立即返回，避免
        单线程循环自等待、以及“持锁等待重排线程再等同一把锁”的死锁。
        并进在途 Future 时无论是否等待都要推进代数：submit/finish 可能登记在
        “已经查过空队列”的旧轮之后，只等待不标脏会把派发结果丢掉。
        """

        with self._reconcile_wakeup_lock:
            future = None
            if not self._closed:
                self._reconcile_generation += 1
                future = self._pending_reconcile
                if future is None or future.done():
                    future = submit_with_context(
                        _RECONCILE_EXECUTOR,
                        _run_reconcile,
                        self._drain_pending_reconcile,
                    )
                    self._pending_reconcile = future
        if release_before_wait is not None:
            release_before_wait()
        if future is None:
            return []
        if (
            not wait
            or bool(getattr(_RECONCILE_THREAD, "active", False))
            or self._owns_scheduler_lock()
        ):
            future.add_done_callback(_log_background_reconcile_failure)
            return []
        return future.result()

    def reschedule(self) -> list[dict[str, Any]]:
        """手动触发重排并先通知来源准入重试监听器。

        参数：无。返回：本轮旧调度器实际派发摘要。异常：准入监听器或调度
        失败原样传播；监听器在调度锁外运行，可把受阻任务安全注册到本调度器。
        """

        thread_id = threading.get_ident()
        with self._reconcile_condition:
            if self._closed:
                return []
            self._active_reschedule_threads[thread_id] = self._active_reschedule_threads.get(thread_id, 0) + 1
        try:
            with self._lock:
                admission_retry_listeners = tuple(self._admission_retry_listeners)
            for listener in admission_retry_listeners:
                if self._closed:
                    return []
                listener()
            with self._lock:
                return self._reschedule_locked()
        finally:
            with self._reconcile_condition:
                remaining = self._active_reschedule_threads[thread_id] - 1
                if remaining:
                    self._active_reschedule_threads[thread_id] = remaining
                else:
                    del self._active_reschedule_threads[thread_id]
                self._reconcile_condition.notify_all()

    def _reschedule_locked(self) -> list[dict[str, Any]]:
        if self._closed:
            return []
        with span(
            "workflow.task.reconcile",
            attributes={"scheduler.round": self._reschedule_count + 1},
        ) as reschedule_span:
            dispatched = []
            visited: set[str] = set()
            while True:
                batch, blocker = self._reschedule_impl()
                dispatched.extend(batch)
                if not self._serial_queue or self._draining or self._inflight:
                    break
                if not self._serial_workflows.advance(self._active_workflow_submit_times(), blocker, visited):
                    break
                if self._select_serial_workflow() in self._serial_admissions:
                    # 准入监听器在本锁之外运行；让下一轮先处理新队首的库存准入。
                    self._wake_reconcile(wait=False)
                    break
            add_event(
                "workflow.task.reconcile.result",
                {"scheduler.dispatched.count": len(dispatched)},
                span=reschedule_span,
            )
            return dispatched

    def _fail_prepared_node(self, run: WorkflowRun, node: WorkflowNode) -> None:
        """把准备失败接入同一暂停入口，保留具体异常供用户处理。"""
        import sys
        error = sys.exception()
        message = str(error or "节点准备失败")
        error_code = (
            error.code
            if isinstance(error, MaterialIOError) and isinstance(error.code, str)
            else None
        )
        if self._station_control is None:
            run.mark_failed(node.id)
            return
        self._station_control.report_error(
            node.job_id,
            phase="prepare",
            message=message,
            error_code=error_code,
        )
        run.hold_error(node.id)

    def _active_workflow_submit_times(self) -> dict[str, float]:
        active = dict(self._serial_admissions)
        active.update({
            workflow_id: run.spec.submitted_at
            for workflow_id, run in self._workflows.items()
            if run.state not in self._TERMINAL_STATES
        })
        return active

    def _select_serial_workflow(self) -> str | None:
        return self._serial_workflows.select(
            self._active_workflow_submit_times(), {job.workflow_id for job in self._inflight.values()}
        )

    def _dispatch_error_manual_locked(self, workflow_id: str | None = None) -> list[dict[str, Any]]:
        """错误处置单动作复用执行适配器与结果路由，独立于普通资源排序。"""
        dispatched = []
        if self._station_control is None:
            return dispatched
        for run in tuple(self._workflows.values()):
            if workflow_id is not None and run.spec.workflow_id != workflow_id:
                continue
            if run.state is not WorkflowState.RUNNING:
                continue
            for node in run.ready_nodes():
                if not self._station_control.is_manual(node.job_id):
                    continue
                payload = build_job_start_payload(job_id=node.job_id, task_id=run.spec.task_id,
                    workflow_id=run.spec.workflow_id, node_id=node.id, device_id=node.device_id,
                    action_name=node.action_name, action_type=node.action_type, action_args=node.param, always_free=True)
                credentials = self._station_control.prepare_manual_dispatch(node.job_id, payload)
                if credentials is None:
                    continue
                payload.update(credentials)
                run.mark_dispatched(node.id)
                self._inflight[node.job_id] = DispatchedJob(job_id=node.job_id, workflow_id=run.spec.workflow_id,
                    node_id=node.id, device_action_key=node.device_action_key, device_id=node.device_id,
                    device_material_uuid=node.device_material_uuid, action_name=node.action_name, resolved_args=dict(node.param))
                workflow_trace = self._workflow_spans.get(run.spec.workflow_id)
                action_trace = start_detached_span("action.manual", parent_context=workflow_trace.context if workflow_trace else None,
                    attributes={"workflow.job.uuid": node.job_id, "workflow.task.uuid": run.spec.task_id})
                self._job_spans[node.job_id] = action_trace
                try:
                    with action_trace.activate():
                        self._dispatcher.dispatch(payload)
                    if not getattr(self._dispatcher, "deferred_start", False):
                        self._notify_job_dispatch_accepted(node.job_id)
                except Exception:
                    self._notify_job_dispatch_uncertain(node.job_id, "local_dispatch_acceptance_unknown")
                    raise
                dispatched.append({"job_id": node.job_id, "workflow_id": run.spec.workflow_id, "node_id": node.id})
        return dispatched

    def _reschedule_impl(self) -> tuple[list[dict[str, Any]], str | None]:
        """执行一轮完整重排，并下发当前能够安全执行的作业（Job）。

        参数：无；读取当前调度器（Scheduler）的工作流、库存、动作物料锁和
        进程内设备忙碌事实。
        Returns:
            本轮成功派发的作业摘要列表及因物料依赖阻塞队首的工作流 ID。

        异常：参数解析和动作物料锁合同错误在对应工作流节点上失败关闭；库存
        或派发基础设施异常按既有边界处理。设备级互斥只提供当前进程安全桥，
        不表示已经取得持久作业执行占用（JobExecutionClaim）。
        """

        self._reschedule_count += 1

        serial_id = self._select_serial_workflow() if self._serial_queue else None
        allow_new_task_work = not self._serial_queue or serial_id is not None
        manual_dispatched = self._dispatch_error_manual_locked(serial_id) if allow_new_task_work else []

        continued = [
            item
            for job in tuple(self._inflight.values())
            if self._station_control is None or self._station_control.allows_task(job.workflow_id)
            if (item := self._try_dispatch_approved_manual_locked(job)) is not None
        ]

        # 排空期间不接纳新的 Job；已准入并经人工批准的同一 Job 属于在途继续，
        # 必须允许越过物理边界，否则 drain 会永久阻塞。
        if self._draining or not allow_new_task_work:
            return manual_dispatched + continued, None

        if self.physical_dispatch_enabled and self._dispatch_admission_authority is None:
            raise ExecutionPolicyError("持久派发准入权威未装配")

        # 串行恢复完成后才能确定队首，避免尚未恢复的在途作业被抢占库存。
        recovering_serial_queue = self._serial_queue and getattr(self._station_control, "recovering", False)
        if self._inventory is not None and not recovering_serial_queue:
            for run in self._workflows.values():
                if serial_id is not None and run.spec.workflow_id != serial_id:
                    continue
                if run.state not in {WorkflowState.PENDING, WorkflowState.WAITING_MATERIAL}:
                    continue
                if run.dispatch_gate != "active" and not (self._serial_queue and run.execution_mode == "step"):
                    continue
                workflow_trace = self._workflow_spans.get(run.spec.workflow_id)
                activation = (
                    workflow_trace.activate()
                    if workflow_trace is not None
                    else span("workflow.material.retry")
                )
                with activation:
                    reserved = self._try_reserve(run)
                if reserved:
                    run.state = (
                        WorkflowState.PAUSED
                        if self._serial_queue and run.execution_mode == "step"
                        else WorkflowState.RUNNING
                    )
                    logger.info(
                        "[EdgeScheduler] workflow %s material reserved, resume running",
                        run.spec.workflow_id,
                    )
                    self._emit_monitor(
                        "scheduler",
                        "workflow_resumed",
                        {
                            "workflow_id": run.spec.workflow_id,
                            "reason": "material_reserved",
                        },
                    )
                    self._safe_history("record_state", run.spec.workflow_id, run.state.value)
                else:
                    run.state = WorkflowState.WAITING_MATERIAL

        ready: list[ReadyTask] = []
        serial_blocker: str | None = None
        for run in self._workflows.values():
            if serial_id is not None and run.spec.workflow_id != serial_id:
                continue
            if run.state is not WorkflowState.RUNNING:
                continue
            step_target = self._step_targets.get(run.spec.workflow_id)
            if run.dispatch_gate != "active" and step_target is None:
                continue
            if self._station_control is not None and not self._station_control.allows_task(run.spec.task_id):
                continue
            stepped_local_control = False
            deferred_commands: set[str] = set()
            while True:
                evaluation = run.prepare_local_control(deferred_commands)
                if evaluation is None:
                    break
                # 自动模式可连续推进调度器本地控制节点；单步模式只允许本次
                # 明确选择的一个可见控制节点，不能顺带跨过同层其他条件或循环。
                if step_target is not None and str(evaluation.get("node_id") or "") != step_target:
                    break
                # 监听器可以把“物化失败”原子改写为同一控制节点的失败决定；
                # 提交已投影的事件副本，保证持久状态与内存 DAG 不会分叉。
                committed = self._commit_prepared_local_control_locked(run, evaluation)
                if committed.get("pending"):
                    if self._serial_queue and serial_blocker is None:
                        serial_blocker = committed.get("serial_blocking_workflow_id")
                    deferred_commands.add(str(evaluation["node_id"]))
                    if step_target is not None:
                        break
                    continue
                if self._station_control is not None and not self._station_control.allows_task(run.spec.task_id):
                    break
                if step_target is not None:
                    stepped_local_control = True
                    break
                if run.state is not WorkflowState.RUNNING:
                    break
            if stepped_local_control:
                continue
            if run.state is not WorkflowState.RUNNING:
                continue
            weight = priority_weight(run.spec.priority)
            for node in run.ready_nodes():
                # 本地控制节点只由上面的本地控制循环结算；返回 pending 的来源同样
                # 留待下一轮重评，不能落入设备派发。
                if node.executor_kind in {"condition", "workflow_command", "material_source"}:
                    continue
                if step_target is not None and node.id != step_target:
                    continue
                ready.append(
                    ReadyTask(
                        workflow_id=run.spec.workflow_id,
                        node=node,
                        priority_weight=weight,
                        submitted_at=run.spec.submitted_at,
                    )
                )

        if not ready:
            return (manual_dispatched + continued if self._serial_queue else continued), serial_blocker

        busy = self._busy_keys()
        held_resource_locks = self._held_resource_locks()
        ordered = self._orderer.order(ready, OrderingContext(set(busy)))
        ordered.sort(
            key=lambda item: (
                0 if self._continuation_resource_keys(item.workflow_id, item.node) else 1
            )
        )

        dispatched: list[dict[str, Any]] = manual_dispatched + list(continued)
        for task in ordered:
            run = self._workflows[task.workflow_id]
            if (
                run.dispatch_gate != "active"
                and self._step_targets.get(task.workflow_id) is None
            ):
                continue
            if self._station_control is not None and self._station_control.is_manual(task.node.job_id):
                continue
            if self._station_control is not None and not self._station_control.allows_task(task.workflow_id):
                continue
            # 人工确认必须先占完整设备/物料/Site 资源，即使底层动作声明
            # always_free 也不能绕过设备互斥；这是人员到场确认的安全边界。
            manual_confirm = task.node.waits_for_manual_confirmation()
            material_io = task.node.is_material_io()
            bypass_device_lock = material_io or (
                task.node.always_free and not manual_confirm
            )
            # ``job_id`` 优先复用标准工作流节点作业（WorkflowNodeJob）身份；旧整图
            # 没有提供时才维持历史随机身份行为。等待与派发必须使用同一身份。
            job_id = task.node.job_id or uuid_mod.uuid4().hex
            continuation_keys = self._continuation_resource_keys(
                task.workflow_id,
                task.node,
            )
            shared_scope_holders = self._shared_scope_holders(
                task.workflow_id,
                task.node,
            )
            nonshareable_active_keys = {
                key
                for job in self._inflight.values()
                for key in job.resource_lock_keys
                if key not in shared_scope_holders.get(job.job_id, set())
            }
            for job in self._inflight.values():
                shared_keys = shared_scope_holders.get(job.job_id, set())
                for identity in (job.device_id, job.device_material_uuid):
                    if not identity:
                        continue
                    key = device_lock_key(identity)
                    if key not in shared_keys and key not in job.operator_released_keys:
                        nonshareable_active_keys.add(key)
            # 默认连续区间只能由一个在途后继接管。只有显式共同 scope 才允许
            # 同 Task 的并行 Job 复用同一资源；层级 Material/Site 锁也按真实
            # 冲突关系移出可继承集合，避免后继绕过当前在途持有者。
            continuation_keys -= conflicting_resource_lock_keys(
                continuation_keys,
                nonshareable_active_keys,
            )

            try:
                selected_device = (
                    ResolvedDeviceTarget("scheduler-control", "")
                    if material_io
                    else self._resolve_device_target(
                        task.node,
                        busy - continuation_keys,
                        continuation_keys=continuation_keys,
                    )
                )
            except DeviceTargetUnavailable as error:
                self._notify_job_execution_wait(
                    {
                        "job_id": job_id,
                        "workflow_id": task.workflow_id,
                        "node_id": task.node.id,
                        "resolved_args": {},
                        "execution_locks": [],
                        "blocking_job_id": None,
                        "blocking_workflow_id": None,
                        "wait_code": error.code,
                        "wait_message": error.message,
                        "wait_resources": [dict(item) for item in error.resources],
                    }
                )
                continue
            selected_device_id = selected_device.local_device_id
            selected_device_material_uuid = selected_device.material_uuid
            # 动作键继续服务执行会话；设备资源键使用库存 Material UUID，确保
            # 不同动作、动态选择和长期托管引用同一个物理设备身份。
            action_key = f"/devices/{selected_device_id}/{task.node.action_name}"
            device_key = device_lock_key(
                selected_device_material_uuid or selected_device_id
            )

            # ``transfer_dispatch_condition`` 由库存解析快照产生，但只在门禁 7 的
            # 同一库存事务内复验后才具有派发效力。
            transfer_dispatch_condition: dict[str, str] | None = None
            inventory_relocation_condition: dict[str, str] | None = None
            transfer_dispatch_candidates: list[dict[str, Any]] = []
            site_selection_audit: dict[str, Any] | None = None
            operate_in_place_condition: dict[str, str] | list[dict[str, str]] | None = None
            aliquot_dispatch_condition: dict[str, Any] | None = None
            runtime_material_keys: set[str] = set()
            try:
                resolved_args = run.resolve_params(task.node.id)
            except ParamResolveError as exc:
                logger.error(
                    "[EdgeScheduler] param resolve failed for wf=%s node=%s: %s",
                    task.workflow_id,
                    task.node.id,
                    exc,
                )
                self._fail_prepared_node(run, task.node)
                continue

            # Schema 或库位解析失败必须关闭执行，不能退化为“没有资源锁”。
            try:
                # 动态身份来自已执行分支的物料数据边。空结果不能派发；新增的
                # 物料及转运资源仍由下方库存 Permit 在同一事务中复验和占用。
                for parameter in task.node.runtime_material_parameters:
                    identity = _resource_argument_uuid(resolved_args.get(parameter), argument_name=parameter)
                    runtime_material_keys.add(material_lock_key(identity))
                transfer_contract = self._transfer_resource_contract(task.node)
                unresolved_transfer_args = dict(resolved_args)
                # 同一冻结连续区间已预留的目标库位由后继继承，不属于
                # “其他作业”占用。最终所有权仍由派发 Permit 的同一事务复验。
                _, _, inherited_site_keys, _ = self._interval_projection(run, task.node)
                unavailable_site_uuids = _claimed_site_uuids(
                    held_resource_locks - inherited_site_keys
                )
                execution_policy = self._execution_policy_with_runtime_site_group(
                    task.node,
                    resolved_args,
                    transfer_contract=transfer_contract,
                    unavailable_site_uuids=unavailable_site_uuids,
                )
                resource_policy = resolve_execution_resource_policy(
                    execution_policy,
                    resolved_args,
                )
                raw_site_selection = execution_policy.get("target_site_selection")
                if isinstance(raw_site_selection, Mapping):
                    site_selection_audit = deepcopy(dict(raw_site_selection))
                has_site_selection = site_selection_audit is not None
                defer_site_availability = (
                    has_site_selection and self._dispatch_admission_authority is not None
                )
                resolved_args, resolved_site = self._resolve_transfer_site_target(
                    task.node,
                    resolved_args,
                    transfer_contract=transfer_contract,
                    site_uuids=resource_policy.target_site_uuids,
                    unavailable_site_uuids=(
                        () if defer_site_availability else unavailable_site_uuids
                    ),
                    require_available=not defer_site_availability,
                )
                lock_keys = self._resource_lock_keys(
                    task.node,
                    resolved_args,
                    resolved_site=resolved_site,
                )
                lock_keys.update(resource_policy.device_lock_keys)
                if material_io:
                    material_io_service = getattr(
                        self._inventory, "material_io", None
                    )
                    if material_io_service is None:
                        raise ExecutionPolicyError(
                            "Material I/O 未装配本地库存服务"
                        )
                    mount_value = resolved_args.get("mount")
                    material_value = resolved_args.get("material")
                    material_io_resources = material_io_service.resolve_resources(
                        operation=resolved_args.get("mode"),
                        mount_material_uuid=(
                            _resource_argument_uuid(
                                mount_value, argument_name="mount"
                            )
                            if mount_value is not None
                            else None
                        ),
                        site_uuid=resolved_args.get("site_uuid"),
                        site=resolved_args.get("site"),
                        material_uuid=(
                            _resource_argument_uuid(
                                material_value, argument_name="material"
                            )
                            if material_value is not None
                            else None
                        ),
                        scope=resolved_args.get("scope"),
                        # 处置方式与补液试剂一并预检，确认前即拒绝结算必然失败的请求。
                        disposition=resolved_args.get("disposition"),
                        reagent=resolved_args.get("reagent"),
                    )
                    lock_keys.update(
                        str(resource["lock_key"])
                        for resource in material_io_resources
                    )
                    # Material I/O 的完整作用域（尤其递归下料的子 Material）
                    # 只能由库存权威基于冻结参数在派发前解析；这些键仍会进入
                    # 同一 Dispatch Permit 原子复验，不能被调用方自行扩展。
                    runtime_material_keys.update(
                        str(resource["lock_key"])
                        for resource in material_io_resources
                    )
                if not bypass_device_lock:
                    lock_keys.add(device_key)
                if transfer_contract is not None and resolved_site is not None:
                    moved_material_uuid = _resource_argument_uuid(
                        resolved_args.get(transfer_contract["material_param"]),
                        argument_name=transfer_contract["material_param"],
                    )
                    transfer_step = (task.node.action_resource_contract or {}).get(
                        "transfer_step"
                    ) or {}
                    transfer_resources = resolve_transfer_resource_set(
                        self._required_station_resources(),
                        resource_material_uuid=moved_material_uuid,
                        target=resolved_site,
                        executor_material_uuid=selected_device_material_uuid,
                        allow_held_material=transfer_step.get("operation") == "place",
                        gripper_site_role=transfer_contract["gripper_site_role"],
                        # S3/S10/S11 等被动库位归属于工站 Deck，不存在设备祖先；
                        # 仍由物料、来源/目标 Site、机械臂与夹爪锁完整保护。端点
                        # 能追溯到设备时解析器会额外返回并锁住设备，但夹爪角色
                        # 本身不应强制两个端点都必须是设备。
                        require_device_owners=False,
                    )
                    resolved_args = self._inject_actual_transfer_source(
                        resolved_args,
                        transfer_contract=transfer_contract,
                        source_owner_material_uuid=(transfer_resources.source_owner_material_uuid),
                        source_site_uuid=transfer_resources.source_site_uuid,
                        source_site_name=transfer_resources.source_site_name,
                        address_site_uuid=self._address_site_resolver(
                            transfer_resources.source_owner_material_uuid
                        ),
                    )
                    lock_keys.update(transfer_resources.lock_keys)
                    if self._transfer_uses_runtime_material(task.node, transfer_contract):
                        runtime_material_keys.update(transfer_resources.lock_keys)
                    transfer_dispatch_condition = {
                        "material_uuid": moved_material_uuid,
                        "source_owner_material_uuid": (
                            transfer_resources.source_owner_material_uuid
                        ),
                        "source_site_uuid": transfer_resources.source_site_uuid,
                        "target_owner_material_uuid": resolved_site.owner_material_uuid,
                        "target_site_uuid": resolved_site.uuid,
                        "executor_material_uuid": selected_device_material_uuid,
                        "gripper_site_uuid": transfer_resources.gripper_site_uuid,
                    }
                    transfer_dispatch_candidates.append(
                        {
                            "resolved_args": dict(resolved_args),
                            "lock_keys": set(lock_keys),
                            "transfer_dispatch_condition": dict(transfer_dispatch_condition),
                            "target_site_uuid": resolved_site.uuid,
                        }
                    )
                    if len(resource_policy.target_site_uuids) > 1:
                        mount_uuid = _resource_argument_uuid(
                            unresolved_transfer_args.get(transfer_contract["target_owner_param"]),
                            argument_name=transfer_contract["target_owner_param"],
                        )
                        for candidate_site_uuid in resource_policy.target_site_uuids:
                            if candidate_site_uuid == resolved_site.uuid:
                                continue
                            try:
                                candidate_target = resolve_site_target(
                                    self._required_station_resources(),
                                    owner_material_uuid=mount_uuid,
                                    site_uuid=candidate_site_uuid,
                                    occupant_material_uuid=moved_material_uuid,
                                    require_available=False,
                                )
                            except SiteTargetResolutionError as candidate_error:
                                if is_temporary_resource_condition(candidate_error.code):
                                    continue
                                raise
                            candidate_args = dict(unresolved_transfer_args)
                            target_name_param = transfer_contract["target_site_name_param"]
                            target_uuid_param = transfer_contract["target_site_uuid_param"]
                            if target_name_param:
                                candidate_args[target_name_param] = candidate_target.name
                            if target_uuid_param:
                                candidate_args[target_uuid_param] = candidate_target.uuid
                            candidate_resources = resolve_transfer_resource_set(
                                self._required_station_resources(),
                                resource_material_uuid=moved_material_uuid,
                                target=candidate_target,
                                executor_material_uuid=(selected_device_material_uuid),
                                allow_held_material=transfer_step.get("operation") == "place",
                                gripper_site_role=transfer_contract["gripper_site_role"],
                                require_device_owners=False,
                            )
                            candidate_args = self._inject_actual_transfer_source(
                                candidate_args,
                                transfer_contract=transfer_contract,
                                source_owner_material_uuid=(
                                    candidate_resources.source_owner_material_uuid
                                ),
                                source_site_uuid=(candidate_resources.source_site_uuid),
                                source_site_name=(candidate_resources.source_site_name),
                                address_site_uuid=self._address_site_resolver(
                                    candidate_resources.source_owner_material_uuid
                                ),
                            )
                            candidate_locks = self._resource_lock_keys(
                                task.node,
                                candidate_args,
                                resolved_site=candidate_target,
                            )
                            candidate_locks.update(resource_policy.device_lock_keys)
                            if not bypass_device_lock:
                                candidate_locks.add(device_key)
                            candidate_locks.update(candidate_resources.lock_keys)
                            if self._transfer_uses_runtime_material(task.node, transfer_contract):
                                runtime_material_keys.update(candidate_resources.lock_keys)
                            transfer_dispatch_candidates.append(
                                {
                                    "resolved_args": candidate_args,
                                    "lock_keys": candidate_locks,
                                    "transfer_dispatch_condition": {
                                        "material_uuid": moved_material_uuid,
                                        "source_owner_material_uuid": (
                                            candidate_resources.source_owner_material_uuid
                                        ),
                                        "source_site_uuid": (candidate_resources.source_site_uuid),
                                        "target_owner_material_uuid": (
                                            candidate_target.owner_material_uuid
                                        ),
                                        "target_site_uuid": candidate_target.uuid,
                                        "executor_material_uuid": (selected_device_material_uuid),
                                        "gripper_site_uuid": (
                                            candidate_resources.gripper_site_uuid
                                        ),
                                    },
                                    "target_site_uuid": candidate_target.uuid,
                                }
                            )
                        candidate_order = {
                            site_uuid: index
                            for index, site_uuid in enumerate(resource_policy.target_site_uuids)
                        }
                        transfer_dispatch_candidates.sort(
                            key=lambda item: candidate_order[str(item["target_site_uuid"])]
                        )
                        selected_candidate = transfer_dispatch_candidates[0]
                        resolved_args = dict(selected_candidate["resolved_args"])
                        lock_keys = set(selected_candidate["lock_keys"])
                        transfer_dispatch_condition = dict(
                            selected_candidate["transfer_dispatch_condition"]
                        )
                operate_contract = self._operate_in_place_contract(task.node)
                if operate_contract is not None:
                    conditions = []
                    for parameter in operate_contract:
                        material_uuid = _resource_argument_uuid(
                            resolved_args.get(parameter),
                            argument_name=parameter,
                        )
                        if any(item["material_uuid"] == material_uuid for item in conditions):
                            raise ExecutionPolicyError("原位操作的多个物料必须互不相同")
                        facts = self._required_station_resources().resolve_operate_in_place(
                            material_uuid=material_uuid,
                            device_material_uuid=selected_device_material_uuid,
                        )
                        lock_keys.update(
                            {
                                device_lock_key(facts.device_material_uuid),
                                material_lock_key(facts.material_uuid),
                                site_lock_key(facts.site_owner_material_uuid, facts.site_uuid),
                            }
                        )
                        conditions.append(
                            {
                                "material_uuid": facts.material_uuid,
                                "site_owner_material_uuid": facts.site_owner_material_uuid,
                                "site_uuid": facts.site_uuid,
                                "device_material_uuid": facts.device_material_uuid,
                            }
                        )
                    operate_in_place_condition = conditions[0] if len(conditions) == 1 else conditions
                aliquot_contract = self._aliquot_resource_contract(task.node)
                if aliquot_contract is not None:
                    source_uuid = _resource_argument_uuid(
                        resolved_args.get(aliquot_contract["source_material_param"]),
                        argument_name=aliquot_contract["source_material_param"],
                    )
                    target_uuids = tuple(
                        _resource_argument_uuid(
                            resolved_args.get(parameter), argument_name=parameter
                        )
                        for parameter in aliquot_contract["target_material_params"]
                    )
                    if len(set(target_uuids)) != len(target_uuids) or source_uuid in target_uuids:
                        raise ExecutionPolicyError("分装来源与目标容器必须互异")
                    lock_keys.update(
                        material_lock_key(value) for value in (source_uuid, *target_uuids)
                    )
                    aliquot_dispatch_condition = {
                        "source_material_uuid": source_uuid,
                        "target_material_uuids": list(target_uuids),
                    }
                if (
                    str(getattr(task.node, "action_name", "") or "") == "transfer_resource"
                    and transfer_contract is None
                ):
                    mount_uuid = _resource_argument_uuid(
                        resolved_args.get("mount_resource"),
                        argument_name="mount_resource",
                    )
                    moved_material_uuid = _resource_argument_uuid(
                        resolved_args.get("resource"),
                        argument_name="resource",
                    )
                    site_uuid = str(resolved_args.get("site_uuid") or "").strip()
                    site_name = str(resolved_args.get("site") or "").strip()
                    if not site_uuid and not site_name:
                        raise SiteTargetResolutionError(
                            "site_selector_missing",
                            "库存改挂必须提供目标库位名称或稳定 UUID",
                        )
                    relocation_target = resolve_site_target(
                        self._required_station_resources(),
                        owner_material_uuid=mount_uuid,
                        site_uuid=site_uuid,
                        site_name=site_name,
                        occupant_material_uuid=moved_material_uuid,
                        unavailable_site_uuids=(
                            () if defer_site_availability else unavailable_site_uuids
                        ),
                        require_available=not defer_site_availability,
                    )
                    resolved_args = dict(resolved_args)
                    resolved_args["site"] = relocation_target.name
                    resolved_args["site_uuid"] = relocation_target.uuid
                    relocation_resources = resolve_transfer_resource_set(
                        self._required_station_resources(),
                        resource_material_uuid=moved_material_uuid,
                        target=relocation_target,
                        gripper_site_role="",
                        require_device_owners=False,
                    )
                    lock_keys.update(relocation_resources.lock_keys)
                    if "resource" in task.node.runtime_material_parameters:
                        runtime_material_keys.update(relocation_resources.lock_keys)
                    inventory_relocation_condition = {
                        "material_uuid": moved_material_uuid,
                        "source_owner_material_uuid": (
                            relocation_resources.source_owner_material_uuid
                        ),
                        "source_site_uuid": relocation_resources.source_site_uuid,
                        "target_owner_material_uuid": relocation_target.owner_material_uuid,
                        "target_site_uuid": relocation_target.uuid,
                    }
            except SiteTargetResolutionError as error:
                if is_temporary_resource_condition(error.code):
                    self._notify_job_execution_wait(
                        {
                            "job_id": job_id,
                            "workflow_id": task.workflow_id,
                            "node_id": task.node.id,
                            "resolved_args": resolved_args,
                            "execution_locks": [],
                            "blocking_job_id": None,
                            "blocking_workflow_id": None,
                            "wait_code": error.code,
                            "wait_message": error.message,
                            "wait_resources": [dict(resource) for resource in error.resources],
                        }
                    )
                    continue
                logger.error(
                    "[EdgeScheduler] 目标库位解析失败 wf=%s node=%s code=%s: %s",
                    task.workflow_id,
                    task.node.id,
                    error.code,
                    error.message,
                )
                self._fail_prepared_node(run, task.node)
                continue
            except TransferResourceSetError as error:
                if is_temporary_resource_condition(error.code):
                    self._notify_job_execution_wait(
                        {
                            "job_id": job_id,
                            "workflow_id": task.workflow_id,
                            "node_id": task.node.id,
                            "resolved_args": resolved_args,
                            "execution_locks": [],
                            "blocking_job_id": None,
                            "blocking_workflow_id": None,
                            "wait_code": error.code,
                            "wait_message": error.message,
                            "wait_resources": [dict(resource) for resource in error.resources],
                        }
                    )
                    continue
                logger.error(
                    "[EdgeScheduler] 转运完整资源集解析失败 wf=%s node=%s code=%s: %s",
                    task.workflow_id,
                    task.node.id,
                    error.code,
                    error.message,
                )
                self._fail_prepared_node(run, task.node)
                continue
            except StationResourceError as error:
                if is_temporary_resource_condition(error.code):
                    self._notify_job_execution_wait(
                        {
                            "job_id": job_id,
                            "workflow_id": task.workflow_id,
                            "node_id": task.node.id,
                            "resolved_args": resolved_args,
                            "execution_locks": [],
                            "blocking_job_id": None,
                            "blocking_workflow_id": None,
                            "wait_code": error.code,
                            "wait_message": error.message,
                            "wait_resources": [dict(resource) for resource in error.resources],
                        }
                    )
                    continue
                logger.error(
                    "[EdgeScheduler] 原位资源条件失败 工作流=%s 节点=%s 代码=%s：%s",
                    task.workflow_id,
                    task.node.id,
                    error.code,
                    error.message,
                )
                self._fail_prepared_node(run, task.node)
                continue
            except (
                ExecutionPolicyError,
                ExecutionResourcePolicyError,
                MaterialLockSchemaError,
                MaterialIOError,
            ) as error:
                logger.error(
                    "[EdgeScheduler] 动作资源锁解析失败 " "wf=%s node=%s code=%s path=%s: %s",
                    task.workflow_id,
                    task.node.id,
                    getattr(error, "code", "invalid_execution_policy"),
                    getattr(error, "path", "/"),
                    getattr(error, "message", str(error)),
                )
                self._fail_prepared_node(run, task.node)
                continue
            try:
                (
                    _plan,
                    plan_resource_keys,
                    inherited_resource_keys,
                    interval_ids_by_lock,
                ) = self._interval_projection(
                    run,
                    task.node,
                )
                if _plan is not None:
                    if resolved_site is not None:
                        narrow_runtime_owner_lock_to_site(
                            runtime_material_keys,
                            owner_material_uuid=resolved_site.owner_material_uuid,
                            site_uuid=resolved_site.uuid,
                        )
                    unplanned = normalize_resource_lock_keys(
                        lock_keys | plan_resource_keys
                    ) - normalize_resource_lock_keys(plan_resource_keys | runtime_material_keys)
                    if unplanned:
                        raise ExecutionPolicyError(
                            "实际派发申请计划外资源：" + ",".join(sorted(unplanned))
                        )
                    for candidate in transfer_dispatch_candidates:
                        extras = normalize_resource_lock_keys(
                            set(candidate["lock_keys"]) | plan_resource_keys
                        ) - normalize_resource_lock_keys(plan_resource_keys | runtime_material_keys)
                        if extras:
                            raise ExecutionPolicyError(
                                "后备搬运目标超出冻结资源计划：" + ",".join(sorted(extras))
                            )
                        candidate["lock_keys"] = set(candidate["lock_keys"]) | plan_resource_keys
            except ExecutionPolicyError as error:
                logger.error(
                    "[EdgeScheduler] 冻结资源计划与派发不一致 wf=%s node=%s: %s",
                    task.workflow_id,
                    task.node.id,
                    error,
                )
                self._fail_prepared_node(run, task.node)
                continue
            continuation_job_uuids = self._continuation_resource_job_uuids(
                task.workflow_id,
                task.node,
            )
            inherited_resource_keys &= continuation_keys
            lock_keys.update(plan_resource_keys)
            lock_keys.update(inherited_resource_keys)
            active_resource_lock_keys = self._active_use_resource_keys(
                run,
                task.node,
                plan=_plan,
                plan_resource_keys=plan_resource_keys,
                requested_lock_keys=lock_keys,
            )
            # 命名 Site 组的候选位置还没有在库存 Gate 7 中裁决；不能用首选
            # Site 的旧锁视图把整个候选集变成不可选。物料本体、来源
            # Site 和设备仍然在本地 active-use 互斥；目标 Site 只在库存事务
            # 返回具体选择后再进入作业的最终活动键。
            preflight_active_resource_lock_keys = set(active_resource_lock_keys)
            deferred_target_site_keys: set[str] = set()
            if defer_site_availability:
                for candidate in transfer_dispatch_candidates:
                    condition = candidate.get("transfer_dispatch_condition")
                    if not isinstance(condition, Mapping):
                        continue
                    owner_uuid = str(
                        condition.get("target_owner_material_uuid") or ""
                    )
                    site_uuid = str(condition.get("target_site_uuid") or "")
                    if not owner_uuid or not site_uuid:
                        continue
                    try:
                        deferred_target_site_keys.add(
                            site_lock_key(owner_uuid, site_uuid)
                        )
                    except ValueError:
                        # 传运条件的身份会在前方解析阶段被拒绝；这里不把无法
                        # 构造的键静默当成可用资源。
                        continue
                preflight_active_resource_lock_keys -= deferred_target_site_keys
            held_active_resource_keys = {
                key
                for inflight_job in self._inflight.values()
                for key in inflight_job.active_resource_lock_keys
            }
            reservation_conflicts = conflicting_resource_lock_keys(
                (lock_keys - inherited_resource_keys) - deferred_target_site_keys,
                (held_resource_locks - inherited_resource_keys)
                - deferred_target_site_keys,
            )
            active_use_conflicts = conflicting_resource_lock_keys(
                preflight_active_resource_lock_keys,
                held_active_resource_keys,
            )
            conflicting_lock_keys = reservation_conflicts | active_use_conflicts
            device_conflict = not bypass_device_lock and (
                action_key in (busy - inherited_resource_keys)
                or device_key in (busy - inherited_resource_keys)
            )
            if defer_site_availability:
                # 命名组和精确覆盖的物理可用性只由 Gate 7 的单一库存事务裁决。
                # 本地锁视图可能比库存 Claim 稍旧，也不能因首选冲突跳过后备候选。
                # JobActiveUse 不属于可共享的连续 reservation；即使库存负责选择
                # Site，同一物料/设备的在途操作仍必须在本地关闭式串行。候选
                # target Site 已从 reservation 预检查排除，由库存权威决定具体位置；
                # 其他 reservation（尤其连续 scope）和设备互斥仍在本地生效。
                conflicting_lock_keys = reservation_conflicts | active_use_conflicts
            execution_locks = self._execution_lock_descriptors(lock_keys)
            dispatch_candidates = [
                {
                    "resolved_args": dict(candidate["resolved_args"]),
                    "execution_locks": self._execution_lock_descriptors(
                        set(candidate["lock_keys"])
                    ),
                    "transfer_dispatch_condition": dict(candidate["transfer_dispatch_condition"]),
                }
                for candidate in transfer_dispatch_candidates
            ]
            if device_conflict or conflicting_lock_keys:
                blocking_job = next(
                    (
                        candidate
                        for candidate in sorted(
                            self._inflight.values(), key=lambda item: item.job_id
                        )
                        if (
                            device_conflict
                            and (
                                candidate.device_action_key == action_key
                                or candidate.device_id == selected_device_id
                            )
                        )
                        or conflicting_resource_lock_keys(
                            preflight_active_resource_lock_keys,
                            candidate.active_resource_lock_keys,
                        )
                        or conflicting_resource_lock_keys(
                            lock_keys,
                            self._job_resource_locks.get(candidate.job_id, set()),
                        )
                    ),
                    None,
                )
                self._notify_job_execution_wait(
                    {
                        "job_id": job_id,
                        "workflow_id": task.workflow_id,
                        "node_id": task.node.id,
                        "resolved_args": resolved_args,
                        "execution_locks": execution_locks,
                        "resource_plan_id": task.node.resource_plan_id,
                        "resource_interval_ids": list(task.node.resource_interval_ids),
                        "resource_acquire_set_id": task.node.resource_acquire_set_id,
                        "resource_shared_scope_keys": sorted(
                            set().union(
                                *self._shared_scope_holders(task.workflow_id, task.node).values()
                            )
                        ),
                        "resource_preheld_lock_keys": sorted(inherited_resource_keys),
                        "resource_preheld_job_uuids": sorted(continuation_job_uuids),
                        "resource_interval_ids_by_lock": {
                            key: sorted(value) for key, value in interval_ids_by_lock.items()
                        },
                        "device_tenancy": resource_policy.device_tenancy,
                        "blocking_job_id": (
                            blocking_job.job_id if blocking_job is not None else None
                        ),
                        "blocking_workflow_id": (
                            blocking_job.workflow_id if blocking_job is not None else None
                        ),
                    }
                )
                logger.info(
                    "[EdgeScheduler] node %s waits for execution lock(s) %s (wf=%s)",
                    task.node.id,
                    sorted(
                        set(conflicting_lock_keys) | ({device_key} if device_conflict else set())
                    ),
                    task.workflow_id,
                )
                continue
            payload = build_job_start_payload(
                job_id=job_id,
                task_id=run.spec.task_id,
                workflow_id=task.workflow_id,
                node_id=task.node.id,
                device_id=selected_device_id,
                action_name=task.node.action_name,
                action_type=task.node.action_type,
                action_args=resolved_args,
                always_free=task.node.always_free,
            )
            # 预估基于 sjson 覆写后的 resolved 参数：父节点经 gjson/sjson 传下来的
            # 实际值（如 time）直接决定声明式预估结果
            estimated_s, estimate_source = self._estimator.estimate(action_key, resolved_args)
            workflow_trace = self._workflow_spans.get(task.workflow_id)
            action_trace = start_detached_span(
                "action.run",
                parent_context=(workflow_trace.context if workflow_trace is not None else None),
                attributes={
                    "workflow.job.uuid": job_id,
                    "workflow.uuid": task.workflow_id,
                    "workflow.node.uuid": task.node.id,
                    "device.name": selected_device_id,
                    "action.name": task.node.action_name,
                    "action.type": task.node.action_type,
                    "action.manual_confirm": manual_confirm,
                },
            )
            self._job_spans[job_id] = action_trace
            dispatch_intent_committed = False
            try:
                with (
                    action_trace.activate(),
                    span(
                        "workflow.job.dispatch",
                        attributes={
                            "workflow.job.uuid": job_id,
                            "workflow.uuid": task.workflow_id,
                            "workflow.node.uuid": task.node.id,
                            "device.name": selected_device_id,
                            "action.name": task.node.action_name,
                        },
                    ),
                ):
                    # 标准任务/作业必须先提交派发意图，才能越过物理执行边界。
                    dispatching = {
                        "job_id": job_id,
                        "workflow_id": task.workflow_id,
                        "node_id": task.node.id,
                        "executor_kind": task.node.executor_kind,
                        "dispatch_payload": deepcopy(payload),
                        "device_action_key": action_key,
                        "estimated_s": round(estimated_s, 3),
                        "estimate_source": estimate_source,
                        "resolved_args": resolved_args,
                        "actual_executor": {
                            "local_device_id": selected_device_id,
                            "material_uuid": selected_device_material_uuid,
                        },
                        "execution_locks": execution_locks,
                        "resource_plan_id": task.node.resource_plan_id,
                        "resource_interval_ids": list(task.node.resource_interval_ids),
                        "resource_acquire_set_id": task.node.resource_acquire_set_id,
                        "resource_shared_scope_keys": sorted(
                            set().union(
                                *self._shared_scope_holders(task.workflow_id, task.node).values()
                            )
                        ),
                        "resource_preheld_lock_keys": sorted(inherited_resource_keys),
                        "resource_preheld_job_uuids": sorted(continuation_job_uuids),
                        "resource_interval_ids_by_lock": {
                            key: sorted(value) for key, value in interval_ids_by_lock.items()
                        },
                        "device_tenancy": resource_policy.device_tenancy,
                        "transfer_dispatch_condition": transfer_dispatch_condition,
                        "inventory_relocation_condition": inventory_relocation_condition,
                        "transfer_place_step": bool(
                            (task.node.action_resource_contract or {})
                            .get("transfer_step", {})
                            .get("operation")
                            == "place"
                        ),
                        "site_selection": site_selection_audit,
                        "operate_in_place_condition": operate_in_place_condition,
                        "aliquot_dispatch_condition": aliquot_dispatch_condition,
                        **(
                            {"manual_confirmation": dict(task.node.manual_confirmation)}
                            if manual_confirm
                            else {}
                        ),
                        **(
                            {"dispatch_candidates": dispatch_candidates}
                            if len(dispatch_candidates) > 1
                            else {}
                        ),
                    }
                    try:
                        admitted = self._notify_job_pre_dispatch(dispatching)
                    except DispatchAdmissionConflict:
                        # 永久性准入冲突（如物料已被删除）只属于本节点：让它失败并
                        # 进入工站错误处置，不能让异常冒出整个重排循环卡死工站。
                        logger.exception(
                            "[EdgeScheduler] 派发准入冲突 工作流=%s 节点=%s",
                            task.workflow_id,
                            task.node.id,
                        )
                        action_trace.end()
                        self._job_spans.pop(job_id, None)
                        self._fail_prepared_node(run, task.node)
                        continue
                    if not admitted:
                        if self._serial_queue and serial_blocker is None:
                            serial_blocker = dispatching.get("serial_blocking_workflow_id")
                        action_trace.event(
                            "action.waiting_for_execution_lock",
                            {"workflow.job.uuid": job_id},
                        )
                        action_trace.end()
                        self._job_spans.pop(job_id, None)
                        continue
                    required_dispatch_fields = (
                        "attempt",
                        "command_uuid",
                        "claim_uuid",
                        "fences",
                        "effect_uuid",
                        "parameter_hash",
                        "expected_change_set",
                    )
                    missing_credentials = [
                        field for field in required_dispatch_fields if field not in dispatching
                    ]
                    if missing_credentials:
                        raise ExecutionPolicyError(
                            "持久派发凭据不完整：" + ",".join(sorted(missing_credentials))
                        )
                    for field in required_dispatch_fields:
                        payload[field] = dispatching[field]
                    if isinstance(dispatching.get("resolved_args"), Mapping):
                        resolved_args = dict(dispatching["resolved_args"])
                        payload["action_args"] = resolved_args
                    if isinstance(dispatching.get("execution_locks"), list):
                        execution_locks = list(dispatching["execution_locks"])
                        selected_lock_keys = {
                            str(item.get("lock_key") or "")
                            for item in execution_locks
                            if isinstance(item, Mapping)
                        }
                        if "" in selected_lock_keys:
                            raise ExecutionPolicyError("库存权威选中的执行锁缺少规范 lock_key")
                        lock_keys = selected_lock_keys
                        active_resource_lock_keys = self._active_use_resource_keys(
                            run,
                            task.node,
                            plan=_plan,
                            plan_resource_keys=plan_resource_keys,
                            requested_lock_keys=lock_keys,
                        )
                    selected_transfer = dispatching.get("transfer_dispatch_condition")
                    if isinstance(selected_transfer, Mapping):
                        selected_site_audit = dispatching.get("site_selection")
                        site_audit_attributes: dict[str, str] = {}
                        if isinstance(selected_site_audit, Mapping):
                            site_audit_attributes = {
                                "site.selector.group_key": str(
                                    selected_site_audit.get("group_key") or ""
                                ),
                                "site.selector.strategy": str(
                                    selected_site_audit.get("strategy") or ""
                                ),
                                "site.selector.fingerprint": str(
                                    selected_site_audit.get("fingerprint") or ""
                                ),
                                "site.selector.requested_reference": str(
                                    selected_site_audit.get("requested_reference") or ""
                                ),
                            }
                        action_trace.event(
                            "action.target_site.selected",
                            {
                                "workflow.job.uuid": job_id,
                                "material.uuid": str(selected_transfer.get("material_uuid") or ""),
                                "target.site.uuid": str(
                                    selected_transfer.get("target_site_uuid") or ""
                                ),
                                "target.owner.uuid": str(
                                    selected_transfer.get("target_owner_material_uuid") or ""
                                ),
                                **site_audit_attributes,
                            },
                        )
                    dispatch_intent_committed = True
                    # 派发意图持久化后，先保守登记本地在途作业和动作物料锁，再
                    # 调用不可原子确认的执行适配器。适配器异常不得回滚这些事实。
                    run.mark_dispatched(task.node.id)
                    self._inflight[job_id] = DispatchedJob(
                        job_id=job_id,
                        workflow_id=task.workflow_id,
                        node_id=task.node.id,
                        device_action_key=action_key,
                        dispatched_at=self._clock(),
                        device_id=selected_device_id,
                        device_material_uuid=selected_device_material_uuid,
                        action_name=task.node.action_name,
                        resolved_args=dict(resolved_args),
                        dispatch_credentials={
                            field: deepcopy(dispatching[field])
                            for field in required_dispatch_fields
                        },
                        resource_lock_keys=set(lock_keys),
                        active_resource_lock_keys=set(active_resource_lock_keys),
                        resource_plan_id=task.node.resource_plan_id,
                        resource_interval_ids=list(task.node.resource_interval_ids),
                        resource_acquire_set_id=task.node.resource_acquire_set_id,
                        estimated_s=estimated_s,
                        estimate_source=estimate_source,
                        timing_origin_observed=True,
                    )
                    self._open_resource_intervals_for_job(
                        run,
                        self._inflight[job_id],
                    )
                    if lock_keys:
                        self._job_resource_locks[job_id] = lock_keys
                        held_resource_locks |= lock_keys
                    if not manual_confirm:
                        self._dispatcher.dispatch(payload)
                        if not getattr(self._dispatcher, "deferred_start", False):
                            self._notify_job_dispatch_accepted(job_id)
            except BaseException as exc:
                action_trace.fail(exc)
                action_trace.end()
                self._job_spans.pop(job_id, None)
                if dispatch_intent_committed:
                    try:
                        self._notify_job_dispatch_uncertain(
                            job_id,
                            "local_dispatch_acceptance_unknown",
                        )
                    except BaseException as projection_error:
                        raise projection_error from exc
                raise
            # 人工确认节点已完整准入并登记为在途，但批准前不进入执行器。
            action_trace.event(
                "action.dispatched",
                {
                    "workflow.job.uuid": job_id,
                    "action.estimate.seconds": estimated_s,
                    "action.estimate.source": estimate_source,
                },
            )
            if not bypass_device_lock:
                # 同轮立即登记两种键；后续候选即使动作不同，也不能绕过设备互斥。
                busy.update((action_key, device_key))
            # ``dispatched_item`` 同时供返回值、监控和标准 Task/Job 状态投影使用。
            dispatched_item = {
                "job_id": job_id,
                "workflow_id": task.workflow_id,
                "node_id": task.node.id,
                "device_action_key": action_key,
                "estimated_s": round(estimated_s, 3),
                "estimate_source": estimate_source,
            }
            dispatched.append(dispatched_item)
            self._emit_monitor(
                "action",
                "job_dispatched",
                {
                    "job_id": job_id,
                    "workflow_id": task.workflow_id,
                    "node_id": task.node.id,
                    "device_id": selected_device_id,
                    "action_name": task.node.action_name,
                    "device_action_key": action_key,
                    "estimated_s": round(estimated_s, 3),
                    "estimate_source": estimate_source,
                    "manual_confirm": manual_confirm,
                },
            )
            if not manual_confirm:
                self._emit_monitor(
                    "device",
                    "device_busy",
                    {
                        "device_id": selected_device_id,
                        "action_name": task.node.action_name,
                        "device_action_key": action_key,
                        "job_id": job_id,
                        "workflow_id": task.workflow_id,
                    },
                )

        if ready:
            self._emit_monitor(
                "scheduler",
                "reschedule",
                {
                    "round": self._reschedule_count,
                    "ready": len(ready),
                    "dispatched": len(dispatched),
                },
            )
        return dispatched, serial_blocker

    # 终态集合与云端 workflow_task 一致；TIMEOUT 当前由云端判定，列入以备
    # Edge 后续本地超时（词汇不再变更）。
    _TERMINAL_STATES = (
        WorkflowState.SUCCESS,
        WorkflowState.FAILED,
        WorkflowState.CANCELED,
        WorkflowState.TIMEOUT,
    )

    def _collect_terminal_notifications(self) -> list[tuple[str, str]]:
        """收集已经物理结算的终态工作流。

        参数：无；调用方必须持有调度器锁。返回：尚未通知且已经没有在途作业的
        ``(workflow_id, state)`` 列表。仍有设备动作在途的失败任务延后通知与库存
        释放，避免业务失败先于物理停止时让同一物料被再次使用。
        """

        pending: list[tuple[str, str]] = []
        for wid, run in self._workflows.items():
            if (
                run.state not in self._TERMINAL_STATES
                or wid in self._notified_workflows
                or any(job.workflow_id == wid for job in self._inflight.values())
            ):
                continue
            self._clear_interval_holders(
                wid,
                preserve_explicit=run.state
                in {WorkflowState.FAILED, WorkflowState.CANCELED, WorkflowState.TIMEOUT},
            )
            self._notified_workflows.add(wid)
            pending.append((wid, run.state.value))
            self._emit_monitor(
                "scheduler",
                "workflow_state",
                {"workflow_id": wid, "state": run.state.value},
            )
            self._safe_history("record_state", wid, run.state.value)
        return pending

    def _fire_notifications(self, notifications: list[tuple[str, str]]) -> None:
        for wid, state in notifications:
            workflow_trace = self._workflow_spans.get(wid)
            activation = (
                workflow_trace.activate()
                if workflow_trace is not None
                else span("workflow.task.terminal")
            )
            with activation:
                add_event(
                    "workflow.task.terminal",
                    {"workflow.uuid": wid, "workflow.state": state},
                    span=workflow_trace.span if workflow_trace is not None else None,
                )
                if workflow_trace is not None and state != WorkflowState.SUCCESS.value:
                    workflow_trace.error(f"workflow {state}")
                # 终态工作流释放剩余 active 预留（幂等，依据 DB 状态而非内存）
                run = self._workflows.get(wid)
                plan = self._resource_plan_for_spec(run.spec) if run is not None else None
                retains_explicit_failure = bool(
                    state != WorkflowState.SUCCESS.value
                    and plan is not None
                    and any(interval.explicit_boundary for interval in plan.intervals)
                )
                if (
                    wid in self._material_workflows
                    and not retains_explicit_failure
                    and self._can_release_workflow_inventory(wid)
                ):
                    self._safe_inventory_call(
                        "release_workflow",
                        wid,
                        reason=f"workflow_{state}",
                    )
                if self._workflow_state_listener is not None:
                    try:
                        self._workflow_state_listener(wid, state)
                    except Exception:
                        logger.exception(
                            "[EdgeScheduler] workflow state listener failed"
                        )
            if workflow_trace is not None:
                workflow_trace.end()
                self._workflow_spans.pop(wid, None)

    def set_workflow_inventory_release_guard(
        self, guard: Callable[[str], bool] | None
    ) -> None:
        """由持久工作流权威判断终态通知是否可以释放任务库存预留。"""
        with self._lock:
            self._workflow_inventory_release_guard = guard

    def _can_release_workflow_inventory(self, workflow_id: str) -> bool:
        """内存终态不证明跨库结算完成；权威读取失败时仍保留全部预留。"""
        guard = self._workflow_inventory_release_guard
        if guard is None:
            return True
        try:
            return guard(workflow_id) is True
        except Exception:
            logger.exception("[EdgeScheduler] 无法确认库存结算，保留任务预留：%s", workflow_id)
            return False

    def _safe_inventory_call(self, method: str, *args: Any, **kwargs: Any) -> None:
        """调用 inventory（release/quarantine 等善后操作）；失败记日志不阻断调度。"""
        if self._inventory is None:
            return
        try:
            getattr(self._inventory, method)(*args, **kwargs)
        except Exception:
            logger.exception("[EdgeScheduler] inventory.%s failed", method)

    # ── 执行资源锁 ───────────────────────────────────────────

    def _resolve_device_target(
        self,
        node: Any,
        busy_keys: set[str],
        *,
        continuation_keys: set[str] | None = None,
    ) -> ResolvedDeviceTarget:
        """返回固定设备，或按冻结类型从当前注册选择首个可用实例。"""

        device_id = str(getattr(node, "device_id", "") or "").strip()
        if device_id:
            material_uuid = str(getattr(node, "device_material_uuid", "") or "").strip()
            if self._device_target_resolver is not None:
                return self._device_target_resolver(
                    {
                        "mode": "fixed",
                        "local_device_id": device_id,
                        "material_uuid": material_uuid,
                    },
                    str(getattr(node, "action_name", "") or ""),
                    set(busy_keys)
                    | (self._held_resource_locks() - set(continuation_keys or ())),
                )
            if self.physical_dispatch_enabled:
                raise DeviceTargetUnavailable(
                    "device_authority_unavailable",
                    "物理派发的固定设备未装配当前注册状态权威",
                )
            return ResolvedDeviceTarget(
                local_device_id=device_id,
                material_uuid=material_uuid,
            )
        selector = getattr(node, "device_selector", None)
        if not isinstance(selector, Mapping) or not selector:
            raise DeviceTargetUnavailable(
                "invalid_device_selector",
                "设备动作没有固定设备或动态设备选择器",
            )
        if self._device_target_resolver is None:
            raise DeviceTargetUnavailable(
                "device_authority_unavailable",
                "调度器尚未装配动态设备注册解析器",
            )
        return self._device_target_resolver(
            selector,
            str(getattr(node, "action_name", "") or ""),
            set(busy_keys)
            | (self._held_resource_locks() - set(continuation_keys or ())),
        )

    def preflight_device_target(
        self, planned_node: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """用本轮派发相同注册事实只读检查一个冻结节点的设备目标。"""

        if self._device_target_resolver is None:
            raise DeviceTargetUnavailable(
                "device_authority_unavailable", "未装配当前设备注册状态权威"
            )
        action_name = str(planned_node.get("action_name") or "").strip()
        device_id = str(planned_node.get("device_id") or "").strip()
        if device_id:
            selector: Mapping[str, Any] = {
                "mode": "fixed",
                "local_device_id": device_id,
                "material_uuid": str(planned_node.get("material_uuid") or "").strip(),
            }
        else:
            raw_selector = planned_node.get("device_selector")
            if not isinstance(raw_selector, Mapping):
                raise DeviceTargetUnavailable(
                    "invalid_device_selector", "冻结节点缺少设备选择器"
                )
            selector = raw_selector
        selected = self._device_target_resolver(
            selector,
            action_name,
            self._busy_keys() | self._held_resource_locks(),
        )
        return {
            "local_device_id": selected.local_device_id,
            "material_uuid": selected.material_uuid,
        }

    def _held_resource_locks(self) -> set[str]:
        held: set[str] = set()
        for keys in self._job_resource_locks.values():
            held |= keys
        for keys in self._interval_resource_holders.values():
            held |= keys
        return held

    def _validate_station_admission(self, spec: WorkflowSpec) -> None:
        """校验绑定及运行能力；没有时间规划时证明未完成任务的联合资源图。"""
        candidate = self._resource_plan_for_spec(spec)
        if candidate is None:
            return
        if candidate.binding_state != "bound":
            raise ExecutionPolicyError("资源计划必须在派发前绑定到实际实例")
        if SCHEDULER_RESOURCE_ORDER_CAPABILITY in candidate.capabilities and not self.time_scheduling_enabled:
            raise ExecutionPolicyError("资源计划要求启用实际派发顺序门禁")
        if self.time_scheduling_enabled:
            return
        plans = [candidate]
        for run in self._workflows.values():
            if run.state in self._TERMINAL_STATES:
                continue
            other = self._resource_plan_for_spec(run.spec)
            if other is not None:
                plans.append(other)
        validate_station_resource_plans(plans)

    def _resource_plan_for_spec(self, spec: WorkflowSpec) -> ResourcePlan | None:
        """恢复并缓存当前 WorkflowSpec 的 bound 资源计划。"""

        raw_plan = spec.resource_plan
        if not isinstance(raw_plan, Mapping):
            return None
        cached = self._resource_plans.get(spec.workflow_id)
        if cached is not None and cached.plan_id == str(raw_plan.get("plan_id") or ""):
            return cached
        plan = deserialize_resource_plan(raw_plan)
        self._resource_plans[spec.workflow_id] = plan
        return plan

    def _interval_projection(
        self,
        run: WorkflowRun,
        node: WorkflowNode,
        *,
        current_job_id: str | None = None,
    ) -> tuple[ResourcePlan | None, set[str], set[str], dict[str, set[str]]]:
        """返回节点计划资源、继承资源及锁键到区间身份的映射。"""

        plan = self._resource_plan_for_spec(run.spec)
        if plan is None:
            return None, set(), set(), {}
        node_projection = resource_plan_for_node(plan, run.resource_template_node_uuid(node.id))
        interval_ids = set(node.resource_interval_ids)
        if node.resource_plan_id != plan.plan_id:
            raise ExecutionPolicyError("节点引用的资源计划身份与工作流冻结计划不一致")
        available_interval_ids = {
            str(item.get("interval_id") or "") for item in node_projection["intervals"]
        }
        if interval_ids != available_interval_ids:
            raise ExecutionPolicyError("节点连续区间投影与冻结资源计划不一致")
        acquire_set_ids = [
            str(item.get("acquire_set_id") or "")
            for item in node_projection["acquire_sets"]
        ]
        if len(acquire_set_ids) > 1:
            raise ExecutionPolicyError("资源计划为同一节点声明了多个新增资源集合")
        expected_acquire_set_id = acquire_set_ids[0] if acquire_set_ids else ""
        if node.resource_acquire_set_id != expected_acquire_set_id:
            raise ExecutionPolicyError("节点新增资源集合投影与冻结资源计划不一致")
        intervals = [
            interval
            for interval in node_projection["intervals"]
            if str(interval.get("interval_id") or "") in interval_ids
        ]
        resources = {item.resource_id: item for item in plan.resources}
        interval_ids_by_lock: dict[str, set[str]] = {}
        for interval in intervals:
            resource = resources.get(str(interval.get("resource_id") or ""))
            if resource is None:
                raise ExecutionPolicyError(
                    "资源计划区间引用了不存在的资源：" f"{interval.get('resource_id')}"
                )
            lock_key = _bound_resource_lock_key(
                {
                    "canonical_key": resource.canonical_key,
                    "kind": resource.kind,
                    "instance_uuid": resource.instance_uuid,
                }
            )
            if lock_key:
                interval_ids_by_lock.setdefault(lock_key, set()).add(str(interval["interval_id"]))
        plan_keys = set(interval_ids_by_lock)
        projected_keys = {item["lock_key"] for item in self._execution_lock_descriptors(plan_keys)}
        if plan_keys != projected_keys:
            raise ExecutionPolicyError(
                "资源计划包含库存无法表示的资源键：" + ",".join(sorted(plan_keys - projected_keys))
            )
        explicit_interval_ids = {
            str(interval.get("interval_id") or "")
            for interval in intervals
            if bool(interval.get("explicit_boundary"))
        }
        for interval_id in sorted(interval_ids):
            state_key = (run.spec.workflow_id, interval_id)
            if state_key not in self._opened_resource_intervals:
                continue
            expected_keys = {
                lock_key
                for lock_key, lock_interval_ids in interval_ids_by_lock.items()
                if interval_id in lock_interval_ids
            }
            holder_keys = set(self._interval_resource_holders.get(state_key, set()))
            current_job = (
                self._inflight.get(current_job_id) if current_job_id is not None else None
            )
            if (
                current_job is not None
                and current_job.workflow_id == run.spec.workflow_id
                and interval_id in current_job.resource_interval_ids
            ):
                holder_keys |= current_job.resource_lock_keys
            if interval_id in explicit_interval_ids:
                holder_keys |= {
                    lock_key
                    for job in self._inflight.values()
                    if job.workflow_id == run.spec.workflow_id
                    and interval_id in job.resource_interval_ids
                    for lock_key in job.resource_lock_keys
                }
            if not expected_keys <= holder_keys:
                raise ExecutionPolicyError(
                    "已打开的连续资源区间缺少可继承所有权：" + interval_id
                )
        inherited: set[str] = set()
        for interval_id in interval_ids:
            inherited |= self._interval_resource_holders.get(
                (run.spec.workflow_id, interval_id),
                set(),
            )
        inherited |= set().union(*self._shared_scope_holders(run.spec.workflow_id, node).values())
        return plan, plan_keys, inherited, interval_ids_by_lock

    def _active_use_resource_keys(
        self,
        run: WorkflowRun,
        node: WorkflowNode,
        *,
        plan: ResourcePlan | None,
        plan_resource_keys: set[str],
        requested_lock_keys: set[str],
    ) -> set[str]:
        """把作用域 reservation 与当前 Job 的实际操作资源分离。

        新计划在 metadata 中冻结节点直接资源；仅由外层作用域带入的键可由
        同 Task 的兄弟 Job 共同预持有，却不算兄弟正在操作。旧计划没有这份
        证明时关闭式把全部计划键视为 active-use，避免恢复后放宽互斥。
        """

        if plan is None:
            return normalize_resource_lock_keys(requested_lock_keys)
        raw_by_node = plan.metadata.get("active_resource_ids_by_node")
        if raw_by_node is None:
            planned_active_keys = set(plan_resource_keys)
        else:
            if not isinstance(raw_by_node, Mapping):
                raise ExecutionPolicyError("资源计划 active-use 投影必须是对象")
            template_node_uuid = run.resource_template_node_uuid(node.id)
            raw_resource_ids = raw_by_node.get(template_node_uuid, ())
            if not isinstance(raw_resource_ids, Sequence) or isinstance(
                raw_resource_ids,
                (str, bytes),
            ):
                raise ExecutionPolicyError("资源计划节点 active-use 集合必须是数组")
            resource_ids = [str(value) for value in raw_resource_ids]
            if len(resource_ids) != len(set(resource_ids)) or any(
                not value for value in resource_ids
            ):
                raise ExecutionPolicyError("资源计划节点 active-use 身份无效")
            resources = {resource.resource_id: resource for resource in plan.resources}
            if not set(resource_ids) <= set(resources):
                raise ExecutionPolicyError("资源计划节点 active-use 引用未知资源")
            planned_active_keys = {
                _bound_resource_lock_key(asdict(resources[resource_id]))
                for resource_id in resource_ids
            }
            if "" in planned_active_keys or not planned_active_keys <= plan_resource_keys:
                raise ExecutionPolicyError("资源计划节点 active-use 不属于当前资源区间")
        scope_only_keys = plan_resource_keys - planned_active_keys
        return normalize_resource_lock_keys(
            (set(requested_lock_keys) - scope_only_keys) | planned_active_keys
        )

    @staticmethod
    def _validate_restored_manual_job_identity(
        spec: WorkflowSpec,
        node: WorkflowNode,
        job: DispatchedJob,
    ) -> None:
        """验证恢复 Job 身份与冻结节点完全一致。

        恢复数据是已跨过持久准入的安全事实，不能用调用方传入的
        动作键、资源区间或工作流身份替换冻结计划。
        """

        if job.workflow_id != spec.workflow_id:
            raise ExecutionPolicyError("恢复的人工确认作业与工作流身份不一致")
        if (
            job.device_id != node.device_id
            or job.action_name != node.action_name
            or job.device_action_key != node.device_action_key
        ):
            raise ExecutionPolicyError("恢复的人工确认作业执行身份与冻结节点不一致")
        if (
            node.device_material_uuid
            and job.device_material_uuid != node.device_material_uuid
        ):
            raise ExecutionPolicyError("恢复的人工确认作业设备物料身份不一致")
        if (
            job.resource_plan_id != node.resource_plan_id
            or set(job.resource_interval_ids) != set(node.resource_interval_ids)
            or job.resource_acquire_set_id != node.resource_acquire_set_id
        ):
            raise ExecutionPolicyError("恢复的人工确认作业资源计划投影不一致")

    def _refresh_job_active_use(
        self,
        run: WorkflowRun,
        node: WorkflowNode,
        job: DispatchedJob,
        *,
        require_all_plan_keys: bool,
    ) -> None:
        """从冻结计划重算 Job active-use，不信任恢复载荷缓存。"""

        plan, plan_keys, _inherited, _intervals = self._interval_projection(
            run,
            node,
            current_job_id=job.job_id,
        )
        requested_keys = normalize_resource_lock_keys(job.resource_lock_keys)
        if require_all_plan_keys and plan is not None:
            missing_plan_keys = normalize_resource_lock_keys(plan_keys) - requested_keys
            if missing_plan_keys:
                raise ExecutionPolicyError(
                    "恢复的人工确认作业缺少冻结计划资源："
                    + ",".join(sorted(missing_plan_keys))
                )
        job.resource_lock_keys = requested_keys
        job.active_resource_lock_keys = self._active_use_resource_keys(
            run,
            node,
            plan=plan,
            plan_resource_keys=plan_keys,
            requested_lock_keys=requested_keys,
        )

    def _inflight_job_conflict(
        self,
        candidate: DispatchedJob,
    ) -> tuple[DispatchedJob, set[str], bool] | None:
        """返回候选 Job 与现有在途 Job 的 active-use 或设备冲突。"""

        candidate_device = candidate.device_material_uuid or candidate.device_id
        for other in sorted(self._inflight.values(), key=lambda item: item.job_id):
            if other.job_id == candidate.job_id:
                continue
            other_device = other.device_material_uuid or other.device_id
            device_conflict = bool(
                (
                    candidate.device_action_key
                    and candidate.device_action_key == other.device_action_key
                )
                or (candidate_device and candidate_device == other_device)
            )
            if other_device and device_lock_key(other_device) in other.operator_released_keys:
                device_conflict = False
            active_conflicts = conflicting_resource_lock_keys(
                candidate.active_resource_lock_keys,
                other.active_resource_lock_keys,
            )
            if device_conflict or active_conflicts:
                return other, active_conflicts, device_conflict
        return None

    def _shared_scope_holders(self, workflow_id: str, node: WorkflowNode) -> dict[str, set[str]]:
        """同一资源的显式范围可经共同动作传递所有权，隐式持料延伸不扩大共享。"""
        run = self._workflows.get(workflow_id)
        plan = self._resource_plan_for_spec(run.spec) if run is not None else None
        if plan is None:
            return {}
        shared_ids = {
            i.interval_id
            for i in plan.intervals
            if i.explicit_boundary and i.interval_id in node.resource_interval_ids
        }
        keys = {
            i.interval_id: _bound_resource_lock_key(
                asdict(next(r for r in plan.resources if r.resource_id == i.resource_id))
            )
            for i in plan.intervals
            if i.interval_id in shared_ids
        }
        template_node = run.resource_template_node_uuid(node.id)
        intervals = {i.interval_id: i for i in plan.intervals}
        shared_members: dict[str, set[str]] = {}
        for interval_id in shared_ids:
            interval = intervals[interval_id]
            members = {template_node}
            pending = [set(scope.node_uuids) & set(interval.node_uuids)
                       for scope in plan.scopes if interval.resource_id in scope.resource_ids]
            # ABCD 与 DE 通过共同的 D 共享该资源；没有共同动作的同名资源
            # 不连接。物理持料自动延长的节点也必须有显式范围连接才可共享。
            while True:
                connected = [scope_nodes for scope_nodes in pending if members & scope_nodes]
                if not connected:
                    break
                for scope_nodes in connected:
                    members.update(scope_nodes)
                    pending.remove(scope_nodes)
            shared_members[interval_id] = members
        holders: dict[str, set[str]] = {}
        for job in self._inflight.values():
            if job.workflow_id != workflow_id:
                continue
            template_holder = run.resource_template_node_uuid(job.node_id)
            shared_keys = {
                keys[interval_id]
                for interval_id in shared_ids & set(job.resource_interval_ids)
                if template_holder in shared_members[interval_id]
            } - job.operator_released_keys
            if shared_keys:
                holders[job.job_id] = shared_keys
        return holders

    def _continuation_resource_keys(
        self,
        workflow_id: str,
        node: WorkflowNode,
    ) -> set[str]:
        return set().union(*self._shared_scope_holders(workflow_id, node).values()) | set(
            key
            for interval_id in node.resource_interval_ids
            for key in self._interval_resource_holders.get(
                (workflow_id, interval_id),
                set(),
            )
        )

    def _continuation_resource_job_uuids(
        self,
        workflow_id: str,
        node: WorkflowNode,
    ) -> set[str]:
        """返回当前区间预持有资源对应的上一 Job 身份。"""

        return set(self._shared_scope_holders(workflow_id, node)) | {
            job_uuid
            for interval_id in node.resource_interval_ids
            for job_uuid in self._interval_resource_holder_jobs.get(
                (workflow_id, interval_id), set()
            )
        }

    def _record_interval_handoff(
        self,
        job: DispatchedJob,
        *,
        success: bool,
    ) -> None:
        """在明确成功后保留未到释放节点的区间资源。"""

        run = self._workflows.get(job.workflow_id)
        if run is None or not job.resource_interval_ids:
            return
        plan = self._resource_plan_for_spec(run.spec)
        if plan is None:
            return
        intervals = {item.interval_id: item for item in plan.intervals}
        for interval_id in job.resource_interval_ids:
            interval = intervals.get(interval_id)
            key = (job.workflow_id, interval_id)
            resources = {item.resource_id: item for item in plan.resources}
            held = (
                {
                    _bound_resource_lock_key(
                        {
                            "canonical_key": resources[interval.resource_id].canonical_key,
                            "kind": resources[interval.resource_id].kind,
                            "instance_uuid": resources[interval.resource_id].instance_uuid,
                        }
                    )
                }
                & set(job.resource_lock_keys)
                if interval is not None
                else set()
            )
            failure_latched = bool(
                interval is not None
                and interval.explicit_boundary
                and (
                    not success
                    or run.state
                    in {
                        WorkflowState.FAILED,
                        WorkflowState.CANCELED,
                        WorkflowState.TIMEOUT,
                    }
                )
            )
            if failure_latched:
                # 显式连续区间的异常结果是单向闩锁。保留已经取得的物理键，
                # 直到任务级 ``unlock_resources`` 完成人工整组释放。
                if held:
                    self._interval_resource_holders.setdefault(key, set()).update(held)
                    self._interval_resource_holder_jobs.setdefault(key, set()).add(
                        job.job_id
                    )
                    self._opened_resource_intervals.add(key)
                continue
            if (
                not success
                or interval is None
                or all(
                    self._resource_interval_member_completed(
                        run,
                        member,
                        current_node_id=job.node_id,
                    )
                    for member in interval.node_uuids
                )
            ):
                self._interval_resource_holders.pop(key, None)
                self._interval_resource_holder_jobs.pop(key, None)
                self._opened_resource_intervals.discard(key)
                continue
            if held:
                self._interval_resource_holders[key] = held
                self._interval_resource_holder_jobs.setdefault(key, set()).add(job.job_id)

    @staticmethod
    def _resource_interval_member_completed(
        run: WorkflowRun,
        member_node_id: str,
        *,
        current_node_id: str = "",
    ) -> bool:
        """判断资源区间成员是否已有明确完成事实。"""

        runtime_node_id = run.resource_runtime_node_uuid(member_node_id)
        if runtime_node_id == current_node_id:
            return True
        state = run.node_state(runtime_node_id)
        terminal_states = {
            NodeState.SUCCESS,
            NodeState.SKIPPED,
            NodeState.FAILED,
            NodeState.CANCELED,
            NodeState.TIMEOUT,
        }
        if state in terminal_states:
            return True
        # RepeatUntil 退出后会销毁本轮子 DAG；此时动态 body 身份已不再能从
        # ``node_state`` 读取，但控制节点的确定终态证明它拥有的模板成员均已
        # 结算。嵌套循环从最内层控制节点往外逐层判断：内层仍在运行时不能提前
        # 释放；而当外层控制节点已终态（例如整个分支被条件跳过、内层循环从未
        # 物化，因此内层控制节点根本没有状态）时，成员同样已经结算——否则
        # 跨越该分支的显式资源区间会在内存里永久持有（pTLC s9 复核分支被跳过
        # 后，s10 首个动作等 photoscrape/control 直到超时）。
        def owning_repeat_controls(
            regions: Mapping[str, RepeatUntilRegion],
        ) -> list[str]:
            for control_node_id, region in regions.items():
                nested_chain = owning_repeat_controls(region.repeat_regions)
                if nested_chain:
                    return [*nested_chain, control_node_id]
                if any(node.id == member_node_id for node in region.nodes):
                    return [control_node_id]
            return []

        for owner_template_id in owning_repeat_controls(run.spec.repeat_regions):
            owner_runtime_id = run.resource_runtime_node_uuid(owner_template_id)
            if run.node_state(owner_runtime_id) in terminal_states:
                return True
        return (
            state is None
            and runtime_node_id == member_node_id
            and member_node_id
            in set(run.spec.resource_coordinator_node_ids)
        )

    @staticmethod
    def _is_physical_resource_node(node: WorkflowNode) -> bool:
        """判断节点是否真正越过物理派发边界。"""

        return node.executor_kind not in {
            "condition",
            "repeat_until",
            "material_source",
            "workflow_input",
            "workflow_output",
        }

    def _open_resource_intervals_for_job(
        self,
        run: WorkflowRun,
        job: DispatchedJob,
    ) -> None:
        """在物理 Job 登记为在途后标记本轮连续区间已经开始。"""

        node = run.node(job.node_id)
        if node is None or not self._is_physical_resource_node(node):
            return
        self._opened_resource_intervals.update(
            (job.workflow_id, interval_id)
            for interval_id in job.resource_interval_ids
        )

    def _clear_interval_holders(
        self,
        workflow_id: str,
        *,
        preserve_explicit: bool = False,
    ) -> None:
        explicit_ids: set[str] = set()
        if preserve_explicit:
            run = self._workflows.get(workflow_id)
            plan = self._resource_plan_for_spec(run.spec) if run is not None else None
            if plan is not None:
                explicit_ids = {
                    interval.interval_id
                    for interval in plan.intervals
                    if interval.explicit_boundary
                }
        for key in tuple(self._interval_resource_holders):
            if key[0] == workflow_id and key[1] not in explicit_ids:
                self._interval_resource_holders.pop(key, None)
                self._interval_resource_holder_jobs.pop(key, None)
        self._opened_resource_intervals = {
            key
            for key in self._opened_resource_intervals
            if key[0] != workflow_id or key[1] in explicit_ids
        }

    def _release_completed_interval_holders(
        self,
        workflow_id: str,
    ) -> None:
        """本地控制提交后立即终止所有已经完整结算的连续持有。"""

        run = self._workflows.get(workflow_id)
        plan = self._resource_plan_for_spec(run.spec) if run is not None else None
        if plan is None:
            return
        finished_intervals = {
            interval.interval_id
            for interval in plan.intervals
            if all(
                self._resource_interval_member_completed(run, member)
                for member in interval.node_uuids
            )
        }
        for key in tuple(self._interval_resource_holders):
            if key[0] == workflow_id and key[1] in finished_intervals:
                self._interval_resource_holders.pop(key, None)
                self._interval_resource_holder_jobs.pop(key, None)
        self._opened_resource_intervals.difference_update(
            (workflow_id, interval_id) for interval_id in finished_intervals
        )

    @staticmethod
    def _execution_lock_descriptors(
        lock_keys: set[str],
    ) -> list[dict[str, Any]]:
        """把规范锁键投影为库存 DispatchResource 的公共描述形状。"""

        result: list[dict[str, Any]] = []
        for lock_key in sorted(lock_keys):
            scope = canonical_resource_lock_scope(lock_key)
            parts = lock_key.split("/")
            if scope == "resource":
                result.append({"lock_key": lock_key, "scope": "resource"})
            elif scope == "device":
                result.append(
                    {
                        "lock_key": lock_key,
                        "scope": "device",
                        "material_uuid": parts[2],
                    }
                )
            elif scope == "material":
                result.append(
                    {
                        "lock_key": lock_key,
                        "scope": "material",
                        "material_uuid": parts[1],
                    }
                )
            elif scope == "material_site":
                result.append(
                    {
                        "lock_key": lock_key,
                        "scope": "material_site",
                        "material_uuid": parts[1],
                        "site_uuid": parts[3],
                    }
                )
        return result

    def _execution_policy_with_runtime_site_group(
        self,
        node: Any,
        resolved_args: Mapping[str, Any],
        *,
        transfer_contract: Mapping[str, str] | None,
        unavailable_site_uuids: Sequence[str],
    ) -> Mapping[str, Any]:
        """所属物料到派发才有 UUID 时，把命名库位组收成第一个能放的孔。

        参数：``node`` 携带提交时记下的 ``deferred_site_group``；``resolved_args``
        是上游已经选出的所属物料和待放物料；``transfer_contract`` 指出这两个参数
        名；``unavailable_site_uuids`` 是本轮其他作业已经申领的孔。返回：原策略，
        或只含选中孔 UUID 的副本。作者写死的孔名不走这里。异常：组不存在、孔都
        被占或所属物料还不是 UUID 时抛 ``SiteTargetResolutionError``。
        """

        policy = node.execution_policy
        if not isinstance(policy, Mapping):
            return {}
        marker = policy.get("deferred_site_group")
        if (
            transfer_contract is None
            or not isinstance(marker, Mapping)
            or policy.get("target_site_group")
        ):
            return policy
        group_key = str(marker.get("group_key") or "").strip()
        owner_parameter = str(marker.get("owner_parameter") or "").strip()
        if not group_key or not owner_parameter:
            raise SiteTargetResolutionError(
                "deferred_site_group_invalid",
                "运行时库位组缺少组键或所属资源参数",
            )
        owner_uuid = _resource_argument_uuid(
            resolved_args.get(owner_parameter),
            argument_name=owner_parameter,
        )
        occupant_uuid = _resource_argument_uuid(
            resolved_args.get(transfer_contract["material_param"]),
            argument_name=transfer_contract["material_param"],
        )
        station = self._required_station_resources()
        try:
            selection = station.resolve_named_site_group(
                owner_material_uuid=owner_uuid,
                group_key=group_key,
            )
        except StationResourceError as error:
            raise SiteTargetResolutionError(error.code, error.message) from error
        raw_site_uuids = selection.get("site_uuids") if isinstance(selection, Mapping) else None
        if not isinstance(raw_site_uuids, Sequence) or isinstance(raw_site_uuids, (str, bytes)):
            raise SiteTargetResolutionError(
                "site_group_unresolved",
                f"命名库位组 {group_key} 没有返回候选库位",
            )
        chosen = resolve_site_target(
            station,
            owner_material_uuid=owner_uuid,
            site_uuids=tuple(str(item) for item in raw_site_uuids),
            occupant_material_uuid=occupant_uuid,
            unavailable_site_uuids=tuple(unavailable_site_uuids),
            require_available=True,
        )
        updated = deepcopy(dict(policy))
        updated["target_site_group"] = [chosen.uuid]
        return updated

    def _check_pick_destination(self, target_key: str, material_uuid: str) -> None:
        target = parse_canonical_resource_lock_key(target_key)
        if target is None or target.scope != "material_site":
            raise ValueError("拆分搬运最终目标缺少绑定库位")
        self._required_station_resources().check_pick_destination(target.material_uuid, target.site_uuid, material_uuid)

    def _resolve_transfer_site_target(
        self,
        node: Any,
        resolved_args: dict[str, Any],
        *,
        transfer_contract: Mapping[str, str] | None = None,
        site_uuids: tuple[str, ...] = (),
        unavailable_site_uuids: tuple[str, ...] = (),
        require_available: bool = True,
        reserved_pick_target: str = "",
    ) -> tuple[dict[str, Any], ResolvedSiteTarget | None]:
        """解析转运动作的目标库位并规范化设备执行名称。

        参数：``node`` 是候选工作流节点；``resolved_args`` 是合并上游输出后的
        最终参数；``transfer_contract`` 是 AST 冻结的转运参数与夹爪角色映射；
        ``site_uuids`` 是冻结等价组，``unavailable_site_uuids`` 是本轮已有作业
        执行占用选中的库位；``require_available`` 为假时仅解析候选身份，物理
        可用性延后到 Gate 7 的单一库存事务。返回：规范参数和具体目标；非转运
        动作原样返回。
        异常：合同字段缺失、目标库位无法验证或库存权威不可用时抛
        ``SiteTargetResolutionError``；不根据动作名称或库位名称猜测资源。
        """

        if transfer_contract is None:
            if reserved_pick_target:
                self._check_pick_destination(reserved_pick_target, "")
            return resolved_args, None
        site_uuid_param = transfer_contract["target_site_uuid_param"]
        site_name_param = transfer_contract["target_site_name_param"]
        material_param = transfer_contract["material_param"]
        owner_param = transfer_contract["target_owner_param"]
        site_uuid = str(
            (resolved_args.get(site_uuid_param) if site_uuid_param else "") or ""
        ).strip()
        site_name = str(
            (resolved_args.get(site_name_param) if site_name_param else "") or ""
        ).strip()
        if not site_uuid and not site_name and not site_uuids:
            if transfer_contract["gripper_site_role"]:
                raise SiteTargetResolutionError(
                    "site_selector_missing",
                    "机械臂转运动作必须提供目标库位或显式等价库位组",
                )
            return resolved_args, None

        if self._station_resources is None:
            if site_uuid or transfer_contract["gripper_site_role"]:
                raise SiteTargetResolutionError(
                    "site_authority_unavailable",
                    "机械臂转运或稳定库位解析必须先初始化本地库存权威",
                )
            return resolved_args, None

        mount_uuid = _resource_argument_uuid(
            resolved_args.get(owner_param),
            argument_name=owner_param,
        )
        resource_uuid = _resource_argument_uuid(
            resolved_args.get(material_param),
            argument_name=material_param,
        )
        if reserved_pick_target:
            self._check_pick_destination(reserved_pick_target, resource_uuid)
        target = resolve_site_target(
            self._station_resources,
            owner_material_uuid=mount_uuid,
            site_uuid=site_uuid,
            site_name=site_name,
            site_uuids=site_uuids,
            occupant_material_uuid=resource_uuid,
            unavailable_site_uuids=unavailable_site_uuids,
            require_available=require_available,
        )
        canonical_args = dict(resolved_args)
        # 设备驱动沿用库位名称；稳定 UUID 只用于身份解析和本地互斥。作者写的是上层物料地址
        #（厂家位号）、按地址表正好指到这个库位时，驱动拿到的仍是地址写法。
        resolve_address = self._address_site_resolver(str(target.owner_material_uuid)) if site_name else None
        named_by_address = bool(
            resolve_address is not None
            and site_name.casefold() != str(target.name).casefold()
            and resolve_address(site_name) == str(target.uuid)
        )
        if site_name_param and not named_by_address:
            canonical_args[site_name_param] = target.name
        if site_uuid_param:
            canonical_args[site_uuid_param] = target.uuid
        return canonical_args, target

    def _required_station_resources(self) -> StationResourceInventory:
        """返回已装配的工站资源接口并对缺失配置失败关闭。

        参数：无。返回：设备、库位（Site）与转运库存接口。异常：未装配时抛
        ``TransferResourceSetError``，禁止把缺失库存事实降级成空资源集合。
        """

        if self._station_resources is None:
            raise TransferResourceSetError(
                "station_resource_authority_unavailable",
                "本地库存权威未初始化，无法解析转运完整资源集",
            )
        return self._station_resources

    @staticmethod
    def _transfer_resource_contract(node: Any) -> dict[str, str] | None:
        """读取节点冻结的机械臂转运资源映射。

        参数：``node`` 是调度候选节点。返回：AST 资源合同中的 ``transfer`` 映射；
        非转运动作返回 ``None``。异常：冻结合同 ``transfer`` 不是对象或字段值不是字符串时
        抛 ``TransferResourceSetError``，禁止根据动作实现猜测资源。
        """

        resource_contract = getattr(node, "action_resource_contract", None)
        transfer = (
            resource_contract.get("transfer") if isinstance(resource_contract, Mapping) else None
        )
        if transfer is None:
            # host transfer_resource 只把已到位物料改挂到目标库位，不走机械臂。
            if str(getattr(node, "action_name", "") or "") == "transfer_resource":
                return None
            if getattr(node, "executor_kind", "") == "material_transfer" or (
                isinstance(resource_contract, Mapping) and resource_contract.get("transfer_step")
            ):
                raise TransferResourceSetError(
                    "missing_transfer_resource_contract",
                    "物料转移动作缺少 AST 冻结资源合同",
                )
            return None
        if not isinstance(transfer, Mapping):
            raise TransferResourceSetError(
                "invalid_transfer_resource_contract",
                "冻结动作的 transfer 资源合同不是对象",
            )
        required = {
            "material_param",
            "source_owner_param",
            "source_site_uuid_param",
            "source_site_name_param",
            "target_owner_param",
            "target_site_uuid_param",
            "target_site_name_param",
            "gripper_site_role",
        }
        legacy_required = required - {
            "source_owner_param",
            "source_site_uuid_param",
            "source_site_name_param",
        }
        optional = {"motion_resource_roles", "tool_resource_roles"}
        base_fields = set(transfer) - optional
        valid_roles = all(
            isinstance(transfer[field], (list, tuple))
            and transfer[field]
            and all(isinstance(role, str) and role.strip() for role in transfer[field])
            for field in optional & set(transfer)
        )
        if (
            base_fields not in (required, legacy_required)
            or not valid_roles
            or any(not isinstance(transfer[field], str) for field in base_fields)
        ):
            raise TransferResourceSetError(
                "invalid_transfer_resource_contract",
                "冻结动作的 transfer 资源合同字段非法",
            )
        return {field: str(transfer.get(field) or "") for field in required}

    @staticmethod
    def _transfer_uses_runtime_material(node: Any, transfer_contract: Mapping[str, Any]) -> bool:
        """被搬物料或任一端所属物料只在运行时确定时，本次搬运的库存资源不在冻结计划内。

        这些键仍由库存派发准入在同一事务内逐项复核（来源、目标、夹爪与设备锁），
        只是不要求它们预先出现在计划期资源区间中。
        """
        runtime = set(getattr(node, "runtime_material_parameters", None) or ())
        return any(
            transfer_contract.get(key) and transfer_contract[key] in runtime
            for key in ("material_param", "target_owner_param", "source_owner_param")
        )

    def _address_site_resolver(self, owner_material_uuid: str) -> Callable[[str], str | None] | None:
        """库存支持库位地址时，给出“地址名 → 实际所属载架上的库位 UUID”的反查；否则 ``None``。"""

        lookup = getattr(self._station_resources, "site_uuid_for_address", None)
        if not callable(lookup) or not owner_material_uuid:
            return None
        return lambda name: lookup(owner_material_uuid, name)

    @staticmethod
    def _inject_actual_transfer_source(
        resolved_args: Mapping[str, Any],
        *,
        transfer_contract: Mapping[str, str],
        source_owner_material_uuid: str,
        source_site_uuid: str,
        source_site_name: str,
        address_site_uuid: Callable[[str], str | None] | None = None,
    ) -> dict[str, Any]:
        """用 SiteOccupancy 权威来源覆盖机械臂动作来源参数。

        参数：``resolved_args`` 是 DAG 已解析参数；合同声明可选来源字段映射；
        其余参数来自本轮库存事实。返回：与输入隔离的规范动作参数。异常：作者
        提供的来源约束与实际占用不一致时抛稳定非临时错误，禁止向错误地点 pick。
        """

        canonical = dict(resolved_args)
        owner_param = str(transfer_contract.get("source_owner_param") or "")
        site_uuid_param = str(transfer_contract.get("source_site_uuid_param") or "")
        site_name_param = str(transfer_contract.get("source_site_name_param") or "")
        if not owner_param:
            return canonical
        expected_owner = canonical.get(owner_param)
        if expected_owner not in (None, ""):
            expected_owner_uuid = _resource_argument_uuid(
                expected_owner,
                argument_name=owner_param,
            )
            if expected_owner_uuid != source_owner_material_uuid:
                raise TransferResourceSetError(
                    "source_constraint_mismatch",
                    "工作流声明的来源父资源与物料实际库位不一致",
                )
        expected_site_uuid = str(
            (canonical.get(site_uuid_param) if site_uuid_param else "") or ""
        ).strip()
        expected_site_name = str(
            (canonical.get(site_name_param) if site_name_param else "") or ""
        ).strip()
        if expected_site_uuid and expected_site_uuid != source_site_uuid:
            raise TransferResourceSetError(
                "source_constraint_mismatch",
                "工作流声明的来源库位 UUID 与物料实际库位不一致",
            )
        # 库位名可以是上层物料的地址（台面位号、料架层号），由地址表指到实际所属载架上的这个库位。
        # 这种写法照原样交给驱动：驱动按作者写的地址（厂家位号）动作，物理库位只用于库存与锁。
        named_by_address = bool(
            expected_site_name
            and expected_site_name.casefold() != source_site_name.casefold()
            and address_site_uuid is not None
            and address_site_uuid(expected_site_name) == source_site_uuid
        )
        if (
            expected_site_name
            and expected_site_name.casefold() != source_site_name.casefold()
            and not named_by_address
        ):
            raise TransferResourceSetError(
                "source_constraint_mismatch",
                "工作流声明的来源库位名称与物料实际库位不一致",
            )
        canonical[owner_param] = {"uuid": source_owner_material_uuid}
        if site_uuid_param:
            canonical[site_uuid_param] = source_site_uuid
        if site_name_param and not named_by_address:
            canonical[site_name_param] = source_site_name
        return canonical

    @staticmethod
    def _operate_in_place_contract(node: Any) -> tuple[str, ...] | None:
        """读取节点冻结的原位操作物料参数合同。"""

        resource_contract = getattr(node, "action_resource_contract", None)
        operate = (
            resource_contract.get("operate_in_place")
            if isinstance(resource_contract, Mapping)
            else None
        )
        if operate is None:
            return None
        from unilabos.registry.action_resource_contract import operate_in_place_parameters

        try:
            return operate_in_place_parameters(operate)
        except ValueError as error:
            raise ExecutionPolicyError("冻结动作的 operate_in_place 合同损坏") from error

    @staticmethod
    def _aliquot_resource_contract(node: Any) -> dict[str, Any] | None:
        """读取 AST 冻结的分装来源和完整目标参数闭集。"""

        resource_contract = getattr(node, "action_resource_contract", None)
        raw = (
            resource_contract.get("aliquot")
            if isinstance(resource_contract, Mapping)
            else None
        )
        if raw is None:
            return None
        if not isinstance(raw, Mapping) or set(raw) != {
            "source_material_param",
            "target_material_params",
        }:
            raise ExecutionPolicyError("冻结动作的 aliquot 合同损坏")
        source = str(raw.get("source_material_param") or "").strip()
        targets = raw.get("target_material_params")
        if (
            not source
            or not isinstance(targets, list)
            or not targets
            or any(not isinstance(value, str) or not value.strip() for value in targets)
        ):
            raise ExecutionPolicyError("冻结动作的 aliquot 参数集合损坏")
        return {
            "source_material_param": source,
            "target_material_params": list(targets),
        }

    def _resource_lock_keys(
        self,
        node: Any,
        resolved_args: dict[str, Any],
        *,
        resolved_site: ResolvedSiteTarget | None = None,
    ) -> set[str]:
        """生成节点本次执行需要持有的物料锁和库位锁键。

        参数：``node`` 是当前准备派发的工作流节点（WorkflowNode），
        ``resolved_args`` 是合并上游输出后的最终动作参数。返回：使用
        ``material/{uuid}/exclusive`` 物料锁；``resolved_site`` 非空时把
        ``mount_resource`` 的整物料锁替换为具体库位锁。异常：冻结动作合同
        （Action Contract）或最终参数不能安全解析时抛
        ``MaterialLockSchemaError``。
        """

        keys: set[str] = set()
        transfer_owners: list[str] = []
        frozen_schema = getattr(node, "param_schema", None)
        if frozen_schema is not None:
            validation_args = resolved_args
            if resolved_site is not None:
                transfer_contract = self._transfer_resource_contract(node)
                site_name_param = (
                    transfer_contract["target_site_name_param"]
                    if transfer_contract is not None
                    else ""
                )
                if site_name_param:
                    # SiteSelector 的动作 Schema 使用 UUID 供编辑器保存稳定身份，
                    # 但库存权威解析后，设备驱动参数会规范为现场库位名称。物料锁
                    # 仍需严格校验完整动作参数，因此仅在校验副本中恢复已验证的
                    # Site UUID；不得改变最终派发给设备的名称参数。
                    validation_args = dict(resolved_args)
                    validation_args[site_name_param] = resolved_site.uuid
            material_uuids = compile_material_lock_schema(
                frozen_schema
            ).material_lock_uuids(validation_args)
            keys.update(material_lock_key(item) for item in material_uuids)
            # 搬运两端的所属物件（source/target owner）只用来定位端点库位，不整件独占：
            # 端点由库位锁保护，载架上其他库位里正被占用的物料不应挡住这次搬运。
            # 和冻结计划用同一个解析（transfer_endpoint_site_keys）：解析不出库位的一端仍整件锁。
            transfer_contract = self._transfer_resource_contract(node)
            if transfer_contract is not None:
                from unilabos.workflow.inventory_resource_plan import transfer_endpoint_site_keys

                _site_keys, unresolved = transfer_endpoint_site_keys(
                    self._station_resources, transfer_contract, resolved_args
                )
                for field in ("source_owner_param", "target_owner_param"):
                    owner = resolved_args.get(transfer_contract.get(field) or "")
                    owner_uuid = (owner.get("uuid") or owner.get("material_uuid")) if isinstance(owner, Mapping) else owner
                    if isinstance(owner_uuid, str) and owner_uuid and owner_uuid not in unresolved:
                        transfer_owners.append(owner_uuid)
        if resolved_site is not None:
            keys.discard(material_lock_key(resolved_site.owner_material_uuid))
            keys.add(
                site_lock_key(
                    resolved_site.owner_material_uuid,
                    resolved_site.uuid,
                )
            )
        # 端点已经落到具体库位的所属物件不再整件独占；没解析出库位的一端仍按整件锁保护（旧语义）。
        for owner_uuid in transfer_owners:
            keys.discard(material_lock_key(owner_uuid))
        # 工作流级实体物料需求是独立声明；若它要求整父物料，则必须覆盖上方
        # 动作参数派生出的细粒度库位键，不能被替换逻辑误删。
        for req in getattr(node, "material_requirements", []) or []:
            if getattr(req, "instance_uuid", ""):
                keys.add(material_lock_key(req.instance_uuid))
        # 实体型物料需求可能与动作合同中的子库位成员指向同一拥有者；整物料
        # 占用覆盖子库位，归一化后避免同一作业保存冗余键。
        return normalize_resource_lock_keys(keys)

    def _busy_keys(self) -> set[str]:
        """合并外部与本地在途作业的动作级、设备级内存忙碌键。

        参数：无；外部键来自构造注入集合和可选实时提供者。
        返回：供一次准入重排使用的忙碌键副本；人工确认等待同样计入互斥。
        异常：外部提供者异常会被记录，并沿用既有降级，仅使用已知本地事实。

        该集合不会跨进程重启恢复，也没有占用 UUID 或栅栏令牌，因此不是持久
        作业执行占用（JobExecutionClaim）。
        """

        busy = set(self._external_busy_keys)
        if self._busy_key_provider is not None:
            try:
                busy |= set(self._busy_key_provider())
            except Exception:
                logger.exception("[EdgeScheduler] busy_key_provider failed")
        # 外部执行层仍使用动作级忙碌键；保留原键用于既有协议，同时把严格
        # 形状稳定提升为设备键，使取消后的物理在途作业继续阻塞同设备其他动作。
        for external_action_key in tuple(busy):
            device_key = _device_key_from_strict_action_key(external_action_key)
            if device_key is not None:
                busy.add(device_key)
        for job in self._inflight.values():
            device_identity = job.device_material_uuid or job.device_id
            released = False
            if device_identity:
                key = device_lock_key(device_identity)
                released = key in job.operator_released_keys
                if not released:
                    busy.add(key)
            if not released:
                busy.add(job.device_action_key)
        for keys in self._interval_resource_holders.values():
            busy |= keys
        return busy

    # ── 泳道图时间线 ─────────────────────────────────────────

    def _record_timeline(
        self,
        job: DispatchedJob,
        success: bool,
        suc_type: str = "normal",
        state: str = "",
        ret_value: Any = None,
    ) -> None:
        """job 完结（成功/失败/取消）时记录时间线并喂历史统计（须在锁内调用）。"""
        ended_at = self._clock()
        actual_s = max(0.0, ended_at - job.dispatched_at)
        if not state:
            state = "success" if success else "failed"
        # 只有正常成功的样本才进入历史统计（skip/失败/取消的时长不代表真实执行）
        if success and suc_type == "normal":
            self._estimator.observe(job.device_action_key, actual_s)
        entry = {
            "job_id": job.job_id,
            "workflow_id": job.workflow_id,
            "node_id": job.node_id,
            "device_id": job.device_id,
            "action_name": job.action_name,
            "device_action_key": job.device_action_key,
            "started_at": job.dispatched_at,
            "ended_at": ended_at,
            "actual_s": round(actual_s, 3),
            "estimated_s": round(job.estimated_s, 3),
            "estimate_source": job.estimate_source,
            "state": state,
            "suc_type": suc_type,
        }
        self._timeline.append(entry)
        # 历史库落盘（独立 SQLite；含截断后的返回值，供审计/回放）
        self._safe_history("record_job", entry, ret_value)
        self._emit_monitor(
            "action",
            "job_finished",
            {
                "job_id": job.job_id,
                "workflow_id": job.workflow_id,
                "node_id": job.node_id,
                "device_id": job.device_id,
                "action_name": job.action_name,
                "device_action_key": job.device_action_key,
                "state": state,
                "suc_type": suc_type,
                "actual_s": round(actual_s, 3),
                "estimated_s": round(job.estimated_s, 3),
            },
        )
        self._emit_monitor(
            "device",
            "device_idle",
            {
                "device_id": job.device_id,
                "action_name": job.action_name,
                "device_action_key": job.device_action_key,
                "job_id": job.job_id,
            },
        )

    def timeline(self, window_s: float = 3600.0) -> dict[str, Any]:
        """泳道图数据：执行中 job + 窗口内已完结 job + 预估器状态。

        泳道由前端按 device_id（或 device_action_key）分组；running 条目
        用 started_at + estimated_s 画预估终点，completed 条目画实际区间。
        """
        now = self._clock()
        cutoff = now - max(window_s, 0.0)
        with self._lock:
            running = [
                {
                    "job_id": j.job_id,
                    "workflow_id": j.workflow_id,
                    "node_id": j.node_id,
                    "device_id": j.device_id,
                    "action_name": j.action_name,
                    "device_action_key": j.device_action_key,
                    "started_at": j.dispatched_at,
                    "elapsed_s": round(max(0.0, now - j.dispatched_at), 3),
                    "estimated_s": round(j.estimated_s, 3),
                    "estimate_source": j.estimate_source,
                }
                for j in self._inflight.values()
            ]
            completed = [e for e in self._timeline if e["ended_at"] >= cutoff]
            return {
                "now": now,
                "window_s": window_s,
                "running": running,
                "completed": completed,
                "time_schedule": self.time_schedule_snapshot(),
                "estimator": {
                    "mode": self._estimator.mode,
                    "default_s": self._estimator.default_s,
                    "stats": self._estimator.stats(),
                },
            }

    def device_status(self) -> list[dict[str, Any]]:
        """设备占用视图（监控面板）：busy 来自 inflight，idle 来自时间线痕迹。"""
        now = self._clock()
        with self._lock:
            devices: dict[str, dict[str, Any]] = {}
            # 时间线里出现过的设备默认 idle（带最近一次动作）
            for entry in self._timeline:
                dev = entry["device_id"] or entry["device_action_key"]
                cur = devices.get(dev)
                if cur is None or entry["ended_at"] > cur.get("last_seen", 0):
                    devices[dev] = {
                        "device_id": dev,
                        "status": "idle",
                        "last_action": entry["action_name"],
                        "last_state": entry["state"],
                        "last_seen": entry["ended_at"],
                    }
            # 在执行 job 的设备置 busy
            for j in self._inflight.values():
                dev = j.device_id or j.device_action_key
                devices[dev] = {
                    "device_id": dev,
                    "status": "busy",
                    "action_name": j.action_name,
                    "job_id": j.job_id,
                    "workflow_id": j.workflow_id,
                    "started_at": j.dispatched_at,
                    "elapsed_s": round(max(0.0, now - j.dispatched_at), 3),
                    "estimated_s": round(j.estimated_s, 3),
                    "estimate_source": j.estimate_source,
                    "last_seen": now,
                }
            return sorted(devices.values(), key=lambda d: d["device_id"])

    # ── 查询 ─────────────────────────────────────────────────

    def begin_drain(self) -> dict[str, Any]:
        """停止新设备作业派发，并返回当前排空状态。

        参数：无。返回：包含阶段、是否接受派发和在途设备作业身份的稳定快照。
        异常：不主动抛出异常；重复调用幂等。已有设备作业不会被取消或伪造完成，
        只有它们通过原结果通道收敛后，阶段才从 ``draining`` 变为 ``drained``。
        """

        with self._lock:
            self._draining = True
            snapshot = self._capture_drain_status_locked()
        return self._resolve_drain_status(snapshot)

    def set_drain_blocker_provider(
        self,
        provider: Callable[[], set[str]] | None,
    ) -> None:
        """装配或移除持久执行安全事实提供者。

        参数：``provider`` 返回仍可能在设备侧执行或结果未知的 Job 身份集合；
        ``None`` 表示移除。返回：无。异常：提供者异常在查询排空状态时原样传播，
        使 HTTP/Host 关闭式失败；本方法不读取、缓存或修改持久 Task/Job。
        """

        with self._lock:
            self._drain_blocker_provider = provider

    def drain_status(self) -> dict[str, Any]:
        """查询调度器是否已经安全排空。

        参数：无。返回：排空阶段以及仍在设备侧执行的作业列表。异常：持久
        阻塞项读取失败原样传播，关闭式拒绝把未知执行报告成已排空。调度内存
        快照在锁内复制，较慢的持久 Task/Job 扫描在锁外完成，不阻塞重排。
        """

        with self._lock:
            snapshot = self._capture_drain_status_locked()
        return self._resolve_drain_status(snapshot)

    def resume_from_drain(self) -> dict[str, Any]:
        """退出排空状态并立即继续派发此前被门禁拦住的作业。

        参数：无。返回：恢复后的运行阶段和本轮实际派发摘要。异常：调度、库存
        或执行适配器错误原样传播；失败时排空标记已经清除，调用方可再次排空。
        """

        with self._lock:
            self._draining = False
            needs_admission = self._serial_queue and self._select_serial_workflow() in self._serial_admissions
            dispatched = [] if needs_admission else self._reschedule_locked()
        if needs_admission:
            dispatched = self._wake_reconcile()
        with self._lock:
            snapshot = self._capture_drain_status_locked()
        return {
            **self._resolve_drain_status(snapshot),
            "dispatched": dispatched,
        }

    def _capture_drain_status_locked(
        self,
    ) -> tuple[bool, set[str], Callable[[], set[str]] | None]:
        """在调度锁内复制排空内存事实和持久事实读取端口。"""

        return (
            self._draining,
            {
                job_id
                for job_id, job in self._inflight.items()
                if self._is_device_job_active_locked(job)
            },
            self._drain_blocker_provider,
        )

    @staticmethod
    def _resolve_drain_status(
        snapshot: tuple[bool, set[str], Callable[[], set[str]] | None],
    ) -> dict[str, Any]:
        """在调度锁外合并持久阻塞项并构造关闭式排空投影。"""

        draining, memory_job_ids, blocker_provider = snapshot
        persisted_job_ids = (
            set(blocker_provider()) if blocker_provider is not None else set()
        )
        active_device_job_ids = sorted(memory_job_ids | persisted_job_ids)
        if not draining:
            phase = "running"
        elif active_device_job_ids:
            phase = "draining"
        else:
            phase = "drained"
        return {
            "phase": phase,
            "accepting_new_dispatches": not draining,
            "active_device_job_count": len(active_device_job_ids),
            "active_device_job_ids": active_device_job_ids,
        }

    def _is_device_job_active_locked(self, job: DispatchedJob) -> bool:
        """判断在途作业是否会阻止安全排空。

        已完整准入的人工确认持有设备资源，也属于 drain blocker。
        """

        return True

    def workflow_snapshot(self, workflow_id: str) -> dict[str, Any] | None:
        with self._lock:
            run = self._workflows.get(workflow_id)
            if run is None:
                return None
            snap = run.snapshot()
            # 叠加在执行 job_id：前端对 manual_confirm 节点凭它调 /jobs/{id}/finish
            nodes = snap.get("nodes", {})
            for job_id, job in self._inflight.items():
                if job.workflow_id == workflow_id and job.node_id in nodes:
                    nodes[job.node_id]["job_id"] = job_id
            return snap

    def snapshot(self) -> dict[str, Any]:
        """返回调度器当前工作流、在途作业和排空状态的只读快照。

        参数：无。返回：包含工作流运行投影、在途作业的稳定身份/资源计划元数据、
        重排次数和排空状态的字典；资源计划字段仅描述冻结计划，不替代持久作业
        执行占用（JobExecutionClaim）。异常：资源阻塞读取端口损坏时由排空投影
        原样抛出，避免以不完整快照掩盖安全事实。
        """

        with self._lock:
            snapshot = {
                "workflows": {
                    wid: run.snapshot() for wid, run in self._workflows.items()
                },
                "inflight_jobs": {
                    job_id: {
                        "workflow_id": j.workflow_id,
                        "node_id": j.node_id,
                        "device_action_key": j.device_action_key,
                        "resource_locks": sorted(
                            self._job_resource_locks.get(job_id, set())
                        ),
                        "active_resource_locks": sorted(
                            j.active_resource_lock_keys
                        ),
                        "started_at": j.dispatched_at,
                        "estimated_s": round(j.estimated_s, 3),
                        "estimate_source": j.estimate_source,
                        "resource_plan_id": j.resource_plan_id,
                        "resource_interval_ids": list(j.resource_interval_ids),
                        "resource_acquire_set_id": j.resource_acquire_set_id,
                    }
                    for job_id, j in self._inflight.items()
                },
                "reschedule_count": self._reschedule_count,
            }
            drain_snapshot = self._capture_drain_status_locked()
        return {
            **snapshot,
            "drain": self._resolve_drain_status(drain_snapshot),
        }

    def release_error_occupancies(self, occupancies: Sequence[Mapping[str, Any]], *, include_shared: bool = True) -> None:
        """人工释放指定逻辑占用，保留实际在途执行以接收迟到回执。"""
        with self._lock:
            for item in occupancies:
                identity, key = str(item["workflow_node_job_uuid"]), str(item["lock_key"])
                for job in self._inflight.values():
                    same_fence = any(
                        fence.get("lock_key") == key and fence.get("fencing_token") == item.get("fencing_token")
                        for fence in job.dispatch_credentials.get("fences", [])
                    )
                    if job.workflow_id != item["workflow_task_uuid"] or not (job.job_id == identity or (include_shared and same_fence)):
                        continue
                    job.operator_released_keys.add(key)
                    job.resource_lock_keys.discard(key)
                    job.active_resource_lock_keys.discard(key)
                    self._job_resource_locks.get(job.job_id, set()).discard(key)
                for (task_id, interval_id), keys in self._interval_resource_holders.items():
                    if task_id == item["workflow_task_uuid"]:
                        holders = self._interval_resource_holder_jobs.get((task_id, interval_id), set())
                        if not include_shared and holders - {identity}:
                            continue
                        keys.discard(key)

    def settle_canceled_before_start(self, job_uuid: str) -> None:
        """持久开始屏障已撤销命令后，清理对应内存等待，不伪造设备停止。"""
        with self._lock:
            job = self._inflight.pop(job_uuid, None)
            if job is None:
                return
            run = self._workflows.get(job.workflow_id)
            if run is not None:
                run.mark_canceled(job.node_id)
            self._job_resource_locks.pop(job_uuid, None)
            span = self._job_spans.pop(job_uuid, None)
            if span is not None:
                span.end()

    def cancel_workflow(self, workflow_id: str, *, execution_job_uuid: str | None = None, cancel_current: bool = True) -> bool:
        """停止后续派发并请求执行器取消全部在途设备作业。

        参数：``workflow_id`` 是本地工作流任务（WorkflowTask）身份。返回：找到
        运行并提交取消时为真，不存在时为假。异常：持久监听器或执行适配器异常
        不会释放在途作业；调用方可重放命令。

        未发送作业可立即结算；已发送作业始终保留在 ``_inflight`` 与资源锁中，
        直到设备明确返回终态。执行器拒绝、缺少取消能力或调用异常只会让作业
        保持 ``running`` 并等待物理对账，绝不伪造设备已经停止。
        """

        with self._lock:
            run = self._workflows.get(workflow_id)
            if run is None and execution_job_uuid not in self._inflight:
                return False
            if cancel_current and run is not None:
                run.cancel()
            canceling_jobs = [
                job_id
                for job_id, j in self._inflight.items()
                if j.workflow_id == workflow_id
                and (execution_job_uuid is None or job_id == execution_job_uuid)
            ]
            cancel_method = getattr(self._dispatcher, "cancel", None)
            for job_id in canceling_jobs:
                current_job = self._inflight.get(job_id)
                current_run = (
                    self._workflows.get(current_job.workflow_id)
                    if current_job is not None
                    else None
                )
                current_node = (
                    current_run.node(current_job.node_id)
                    if current_run is not None and current_job is not None
                    else None
                )
                if (
                    current_job is not None
                    and current_node is not None
                    and current_node.waits_for_manual_confirmation()
                    and not current_job.manual_action_dispatched
                ):
                    self._notify_job_cancel_no_send(job_id)
                    self._inflight.pop(job_id, None)
                    self._job_resource_locks.pop(job_id, None)
                    action_trace = self._job_spans.pop(job_id, None)
                    if action_trace is not None:
                        action_trace.end()
                    if cancel_current:
                        current_run.mark_canceled(current_job.node_id)
                    self._record_timeline(
                        current_job,
                        success=False,
                        suc_type="canceled",
                        state="canceled",
                    )
                    self._notify_job_settled(job_id, False, None, "canceled")
                    if cancel_current:
                        self._clear_interval_holders(workflow_id)
                    continue
                try:
                    state = (
                        cancel_method(
                            job_id,
                            lambda accepted, current_job_id=job_id: (
                                self._notify_job_cancel_accepted(current_job_id)
                                if accepted
                                else self._notify_job_cancel_uncertain(
                                    current_job_id,
                                    "local_cancel_rejected",
                                )
                            ),
                        )
                        if callable(cancel_method)
                        else CancelDispatchState.UNAVAILABLE
                    )
                except BaseException:
                    self._notify_job_cancel_uncertain(
                        job_id,
                        "local_cancel_request_exception",
                    )
                    continue
                try:
                    normalized_state = CancelDispatchState(state)
                except ValueError:
                    normalized_state = CancelDispatchState.UNAVAILABLE
                if normalized_state == CancelDispatchState.REQUESTED:
                    continue
                if normalized_state == CancelDispatchState.NOT_SENT:
                    # No-send Proof 允许同步结算并释放内存锁；持久监听器必须先成功。
                    self._notify_job_cancel_no_send(job_id)
                    job = self._inflight.pop(job_id, None)
                    self._job_resource_locks.pop(job_id, None)
                    action_trace = self._job_spans.pop(job_id, None)
                    if action_trace is not None:
                        action_trace.event(
                            "action.cancel_no_send",
                            {"workflow.job.uuid": job_id},
                        )
                        action_trace.end()
                    if job is not None:
                        if cancel_current and run is not None:
                            run.mark_canceled(job.node_id)
                        self._record_timeline(
                            job,
                            success=False,
                            suc_type="canceled",
                            state="canceled",
                        )
                    self._notify_job_settled(job_id, False, None, "canceled")
                    self._clear_interval_holders(workflow_id)
                    continue
                self._notify_job_cancel_uncertain(
                    job_id,
                    "local_cancel_acceptance_unavailable",
                )
            notifications = self._collect_terminal_notifications()
        self._fire_notifications(notifications)
        if self._serial_queue:
            self._wake_reconcile(wait=False)
        return True

    def release_terminal_workflow_resources(self, workflow_id: str) -> dict[str, int]:
        """撤销已由操作员确认安全的异常终态运行内存占用。

        参数：``workflow_id`` 必须先由工作流权威证明 Task 和所有
        Job 已进入终态，且操作员已确认物理现场安全。返回撤销的
        本地运行和在途 Job 数量。异常：空身份抛 ``ValueError``；本方法
        不发送取消或伪造设备结果，只在调用方已提供人工停止证明后
        删除内存调度占用。
        """

        normalized_workflow_id = str(workflow_id or "").strip()
        if not normalized_workflow_id:
            raise ValueError("workflow_id 不能为空")
        with self._lock:
            run = self._workflows.pop(normalized_workflow_id, None)
            inflight_job_ids = [
                job_id
                for job_id, job in self._inflight.items()
                if job.workflow_id == normalized_workflow_id
            ]
            for job_id in inflight_job_ids:
                self._inflight.pop(job_id, None)
                self._job_resource_locks.pop(job_id, None)
                action_trace = self._job_spans.pop(job_id, None)
                if action_trace is not None:
                    action_trace.event(
                        "action.operator_resource_unlock",
                        {"workflow.job.uuid": job_id},
                    )
                    action_trace.end()
            self._clear_interval_holders(normalized_workflow_id)
            self._step_targets.pop(normalized_workflow_id, None)
            had_material_reservation = (
                normalized_workflow_id in self._material_workflows
            )
            self._material_workflows.discard(normalized_workflow_id)
            self._notified_workflows.discard(normalized_workflow_id)
            workflow_trace = self._workflow_spans.pop(
                normalized_workflow_id,
                None,
            )
        if workflow_trace is not None:
            workflow_trace.event(
                "workflow.operator_resource_unlock",
                {"workflow.uuid": normalized_workflow_id},
            )
            workflow_trace.end()
        if had_material_reservation:
            self._safe_inventory_call(
                "release_workflow",
                normalized_workflow_id,
                reason="operator_resource_unlock",
            )
        if self._serial_queue:
            self._wake_reconcile(wait=False)
        return {
            "inflight_jobs": len(inflight_job_ids),
            "workflow_runs": int(run is not None),
        }

    def discard_workflow(self, workflow_id: str) -> bool:
        """丢弃可证明从未越过设备派发边界的失败提交占位。"""

        with self._lock:
            if any(job.workflow_id == workflow_id for job in self._inflight.values()):
                raise ValueError("已存在在途作业的工作流不能丢弃")
            run = self._workflows.pop(workflow_id, None)
            if run is None:
                return False
            self._clear_interval_holders(workflow_id)
            self._step_targets.pop(workflow_id, None)
            had_material_reservation = workflow_id in self._material_workflows
            self._material_workflows.discard(workflow_id)
            self._notified_workflows.discard(workflow_id)
            workflow_trace = self._workflow_spans.pop(workflow_id, None)
        if workflow_trace is not None:
            workflow_trace.end()
        if had_material_reservation:
            self._safe_inventory_call(
                "release_workflow",
                workflow_id,
                reason="workflow_discarded",
            )
        return True


def _claimed_site_uuids(lock_keys: set[str]) -> tuple[str, ...]:
    """从当前调度轮的规范库位执行占用键中提取具体库位身份。

    参数：``lock_keys`` 是所有在途作业的设备、物料和库位忙碌键。返回：稳定
    排序且去重的库位 UUID；无法识别的键被忽略。异常：无。该快照只用于门禁 6
    选择等价库位备选，最终互斥仍由门禁 7 的持久作业执行占用保证。
    """

    site_uuids: set[str] = set()
    for lock_key in lock_keys:
        parts = lock_key.split("/")
        if (
            len(parts) == 5
            and parts[0] == "material"
            and parts[2] == "site"
            and parts[4] == "exclusive"
        ):
            site_uuids.add(parts[3])
    return tuple(sorted(site_uuids))


__all__ = ["EdgeScheduler"]
