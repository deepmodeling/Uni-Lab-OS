"""设备执行微后端（job 生命周期管理层）。

定位：调度器与执行器之间的解耦层——

    EdgeScheduler（DAG 决策/排序）
        │ dispatch(job_start payload)         ↑ on_job_finished(job_id, ...)
        ▼                                     │
    JobExecutionBackend（本模块：job_start 生命周期 + 设备锁队列 + 状态回报路由）
        │ adapter.send_goal                   ↑ publish_job_status（bridge 形状）
        ▼                                     │
    HostLink / ROS2 设备执行适配器

- 对调度器：实现 ``Dispatcher`` 协议（``dispatch``），并以 listener 回推完成事件；
  调度器不感知 HostNode/DeviceActionManager。
- 对执行适配器：实现 bridge 形状（``publish_job_status`` / ``publish_device_status``），
  注册进 adapter bridges 即可接收执行回报与设备属性更新；与
  legacy_support.websocket.LegacyWebSocketClient 同款接口，两条链路可并存。
- 设备锁队列直接复用 legacy_support.websocket.DeviceActionManager（其不依赖 WS 连接）。
- 设备状态归本微后端管：属性更新经 worker 串行写入独立的
  DeviceStateStore（SQLite WAL，与物料/工作流库分开），并向监控总线
  device 通道发 device_property 事件。
- 所有 send_goal 与完成处理都在内部 worker 线程串行执行，避免在 ROS 回调线程里
  阻塞（与 QueueProcessor.pending_starts 同样的动机）。
"""

from __future__ import annotations

import logging
import json
import queue
import threading
import time
import uuid
from copy import deepcopy
from typing import Any, Callable, Dict, List, Mapping, Optional, Set

from unilabos.legacy_support.websocket import (
    DeviceActionManager,
    JobInfo,
    JobStatus,
    QueueItem,
    format_job_log,
)
from unilabos.server.scheduler.dispatch import DispatchPayload
from unilabos.server.scheduler.material_locks import (
    MaterialActionLockManager,
    extract_material_uuids,
)
from unilabos.registry.action_policy import (
    SUCCESS_TYPE_OPERATOR_INTERVENTION,
    resolve_error_options_by_names,
)
from unilabos.registry.material_locks import normalize_material_parameter_names
from unilabos.utils.type_check import serialize_result_info
from unilabos.utils.tracing import (
    add_event,
    capture_context,
    extract_trace_context,
    inject_trace_context,
    span,
    use_context,
)

logger = logging.getLogger(__name__)

# listener 签名：(job_id, success, ret_value, suc_type) -> None
# suc_type 取值 normal / skip / operator_intervention（见 registry.action_policy）
JobFinishedListener = Callable[[str, bool, Any, str], None]

class JobExecutionBackend:
    """job_start 生命周期微后端。"""

    owns_job_lifecycle = True
    _ACTION_ERROR_DECISION_TOMBSTONE_TTL_SECONDS = 3600.0

    def __init__(
        self,
        device_manager: Optional[DeviceActionManager] = None,
        host_node_getter: Optional[Callable[[], Any]] = None,
        device_state_store: Any = None,
        monitor: Any = None,
        status_policy_resolver: Optional[
            Callable[[str, str], Optional[Dict[str, Any]]]
        ] = None,
        status_incidents: Any = None,
        result_bridges: Optional[List[Any]] = None,
        queue_conflicts: bool = False,
        materials_need_lock_resolver: Optional[
            Callable[[str, str], List[str]]
        ] = None,
    ):
        self.device_manager = device_manager or DeviceActionManager()
        self._host_node_getter = host_node_getter or self._default_host_getter
        self._listeners: List[JobFinishedListener] = []
        # 设备状态存储（DeviceStateStore；None = 不落盘）与监控总线
        self.device_state = device_state_store
        self._monitor = monitor
        self._status_policy_resolver = status_policy_resolver
        self.status_incidents = status_incidents
        self.result_bridges = [
            bridge for bridge in (result_bridges or []) if bridge is not self
        ]
        # 后端控制模式不在 Edge 重排命令；冲突代表上游调度错误。
        self.queue_conflicts = bool(queue_conflicts)
        self._materials_need_lock_resolver = materials_need_lock_resolver
        self._material_locks = MaterialActionLockManager()
        self._job_material_uuids: Dict[str, tuple[str, ...]] = {}
        self._material_waiting_jobs: Dict[str, JobInfo] = {}
        self._dispatch_lock = threading.RLock()
        self._pending_action_error_decisions: Dict[str, Dict[str, Any]] = {}
        self._resolved_action_error_decisions: Dict[str, Dict[str, Any]] = {}
        self._pending_action_error_decisions_lock = threading.RLock()
        self._status_held_jobs: Dict[str, JobInfo] = {}
        self._status_held_lock = threading.RLock()
        if self.status_incidents is not None:
            self.status_incidents.add_listener(self._on_status_incident_event)

        self._events: "queue.Queue[tuple[Any, tuple]]" = queue.Queue()
        self._worker: Optional[threading.Thread] = None
        self._running = False
        self._pending = 0
        self._pending_lock = threading.Lock()

    # ── 生命周期 ─────────────────────────────────────────────

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._worker = threading.Thread(target=self._run, daemon=True, name="JobExecutionBackend")
        self._worker.start()

    def stop(self) -> None:
        self._running = False
        self._events.put((None, ("__stop__",)))
        if self._worker and self._worker.is_alive():
            self._worker.join(timeout=2)
        if self.status_incidents is not None:
            self.status_incidents.remove_listener(self._on_status_incident_event)

    def wait_idle(self, timeout: float = 5.0) -> bool:
        """等待全部已入队事件处理完（测试/关停用）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._pending_lock:
                if self._pending == 0:
                    return True
            time.sleep(0.01)
        return False

    def _put_event(self, event: tuple, context: Any = None) -> None:
        with self._pending_lock:
            self._pending += 1
        self._events.put(
            (context if context is not None else capture_context(), event)
        )

    # ── 调度器侧接口（Dispatcher 协议） ───────────────────────

    def dispatch(self, payload: DispatchPayload) -> None:
        """接收调度器下发的 job_start 载荷：入队/直发（同 _handle_job_start 语义）。"""
        job_info = JobInfo(
            job_id=payload["job_id"],
            task_id=payload.get("task_id", ""),
            device_id=payload["device_id"],
            notebook_id=payload.get("notebook_id", "") or "",
            action_name=payload["action"],
            device_action_key=f"/devices/{payload['device_id']}/{payload['action']}",
            status=JobStatus.QUEUE,
            start_time=time.time(),
            always_free=bool(payload.get("always_free", False)),
            action_type=payload.get("action_type", ""),
            action_args=payload.get("action_args", {}) or {},
            sample_material=payload.get("sample_material", {}) or {},
            server_info=payload.get("server_info"),
            node_id=payload.get("node_id", ""),
            retry_count=int(payload.get("retry_count", 0) or 0),
        )
        try:
            parameter_names = normalize_material_parameter_names(
                payload.get("materials_need_lock")
            )
            # Wire 元数据用于持久化重放，但不能削弱本地注册表声明。
            parameter_names.extend(
                self._resolve_material_lock_parameters(
                    job_info.device_id,
                    job_info.action_name,
                )
            )
            material_uuids = self._material_uuids_from_arguments(
                parameter_names,
                job_info.action_args,
            )
        except (TypeError, ValueError) as exc:
            self._reject_job(
                job_info,
                str(exc),
                "MaterialLockValidationError",
            )
            return
        self._job_material_uuids[job_info.job_id] = material_uuids
        if (
            not job_info.always_free
            and self.status_incidents is not None
            and self.status_incidents.is_device_held(job_info.device_id)
        ):
            if not self.queue_conflicts:
                self._reject_job(
                    job_info,
                    "device is blocked by an active status incident",
                    "DeviceStatusConflict",
                )
                return
            with self._status_held_lock:
                self._status_held_jobs[job_info.job_id] = job_info
            logger.warning(
                "[JobExecutionBackend] job %s held by active device status incident",
                format_job_log(
                    job_info.job_id,
                    job_info.task_id,
                    job_info.device_id,
                    job_info.action_name,
                ),
            )
            if self._monitor is not None:
                self._monitor.emit(
                    "action",
                    "job_status_held",
                    {
                        "job_id": job_info.job_id,
                        "task_id": job_info.task_id,
                        "device_id": job_info.device_id,
                        "action_name": job_info.action_name,
                        "reason": "device_status_incident",
                    },
                )
            return
        self._enqueue_job(job_info)

    def _enqueue_job(self, job_info: JobInfo) -> None:
        """Enter DeviceActionManager after status holds have been checked."""

        with span(
            "action.queue",
            attributes={
                "workflow.job.uuid": job_info.job_id,
                "workflow.task.uuid": job_info.task_id,
                "device.name": job_info.device_id,
                "action.name": job_info.action_name,
            },
        ) as queue_span:
            # 后续 worker 以 queue span 为父；只保存 OTel context，不保存业务 payload。
            job_info.trace_context = capture_context()
            with self._dispatch_lock:
                if self.device_manager.get_job_info(job_info.job_id) is not None:
                    logger.info(
                        "[JobExecutionBackend] duplicate job %s ignored",
                        job_info.job_id,
                    )
                    return
                should_start_now, _lock_became_busy = (
                    self.device_manager.enqueue_job(job_info)
                )
                if not should_start_now and not self.queue_conflicts:
                    # enqueue_job 的锁判断与登记是原子的；随即移除，仅用它做
                    # 冲突检测，不把上游调度错误变成本地等待队列。
                    self.device_manager.cancel_job(job_info.job_id)
            add_event(
                "action.queued",
                {"action.queue.start_immediately": should_start_now},
                span=queue_span,
            )
        job_log = format_job_log(job_info.job_id, job_info.task_id, job_info.device_id, job_info.action_name)
        if should_start_now:
            logger.info("[JobExecutionBackend] job %s start now", job_log)
            self._request_material_locks_or_wait(job_info)
        elif not self.queue_conflicts:
            logger.error(
                "[JobExecutionBackend] scheduler dispatched conflicting job %s",
                job_log,
            )
            self._reject_job(
                job_info,
                "backend scheduler dispatched a conflicting device action",
                "SchedulerDispatchConflict",
            )
        else:
            logger.info("[JobExecutionBackend] job %s queued", job_log)

    def _resolve_material_lock_parameters(
        self, device_id: str, action_name: str
    ) -> List[str]:
        if self._materials_need_lock_resolver is not None:
            return normalize_material_parameter_names(
                self._materials_need_lock_resolver(device_id, action_name)
            )
        adapter = self._host_node_getter()
        mappings = getattr(adapter, "_action_value_mappings", {}) if adapter else {}
        actions = mappings.get(device_id, {}) if isinstance(mappings, dict) else {}
        for candidate in (action_name, f"auto-{action_name}"):
            mapping = actions.get(candidate)
            if isinstance(mapping, dict):
                return normalize_material_parameter_names(
                    mapping.get("materials_need_lock")
                )
        return []

    @staticmethod
    def _material_uuids_from_arguments(
        parameter_names: Any,
        action_args: Mapping[str, Any],
    ) -> tuple[str, ...]:
        names = normalize_material_parameter_names(parameter_names)
        material_uuids: Set[str] = set()
        for name in names:
            if name not in action_args:
                raise ValueError(
                    f"materials_need_lock 参数 {name!r} 未出现在 action_args 中"
                )
            resolved = extract_material_uuids(action_args[name])
            if not resolved:
                raise ValueError(
                    f"materials_need_lock 参数 {name!r} 无法解析权威物料 UUID"
                )
            material_uuids.update(resolved)
        return MaterialActionLockManager.canonicalize(material_uuids)

    def _request_material_locks_or_wait(self, job: JobInfo) -> None:
        with self._dispatch_lock:
            material_uuids = self._job_material_uuids.get(job.job_id, ())
            acquired = self._material_locks.request(job.job_id, material_uuids)
            if not acquired:
                self._material_waiting_jobs[job.job_id] = job
        if acquired:
            self._put_event(("start", job), context=job.trace_context)
            return
        logger.info(
            "[JobExecutionBackend] job %s 等待物料 UUID %s",
            job.job_id[:8],
            list(material_uuids),
        )

    def _release_material_locks(self, job_id: str) -> None:
        ready_jobs: List[JobInfo] = []
        with self._dispatch_lock:
            ready_job_ids = self._material_locks.release(job_id)
            self._job_material_uuids.pop(job_id, None)
            self._material_waiting_jobs.pop(job_id, None)
            while ready_job_ids:
                ready_job_id = ready_job_ids.pop(0)
                ready_job = self._material_waiting_jobs.pop(ready_job_id, None)
                if (
                    ready_job is None
                    or self.device_manager.get_job_info(ready_job_id) is None
                ):
                    ready_job_ids.extend(
                        self._material_locks.release(ready_job_id)
                    )
                    continue
                ready_jobs.append(ready_job)
        for ready_job in ready_jobs:
            self._put_event(
                ("start", ready_job),
                context=getattr(ready_job, "trace_context", None),
            )

    def _reject_job(
        self,
        job: JobInfo,
        message: str,
        exception_type: str,
    ) -> None:
        """把微后端无法接受的调度命令作为该 attempt 的 failed 回报。"""

        self._release_material_locks(job.job_id)

        item = QueueItem(
            task_type="job_call_back_status",
            device_id=job.device_id,
            action_name=job.action_name,
            task_id=job.task_id,
            job_id=job.job_id,
            notebook_id=job.notebook_id,
            device_action_key=job.device_action_key,
            node_id=job.node_id,
            retry_count=job.retry_count,
        )
        item.trace_context = getattr(job, "trace_context", {})
        return_info = serialize_result_info(
            message,
            False,
            {},
            error_info={
                "action_name": job.action_name,
                "exception_type": exception_type,
                "exception_mro": [exception_type, "RuntimeError", "Exception"],
                "error_message": message,
                "category": "scheduling",
                "severity": "fatal",
            },
        )
        if not self._begin_action_error_decision(item, return_info, {}):
            self._release_terminal(item, "failed", return_info, {})

    def add_job_finished_listener(self, listener: Callable[..., None]) -> None:
        """注册完成回调；兼容 3 参 (job_id, success, ret_value) 旧签名。"""
        import inspect

        try:
            params = [
                p for p in inspect.signature(listener).parameters.values()
                if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD, p.VAR_POSITIONAL)
            ]
            accepts_suc_type = any(p.kind == p.VAR_POSITIONAL for p in params) or len(params) >= 4
        except (TypeError, ValueError):
            accepts_suc_type = True
        if accepts_suc_type:
            self._listeners.append(listener)
        else:
            self._listeners.append(
                lambda job_id, success, ret_value, _suc_type: listener(job_id, success, ret_value)
            )

    def remove_job_finished_listener(self, listener: JobFinishedListener) -> None:
        """解绑组合根拥有的 listener（关停/测试重装时避免重复回调）。"""

        self._listeners = [item for item in self._listeners if item != listener]

    def _cancel_pending_error_decisions(self, job_ids: Set[str]) -> None:
        """取消 job 时由微后端原子消费 pending，并留下幂等审计。"""

        resolved: List[Dict[str, Any]] = []
        now = time.time()
        with self._pending_action_error_decisions_lock:
            for decision_id, pending in list(
                self._pending_action_error_decisions.items()
            ):
                if pending.get("job_id") not in job_ids:
                    continue
                self._pending_action_error_decisions.pop(decision_id, None)
                item = pending["item"]
                report = {
                    "decision_id": decision_id,
                    "job_id": pending["job_id"],
                    "task_id": item.task_id,
                    "node_id": str(getattr(item, "node_id", "") or ""),
                    "device_id": item.device_id,
                    "action_name": item.action_name,
                    "selected_action": "abort",
                    "reason": "job_canceled",
                    "resolved_at": now,
                }
                self._resolved_action_error_decisions[decision_id] = {
                    "report": deepcopy(report),
                    "retain_until": (
                        now + self._ACTION_ERROR_DECISION_TOMBSTONE_TTL_SECONDS
                    ),
                }
                resolved.append(report)
        if self._monitor is not None:
            for report in resolved:
                try:
                    self._monitor.emit(
                        "action", "job_error_decision_resolved", report
                    )
                except Exception:  # noqa: BLE001 - 观测不能阻断取消
                    logger.exception(
                        "[JobExecutionBackend] failed to emit canceled decision"
                    )

    def cancel_job(self, job_id: str) -> bool:
        """Cancel one microbackend-owned job through its active adapter."""

        job = self.device_manager.get_job_info(job_id)
        if job is None:
            return False
        adapter = self._host_node_getter()
        if adapter is not None:
            try:
                adapter.cancel_goal(job_id)
            except Exception:  # noqa: BLE001 - local state still must converge
                logger.exception(
                    "[JobExecutionBackend] cancel goal failed for %s", job_id
                )
        self._cancel_pending_error_decisions({job_id})
        success, next_job, _freed = self.device_manager.cancel_job(job_id)
        if not success:
            return False
        self._release_material_locks(job_id)
        item = QueueItem(
            task_type="job_call_back_status",
            device_id=job.device_id,
            action_name=job.action_name,
            task_id=job.task_id,
            job_id=job.job_id,
            notebook_id=job.notebook_id,
            device_action_key=job.device_action_key,
            node_id=job.node_id,
            retry_count=job.retry_count,
        )
        return_info = serialize_result_info("Job was cancelled", False, {})
        self._publish_to_result_bridges({}, item, "canceled", return_info)
        for listener in self._listeners:
            try:
                listener(job_id, False, None, "normal")
            except Exception:  # noqa: BLE001 - cancellation must continue
                logger.exception(
                    "[JobExecutionBackend] cancellation listener failed"
                )
        if next_job is not None:
            self._request_material_locks_or_wait(next_job)
        return True

    def cancel_task(self, task_id: str) -> List[str]:
        """取消本 backend 管理的整张任务，并继续启动其他任务被提升的 job。"""

        jobs = [
            job
            for job in self.device_manager.get_active_jobs()
            + self.device_manager.get_queued_jobs()
            if job.task_id == task_id
        ]
        jobs_by_id = {job.job_id: job for job in jobs}
        adapter = self._host_node_getter()
        if adapter is not None:
            for job in jobs:
                try:
                    adapter.cancel_goal(job.job_id)
                except Exception:  # noqa: BLE001 - local state still converges
                    logger.exception(
                        "[JobExecutionBackend] cancel goal failed for %s",
                        job.job_id,
                    )
        self._cancel_pending_error_decisions(set(jobs_by_id))
        cancelled, next_jobs, _freed = self.device_manager.cancel_jobs_by_task_id(
            task_id
        )
        for job_id in cancelled:
            self._release_material_locks(job_id)
            job = jobs_by_id.get(job_id)
            if job is None:
                continue
            item = QueueItem(
                task_type="job_call_back_status",
                device_id=job.device_id,
                action_name=job.action_name,
                task_id=job.task_id,
                job_id=job.job_id,
                notebook_id=job.notebook_id,
                device_action_key=job.device_action_key,
                node_id=job.node_id,
                retry_count=job.retry_count,
            )
            self._publish_to_result_bridges(
                {},
                item,
                "canceled",
                serialize_result_info("Job was cancelled", False, {}),
            )
            for listener in self._listeners:
                try:
                    listener(job_id, False, None, "normal")
                except Exception:  # noqa: BLE001 - cancellation must continue
                    logger.exception(
                        "[JobExecutionBackend] cancellation listener failed"
                    )
        for next_job in next_jobs:
            self._request_material_locks_or_wait(next_job)
        with self._status_held_lock:
            held = [
                job
                for job in self._status_held_jobs.values()
                if job.task_id == task_id
            ]
            for job in held:
                self._status_held_jobs.pop(job.job_id, None)
        for job in held:
            self._release_material_locks(job.job_id)
        return cancelled + [job.job_id for job in held]

    def busy_device_action_keys(self) -> Set[str]:
        """当前被占用的 device_action_key（供调度器做锁视图合并）。"""
        busy: Set[str] = set()
        for job in self.device_manager.get_active_jobs():
            busy.add(job.device_action_key)
        for job in self.device_manager.get_queued_jobs():
            busy.add(job.device_action_key)
        with self._status_held_lock:
            busy.update(job.device_action_key for job in self._status_held_jobs.values())
        return busy

    # ── 执行适配器侧接口（bridge 形状，duck-typing） ──────────

    def publish_job_status(
        self,
        feedback_data: dict,
        item: QueueItem,
        status: str,
        return_info: Optional[dict] = None,
    ) -> None:
        """Receive a raw adapter result and advance the canonical job lifecycle."""

        if self.device_manager.get_job_info(item.job_id) is None:
            return

        if status == "running":
            self._publish_to_result_bridges(feedback_data, item, status, return_info)
            return
        if status not in ("success", "failed", "canceled"):
            return

        # 设备已经结束对物料的访问；审批只决定后续调度，不继续占用执行锁。
        self._release_material_locks(item.job_id)

        normalized_return_info = (
            dict(return_info) if isinstance(return_info, dict) else {}
        )
        if status == "failed":
            normalized_return_info.setdefault(
                "error_info",
                {
                    "action_name": item.action_name,
                    "exception_type": "DeviceActionError",
                    "exception_mro": ["DeviceActionError", "Exception"],
                    "error_message": str(
                        normalized_return_info.get("error")
                        or "device action failed"
                    ),
                    "category": "execution",
                    "severity": "error",
                },
            )
            if self._begin_action_error_decision(
                item,
                normalized_return_info,
                dict(feedback_data or {}),
            ):
                return
        self._release_terminal(
            item,
            status,
            normalized_return_info,
            dict(feedback_data or {}),
        )

    def publish_job_started(self, item: QueueItem) -> None:
        """Forward an adapter acknowledgement without transferring ownership."""

        for bridge in self.result_bridges:
            callback = getattr(bridge, "publish_job_started", None)
            if callable(callback):
                try:
                    callback(item)
                except Exception:  # noqa: BLE001 - 回报失败不能重复执行 action
                    logger.exception(
                        "[JobExecutionBackend] failed to publish job started"
                    )

    def publish_host_ready(self) -> None:
        """HostLink/ROS2 adapter ready 后恢复数据库中的未下发 attempt。"""

        for bridge in self.result_bridges:
            callback = getattr(bridge, "resume_pending_dispatches", None)
            if callable(callback):
                try:
                    callback()
                except Exception:  # noqa: BLE001 - 后续重连仍可恢复
                    logger.exception(
                        "[JobExecutionBackend] failed to resume pending dispatches"
                    )

    def _publish_to_result_bridges(
        self,
        feedback_data: Dict[str, Any],
        item: QueueItem,
        status: str,
        return_info: Optional[Dict[str, Any]] = None,
    ) -> None:
        for bridge in self.result_bridges:
            callback = getattr(bridge, "publish_job_status", None)
            if callable(callback):
                try:
                    callback(feedback_data, item, status, return_info)
                except Exception:  # noqa: BLE001 - lifecycle must still converge
                    logger.exception(
                        "[JobExecutionBackend] failed to publish job status %s",
                        status,
                    )

    def _release_terminal(
        self,
        item: QueueItem,
        status: str,
        return_info: Dict[str, Any],
        result_data: Dict[str, Any],
    ) -> None:
        """Publish exactly the terminal result released by the microbackend."""

        try:
            from unilabos.app.web.controller import store_job_result

            store_job_result(item.job_id, status, return_info, result_data)
        except (ImportError, RuntimeError):
            pass
        except Exception:  # noqa: BLE001 - persistence must not block reporting
            logger.exception(
                "[JobExecutionBackend] failed to store terminal result for %s",
                item.job_id,
            )

        self._publish_to_result_bridges(result_data, item, status, return_info)

        ret_value = None
        suc_type = "normal"
        ret_value = return_info.get("return_value")
        suc_type = str(return_info.get("suc_type") or "normal")
        parent = extract_trace_context(getattr(item, "trace_context", {}))
        self._put_event(
            ("finished", item.job_id, status == "success", ret_value, suc_type),
            context=parent,
        )

    def _begin_action_error_decision(
        self,
        item: QueueItem,
        return_info: Dict[str, Any],
        result_data: Dict[str, Any],
    ) -> bool:
        """Hold a failed attempt until Backend has updated scheduling and releases it."""

        raw_error_info = return_info.get("error_info")
        if not isinstance(raw_error_info, dict):
            return False
        adapter = self._host_node_getter()
        mappings = getattr(adapter, "_action_value_mappings", {}) if adapter else {}
        action_mappings = mappings.get(item.device_id, {}) if isinstance(mappings, dict) else {}
        report_action_name = str(
            raw_error_info.get("action_name") or item.action_name
        )
        candidates = [report_action_name, item.action_name]
        candidates.extend(
            f"auto-{candidate}"
            for candidate in list(candidates)
            if not candidate.startswith("auto-")
        )
        policy: Optional[Mapping[str, Any]] = None
        for candidate in candidates:
            mapping = action_mappings.get(candidate)
            configured_policy = (
                mapping.get("error_policy") if isinstance(mapping, dict) else None
            )
            if isinstance(configured_policy, Mapping) and configured_policy:
                policy = configured_policy
                break
        exception_mro = raw_error_info.get("exception_mro")
        if not isinstance(exception_mro, list):
            exception_mro = [
                str(raw_error_info.get("exception_type") or "Exception")
            ]
        if policy is None:
            # 所有设备失败都走同一条 Backend 决策链；具体 retry 由 Backend
            # 创建新的 attempt，本地只放行原 attempt 的 failed 或人工替换。
            options = [
                {
                    "action": "retry",
                    "label": "重试",
                    "description": "Backend 更新调度并创建新的执行 attempt",
                },
                {
                    "action": "abort",
                    "label": "标记失败",
                    "description": "放行当前 attempt 的 failed 结果",
                },
                {
                    "action": "operator_intervention",
                    "label": "人工替换结果",
                    "description": "由人工提供当前 attempt 的有效结果",
                },
            ]
            policy = {}
        else:
            options = resolve_error_options_by_names(policy, exception_mro)
            if not options:
                return False
        decision_bridges = [
            bridge
            for bridge in self.result_bridges
            if (
                callable(getattr(bridge, "publish_job_error_pending", None))
                or callable(
                    getattr(bridge, "publish_job_error_decision_required", None)
                )
            )
        ]
        if not decision_bridges:
            return False

        retry_count = int(getattr(item, "retry_count", 0) or 0)
        max_retries = int(policy.get("max_retries", 3))
        timeout_seconds = float(policy.get("decision_timeout_seconds", 300.0))
        timeout_action = str(policy.get("default_on_decision_timeout", "abort"))
        if timeout_action != "abort" and timeout_action not in {
            str(option.get("action")) for option in options
        }:
            timeout_action = "abort"
        created_at = time.time()
        decision_id = str(uuid.uuid4())
        error_info = {
            **raw_error_info,
            "options": options,
            "max_retries": max_retries,
            "decision_timeout_seconds": timeout_seconds,
            "default_on_decision_timeout": timeout_action,
            "expires_at": created_at + timeout_seconds,
        }
        report: Dict[str, Any] = {
            "decision_id": decision_id,
            "device_id": item.device_id,
            "action_name": report_action_name,
            "task_id": item.task_id,
            "job_id": item.job_id,
            "node_id": str(getattr(item, "node_id", "") or ""),
            "exception_type": error_info.get("exception_type", "Exception"),
            "error_message": error_info.get(
                "error_message", return_info.get("error", "")
            ),
            "traceback": error_info.get(
                "traceback", return_info.get("error", "")
            ),
            "options": options,
            "retry_count": retry_count,
            "max_retries": max_retries,
            "created_at": created_at,
            "decision_timeout_seconds": timeout_seconds,
            "expires_at": error_info["expires_at"],
            "default_on_decision_timeout": timeout_action,
            "require_confirmation": True,
        }
        for key in ("category", "severity"):
            if error_info.get(key) is not None:
                report[key] = error_info[key]
        pending = {
            "decision_id": decision_id,
            "job_id": item.job_id,
            "item": item,
            "return_info": deepcopy(return_info),
            "result_data": deepcopy(result_data),
            "error_info": error_info,
            "report": report,
            "resolving": False,
            # 微后端只向 Backend 暴露截止时间，不在本地擅自执行超时策略。
            "timer": None,
        }
        with self._pending_action_error_decisions_lock:
            self._pending_action_error_decisions[decision_id] = pending

        for bridge in decision_bridges:
            try:
                rich_callback = getattr(bridge, "publish_job_error_pending", None)
                if callable(rich_callback):
                    rich_callback(
                        deepcopy(report),
                        item,
                        deepcopy(return_info),
                        deepcopy(result_data),
                        deepcopy(error_info),
                    )
                else:
                    bridge.publish_job_error_decision_required(deepcopy(report))
            except Exception:  # noqa: BLE001 - reconnect replay keeps it pending
                logger.exception(
                    "[JobExecutionBackend] failed to publish error decision %s",
                    decision_id,
                )
        if self._monitor is not None:
            try:
                self._monitor.emit(
                    "action", "job_error_decision_required", report
                )
            except Exception:  # noqa: BLE001 - pending 仍可通过查询/重连恢复
                logger.exception(
                    "[JobExecutionBackend] failed to emit required decision"
                )
        return True

    # ── 设备状态桥（bridge 形状：publish_device_status） ──────

    def publish_device_status(self, device_status: dict, device_id: str, property_name: str) -> None:
        """HostNode 设备属性更新入口（值变化时被调，与 ws_client 同形状）。

        ROS 回调线程里只做入队，SQLite 写入由 worker 串行执行。
        """
        if self.device_state is None and self.status_incidents is None:
            return
        value = device_status.get(device_id, {}).get(property_name)
        if not isinstance(value, (bool, int, float, str)):
            return  # 与 HostNode.property_callback 的标量过滤口径一致
        self._put_event(("device_status", device_id, property_name, value))

    def report_device_properties(self, device_id: str, properties: Dict[str, Any]) -> Dict[str, bool]:
        """直接上报入口（REST / 非 ROS 设备）：同步写入并发监控事件。"""
        if self.device_state is None and self.status_incidents is None:
            raise RuntimeError("device state and status incident services not enabled")
        results: Dict[str, bool] = {}
        for prop, value in properties.items():
            results[prop] = self._write_device_property(device_id, prop, value)
        return results

    def _write_device_property(self, device_id: str, prop: str, value: Any) -> bool:
        changed = self.device_state.set(device_id, prop, value) if self.device_state is not None else False
        if changed and self._monitor is not None:
            try:
                self._monitor.emit(
                    "device",
                    "device_property",
                    {"device_id": device_id, "property": prop, "value": value},
                )
            except Exception:  # noqa: BLE001 - 监控故障不影响状态落盘
                pass
        self._observe_status_policy(device_id, prop, value)
        return changed

    def _observe_status_policy(
        self,
        device_id: str,
        prop: str,
        value: Any,
        *,
        now: Optional[float] = None,
    ) -> bool:
        """在调度权威侧求值；损坏的显式策略按设备级 fail-closed 处理。"""

        if self.status_incidents is None or self._status_policy_resolver is None:
            return False
        try:
            policy = self._status_policy_resolver(device_id, prop)
            if not policy:
                return False
            self.status_incidents.observe(
                device_id,
                prop,
                value,
                policy,
                now=now,
            )
        except (TypeError, ValueError) as exc:
            logger.error(
                "[JobExecutionBackend] invalid status policy for %s/%s: %s",
                device_id,
                prop,
                exc,
            )
            self.status_incidents.observe(
                device_id,
                prop,
                value,
                {
                    "unknown_incident": {
                        "code": "unilabos.status_policy.invalid",
                        "severity": "critical",
                        "message": (
                            f"设备 {device_id} 的状态 {prop} 策略无效；"
                            "已暂停该设备的新调度，请修复注册表配置"
                        ),
                        "hold": True,
                    }
                },
                now=now,
            )
        except Exception:  # noqa: BLE001 - 状态线程必须继续消费后续消息
            logger.exception(
                "[JobExecutionBackend] status policy evaluation failed for %s/%s",
                device_id,
                prop,
            )
            return False
        return True

    def rebuild_status_incidents(self) -> int:
        """Re-evaluate persisted latest values after process restart."""

        if (
            self.device_state is None
            or self.status_incidents is None
            or self._status_policy_resolver is None
        ):
            return 0
        observed = 0
        for device_id, properties in self.device_state.latest_all().items():
            for prop, item in properties.items():
                if self._observe_status_policy(
                    device_id,
                    prop,
                    item["value"],
                    now=float(item["updated_at"]) / 1000.0,
                ):
                    observed += 1
        return observed

    def _on_status_incident_event(self, event: Dict[str, Any]) -> None:
        """Release held canonical jobs only after the device has recovered."""

        incident = event.get("incident") or {}
        device_id = str(incident.get("device_id") or "")
        if (
            not device_id
            or event.get("type")
            not in {"status_incident_resolved", "status_incident_cleared"}
            or self.status_incidents.is_device_held(device_id)
        ):
            return
        with self._status_held_lock:
            jobs = [
                job
                for job in self._status_held_jobs.values()
                if job.device_id == device_id
            ]
            for job in jobs:
                self._status_held_jobs.pop(job.job_id, None)
        for job in jobs:
            self._enqueue_job(job)

    # ── 微后端异常决策权威 ──────────────────────────────────

    def list_error_decisions(self) -> List[Dict[str, Any]]:
        """Return failures held by this microbackend, not by an executor."""

        with self._pending_action_error_decisions_lock:
            return [
                deepcopy(pending["report"])
                for pending in self._pending_action_error_decisions.values()
            ]

    def get_pending_action_error_decisions(self) -> List[Dict[str, Any]]:
        """Compatibility name used by reconnect/reporting paths."""

        return self.list_error_decisions()

    def restore_action_error_decision(self, snapshot: Dict[str, Any]) -> bool:
        """从 history.db 恢复重启前尚未由 Backend 放行的失败。"""

        report = snapshot.get("report")
        item_data = snapshot.get("item")
        return_info = snapshot.get("return_info")
        result_data = snapshot.get("result_data")
        error_info = snapshot.get("error_info")
        if not all(
            isinstance(value, dict)
            for value in (report, item_data, return_info, result_data, error_info)
        ):
            return False
        decision_id = str(report.get("decision_id") or "")
        job_id = str(report.get("job_id") or "")
        if not decision_id or not job_id:
            return False
        item_fields = {
            name: item_data[name]
            for name in QueueItem.__dataclass_fields__
            if name in item_data
        }
        try:
            item = QueueItem(**item_fields)
        except (TypeError, ValueError):
            return False
        with self._pending_action_error_decisions_lock:
            existing = self._pending_action_error_decisions.get(decision_id)
            if existing is not None:
                return existing.get("job_id") == job_id
            self._pending_action_error_decisions[decision_id] = {
                "decision_id": decision_id,
                "job_id": job_id,
                "item": item,
                "return_info": deepcopy(return_info),
                "result_data": deepcopy(result_data),
                "error_info": deepcopy(error_info),
                "report": deepcopy(report),
                "resolving": False,
                "timer": None,
            }
        return True

    def host_ready(self) -> bool:
        """Whether a transport adapter is ready to execute commands."""

        return self._host_node_getter() is not None

    def resolve_error_decision(self, decision_id: str, decision: Dict[str, Any]) -> bool:
        """Resolve a held failure after Backend confirms scheduler update."""

        pending = next(
            (
                item
                for item in self.list_error_decisions()
                if str(item.get("decision_id") or "") == decision_id
            ),
            None,
        )
        if pending is None:
            return False
        payload = {
            "decision_id": decision_id,
            "job_id": str(pending.get("job_id") or ""),
            "device_id": str(pending.get("device_id") or ""),
            **decision,
        }
        return self.handle_action_error_decision(
            decision_id,
            payload["job_id"],
            payload,
        )

    def get_resolved_action_error_decision(
        self,
        decision_id: str,
        job_id: str,
        device_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Return a short-lived idempotency tombstone for repeated releases."""

        now = time.time()
        with self._pending_action_error_decisions_lock:
            stale = [
                key
                for key, value in self._resolved_action_error_decisions.items()
                if float(value.get("retain_until", 0.0)) <= now
            ]
            for key in stale:
                self._resolved_action_error_decisions.pop(key, None)
            tombstone = self._resolved_action_error_decisions.get(decision_id)
            if tombstone is None:
                return None
            report = tombstone.get("report")
            if not isinstance(report, dict):
                return None
            if report.get("job_id") != job_id or report.get("device_id") != device_id:
                return None
            return deepcopy(report)

    def handle_action_error_decision(
        self,
        decision_id: str,
        job_id: str,
        decision: Dict[str, Any],
    ) -> bool:
        """Release a failure; only operator intervention may replace its result."""

        device_id = str(decision.get("device_id") or "")
        if (
            not decision_id
            or str(decision.get("decision_id") or "") != decision_id
            or not job_id
            or str(decision.get("job_id") or "") != job_id
            or not device_id
            or decision.get("scheduler_updated") is not True
        ):
            return False

        with self._pending_action_error_decisions_lock:
            pending = self._pending_action_error_decisions.get(decision_id)
            if (
                pending is None
                or pending.get("resolving")
                or pending.get("job_id") != job_id
                or pending["item"].device_id != device_id
            ):
                return False
            selected_option = decision.get("option")
            if isinstance(selected_option, dict):
                selected = str(selected_option.get("action") or "abort")
                for key in ("result", "return_value"):
                    if key not in decision and key in selected_option:
                        decision[key] = selected_option[key]
            else:
                selected = str(
                    decision.get("action") or selected_option or "abort"
                )
            if selected not in {
                str(option.get("action"))
                for option in pending["error_info"]["options"]
            }:
                return False
            pending["resolving"] = True
            self._pending_action_error_decisions.pop(decision_id, None)
            resolved_report = {
                "decision_id": decision_id,
                "job_id": job_id,
                "task_id": pending["item"].task_id,
                "node_id": str(getattr(pending["item"], "node_id", "") or ""),
                "device_id": device_id,
                "action_name": pending["item"].action_name,
                "selected_action": selected,
                "reason": str(decision.get("reason") or ""),
                "resolved_at": time.time(),
            }
            self._resolved_action_error_decisions[decision_id] = {
                "report": deepcopy(resolved_report),
                "retain_until": (
                    time.time() + self._ACTION_ERROR_DECISION_TOMBSTONE_TTL_SECONDS
                ),
            }

        if self._monitor is not None:
            try:
                self._monitor.emit(
                    "action", "job_error_decision_resolved", resolved_report
                )
            except Exception:  # noqa: BLE001 - 观测不能阻断 failure release
                logger.exception(
                    "[JobExecutionBackend] failed to emit resolved decision"
                )
        item = pending["item"]
        if selected == "operator_intervention" and (
            "result" in decision or "return_value" in decision
        ):
            return_value = decision.get("result", decision.get("return_value"))
            return_info = serialize_result_info(
                "",
                True,
                return_value,
                suc_type=SUCCESS_TYPE_OPERATOR_INTERVENTION,
            )
            return_info["error_resolution"] = {
                "decision_id": decision_id,
                "selected_action": selected,
                "reason": str(decision.get("reason") or ""),
                "scheduler_updated": True,
            }
            result_data = deepcopy(pending["result_data"])
            result_data["raw_return_info"] = deepcopy(pending["return_info"])
            if "return_info" in result_data:
                result_data["return_info"] = json.dumps(
                    return_info, ensure_ascii=False
                )
            self._release_terminal(item, "success", return_info, result_data)
            return True

        return_info = deepcopy(pending["return_info"])
        return_info["error_resolution"] = {
            "decision_id": decision_id,
            "selected_action": selected,
            "reason": str(decision.get("reason") or ""),
            "scheduler_updated": True,
        }
        result_data = deepcopy(pending["result_data"])
        if "return_info" in result_data:
            result_data["return_info"] = json.dumps(
                return_info, ensure_ascii=False
            )
        self._release_terminal(item, "failed", return_info, result_data)
        return True

    # ── worker ───────────────────────────────────────────────

    def _run(self) -> None:
        while self._running:
            event_context, event = self._events.get()
            if event[0] == "__stop__":
                break
            try:
                with use_context(event_context):
                    with span(
                        "action.worker",
                        attributes={"action.worker.event": event[0]},
                    ):
                        if event[0] == "start":
                            self._start_goal(event[1])
                        elif event[0] == "finished":
                            suc_type = event[4] if len(event) > 4 else "normal"
                            self._handle_finished(event[1], event[2], event[3], suc_type)
                        elif event[0] == "device_status":
                            self._write_device_property(event[1], event[2], event[3])
            except Exception:  # noqa: BLE001 - worker 不允许死
                logger.exception("[JobExecutionBackend] event %s failed", event[0])
            finally:
                with self._pending_lock:
                    self._pending -= 1

    def _start_goal(self, job: JobInfo) -> None:
        if self.device_manager.get_job_info(job.job_id) is None:
            self._release_material_locks(job.job_id)
            return
        job_log = format_job_log(job.job_id, job.task_id, job.device_id, job.action_name)
        queue_item = QueueItem(
            task_type="job_call_back_status",
            device_id=job.device_id,
            action_name=job.action_name,
            task_id=job.task_id,
            job_id=job.job_id,
            notebook_id=job.notebook_id,
            device_action_key=job.device_action_key,
            node_id=job.node_id,
            retry_count=job.retry_count,
        )
        # QueueItem 不是 wire schema；动态附加只读追踪上下文供回调恢复。
        queue_item.trace_context = {}
        inject_trace_context(queue_item.trace_context)
        adapter = self._host_node_getter()
        if adapter is None:
            logger.error(
                "[JobExecutionBackend] execution adapter unavailable for job %s",
                job_log,
            )
            return_info = serialize_result_info(
                "Device execution adapter is not available", False, {}
            )
            return_info["error_info"] = {
                "action_name": job.action_name,
                "exception_type": "ExecutionAdapterUnavailable",
                "exception_mro": ["ExecutionAdapterUnavailable", "Exception"],
                "error_message": "Device execution adapter is not available",
                "category": "transport",
                "severity": "fatal",
            }
            self._release_material_locks(job.job_id)
            if not self._begin_action_error_decision(
                queue_item,
                return_info,
                {},
            ):
                self._release_terminal(queue_item, "failed", return_info, {})
            return
        try:
            adapter.send_goal(
                queue_item,
                action_type=job.action_type,
                action_kwargs=job.action_args,
                sample_material=job.sample_material,
                server_info=job.server_info,
            )
            self.publish_job_started(queue_item)
            logger.info("[JobExecutionBackend] goal sent for job %s", job_log)
        except Exception:  # noqa: BLE001 - 启动失败必须走完结流程释放锁
            logger.exception("[JobExecutionBackend] send_goal failed for job %s", job_log)
            return_info = serialize_result_info(
                "Failed to dispatch action to device adapter", False, {}
            )
            return_info["error_info"] = {
                "action_name": job.action_name,
                "exception_type": "ExecutionDispatchError",
                "exception_mro": ["ExecutionDispatchError", "Exception"],
                "error_message": "Failed to dispatch action to device adapter",
                "category": "transport",
                "severity": "fatal",
            }
            self._release_material_locks(job.job_id)
            if not self._begin_action_error_decision(
                queue_item, return_info, {}
            ):
                self._release_terminal(queue_item, "failed", return_info, {})

    def _handle_finished(
        self, job_id: str, success: bool, ret_value: Any, suc_type: str = "normal"
    ) -> None:
        finished_job = self.device_manager.get_job_info(job_id)
        # 只有显式本地 Scheduler 模式才可能存在被提升的等待 job。
        next_job = None
        if finished_job is not None:
            next_job, _lock_became_free = self.device_manager.end_job(job_id)
        if next_job is not None:
            self._request_material_locks_or_wait(next_job)

        add_event(
            "action.finished",
            {
                "workflow.job.uuid": job_id,
                "device.name": getattr(finished_job, "device_id", ""),
                "action.name": getattr(finished_job, "action_name", ""),
                "action.success": success,
                "action.success.type": suc_type,
            },
        )

        for listener in self._listeners:
            try:
                listener(job_id, success, ret_value, suc_type)
            except Exception:  # noqa: BLE001 - 单个 listener 异常不阻断其他
                logger.exception("[JobExecutionBackend] job finished listener failed")
        self._release_material_locks(job_id)

    @staticmethod
    def _default_host_getter() -> Any:
        from unilabos.app.execution_adapter import get_execution_adapter

        return get_execution_adapter(0)


def make_device_materials_need_lock_resolver(
    host_node_getter: Optional[Callable[[], Any]] = None,
) -> Callable[[str, str], List[str]]:
    """读取 ``@action(materials_need_lock=[...])`` 的参数名声明。

    查找顺序（对齐「Slave 与 Host 同注册表副本」机制）：

    1. HostNode._action_value_mappings[device_id] —— Host 侧权威副本，
       覆盖本地设备（装配时写入）与 **slave 远端设备**（main_slave_run /
       SYNC_SLAVE_NODE_INFO 上报 registry_config 时写入）；
    2. 本地设备实例 _ros_node._action_value_mappings —— Host 副本尚未
       建立时（如设备刚创建）的回退。
    """
    getter = host_node_getter or JobExecutionBackend._default_host_getter

    def _lock_from(mappings: Any, action_name: str) -> Optional[List[str]]:
        if not isinstance(mappings, dict):
            return None
        mapping = mappings.get(action_name) or mappings.get(f"auto-{action_name}")
        if not isinstance(mapping, dict):
            return None
        return normalize_material_parameter_names(
            mapping.get("materials_need_lock")
        )

    def resolve(device_id: str, action_name: str) -> List[str]:
        host_node = getter()
        if host_node is None:
            return []
        # ① Host 权威副本（含 slave 设备的注册表镜像）
        host_mappings = getattr(host_node, "_action_value_mappings", None) or {}
        found = _lock_from(host_mappings.get(device_id), action_name)
        if found is not None:
            return found
        # ② 本地设备实例回退
        wrapper = getattr(host_node, "devices_instances", {}).get(device_id)
        base_node = getattr(wrapper, "_ros_node", None) if wrapper is not None else None
        found = _lock_from(getattr(base_node, "_action_value_mappings", None), action_name)
        return found if found is not None else []

    return resolve


def make_device_status_policy_resolver(
    host_node_getter: Optional[Callable[[], Any]] = None,
) -> Callable[[str, str], Optional[Dict[str, Any]]]:
    """Resolve ``@topic_config(status_policy=...)`` for local or mirrored devices."""

    from unilabos.registry.status_policy import normalize_status_policy

    getter = host_node_getter or JobExecutionBackend._default_host_getter

    def _from_class(driver_class: Any, property_name: str) -> Optional[Dict[str, Any]]:
        from unilabos.registry.decorators import get_topic_config

        if not isinstance(driver_class, type):
            return None
        for base in driver_class.__mro__:
            for method_name, candidate in vars(base).items():
                if isinstance(candidate, property):
                    config = get_topic_config(candidate.fget) if candidate.fget else {}
                elif callable(candidate):
                    config = get_topic_config(candidate)
                else:
                    continue
                default_name = method_name[4:] if method_name.startswith("get_") else method_name
                if (config.get("name") or default_name) == property_name:
                    return normalize_status_policy(config.get("status_policy"))
        return None

    def _registry_name(host_node: Any, device_id: str) -> str:
        wrapper = getattr(host_node, "devices_instances", {}).get(device_id)
        device_config = getattr(wrapper, "device_config", None)
        content = getattr(device_config, "res_content", None)
        name = getattr(content, "klass", "")
        if isinstance(name, str) and name:
            return name
        for node in getattr(getattr(host_node, "devices_config", None), "all_nodes", []):
            content = getattr(node, "res_content", None)
            if getattr(content, "id", None) == device_id:
                value = getattr(content, "klass", "")
                return value if isinstance(value, str) else ""
        return ""

    def resolve(device_id: str, property_name: str) -> Optional[Dict[str, Any]]:
        host_node = getter()
        if host_node is None:
            return None
        wrapper = getattr(host_node, "devices_instances", {}).get(device_id)
        policy = _from_class(getattr(wrapper, "_driver_class", None), property_name)
        if policy:
            return policy

        registry_name = _registry_name(host_node, device_id)
        if not registry_name:
            return None
        from unilabos.registry.registry import lab_registry

        # 镜像设备以 slave 随注册表同步过来的版本为准；本地注册表仅作回退。
        entries = (
            getattr(host_node, "_slave_registry_configs", {}).get(registry_name, {}),
            lab_registry.device_type_registry.get(registry_name, {}),
        )
        for entry in entries:
            policies = entry.get("class", {}).get("status_policies", {})
            if not isinstance(policies, dict) or property_name not in policies:
                continue
            return normalize_status_policy(policies[property_name])
        return None

    return resolve


def create_edge_stack(
    orderer: Any = None,
    device_manager: Optional[DeviceActionManager] = None,
    host_node_getter: Optional[Callable[[], Any]] = None,
    inventory: Any = None,
    estimator: Any = None,
    monitor: Any = None,
    device_state_store: Any = None,
    history: Any = None,
    status_incidents: Any = None,
    result_bridges: Optional[List[Any]] = None,
) -> "tuple[Any, JobExecutionBackend]":
    """组装 EdgeScheduler + 微后端（composition root）。

    返回 (scheduler, backend)；backend 已 start，并需由调用方注册进
    执行适配器 bridges（或在测试中手动回调 ``publish_job_status``）。
    ``inventory`` 传入 InventoryService 时启用物料预留/消费衔接。
    物料锁 resolver 默认接 action_value_mappings 的 materials_need_lock 声明。
    ``estimator`` 传入 DurationEstimator 时用于泳道图预估（与 orderer 共享）。
    ``monitor`` 传入 MonitorBus 时向实时监控面板推事件。
    ``device_state_store`` 传入 DeviceStateStore 时启用设备状态落盘
    （publish_device_status bridge + REST 上报，独立 SQLite）。
    ``history`` 传入 WorkflowHistoryStore 时持久化工作流/job 执行历史
    （第三个独立 SQLite）。
    """
    from unilabos.server.scheduler.service import EdgeScheduler

    if status_incidents is None:
        from unilabos.server.scheduler.status_incidents import StatusIncidentManager

        status_incidents = StatusIncidentManager(monitor=monitor)
    status_policy_resolver = make_device_status_policy_resolver(host_node_getter)
    backend = JobExecutionBackend(
        device_manager=device_manager,
        host_node_getter=host_node_getter,
        device_state_store=device_state_store,
        monitor=monitor,
        status_policy_resolver=status_policy_resolver,
        status_incidents=status_incidents,
        result_bridges=result_bridges,
        queue_conflicts=True,
        materials_need_lock_resolver=make_device_materials_need_lock_resolver(
            host_node_getter
        ),
    )
    scheduler = EdgeScheduler(
        orderer=orderer,
        dispatcher=backend,
        busy_key_provider=backend.busy_device_action_keys,
        inventory=inventory,
        materials_need_lock_resolver=make_device_materials_need_lock_resolver(
            host_node_getter
        ),
        estimator=estimator,
        monitor=monitor,
        history=history,
        held_device_provider=status_incidents.held_device_ids,
    )
    status_incidents.add_listener(lambda _event: scheduler.reschedule())
    backend.add_job_finished_listener(scheduler.on_job_finished)
    backend.start()
    backend.rebuild_status_incidents()
    return scheduler, backend


__all__ = [
    "JobExecutionBackend",
    "JobFinishedListener",
    "create_edge_stack",
    "make_device_materials_need_lock_resolver",
    "make_device_status_policy_resolver",
]
