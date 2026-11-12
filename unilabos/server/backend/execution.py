"""Backend Job 到 HostLink/ROS2 动作的执行适配层。

调度、资源等待和库存预占都属于 ``server.backend.scheduler``。本模块只接受已经
获准执行的 Job：动作冲突直接拒绝，库存仅校验既有 reservation 并在驱动调用前
幂等消费，不保存本地待调度队列。
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

from unilabos.server.backend.execution_queue import (
    DeviceActionManager,
    JobInfo,
    JobStatus,
    QueueItem,
    format_job_log,
)
from unilabos.server.backend.scheduler.payloads import DispatchPayload
from unilabos.server.backend.scheduler.materials import material_uuids_for_parameters
from unilabos.server.backend.inventory import (
    ExecutionInventoryCoordinator,
    ExecutionInventoryError,
)
from unilabos.backend.runtime.exception import (
    ExecutionTimeoutException,
    TimeoutException,
)
from unilabos.registry.action_policy import (
    SUCCESS_TYPE_CANCELLATION,
    SUCCESS_TYPE_OPERATOR_INTERVENTION,
    resolve_error_options_by_names,
)
from unilabos.registry.action_timeout import (
    TimeoutExpressionError,
    evaluate_execution_timeout,
    normalize_action_timeout,
    normalize_execution_timeout,
)
from unilabos.registry.material_locks import normalize_material_parameter_names
from unilabos.utils.serialization import serialize_result_info
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

# 执行面硬超时看门狗比声明值多等这么久：能自己执行 timeout 的运行时（HostLink 本地
# 运行时用 asyncio.wait_for）会先抛出带完整 traceback 的 TimeoutException，看门狗只兜底。
HARD_TIMEOUT_GRACE_SECONDS = 1.0


def _positive_seconds(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None

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
        materials_need_lock_resolver: Optional[
            Callable[[str, str], List[str]]
        ] = None,
        materials_gateway: Any = None,
    ):
        self.device_manager = device_manager or DeviceActionManager()
        self._host_node_getter = host_node_getter or self._default_host_getter
        self._listeners: List[JobFinishedListener] = []
        # 设备状态读取投影（None = 不启用状态联锁）与可选观测钩子
        self.device_state = device_state_store
        self._monitor = monitor
        self._status_policy_resolver = status_policy_resolver
        self.status_incidents = status_incidents
        self.result_bridges = [
            bridge for bridge in (result_bridges or []) if bridge is not self
        ]
        self._materials_need_lock_resolver = materials_need_lock_resolver
        self._inventory_authority = (
            ExecutionInventoryCoordinator(materials_gateway)
            if materials_gateway is not None
            else None
        )
        self._pending_action_error_decisions: Dict[str, Dict[str, Any]] = {}
        self._resolved_action_error_decisions: Dict[str, Dict[str, Any]] = {}
        self._pending_action_error_decisions_lock = threading.RLock()
        # @action(timeout / execution_timeout) 看门狗：job_id -> {"hard": Timer, "soft": Timer, ...}
        self._job_timeouts: Dict[str, Dict[str, Any]] = {}
        # 终态已由超时闸门决定的 job：之后设备迟到的 feedback 一律忽略
        self._superseded_jobs: Set[str] = set()
        # 超时闸门已取代、驱动尚未真实返回的 job（ADR 0008）：job_id -> 记录。终态放行时
        # 附 timeout_hold，调度权威据此结算 attempt 但继续持有锁；驱动真实返回后作为迟到结果
        # 转交 owner bridge（publish_job_late_outcome），由它释放锁。
        self._overrun_jobs: Dict[str, Dict[str, Any]] = {}
        # 已交给驱动执行的 job；对它们的取消只是请求，驱动真实返回后才收口（锁也随之释放）
        self._cancel_state_lock = threading.Lock()
        self._goal_sent_jobs: Set[str] = set()
        self._cancel_requested_jobs: Set[str] = set()
        self._timeout_lock = threading.RLock()
        self._events: "queue.Queue[tuple[Any, tuple]]" = queue.Queue()
        self._worker: Optional[threading.Thread] = None
        self._running = False
        self._execution_authority = None
        self._require_execution_permit = False
        from uuid import uuid4
        self._executor_epoch = str(uuid4())
        self._pending = 0
        self._pending_lock = threading.Lock()

    def configure_execution_authority(self, channel: Any) -> None:
        """本机直调或远端 HTTP 共用一次开始许可合同。"""
        self._execution_authority = channel
        self._require_execution_permit = True

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

    # ── Backend Scheduler 接口 ───────────────────────────────

    def dispatch(self, payload: DispatchPayload) -> None:
        """接收一个已获调度权威准入的 Job，并立即执行或明确拒绝。"""
        job_info = JobInfo(
            job_id=payload["job_id"],
            task_id=payload.get("task_id", ""),
            device_id=payload["device_id"],
            notebook_id=payload.get("notebook_id", "") or "",
            action_name=payload["action"],
            device_action_key=f"/devices/{payload['device_id']}/{payload['action']}",
            status=JobStatus.STARTED,
            start_time=time.time(),
            always_free=bool(payload.get("always_free", False)),
            action_type=payload.get("action_type", ""),
            action_args=payload.get("action_args", {}) or {},
            execution_payload=dict(payload),
            sample_material=payload.get("sample_material", {}) or {},
            server_info=payload.get("server_info"),
            node_id=payload.get("node_id", ""),
            node_run_uuid=str(payload.get("node_run_uuid", "") or ""),
            origin=str(payload.get("origin", "") or ""),
            retry_count=int(payload.get("retry_count", 0) or 0),
            timeout_seconds=_positive_seconds(payload.get("timeout_seconds")),
            execution_timeout_seconds=_positive_seconds(
                payload.get("execution_timeout_seconds")
            ),
        )
        if self.device_manager.get_job_info(job_info.job_id) is not None:
            self._enqueue_job(job_info)
            return
        try:
            parameter_names = normalize_material_parameter_names(
                payload.get("materials_need_lock")
            )
            # The wire copy is useful for durable replay, but it may never
            # weaken the local registry contract.  Union both declarations so
            # a stale Backend cannot accidentally dispatch an action unlocked.
            parameter_names.extend(
                normalize_material_parameter_names(
                    self.resolve_material_lock_parameters(
                        job_info.device_id,
                        job_info.action_name,
                    )
                )
            )
            material_uuids_for_parameters(parameter_names, job_info.action_args)
        except (TypeError, ValueError) as exc:
            self._reject_job(
                job_info,
                str(exc),
                "MaterialLockResolutionError",
            )
            return
        if self._inventory_authority is not None:
            try:
                self._inventory_authority.prepare(payload)
            except ExecutionInventoryError as exc:
                self._reject_job(
                    job_info,
                    str(exc),
                    "InventoryReservationError",
                )
                return
        elif payload.get("inventory_requirements") or payload.get(
            "inventory_reservation_uuid"
        ):
            self._reject_job(
                job_info,
                "materials authority is unavailable for inventory requirements",
                "InventoryAuthorityUnavailable",
            )
            return
        if (
            not job_info.always_free
            and self.status_incidents is not None
            and self.status_incidents.is_device_held(job_info.device_id)
        ):
            self._reject_job(
                job_info,
                "device is blocked by an active status incident",
                "DeviceStatusConflict",
            )
            return
        self._enqueue_job(job_info)

    def _enqueue_job(self, job_info: JobInfo) -> None:
        """原子登记执行占用；冲突必须回 Backend 触发重算。"""

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
            result = self.device_manager.accept_job(job_info)
            add_event(
                "action.accepted",
                {"action.execution.acceptance": result},
                span=queue_span,
            )
        job_log = format_job_log(job_info.job_id, job_info.task_id, job_info.device_id, job_info.action_name)
        if result == "accepted":
            logger.info("[JobExecutionBackend] job %s start now", job_log)
            self._put_event(("start", job_info), context=job_info.trace_context)
        elif result == "duplicate":
            logger.info("[JobExecutionBackend] duplicate job %s ignored", job_log)
        else:
            logger.error(
                "[JobExecutionBackend] scheduler dispatched conflicting job %s",
                job_log,
            )
            self._reject_job(
                job_info,
                "backend scheduler dispatched a conflicting device action",
                "SchedulerDispatchConflict",
            )

    def _action_mapping(self, device_id: str, action_name: str) -> Optional[Dict[str, Any]]:
        """执行适配器持有的注册表动作声明（Host 侧权威副本，含 slave 远端设备）。"""

        adapter = self._host_node_getter()
        mappings = getattr(adapter, "_action_value_mappings", {}) if adapter else {}
        actions = mappings.get(device_id, {}) if isinstance(mappings, dict) else {}
        for candidate in (action_name, f"auto-{action_name}"):
            mapping = actions.get(candidate)
            if isinstance(mapping, dict):
                return mapping
        return None

    def resolve_action_schema(self, device_id: str, action_name: str) -> dict[str, Any] | None:
        """从当前注册表读取动作合同，供候选计划冻结及预检使用。"""
        action = self._action_mapping(device_id, action_name)
        schema = action.get("schema") if action else None
        return deepcopy(schema) if isinstance(schema, dict) else None

    def resolve_material_lock_parameters(
        self, device_id: str, action_name: str
    ) -> List[str]:
        if self._materials_need_lock_resolver is not None:
            return list(
                self._materials_need_lock_resolver(device_id, action_name) or []
            )
        mapping = self._action_mapping(device_id, action_name)
        if mapping is None:
            return []
        return normalize_material_parameter_names(mapping.get("materials_need_lock"))

    def resolve_action_always_free(self, device_id: str, action_name: str) -> bool:
        """``@action(always_free=True)`` 声明：该动作不占用 ``(device_id, action_name)`` 动作锁。"""

        mapping = self._action_mapping(device_id, action_name)
        return bool(mapping.get("always_free", False)) if mapping is not None else False

    def resolve_action_error_policy(self, device_id: str, action_name: str) -> Dict[str, Any]:
        """注册表为该动作声明的 ``error_policy``（未配置为 ``{}``）。

        调度器在重启后的执行态裁决里用它取 ``max_retries``，与执行面放行失败
        决策时的重试上限口径一致。
        """

        mapping = self._action_mapping(device_id, action_name)
        policy = mapping.get("error_policy") if mapping is not None else None
        return dict(policy) if isinstance(policy, Mapping) else {}

    # ── @action(timeout / execution_timeout) ─────────────────

    def resolve_action_timeouts(
        self,
        device_id: str,
        action_name: str,
        action_args: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """注册表为该动作声明的超时，并用真实参数求出软超时秒数。

        返回 ``{"timeout": 秒|None, "execution_timeout": 秒|None,
        "execution_timeout_spec": 声明原文|None, "error": 求值失败原因|None}``。
        软超时表达式按 ``goal_default`` 叠加 ``action_args`` 求值；求值失败不阻断下发，
        只记录告警并放弃这一道看门狗（硬超时不受影响）。
        """

        mapping = self._action_mapping(device_id, action_name) or {}
        resolved: Dict[str, Any] = {
            "timeout": None,
            "execution_timeout": None,
            "execution_timeout_spec": None,
            "error": None,
        }
        label = f"{device_id}.{action_name}"
        try:
            resolved["timeout"] = normalize_action_timeout(
                mapping.get("timeout"), action_name=label
            )
        except (TypeError, ValueError) as exc:
            resolved["error"] = f"timeout 声明无效: {exc}"
            logger.warning("[JobExecutionBackend] %s", resolved["error"])
        spec = mapping.get("execution_timeout")
        if spec is None:
            return resolved
        try:
            normalized_spec = normalize_execution_timeout(spec, action_name=label)
        except (TypeError, ValueError) as exc:
            resolved["error"] = f"execution_timeout 声明无效: {exc}"
            logger.warning("[JobExecutionBackend] %s", resolved["error"])
            return resolved
        resolved["execution_timeout_spec"] = normalized_spec
        defaults = mapping.get("goal_default")
        values: Dict[str, Any] = dict(defaults) if isinstance(defaults, Mapping) else {}
        values.update(dict(action_args or {}))
        try:
            resolved["execution_timeout"] = evaluate_execution_timeout(
                normalized_spec, values, action_name=label
            )
        except TimeoutExpressionError as exc:
            resolved["error"] = str(exc)
            logger.warning(
                "[JobExecutionBackend] execution_timeout 求值失败，放弃软超时看门狗: %s",
                exc,
            )
        return resolved

    def _arm_job_timeouts(self, job: JobInfo) -> Dict[str, Any]:
        """动作真正下发后启动硬 / 软超时看门狗；到期事件回到 worker 线程串行处理。

        调度权威随载荷下发的 ``timeout_seconds`` / ``execution_timeout_seconds``（节点
        ``execution_policy`` 或已按最终参数求值的注册表表达式）优先；缺省按本地注册表副本解析。
        """

        resolved = self.resolve_action_timeouts(
            job.device_id, job.action_name, job.action_args
        )
        if job.timeout_seconds is not None:
            resolved["timeout"] = float(job.timeout_seconds)
            resolved["timeout_source"] = "dispatch"
        if job.execution_timeout_seconds is not None:
            resolved["execution_timeout"] = float(job.execution_timeout_seconds)
            resolved["execution_timeout_source"] = "dispatch"
        hard = resolved.get("timeout")
        soft = resolved.get("execution_timeout")
        if hard is None and soft is None:
            return resolved
        if hard is not None and soft is not None and soft >= hard:
            logger.warning(
                "[JobExecutionBackend] %s.%s 的 execution_timeout (%.3fs) 不小于 timeout "
                "(%.3fs)，软超时永远不会先触发",
                job.device_id,
                job.action_name,
                soft,
                hard,
            )
        entry: Dict[str, Any] = {
            "started_at": time.time(),
            "hard_seconds": hard,
            "soft_seconds": soft,
            "soft_spec": resolved.get("execution_timeout_spec"),
            "hard": None,
            "soft": None,
        }
        with self._timeout_lock:
            self._clear_job_timeouts_locked(job.job_id)
            self._job_timeouts[job.job_id] = entry
            if hard is not None:
                entry["hard"] = self._start_timeout_timer(
                    job, hard, "hard_timeout", entry, delay=hard + HARD_TIMEOUT_GRACE_SECONDS
                )
            if soft is not None:
                entry["soft"] = self._start_timeout_timer(
                    job, soft, "execution_timeout", entry
                )
        return resolved

    def _start_timeout_timer(
        self,
        job: JobInfo,
        seconds: float,
        event: str,
        entry: Dict[str, Any],
        *,
        delay: Optional[float] = None,
    ) -> threading.Timer:
        context = job.trace_context

        def _fire() -> None:
            self._put_event((event, job.job_id, seconds), context=context)

        timer = threading.Timer(seconds if delay is None else delay, _fire)
        timer.daemon = True
        timer.name = f"ActionTimeout-{event}-{job.job_id[:8]}"
        timer.start()
        return timer

    def _rearm_execution_timeout(self, job: JobInfo) -> Optional[float]:
        """操作员选择继续等待：以同样的软超时秒数重新计时。"""

        with self._timeout_lock:
            entry = self._job_timeouts.get(job.job_id)
            if entry is None:
                entry = {
                    "started_at": time.time(),
                    "hard_seconds": None,
                    "soft_seconds": None,
                    "soft_spec": None,
                    "hard": None,
                    "soft": None,
                }
                self._job_timeouts[job.job_id] = entry
            seconds = entry.get("soft_seconds")
            if seconds is None:
                return None
            existing = entry.get("soft")
            if existing is not None:
                existing.cancel()
            entry["soft"] = self._start_timeout_timer(
                job, float(seconds), "execution_timeout", entry
            )
            entry["soft_rearmed_at"] = time.time()
            return float(seconds)

    def _clear_job_timeouts_locked(self, job_id: str) -> None:
        entry = self._job_timeouts.pop(job_id, None)
        if entry is None:
            return
        for key in ("hard", "soft"):
            timer = entry.get(key)
            if timer is not None:
                timer.cancel()

    def _clear_job_timeouts(self, job_id: str) -> None:
        with self._timeout_lock:
            self._clear_job_timeouts_locked(job_id)

    def _forget_job_timeouts(self, job_id: str) -> None:
        """job 生命周期结束（终态 / 取消）：停掉看门狗并清除超时闸门标记。"""

        with self._timeout_lock:
            self._clear_job_timeouts_locked(job_id)
            self._superseded_jobs.discard(job_id)

    def _supersede_job(self, job_id: str) -> None:
        """终态改由超时闸门决定：停掉看门狗，之后设备迟到的回调全部忽略。"""

        with self._timeout_lock:
            self._superseded_jobs.add(job_id)
            self._clear_job_timeouts_locked(job_id)

    def _is_superseded(self, job_id: str) -> bool:
        with self._timeout_lock:
            return job_id in self._superseded_jobs

    # ── 超时后驱动未返回（ADR 0008） ─────────────────────────

    def _register_overrun(self, job_id: str, item: QueueItem, kind: str, seconds: Any) -> None:
        """超时闸门决定了终态而驱动线程还在跑（Python 杀不掉它）：登记，等它真实返回。"""

        with self._timeout_lock:
            self._overrun_jobs.setdefault(
                job_id,
                {
                    "item": item,
                    "kind": kind,
                    "timeout_seconds": seconds,
                    "timed_out_at": time.time(),
                    "releasing": False,
                    "released": False,
                    "late": None,
                },
            )

    def _overrun_terminal_extras(self, job_id: str) -> Dict[str, Any]:
        """终态放行前调用：驱动未返回则附 ``timeout_hold``，已返回则附 ``late_outcome``。"""

        with self._timeout_lock:
            entry = self._overrun_jobs.get(job_id)
            if entry is None or entry["releasing"] or entry["released"]:
                return {}
            if entry["late"] is not None:
                self._overrun_jobs.pop(job_id, None)
                return {"late_outcome": deepcopy(entry["late"])}
            entry["releasing"] = True
            return {
                "timeout_hold": {
                    "kind": entry["kind"],
                    "timeout_seconds": entry["timeout_seconds"],
                    "timed_out_at": entry["timed_out_at"],
                    "reason": "driver_still_running",
                }
            }

    def _overrun_terminal_released(self, job_id: str) -> None:
        """带 ``timeout_hold`` 的终态已入队：此后到达的迟到结果排在终态之后转交。"""

        with self._timeout_lock:
            entry = self._overrun_jobs.get(job_id)
            if entry is None or not entry["releasing"]:
                return
            entry["released"] = True
            if entry["late"] is not None:
                self._overrun_jobs.pop(job_id, None)
                self._put_event(("late_outcome", entry["item"], deepcopy(entry["late"])))

    def _take_late_outcome(
        self,
        job_id: str,
        status: str,
        return_info: Optional[Dict[str, Any]],
        feedback_data: Optional[Dict[str, Any]],
    ) -> bool:
        """驱动在超时之后真实返回：记录迟到结果；返回 True 表示已接管这次回报。"""

        error_info = (return_info or {}).get("error_info") if isinstance(return_info, dict) else None
        with self._timeout_lock:
            entry = self._overrun_jobs.get(job_id)
            if entry is None:
                return False
            if isinstance(error_info, dict) and error_info.get("driver_still_running"):
                # 运行时自己的超时报告（看门狗已先一步结算）：驱动仍未返回，不是迟到结果
                return True
            if entry["late"] is None:
                entry["late"] = {
                    "status": status,
                    "return_info": deepcopy(return_info) if isinstance(return_info, dict) else {},
                    "feedback_data": deepcopy(dict(feedback_data or {})),
                    "received_at": time.time(),
                    "timeout_kind": entry["kind"],
                    "timed_out_at": entry["timed_out_at"],
                }
            if entry["released"]:
                self._overrun_jobs.pop(job_id, None)
                # 经 worker 队列转交：与终态事件保持先后顺序
                self._put_event(("late_outcome", entry["item"], deepcopy(entry["late"])))
        logger.info(
            "[JobExecutionBackend] 超时的 job %s 驱动迟到返回（%s），按迟到结果转交", job_id, status
        )
        return True

    def _handle_late_outcome(self, item: QueueItem, late: Dict[str, Any]) -> None:
        report = {
            "job_id": item.job_id,
            "task_id": item.task_id,
            "node_id": str(getattr(item, "node_id", "") or ""),
            "node_run_uuid": str(getattr(item, "node_run_uuid", "") or ""),
            "device_id": item.device_id,
            "action_name": item.action_name,
            **late,
        }
        for bridge in self._bridges_for(item):
            callback = getattr(bridge, "publish_job_late_outcome", None)
            if callable(callback):
                try:
                    callback(deepcopy(report), item)
                except Exception:  # noqa: BLE001 - 单个 bridge 失败不影响其他 bridge
                    logger.exception(
                        "[JobExecutionBackend] job %s 的迟到结果转交失败", item.job_id
                    )
        if self._monitor is not None:
            try:
                self._monitor.emit("action", "job_late_outcome", report)
            except Exception:  # noqa: BLE001 - 观测不能阻断执行链路
                logger.exception("[JobExecutionBackend] 迟到结果观测事件发送失败")

    def _release_overrun_cancel(self, job: JobInfo) -> bool:
        """取消一个超时后驱动未返回、终态尚未放行的 job：立即按取消结算并继续持有锁。"""

        with self._timeout_lock:
            entry = self._overrun_jobs.get(job.job_id)
            if entry is None or entry["releasing"] or entry["released"]:
                return False
        self._cancel_pending_error_decisions({job.job_id})
        item = self._queue_item_for(job)
        item.trace_context = getattr(job, "trace_context", {}) or {}
        cancel_info = serialize_result_info(
            "Job was cancelled",
            False,
            {},
            suc_type=SUCCESS_TYPE_CANCELLATION,
        )
        self._release_terminal(item, "failed", cancel_info, {})
        return True

    def _pending_soft_timeout_for(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self._pending_action_error_decisions_lock:
            for pending in self._pending_action_error_decisions.values():
                if pending.get("job_id") == job_id and pending.get("soft_timeout"):
                    return pending
        return None

    def _has_pending_decision(self, job_id: str) -> bool:
        with self._pending_action_error_decisions_lock:
            return any(
                pending.get("job_id") == job_id
                for pending in self._pending_action_error_decisions.values()
            )

    def _resume_bridges(self, item: QueueItem) -> List[Any]:
        """支持把 ``intervention_required`` 收回 ``running`` 的 owner bridge。"""

        return [
            bridge
            for bridge in self._bridges_for(item)
            if callable(getattr(bridge, "publish_job_error_decision_resumed", None))
        ]

    def _retire_pending_decision(
        self, pending: Dict[str, Any], *, selected_action: str, reason: str
    ) -> Optional[Dict[str, Any]]:
        """把一条 pending 决策变成已解决墓碑（幂等重放用），返回解决报告。"""

        decision_id = str(pending.get("decision_id") or "")
        item = pending["item"]
        now = time.time()
        report = {
            "decision_id": decision_id,
            "job_id": pending.get("job_id"),
            "task_id": item.task_id,
            "node_id": str(getattr(item, "node_id", "") or ""),
            "node_run_uuid": str(getattr(item, "node_run_uuid", "") or ""),
            "device_id": item.device_id,
            "action_name": item.action_name,
            "selected_action": selected_action,
            "reason": reason,
            "resolved_at": now,
        }
        with self._pending_action_error_decisions_lock:
            if self._pending_action_error_decisions.pop(decision_id, None) is None:
                return None
            self._resolved_action_error_decisions[decision_id] = {
                "report": deepcopy(report),
                "retain_until": now + self._ACTION_ERROR_DECISION_TOMBSTONE_TTL_SECONDS,
            }
        if self._monitor is not None:
            try:
                self._monitor.emit("action", "job_error_decision_resolved", report)
            except Exception:  # noqa: BLE001 - 观测不能阻断执行链路
                logger.exception("[JobExecutionBackend] failed to emit resolved decision")
        return report

    def _publish_decision_resumed(self, item: QueueItem, report: Dict[str, Any]) -> None:
        for bridge in self._resume_bridges(item):
            try:
                bridge.publish_job_error_decision_resumed(deepcopy(report))
            except Exception:  # noqa: BLE001 - 恢复通知失败不影响动作继续执行
                logger.exception(
                    "[JobExecutionBackend] failed to publish decision resumed for %s",
                    report.get("job_id"),
                )

    def _safe_inventory_cancel(self, job_id: str, *, reason: str) -> None:
        if self._inventory_authority is None:
            return
        try:
            self._inventory_authority.cancel(job_id, reason=reason)
        except Exception:  # noqa: BLE001 - physical lock release must still converge
            logger.exception(
                "[JobExecutionBackend] inventory cancel failed for %s", job_id
            )

    def _safe_inventory_terminal(
        self,
        job_id: str,
        *,
        success: bool,
        reason: str,
    ) -> None:
        if self._inventory_authority is None:
            return
        try:
            self._inventory_authority.terminal(
                job_id,
                success=success,
                reason=reason,
            )
        except Exception:  # noqa: BLE001 - physical lock release must still converge
            logger.exception(
                "[JobExecutionBackend] inventory terminal transition failed for %s",
                job_id,
            )

    def _reject_job(
        self,
        job: JobInfo,
        message: str,
        exception_type: str,
    ) -> None:
        """把微后端无法接受的调度命令作为该 attempt 的 failed 回报。"""

        self._safe_inventory_cancel(job.job_id, reason="job_rejected_before_execution")
        item = self._queue_item_for(job)
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
        """注册完成回调。

        兼容三种签名：``(job_id, success, ret_value)``、追加 ``suc_type`` 的四参数，
        以及再追加 ``return_info``（含 ``error_resolution``，调度器据此识别 retry
        决策）的五参数。
        """
        import inspect

        try:
            params = [
                p for p in inspect.signature(listener).parameters.values()
                if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD, p.VAR_POSITIONAL)
            ]
            arity = 5 if any(p.kind == p.VAR_POSITIONAL for p in params) else min(len(params), 5)
        except (TypeError, ValueError):
            arity = 5

        def wrapped(job_id, success, ret_value, suc_type, return_info):
            if arity >= 5:
                listener(job_id, success, ret_value, suc_type, return_info)
            elif arity == 4:
                listener(job_id, success, ret_value, suc_type)
            else:
                listener(job_id, success, ret_value)

        wrapped._source = listener  # type: ignore[attr-defined]
        self._listeners.append(wrapped)

    def remove_job_finished_listener(self, listener: JobFinishedListener) -> None:
        """解绑组合根拥有的 listener（关停/测试重装时避免重复回调）。"""

        self._listeners = [
            item for item in self._listeners
            if item is not listener and getattr(item, "_source", None) is not listener
        ]

    def _notify_finished(
        self,
        job_id: str,
        success: bool,
        ret_value: Any,
        suc_type: str = "normal",
        return_info: Optional[Dict[str, Any]] = None,
    ) -> None:
        for listener in self._listeners:
            try:
                listener(job_id, success, ret_value, suc_type, return_info or {})
            except Exception:  # noqa: BLE001 - 单个 listener 异常不阻断其他
                logger.exception("[JobExecutionBackend] job finished listener failed")

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

    def _request_running_cancel(self, job_id: str) -> None:
        """对已交给驱动的 job 只发取消请求。

        协程驱动在下一个 await 处收到 CancelledError；同步驱动跑在线程里无法被打断，
        只能等它返回（可在驱动里调用 ``context.raise_if_cancelled()`` 配合）。真实返回经
        ``publish_job_status`` 按取消收口；在此之前 Job 保持活动，设备与锁都不会被让出。
        """

        with self._cancel_state_lock:
            self._cancel_requested_jobs.add(job_id)
        adapter = self._host_node_getter()
        if adapter is not None:
            try:
                adapter.cancel_goal(job_id)
            except Exception:  # noqa: BLE001 - 取消请求失败时等驱动自然返回
                logger.exception(
                    "[JobExecutionBackend] cancel goal failed for %s", job_id
                )
        self._cancel_pending_error_decisions({job_id})

    def _goal_sent(self, job_id: str) -> bool:
        with self._cancel_state_lock:
            return job_id in self._goal_sent_jobs

    def _forget_cancel_state(self, job_id: str) -> None:
        with self._cancel_state_lock:
            self._goal_sent_jobs.discard(job_id)
            self._cancel_requested_jobs.discard(job_id)

    def cancel_job(self, job_id: str) -> bool:
        """Cancel one microbackend-owned job through its active adapter.

        尚未交给驱动的 job 立即结束；已在执行的只请求取消，等驱动真实返回再收口。
        """

        job = self.device_manager.get_job_info(job_id)
        if job is None:
            return False
        if self._release_overrun_cancel(job):
            return True
        self._forget_job_timeouts(job_id)
        if self._goal_sent(job_id):
            self._request_running_cancel(job_id)
            return True
        adapter = self._host_node_getter()
        if adapter is not None:
            try:
                adapter.cancel_goal(job_id)
            except Exception:  # noqa: BLE001 - local state still must converge
                logger.exception(
                    "[JobExecutionBackend] cancel goal failed for %s", job_id
                )
        self._cancel_pending_error_decisions({job_id})
        if not self.device_manager.cancel_job(job_id):
            return False
        self._forget_cancel_state(job_id)
        self._safe_inventory_cancel(job_id, reason="job_canceled")
        item = self._queue_item_for(job)
        return_info = serialize_result_info(
            "Job was cancelled",
            False,
            {},
            suc_type=SUCCESS_TYPE_CANCELLATION,
        )
        self._publish_to_result_bridges({}, item, "failed", return_info)
        self._notify_finished(job_id, False, None, SUCCESS_TYPE_CANCELLATION, return_info)
        return True

    def cancel_task(self, task_id: str) -> List[str]:
        """取消本 execution backend 当前正在执行的整张任务。"""

        jobs = [
            job
            for job in self.device_manager.get_active_jobs()
            if job.task_id == task_id
        ]
        jobs_by_id = {job.job_id: job for job in jobs}
        # 超时后驱动未返回、正等决策的 job：立即按取消结算并继续持有锁，不等驱动
        for job_id in [job_id for job_id, job in jobs_by_id.items() if self._release_overrun_cancel(job)]:
            jobs_by_id.pop(job_id)
        jobs = list(jobs_by_id.values())
        for job_id in jobs_by_id:
            self._forget_job_timeouts(job_id)
        # 已在执行的只请求取消，等驱动返回；其余（排队中）立即结束
        running = {job_id for job_id in jobs_by_id if self._goal_sent(job_id)}
        for job_id in running:
            self._request_running_cancel(job_id)
        adapter = self._host_node_getter()
        if adapter is not None:
            for job in jobs:
                if job.job_id in running:
                    continue
                try:
                    adapter.cancel_goal(job.job_id)
                except Exception:  # noqa: BLE001 - local state still converges
                    logger.exception(
                        "[JobExecutionBackend] cancel goal failed for %s",
                        job.job_id,
                    )
        self._cancel_pending_error_decisions(set(jobs_by_id) - running)
        cancelled_jobs = [
            job
            for job_id, job in jobs_by_id.items()
            if job_id not in running and self.device_manager.cancel_job(job_id)
        ]
        for job in cancelled_jobs:
            job_id = job.job_id
            self._forget_cancel_state(job_id)
            self._safe_inventory_cancel(job_id, reason="task_canceled")
            item = self._queue_item_for(job)
            cancel_info = serialize_result_info(
                "Job was cancelled",
                False,
                {},
                suc_type=SUCCESS_TYPE_CANCELLATION,
            )
            self._publish_to_result_bridges({}, item, "failed", cancel_info)
            self._notify_finished(job_id, False, None, SUCCESS_TYPE_CANCELLATION, cancel_info)
        return [job.job_id for job in cancelled_jobs]

    @staticmethod
    def _queue_item_for(job: JobInfo) -> QueueItem:
        """由已接受的 JobInfo 构造回调/决策链使用的执行引用。"""

        return QueueItem(
            task_type="job_call_back_status",
            device_id=job.device_id,
            action_name=job.action_name,
            task_id=job.task_id,
            job_id=job.job_id,
            notebook_id=job.notebook_id,
            device_action_key=job.device_action_key,
            node_id=job.node_id,
            node_run_uuid=job.node_run_uuid,
            origin=job.origin,
            retry_count=job.retry_count,
        )

    def _bridges_for(self, item: Any) -> List[Any]:
        """按 job 的生命周期 owner（origin）路由：声明了 ``job_origins`` 的 bridge 只收
        自己派发的 job，未声明的 bridge 是观察者，收到全部 job。"""

        origin = str(getattr(item, "origin", "") or "")
        return [
            bridge
            for bridge in self.result_bridges
            if (owned := getattr(bridge, "job_origins", None)) is None or origin in owned
        ]

    def busy_device_action_keys(self) -> Set[str]:
        """当前被占用的 device_action_key（供调度器做锁视图合并）。"""
        return self.device_manager.busy_keys()

    # ── 执行适配器侧接口（bridge 形状，duck-typing） ──────────

    def publish_job_status(
        self,
        feedback_data: dict,
        item: QueueItem,
        status: str,
        return_info: Optional[dict] = None,
    ) -> None:
        """Receive a raw adapter result and advance the canonical job lifecycle."""

        if status in ("success", "failed", "canceled") and self._take_late_outcome(
            item.job_id, status, return_info, feedback_data
        ):
            # 超时闸门之后驱动真实返回：不改已结算的 attempt，只作为迟到结果转交并释放锁
            return
        if self.device_manager.get_job_info(item.job_id) is None:
            return
        if self._is_superseded(item.job_id):
            # 终态已由 timeout / execution_timeout 闸门决定：设备迟到的 feedback 只记日志
            logger.debug(
                "[JobExecutionBackend] ignore late %s from device for timed-out job %s",
                status,
                item.job_id,
            )
            return

        if status == "running":
            self._publish_to_result_bridges(feedback_data, item, status, return_info)
            return
        if status not in ("success", "failed", "canceled"):
            return

        from unilabos.registry.action_policy import SUCCESS_TYPE_EXECUTION_UNKNOWN
        if (return_info or {}).get("suc_type") == SUCCESS_TYPE_EXECUTION_UNKNOWN:
            self._release_terminal(item, "failed", dict(return_info), dict(feedback_data or {}))
            return
        if status == "success":
            from unilabos.registry.typed_contract import TypedActionValueError, validate_action_values

            schema = (self._action_mapping(item.device_id, item.action_name) or {}).get("schema")
            if schema and "x-unilabos-action-contract" in schema:
                try:
                    validate_action_values(schema, (return_info or {}).get("return_value"), section="result")
                except TypedActionValueError as error:
                    status = "failed"
                    return_info = {**(return_info or {}), "suc": False, "error": str(error), "error_info": {
                        "action_name": item.action_name, "exception_type": "TypedActionValueError",
                        "exception_mro": ["TypedActionValueError", "ValueError", "Exception"],
                        "error_message": str(error), "category": "execution", "severity": "error",
                    }}
                    feedback_data = dict(feedback_data or {})
                    if "return_info" in feedback_data:
                        feedback_data["return_info"] = json.dumps(return_info, ensure_ascii=False)
        reported = (return_info or {}).get("error_info") if isinstance(return_info, dict) else None
        if status == "failed" and isinstance(reported, dict) and reported.get("driver_still_running"):
            # 运行时先于看门狗报出硬超时，但同步驱动线程仍在跑（ADR 0008）：与看门狗同一口径，
            # attempt 照常进决策链结算，锁继续持有，线程返回后作为迟到结果释放
            self._supersede_job(item.job_id)
            kind = "execution_timeout" if reported.get("exception_type") == "ExecutionTimeoutException" else "timeout"
            self._register_overrun(item.job_id, item, kind, reported.get("timeout_seconds"))
        with self._cancel_state_lock:
            cancel_requested = item.job_id in self._cancel_requested_jobs
        if cancel_requested and status != "success":
            # 取消请求之后驱动的真实返回：按取消收口，不进失败决策链
            self._clear_job_timeouts(item.job_id)
            self._safe_inventory_cancel(item.job_id, reason="job_canceled")
            cancel_info = serialize_result_info(
                "Job was cancelled",
                False,
                {},
                suc_type=SUCCESS_TYPE_CANCELLATION,
            )
            cancel_info["device_status"] = status
            self._release_terminal(item, "failed", cancel_info, dict(feedback_data or {}))
            return

        self._clear_job_timeouts(item.job_id)
        soft_pending = self._pending_soft_timeout_for(item.job_id)
        if soft_pending is not None:
            if not self._resume_bridges(item):
                # Backend-controlled：终态闸门已在权威侧打开，真实结果只能附在待决策上，
                # 由操作员以 operator_intervention 放行（缺省结果即设备真实返回值）。
                with self._pending_action_error_decisions_lock:
                    soft_pending["late_result"] = {
                        "status": status,
                        "return_info": deepcopy(return_info) if isinstance(return_info, dict) else {},
                        "feedback_data": deepcopy(dict(feedback_data or {})),
                        "received_at": time.time(),
                    }
                logger.info(
                    "[JobExecutionBackend] job %s finished (%s) while its execution_timeout "
                    "decision %s is pending on the Backend; result attached to the decision",
                    item.job_id,
                    status,
                    soft_pending.get("decision_id"),
                )
                self._safe_inventory_terminal(
                    item.job_id,
                    success=status == "success",
                    reason=f"action_{status}",
                )
                return
            # 本机调度：真实结果优先于软超时决策，先收回 intervention_required 再照常收口
            resolved = self._retire_pending_decision(
                soft_pending,
                selected_action="superseded",
                reason=f"action_{status}",
            )
            if resolved is not None:
                self._publish_decision_resumed(item, resolved)

        # 驱动返回终态后即可推进库存；Job 的业务终态仍可能等待 Backend gate。
        self._safe_inventory_terminal(
            item.job_id,
            success=status == "success",
            reason=f"action_{status}",
        )
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

        for bridge in self._bridges_for(item):
            callback = getattr(bridge, "publish_job_started", None)
            if callable(callback):
                try:
                    callback(item)
                except Exception:  # noqa: BLE001 - 回报失败不能重复执行 action
                    logger.exception(
                        "[JobExecutionBackend] failed to publish job started"
                    )

    def execution_adapter(self) -> Any:
        """返回当前 Host 执行适配器；尚未就绪时返回 ``None``。"""

        try:
            return self._host_node_getter()
        except Exception:  # noqa: BLE001 - 适配器未注册时保持可选语义
            return None

    def publish_capabilities_changed(self) -> None:
        """设备/动作能力集变化：让 coordinator 刷新 endpoint 能力快照。"""

        for bridge in self.result_bridges:
            callback = getattr(bridge, "publish_endpoint_capabilities", None)
            if callable(callback):
                try:
                    callback()
                except Exception:  # noqa: BLE001 - 能力快照缺失不阻塞执行链路
                    logger.exception(
                        "[JobExecutionBackend] failed to publish endpoint capabilities"
                    )

    def publish_host_ready(self) -> None:
        """HostLink/ROS2 adapter ready 后恢复未下发 attempt 并刷新能力快照。"""

        for bridge in self.result_bridges:
            callback = getattr(bridge, "resume_pending_dispatches", None)
            if callable(callback):
                try:
                    callback()
                except Exception:  # noqa: BLE001 - 后续重连仍可恢复
                    logger.exception(
                        "[JobExecutionBackend] failed to resume pending dispatches"
                    )
            publish_capabilities = getattr(
                bridge, "publish_endpoint_capabilities", None
            )
            if callable(publish_capabilities):
                try:
                    publish_capabilities()
                except Exception:  # noqa: BLE001 - 能力快照缺失不阻塞执行链路
                    logger.exception(
                        "[JobExecutionBackend] failed to publish endpoint capabilities"
                    )

    def _publish_to_result_bridges(
        self,
        feedback_data: Dict[str, Any],
        item: QueueItem,
        status: str,
        return_info: Optional[Dict[str, Any]] = None,
    ) -> None:
        for bridge in self._bridges_for(item):
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

        self._forget_cancel_state(item.job_id)
        extras = self._overrun_terminal_extras(item.job_id)
        if extras:
            return_info = {**return_info, **extras}
            if isinstance(result_data.get("return_info"), str):
                result_data = {**result_data, "return_info": json.dumps(return_info, ensure_ascii=False)}
        self._publish_to_result_bridges(result_data, item, status, return_info)

        ret_value = None
        suc_type = "normal"
        ret_value = return_info.get("return_value")
        suc_type = str(return_info.get("suc_type") or "normal")
        parent = extract_trace_context(getattr(item, "trace_context", {}))
        self._put_event(
            ("finished", item.job_id, status == "success", ret_value, suc_type, return_info),
            context=parent,
        )
        if "timeout_hold" in extras:
            self._overrun_terminal_released(item.job_id)

    def _begin_action_error_decision(
        self,
        item: QueueItem,
        return_info: Dict[str, Any],
        result_data: Dict[str, Any],
        *,
        soft_timeout: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Hold a failed attempt until Backend has updated scheduling and releases it.

        ``soft_timeout`` 非空表示这是 ``execution_timeout`` 触发的决策：动作**仍在执行**，
        没有失败结果可放行；支持恢复的 owner bridge 会多得到一个 ``wait`` 选项，
        任何终态选项都会先取消动作再按失败处理。
        """

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
            for bridge in self._bridges_for(item)
            if (
                callable(getattr(bridge, "publish_job_error_pending", None))
                or callable(
                    getattr(bridge, "publish_job_error_decision_required", None)
                )
            )
        ]
        if not decision_bridges:
            return False
        if soft_timeout is not None and self._resume_bridges(item):
            # 软超时特有：动作还在跑，操作员可以只是"再等一会"；terminal 选项照旧
            if "wait" not in {str(option.get("action")) for option in options}:
                options = [
                    {
                        "action": "wait",
                        "label": "继续等待",
                        "description": "动作继续执行，按同样的 execution_timeout 重新计时",
                    },
                    *options,
                ]

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
            "node_run_uuid": str(getattr(item, "node_run_uuid", "") or ""),
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
        if soft_timeout is not None:
            report["action_still_running"] = True
            report["timeout_seconds"] = soft_timeout.get("seconds")
            report["timeout_spec"] = soft_timeout.get("spec")
            report["timeout_kind"] = "execution_timeout"
        elif error_info.get("timeout_seconds") is not None:
            report["timeout_seconds"] = error_info["timeout_seconds"]
            report["timeout_kind"] = "timeout"
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
            "soft_timeout": bool(soft_timeout is not None),
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
        """HostNode 设备属性更新入口（值变化时调用）。

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
                "soft_timeout": bool(report.get("action_still_running")),
            }
        if report.get("timeout_kind") == "timeout":
            # 硬超时决策跨 Host 重启恢复：原驱动已随进程结束、不会再返回，放行后锁继续持有，
            # 只能由人工核实现场后强制释放（ADR 0008）
            self._register_overrun(job_id, item, "timeout", report.get("timeout_seconds"))
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
        if str(decision.get("action") or "") == "retry" and int(
            pending.get("retry_count") or 0
        ) >= int(pending.get("max_retries") or 0):
            # Host 报告只如实携带 retry_count/max_retries；上限由调度权威在放行时执行。
            # 本机 Workflow Authority 就是这个权威：超限的 retry 不放行。
            logger.warning(
                "[JobExecutionBackend] retry rejected for decision %s: attempt limit "
                "%s reached",
                decision_id,
                pending.get("max_retries"),
            )
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
            if selected == "operator_intervention":
                schema = (self._action_mapping(device_id, pending["item"].action_name) or {}).get("schema")
                if schema and "x-unilabos-action-contract" in schema:
                    from unilabos.registry.typed_contract import TypedActionValueError, validate_action_values

                    late = pending.get("late_result") if pending.get("soft_timeout") else None
                    if "result" in decision or "return_value" in decision or isinstance(late, dict):
                        replacement = decision.get("result", decision.get("return_value", (late or {}).get("return_info", {}).get("return_value")))
                        try:
                            validate_action_values(schema, replacement, section="result")
                        except TypedActionValueError:
                            # 无效替换不得消费待决策记录，操作员可修改后重试。
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
        if pending.get("soft_timeout"):
            late_result = pending.get("late_result")
            if selected == "wait":
                # 动作从未停止：收回 intervention_required，按同样的软超时重新计时
                job = self.device_manager.get_job_info(job_id)
                if job is not None:
                    self._rearm_execution_timeout(job)
                self._publish_decision_resumed(item, resolved_report)
                logger.info(
                    "[JobExecutionBackend] execution_timeout decision %s: keep waiting for job %s",
                    decision_id,
                    job_id,
                )
                return True
            if late_result is None:
                # 动作仍在执行：任何终态选项都先协作式取消，再按失败 / 替换结果收口；
                # 驱动返回前锁继续由该 attempt 持有（与硬超时同一口径）
                self._supersede_job(job_id)
                self._register_overrun(
                    job_id, item, "execution_timeout", pending.get("report", {}).get("timeout_seconds"),
                )
                adapter = self._host_node_getter()
                if adapter is not None:
                    try:
                        adapter.cancel_goal(job_id)
                    except Exception:  # noqa: BLE001 - 本地状态仍要收敛
                        logger.exception(
                            "[JobExecutionBackend] cancel goal failed for timed-out job %s",
                            job_id,
                        )
                self._safe_inventory_terminal(
                    job_id,
                    success=selected == "operator_intervention",
                    reason=f"execution_timeout_{selected}",
                )
            elif (
                selected == "operator_intervention"
                and "result" not in decision
                and "return_value" not in decision
                and str(late_result.get("status") or "") == "success"
            ):
                # Backend-controlled 下动作已在等待期间真实完成：缺省替换结果就是设备返回值
                real_info = late_result.get("return_info") or {}
                decision["result"] = real_info.get("return_value")
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
                            return_info = event[5] if len(event) > 5 else None
                            self._handle_finished(
                                event[1], event[2], event[3], suc_type, return_info
                            )
                        elif event[0] == "device_status":
                            self._write_device_property(event[1], event[2], event[3])
                        elif event[0] == "hard_timeout":
                            self._handle_hard_timeout(event[1], float(event[2]))
                        elif event[0] == "execution_timeout":
                            self._handle_execution_timeout(event[1], float(event[2]))
                        elif event[0] == "late_outcome":
                            self._handle_late_outcome(event[1], event[2])
            except Exception:  # noqa: BLE001 - worker 不允许死
                logger.exception("[JobExecutionBackend] event %s failed", event[0])
            finally:
                with self._pending_lock:
                    self._pending -= 1

    def _start_goal(self, job: JobInfo) -> None:
        if self.device_manager.get_job_info(job.job_id) is None:
            self._safe_inventory_cancel(
                job.job_id,
                reason="job_removed_before_execution",
            )
            return
        job_log = format_job_log(job.job_id, job.task_id, job.device_id, job.action_name)
        queue_item = self._queue_item_for(job)
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
            self._safe_inventory_cancel(
                job.job_id,
                reason="execution_adapter_unavailable",
            )
            if not self._begin_action_error_decision(
                queue_item,
                return_info,
                {},
            ):
                self._release_terminal(queue_item, "failed", return_info, {})
            return
        from unilabos.registry.typed_contract import validate_action_values

        schema = (self._action_mapping(job.device_id, job.action_name) or {}).get("schema")
        try:
            from unilabos.registry.typed_contract import require_frozen_resource_contract
            require_frozen_resource_contract(schema, job.execution_payload)
            job.action_args = validate_action_values(schema, job.action_args,
                managed_resources=bool(job.execution_payload.get("execution_permit")))
            if schema and "x-unilabos-action-contract" in schema:
                from unilabos.server.services.runtime.workflow.run_preflight import validate_resource_values

                gateway = self._inventory_authority.gateway if self._inventory_authority is not None else None
                validate_resource_values(schema, job.action_args, gateway)
        except ValueError as error:
            self._reject_job(job, str(error), "TypedActionValueError")
            return
        from unilabos.server.backend.execution_start import ExecutionStartGuard, confirm_execution_start
        from unilabos.registry.action_policy import SUCCESS_TYPE_EXECUTION_UNKNOWN
        guarded = self._require_execution_permit or bool(job.execution_payload.get("execution_permit"))
        if guarded:
            queue_item.start_guard = ExecutionStartGuard(
                self._execution_authority, {**job.execution_payload, "action_args": job.action_args}, self._executor_epoch,
                canceled=lambda: (self.device_manager.get_job_info(job.job_id) is None
                                  or job.job_id in self._cancel_requested_jobs),
                consume=lambda: self._inventory_authority.consume(job.job_id) if self._inventory_authority else None,
                stopped=lambda: not self._running,
            )
        elif self._inventory_authority is not None:
            try:
                self._inventory_authority.consume(job.job_id)
            except ExecutionInventoryError as exc:
                self._reject_job(job, str(exc), "InventoryConsumeError")
                return
        try:
            if guarded and not getattr(adapter, "supports_execution_start_guard", False):
                confirm_execution_start(queue_item)
            adapter.send_goal(
                queue_item,
                action_type=job.action_type,
                action_kwargs=job.action_args,
                sample_material=job.sample_material,
                server_info=job.server_info,
            )
            with self._cancel_state_lock:
                self._goal_sent_jobs.add(job.job_id)
            self.publish_job_started(queue_item)
            logger.info("[JobExecutionBackend] goal sent for job %s", job_log)
            timeouts = self._arm_job_timeouts(job)
            if timeouts.get("timeout") is not None or timeouts.get("execution_timeout") is not None:
                logger.info(
                    "[JobExecutionBackend] job %s watchdog armed: timeout=%s execution_timeout=%s (%s)",
                    job_log,
                    timeouts.get("timeout"),
                    timeouts.get("execution_timeout"),
                    timeouts.get("execution_timeout_spec"),
                )
        except Exception as exc:  # noqa: BLE001 - 开始许可之后的异常不能推断设备未执行
            if guarded:
                return_info = serialize_result_info(str(exc), False, {}, suc_type=SUCCESS_TYPE_EXECUTION_UNKNOWN)
                return_info["uncertainty_reason"] = str(exc)
                self._release_terminal(queue_item, "failed", return_info, {})
                return
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
            self._safe_inventory_terminal(
                job.job_id,
                success=False,
                reason="action_dispatch_failed",
            )
            if not self._begin_action_error_decision(
                queue_item, return_info, {}
            ):
                self._release_terminal(queue_item, "failed", return_info, {})

    # ── 超时闸门（worker 线程） ──────────────────────────────

    def _handle_hard_timeout(self, job_id: str, seconds: float) -> None:
        """``@action(timeout=...)`` 到期：协作式取消动作，attempt 以 TimeoutException 进入决策链。"""

        job = self.device_manager.get_job_info(job_id)
        if job is None or self._is_superseded(job_id):
            return
        job_log = format_job_log(job.job_id, job.task_id, job.device_id, job.action_name)
        item = self._queue_item_for(job)
        item.trace_context = getattr(job, "trace_context", {}) or {}
        # 软超时决策若还挂着，被硬超时取代：同一 job 只保留一条待决策
        soft_pending = self._pending_soft_timeout_for(job_id)
        if soft_pending is not None:
            self._retire_pending_decision(
                soft_pending,
                selected_action="superseded",
                reason="hard_timeout",
            )
        self._supersede_job(job_id)
        # 同步驱动的线程杀不掉：取消只是请求，attempt 结算后锁继续由它持有，直到驱动真实返回
        self._register_overrun(job_id, item, "timeout", seconds)
        adapter = self._host_node_getter()
        if adapter is not None:
            try:
                adapter.cancel_goal(job_id)
            except Exception:  # noqa: BLE001 - 取消失败也要收敛本地状态
                logger.exception(
                    "[JobExecutionBackend] cancel goal failed for timed-out job %s", job_log
                )
        elapsed = time.time() - float(job.start_time or time.time())
        error = TimeoutException(
            job.action_name,
            seconds,
            device_id=job.device_id,
            elapsed_seconds=elapsed,
        )
        logger.warning("[JobExecutionBackend] %s: %s", job_log, error)
        return_info = serialize_result_info(str(error), False, {})
        return_info["error_info"] = error.to_error_info()
        self._safe_inventory_terminal(job_id, success=False, reason="action_timeout")
        if not self._begin_action_error_decision(item, return_info, {}):
            self._release_terminal(item, "failed", return_info, {})

    def _handle_execution_timeout(self, job_id: str, seconds: float) -> None:
        """``@action(execution_timeout=...)`` 到期：动作继续执行，只打开一条带 wait 的决策。"""

        job = self.device_manager.get_job_info(job_id)
        if job is None or self._is_superseded(job_id):
            return
        if self._has_pending_decision(job_id):
            return
        job_log = format_job_log(job.job_id, job.task_id, job.device_id, job.action_name)
        item = self._queue_item_for(job)
        item.trace_context = getattr(job, "trace_context", {}) or {}
        with self._timeout_lock:
            entry = self._job_timeouts.get(job_id) or {}
            spec = entry.get("soft_spec")
        elapsed = time.time() - float(job.start_time or time.time())
        error = ExecutionTimeoutException(
            job.action_name,
            seconds,
            device_id=job.device_id,
            elapsed_seconds=elapsed,
        )
        logger.warning("[JobExecutionBackend] %s: %s", job_log, error)
        return_info = serialize_result_info(str(error), False, {})
        return_info["error_info"] = error.to_error_info()
        if not self._begin_action_error_decision(
            item,
            return_info,
            {},
            soft_timeout={"seconds": seconds, "spec": spec},
        ):
            # 没有能决策的 bridge：不能替操作员终止动作，只留告警
            logger.warning(
                "[JobExecutionBackend] execution_timeout for %s has no decision bridge; "
                "action keeps running",
                job_log,
            )

    def _handle_finished(
        self,
        job_id: str,
        success: bool,
        ret_value: Any,
        suc_type: str = "normal",
        return_info: Optional[Dict[str, Any]] = None,
    ) -> None:
        finished_job = self.device_manager.get_job_info(job_id)
        self._forget_job_timeouts(job_id)
        try:
            if finished_job is not None:
                self.device_manager.end_job(job_id)

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
        finally:
            # 本 attempt 的库存 reservation 先收敛（失败即隔离/释放），再通知调度器：
            # retry 的新 attempt 需要在旧 reservation 处置之后重新预留。
            from unilabos.registry.action_policy import SUCCESS_TYPE_EXECUTION_UNKNOWN
            if suc_type != SUCCESS_TYPE_EXECUTION_UNKNOWN:
                self._safe_inventory_terminal(job_id, success=success, reason="action_finished")
        self._notify_finished(job_id, success, ret_value, suc_type, return_info)

    @staticmethod
    def _default_host_getter() -> Any:
        from unilabos.backend.hostlink.adapter_registry import get_execution_adapter

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
        name = getattr(content, "template_name", "")
        if isinstance(name, str) and name:
            return name
        for node in getattr(getattr(host_node, "devices_config", None), "all_nodes", []):
            content = getattr(node, "res_content", None)
            if getattr(content, "id", None) == device_id:
                value = getattr(content, "template_name", "")
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


__all__ = [
    "JobExecutionBackend",
    "JobFinishedListener",
    "make_device_materials_need_lock_resolver",
    "make_device_status_policy_resolver",
]
