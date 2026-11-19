"""Backend 调度命令与 HostLink/ROS2 执行器之间的持久化业务协调层。"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from dataclasses import asdict, is_dataclass
from types import SimpleNamespace
from typing import Any, Callable, Optional

from unilabos.server.backend.execution_queue import JOB_ORIGIN_BACKEND_CONTROL
from unilabos.server.backend.legacy_adaptor.http import BackendHTTPClient
from unilabos.server.database.tables.runtime import ExecutionJobRecord
from unilabos.protocol.base import canonical_hash, canonical_json
from unilabos.protocol.runtime.control import (
    BackendCommandNotice,
    BackendSessionNotice,
    CancelJobContent,
    EdgeChangeAck,
    EdgeChangeNotice,
    ErrorDecisionContent,
    ExecuteJobContent,
)
from unilabos.protocol.history import (
    HistoryEventAppend,
    HistoryEventQuery,
    InlinePayloadWrite,
    ManualResultReplacement,
)
from unilabos.protocol.runtime import (
    AdapterCommandAck,
    AdapterCommandClaim,
    BackendEventAck,
    BackendEventClaim,
    BackendEventEnqueue,
    BackendSessionUpsert,
    EndpointSnapshotUpsert,
    ErrorGateDecision,
    ErrorGateOpen,
    ErrorGateResume,
    ExecutionJobCancel,
    ExecutionJobCreate,
    ExecutionJobFeedback,
    ExecutionJobTransition,
)
from unilabos.server.services.history import HistoryService
from unilabos.server.services.runtime import (
    RuntimeConflictError,
    RuntimeNotFoundError,
    RuntimeService,
)


logger = logging.getLogger(__name__)


class WorkflowBusinessCoordinator:
    """只执行 Backend 已调度的 attempt，不在 Edge 构建 DAG 或 retry。"""

    #: 只拥有自己经 runtime.v1 ``execute_job`` 建档并派发的 job 的生命周期；
    #: 本机调度 / 旧协议派发的 job 不会路由到这里。
    job_origins = frozenset({JOB_ORIGIN_BACKEND_CONTROL})

    def __init__(
        self,
        runtime: RuntimeService,
        history: HistoryService,
        executor: Any,
        *,
        endpoint_uuid: str,
        transport: str,
        host_uuid: str,
        instance_name: str,
        data_plane: Optional[BackendHTTPClient] = None,
        notice_callback: Optional[Callable[[], None]] = None,
    ) -> None:
        if transport not in {"hostlink", "ros2"}:
            raise ValueError("execution transport must be hostlink or ros2")
        self.runtime = runtime
        self.history = history
        self.executor = executor
        self.endpoint_uuid = endpoint_uuid
        self.transport = transport
        self.host_uuid = host_uuid
        self.instance_name = instance_name
        # 懒初始化：本机调度（未配置云端地址）时不创建 HTTP 数据面客户端；
        # Backend-controlled 模式首个通知到达时 remote_addr 必已配置。
        self._data_plane = data_plane
        self.notice_callback = notice_callback
        self.adapter_epoch = str(uuid.uuid4())
        self._active_session_uuid: Optional[str] = None
        self._lock = threading.RLock()
        self._capability_snapshot_hash = ""
        self.runtime.upsert_endpoint_snapshot(
            EndpointSnapshotUpsert(
                endpoint_uuid=endpoint_uuid,
                transport=transport,
                host_uuid=host_uuid,
                instance_name=instance_name,
                authority_epoch=f"edge:{host_uuid}",
                adapter_epoch=self.adapter_epoch,
                state="online",
            )
        )

    def publish_endpoint_capabilities(self) -> None:
        """把执行适配器的设备/动作能力投影进 endpoint 快照。

        微前端设备页、单点动作表单与工作流画布节点目录以该快照为数据源。
        由 ``JobExecutionBackend.publish_host_ready``（执行器就绪 / 设备增减）
        触发；内容哈希去重，未变化不重复写库。
        """

        import time

        adapter_getter = getattr(self.executor, "execution_adapter", None)
        adapter = adapter_getter() if callable(adapter_getter) else None
        if adapter is None:
            return
        from unilabos.server.backend.capabilities import build_endpoint_capabilities

        now_ms = int(time.time() * 1000)
        routes, capabilities = build_endpoint_capabilities(
            adapter, observed_at_ms=now_ms
        )
        snapshot_hash = canonical_hash(
            {
                "routes": [route.model_dump(mode="json") for route in routes],
                "capabilities": [
                    {
                        key: value
                        for key, value in capability.model_dump(mode="json").items()
                        if key != "observed_at_ms"
                    }
                    for capability in capabilities
                ],
            }
        )
        with self._lock:
            if snapshot_hash == self._capability_snapshot_hash:
                return
            self._capability_snapshot_hash = snapshot_hash
            self.runtime.upsert_endpoint_snapshot(
                EndpointSnapshotUpsert(
                    endpoint_uuid=self.endpoint_uuid,
                    transport=self.transport,  # type: ignore[arg-type]
                    host_uuid=self.host_uuid,
                    instance_name=self.instance_name,
                    authority_epoch=f"edge:{self.host_uuid}",
                    adapter_epoch=self.adapter_epoch,
                    state="online",
                    device_routes=routes,
                    action_capabilities=capabilities,
                    observed_at_ms=now_ms,
                )
            )
        logger.info(
            "[Coordinator] endpoint 能力快照已更新：%d 设备 / %d 动作",
            len(routes),
            len(capabilities),
        )

    @property
    def data_plane(self) -> BackendHTTPClient:
        if self._data_plane is None:
            self._data_plane = BackendHTTPClient()
        return self._data_plane

    # -- WS notice -> HTTP command -------------------------------------

    def handle_backend_notice(self, value: BackendCommandNotice | dict[str, Any]) -> None:
        """拉取、校验、持久化并应用一条 Backend 命令。"""

        notice = (
            value
            if isinstance(value, BackendCommandNotice)
            else BackendCommandNotice.model_validate(value)
        )
        with self._lock:
            self._bind_session(notice)
            document = self.data_plane.fetch_command(notice.command_uuid)
            command = document.command
            expected_identity = (
                notice.command_uuid,
                notice.command_type,
                notice.session_uuid,
                notice.backend_sequence,
            )
            actual_identity = (
                command.command_uuid,
                command.command_type,
                command.session_uuid,
                command.backend_sequence,
            )
            if actual_identity != expected_identity:
                raise ValueError("HTTP command identity does not match WS notice")
            payload_sha256 = canonical_hash(document.payload)
            if payload_sha256 != notice.content_sha256:
                raise ValueError("HTTP command content does not match WS notice hash")
            if payload_sha256 != command.payload_sha256:
                raise ValueError("HTTP command content does not match command hash")
            if command.payload_uuid is None:
                raise ValueError("business command requires payload_uuid")

            self._store_json(document.payload, payload_uuid=command.payload_uuid)
            self.runtime.receive_command(command)
            if command.command_type == "execute_job":
                self._apply_execute(command.command_uuid, document.payload)
            elif command.command_type in {"release_failed", "replace_result"}:
                self._apply_error_decision(
                    command.command_uuid,
                    command.command_type,
                    command.job_uuid or "",
                    command.payload_uuid,
                    document.payload,
                )
            elif command.command_type == "resume_pending":
                self._apply_error_resume(
                    command.command_uuid,
                    command.job_uuid or "",
                    command.payload_uuid,
                    document.payload,
                )
            elif command.command_type == "cancel_job":
                self._apply_cancel(
                    command.command_uuid,
                    command.job_uuid or "",
                    command.payload_uuid,
                    document.payload,
                )
            else:
                raise ValueError(
                    f"command type {command.command_type!r} has no workflow handler"
                )

    def _bind_session(self, notice: BackendCommandNotice) -> None:
        self.bind_backend_session(
            BackendSessionNotice(
                session_uuid=notice.session_uuid,
                edge_uuid=notice.edge_uuid,
                authority_epoch=notice.authority_epoch,
                connection_epoch=notice.connection_epoch,
                occurred_at_ms=notice.occurred_at_ms,
            )
        )

    def bind_backend_session(
        self, value: BackendSessionNotice | dict[str, Any]
    ) -> None:
        """建立/恢复 WS session，并立即尝试重放未 ACK 的短通知。"""

        notice = (
            value
            if isinstance(value, BackendSessionNotice)
            else BackendSessionNotice.model_validate(value)
        )
        backend_uri = str(getattr(self.data_plane, "base_url", "backend-http"))
        self.runtime.upsert_backend_session(
            BackendSessionUpsert(
                session_uuid=notice.session_uuid,
                edge_uuid=notice.edge_uuid,
                backend_uri=backend_uri,
                authority_epoch=notice.authority_epoch,
                connection_epoch=notice.connection_epoch,
                state="active",
                observed_at_ms=notice.occurred_at_ms,
            )
        )
        self._active_session_uuid = notice.session_uuid
        self._notify()

    def _apply_execute(self, command_uuid: str, payload: dict[str, Any]) -> None:
        content = ExecuteJobContent.model_validate(payload)
        command = self.runtime.get_command(command_uuid)
        if command.job_uuid != content.job_uuid:
            raise ValueError("execute command and payload job_uuid do not agree")
        if content.endpoint_uuid not in {None, self.endpoint_uuid}:
            raise ValueError("job was routed to another executor endpoint")
        if content.transport not in {None, self.transport}:
            raise ValueError("job transport does not match this executor")
        route_uuid = content.route_uuid or (
            f"{self.endpoint_uuid}:{content.device_uuid}"
        )
        job = self.runtime.create_execution_job(
            ExecutionJobCreate(
                job_uuid=content.job_uuid,
                task_uuid=content.task_uuid,
                node_uuid=content.node_uuid,
                attempt_group_uuid=content.attempt_group_uuid,
                retry_of_job_uuid=content.retry_of_job_uuid,
                attempt_no=content.attempt_no,
                attempt_trigger=content.attempt_trigger,
                execute_command_uuid=command_uuid,
                device_uuid=content.device_uuid,
                action_name=content.action_name,
                action_payload_uuid=command.payload_uuid or "",
                route_uuid=route_uuid,
                endpoint_uuid=self.endpoint_uuid,
                transport=self.transport,
                material_bindings=content.material_bindings,
                scheduler_revision=content.scheduler_revision,
            )
        )
        if job.status == "accepted":
            job = self.runtime.transition_execution_job(
                job.job_uuid,
                ExecutionJobTransition(
                    expected_version=job.version,
                    status="dispatch_pending",
                ),
            )
            self._emit_job_event(
                job,
                "execution.dispatch_pending",
                {"status": job.status, "command_uuid": command_uuid},
            )
        if job.status == "dispatch_pending":
            self._dispatch(content)

    def _dispatch(self, content: ExecuteJobContent) -> None:
        self.executor.dispatch(
            {
                "execution_permit": content.execution_permit.model_dump(mode="json") if content.execution_permit else None,
                "resource_contract": content.resource_contract,
                "material_execution": content.material_execution,
                "job_id": content.job_uuid,
                "task_id": content.task_uuid,
                "node_id": content.node_uuid,
                "device_id": content.device_uuid,
                "action": content.action_name,
                "action_type": content.action_type,
                "action_args": content.action_args,
                "materials_need_lock": sorted(
                    set(content.materials_need_lock)
                    | set(
                        self._action_materials_need_lock(
                            content.device_uuid, content.action_name
                        )
                    )
                ),
                "sample_material": content.sample_material,
                "inventory_requirements": [
                    item.model_dump(mode="json", exclude_none=False)
                    for item in content.inventory_requirements
                ],
                "inventory_reservation_uuid": content.inventory_reservation_uuid,
                "scheduler_revision": content.scheduler_revision,
                "server_info": content.server_info,
                "notebook_id": content.notebook_uuid,
                "node_run_uuid": content.attempt_group_uuid,
                "attempt_no": content.attempt_no,
                "retry_of_job_uuid": content.retry_of_job_uuid,
                "attempt_trigger": content.attempt_trigger,
                # 循环体每轮的 attempt 不是重试：调度权威给出的 retry_count 优先
                "retry_count": (
                    content.retry_count
                    if content.retry_count is not None
                    else (content.attempt_no - 1 if content.retry_of_job_uuid else 0)
                ),
                "origin": JOB_ORIGIN_BACKEND_CONTROL,
                "always_free": self._action_always_free(
                    content.device_uuid, content.action_name
                ),
                # 调度权威已解析的超时；缺省时执行面按本地注册表副本自行解析
                "timeout_seconds": content.timeout_seconds,
                "execution_timeout_seconds": content.execution_timeout_seconds,
            }
        )

    def _action_always_free(self, device_uuid: str, action_name: str) -> bool:
        try:
            adapter = self.executor._host_node_getter()
            mappings = getattr(adapter, "_action_value_mappings", {})
            actions = mappings.get(device_uuid, {}) if isinstance(mappings, dict) else {}
            for candidate in (action_name, f"auto-{action_name}"):
                value = actions.get(candidate)
                if isinstance(value, dict):
                    return bool(value.get("always_free", False))
        except Exception:  # noqa: BLE001 - capability hint cannot reject a job
            pass
        return False

    def _action_materials_need_lock(
        self, device_uuid: str, action_name: str
    ) -> list[str]:
        try:
            adapter = self.executor._host_node_getter()
            mappings = getattr(adapter, "_action_value_mappings", {})
            actions = mappings.get(device_uuid, {}) if isinstance(mappings, dict) else {}
            for candidate in (action_name, f"auto-{action_name}"):
                value = actions.get(candidate)
                if isinstance(value, dict):
                    return list(value.get("materials_need_lock") or [])
        except Exception:  # noqa: BLE001 - capability hint cannot reject a job
            pass
        return []

    def _apply_error_decision(
        self,
        command_uuid: str,
        command_type: str,
        job_uuid: str,
        payload_uuid: str,
        payload: dict[str, Any],
    ) -> None:
        content = ErrorDecisionContent.model_validate(payload)
        job = self.runtime.get_execution_job(job_uuid)
        if content.decision_uuid != job.terminal_error_uuid:
            raise ValueError("decision does not match the pending job error")
        action = "replace_result" if command_type == "replace_result" else "release_failed"
        result_uuid: Optional[str] = None
        if action == "replace_result":
            if content.result is None:
                raise ValueError("manual result replacement requires result")
            result_uuid = self._store_json(
                {"result": content.result, "decision_uuid": content.decision_uuid}
            ).payload_uuid
        decision = content.model_dump(mode="json", exclude_none=True)
        decision["action"] = action
        updated = self.runtime.decide_error_gate(
            job_uuid,
            ErrorGateDecision(
                expected_version=job.version,
                decision_command_uuid=command_uuid,
                action=action,
                confirmed_scheduler_revision=content.confirmed_scheduler_revision,
                adapter_command_uuid=content.adapter_command_uuid,
                payload_uuid=payload_uuid,
                result_uuid=result_uuid,
                decision=decision,
            ),
        )
        self._append_decision_audit(updated, payload_uuid, content)
        self.drain_adapter_commands()

    def _apply_error_resume(
        self,
        command_uuid: str,
        job_uuid: str,
        payload_uuid: str,
        payload: dict[str, Any],
    ) -> None:
        """Backend 对 ``execution_timeout`` 软超时决策选择 ``wait``：关闭终态闸门，job 回到 running，
        再经 adapter outbox 让执行面重新计时。"""

        content = ErrorDecisionContent.model_validate(payload)
        if content.selected_action != "wait":
            raise ValueError("resume_pending only carries the wait decision")
        job = self.runtime.get_execution_job(job_uuid)
        if content.decision_uuid != job.terminal_error_uuid:
            raise ValueError("resume does not match the pending job error")
        decision = content.model_dump(mode="json", exclude_none=True)
        decision["action"] = "resume_pending"
        updated = self.runtime.resume_error_gate(
            job_uuid,
            ErrorGateResume(
                expected_version=job.version,
                error_uuid=content.decision_uuid,
                reason=content.reason or "wait",
                decision_command_uuid=command_uuid,
                adapter_command_uuid=content.adapter_command_uuid,
                payload_uuid=payload_uuid,
                decision=decision,
            ),
        )
        self._append_decision_audit(updated, payload_uuid, content)
        self.drain_adapter_commands()

    def publish_job_error_decision_resumed(self, report: dict[str, Any]) -> bool:
        """执行面决策桥：软超时决策被收回（``wait`` 已由 adapter 命令落地，或动作在等待期间真实完成）。

        Backend 主导的 ``wait`` 到这里时闸门已经关闭（``_apply_error_resume``），幂等返回；
        执行面自行收回（真实结果先到）时在此关闭闸门，并让 ``execution.error_resumed`` 事件
        告知 Backend 撤销待决策。
        """

        item = SimpleNamespace(job_id=str(report.get("job_id") or ""))
        job = self._owned_job(item, "resume")
        if job is None:
            return False
        if job.terminal_gate_state == "none":
            return True
        decision_id = str(report.get("decision_id") or "")
        if decision_id and job.terminal_error_uuid not in {None, decision_id}:
            return False
        try:
            self.runtime.resume_error_gate(
                job.job_uuid,
                ErrorGateResume(
                    expected_version=job.version,
                    error_uuid=str(job.terminal_error_uuid or decision_id),
                    reason=str(report.get("reason") or report.get("selected_action") or ""),
                    decision={
                        "selected_action": str(report.get("selected_action") or ""),
                        "decision_id": decision_id,
                    },
                ),
            )
        except RuntimeConflictError as exc:
            logger.warning(
                "[Coordinator] cannot resume error gate for job %s: %s", job.job_uuid, exc
            )
            return False
        self._notify()
        return True

    def _apply_cancel(
        self,
        command_uuid: str,
        job_uuid: str,
        payload_uuid: str,
        payload: dict[str, Any],
    ) -> None:
        """通过 runtime adapter outbox 取消，不绕过命令幂等记录。"""

        content = CancelJobContent.model_validate(payload)
        job = self.runtime.get_execution_job(job_uuid)
        self.runtime.request_execution_cancel(
            job_uuid,
            ExecutionJobCancel(
                expected_version=job.version,
                cancel_command_uuid=command_uuid,
                adapter_command_uuid=content.adapter_command_uuid,
                payload_uuid=payload_uuid,
            ),
        )
        self.drain_adapter_commands()

    # -- Executor result bridge ----------------------------------------

    def _owned_job(self, item: Any, callback: str) -> Optional[ExecutionJobRecord]:
        """执行面只把本协调器派发（origin=backend_control）的 job 路由到这里；
        查不到建档记录说明 execute_job 建档与派发之间出现了不一致。"""

        try:
            return self.runtime.get_execution_job(str(item.job_id))
        except RuntimeNotFoundError:
            logger.warning(
                "%s callback for job %s has no execution_job record", callback, item.job_id
            )
            return None

    def publish_job_started(self, item: Any) -> None:
        job = self._owned_job(item, "start")
        if job is None:
            return
        if job.status == "dispatch_pending":
            job = self.runtime.transition_execution_job(
                job.job_uuid,
                ExecutionJobTransition(
                    expected_version=job.version,
                    status="dispatched",
                ),
            )
            self._emit_job_event(job, "execution.dispatched", {"status": job.status})

    def publish_job_status(
        self,
        feedback_data: dict[str, Any],
        item: Any,
        status: str,
        return_info: Optional[dict[str, Any]] = None,
    ) -> None:
        job = self._owned_job(item, "status")
        if job is None:
            return
        if status == "running":
            if job.status == "dispatch_pending":
                self.publish_job_started(item)
                job = self.runtime.get_execution_job(job.job_uuid)
            if job.status == "dispatched":
                job = self.runtime.transition_execution_job(
                    job.job_uuid,
                    ExecutionJobTransition(
                        expected_version=job.version,
                        status="running",
                    ),
                )
                self._emit_job_event(job, "execution.running", {"status": job.status})
            if feedback_data:
                self._emit_feedback(job, feedback_data)
            return
        if status not in {"success", "failed", "canceled"}:
            return
        latest = self.runtime.get_execution_job(job.job_uuid)
        if latest.status in {"succeeded", "failed", "canceled", "rejected"}:
            return
        payload = {
            "status": status,
            "feedback_data": feedback_data or {},
            "return_info": return_info or {},
        }
        result_payload = self._store_json(payload)
        target_status = {
            "success": "succeeded",
            "failed": "failed",
            "canceled": "canceled",
        }[status]
        from unilabos.registry.action_policy import SUCCESS_TYPE_EXECUTION_UNKNOWN
        if (return_info or {}).get("suc_type") == SUCCESS_TYPE_EXECUTION_UNKNOWN:
            target_status = "execution_unknown"
            if latest.status == target_status:
                return
        transition = ExecutionJobTransition(
            expected_version=latest.version,
            status=target_status,
            result_uuid=result_payload.payload_uuid,
            error_code=(
                str((return_info or {}).get("error_info", {}).get("exception_type") or "execution_error")
                if target_status == "failed"
                else None
            ),
            error_summary=(
                str((return_info or {}).get("error") or "execution failed")
                if target_status == "failed"
                else None
            ),
        )
        # 极快的动作里 running 与终态回调可能在不同线程几乎同时到达：乐观版本冲突时
        # 重读记录再提交，而不是让 job 永远停在非终态。
        for _attempt in range(5):
            try:
                latest = self.runtime.transition_execution_job(latest.job_uuid, transition)
                break
            except RuntimeConflictError as exc:
                if "version" not in str(exc):
                    raise
                latest = self.runtime.get_execution_job(job.job_uuid)
                if latest.status in {"succeeded", "failed", "canceled", "rejected"}:
                    return
                transition = transition.model_copy(update={"expected_version": latest.version})
        else:
            raise RuntimeConflictError(
                f"job {job.job_uuid} terminal transition kept conflicting on version"
            )
        event_uuid = str(uuid.uuid4())
        if latest.terminal_gate_state == "result_replaced":
            raw_event = self._raw_failure_event(latest.job_uuid)
            if raw_event is not None:
                actor_uuid = str(latest.terminal_decision.get("actor_uuid") or "operator")
                self.history.append_replacement(
                    ManualResultReplacement(
                        supersedes_event_uuid=raw_event.event_uuid,
                        event_uuid=event_uuid,
                        payload_uuid=result_payload.payload_uuid,
                        summary={"status": target_status},
                        actor_uuid=actor_uuid,
                    )
                )
            else:
                self._append_result_event(latest, event_uuid, result_payload.payload_uuid)
        else:
            self._append_result_event(latest, event_uuid, result_payload.payload_uuid)
        self._enqueue_backend_event(
            latest,
            event_uuid,
            f"execution.{target_status}",
            result_payload.payload_uuid,
            {"status": target_status},
        )

    def publish_job_late_outcome(self, report: dict[str, Any], item: Any = None) -> bool:
        """超时结算后驱动真实返回（ADR 0008）：存证并以 ``execution.late_outcome`` 上报调度权威。

        execution_job 已是终态，不再改动；迟到结果只是附加事实，由调度权威据此释放该 attempt
        仍持有的锁。
        """

        job = self._owned_job(item, "late_outcome") if item is not None else None
        if job is None:
            return False
        status = str(report.get("status") or "")
        payload = self._store_json({"status": status, "return_info": report.get("return_info") or {},
                                    "report": report})
        event_uuid = str(uuid.uuid4())
        self.history.append_event(
            HistoryEventAppend(
                event_uuid=event_uuid,
                event_type="job_result",
                job_uuid=job.job_uuid,
                endpoint_uuid=job.endpoint_uuid,
                device_uuid=job.device_uuid,
                action_name=job.action_name,
                event_key="late_outcome",
                payload_uuid=payload.payload_uuid,
                summary={"status": status, "effective": False},
                actor_type="device",
                actor_uuid=job.device_uuid,
            )
        )
        self._enqueue_backend_event(
            job, event_uuid, "execution.late_outcome", payload.payload_uuid, {"status": status}
        )
        return True

    def publish_job_error_pending(
        self,
        report: dict[str, Any],
        item: Any,
        return_info: dict[str, Any],
        result_data: dict[str, Any],
        error_info: Optional[dict[str, Any]] = None,
    ) -> bool:
        """先持久化完整失败快照，再打开 Backend 控制的终态闸门。"""

        job = self._owned_job(item, "error")
        if job is None:
            return False
        item_data = asdict(item) if is_dataclass(item) else dict(vars(item))
        item_data.pop("trace_context", None)
        raw_payload = self._store_json(
            {
                "status": "failed",
                "return_info": return_info,
                "result_data": result_data,
            }
        )
        raw_event_uuid = str(uuid.uuid4())
        self.history.append_event(
            HistoryEventAppend(
                event_uuid=raw_event_uuid,
                event_type="job_result",
                job_uuid=job.job_uuid,
                endpoint_uuid=job.endpoint_uuid,
                device_uuid=job.device_uuid,
                action_name=job.action_name,
                event_key="raw_device_failure",
                state_version=job.version,
                payload_uuid=raw_payload.payload_uuid,
                summary={"status": "failed", "effective": False},
                severity=str(report.get("severity") or "error"),
                actor_type="device",
                actor_uuid=job.device_uuid,
            )
        )
        snapshot = {
            "report": report,
            "item": item_data,
            "return_info": return_info,
            "result_data": result_data,
            "error_info": error_info or return_info.get("error_info") or {},
            "raw_result_event_uuid": raw_event_uuid,
        }
        snapshot_payload = self._store_json(snapshot)
        event_uuid = str(uuid.uuid4())
        self.history.append_event(
            HistoryEventAppend(
                event_uuid=event_uuid,
                event_type="error_snapshot",
                job_uuid=job.job_uuid,
                endpoint_uuid=job.endpoint_uuid,
                device_uuid=job.device_uuid,
                action_name=job.action_name,
                event_key="terminal_error_pending",
                state_version=job.version,
                payload_uuid=snapshot_payload.payload_uuid,
                summary={
                    "error_uuid": str(report["decision_id"]),
                    "exception_type": str(report.get("exception_type") or "Exception"),
                },
                severity=str(report.get("severity") or "error"),
                actor_type="device",
                actor_uuid=job.device_uuid,
            )
        )
        self.runtime.open_error_gate(
            job.job_uuid,
            ErrorGateOpen(
                expected_version=job.version,
                error_uuid=str(report["decision_id"]),
                error_code=str(report.get("exception_type") or "execution_error"),
                error_summary=str(report.get("error_message") or "execution failed"),
                required_scheduler_revision=job.scheduler_revision + 1,
                request_event_uuid=event_uuid,
                detail_payload_uuid=snapshot_payload.payload_uuid,
                summary={
                    "device_uuid": job.device_uuid,
                    "action_name": job.action_name,
                    "category": str(report.get("category") or "execution"),
                    "severity": str(report.get("severity") or "error"),
                },
            ),
        )
        self._notify()
        return True

    # -- Durable adapter/outbound processing ---------------------------

    def drain_adapter_commands(self) -> int:
        commands = self.runtime.claim_adapter_commands(
            AdapterCommandClaim(endpoint_uuid=self.endpoint_uuid)
        )
        completed = 0
        for command in commands:
            if command.job_uuid is None:
                continue
            job = self.runtime.get_execution_job(command.job_uuid)
            applied = False
            if command.command_type in {"release_failed", "replace_result"}:
                decision = dict(job.terminal_decision)
                selected_action = str(decision.get("selected_action") or "abort")
                wire = {
                    "decision_id": job.terminal_error_uuid,
                    "job_id": job.job_uuid,
                    "device_id": job.device_uuid,
                    "action": selected_action,
                    "reason": str(decision.get("reason") or ""),
                    "scheduler_updated": True,
                }
                if "result" in decision:
                    wire["result"] = decision["result"]
                applied = bool(
                    self.executor.handle_action_error_decision(
                        str(job.terminal_error_uuid or ""), job.job_uuid, wire
                    )
                )
                if not applied:
                    resolved = self.executor.get_resolved_action_error_decision(
                        str(job.terminal_error_uuid or ""),
                        job.job_uuid,
                        job.device_uuid,
                    )
                    applied = resolved is not None
            elif command.command_type == "resume_pending":
                # 闸门已在 _apply_error_resume 关闭，decision_uuid 只剩命令正文里有
                content = self._load_json(command.payload_uuid) if command.payload_uuid else None
                decision_id = (
                    str(content.get("decision_uuid") or "") if isinstance(content, dict) else ""
                )
                wire = {
                    "decision_id": decision_id,
                    "job_id": job.job_uuid,
                    "device_id": job.device_uuid,
                    "action": "wait",
                    "reason": (
                        str(content.get("reason") or "") if isinstance(content, dict) else ""
                    ),
                    "scheduler_updated": True,
                }
                applied = bool(
                    self.executor.handle_action_error_decision(decision_id, job.job_uuid, wire)
                )
                if not applied:
                    # 决策已被执行面自行收回（动作在等待期间完成）或已重放：视为落地
                    resolved = self.executor.get_resolved_action_error_decision(
                        decision_id, job.job_uuid, job.device_uuid
                    )
                    applied = resolved is not None or job.status in {
                        "running",
                        "succeeded",
                        "failed",
                        "canceled",
                    }
            elif command.command_type == "cancel":
                applied = bool(self.executor.cancel_job(job.job_uuid))
            if not applied:
                continue
            self.runtime.acknowledge_adapter_command(
                AdapterCommandAck(
                    adapter_command_uuid=command.adapter_command_uuid,
                    ack_event_uuid=str(uuid.uuid4()),
                )
            )
            completed += 1
        return completed

    def claim_edge_changes(self, limit: int = 100) -> list[EdgeChangeNotice]:
        session_uuid = self._active_session_uuid
        if session_uuid is None:
            return []
        events = self.runtime.claim_backend_events(
            BackendEventClaim(session_uuid=session_uuid, limit=limit)
        )
        return [
            EdgeChangeNotice(
                session_uuid=session_uuid,
                event_uuid=event.event_uuid,
                event_sequence=event.sequence or 0,
                event_type=event.event_type,
                aggregate_type=event.aggregate_type,
                aggregate_uuid=event.aggregate_uuid,
                aggregate_version=event.aggregate_version,
                job_uuid=event.job_uuid,
                detail_payload_uuid=event.detail_payload_uuid,
            )
            for event in events
        ]

    def acknowledge_edge_changes(self, value: EdgeChangeAck | dict[str, Any]) -> int:
        ack = value if isinstance(value, EdgeChangeAck) else EdgeChangeAck.model_validate(value)
        return self.runtime.acknowledge_backend_events(
            BackendEventAck(
                session_uuid=ack.session_uuid,
                through_sequence=ack.through_sequence,
                acknowledged_at_ms=ack.acknowledged_at_ms,
            )
        )

    def restore(self) -> None:
        """重启后恢复待决失败；dispatch 必须等 Host/ROS adapter ready。

        本进程刚启动：库里仍是 dispatched / running 的 job 属于上一个 Host 进程，动作已随进程
        中断、结果无法确认，转 execution_unknown 且不重放。调度权威据 Host 身份变化另开人工裁决。
        """

        for status in ("dispatched", "running"):
            for job in self.runtime.list_execution_jobs(status=status, limit=1000):
                try:
                    self.runtime.transition_execution_job(
                        job.job_uuid,
                        ExecutionJobTransition(
                            expected_version=job.version,
                            status="execution_unknown",
                            error_code="host_restarted",
                            error_summary="Host 进程重启时该动作正在执行，结果无法确认",
                        ),
                    )
                except Exception:  # noqa: BLE001 - 单条失败不阻断启动
                    logger.exception("[Coordinator] 无法把遗留 job %s 标为 execution_unknown", job.job_uuid)
        restore_error = getattr(self.executor, "restore_action_error_decision", None)
        if callable(restore_error):
            for job in self.runtime.list_execution_jobs(status="terminal_waiting", limit=1000):
                events = self.history.query_events(
                    HistoryEventQuery(
                        job_uuid=job.job_uuid,
                        event_types=["error_snapshot"],
                        limit=1000,
                    )
                )
                if not events:
                    continue
                snapshot = self._load_json(events[-1].payload_uuid)
                if isinstance(snapshot, dict):
                    restore_error(snapshot)
        self.drain_adapter_commands()

    def resume_pending_dispatches(self) -> int:
        """HostNode ready 后恢复尚未交给 adapter 的命令。"""

        resumed = 0
        for status in ("accepted", "dispatch_pending"):
            for job in self.runtime.list_execution_jobs(status=status, limit=1000):
                payload = self._load_json(job.action_payload_uuid)
                if not isinstance(payload, dict):
                    continue
                content = ExecuteJobContent.model_validate(payload)
                if job.status == "accepted":
                    self.runtime.transition_execution_job(
                        job.job_uuid,
                        ExecutionJobTransition(
                            expected_version=job.version,
                            status="dispatch_pending",
                        ),
                    )
                self._dispatch(content)
                resumed += 1
        return resumed

    # -- Persistence helpers -------------------------------------------

    def _store_json(self, value: Any, *, payload_uuid: Optional[str] = None):
        return self.history.store_payload(
            InlinePayloadWrite(
                payload_uuid=payload_uuid,
                media_type="application/json",
                encoding="utf-8",
                inline_payload=canonical_json(value).encode("utf-8"),
            )
        )

    def _load_json(self, payload_uuid: Optional[str]) -> Any:
        if payload_uuid is None:
            return None
        payload = self.history.get_payload(payload_uuid)
        if payload.inline_payload is None:
            logger.warning("cannot restore external payload %s", payload_uuid)
            return None
        return json.loads(payload.inline_payload.decode(payload.encoding))

    def _emit_feedback(self, job: ExecutionJobRecord, feedback: dict[str, Any]) -> None:
        payload = self._store_json(feedback)
        event_uuid = str(uuid.uuid4())
        job = self.runtime.record_execution_feedback(
            job.job_uuid,
            ExecutionJobFeedback(
                expected_version=job.version,
                feedback_sequence=job.feedback_sequence + 1,
            ),
        )
        self.history.append_event(
            HistoryEventAppend(
                event_uuid=event_uuid,
                event_type="job_feedback",
                job_uuid=job.job_uuid,
                endpoint_uuid=job.endpoint_uuid,
                device_uuid=job.device_uuid,
                action_name=job.action_name,
                job_sequence=job.feedback_sequence,
                state_version=job.version,
                payload_uuid=payload.payload_uuid,
                summary={"feedback_type": "action_feedback"},
                actor_type="device",
                actor_uuid=job.device_uuid,
            )
        )
        self._enqueue_backend_event(
            job,
            event_uuid,
            "execution.feedback",
            payload.payload_uuid,
            {"feedback_type": "action_feedback"},
        )

    def _emit_job_event(
        self, job: ExecutionJobRecord, event_type: str, detail: dict[str, Any]
    ) -> None:
        payload = self._store_json(detail)
        event_uuid = str(uuid.uuid4())
        self.history.append_event(
            HistoryEventAppend(
                event_uuid=event_uuid,
                event_type="job_transition",
                job_uuid=job.job_uuid,
                endpoint_uuid=job.endpoint_uuid,
                device_uuid=job.device_uuid,
                action_name=job.action_name,
                state_version=job.version,
                payload_uuid=payload.payload_uuid,
                summary={"status": job.status},
                actor_type="edge",
                actor_uuid=self.endpoint_uuid,
            )
        )
        self._enqueue_backend_event(
            job, event_uuid, event_type, payload.payload_uuid, {"status": job.status}
        )

    def _append_result_event(
        self, job: ExecutionJobRecord, event_uuid: str, payload_uuid: str
    ) -> None:
        self.history.append_event(
            HistoryEventAppend(
                event_uuid=event_uuid,
                event_type="job_result",
                job_uuid=job.job_uuid,
                endpoint_uuid=job.endpoint_uuid,
                device_uuid=job.device_uuid,
                action_name=job.action_name,
                event_key="effective_result",
                state_version=job.version,
                payload_uuid=payload_uuid,
                summary={"status": job.status, "effective": True},
                actor_type="edge",
                actor_uuid=self.endpoint_uuid,
            )
        )

    def _append_decision_audit(
        self,
        job: ExecutionJobRecord,
        payload_uuid: str,
        content: ErrorDecisionContent,
    ) -> None:
        self.history.append_event(
            HistoryEventAppend(
                event_type="decision_audit",
                job_uuid=job.job_uuid,
                endpoint_uuid=job.endpoint_uuid,
                device_uuid=job.device_uuid,
                action_name=job.action_name,
                event_key="terminal_error_decision",
                state_version=job.version,
                payload_uuid=payload_uuid,
                summary={
                    "decision_uuid": content.decision_uuid,
                    "selected_action": content.selected_action,
                    "scheduler_revision": content.confirmed_scheduler_revision,
                },
                actor_type="backend",
                actor_uuid=content.actor_uuid,
            )
        )

    def _raw_failure_event(self, job_uuid: str):
        events = self.history.query_events(
            HistoryEventQuery(
                job_uuid=job_uuid,
                event_types=["job_result"],
                event_key="raw_device_failure",
                limit=1000,
            )
        )
        return events[-1] if events else None

    def _enqueue_backend_event(
        self,
        job: ExecutionJobRecord,
        event_uuid: str,
        event_type: str,
        payload_uuid: Optional[str],
        summary: dict[str, Any],
    ) -> None:
        self.runtime.enqueue_backend_event(
            BackendEventEnqueue(
                event_uuid=event_uuid,
                event_type=event_type,
                aggregate_type="execution_job",
                aggregate_uuid=job.job_uuid,
                aggregate_version=job.version,
                job_uuid=job.job_uuid,
                summary=summary,
                detail_payload_uuid=payload_uuid,
            )
        )
        self._notify()

    def _notify(self) -> None:
        if self.notice_callback is None:
            return
        try:
            self.notice_callback()
        except Exception:  # noqa: BLE001 - outbox remains durable for reconnect
            logger.exception("failed to publish runtime change notice")


__all__ = ["WorkflowBusinessCoordinator"]
