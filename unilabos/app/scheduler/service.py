"""EdgeScheduler：Edge 侧执行态调度器（推进的唯一入口）。

重排触发点（硬性约定，二者都强制全量 reschedule）：

1. **每个工作流提交**（``submit_workflow``）
2. **每个子 action 完成**（``on_job_finished``，含成功/失败）

每次 reschedule：

    收集所有 RUNNING 工作流的 ready 节点
      → TaskOrderer 排序（本地 stub 或 HTTP 调 uni-lab-scheduler）
      → 按序下发；device_action_key 被占用的节点跳过，等下一次触发
      → 下发前解析父节点传参（gjson/sjson + ``@@@`` 语义）

不做一次性拓扑序：ready 集合每次触发点都重新计算、重新排序。

物料衔接（注入 InventoryService 时启用；spec 无物料字段则行为完全不变）：

- submit：汇总 DAG 全部物料需求，入队前 all-or-nothing 预留；
  不足 → workflow 置 ``waiting_for_material``，不进入执行队列，每次重排重试预留
- 节点下发前：预留 → FIFO lot 消费 + 实例 deploy（幂等键 workflow:node:attempt）
- 节点失败：该节点已消费的物料转 quarantined（人工复核，不虚假加回）
- 节点异常后人工选择 skip（suc_type=skip）：节点算成功继续推进，但其已消费
  物料状态不明，同样转 quarantined 待复核
- 工作流终态（failed/canceled）：剩余 active 预留自动 release（依据 DB，不依赖内存）

物料锁（``@action(lock_resource=[...])``，注入 lock_resource_resolver 时启用）：

- 下发前用 resolver 取该动作声明的 ResourceSlot 参数名，从已解析参数里提取
  资源标识生成锁键；与在执行 job 的锁键冲突 → 本轮跳过（等释放后的重排）
- 实体型物料需求（instance_uuid / barcode）自动并入锁键，双保险防同料并用
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
from unilabos.app.scheduler.inventory.domain import InsufficientStock, InventoryError
from unilabos.app.scheduler.models import (
    DispatchedJob,
    ReadyTask,
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
from unilabos.utils.tracing import (
    DetachedSpan,
    add_event,
    span,
    start_detached_span,
)

logger = logging.getLogger(__name__)

# ResourceSlot 参数值里可作为资源标识的字段（按优先级取第一个非空）
_RESOURCE_ID_FIELDS = ("unilabos_uuid", "uuid", "id", "name")


def _extract_resource_ids(value: Any) -> Set[str]:
    """从 action 参数值提取资源标识（锁键素材）。

    支持形态：字符串（uuid/名称）、dict（ResourceSlot 原始入参，含
    unilabos_uuid/uuid/id/name 任一字段，或嵌套 ``data.unilabos_uuid``）、
    以及它们的 list/tuple。取不到标识的值直接忽略（宁可漏锁不误锁）。
    """
    ids: Set[str] = set()
    if value is None:
        return ids
    if isinstance(value, str):
        if value:
            ids.add(value)
        return ids
    if isinstance(value, (list, tuple, set)):
        for item in value:
            ids |= _extract_resource_ids(item)
        return ids
    if isinstance(value, dict):
        nested = value.get("data")
        if isinstance(nested, dict) and nested.get("unilabos_uuid"):
            ids.add(str(nested["unilabos_uuid"]))
            return ids
        for field_name in _RESOURCE_ID_FIELDS:
            field_value = value.get(field_name)
            if isinstance(field_value, str) and field_value:
                ids.add(field_value)
                return ids
        return ids
    return ids


class EdgeScheduler:
    def __init__(
        self,
        orderer: Optional[TaskOrderer] = None,
        dispatcher: Optional[Dispatcher] = None,
        external_busy_keys: Optional[Set[str]] = None,
        busy_key_provider: Optional["Callable[[], Set[str]]"] = None,
        workflow_state_listener: Optional["Callable[[str, str], None]"] = None,
        inventory: Any = None,
        lock_resource_resolver: Optional["Callable[[str, str], List[str]]"] = None,
        estimator: Optional[DurationEstimator] = None,
        timeline_capacity: int = 400,
        monitor: Any = None,
        history: Any = None,
        held_device_provider: Optional["Callable[[], Set[str]]"] = None,
    ):
        self._orderer = orderer or StableLocalOrderer()
        self._dispatcher = dispatcher or RecordingDispatcher()
        self._lock = threading.RLock()

        self._workflows: Dict[str, WorkflowRun] = {}
        # job_id -> DispatchedJob（完成回调路由 + 资源锁）
        self._inflight: Dict[str, DispatchedJob] = {}
        # 外部注入的锁（例如 DeviceActionManager 已占用的设备），可选
        self._external_busy_keys = external_busy_keys if external_busy_keys is not None else set()
        # 实时锁视图提供者（微后端 busy_device_action_keys），可选
        self._busy_key_provider = busy_key_provider
        # 工作流终态通知（success/failed/canceled 各通知一次；锁外触发）
        self._workflow_state_listener = workflow_state_listener
        self._notified_workflows: Set[str] = set()
        self._reschedule_count = 0
        # 可选 InventoryService（duck-typed：reserve_workflow / consume_reservation /
        # quarantine_reservation / release_workflow）；None = 物料衔接整体关闭
        self._inventory = inventory
        # 有物料需求的 workflow（其余 workflow 不产生任何 inventory 调用）
        self._material_workflows: Set[str] = set()
        # 物料/资源锁：resolver(device_id, action_name) -> @action(lock_resource=[...])
        # 声明的参数名列表；None = 物料锁关闭
        self._lock_resource_resolver = lock_resource_resolver
        # job_id -> 该 job 持有的资源锁键（job 完成/取消时释放）
        self._job_resource_locks: Dict[str, Set[str]] = {}
        # 时长预估器（declared / historical / auto 三种 mode，内含两种计算模式）
        self._estimator = estimator or DurationEstimator()
        # 泳道图时间线：已完结 job 的起止记录（环形缓冲）
        self._timeline: Deque[Dict[str, Any]] = deque(maxlen=timeline_capacity)
        # 实时监控总线（duck-typed emit(channel, type, data)）；None = 关闭
        self._monitor = monitor
        # 工作流执行历史（WorkflowHistoryStore，独立 SQLite）；None = 不落盘
        self._history = history
        # 设备状态 incident 对新 dispatch 的暂停视图；运行中的动作不被追杀。
        self._held_device_provider = held_device_provider
        # 长生命周期根 span：workflow → action/job。只保存上下文/句柄，不保存 payload。
        self._workflow_spans: Dict[str, DetachedSpan] = {}
        self._job_spans: Dict[str, DetachedSpan] = {}

    def _emit_monitor(self, channel: str, event_type: str, data: Dict[str, Any]) -> None:
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

    def set_workflow_state_listener(self, listener: "Callable[[str, str], None]") -> None:
        self._workflow_state_listener = listener

    # ── 触发点 1：任务进来 ────────────────────────────────────

    def submit_workflow(self, spec: WorkflowSpec) -> Dict[str, Any]:
        with self._lock:
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

    def _submit_workflow(self, spec: WorkflowSpec) -> Dict[str, Any]:
        """提交工作流并立即重排。返回本次下发结果。

        带物料需求时：入队前整 DAG all-or-nothing 预留；不足则置
        ``waiting_for_material``（不进入执行队列，后续每次重排自动重试）。
        """
        with self._lock:
            if spec.workflow_id in self._workflows:
                raise ValueError(f"workflow {spec.workflow_id} already submitted")
            run = WorkflowRun(spec)  # 构图 + 环检测，失败直接抛
            self._workflows[spec.workflow_id] = run

            requirements = spec.material_requirements_by_node()
            if requirements:
                if self._inventory is None:
                    logger.warning(
                        "[EdgeScheduler] workflow %s declares materials but no inventory "
                        "service wired; proceeding without reservation",
                        spec.workflow_id,
                    )
                else:
                    self._material_workflows.add(spec.workflow_id)
                    if not self._try_reserve(run):
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
            dispatched = self._reschedule_locked()
            notifications = self._collect_terminal_notifications()
        self._fire_notifications(notifications)
        return {
            "workflow_id": spec.workflow_id,
            "state": run.state.value,
            "dispatched": dispatched,
        }

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

    def on_job_finished(
        self,
        job_id: str,
        success: bool,
        ret_value: Any = None,
        suc_type: str = "normal",
    ) -> Dict[str, Any]:
        action_trace = self._job_spans.get(job_id)
        if action_trace is None:
            return self._on_job_finished(job_id, success, ret_value, suc_type)
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
                    job_id, success, ret_value, suc_type
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
    ) -> Dict[str, Any]:
        """job 完成回调（成功或失败）：写回结果 → 清依赖 → 强制重排。

        ``suc_type`` 来自设备侧异常决策（registry.action_policy）：
        normal / skip / operator_intervention。skip 表示动作报错后人工选择
        跳过——节点按成功推进，但其已消费物料隔离待复核。
        """
        with self._lock:
            job = self._inflight.pop(job_id, None)
            self._job_resource_locks.pop(job_id, None)
            if job is None:
                logger.warning("[EdgeScheduler] unknown job finished: %s", job_id)
                return {"dispatched": []}

            # 泳道图时间线：记录实际起止 + 喂给历史统计（EMA）+ 历史库落盘
            self._record_timeline(job, success=success, suc_type=suc_type, ret_value=ret_value)

            run = self._workflows.get(job.workflow_id)
            if run is None:
                return {"dispatched": []}

            if success:
                run.mark_finished(job.node_id, ret_value)
                if suc_type == "skip" and job.workflow_id in self._material_workflows:
                    # 异常后跳过：动作未真正完成，该节点已消费物料状态不明 → 隔离
                    logger.warning(
                        "[EdgeScheduler] node %s skipped after error, "
                        "quarantine its consumed materials (wf=%s)",
                        job.node_id,
                        job.workflow_id,
                    )
                    self._safe_inventory_call(
                        "quarantine_reservation",
                        job.workflow_id,
                        job.node_id,
                        reason="node_skipped_after_error",
                    )
            else:
                run.mark_failed(job.node_id)
                # 失败节点已物理使用的物料转 quarantined（不虚假加回）
                if job.workflow_id in self._material_workflows:
                    self._safe_inventory_call(
                        "quarantine_reservation", job.workflow_id, job.node_id,
                    )
                # 失败工作流的未下发节点不再推进；已下发的等它们各自回调
                logger.warning(
                    "[EdgeScheduler] node %s failed, workflow %s stops advancing",
                    job.node_id,
                    job.workflow_id,
                )

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

    # ── 重排核心 ─────────────────────────────────────────────

    def reschedule(self) -> List[Dict[str, Any]]:
        """手动触发重排（API 暴露；正常推进依赖两个自动触发点）。"""
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
        self._reschedule_count += 1

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
                    reserved = (
                        run.state is WorkflowState.WAITING_MATERIAL
                        and self._try_reserve(run)
                    )
                if reserved:
                    run.state = WorkflowState.RUNNING
                    logger.info(
                        "[EdgeScheduler] workflow %s material reserved, resume running",
                        run.spec.workflow_id,
                    )
                    self._emit_monitor(
                        "scheduler",
                        "workflow_resumed",
                        {"workflow_id": run.spec.workflow_id, "reason": "material_reserved"},
                    )
                    self._safe_history("record_state", run.spec.workflow_id, "running")

        ready: List[ReadyTask] = []
        for run in self._workflows.values():
            if run.state is not WorkflowState.RUNNING:
                continue
            weight = priority_weight(run.spec.priority)
            for node in run.ready_nodes():
                ready.append(
                    ReadyTask(
                        workflow_id=run.spec.workflow_id,
                        node=node,
                        priority_weight=weight,
                        submitted_at=run.spec.submitted_at,
                    )
                )

        if not ready:
            return []

        busy = self._busy_keys()
        held_resource_locks = self._held_resource_locks()
        held_devices = (
            set(self._held_device_provider())
            if self._held_device_provider is not None
            else set()
        )
        ordered = self._orderer.order(ready, OrderingContext(set(busy)))

        dispatched: List[Dict[str, Any]] = []
        for task in ordered:
            key = task.node.device_action_key
            # manual_confirm 是 always-free 特殊节点：不占设备动作锁，也不受其阻塞
            manual_confirm = task.node.is_manual_confirm()
            if not manual_confirm and task.node.device_id in held_devices:
                logger.info(
                    "[EdgeScheduler] node %s waits for device status recovery (%s)",
                    task.node.id,
                    task.node.device_id,
                )
                continue
            if not manual_confirm and key in busy:
                # 设备/动作被占用：本轮跳过，等占用 job 完成的那次重排再下发
                continue

            run = self._workflows[task.workflow_id]
            try:
                resolved_args = run.resolve_params(task.node.id)
            except ParamResolveError as exc:
                logger.error(
                    "[EdgeScheduler] param resolve failed for wf=%s node=%s: %s",
                    task.workflow_id,
                    task.node.id,
                    exc,
                )
                run.mark_failed(task.node.id)
                continue

            # 物料锁：@action(lock_resource=[...]) 声明的资源被在执行 job 占用 → 本轮跳过
            lock_keys = self._resource_lock_keys(task.node, resolved_args)
            if lock_keys & held_resource_locks:
                logger.info(
                    "[EdgeScheduler] node %s waits for resource lock(s) %s (wf=%s)",
                    task.node.id,
                    sorted(lock_keys & held_resource_locks),
                    task.workflow_id,
                )
                continue

            # 节点开始：预留 → FIFO lot 消费 + 实例 deploy（同一 SQLite 事务，幂等）
            if (
                task.workflow_id in self._material_workflows
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
                            task.workflow_id, task.node.id
                        )
                except InventoryError as exc:
                    logger.error(
                        "[EdgeScheduler] material consume failed for wf=%s node=%s: %s",
                        task.workflow_id,
                        task.node.id,
                        exc,
                    )
                    run.mark_failed(task.node.id)
                    continue

            job_id = uuid_mod.uuid4().hex
            payload = build_job_start_payload(
                job_id=job_id,
                task_id=run.spec.task_id,
                workflow_id=task.workflow_id,
                node_id=task.node.id,
                device_id=task.node.device_id,
                action_name=task.node.action_name,
                action_type=task.node.action_type,
                action_args=resolved_args,
            )
            # 预估基于 sjson 覆写后的 resolved 参数：父节点经 gjson/sjson 传下来的
            # 实际值（如 time）直接决定声明式预估结果
            estimated_s, estimate_source = self._estimator.estimate(key, resolved_args)
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
                        if not manual_confirm:
                            self._dispatcher.dispatch(payload)
            except BaseException as exc:
                action_trace.fail(exc)
                action_trace.end()
                self._job_spans.pop(job_id, None)
                raise
            # manual_confirm 不进执行器：job 停驻在 inflight，
            # 由 POST /jobs/{job_id}/finish（人工确认）走统一完成路径
            run.mark_dispatched(task.node.id)
            self._inflight[job_id] = DispatchedJob(
                job_id=job_id,
                workflow_id=task.workflow_id,
                node_id=task.node.id,
                device_action_key=key,
                device_id=task.node.device_id,
                action_name=task.node.action_name,
                estimated_s=estimated_s,
                estimate_source=estimate_source,
            )
            action_trace.event(
                "action.dispatched",
                {
                    "workflow.job.uuid": job_id,
                    "action.estimate.seconds": estimated_s,
                    "action.estimate.source": estimate_source,
                },
            )
            if not manual_confirm:
                busy.add(key)
            if lock_keys:
                self._job_resource_locks[job_id] = lock_keys
                held_resource_locks |= lock_keys
            dispatched.append(
                {
                    "job_id": job_id,
                    "workflow_id": task.workflow_id,
                    "node_id": task.node.id,
                    "device_action_key": key,
                    "estimated_s": round(estimated_s, 3),
                    "estimate_source": estimate_source,
                }
            )
            self._emit_monitor(
                "action",
                "job_dispatched",
                {
                    "job_id": job_id,
                    "workflow_id": task.workflow_id,
                    "node_id": task.node.id,
                    "device_id": task.node.device_id,
                    "action_name": task.node.action_name,
                    "device_action_key": key,
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
                        "device_action_key": key,
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
            if run.state not in self._TERMINAL_STATES or wid in self._notified_workflows:
                continue
            self._notified_workflows.add(wid)
            pending.append((wid, run.state.value))
            self._emit_monitor(
                "scheduler", "workflow_state", {"workflow_id": wid, "state": run.state.value},
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
                    {"workflow.uuid": wid, "workflow.state": state},
                    span=workflow_trace.span if workflow_trace is not None else None,
                )
                if (
                    workflow_trace is not None
                    and state != WorkflowState.SUCCESS.value
                ):
                    workflow_trace.error(f"workflow {state}")
            # 终态工作流释放剩余 active 预留（幂等，依据 DB 状态而非内存）
                if wid in self._material_workflows and state != WorkflowState.SUCCESS.value:
                    self._safe_inventory_call(
                        "release_workflow", wid, reason=f"workflow_{state}",
                    )
                if self._workflow_state_listener is not None:
                    try:
                        self._workflow_state_listener(wid, state)
                    except Exception:  # noqa: BLE001 - 通知失败不影响调度
                        logger.exception("[EdgeScheduler] workflow state listener failed")
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
        """节点的资源锁键集合：lock_resource 参数值 + 实体型物料需求。"""
        keys: Set[str] = set()
        if self._lock_resource_resolver is not None:
            try:
                param_names = self._lock_resource_resolver(node.device_id, node.action_name) or []
            except Exception:  # noqa: BLE001 - resolver 失败按无锁处理，不阻断下发
                logger.exception("[EdgeScheduler] lock_resource resolver failed")
                param_names = []
            for name in param_names:
                for rid in _extract_resource_ids(resolved_args.get(name)):
                    keys.add(f"res:{rid}")
        for req in getattr(node, "material_requirements", []) or []:
            if getattr(req, "instance_uuid", ""):
                keys.add(f"res:{req.instance_uuid}")
            elif getattr(req, "barcode", ""):
                keys.add(f"res:barcode:{req.barcode}")
        return keys

    def _busy_keys(self) -> Set[str]:
        busy = set(self._external_busy_keys)
        if self._busy_key_provider is not None:
            try:
                busy |= set(self._busy_key_provider())
            except Exception:  # noqa: BLE001 - 锁视图失败时退化为 inflight 视图
                logger.exception("[EdgeScheduler] busy_key_provider failed")
        for job in self._inflight.values():
            busy.add(job.device_action_key)
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
            return snap

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "workflows": {wid: run.snapshot() for wid, run in self._workflows.items()},
                "inflight_jobs": {
                    job_id: {
                        "workflow_id": j.workflow_id,
                        "node_id": j.node_id,
                        "device_action_key": j.device_action_key,
                        "resource_locks": sorted(self._job_resource_locks.get(job_id, set())),
                        "started_at": j.dispatched_at,
                        "estimated_s": round(j.estimated_s, 3),
                        "estimate_source": j.estimate_source,
                    }
                    for job_id, j in self._inflight.items()
                },
                "reschedule_count": self._reschedule_count,
            }

    def cancel_workflow(self, workflow_id: str) -> bool:
        with self._lock:
            run = self._workflows.get(workflow_id)
            if run is None:
                return False
            run.cancel()
            removed = [
                job_id for job_id, j in self._inflight.items() if j.workflow_id == workflow_id
            ]
            for job_id in removed:
                job = self._inflight.pop(job_id, None)
                self._job_resource_locks.pop(job_id, None)
                action_trace = self._job_spans.pop(job_id, None)
                if action_trace is not None:
                    action_trace.error("action canceled")
                    action_trace.event(
                        "action.canceled", {"workflow.job.uuid": job_id}
                    )
                    action_trace.end()
                if job is not None:
                    self._record_timeline(job, success=False, state="canceled")
            notifications = self._collect_terminal_notifications()
        self._fire_notifications(notifications)
        return True


__all__ = ["EdgeScheduler"]
