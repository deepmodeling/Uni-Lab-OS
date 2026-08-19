"""WS 轻通知、HTTP 权威命令和持久化执行/错误业务链测试。"""

from __future__ import annotations

from types import SimpleNamespace

from unilabos.server.protocol.common import canonical_hash
from unilabos.server.protocol.control import (
    BackendCommandDocument,
    BackendCommandNotice,
)
from unilabos.server.protocol.history import HistoryEventQuery
from unilabos.server.protocol.runtime import CommandEnvelope
from unilabos.server.scheduler.coordinator import WorkflowBusinessCoordinator
from unilabos.server.services.history import HistoryService
from unilabos.server.services.runtime import RuntimeService


class _DataPlane:
    base_url = "https://backend.example/api/v1"

    def __init__(self) -> None:
        self.documents: dict[str, BackendCommandDocument] = {}
        self.fetched: list[str] = []

    def fetch_command(self, command_uuid: str) -> BackendCommandDocument:
        self.fetched.append(command_uuid)
        return self.documents[command_uuid]


class _Executor:
    def __init__(self) -> None:
        self.dispatched: list[dict] = []
        self.decisions: list[dict] = []
        self.coordinator = None
        self.failed_item = None
        self.failed_return_info = None
        self.cancel_item = None

    def _host_node_getter(self):
        return None

    def dispatch(self, payload: dict) -> None:
        self.dispatched.append(payload)

    def cancel_job(self, _job_uuid: str) -> bool:
        if self.coordinator is not None and self.cancel_item is not None:
            self.coordinator.publish_job_status(
                {}, self.cancel_item, "canceled", {"error": "canceled"}
            )
        return True

    def handle_action_error_decision(
        self, decision_uuid: str, job_uuid: str, decision: dict
    ) -> bool:
        self.decisions.append(decision)
        if self.coordinator is not None and self.failed_item is not None:
            operator_replaced = decision.get("action") == "operator_intervention"
            self.coordinator.publish_job_status(
                {},
                self.failed_item,
                "success" if operator_replaced else "failed",
                (
                    {"return_value": decision.get("result"), "suc": True}
                    if operator_replaced
                    else self.failed_return_info
                ),
            )
        return bool(decision_uuid and job_uuid)

    def get_resolved_action_error_decision(self, *_args):
        return None


def _document(
    command_uuid: str,
    sequence: int,
    command_type: str,
    payload_uuid: str,
    payload: dict,
    *,
    job_uuid: str,
) -> tuple[BackendCommandNotice, BackendCommandDocument]:
    payload_hash = canonical_hash(payload)
    command = CommandEnvelope(
        command_uuid=command_uuid,
        session_uuid="session-1",
        backend_sequence=sequence,
        command_type=command_type,
        job_uuid=job_uuid,
        payload_uuid=payload_uuid,
        payload_sha256=payload_hash,
    )
    notice = BackendCommandNotice(
        notice_uuid=f"notice-{sequence}",
        command_uuid=command_uuid,
        command_type=command_type,
        session_uuid="session-1",
        backend_sequence=sequence,
        edge_uuid="edge-1",
        authority_epoch="authority-1",
        connection_epoch="connection-1",
        content_sha256=payload_hash,
    )
    return notice, BackendCommandDocument(command=command, payload=payload)


def _coordinator(tmp_path):
    runtime = RuntimeService(tmp_path / "runtime.db")
    history = HistoryService(tmp_path / "history.db")
    executor = _Executor()
    data_plane = _DataPlane()
    coordinator = WorkflowBusinessCoordinator(
        runtime,
        history,
        executor,
        endpoint_uuid="hostlink:edge-1",
        transport="hostlink",
        host_uuid="edge-1",
        instance_name="host",
        data_plane=data_plane,
    )
    executor.coordinator = coordinator
    return coordinator, runtime, history, executor, data_plane


def _execute_payload() -> dict:
    return {
        "job_uuid": "job-1",
        "task_uuid": "task-1",
        "node_uuid": "node-1",
        "attempt_group_uuid": "attempt-group-1",
        "attempt_no": 1,
        "device_uuid": "pump-1",
        "action_name": "transfer",
        "action_type": "TransferLiquid",
        "action_args": {"volume": 5},
        "sample_material": {"source": "material-1"},
        "scheduler_revision": 7,
    }


def _item():
    return SimpleNamespace(
        task_type="job_call_back_status",
        device_id="pump-1",
        action_name="transfer",
        task_id="task-1",
        job_id="job-1",
        notebook_id="",
        device_action_key="/devices/pump-1/transfer",
        node_id="node-1",
        retry_count=0,
    )


def test_execute_notice_pulls_http_document_and_ws_outbound_stays_small(tmp_path) -> None:
    coordinator, runtime, history, executor, data_plane = _coordinator(tmp_path)
    notice, document = _document(
        "execute-1", 1, "execute_job", "payload-execute-1", _execute_payload(), job_uuid="job-1"
    )
    data_plane.documents["execute-1"] = document

    coordinator.handle_backend_notice(notice)

    assert data_plane.fetched == ["execute-1"]
    assert executor.dispatched[0]["action_args"] == {"volume": 5}
    assert runtime.get_execution_job("job-1").status == "dispatch_pending"
    assert history.get_payload("payload-execute-1").sha256 == notice.content_sha256

    item = _item()
    coordinator.publish_job_started(item)
    coordinator.publish_job_status({"progress": 0.5}, item, "running")
    coordinator.publish_job_status({"progress": 0.8}, item, "running")
    coordinator.publish_job_status(
        {"result": "ok"}, item, "success", {"return_value": "ok"}
    )

    assert runtime.get_execution_job("job-1").status == "succeeded"
    notices = coordinator.claim_edge_changes()
    assert notices
    assert all(notice.detail_payload_uuid for notice in notices)
    encoded = [notice.model_dump(mode="json") for notice in notices]
    assert all("action_args" not in value for value in encoded)
    assert all("return_info" not in value for value in encoded)


def test_failure_waits_for_backend_scheduler_revision_then_releases_failed(tmp_path) -> None:
    coordinator, runtime, history, executor, data_plane = _coordinator(tmp_path)
    execute_notice, execute_document = _document(
        "execute-1", 1, "execute_job", "payload-execute-1", _execute_payload(), job_uuid="job-1"
    )
    data_plane.documents["execute-1"] = execute_document
    coordinator.handle_backend_notice(execute_notice)
    item = _item()
    coordinator.publish_job_started(item)
    coordinator.publish_job_status({}, item, "running")

    return_info = {
        "error": "pump offline",
        "error_info": {
            "exception_type": "CommunicationError",
            "exception_mro": ["CommunicationError", "Exception"],
            "error_message": "pump offline",
        },
    }
    report = {
        "decision_id": "error-1",
        "job_id": "job-1",
        "device_id": "pump-1",
        "action_name": "transfer",
        "exception_type": "CommunicationError",
        "error_message": "pump offline",
        "options": [
            {"action": "retry", "label": "Retry"},
            {"action": "abort", "label": "Fail"},
        ],
    }
    assert coordinator.publish_job_error_pending(
        report, item, return_info, {}, return_info["error_info"]
    )
    waiting = runtime.get_execution_job("job-1")
    assert waiting.status == "terminal_waiting"
    assert waiting.terminal_gate_state == "waiting_backend"
    assert waiting.terminal_required_scheduler_revision == 8

    executor.failed_item = item
    executor.failed_return_info = return_info
    decision_payload = {
        "decision_uuid": "error-1",
        "confirmed_scheduler_revision": 8,
        "adapter_command_uuid": "adapter-release-1",
        "selected_action": "retry",
        "reason": "backend inserted retry attempt",
    }
    decision_notice, decision_document = _document(
        "release-1",
        2,
        "release_failed",
        "payload-release-1",
        decision_payload,
        job_uuid="job-1",
    )
    data_plane.documents["release-1"] = decision_document
    coordinator.handle_backend_notice(decision_notice)

    failed = runtime.get_execution_job("job-1")
    assert failed.status == "failed"
    assert failed.terminal_gate_state == "released_failed"
    assert executor.decisions[0]["action"] == "retry"
    assert runtime.list_execution_jobs(attempt_group_uuid="attempt-group-1") == [failed]
    assert history.query_events(
        HistoryEventQuery(job_uuid="job-1", event_types=["error_snapshot"])
    )


def test_cancel_is_applied_through_command_inbox_and_adapter_outbox(tmp_path) -> None:
    coordinator, runtime, _history, executor, data_plane = _coordinator(tmp_path)
    execute_notice, execute_document = _document(
        "execute-1", 1, "execute_job", "payload-execute-1", _execute_payload(), job_uuid="job-1"
    )
    data_plane.documents["execute-1"] = execute_document
    coordinator.handle_backend_notice(execute_notice)
    item = _item()
    coordinator.publish_job_started(item)
    executor.cancel_item = item

    cancel_payload = {
        "adapter_command_uuid": "adapter-cancel-1",
        "reason": "backend canceled workflow",
    }
    cancel_notice, cancel_document = _document(
        "cancel-1",
        2,
        "cancel_job",
        "payload-cancel-1",
        cancel_payload,
        job_uuid="job-1",
    )
    data_plane.documents["cancel-1"] = cancel_document
    coordinator.handle_backend_notice(cancel_notice)

    assert runtime.get_command("cancel-1").status == "applied"
    assert runtime.get_adapter_command("adapter-cancel-1").status == "acknowledged"
    assert runtime.get_execution_job("job-1").status == "canceled"


def test_operator_result_replacement_preserves_raw_failure_chain(tmp_path) -> None:
    coordinator, runtime, history, executor, data_plane = _coordinator(tmp_path)
    execute_notice, execute_document = _document(
        "execute-1", 1, "execute_job", "payload-execute-1", _execute_payload(), job_uuid="job-1"
    )
    data_plane.documents["execute-1"] = execute_document
    coordinator.handle_backend_notice(execute_notice)
    item = _item()
    coordinator.publish_job_started(item)
    coordinator.publish_job_status({}, item, "running")
    return_info = {
        "error": "pump offline",
        "error_info": {
            "exception_type": "CommunicationError",
            "exception_mro": ["CommunicationError", "Exception"],
            "error_message": "pump offline",
        },
    }
    report = {
        "decision_id": "error-1",
        "job_id": "job-1",
        "device_id": "pump-1",
        "action_name": "transfer",
        "exception_type": "CommunicationError",
        "error_message": "pump offline",
        "options": [
            {"action": "operator_intervention", "label": "Manual result"}
        ],
    }
    coordinator.publish_job_error_pending(
        report, item, return_info, {}, return_info["error_info"]
    )
    executor.failed_item = item
    executor.failed_return_info = return_info
    replacement = {
        "decision_uuid": "error-1",
        "confirmed_scheduler_revision": 8,
        "adapter_command_uuid": "adapter-replace-1",
        "selected_action": "operator_intervention",
        "reason": "operator confirmed physical completion",
        "result": {"transferred": True},
        "actor_uuid": "operator-1",
    }
    notice, document = _document(
        "replace-1",
        2,
        "replace_result",
        "payload-replace-1",
        replacement,
        job_uuid="job-1",
    )
    data_plane.documents["replace-1"] = document
    coordinator.handle_backend_notice(notice)

    job = runtime.get_execution_job("job-1")
    assert job.status == "succeeded"
    assert job.terminal_gate_state == "result_replaced"
    raw = history.query_events(
        HistoryEventQuery(
            job_uuid="job-1",
            event_types=["job_result"],
            event_key="raw_device_failure",
        )
    )[0]
    chain = history.replacement_chain(raw.event_uuid)
    assert len(chain) == 2
    assert chain[1].actor_uuid == "operator-1"
