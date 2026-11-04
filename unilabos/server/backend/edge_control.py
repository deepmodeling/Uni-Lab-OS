"""runtime.v1 控制面服务端：向 Edge 下发命令并接收执行事件。

该服务用于 ``--role backend`` 的进程分离模式。它实现
:class:`BackendScheduler` 的 executor 契约（``dispatch`` / ``cancel_task`` /
``add_job_finished_listener``），通过 WebSocket 发送轻量通知，并通过 HTTP
提供权威命令文档。Edge 重连后可继续处理尚未拉取的命令。
"""

from __future__ import annotations

import base64
import json
import logging
import queue
import threading
import time
import uuid
from copy import deepcopy
from typing import Any, Callable, Dict, List, Mapping, Optional

from unilabos.registry.action_policy import SUCCESS_TYPE_EXECUTION_UNKNOWN
from unilabos.protocol.base import canonical_hash
from unilabos.protocol.runtime.control import (
    BackendCommandDocument,
    BackendCommandNotice,
    BackendHttpRequest,
    BackendSessionNotice,
    CancelJobContent,
    EdgeChangeAck,
    EdgeChangeNotice,
    EdgeHttpResponse,
    ErrorDecisionContent,
    ExecuteJobContent,
    PingNotice,
    PongNotice,
)
from unilabos.protocol.runtime import CommandEnvelope
from unilabos.server.backend.scheduler.payloads import DispatchPayload

logger = logging.getLogger(__name__)

# listener 签名与 JobExecutionBackend 一致：(job_id, success, ret_value, suc_type, return_info)
JobFinishedListener = Callable[..., None]

_TERMINAL_EVENT_STATUS = {
    "execution.succeeded": ("succeeded", True),
    "execution.failed": ("failed", False),
    "execution.canceled": ("canceled", False),
}


def _now_ms() -> int:
    return int(time.time() * 1000)


def _positive_seconds(value: Any) -> Optional[float]:
    """调度器载荷里的超时秒数：正数才有意义，其余视为未声明。"""

    if value is None or isinstance(value, bool):
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None


def _decode_payload_document(body: Any) -> Optional[Any]:
    """``GET /api/v1/history/payloads/{uuid}`` 的正文 → 原始 JSON。"""

    if not isinstance(body, dict):
        return None
    inline = body.get("inline_payload")
    if inline is None:
        return None
    raw = base64.b64decode(inline)
    encoding = str(body.get("encoding") or "utf-8")
    if encoding == "binary":
        encoding = "utf-8"
    return json.loads(raw.decode(encoding))


class EdgePayloadClient:
    """经控制 WS 让 Edge 在进程内读出 durable 事件正文（Edge 不监听端口）。"""

    def __init__(self, service: "EdgeControlService", timeout: float = 15.0) -> None:
        self._service = service
        self.timeout = timeout

    def fetch_json(self, payload_uuid: str) -> Optional[Any]:
        response = self._service.http_request(
            "GET", f"/api/v1/history/payloads/{payload_uuid}", timeout=self.timeout
        )
        if response is None or response.status_code != 200:
            logger.warning(
                "[EdgeControl] 拉取 payload %s 失败: %s",
                payload_uuid,
                "Edge 未响应" if response is None else f"HTTP {response.status_code}",
            )
            return None
        try:
            return _decode_payload_document(json.loads(response.body_bytes().decode("utf-8")))
        except (ValueError, UnicodeDecodeError):
            logger.warning("[EdgeControl] payload %s 正文不是合法 JSON", payload_uuid)
            return None


class _HttpWaiter:
    __slots__ = ("event", "response")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.response: Optional["EdgeHttpResponse"] = None


class EdgeControlService:
    """单 Edge 的 runtime.v1 控制面服务端与调度执行代理。"""

    def __init__(
        self,
        *,
        edge_uuid: str = "local-edge",
        payload_client: Optional[Any] = None,
        registry_service: Optional[Any] = None,
    ) -> None:
        self.edge_uuid = edge_uuid
        self.authority_epoch = f"backend:{uuid.uuid4()}"
        # session 跨 WS 重连稳定，Edge durable outbox 依赖它恢复重放
        self.session_uuid = str(uuid.uuid4())
        self.connection_epoch = str(uuid.uuid4())
        self._payloads = payload_client or EdgePayloadClient(self)
        # Backend → Edge 的进程内 HTTP 请求（backend_http）等待表：request_uuid -> waiter
        self._http_waiters: Dict[str, _HttpWaiter] = {}
        # Edge 上报的 Registry Authority 快照用于解析调度预占参数。
        self._registry_service = registry_service
        self._lock = threading.RLock()
        self._sequence = 0
        self._commands: Dict[str, BackendCommandDocument] = {}
        self._notices: Dict[str, BackendCommandNotice] = {}
        self._fetched: set[str] = set()
        self._inflight: Dict[str, Dict[str, Any]] = {}
        self._listeners: List[JobFinishedListener] = []
        # 与 JobExecutionBackend 同形：调度器把自己挂进来后，Edge（重新）接入时收到
        # resume_pending_dispatches，重算等待集合并派发被闸住的节点；Edge 报上来的
        # 失败决策经 publish_job_error_decision_required 让节点运行进入 intervention_required。
        self.result_bridges: List[Any] = []
        # Edge 打开终态闸门、等 Backend 放行的失败：decision_id -> {job_uuid, report, required_scheduler_revision}
        self._pending_decisions: Dict[str, Dict[str, Any]] = {}
        self._last_edge_sequence = 0
        # WS 下行通道：线程安全队列，由 API 层的发送协程消费
        self.outgoing: "queue.Queue[dict[str, Any]]" = queue.Queue()
        self._connected = False
        # 当前 Host 进程身份（连接头 EdgeSession）；变化即上一 Host 进程已退出
        self._edge_process = ""

    # ── Backend → Edge 进程内 HTTP（Edge 不监听端口） ────────────

    def http_request(
        self,
        method: str,
        path: str,
        *,
        headers: Optional[Mapping[str, str]] = None,
        body: bytes = b"",
        timeout: float = 60.0,
    ) -> Optional["EdgeHttpResponse"]:
        """让 Edge 对它自己的 ASGI 应用执行一条请求并等结果；Edge 不在线 / 超时返回 None。"""

        waiter = _HttpWaiter()
        request_uuid = str(uuid.uuid4())
        with self._lock:
            if not self._connected:
                return None
            self._http_waiters[request_uuid] = waiter
        request = BackendHttpRequest(
            request_uuid=request_uuid,
            method=str(method).upper(),
            path=path,
            headers={str(k): str(v) for k, v in (headers or {}).items()},
            body_base64=base64.b64encode(body).decode("ascii") if body else "",
            timeout_seconds=float(timeout),
        )
        self.outgoing.put(
            {"action": "backend_http", "data": request.model_dump(mode="json", exclude_none=True)}
        )
        try:
            if not waiter.event.wait(timeout):
                logger.warning("[EdgeControl] Edge 未在 %.0fs 内响应 %s %s", timeout, method, path)
                return None
            return waiter.response
        finally:
            with self._lock:
                self._http_waiters.pop(request_uuid, None)

    def complete_http_response(self, response: "EdgeHttpResponse") -> bool:
        """Edge 用 HTTP 送回的执行结果；找不到等待者（超时已放弃）返回 False。"""

        with self._lock:
            waiter = self._http_waiters.get(response.request_uuid)
        if waiter is None:
            return False
        waiter.response = response
        waiter.event.set()
        return True

    def _fail_http_waiters(self) -> None:
        with self._lock:
            waiters = list(self._http_waiters.values())
            self._http_waiters.clear()
        for waiter in waiters:
            waiter.event.set()

    # ── executor 契约（BackendScheduler 消费） ────────────────

    def add_job_finished_listener(self, listener: JobFinishedListener) -> None:
        with self._lock:
            if listener not in self._listeners:
                self._listeners.append(listener)

    def remove_job_finished_listener(self, listener: JobFinishedListener) -> None:
        with self._lock:
            if listener in self._listeners:
                self._listeners.remove(listener)

    def dispatch(self, payload: DispatchPayload) -> None:
        """把调度器 execute_job 载荷转为 runtime.v1 控制面命令并通知 Edge。"""

        job_uuid = str(payload["job_id"])
        attempt_no = int(payload.get("attempt_no") or 1)
        retry_of_job_uuid = payload.get("retry_of_job_uuid")
        if retry_of_job_uuid in (None, ""):
            retry_of_job_uuid = None
        else:
            retry_of_job_uuid = str(retry_of_job_uuid)
        content = ExecuteJobContent(
            job_uuid=job_uuid,
            task_uuid=str(payload["task_id"]),
            node_uuid=str(payload["node_id"]),
            # 一个 workflow node run 可以有多个 execution attempt；不能把
            # attempt 自身的 job uuid 当作 group，否则 Backend 无法校验重试链。
            attempt_group_uuid=str(payload.get("node_run_uuid") or job_uuid),
            retry_of_job_uuid=retry_of_job_uuid,
            attempt_no=attempt_no,
            attempt_trigger=str(payload.get("attempt_trigger") or "initial"),
            retry_count=(
                int(payload["retry_count"]) if payload.get("retry_count") is not None else None
            ),
            device_uuid=str(payload["device_id"]),
            action_name=str(payload["action"]),
            action_type=str(payload.get("action_type") or ""),
            action_args=dict(payload.get("action_args") or {}),
            materials_need_lock=list(payload.get("materials_need_lock") or []),
            sample_material=dict(payload.get("sample_material") or {}),
            server_info=payload.get("server_info"),
            notebook_uuid=str(payload.get("notebook_id") or ""),
            route_uuid=payload.get("route_uuid"),
            endpoint_uuid=payload.get("endpoint_uuid"),
            transport=payload.get("transport"),
            material_bindings=list(payload.get("material_bindings") or []),
            inventory_requirements=list(payload.get("inventory_requirements") or []),
            inventory_reservation_uuid=payload.get("inventory_reservation_uuid"),
            scheduler_revision=int(payload.get("scheduler_revision") or 0),
            timeout_seconds=_positive_seconds(payload.get("timeout_seconds")),
            execution_timeout_seconds=_positive_seconds(
                payload.get("execution_timeout_seconds")
            ),
        )
        with self._lock:
            self._inflight[job_uuid] = {
                "task_uuid": content.task_uuid,
                "dispatched_at_ms": _now_ms(),
                # Edge 打开终态闸门时要求 confirmed_scheduler_revision >= 该值 + 1
                "scheduler_revision": content.scheduler_revision,
            }
        self._issue_command(
            command_type="execute_job",
            job_uuid=job_uuid,
            payload=content.model_dump(mode="json", exclude_none=True),
        )

    def cancel_task(self, task_uuid: str) -> None:
        with self._lock:
            job_uuids = [
                job_uuid
                for job_uuid, info in self._inflight.items()
                if info["task_uuid"] == task_uuid
            ]
        for job_uuid in job_uuids:
            self.cancel_job(job_uuid, reason="task_canceled")

    def cancel_job(self, job_uuid: str, reason: str = "") -> None:
        content = CancelJobContent(
            adapter_command_uuid=str(uuid.uuid4()),
            reason=reason,
        )
        self._issue_command(
            command_type="cancel_job",
            job_uuid=job_uuid,
            payload=content.model_dump(mode="json", exclude_none=True),
        )

    # ── 注册表解析（BackendScheduler 的可选 executor 契约，与 JobExecutionBackend 同名） ──

    def _registry(self) -> Any:
        registry_service = self._registry_service
        if registry_service is None:
            # 未显式注入时取进程内 Registry Authority（随本机调度权威一起装配）。
            from unilabos.server.services.runtime.registry import get_registry_service

            registry_service = get_registry_service()
        return registry_service

    @staticmethod
    def _device_class(device_id: str) -> str:
        """调度器给的是设备实例（物料 uuid / resource_id）；注册表按设备类记条目。"""

        from unilabos.server.backend.composition import get_materials_service

        service = get_materials_service()
        if service is not None:
            for lookup in (service.get_material, service.get_material_by_resource_id):
                try:
                    return str(lookup(str(device_id)).material.template_name)
                except Exception:  # noqa: BLE001 - 不是该形态的标识就换下一种
                    continue
        return str(device_id)

    def _action_definition(self, device_id: str, action_name: str) -> Optional[Mapping[str, Any]]:
        registry_service = self._registry()
        if registry_service is None:
            return None
        try:
            return registry_service.action_definition(self._device_class(device_id), action_name)
        except Exception:  # noqa: BLE001 - 注册表查询失败按未声明处理
            logger.exception("[EdgeControl] 解析动作声明失败: %s/%s", device_id, action_name)
            return None

    def resolve_material_lock_parameters(
        self, device_id: str, action_name: str
    ) -> list[str]:
        """从 Edge 上报的 Registry Authority 快照解析动作锁参数。

        Edge 首次上报前镜像为空，此时返回空集也不会削弱执行侧契约：Edge
        coordinator 在 ``_dispatch`` 时会与本地注册表声明取并集，损失的只是
        Backend 预占阶段的互斥精度。
        """

        action = self._action_definition(device_id, action_name)
        names = action.get("materials_need_lock") if action is not None else None
        if not isinstance(names, (list, tuple)):
            return []
        return [str(item) for item in names if str(item).strip()]

    def resolve_action_always_free(self, device_id: str, action_name: str) -> bool:
        action = self._action_definition(device_id, action_name)
        return bool(action.get("always_free", False)) if action is not None else False

    def resolve_action_error_policy(self, device_id: str, action_name: str) -> dict[str, Any]:
        action = self._action_definition(device_id, action_name)
        policy = action.get("error_policy") if action is not None else None
        return dict(policy) if isinstance(policy, Mapping) else {}

    def resolve_action_timeouts(
        self,
        device_id: str,
        action_name: str,
        action_args: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        """从 Edge 上报的注册表快照解析 ``@action(timeout / execution_timeout)``，
        软超时表达式按 ``goal_default`` 叠加 ``action_args`` 求值；与 JobExecutionBackend 同形。"""

        from unilabos.registry.action_timeout import (
            TimeoutExpressionError,
            evaluate_execution_timeout,
            normalize_action_timeout,
            normalize_execution_timeout,
        )

        action = self._action_definition(device_id, action_name) or {}
        resolved: dict[str, Any] = {
            "timeout": None,
            "execution_timeout": None,
            "execution_timeout_spec": None,
            "error": None,
        }
        label = f"{device_id}.{action_name}"
        try:
            resolved["timeout"] = normalize_action_timeout(action.get("timeout"), action_name=label)
        except (TypeError, ValueError) as exc:
            resolved["error"] = f"timeout 声明无效: {exc}"
        spec = action.get("execution_timeout")
        if spec is None:
            return resolved
        try:
            normalized = normalize_execution_timeout(spec, action_name=label)
        except (TypeError, ValueError) as exc:
            resolved["error"] = f"execution_timeout 声明无效: {exc}"
            return resolved
        resolved["execution_timeout_spec"] = normalized
        defaults = action.get("goal_default")
        values: dict[str, Any] = dict(defaults) if isinstance(defaults, Mapping) else {}
        values.update(dict(action_args or {}))
        try:
            resolved["execution_timeout"] = evaluate_execution_timeout(
                normalized, values, action_name=label
            )
        except TimeoutExpressionError as exc:
            resolved["error"] = str(exc)
            logger.warning("[EdgeControl] execution_timeout 求值失败，放弃软超时: %s", exc)
        return resolved

    # ── 失败决策（Edge 打开终态闸门，Backend 放行） ─────────────────

    def list_error_decisions(self) -> list[dict[str, Any]]:
        with self._lock:
            return [deepcopy(item["report"]) for item in self._pending_decisions.values()]

    def resolve_error_decision(self, decision_id: str, decision: Mapping[str, Any]) -> bool:
        """把网页式决策变成 runtime.v1 放行命令：operator_intervention 带结果 → replace_result，
        其余（abort / skip / retry …）→ release_failed，selected_action 原样带给 Edge；Edge
        终态回来时 return_info.error_resolution 让调度器决定是否追加重试 attempt。"""

        with self._lock:
            pending = self._pending_decisions.get(decision_id)
        if pending is None:
            return False
        report = pending["report"]
        option = decision.get("option")
        if isinstance(option, Mapping):
            selected = str(option.get("action") or "abort")
            result = option.get("result", option.get("return_value"))
        else:
            selected = str(decision.get("action") or option or "abort")
            result = None
        if "result" in decision or "return_value" in decision:
            result = decision.get("result", decision.get("return_value"))
        options = {
            str(item.get("action"))
            for item in (report.get("options") or (report.get("error_info") or {}).get("options") or [])
            if isinstance(item, Mapping)
        }
        if options and selected not in options:
            return False
        if selected == "retry" and int(report.get("retry_count") or 0) >= int(
            report.get("max_retries") or 0
        ):
            logger.warning(
                "[EdgeControl] retry rejected for decision %s: attempt limit %s reached",
                decision_id,
                report.get("max_retries"),
            )
            return False
        replace = selected == "operator_intervention" and result is not None
        content = ErrorDecisionContent(
            decision_uuid=decision_id,
            confirmed_scheduler_revision=int(pending["required_scheduler_revision"]),
            adapter_command_uuid=str(uuid.uuid4()),
            selected_action=selected,
            reason=str(decision.get("reason") or ""),
            result=result if replace else None,
            actor_uuid=str(decision.get("actor_uuid") or "") or None,
        )
        with self._lock:
            self._pending_decisions.pop(decision_id, None)
        if selected == "wait":
            # execution_timeout 软超时：动作仍在执行，Edge 关闭闸门并重新计时；
            # 调度器把 attempt / 节点运行从 intervention_required 收回 running。
            self._issue_command(
                command_type="resume_pending",
                job_uuid=str(pending["job_uuid"]),
                payload=content.model_dump(mode="json", exclude_none=True),
            )
            self._publish_decision_resumed(
                {
                    **report,
                    "selected_action": "wait",
                    "reason": content.reason,
                    "resolved_at": time.time(),
                }
            )
            return True
        self._issue_command(
            command_type="replace_result" if replace else "release_failed",
            job_uuid=str(pending["job_uuid"]),
            payload=content.model_dump(mode="json", exclude_none=True),
        )
        return True

    def _publish_decision_resumed(self, report: Mapping[str, Any]) -> None:
        for bridge in list(self.result_bridges):
            callback = getattr(bridge, "publish_job_error_decision_resumed", None)
            if callable(callback):
                try:
                    callback(deepcopy(dict(report)))
                except Exception:  # noqa: BLE001 - 单个 bridge 失败不影响其他 bridge
                    logger.exception("[EdgeControl] publish_job_error_decision_resumed 失败")

    def _record_error_resumed(self, notice: EdgeChangeNotice) -> None:
        """Edge 自行收回了软超时决策（动作在等待期间真实完成，或 wait 已落地）。"""

        job_uuid = notice.job_uuid or notice.aggregate_uuid
        with self._lock:
            resumed = [
                (decision_id, item)
                for decision_id, item in self._pending_decisions.items()
                if item["job_uuid"] == job_uuid
            ]
            for decision_id, _ in resumed:
                self._pending_decisions.pop(decision_id, None)
        for decision_id, item in resumed:
            logger.info("[EdgeControl] job %s 的软超时决策 %s 已收回", job_uuid, decision_id)
            self._publish_decision_resumed(
                {
                    **item["report"],
                    "selected_action": "superseded",
                    "reason": "execution.error_resumed",
                    "resolved_at": time.time(),
                }
            )

    def _record_error_pending(self, notice: EdgeChangeNotice) -> None:
        job_uuid = notice.job_uuid or notice.aggregate_uuid
        snapshot = (
            self._payloads.fetch_json(notice.detail_payload_uuid)
            if notice.detail_payload_uuid
            else None
        )
        report = snapshot.get("report") if isinstance(snapshot, dict) else None
        if not isinstance(report, dict) or not report.get("decision_id"):
            logger.warning("[EdgeControl] job %s 的失败决策快照不完整，无法登记", job_uuid)
            return
        decision_id = str(report["decision_id"])
        with self._lock:
            inflight = self._inflight.get(job_uuid) or {}
            self._pending_decisions[decision_id] = {
                "job_uuid": job_uuid,
                "report": deepcopy(report),
                "required_scheduler_revision": int(inflight.get("scheduler_revision") or 0) + 1,
            }
        logger.info("[EdgeControl] job %s 失败等待决策 (decision=%s)", job_uuid, decision_id)
        for bridge in list(self.result_bridges):
            callback = getattr(bridge, "publish_job_error_decision_required", None)
            if callable(callback):
                try:
                    callback(deepcopy(report))
                except Exception:  # noqa: BLE001 - 单个 bridge 失败不影响决策登记
                    logger.exception("[EdgeControl] publish_job_error_decision_required 失败")

    def wait_idle(self, timeout: float = 5.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if not self._inflight:
                    return True
            time.sleep(0.05)
        return False

    def active_job_ids(self) -> list[str]:
        with self._lock:
            return list(self._inflight)

    def host_ready(self) -> bool:
        """执行端是否可派发：Edge 控制面已连接。"""

        return self.connected

    def _publish_host_ready(self) -> None:
        for bridge in list(self.result_bridges):
            callback = getattr(bridge, "resume_pending_dispatches", None)
            if callable(callback):
                try:
                    callback()
                except Exception:  # noqa: BLE001 - 下次重连仍可恢复
                    logger.exception("[EdgeControl] resume_pending_dispatches 失败")

    # ── 命令签发与文档服务 ───────────────────────────────────

    def _issue_command(
        self,
        *,
        command_type: str,
        job_uuid: str,
        payload: dict[str, Any],
    ) -> str:
        payload_sha256 = canonical_hash(payload)
        command_uuid = str(uuid.uuid4())
        with self._lock:
            self._sequence += 1
            sequence = self._sequence
            envelope = CommandEnvelope(
                command_uuid=command_uuid,
                session_uuid=self.session_uuid,
                backend_sequence=sequence,
                command_type=command_type,  # type: ignore[arg-type]
                job_uuid=job_uuid,
                payload_uuid=str(uuid.uuid4()),
                payload_sha256=payload_sha256,
                received_at_ms=_now_ms(),
            )
            self._commands[command_uuid] = BackendCommandDocument(
                command=envelope,
                payload=payload,
            )
            notice = BackendCommandNotice(
                notice_uuid=str(uuid.uuid4()),
                command_uuid=command_uuid,
                command_type=command_type,  # type: ignore[arg-type]
                session_uuid=self.session_uuid,
                backend_sequence=sequence,
                edge_uuid=self.edge_uuid,
                authority_epoch=self.authority_epoch,
                connection_epoch=self.connection_epoch,
                content_sha256=payload_sha256,
                occurred_at_ms=_now_ms(),
            )
            self._notices[command_uuid] = notice
        self._queue_send(
            {
                "action": "backend_change",
                "data": notice.model_dump(mode="json", exclude_none=True),
            }
        )
        logger.info(
            "[EdgeControl] 命令已签发 %s (%s) job=%s seq=%d",
            command_uuid,
            command_type,
            job_uuid,
            sequence,
        )
        return command_uuid

    def get_command_document(self, command_uuid: str) -> Optional[dict[str, Any]]:
        with self._lock:
            document = self._commands.get(command_uuid)
            if document is not None:
                self._fetched.add(command_uuid)
        if document is None:
            return None
        return document.model_dump(mode="json", exclude_none=True)

    # ── WS 连接生命周期（API 层调用） ─────────────────────────

    def attach_connection(self, edge_process: str = "") -> tuple[str, dict[str, Any]]:
        """新 WS 连接：刷新 connection_epoch，返回 (epoch, backend_session 消息)。

        后来者赢：旧连接的发送协程发现 epoch 不匹配后自行退出。``edge_process`` 是 Host
        进程身份；与上一次不同说明 Host 进程重启过，上一进程已开始执行的 job 改为结果未知。
        """

        with self._lock:
            self.connection_epoch = str(uuid.uuid4())
            epoch = self.connection_epoch
            self._connected = True
            self._drain_outgoing()
            lost = self._take_lost_jobs(str(edge_process or "").strip())
            notice = BackendSessionNotice(
                session_uuid=self.session_uuid,
                edge_uuid=self.edge_uuid,
                authority_epoch=self.authority_epoch,
                connection_epoch=epoch,
                occurred_at_ms=_now_ms(),
            )
            # 断线期间签发但 Edge 尚未拉取的命令：重发轻通知
            pending = [
                self._notices[command_uuid]
                for command_uuid in self._commands
                if command_uuid not in self._fetched
            ]
        logger.info(
            "[EdgeControl] Edge 已连接 (session=%s, connection=%s, 补发命令=%d)",
            self.session_uuid,
            epoch,
            len(pending),
        )
        from unilabos.utils.log_notices import log_notices

        log_notices.changed(sources_changed=True, all_sources=True)
        for notice_item in sorted(pending, key=lambda item: item.backend_sequence):
            resend = notice_item.model_copy(update={"connection_epoch": epoch})
            self.outgoing.put(
                {
                    "action": "backend_change",
                    "data": resend.model_dump(mode="json", exclude_none=True),
                }
            )
        if lost:
            # 回调会落库并开裁决，同样不能阻塞 WS 事件循环
            threading.Thread(
                target=self._notify_lost_jobs, args=(lost,), name="EdgeControlLostJobs", daemon=True
            ).start()
        # attach 在 WS 事件循环线程里被调用；重算等待集合会碰数据库，放到独立线程
        threading.Thread(
            target=self._publish_host_ready, name="EdgeControlHostReady", daemon=True
        ).start()
        return epoch, {
            "action": "backend_session",
            "data": notice.model_dump(mode="json", exclude_none=True),
        }

    def _take_lost_jobs(self, process: str) -> list[str]:
        """Host 进程换了身份时取出上一进程可能执行到一半的 job（调用方持有 ``_lock``）。

        上一进程已拉取执行命令的 job 可能已在设备上执行、随进程退出而中断，结果无法确认；
        尚未拉取的命令从未开始，留在待补发集合里照常发给新进程。网络断线重连身份不变，不受影响。
        """

        previous = self._edge_process
        if process:
            self._edge_process = process
        if not process or not previous or process == previous:
            return []
        started = {
            document.command.job_uuid
            for command_uuid, document in self._commands.items()
            if command_uuid in self._fetched and document.command.command_type == "execute_job"
        }
        lost = [job_uuid for job_uuid in self._inflight if job_uuid in started]
        for job_uuid in lost:
            self._inflight.pop(job_uuid, None)
            for decision_id in [
                key for key, item in self._pending_decisions.items() if item["job_uuid"] == job_uuid
            ]:
                self._pending_decisions.pop(decision_id, None)
            for command_uuid in [
                key for key, document in self._commands.items() if document.command.job_uuid == job_uuid
            ]:
                self._commands.pop(command_uuid, None)
                self._notices.pop(command_uuid, None)
                self._fetched.discard(command_uuid)
        return lost

    def _notify_lost_jobs(self, job_uuids: list[str]) -> None:
        with self._lock:
            listeners = list(self._listeners)
        reason = "执行端（Host）进程在动作进行中重启，无法确认设备是否已完成该动作"
        logger.warning(
            "[EdgeControl] Host 进程已重启：%d 个执行中的 job 结果未知，转人工裁决：%s",
            len(job_uuids),
            ", ".join(job_uuids),
        )
        for job_uuid in job_uuids:
            for listener in listeners:
                try:
                    listener(job_uuid, False, None, SUCCESS_TYPE_EXECUTION_UNKNOWN, {"uncertainty_reason": reason})
                except Exception:  # noqa: BLE001 - 与终态回调一致，互不影响
                    logger.exception("[EdgeControl] lost job listener 失败")

    def detach_connection(self, epoch: str) -> None:
        """断开指定代际的连接；旧代际的 detach 不影响新连接。"""

        with self._lock:
            if epoch != self.connection_epoch:
                return
            self._connected = False
            # 未消费的下行通知随连接作废：命令权威仍在 _commands，
            # 重连 attach 时按未拉取集合补发。
            self._drain_outgoing()
        # 正在等 Edge 回 HTTP 结果的调用方立刻拿到 None，而不是干等到超时
        self._fail_http_waiters()
        logger.info("[EdgeControl] Edge 连接已断开")
        from unilabos.utils.log_notices import log_notices

        log_notices.changed(sources_changed=True, all_sources=True)

    def _drain_outgoing(self) -> None:
        while True:
            try:
                self.outgoing.get_nowait()
            except queue.Empty:
                break

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._connected

    def _queue_send(self, message: dict[str, Any]) -> None:
        with self._lock:
            if not self._connected:
                logger.warning(
                    "[EdgeControl] Edge 未连接，丢弃下行 %s（命令权威仍在存储中）",
                    message.get("action"),
                )
                return
        self.outgoing.put(message)

    # ── Edge 上行处理 ────────────────────────────────────────

    def handle_message(self, action: str, data: dict[str, Any]) -> Optional[dict[str, Any]]:
        """处理一条 Edge 上行消息，返回需要回发的消息（如 ack/pong）。"""

        if action == "runtime_logs_changed":
            from unilabos.protocol.runtime.logs import RuntimeLogNotice
            from unilabos.utils.log_notices import log_notices

            log_notices.publish(RuntimeLogNotice.model_validate(data))
            return None
        if action == "ping":
            # ping/pong 是控制面的快速诊断消息，不经过命令/事件协调器。
            # 严格重建字段，避免把旧协议或业务正文透传到 runtime.v1。
            ping = PingNotice.model_validate(data)
            pong = PongNotice(
                ping_id=ping.ping_id,
                client_timestamp=ping.client_timestamp,
                server_timestamp=time.time(),
            )
            return {
                "action": "pong",
                "data": pong.model_dump(mode="json"),
            }
        if action == "pong":
            # Backend 不需要消费 Edge 的 pong，但仍校验其特殊字段，避免
            # malformed 心跳进入业务处理路径。
            PongNotice.model_validate(data)
            return None
        if action == "edge_change":
            return self._handle_edge_change(data)
        logger.warning("[EdgeControl] 忽略不支持的上行 action: %s", action)
        return None

    def _handle_edge_change(self, data: dict[str, Any]) -> dict[str, Any]:
        notice = EdgeChangeNotice.model_validate(data)
        with self._lock:
            duplicate = notice.event_sequence <= self._last_edge_sequence
            if not duplicate:
                self._last_edge_sequence = notice.event_sequence
        if not duplicate:
            try:
                self._apply_edge_event(notice)
            except Exception:  # noqa: BLE001 - 单条事件失败不阻塞 ack 推进
                logger.exception(
                    "[EdgeControl] 处理 edge 事件 %s (%s) 失败",
                    notice.event_uuid,
                    notice.event_type,
                )
        ack = EdgeChangeAck(
            session_uuid=self.session_uuid,
            through_sequence=notice.event_sequence,
            acknowledged_at_ms=_now_ms(),
        )
        return {
            "action": "edge_change_ack",
            "data": ack.model_dump(mode="json", exclude_none=True),
        }

    def _apply_edge_event(self, notice: EdgeChangeNotice) -> None:
        if notice.event_type == "execution.error_pending":
            self._record_error_pending(notice)
            return
        if notice.event_type == "execution.error_resumed":
            self._record_error_resumed(notice)
            return
        terminal = _TERMINAL_EVENT_STATUS.get(notice.event_type)
        if terminal is None:
            logger.debug(
                "[EdgeControl] 记录非终态事件 %s (%s)",
                notice.event_type,
                notice.aggregate_uuid,
            )
            return
        status, success = terminal
        job_uuid = notice.job_uuid or notice.aggregate_uuid
        detail: dict[str, Any] = {}
        if notice.detail_payload_uuid:
            fetched = self._payloads.fetch_json(notice.detail_payload_uuid)
            if isinstance(fetched, dict):
                detail = fetched
        # detail 形状与 coordinator.publish_job_status 存储的一致：
        # {"status", "feedback_data", "return_info"}；return_info 出自
        # serialize_result_info，与本地执行端 listener 的取值口径对齐。
        return_info = detail.get("return_info")
        if not isinstance(return_info, dict):
            return_info = {}
        ret_value = return_info.get("return_value")
        suc_type = str(return_info.get("suc_type") or "normal")
        with self._lock:
            self._inflight.pop(job_uuid, None)
            for decision_id in [
                key for key, item in self._pending_decisions.items() if item["job_uuid"] == job_uuid
            ]:
                self._pending_decisions.pop(decision_id, None)
            finished_commands = [
                command_uuid
                for command_uuid, document in self._commands.items()
                if document.command.job_uuid == job_uuid
            ]
            for command_uuid in finished_commands:
                self._commands.pop(command_uuid, None)
                self._notices.pop(command_uuid, None)
                self._fetched.discard(command_uuid)
            listeners = list(self._listeners)
        logger.info(
            "[EdgeControl] job %s 终态 %s (success=%s, suc_type=%s)",
            job_uuid,
            status,
            success,
            suc_type,
        )
        # 与 JobExecutionBackend._notify_finished 同一签名：return_info 里的 error_resolution
        # 让调度器知道这是 retry 决策后的失败，从而追加新 attempt 而不是判定任务失败。
        for listener in listeners:
            try:
                listener(job_uuid, success, ret_value, suc_type, return_info)
            except Exception:  # noqa: BLE001 - 与本地执行端一致，回调互不影响
                logger.exception("[EdgeControl] job finished listener 失败")


_service: Optional[EdgeControlService] = None
_service_lock = threading.Lock()


def set_edge_control_service(service: Optional[EdgeControlService]) -> None:
    global _service
    with _service_lock:
        _service = service


def get_edge_control_service() -> Optional[EdgeControlService]:
    return _service


__all__ = [
    "EdgeControlService",
    "EdgePayloadClient",
    "get_edge_control_service",
    "set_edge_control_service",
]
