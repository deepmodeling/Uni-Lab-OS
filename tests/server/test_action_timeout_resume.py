"""``execution_timeout`` 软超时在 runtime.v1 控制面与工作流权威侧的收回合同。

- RuntimeService.resume_error_gate：终态闸门关闭、job 回到 running、发 execution.error_resumed；
- WorkflowStore.mark_job_decision_resumed：attempt / 节点运行从 intervention_required 收回 running；
- EdgeControlService.resolve_error_decision(wait)：签发 resume_pending 并通知调度器恢复；
- 图校验接受节点级 timeout_seconds / execution_timeout_seconds；
- 调度器解析优先级：节点 execution_policy > 注册表表达式（用最终参数求值）。
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from unilabos.protocol.runtime import (
    BackendSessionUpsert,
    CommandEnvelope,
    EndpointSnapshotUpsert,
    ErrorGateOpen,
    ErrorGateResume,
    ExecutionJobCreate,
    ExecutionJobTransition,
)
from unilabos.protocol.utils.workflow_validation import (
    GraphValidationError,
    _validate_execution_policy,
)
from unilabos.server.database.tables.runtime import DeviceRoute
from unilabos.server.services.runtime import RuntimeConflictError, RuntimeService


# ── RuntimeService ──────────────────────────────────────────────


def _runtime_with_waiting_job(tmp_path):
    service = RuntimeService(tmp_path / "runtime.db")
    service.upsert_backend_session(
        BackendSessionUpsert(
            session_uuid="session",
            edge_uuid="edge",
            backend_uri="wss://backend",
            authority_epoch="authority",
            connection_epoch="connection-1",
            state="active",
        )
    )
    service.upsert_endpoint_snapshot(
        EndpointSnapshotUpsert(
            endpoint_uuid="endpoint",
            transport="hostlink",
            host_uuid="host",
            instance_name="main",
            authority_epoch="authority",
            adapter_epoch="adapter-1",
            state="online",
            device_routes=[
                DeviceRoute(
                    route_uuid="route",
                    device_uuid="device",
                    driver_key="driver",
                    config_hash="config-hash",
                )
            ],
        )
    )

    def command(sequence: int, command_uuid: str, command_type: str) -> None:
        service.receive_command(
            CommandEnvelope(
                command_uuid=command_uuid,
                session_uuid="session",
                backend_sequence=sequence,
                command_type=command_type,
                job_uuid="job-1",
                payload_sha256=f"sha-{command_uuid}",
            )
        )

    command(1, "execute-1", "execute_job")
    job = service.create_execution_job(
        ExecutionJobCreate(
            job_uuid="job-1",
            task_uuid="task",
            node_uuid="node",
            attempt_group_uuid="attempt-group",
            execute_command_uuid="execute-1",
            device_uuid="device",
            action_name="heat",
            action_payload_uuid="payload-1",
            route_uuid="route",
            endpoint_uuid="endpoint",
            transport="hostlink",
            scheduler_revision=4,
        )
    )
    for status in ("dispatch_pending", "dispatched", "running"):
        job = service.transition_execution_job(
            job.job_uuid,
            ExecutionJobTransition(expected_version=job.version, status=status),
        )
    waiting = service.open_error_gate(
        job.job_uuid,
        ErrorGateOpen(
            expected_version=job.version,
            error_uuid="decision-1",
            error_code="ExecutionTimeoutException",
            error_summary="动作执行超时，仍在执行",
            required_scheduler_revision=5,
            request_event_uuid="event-error-1",
            summary={"category": "timeout", "severity": "warning"},
        ),
    )
    return service, command, waiting


def test_resume_error_gate_returns_job_to_running_and_emits_event(tmp_path) -> None:
    service, command, waiting = _runtime_with_waiting_job(tmp_path)
    try:
        assert waiting.status == "terminal_waiting"
        with pytest.raises(RuntimeConflictError, match="does not match"):
            service.resume_error_gate(
                waiting.job_uuid,
                ErrorGateResume(expected_version=waiting.version, error_uuid="other"),
            )

        command(2, "resume-1", "resume_pending")
        resumed = service.resume_error_gate(
            waiting.job_uuid,
            ErrorGateResume(
                expected_version=waiting.version,
                error_uuid="decision-1",
                reason="wait",
                decision_command_uuid="resume-1",
                adapter_command_uuid="adapter-resume-1",
                decision={"selected_action": "wait"},
            ),
        )
        assert resumed.status == "running"
        assert resumed.terminal_gate_state == "none"
        assert resumed.terminal_error_uuid is None
        assert service.get_adapter_command("adapter-resume-1").command_type == "resume_pending"
        assert service.get_command("resume-1").status == "applied"

        events = service.list_backend_events(job_uuid="job-1", limit=50)
        types = [event.event_type for event in events]
        assert "execution.error_resumed" in types
        resumed_event = next(e for e in events if e.event_type == "execution.error_resumed")
        assert resumed_event.summary["error_uuid"] == "decision-1"
        assert resumed_event.summary["selected_action"] == "wait"

        # 闸门关闭后可以再次打开（第二次软超时），也可以正常成功收口
        reopened = service.open_error_gate(
            resumed.job_uuid,
            ErrorGateOpen(
                expected_version=resumed.version,
                error_uuid="decision-2",
                error_code="ExecutionTimeoutException",
                error_summary="again",
                required_scheduler_revision=5,
                request_event_uuid="event-error-2",
            ),
        )
        assert reopened.terminal_gate_state == "waiting_backend"
        again = service.resume_error_gate(
            reopened.job_uuid,
            ErrorGateResume(expected_version=reopened.version, error_uuid="decision-2"),
        )
        succeeded = service.transition_execution_job(
            again.job_uuid,
            ExecutionJobTransition(expected_version=again.version, status="succeeded"),
        )
        assert succeeded.status == "succeeded"
    finally:
        service.close()


# ── WorkflowStore：intervention_required → running ──────────────


def test_workflow_store_resumes_decision_pending_attempt() -> None:
    from unilabos.server.services.runtime.workflow.service import WorkflowService

    service = WorkflowService(":memory:")
    try:
        service.create_ad_hoc_task_with_job(
            task_uuid="task-1",
            node_uuid="node-1",
            device_id="device-1",
            action_name="heat",
            action_type="",
            param={"duration": 10},
            execution_policy={},
            execution_timeout_seconds=0,
            description=None,
            meta_data={},
            idempotency_key="idem-1",
            request_fingerprint="fp-1",
        )
        (run,) = service.list_node_runs("task-1")
        job_uuid = str(run["current_job_uuid"])
        service.mark_job_running(job_uuid)
        pending = service.mark_job_decision_pending(
            job_uuid,
            {
                "decision_id": "d-1",
                "exception_type": "ExecutionTimeoutException",
                "error_message": "soft timeout",
                "options": [{"action": "wait"}, {"action": "abort"}],
                "retry_count": 0,
                "max_retries": 3,
            },
        )
        assert pending["status"] == "intervention_required"
        run = service.get_workflow_node_run(pending["workflow_node_run_uuid"])
        assert run["status"] == "intervention_required"

        resumed = service.mark_workflow_node_job_decision_resumed(job_uuid, "d-1")
        assert resumed["status"] == "running"
        assert "pending_decision" not in (resumed.get("control_data") or {})
        assert (resumed["control_data"]["resumed_decisions"][0]["decision_id"]) == "d-1"
        run = service.get_workflow_node_run(pending["workflow_node_run_uuid"])
        assert run["status"] == "running"

        # 幂等：已经 running 的 attempt 再收回一次不改状态
        assert service.mark_job_decision_resumed(job_uuid, "d-1")["status"] == "running"
        updated = service.set_workflow_node_run_execution_timeout(
            pending["workflow_node_run_uuid"], 120
        )
        assert updated["execution_timeout_seconds"] == 120
    finally:
        service.close()


# ── 图校验 ──────────────────────────────────────────────────────


def test_execution_policy_accepts_node_level_timeouts() -> None:
    _validate_execution_policy({"execution_timeout_seconds": 0, "timeout_seconds": 600})
    with pytest.raises(GraphValidationError, match="timeout_seconds 必须是非负整数"):
        _validate_execution_policy({"timeout_seconds": -1})
    with pytest.raises(GraphValidationError, match="execution_timeout_seconds 必须是非负整数"):
        _validate_execution_policy({"execution_timeout_seconds": 1.5})


# ── 调度器解析优先级 ────────────────────────────────────────────


def test_scheduler_action_timeouts_prefer_execution_policy_then_registry() -> None:
    from unilabos.server.backend.scheduler.dag.models import DagNode
    from unilabos.server.backend.scheduler.service import BackendScheduler

    class _Executor:
        def __init__(self) -> None:
            self.calls: List[Dict[str, Any]] = []

        def resolve_action_timeouts(self, device_id: str, action: str, args: Dict[str, Any]):
            self.calls.append({"device": device_id, "action": action, "args": dict(args)})
            return {
                "timeout": 600.0,
                "execution_timeout": float(args["duration"]) * 2,
                "execution_timeout_spec": "duration * 2",
                "error": None,
            }

    scheduler = BackendScheduler.__new__(BackendScheduler)
    scheduler.executor = _Executor()  # type: ignore[attr-defined]
    node = DagNode(node_id="run-1", device_id="device-1", action="heat", action_type="", action_args={})

    resolved = scheduler._action_timeouts(  # noqa: SLF001
        node, {"execution_policy": {}}, {"duration": 30}
    )
    assert resolved["timeout"] == 600.0
    assert resolved["execution_timeout"] == 60.0
    assert resolved["source"] == {"timeout": "registry", "execution_timeout": "registry"}
    assert scheduler.executor.calls[0]["args"] == {"duration": 30}  # 最终参数参与求值

    resolved = scheduler._action_timeouts(  # noqa: SLF001
        node,
        {"execution_policy": {"execution_timeout_seconds": 45, "timeout_seconds": 0}},
        {"duration": 30},
    )
    assert resolved["execution_timeout"] == 45.0  # 节点显式声明优先
    assert resolved["timeout"] == 600.0  # 0 = 未声明，回退注册表
    assert resolved["source"]["execution_timeout"] == "execution_policy"


# ── 调度权威（--role backend / 默认两进程）对 wait 的处理 ─────────


def test_edge_control_wait_issues_resume_pending_and_resumes_scheduler() -> None:
    from unilabos.server.backend.edge_control import EdgeControlService

    service = EdgeControlService.__new__(EdgeControlService)
    import threading

    service._lock = threading.RLock()  # type: ignore[attr-defined]
    service._pending_decisions = {  # type: ignore[attr-defined]
        "d-1": {
            "job_uuid": "job-1",
            "report": {
                "decision_id": "d-1",
                "job_id": "job-1",
                "device_id": "device-1",
                "options": [{"action": "wait"}, {"action": "abort"}],
                "retry_count": 0,
                "max_retries": 3,
                "action_still_running": True,
            },
            "required_scheduler_revision": 5,
        }
    }
    issued: List[Dict[str, Any]] = []
    service._issue_command = lambda **kwargs: issued.append(kwargs) or "cmd"  # type: ignore[attr-defined]

    class _Scheduler:
        def __init__(self) -> None:
            self.resumed: List[Dict[str, Any]] = []

        def publish_job_error_decision_resumed(self, report: Dict[str, Any]) -> bool:
            self.resumed.append(report)
            return True

    scheduler = _Scheduler()
    service.result_bridges = [scheduler]  # type: ignore[attr-defined]

    assert service.resolve_error_decision("d-1", {"action": "wait", "reason": "还在加热"})
    assert issued[0]["command_type"] == "resume_pending"
    assert issued[0]["job_uuid"] == "job-1"
    assert issued[0]["payload"]["selected_action"] == "wait"
    assert issued[0]["payload"]["decision_uuid"] == "d-1"
    assert scheduler.resumed[0]["selected_action"] == "wait"
    assert service.list_error_decisions() == []
