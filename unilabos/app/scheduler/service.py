"""本地调度器（EdgeScheduler）：Edge 侧执行态推进的唯一入口。

重排触发点（硬性约定，二者都强制全量 reschedule）：

1. **每个工作流提交**（``submit_workflow``）
2. **每个子 action 完成**（``on_job_finished``，含成功/失败）

每次 reschedule：

    收集所有 RUNNING 工作流的 ready 节点
      → TaskOrderer 排序（设备锁准入 → 资源解阻类别 → 用户 Priority）
      → 按序下发；动作键或设备级互斥键被占用的节点跳过，等下一次触发
      → 下发前解析父节点传参（gjson/sjson + ``@@@`` 语义）

不做一次性拓扑序：ready 集合每次触发点都重新计算、重新排序。

物料衔接（注入本地库存服务（InventoryService）时启用；spec 无物料字段则行为完全不变）：

- submit：优先汇总 DAG 全部物料需求并 all-or-nothing 预留；若不足则进入
  Action 级延迟门控，只在具体节点 claim 前预留该节点物料，避免提前消耗后续
  Action 才会使用的最后一支 TIP
- 节点下发前：预留 → FIFO lot 消费 + 实例 deploy（幂等键 workflow:node:attempt）
- 节点失败：该节点已消费的物料转 quarantined（人工复核，不虚假加回）
- 节点异常后人工选择 skip（suc_type=skip）：节点算成功继续推进，但其已消费
  物料状态不明，同样转 quarantined 待复核
- 工作流终态（failed/canceled）：剩余 active 预留自动 release（依据 DB，不依赖内存）
- 方案三补料门控：预留不足时原 Run 保持 waiting，并可创建一个独立 urgent
  补料 Run；补料 Run 完成后再次校验库存，原 Run 才恢复调度
- Action 成功：按 ``material_outputs`` 声明原子登记输出实例、内容与堆栈关系；
  Run 失败时已登记的中间产物自动 discard，成功产物留在库存中

动作物料锁（Action Material Lock，注入 ``material_lock_resolver`` 时启用）：

- 下发前校验最终参数，并从规范动作 Schema 的锁标记提取物料 UUID（Material UUID）；
  与在执行作业（Job）的锁键冲突 → 本轮跳过（等释放后的重排）
- 实体型物料需求的 ``instance_uuid`` 自动并入同一物料锁键
- job 完成 / 工作流取消时释放
"""

from __future__ import annotations

import logging
import threading
import time
import uuid as uuid_mod
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional, Set

from unilabos.app.scheduler.dag_state import WorkflowRun
from unilabos.app.scheduler.dispatch import (
    Dispatcher,
    RecordingDispatcher,
    build_job_start_payload,
)
from unilabos.app.scheduler.estimation import DurationEstimator
from unilabos.app.scheduler.inventory.domain import (
    InsufficientStock,
    InventoryError,
    MaterialRequirement,
)
from unilabos.app.scheduler.models import (
    DispatchedJob,
    ReadyTask,
    SchedulableAction,
    WorkflowEdge,
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
from unilabos.registry.material_lock_schema import (
    MaterialLockSchemaError,
    compile_material_lock_schema,
)
from unilabos.utils.tracing import (
    DetachedSpan,
    add_event,
    span,
    start_detached_span,
)

logger = logging.getLogger(__name__)


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
    return f"/devices/{path_parts[2]}"


class EdgeScheduler:
    def __init__(
        self,
        orderer: Optional[TaskOrderer] = None,
        dispatcher: Optional[Dispatcher] = None,
        external_busy_keys: Optional[Set[str]] = None,
        busy_key_provider: Optional["Callable[[], Set[str]]"] = None,
        workflow_state_listener: Optional["Callable[[str, str], None]"] = None,
        inventory: Any = None,
        material_lock_resolver: Optional[
            "Callable[[str, str, Dict[str, Any]], tuple[str, ...]]"
        ] = None,
        estimator: Optional[DurationEstimator] = None,
        timeline_capacity: int = 400,
        monitor: Any = None,
        history: Any = None,
        material_replenishment_factory: Optional[
            "Callable[[WorkflowRun, Dict[str, List[MaterialRequirement]]], Optional[WorkflowSpec]]"
        ] = None,
        material_replenishment_priority: Any = "urgent",
        material_replenishment_s09_device_id: str = "szlab_mixer_pipetting_station",
        material_replenishment_s09_home_position: int = 1,
    ):
        """装配本地执行态调度器（Scheduler）。

        Args:
            orderer: 对已就绪任务进行稳定排序的策略。
            dispatcher: 把作业（Job）提交给执行器的适配器。
            external_busy_keys: 启动时已知的外部设备占用键。
            busy_key_provider: 实时读取设备占用键的函数。
            workflow_state_listener: 工作流（Workflow）终态通知函数。
            inventory: 本地库存（Inventory）预留、消费和释放服务。
            material_lock_resolver: 遗留直接调用根据实时注册表
                （Registry）Schema 与最终参数解析物料 UUID 的兼容函数。
            estimator: 动作预计时长计算器。
            timeline_capacity: 内存时间线最多保留的作业数量。
            monitor: 实时监控事件输出适配器。
            history: 遗留工作流执行历史存储。
            material_replenishment_factory: 物料准入不足时创建独立补料工作流的工厂。
                工厂不负责提交；返回 ``None`` 表示本次只等待外部补料。
            material_replenishment_priority: 补料工作流强制使用的优先级，默认
                ``urgent``。设备锁仍然先于此优先级生效，补料不会抢占执行中的动作。
            material_replenishment_s09_device_id: 换 TIP 前执行 S09 还原动作的设备
                ID，默认使用 SZLab S09 移液站。
            material_replenishment_s09_home_position: S09 还原到的安全位，默认 1。
        """

        self._orderer = orderer or StableLocalOrderer()
        self._dispatcher = dispatcher or RecordingDispatcher()
        self._lock = threading.RLock()

        self._workflows: Dict[str, WorkflowRun] = {}
        # 动态可调度 Action 队列。键使用 (run_id, node_id)，不能只用 node_id；
        # 队列项在提交/完成事件时增量更新，资源暂不可用时保留等待下一轮重排。
        self._schedulable_queue: Dict[tuple[str, str], SchedulableAction] = {}
        self._dirty_run_ids: Set[str] = set()
        # workflow_id -> 本次单步命令唯一允许派发的节点。只在一次重排期间存在。
        self._step_targets: Dict[str, str] = {}
        # job_id -> DispatchedJob（完成回调路由 + 资源锁）
        self._inflight: Dict[str, DispatchedJob] = {}
        # 外部注入的锁（例如 DeviceActionManager 已占用的设备），可选
        self._external_busy_keys = (
            external_busy_keys if external_busy_keys is not None else set()
        )
        # 实时锁视图提供者（微后端 busy_device_action_keys），可选
        self._busy_key_provider = busy_key_provider
        # 工作流终态通知（success/failed/canceled 各通知一次；锁外触发）
        self._workflow_state_listener = workflow_state_listener
        self._notified_workflows: Set[str] = set()
        self._reschedule_count = 0
        # 可选 InventoryService（duck-typed：reserve_workflow / consume_reservation /
        # quarantine_reservation / release_workflow）；None = 物料衔接整体关闭
        self._inventory = inventory
        # 有物料需求的 Run（其余 Run 不产生任何 inventory 调用）
        self._material_workflows: Set[str] = set()
        # 整个 DAG 预留失败时，切换到 Action 级延迟门控：只在具体节点 claim
        # 前预留该节点的物料，避免为了未来节点提前消耗最后一支 TIP。
        self._deferred_material_workflows: Set[str] = set()
        self._material_waiting_nodes: Dict[str, str] = {}
        self._material_replenishment_disabled_runs: Set[str] = set()
        # 已由本 Run 产出的实体物料；Run 失败时统一废弃，避免失败 workflow
        # 留下看似可用的产物。成功终态则保留在库存中。
        self._material_outputs_by_run: Dict[str, Set[str]] = {}
        # 动作物料锁解析器消费规范动作 Schema；None 仅用于无注册表的隔离测试。
        self._material_lock_resolver = material_lock_resolver
        # job_id -> 该作业（Job）持有的物料锁键；完成或取消时释放。
        self._job_resource_locks: Dict[str, Set[str]] = {}
        # 时长预估器（declared / historical / auto 三种 mode，内含两种计算模式）
        self._estimator = estimator or DurationEstimator()
        # 泳道图时间线：已完结 job 的起止记录（环形缓冲）
        self._timeline: Deque[Dict[str, Any]] = deque(maxlen=timeline_capacity)
        # 实时监控总线（duck-typed emit(channel, type, data)）；None = 关闭
        self._monitor = monitor
        # 工作流执行历史（WorkflowHistoryStore，独立 SQLite）；None = 不落盘
        self._history = history
        # Scheme 3：原工作流在 claim-time 物料门禁处受阻时，可由注入的工厂
        # 生成一个独立高优先级补料工作流。这里仅编排依赖，不臆造补料设备动作。
        self._material_replenishment_factory = material_replenishment_factory
        self._material_replenishment_priority = material_replenishment_priority
        self._material_replenishment_s09_device_id = material_replenishment_s09_device_id
        self._material_replenishment_s09_home_position = int(
            material_replenishment_s09_home_position
        )
        # original run_id -> replenishment workflow_id
        self._material_replenishment_by_run: Dict[str, str] = {}
        # replenishment workflow_id -> original run_id
        self._material_replenished_run_by_workflow: Dict[str, str] = {}
        # 长生命周期根 span：workflow → action/job。只保存上下文/句柄，不保存 payload。
        self._workflow_spans: Dict[str, DetachedSpan] = {}
        self._job_spans: Dict[str, DetachedSpan] = {}
        # 生命周期监听器仅承载标准 Task/Job 兼容回写，不成为第二个状态权威。
        self._job_pre_dispatch_listeners: List[Callable[[Dict[str, Any]], None]] = []
        self._job_finished_listeners: List[Callable[[str, bool, Any, str], None]] = []
        # 准入重试监听器把尚未注册为旧调度运行的来源受阻任务接到同一个公开
        # 重排触发点；监听器本身仍由工作流任务桥拥有。
        self._admission_retry_listeners: List[Callable[[], None]] = []

    @property
    def inventory_service(self) -> Any:
        """返回本地调度器持有的库存服务（InventoryService）。

        参数：无。返回：同一库存权威（Inventory Authority）实例；未装配时为
        ``None``。该只读属性只供组合与桥接层验证和复用，禁止替换权威。
        """

        return self._inventory

    def _emit_monitor(
        self, channel: str, event_type: str, data: Dict[str, Any]
    ) -> None:
        if self._monitor is None:
            return
        try:
            self._monitor.emit(channel, event_type, data)
        except Exception:  # noqa: BLE001 - 监控故障不影响调度
            pass

    def _safe_history(self, method: str, *args: Any, **kwargs: Any) -> None:
        """写执行历史；持久化故障不影响调度。"""
        if self._history is None:
            return
        try:
            getattr(self._history, method)(*args, **kwargs)
        except Exception:  # noqa: BLE001
            logger.exception("[EdgeScheduler] history.%s failed", method)

    def set_workflow_state_listener(
        self, listener: "Callable[[str, str], None]"
    ) -> None:
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

    def add_job_pre_dispatch_listener(
        self,
        listener: Callable[[Dict[str, Any]], None],
    ) -> None:
        """注册作业派发前监听器。

        参数：``listener`` 接收即将派发的作业摘要。返回无；监听器必须先提交持久
        派发意图，异常会中止物理派发，禁止形成先发设备后记数据库的窗口。
        """

        self._job_pre_dispatch_listeners.append(listener)

    def remove_job_pre_dispatch_listener(
        self,
        listener: Callable[[Dict[str, Any]], None],
    ) -> None:
        """移除派发前监听器；参数 ``listener`` 必须是此前注册的同一回调。"""

        self._job_pre_dispatch_listeners = [
            current
            for current in self._job_pre_dispatch_listeners
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

    def remove_job_finished_listener(
        self,
        listener: Callable[[str, bool, Any, str], None],
    ) -> None:
        """移除作业完成监听器；参数 ``listener`` 必须是此前注册的同一回调。"""

        self._job_finished_listeners = [
            current for current in self._job_finished_listeners if current != listener
        ]

    def _notify_job_pre_dispatch(self, dispatching: Dict[str, Any]) -> None:
        """同步通知派发意图；参数 ``dispatching`` 是即将越过执行边界的摘要。

        返回无；任何监听器失败都会阻止执行适配器调用，由创建请求收到错误并保留
        可核对的持久事实。
        """

        for listener in tuple(self._job_pre_dispatch_listeners):
            listener(dict(dispatching))

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

    # ── 触发点 1：任务进来 ────────────────────────────────────

    def submit_workflow(self, spec: WorkflowSpec) -> Dict[str, Any]:
        run_identity = spec.run_id or spec.workflow_id
        with self._lock:
            if (
                spec.workflow_id in self._workflows
                or spec.workflow_id in self._workflow_spans
                or any(run.run_id == run_identity for run in self._workflows.values())
            ):
                raise ValueError(
                    f"workflow/run {spec.workflow_id}/{run_identity} already submitted"
                )
            workflow_trace = start_detached_span(
                "workflow.task.run",
                attributes={
                    "workflow.uuid": spec.workflow_id,
                    "workflow.task.uuid": spec.task_id,
                    "workflow.run.uuid": spec.run_id or spec.workflow_id,
                    "lab.id": spec.lab_id,
                    "workflow.plan.node_count": len(spec.nodes),
                    "workflow.priority": str(spec.priority),
                },
            )
            # 先登记 span 也充当 submit 占位，避免并发同 ID 覆盖对方的追踪句柄。
            self._workflow_spans[spec.workflow_id] = workflow_trace
        try:
            with workflow_trace.activate():
                with span(
                    "workflow.task.submit",
                    attributes={
                        "workflow.uuid": spec.workflow_id,
                        "workflow.task.uuid": spec.task_id,
                    },
                ):
                    return self._submit_workflow(spec)
        except BaseException as exc:
            workflow_trace.fail(exc)
            workflow_trace.end()
            self._workflow_spans.pop(spec.workflow_id, None)
            raise

    def _submit_workflow(
        self,
        spec: WorkflowSpec,
        *,
        trigger_reschedule: bool = True,
        allow_material_replenishment: bool = True,
    ) -> Dict[str, Any]:
        """提交工作流并立即重排。返回本次下发结果。

        带物料需求时优先整 DAG all-or-nothing 预留；整 DAG 不足时切换为
        Action 级延迟门控，由具体节点 claim 前决定是否进入
        ``waiting_for_material``。
        """
        with self._lock:
            if spec.workflow_id in self._workflows:
                raise ValueError(f"workflow {spec.workflow_id} already submitted")
            run = WorkflowRun(spec)  # 构图 + 环检测，失败直接抛
            self._workflows[spec.workflow_id] = run
            self._dirty_run_ids.add(run.run_id)
            if not allow_material_replenishment:
                self._material_replenishment_disabled_runs.add(run.run_id)

            requirements = spec.material_requirements_by_node()
            if requirements:
                if self._inventory is None:
                    logger.warning(
                        "[EdgeScheduler] workflow %s declares materials but no inventory "
                        "service wired; proceeding without reservation",
                        spec.workflow_id,
                    )
                else:
                    self._material_workflows.add(run.run_id)
                    if not self._try_reserve(run):
                        # 不在提交阶段创建补料任务。先进入 Action 级门控，
                        # 由真正准备 claim 的节点决定是否需要补料。
                        self._deferred_material_workflows.add(run.run_id)

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
                    "run_id": run.run_id,
                    "nodes": len(spec.nodes),
                    "state": run.state.value,
                    "priority": str(spec.priority),
                },
            )
            self._safe_history("record_submitted", spec, run.state.value)
            dispatched = self._reschedule_locked() if trigger_reschedule else []
            notifications = self._collect_terminal_notifications()
        self._fire_notifications(notifications)
        return {
            "workflow_id": spec.workflow_id,
            "run_id": run.run_id,
            "state": run.state.value,
            "dispatched": dispatched,
        }

    def _ensure_material_replenishment_locked(
        self,
        run: WorkflowRun,
        requirements: Dict[str, List[MaterialRequirement]],
    ) -> Optional[str]:
        """为缺料 Run 建立一次性补料依赖（须持有调度锁）。

        方案三只规定调度编排，不规定“补料”对应的物理动作。因此动作图由
        ``material_replenishment_factory`` 注入；没有工厂时保持原有等料重试行为。
        返回补料 workflow_id，或 ``None``（无工厂/工厂拒绝创建）。
        """

        existing = self._material_replenishment_by_run.get(run.run_id)
        if existing is not None:
            return existing
        factory = self._material_replenishment_factory
        if factory is None:
            return None
        try:
            replenishment_spec = factory(run, requirements)
        except Exception as exc:  # noqa: BLE001 - 工厂故障不应伪造原任务可执行
            logger.exception(
                "[EdgeScheduler] material replenishment factory failed for %s: %s",
                run.spec.workflow_id,
                exc,
            )
            self._emit_monitor(
                "scheduler",
                "material_replenishment_factory_failed",
                {"workflow_id": run.spec.workflow_id, "error": str(exc)},
            )
            return None
        if replenishment_spec is None:
            return None
        if not isinstance(replenishment_spec, WorkflowSpec):
            raise TypeError("material replenishment factory must return WorkflowSpec or None")
        self._ensure_s09_restore_before_tip_replacement(replenishment_spec)
        if not replenishment_spec.workflow_id:
            raise ValueError("material replenishment workflow_id must not be empty")
        if replenishment_spec.workflow_id == run.spec.workflow_id:
            raise ValueError("material replenishment workflow_id must differ from requester")
        if replenishment_spec.workflow_id in self._workflows:
            raise ValueError(
                f"material replenishment workflow {replenishment_spec.workflow_id} already submitted"
            )
        replenishment_run_id = replenishment_spec.run_id or replenishment_spec.workflow_id
        if any(run_item.run_id == replenishment_run_id for run_item in self._workflows.values()):
            raise ValueError(
                f"material replenishment run_id {replenishment_run_id} already submitted"
            )

        # 补料工作流的优先级由调度器托管，避免工厂误传 normal 导致方案三失效。
        replenishment_spec.priority = self._material_replenishment_priority
        self._material_replenishment_by_run[run.run_id] = replenishment_spec.workflow_id
        self._material_replenished_run_by_workflow[
            replenishment_spec.workflow_id
        ] = run.run_id
        self._emit_monitor(
            "scheduler",
            "material_replenishment_created",
            {
                "workflow_id": run.spec.workflow_id,
                "run_id": run.run_id,
                "replenishment_workflow_id": replenishment_spec.workflow_id,
                "priority": str(replenishment_spec.priority),
            },
        )

        # 当前仍在原工作流 submit 的 RLock 内，直接注册补料工作流并延迟重排；
        # 外层 submit 完成关系登记后统一 reschedule，保证依赖不会先于关系可见。
        trace = start_detached_span(
            "workflow.task.run",
            attributes={
                "workflow.uuid": replenishment_spec.workflow_id,
                "workflow.task.uuid": replenishment_spec.task_id,
                "workflow.run.uuid": replenishment_spec.run_id,
                "workflow.plan.node_count": len(replenishment_spec.nodes),
                "workflow.priority": str(replenishment_spec.priority),
                "workflow.material_replenishment": True,
            },
        )
        self._workflow_spans[replenishment_spec.workflow_id] = trace
        try:
            self._submit_workflow(
                replenishment_spec,
                trigger_reschedule=False,
                allow_material_replenishment=False,
            )
        except BaseException:
            self._workflow_spans.pop(replenishment_spec.workflow_id, None)
            self._material_replenishment_by_run.pop(run.run_id, None)
            self._material_replenished_run_by_workflow.pop(
                replenishment_spec.workflow_id, None
            )
            trace.end()
            raise
        return replenishment_spec.workflow_id

    def _ensure_s09_restore_before_tip_replacement(
        self,
        replenishment_spec: WorkflowSpec,
    ) -> None:
        """为换 TIP 补料图加上不可绕过的 S09 安全位前置节点。

        补料工厂可以返回抽象的 ``replace_tip_box``，也可以直接返回四个
        ``submit_*_s09/s02`` 搬运动作。两种形式都必须先执行
        ``go_to_safe_position``，否则机械臂可能在 S09 仍处于加液姿态时进入
        换盒路径。该约束放在补料 Workflow 的 DAG 中，而不是依赖排序偶然性。
        """

        tip_actions = {
            "replace_tip_box",
            "submit_pick_from_s09",
            "submit_place_to_s02",
            "submit_pick_from_s02",
            "submit_place_to_s09",
        }
        active_nodes = [node for node in replenishment_spec.nodes if not node.disabled]
        if not any(node.action_name in tip_actions for node in active_nodes):
            return
        if any(node.action_name == "go_to_safe_position" for node in active_nodes):
            return

        restore_id = f"{replenishment_spec.workflow_id}:s09-restore"
        existing_ids = {node.id for node in replenishment_spec.nodes}
        if restore_id in existing_ids:
            raise ValueError(
                f"material replenishment node id already exists: {restore_id}"
            )
        restore_node = WorkflowNode(
            id=restore_id,
            device_id=self._material_replenishment_s09_device_id,
            action_name="go_to_safe_position",
            action_type="goal",
            param={
                "home_position": self._material_replenishment_s09_home_position,
                "require_allow": True,
            },
        )
        replenishment_spec.nodes.insert(0, restore_node)
        for node in active_nodes:
            replenishment_spec.edges.append(
                WorkflowEdge(
                    uuid=f"{restore_id}->{node.id}",
                    source_node_id=restore_id,
                    target_node_id=node.id,
                )
            )

    def submit_workflow_runs(
        self,
        specs: List[WorkflowSpec],
        *,
        task_id: str = "",
    ) -> Dict[str, Any]:
        """以一个逻辑 Task 原子提交多个独立 WorkflowRun。

        每个 spec 必须拥有唯一 ``run_id``；节点、队列项和 Job 仍分别绑定到
        自己的 Run。启用库存时先对所有 Run 做整组预留，任一 Run 物料不足就
        回滚本组预留并且不派发任何物理动作（strict admission）。
        """

        if not specs:
            raise ValueError("at least one workflow run is required")
        normalized_task_id = task_id.strip() if isinstance(task_id, str) else ""
        if not normalized_task_id:
            normalized_task_id = specs[0].task_id or specs[0].workflow_id
        run_ids = [spec.run_id or spec.workflow_id for spec in specs]
        if len(set(run_ids)) != len(run_ids):
            raise ValueError("workflow runs must have unique run_id values")
        # 先构图校验全部 Run，再触碰库存或调度器状态，避免部分注册。
        validated = [WorkflowRun(spec) for spec in specs]
        reserved: List[str] = []
        try:
            if self._inventory is not None:
                for run in validated:
                    requirements = run.spec.material_requirements_by_node()
                    if not requirements:
                        continue
                    self._inventory.reserve_workflow(run.run_id, requirements)
                    reserved.append(run.run_id)
        except BaseException:
            for run_id in reserved:
                self._safe_inventory_call(
                    "release_workflow", run_id, reason="group_admission_rollback"
                )
            raise

        results: List[Dict[str, Any]] = []
        try:
            for spec in specs:
                spec.task_id = normalized_task_id
                results.append(self._submit_workflow(spec, trigger_reschedule=False))
            with self._lock:
                dispatched = self._reschedule_locked()
                notifications = self._collect_terminal_notifications()
            self._fire_notifications(notifications)
        except BaseException:
            # 注册失败时尽量撤销尚未开始的 Run；已越过物理边界的 Job 仍由正常
            # 回调完成，不能伪造取消事实。
            for spec in specs:
                if spec.workflow_id in self._workflows:
                    self.cancel_workflow(spec.workflow_id)
            for run_id in reserved:
                if run_id not in self._workflows:
                    self._safe_inventory_call(
                        "release_workflow", run_id, reason="group_registration_rollback"
                    )
            raise
        return {
            "task_id": normalized_task_id,
            "runs": results,
            "dispatched": dispatched,
        }

    def step_workflow(
        self,
        workflow_id: str,
        target_node_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """让暂停的单步工作流只派发一个就绪节点，随后立即恢复暂停。

        参数：``workflow_id`` 是已提交运行身份；``target_node_id`` 可指定本次
        必须放行的就绪节点，空值按稳定图顺序选择第一个。返回本轮派发摘要。
        异常：未知任务、非单步任务、非暂停状态或目标尚未就绪时抛 ``ValueError``。
        """

        with self._lock:
            run = self._workflows.get(workflow_id)
            if run is None:
                raise ValueError(f"workflow {workflow_id} not found")
            if run.spec.run_mode != "step":
                raise ValueError(f"workflow {workflow_id} is not in step mode")
            if run.state is not WorkflowState.PAUSED:
                raise ValueError(f"workflow {workflow_id} is not paused")

            # ready_nodes 只允许 RUNNING 状态读取；该活动态仅存在于本次锁内重排。
            run.state = WorkflowState.RUNNING
            self._dirty_run_ids.add(run.run_id)
            ready_nodes = run.ready_nodes()
            if target_node_id is None:
                selected = ready_nodes[0] if ready_nodes else None
            else:
                selected = next(
                    (node for node in ready_nodes if node.id == target_node_id),
                    None,
                )
            if selected is None:
                run.state = WorkflowState.PAUSED
                raise ValueError("step target is not ready")

            self._step_targets[workflow_id] = selected.id
            try:
                dispatched = self._reschedule_locked()
            finally:
                self._step_targets.pop(workflow_id, None)
                if run.state is WorkflowState.RUNNING:
                    run.state = WorkflowState.PAUSED
            notifications = self._collect_terminal_notifications()
            result = {
                "workflow_id": workflow_id,
                "run_id": run.run_id,
                "state": run.state.value,
                "dispatched": dispatched,
            }
        self._fire_notifications(notifications)
        return result

    def restore_workflow(
        self,
        spec: WorkflowSpec,
        completed_results: Dict[str, Any],
    ) -> Dict[str, Any]:
        """从持久成功事实恢复一个未终态工作流（Workflow）。

        参数：``spec`` 是原任务冻结规格；``completed_results`` 按节点
        UUID 提供已持久成功的返回值。返回：恢复后状态与本轮新派
        发摘要。异常：未知完成节点、重复运行或派发失败原样传播；
        已完成节点只恢复 DAG 状态，绝不重放设备动作。
        """

        completed_node_ids = set(completed_results)
        known_node_ids = {node.id for node in spec.nodes if not node.disabled}
        unknown_node_ids = completed_node_ids - known_node_ids
        if unknown_node_ids:
            raise ValueError(
                f"workflow {spec.workflow_id} has unknown completed nodes: "
                f"{sorted(unknown_node_ids)}"
            )
        run_identity = spec.run_id or spec.workflow_id
        with self._lock:
            if (
                spec.workflow_id in self._workflows
                or spec.workflow_id in self._workflow_spans
                or any(run.run_id == run_identity for run in self._workflows.values())
            ):
                raise ValueError(
                    f"workflow/run {spec.workflow_id}/{run_identity} already submitted"
                )
            workflow_trace = start_detached_span(
                "workflow.task.run",
                attributes={
                    "workflow.uuid": spec.workflow_id,
                    "workflow.task.uuid": spec.task_id,
                    "workflow.plan.node_count": len(spec.nodes),
                    "workflow.recovered.node_count": len(completed_results),
                },
            )
            self._workflow_spans[spec.workflow_id] = workflow_trace
        try:
            with workflow_trace.activate(), self._lock:
                run = WorkflowRun(spec)
                self._workflows[spec.workflow_id] = run
                self._dirty_run_ids.add(run.run_id)
                requirements = spec.material_requirements_by_node()
                if requirements and self._inventory is not None:
                    self._material_workflows.add(run.run_id)
                    if not self._try_reserve(run):
                        run.state = WorkflowState.WAITING_MATERIAL
                        self._deferred_material_workflows.add(run.run_id)
                for node in spec.nodes:
                    if node.id in completed_results:
                        run.mark_finished(node.id, completed_results[node.id])
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
                dispatched = self._reschedule_locked()
                notifications = self._collect_terminal_notifications()
            self._fire_notifications(notifications)
            return {
                "workflow_id": spec.workflow_id,
                "run_id": run.run_id,
                "state": run.state.value,
                "dispatched": dispatched,
            }
        except BaseException as exc:
            workflow_trace.fail(exc)
            workflow_trace.end()
            self._workflow_spans.pop(spec.workflow_id, None)
            raise

    def _try_reserve(self, run: WorkflowRun) -> bool:
        """尝试整 DAG 预留；不足返回 False（幂等，可反复重试）。"""
        try:
            self._inventory.reserve_workflow(
                run.run_id, run.spec.material_requirements_by_node()
            )
            return True
        except InsufficientStock as exc:
            logger.info(
                "[EdgeScheduler] workflow %s waiting for material: %s",
                run.spec.workflow_id,
                exc,
            )
            return False

    def _try_reserve_node(self, run: WorkflowRun, node: WorkflowNode) -> bool:
        """只为一个即将 claim 的 Action 预留物料。"""

        if self._inventory is None or not node.material_requirements:
            return True
        try:
            self._inventory.reserve_workflow(
                run.run_id,
                {node.id: node.material_requirements},
            )
            return True
        except InsufficientStock as exc:
            logger.info(
                "[EdgeScheduler] action %s waits for material (wf=%s): %s",
                node.id,
                run.spec.workflow_id,
                exc,
            )
            return False

    # ── 触发点 2：子 action 完成 ──────────────────────────────

    def on_job_finished(
        self,
        job_id: str,
        success: bool,
        ret_value: Any = None,
        suc_type: str = "normal",
        run_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """接收 Job 完成事实；若调用方提供 run_id，必须与 Job 归属一致。"""
        # 先做归属校验，再触碰 action span；错误 Run 的回调不能消费正确
        # 回调所需的在途追踪上下文，也不能改变 Job 的投递重放语义。
        if run_id is not None:
            with self._lock:
                active_job = self._inflight.get(job_id)
                if active_job is not None and active_job.run_id != run_id:
                    raise ValueError(
                        f"job {job_id} belongs to run {active_job.run_id}, not run {run_id}"
                    )
        action_trace = self._job_spans.get(job_id)
        if action_trace is None:
            return self._on_job_finished(job_id, success, ret_value, suc_type, run_id)
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
                    job_id, success, ret_value, suc_type, run_id
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
        run_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """作业（Job）完成回调：写回结果、清理依赖并强制重排。

        ``suc_type`` 来自设备侧异常决策（registry.action_policy）：
        normal / skip / operator_intervention。skip 表示动作报错后人工选择
        跳过——节点按成功推进，但其已消费物料隔离待复核。
        """
        with self._lock:
            job = self._inflight.get(job_id)
            if job is None:
                logger.warning("[EdgeScheduler] unknown job finished: %s", job_id)
                return {"dispatched": []}
            if run_id is not None and run_id != job.run_id:
                raise ValueError(
                    f"job {job_id} belongs to run {job.run_id}, not run {run_id}"
                )

            # 标准完成事实必须先持久化；任一监听器失败时保留在途作业与资源锁，
            # 允许设备对同一结果进行投递重放（DeliveryReplay）。
            self._notify_job_finished(job_id, success, ret_value, suc_type)
            # 设备成功返回后，先把声明式输出物料登记到同一库存权威；登记失败
            # 时保留 inflight 事实，允许设备结果重放，且 register_material_output
            # 通过稳定 edge_uuid 保证不会重复创建输出实例。
            if success:
                self._register_material_outputs_locked(job, ret_value)
                self._apply_material_effects_locked(job)
            self._inflight.pop(job_id, None)
            self._job_resource_locks.pop(job_id, None)

            # 泳道图时间线：记录实际起止 + 喂给历史统计（EMA）+ 历史库落盘
            self._record_timeline(
                job, success=success, suc_type=suc_type, ret_value=ret_value
            )

            run = self._workflows.get(job.workflow_id)
            if run is None:
                return {"dispatched": []}

            if success:
                run.mark_finished(job.node_id, ret_value)
                self._dirty_run_ids.add(run.run_id)
                if suc_type == "skip" and job.run_id in self._material_workflows:
                    # 异常后跳过：动作未真正完成，该节点已消费物料状态不明 → 隔离
                    logger.warning(
                        "[EdgeScheduler] node %s skipped after error, "
                        "quarantine its consumed materials (wf=%s)",
                        job.node_id,
                        job.run_id,
                    )
                    self._safe_inventory_call(
                        "quarantine_reservation",
                        job.run_id,
                        job.node_id,
                        reason="node_skipped_after_error",
                    )
            else:
                run.mark_failed(job.node_id)
                self._dirty_run_ids.add(run.run_id)
                # 失败节点已物理使用的物料转 quarantined（不虚假加回）
                if job.run_id in self._material_workflows:
                    self._safe_inventory_call(
                        "quarantine_reservation",
                        job.run_id,
                        job.node_id,
                    )
                # 失败工作流的未下发节点不再推进；已下发的等它们各自回调
                logger.warning(
                    "[EdgeScheduler] node %s failed, workflow %s stops advancing",
                    job.node_id,
                    job.workflow_id,
                )

            # 补料工作流成功后，先重新执行原 Run 的整 DAG 物料预留，再进入
            # 普通重排；补料失败则保持原 Run 等料，允许后续人工补料/重试。
            self._resume_material_dependents_locked(job.workflow_id)

            logger.info(
                "[EdgeScheduler] job %s (wf=%s node=%s success=%s) finished, reschedule",
                job_id[:8],
                job.workflow_id,
                job.node_id,
                success,
            )
            dispatched = self._reschedule_locked()
            result = {
                "workflow_id": job.workflow_id,
                "workflow_state": run.state.value,
                "dispatched": dispatched,
            }
            notifications = self._collect_terminal_notifications()
        self._fire_notifications(notifications)
        return result

    def _resume_material_dependents_locked(self, replenishment_workflow_id: str) -> None:
        """处理补料工作流终态对原工作流的影响（须持有调度锁）。"""

        requester_run_id = self._material_replenished_run_by_workflow.get(
            replenishment_workflow_id
        )
        if requester_run_id is None:
            return
        replenishment = self._workflows.get(replenishment_workflow_id)
        requester = next(
            (run for run in self._workflows.values() if run.run_id == requester_run_id),
            None,
        )
        if replenishment is None or requester is None:
            return
        if replenishment.state is not WorkflowState.SUCCESS:
            if replenishment.state in self._TERMINAL_STATES:
                self._emit_monitor(
                    "scheduler",
                    "material_replenishment_failed",
                    {
                        "workflow_id": requester.spec.workflow_id,
                        "replenishment_workflow_id": replenishment_workflow_id,
                        "state": replenishment.state.value,
                    },
                )
            return
        if requester.state is not WorkflowState.WAITING_MATERIAL:
            return
        waiting_node_id = self._material_waiting_nodes.get(requester.run_id)
        waiting_node = next(
            (node for node in requester.spec.nodes if node.id == waiting_node_id),
            None,
        )
        reserved = (
            self._try_reserve_node(requester, waiting_node)
            if requester.run_id in self._deferred_material_workflows
            and waiting_node is not None
            else self._try_reserve(requester)
        )
        if reserved:
            requester.state = WorkflowState.RUNNING
            self._dirty_run_ids.add(requester.run_id)
            self._material_waiting_nodes.pop(requester.run_id, None)
            self._emit_monitor(
                "scheduler",
                "workflow_resumed",
                {
                    "workflow_id": requester.spec.workflow_id,
                    "reason": "material_replenishment_succeeded",
                    "replenishment_workflow_id": replenishment_workflow_id,
                },
            )
            self._safe_history("record_state", requester.spec.workflow_id, "running")
        else:
            self._emit_monitor(
                "scheduler",
                "material_replenishment_recheck_failed",
                {
                    "workflow_id": requester.spec.workflow_id,
                    "replenishment_workflow_id": replenishment_workflow_id,
                },
            )

    def _register_material_outputs_locked(
        self, job: DispatchedJob, ret_value: Any
    ) -> None:
        """登记一个成功 Action 的声明式输出物料（须持有调度锁）。"""
        if self._inventory is None:
            return
        run = self._workflows.get(job.workflow_id)
        if run is None:
            return
        node = next((candidate for candidate in run.spec.nodes if candidate.id == job.node_id), None)
        outputs = list(getattr(node, "material_outputs", []) or []) if node else []
        if not outputs:
            return
        returned: list[dict[str, Any]] = []
        if isinstance(ret_value, dict):
            raw = ret_value.get("material_outputs", ret_value.get("outputs", []))
            if isinstance(raw, list):
                returned = [dict(item) for item in raw if isinstance(item, dict)]
        by_name = {
            str(item.get("output_name") or item.get("name") or ""): item
            for item in returned
        }
        for index, declaration in enumerate(outputs):
            declared = dict(declaration)
            name = str(
                declared.get("output_name") or declared.get("name") or f"output_{index}"
            ).strip()
            observed = by_name.get(name)
            if observed is None and index < len(returned):
                observed = returned[index]
            if observed:
                declared.update(observed)
            template_id = str(
                declared.get("template_id")
                or declared.get("resource_template_uuid")
                or ""
            ).strip()
            if not template_id:
                raise ValueError(
                    f"material output {job.node_id}/{name} missing template_id"
                )
            edge_uuid = str(declared.get("edge_uuid") or "").strip()
            if not edge_uuid:
                edge_uuid = f"material-output:{job.run_id}:{job.node_id}:{name}"
            content = declared.get("content")
            if content is None:
                content = {
                    key: declared[key]
                    for key in ("quantity", "unit", "components", "composition")
                    if key in declared
                }
            registered = self._inventory.register_material_output(
                template_id,
                content if isinstance(content, dict) else {"value": content},
                edge_uuid=edge_uuid,
                barcode=str(declared.get("barcode") or ""),
                parent_uuid=str(
                    declared.get("parent_uuid")
                    or (declared.get("destination") or {}).get("parent_uuid", "")
                    if isinstance(declared.get("destination"), dict)
                    else declared.get("parent_uuid") or ""
                ),
                slot_id=str(
                    declared.get("slot_id")
                    or (declared.get("destination") or {}).get("slot_id", "")
                    if isinstance(declared.get("destination"), dict)
                    else declared.get("slot_id") or ""
                ),
                task_id=run.spec.task_id,
                run_id=job.run_id,
                action_id=job.node_id,
                output_name=name,
                actor="scheduler",
                causation_id=job.job_id,
            )
            instance = registered.get("instance") if isinstance(registered, dict) else None
            output_uuid = (
                instance.get("edge_uuid")
                if isinstance(instance, dict)
                else edge_uuid
            )
            if output_uuid:
                self._material_outputs_by_run.setdefault(job.run_id, set()).add(
                    str(output_uuid)
                )

    def _material_preconditions_met(
        self, node: WorkflowNode, resolved_args: Dict[str, Any]
    ) -> bool:
        """检查 Action 的物料位置/持有者前置条件。"""
        conditions = list(getattr(node, "material_preconditions", []) or [])
        if not conditions or self._inventory is None:
            return True
        for condition in conditions:
            if not isinstance(condition, dict):
                return False
            material_uuid = str(condition.get("material_uuid") or "").strip()
            if not material_uuid:
                param_key = str(condition.get("material_param") or "").strip()
                value = resolved_args.get(param_key) if param_key else None
                if isinstance(value, dict):
                    material_uuid = str(value.get("uuid") or "").strip()
                elif isinstance(value, str):
                    material_uuid = value.strip()
            if not material_uuid:
                return False
            custody = self._inventory.material_custody(material_uuid)
            for field in ("location_kind", "location_uuid", "holder_kind", "holder_uuid"):
                expected = condition.get(field)
                if expected not in (None, "") and str(custody.get(field) or "") != str(expected):
                    return False
        return True

    @staticmethod
    def _transfer_target_binding(
        node: WorkflowNode, resolved_args: Dict[str, Any]
    ) -> tuple[str, str, str] | None:
        """从冻结动作合同解析转运目标 Site 与被转运物料身份。

        返回 ``(target_owner_uuid, target_site_identity, material_uuid)``；动作不是
        标准转运、合同不完整或目标仍无法解析时返回 ``None``。这里只读取冻结
        合同和本次最终参数，不根据动作名称猜测现场工位。
        """

        schema = getattr(node, "param_schema", None)
        if not isinstance(schema, dict):
            return None
        action_contract = schema.get("x-unilabos-action-contract")
        if not isinstance(action_contract, dict):
            return None
        resource_contract = action_contract.get("resource_contract")
        if not isinstance(resource_contract, dict):
            return None
        transfer = resource_contract.get("transfer")
        if not isinstance(transfer, dict):
            return None
        owner_key = str(transfer.get("target_owner_param") or "").strip()
        site_name_key = str(transfer.get("target_site_name_param") or "").strip()
        site_uuid_key = str(transfer.get("target_site_uuid_param") or "").strip()
        material_key = str(transfer.get("material_param") or "").strip()
        owner = resolved_args.get(owner_key) if owner_key else None
        site_key = site_uuid_key or site_name_key
        site = resolved_args.get(site_key) if site_key else None
        material = resolved_args.get(material_key) if material_key else None

        def identity(value: Any) -> str:
            if isinstance(value, dict):
                raw_identity = value.get("uuid") or value.get("unilabos_uuid") or ""
                return str(raw_identity).strip()
            return str(value or "").strip()

        owner_uuid = identity(owner)
        site_identity = identity(site)
        material_uuid = identity(material)
        if not owner_uuid or not site_identity:
            return None
        return owner_uuid, site_identity, material_uuid

    def _target_site_is_available(
        self, node: WorkflowNode, resolved_args: Dict[str, Any]
    ) -> bool:
        """在派发转运动作前检查目标 Site，避免执行器内等待造成流水线死锁。"""

        if self._inventory is None:
            return True
        binding = self._transfer_target_binding(node, resolved_args)
        if binding is None:
            return True
        owner_uuid, site_identity, material_uuid = binding
        occupant = self._inventory.site_occupant(owner_uuid, site_identity)
        return occupant in (None, "", material_uuid)

    def _apply_material_effects_locked(self, job: DispatchedJob) -> None:
        """在 Action 成功后提交物料交接效果（须持有调度锁）。"""
        if self._inventory is None:
            return
        run = self._workflows.get(job.workflow_id)
        if run is None:
            return
        node = next((candidate for candidate in run.spec.nodes if candidate.id == job.node_id), None)
        effects = list(getattr(node, "material_effects", []) or []) if node else []
        if not effects:
            return
        resolved_args = run.resolve_params(job.node_id)
        for effect in effects:
            if not isinstance(effect, dict):
                raise ValueError(f"material effect for {job.node_id} must be an object")
            material_uuid = str(effect.get("material_uuid") or "").strip()
            if not material_uuid:
                param_key = str(effect.get("material_param") or "").strip()
                value = resolved_args.get(param_key) if param_key else None
                material_uuid = (
                    str(value.get("uuid") or "").strip()
                    if isinstance(value, dict)
                    else str(value or "").strip()
                )
            if not material_uuid:
                raise ValueError(f"material effect for {job.node_id} missing material_uuid")
            # Completion callbacks may be redelivered.  If this effect already
            # materialized, treat it as idempotent instead of requiring the old
            # expected custody a second time.
            current = self._inventory.material_custody(material_uuid)
            target = {
                "location_kind": str(effect.get("location_kind") or ""),
                "location_uuid": str(effect.get("location_uuid") or ""),
                "holder_kind": str(effect.get("holder_kind") or ""),
                "holder_uuid": str(effect.get("holder_uuid") or ""),
            }
            if all(current.get(field, "") == value for field, value in target.items()):
                continue
            self._inventory.set_material_custody(
                material_uuid,
                location_kind=target["location_kind"],
                location_uuid=target["location_uuid"],
                holder_kind=target["holder_kind"],
                holder_uuid=target["holder_uuid"],
                expected_location_kind=str(effect.get("expected_location_kind") or ""),
                expected_location_uuid=str(effect.get("expected_location_uuid") or ""),
                expected_holder_kind=str(effect.get("expected_holder_kind") or ""),
                expected_holder_uuid=str(effect.get("expected_holder_uuid") or ""),
                actor="scheduler",
                causation_id=job.job_id,
            )

    # ── 重排核心 ─────────────────────────────────────────────

    def _remove_schedulable_locked(
        self, run_id: str, node_id: Optional[str] = None
    ) -> None:
        """从动态队列移除一个 Run 的一个或全部 Action（须持有调度锁）。"""

        if node_id is not None:
            self._schedulable_queue.pop((run_id, node_id), None)
            return
        for key in tuple(self._schedulable_queue):
            if key[0] == run_id:
                self._schedulable_queue.pop(key, None)

    def _refresh_schedulable_queue_locked(self) -> None:
        """只刷新发生状态变化的 Run，避免每轮扫描所有工作流。"""

        dirty_run_ids = set(self._dirty_run_ids)
        if not dirty_run_ids:
            return
        live_keys: Set[tuple[str, str]] = set()
        for run in self._workflows.values():
            if run.run_id not in dirty_run_ids:
                continue
            if run.state is not WorkflowState.RUNNING:
                self._remove_schedulable_locked(run.run_id)
                continue
            step_target = self._step_targets.get(run.spec.workflow_id)
            for node in run.ready_nodes():
                if step_target is not None and node.id != step_target:
                    continue
                try:
                    resolved_params = run.resolve_params(node.id)
                    resource_lock_keys = tuple(
                        sorted(self._resource_lock_keys(node, resolved_params))
                    )
                except ParamResolveError as exc:
                    logger.error(
                        "[EdgeScheduler] param resolve failed for wf=%s node=%s: %s",
                        run.spec.workflow_id,
                        node.id,
                        exc,
                    )
                    run.mark_failed(node.id)
                    continue
                except MaterialLockSchemaError as error:
                    logger.error(
                        "[EdgeScheduler] action material lock schema failed "
                        "for wf=%s node=%s code=%s path=%s: %s",
                        run.spec.workflow_id,
                        node.id,
                        error.code,
                        error.path,
                        error.message,
                    )
                    run.mark_failed(node.id)
                    continue
                key = (run.run_id, node.id)
                live_keys.add(key)
                if key not in self._schedulable_queue:
                    self._schedulable_queue[key] = SchedulableAction(
                        workflow_id=run.spec.workflow_id,
                        node=node,
                        priority_weight=priority_weight(run.spec.priority),
                        submitted_at=run.spec.submitted_at,
                        is_resource_unblocking=(
                            run.spec.workflow_id
                            in self._material_replenished_run_by_workflow
                        ),
                        run_id=run.run_id,
                        resolved_params=resolved_params,
                        resource_lock_keys=resource_lock_keys,
                    )
                else:
                    # 上游 Action 刚完成时，队列中的参数/锁快照必须刷新，
                    # 不能继续使用上一代父节点返回值。
                    queued = self._schedulable_queue[key]
                    queued.resolved_params = resolved_params
                    queued.resource_lock_keys = resource_lock_keys
        # 完成、失败、取消或 step 目标变化后清理陈旧条目；保留因设备/资源
        # 忙碌而暂时未派发的条目，让后续事件直接触发重排。
        for key in tuple(self._schedulable_queue):
            if key[0] in dirty_run_ids and key not in live_keys:
                self._schedulable_queue.pop(key, None)
        self._dirty_run_ids.difference_update(dirty_run_ids)

    def reschedule(self) -> List[Dict[str, Any]]:
        """手动触发重排并先通知来源准入重试监听器。

        参数：无。返回：本轮旧调度器实际派发摘要。异常：准入监听器或调度
        失败原样传播；监听器在调度锁外运行，可把受阻任务安全注册到本调度器。
        """

        with self._lock:
            admission_retry_listeners = tuple(self._admission_retry_listeners)
        for listener in admission_retry_listeners:
            listener()
        with self._lock:
            return self._reschedule_locked()

    def _reschedule_locked(self) -> List[Dict[str, Any]]:
        with span(
            "workflow.task.reconcile",
            attributes={"scheduler.round": self._reschedule_count + 1},
        ) as reschedule_span:
            dispatched = self._reschedule_impl()
            add_event(
                "workflow.task.reconcile.result",
                {"scheduler.dispatched.count": len(dispatched)},
                span=reschedule_span,
            )
            return dispatched

    def _reschedule_impl(self) -> List[Dict[str, Any]]:
        """执行一轮完整重排，并下发当前能够安全执行的作业（Job）。

        参数：无；读取当前调度器（Scheduler）的工作流、库存、动作物料锁和
        进程内设备忙碌事实。
        Returns:
            本轮成功派发的作业摘要列表；物料冲突保持等待，合同错误标记失败。

        异常：参数解析和动作物料锁合同错误在对应工作流节点上失败关闭；库存
        或派发基础设施异常按既有边界处理。设备级互斥只提供当前进程安全桥，
        不表示已经取得持久作业执行占用（JobExecutionClaim）。
        """

        self._reschedule_count += 1
        workflow_count_before = len(self._workflows)

        # 等料工作流每次重排重试预留（补料后自动恢复 RUNNING）
        if self._inventory is not None:
            for run in self._workflows.values():
                workflow_trace = self._workflow_spans.get(run.spec.workflow_id)
                activation = (
                    workflow_trace.activate()
                    if workflow_trace is not None
                    else span("workflow.material.retry")
                )
                with activation:
                    replenishment_id = self._material_replenishment_by_run.get(
                        run.run_id
                    )
                    replenishment = (
                        self._workflows.get(replenishment_id)
                        if replenishment_id is not None
                        else None
                    )
                    # Scheme 3 dependency is strict while the replenishment Run is
                    # active: an unrelated inbound event must not let the requester
                    # bypass its declared high-priority repair workflow.
                    blocked_by_replenishment = (
                        replenishment is not None
                        and replenishment.state not in self._TERMINAL_STATES
                    )
                    reserved = False
                    if run.state is WorkflowState.WAITING_MATERIAL and not blocked_by_replenishment:
                        if run.run_id in self._deferred_material_workflows:
                            waiting_node_id = self._material_waiting_nodes.get(run.run_id)
                            waiting_node = next(
                                (
                                    node
                                    for node in run.spec.nodes
                                    if node.id == waiting_node_id
                                ),
                                None,
                            )
                            if waiting_node is not None:
                                reserved = self._try_reserve_node(run, waiting_node)
                        else:
                            reserved = self._try_reserve(run)
                if reserved:
                    run.state = WorkflowState.RUNNING
                    self._dirty_run_ids.add(run.run_id)
                    self._material_waiting_nodes.pop(run.run_id, None)
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
                    self._safe_history("record_state", run.spec.workflow_id, "running")

        self._refresh_schedulable_queue_locked()
        ready: List[ReadyTask] = list(self._schedulable_queue.values())

        if not ready:
            if len(self._workflows) > workflow_count_before:
                return self._reschedule_impl()
            return []

        busy = self._busy_keys()
        held_resource_locks = self._held_resource_locks()
        # 先做设备锁准入，再在可准入候选中让资源解阻动作领先用户
        # priority。循环内仍会复核 busy，因为同一轮前面的派发可能刚刚
        # 取得了设备锁。
        ordered = self._orderer.order(
            ready,
            OrderingContext(set(busy), set(held_resource_locks)),
        )

        dispatched: List[Dict[str, Any]] = []
        for task in ordered:
            # 动作键继续服务时长估算、遥测与既有执行协议；设备键独立负责保证
            # 同一设备上的不同动作不会在本轮或跨重排并行派发。
            action_key = task.node.device_action_key
            device_key = task.node.device_lock_key
            # manual_confirm 是 always-free 特殊节点：不占设备动作锁，也不受其阻塞
            manual_confirm = task.node.is_manual_confirm()
            if not manual_confirm and (action_key in busy or device_key in busy):
                # 动作或设备已被占用：本轮跳过，等占用作业完成后准入重试。
                continue

            run = self._workflows[task.workflow_id]
            resolved_args = (
                task.resolved_params
                if isinstance(task, SchedulableAction)
                and task.resolved_params is not None
                else run.resolve_params(task.node.id)
            )
            lock_keys = set(
                task.resource_lock_keys
                if isinstance(task, SchedulableAction)
                else self._resource_lock_keys(task.node, resolved_args)
            )
            if lock_keys & held_resource_locks:
                logger.info(
                    "[EdgeScheduler] node %s waits for resource lock(s) %s (wf=%s)",
                    task.node.id,
                    sorted(lock_keys & held_resource_locks),
                    task.workflow_id,
                )
                continue

            if not self._target_site_is_available(task.node, resolved_args):
                logger.info(
                    "[EdgeScheduler] node %s waits for vacant target site (wf=%s)",
                    task.node.id,
                    task.workflow_id,
                )
                continue

            if not self._material_preconditions_met(task.node, resolved_args):
                logger.info(
                    "[EdgeScheduler] node %s waits for material custody precondition (wf=%s)",
                    task.node.id,
                    task.workflow_id,
                )
                continue

            # 延迟物料门控：只有当前 Action 即将越过 claim 边界时，才为该
            # 节点预留物料。这样连续两个各需 1 支 TIP 的 Action，在库存为
            # 1 时可以先完成第一段，第二段真正开始前才触发补料。
            if (
                task.run_id in self._deferred_material_workflows
                and task.node.material_requirements
                and not self._try_reserve_node(run, task.node)
            ):
                run.state = WorkflowState.WAITING_MATERIAL
                self._dirty_run_ids.add(run.run_id)
                self._material_waiting_nodes[run.run_id] = task.node.id
                self._remove_schedulable_locked(task.run_id)
                if run.run_id not in self._material_replenishment_disabled_runs:
                    if run.run_id not in self._material_replenishment_disabled_runs:
                        self._ensure_material_replenishment_locked(
                            run, run.spec.material_requirements_by_node()
                        )
                continue

            # 节点开始：预留 → FIFO lot 消费 + 实例 deploy（同一 SQLite 事务，幂等）
            if (
                task.run_id in self._material_workflows
                and task.node.material_requirements
            ):
                try:
                    workflow_trace = self._workflow_spans.get(task.workflow_id)
                    activation = (
                        workflow_trace.activate()
                        if workflow_trace is not None
                        else span("workflow.material.consume")
                    )
                    with activation:
                        self._inventory.consume_reservation(
                            task.run_id, task.node.id
                        )
                except InsufficientStock as exc:
                    # 预留与实际 claim 之间若库存事实被外部流程改变，按方案三
                    # 回到物料门控，不让大 action 在缺料状态下越过执行边界。
                    run.state = WorkflowState.WAITING_MATERIAL
                    self._dirty_run_ids.add(run.run_id)
                    self._material_waiting_nodes[run.run_id] = task.node.id
                    self._remove_schedulable_locked(task.run_id)
                    self._ensure_material_replenishment_locked(
                        run, run.spec.material_requirements_by_node()
                    )
                    logger.info(
                        "[EdgeScheduler] material claim blocked for wf=%s node=%s: %s",
                        task.workflow_id,
                        task.node.id,
                        exc,
                    )
                    continue
                except InventoryError as exc:
                    logger.error(
                        "[EdgeScheduler] material consume failed for wf=%s node=%s: %s",
                        task.workflow_id,
                        task.node.id,
                        exc,
                    )
                    run.mark_failed(task.node.id)
                    self._remove_schedulable_locked(task.run_id, task.node.id)
                    continue

            # ``job_id`` 优先复用标准工作流节点作业（WorkflowNodeJob）身份；旧整图
            # 没有提供时才维持历史随机身份行为。
            self._remove_schedulable_locked(task.run_id, task.node.id)
            job_id = task.node.job_id or uuid_mod.uuid4().hex
            payload = build_job_start_payload(
                job_id=job_id,
                task_id=run.spec.task_id,
                workflow_id=task.workflow_id,
                node_id=task.node.id,
                device_id=task.node.device_id,
                action_name=task.node.action_name,
                action_type=task.node.action_type,
                action_args=resolved_args,
                run_id=task.run_id,
            )
            # 预估基于 sjson 覆写后的 resolved 参数：父节点经 gjson/sjson 传下来的
            # 实际值（如 time）直接决定声明式预估结果
            estimated_s, estimate_source = self._estimator.estimate(
                action_key, resolved_args
            )
            workflow_trace = self._workflow_spans.get(task.workflow_id)
            action_trace = start_detached_span(
                "action.run",
                parent_context=(
                    workflow_trace.context if workflow_trace is not None else None
                ),
                attributes={
                    "workflow.job.uuid": job_id,
                    "workflow.uuid": task.workflow_id,
                    "workflow.node.uuid": task.node.id,
                    "device.name": task.node.device_id,
                    "action.name": task.node.action_name,
                    "action.type": task.node.action_type,
                    "action.manual_confirm": manual_confirm,
                },
            )
            self._job_spans[job_id] = action_trace
            try:
                with action_trace.activate():
                    with span(
                        "workflow.job.dispatch",
                        attributes={
                            "workflow.job.uuid": job_id,
                            "workflow.uuid": task.workflow_id,
                            "workflow.node.uuid": task.node.id,
                            "device.name": task.node.device_id,
                            "action.name": task.node.action_name,
                        },
                    ):
                        # 标准任务/作业必须先提交派发意图，才能越过物理执行边界。
                        self._notify_job_pre_dispatch(
                            {
                                "job_id": job_id,
                                "workflow_id": task.workflow_id,
                                "node_id": task.node.id,
                                "device_action_key": action_key,
                                "estimated_s": round(estimated_s, 3),
                                "estimate_source": estimate_source,
                                "resolved_args": resolved_args,
                            }
                        )
                        # 派发意图持久化后，先保守登记本地在途作业和动作物料锁，再
                        # 调用不可原子确认的执行适配器。适配器异常不得回滚这些事实。
                        run.mark_dispatched(task.node.id)
                        self._inflight[job_id] = DispatchedJob(
                            job_id=job_id,
                            workflow_id=task.workflow_id,
                            node_id=task.node.id,
                            device_action_key=action_key,
                            device_id=task.node.device_id,
                            action_name=task.node.action_name,
                            estimated_s=estimated_s,
                            estimate_source=estimate_source,
                            run_id=task.run_id,
                        )
                        if lock_keys:
                            self._job_resource_locks[job_id] = lock_keys
                            held_resource_locks |= lock_keys
                        if not manual_confirm:
                            self._dispatcher.dispatch(payload)
            except BaseException as exc:
                action_trace.fail(exc)
                action_trace.end()
                self._job_spans.pop(job_id, None)
                raise
            # 人工确认节点不进入执行器，但仍已在上方登记为在途作业，由统一完成
            # 接口提交明确结果。
            action_trace.event(
                "action.dispatched",
                {
                    "workflow.job.uuid": job_id,
                    "action.estimate.seconds": estimated_s,
                    "action.estimate.source": estimate_source,
                },
            )
            if not manual_confirm:
                # 同轮立即登记两种键；后续候选即使动作不同，也不能绕过设备互斥。
                busy.update((action_key, device_key))
            # ``dispatched_item`` 同时供返回值、监控和标准 Task/Job 状态投影使用。
            dispatched_item = {
                "job_id": job_id,
                "workflow_id": task.workflow_id,
                "run_id": task.run_id,
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
                    "device_id": task.node.device_id,
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
                        "device_id": task.node.device_id,
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
        if len(self._workflows) > workflow_count_before:
            dispatched.extend(self._reschedule_impl())
        return dispatched

    # 终态集合与云端 workflow_task 一致；TIMEOUT 当前由云端判定，列入以备
    # Edge 后续本地超时（词汇不再变更）。
    _TERMINAL_STATES = (
        WorkflowState.SUCCESS,
        WorkflowState.FAILED,
        WorkflowState.CANCELED,
        WorkflowState.TIMEOUT,
    )

    def _collect_terminal_notifications(self) -> List["tuple[str, str]"]:
        """收集未处理过的终态工作流（须在锁内调用；通知/释放在锁外做）。"""
        pending: List["tuple[str, str]"] = []
        for wid, run in self._workflows.items():
            if (
                run.state not in self._TERMINAL_STATES
                or wid in self._notified_workflows
            ):
                continue
            self._notified_workflows.add(wid)
            pending.append((wid, run.state.value))
            self._emit_monitor(
                "scheduler",
                "workflow_state",
                {"workflow_id": wid, "state": run.state.value},
            )
            self._safe_history("record_state", wid, run.state.value)
        return pending

    def _fire_notifications(self, notifications: List["tuple[str, str]"]) -> None:
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
                    {
                        "workflow.uuid": wid,
                        "workflow.run.uuid": self._workflows[wid].run_id,
                        "workflow.state": state,
                    },
                    span=workflow_trace.span if workflow_trace is not None else None,
                )
                if workflow_trace is not None and state != WorkflowState.SUCCESS.value:
                    workflow_trace.error(f"workflow {state}")
                # 终态工作流释放剩余 active 预留（幂等，依据 DB 状态而非内存）
                if (
                    self._workflows[wid].run_id in self._material_workflows
                    and state != WorkflowState.SUCCESS.value
                ):
                    self._safe_inventory_call(
                        "release_workflow",
                        self._workflows[wid].run_id,
                        reason=f"workflow_{state}",
                    )
                # 失败/取消的 Run 可能已经在某个中间 Action 产出实例；这些输出
                # 未经完整流程确认，统一进入废弃终态，而不是留在可用库存中。
                if state != WorkflowState.SUCCESS.value:
                    for output_uuid in self._material_outputs_by_run.pop(
                        self._workflows[wid].run_id, set()
                    ):
                        self._safe_inventory_call(
                            "discard_instance",
                            output_uuid,
                            reason=f"workflow_{state}_output",
                            actor="scheduler",
                        )
                else:
                    # 成功产物已正式成为库存事实，不再需要 Run 终态清理跟踪。
                    self._material_outputs_by_run.pop(self._workflows[wid].run_id, None)
                if self._workflow_state_listener is not None:
                    try:
                        self._workflow_state_listener(wid, state)
                    except Exception:  # noqa: BLE001 - 通知失败不影响调度
                        logger.exception(
                            "[EdgeScheduler] workflow state listener failed"
                        )
            if workflow_trace is not None:
                workflow_trace.end()
                self._workflow_spans.pop(wid, None)

    def _safe_inventory_call(self, method: str, *args: Any, **kwargs: Any) -> None:
        """调用 inventory（release/quarantine 等善后操作）；失败记日志不阻断调度。"""
        if self._inventory is None:
            return
        try:
            getattr(self._inventory, method)(*args, **kwargs)
        except Exception:  # noqa: BLE001 - 善后失败可由人工经 inventory API 补救
            logger.exception("[EdgeScheduler] inventory.%s failed", method)

    # ── 物料/资源锁 ──────────────────────────────────────────

    def _held_resource_locks(self) -> Set[str]:
        held: Set[str] = set()
        for keys in self._job_resource_locks.values():
            held |= keys
        return held

    def _resource_lock_keys(self, node: Any, resolved_args: Dict[str, Any]) -> Set[str]:
        """生成节点本次执行需要持有的物料锁键。

        参数：``node`` 是当前准备派发的工作流节点（WorkflowNode），
        ``resolved_args`` 是合并上游输出后的最终动作参数。返回：使用
        ``material/{uuid}/exclusive`` 规范格式的物料锁键集合。异常：
        冻结动作合同（Action Contract）、遗留注册表（Registry）Schema
        或最终参数不能安全解析时抛 ``MaterialLockSchemaError``。
        """

        keys: Set[str] = set()
        frozen_schema = getattr(node, "param_schema", None)
        if frozen_schema is not None:
            # ``material_uuids`` 优先来自任务创建时的冻结动作合同。
            material_uuids = compile_material_lock_schema(
                frozen_schema
            ).material_lock_uuids(resolved_args)
            keys.update(
                f"material/{material_uuid}/exclusive"
                for material_uuid in material_uuids
            )
        elif self._material_lock_resolver is not None:
            # ``material_uuids`` 只为无冻结合同的遗留直接调用读取实时注册表。
            material_uuids = self._material_lock_resolver(
                node.device_id,
                node.action_name,
                resolved_args,
            )
            keys.update(
                f"material/{material_uuid}/exclusive"
                for material_uuid in material_uuids
            )
        for req in getattr(node, "material_requirements", []) or []:
            if getattr(req, "instance_uuid", ""):
                keys.add(f"material/{req.instance_uuid}/exclusive")
        # 目标 Site 的数据库占用只会在转运动作成功后落账。派发到完成之间仍需
        # 一个短期锁，避免两个不同机器人在同一轮都看到空位并同时驶向该 Site。
        target_binding = self._transfer_target_binding(node, resolved_args)
        if target_binding is not None:
            owner_uuid, site_identity, _ = target_binding
            keys.add(f"site/{owner_uuid}/{site_identity}/exclusive")
        return keys

    def _busy_keys(self) -> Set[str]:
        """合并外部与本地在途作业的动作级、设备级内存忙碌键。

        参数：无；外部键来自构造注入集合和可选实时提供者。
        返回：供一次准入重排使用的忙碌键副本；不会把人工确认节点计入互斥。
        异常：外部提供者异常会被记录，并沿用既有降级，仅使用已知本地事实。

        该集合不会跨进程重启恢复，也没有占用 UUID 或栅栏令牌，因此不是持久
        作业执行占用（JobExecutionClaim）。
        """

        busy = set(self._external_busy_keys)
        if self._busy_key_provider is not None:
            try:
                busy |= set(self._busy_key_provider())
            except Exception:  # noqa: BLE001 - 锁视图失败时退化为 inflight 视图
                logger.exception("[EdgeScheduler] busy_key_provider failed")
        # 外部执行层仍使用动作级忙碌键；保留原键用于既有协议，同时把严格
        # 形状稳定提升为设备键，使取消后的物理在途作业继续阻塞同设备其他动作。
        for external_action_key in tuple(busy):
            device_key = _device_key_from_strict_action_key(external_action_key)
            if device_key is not None:
                busy.add(device_key)
        for job in self._inflight.values():
            # 由在途作业身份回到其工作流节点，只为识别不占设备的人工确认节点。
            run = self._workflows.get(job.workflow_id)
            node = run.node(job.node_id) if run is not None else None
            # 人工确认节点只等待操作者输入，不使用设备执行器，也不建立设备互斥。
            if node is not None and node.is_manual_confirm():
                continue
            busy.add(job.device_action_key)
            busy.add(f"/devices/{job.device_id}")
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
        ended_at = time.time()
        actual_s = max(0.0, ended_at - job.dispatched_at)
        if not state:
            state = "success" if success else "failed"
        # 只有正常成功的样本才进入历史统计（skip/失败/取消的时长不代表真实执行）
        if success and suc_type == "normal":
            self._estimator.observe(job.device_action_key, actual_s)
        entry = {
            "job_id": job.job_id,
            "workflow_id": job.workflow_id,
            "run_id": job.run_id,
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
                "run_id": job.run_id,
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
                "run_id": job.run_id,
            },
        )

    def timeline(self, window_s: float = 3600.0) -> Dict[str, Any]:
        """泳道图数据：执行中 job + 窗口内已完结 job + 预估器状态。

        泳道由前端按 device_id（或 device_action_key）分组；running 条目
        用 started_at + estimated_s 画预估终点，completed 条目画实际区间。
        """
        now = time.time()
        cutoff = now - max(window_s, 0.0)
        with self._lock:
            running = [
                {
                    "job_id": j.job_id,
                    "workflow_id": j.workflow_id,
                    "run_id": j.run_id,
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
                "estimator": {
                    "mode": self._estimator.mode,
                    "default_s": self._estimator.default_s,
                    "stats": self._estimator.stats(),
                },
            }

    def device_status(self) -> List[Dict[str, Any]]:
        """设备占用视图（监控面板）：busy 来自 inflight，idle 来自时间线痕迹。"""
        now = time.time()
        with self._lock:
            devices: Dict[str, Dict[str, Any]] = {}
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

    def workflow_snapshot(self, workflow_id: str) -> Optional[Dict[str, Any]]:
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
            replenishment_id = self._material_replenishment_by_run.get(run.run_id)
            if replenishment_id is not None:
                snap["material_replenishment"] = {
                    "workflow_id": replenishment_id,
                    "state": (
                        self._workflows[replenishment_id].state.value
                        if replenishment_id in self._workflows
                        else "unknown"
                    ),
                }
            return snap

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "workflows": {
                    wid: run.snapshot() for wid, run in self._workflows.items()
                },
                "inflight_jobs": {
                    job_id: {
                        "workflow_id": j.workflow_id,
                        "run_id": j.run_id,
                        "node_id": j.node_id,
                        "device_id": j.device_id,
                        "action_name": j.action_name,
                        "device_action_key": j.device_action_key,
                        "resource_locks": sorted(
                            self._job_resource_locks.get(job_id, set())
                        ),
                        "started_at": j.dispatched_at,
                        "estimated_s": round(j.estimated_s, 3),
                        "estimate_source": j.estimate_source,
                    }
                    for job_id, j in self._inflight.items()
                },
                "schedulable_queue": [
                    {
                        "run_id": action.run_id,
                        "workflow_id": action.workflow_id,
                        "node_id": action.node.id,
                        "priority_weight": action.priority_weight,
                        "is_resource_unblocking": action.is_resource_unblocking,
                        "submitted_at": action.submitted_at,
                    }
                    for action in self._schedulable_queue.values()
                ],
                "material_replenishments": {
                    run_id: {
                        "replenishment_workflow_id": replenishment_id,
                        "replenishment_state": (
                            self._workflows[replenishment_id].state.value
                            if replenishment_id in self._workflows
                            else "unknown"
                        ),
                    }
                    for run_id, replenishment_id in self._material_replenishment_by_run.items()
                },
                "reschedule_count": self._reschedule_count,
            }

    def cancel_workflow(self, workflow_id: str) -> bool:
        with self._lock:
            run = self._workflows.get(workflow_id)
            if run is None:
                return False
            run.cancel()
            self._dirty_run_ids.add(run.run_id)
            self._remove_schedulable_locked(run.run_id)
            removed = [
                job_id
                for job_id, j in self._inflight.items()
                if j.workflow_id == workflow_id
            ]
            for job_id in removed:
                job = self._inflight.pop(job_id, None)
                self._job_resource_locks.pop(job_id, None)
                action_trace = self._job_spans.pop(job_id, None)
                if action_trace is not None:
                    action_trace.error("action canceled")
                    action_trace.event("action.canceled", {"workflow.job.uuid": job_id})
                    action_trace.end()
                if job is not None:
                    self._record_timeline(job, success=False, state="canceled")
            notifications = self._collect_terminal_notifications()
        self._fire_notifications(notifications)
        return True


__all__ = ["EdgeScheduler"]
