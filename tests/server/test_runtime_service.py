"""新 runtime authority 的协议、幂等、错误 gate 与可靠 outbox 测试。"""

from __future__ import annotations

import time

import pytest

from unilabos.server.models.runtime import DeviceRoute
from unilabos.server.protocol.runtime import (
    AdapterCommandAck,
    AdapterCommandClaim,
    AdapterCommandEnqueue,
    BackendEventAck,
    BackendEventClaim,
    BackendEventEnqueue,
    BackendSessionUpsert,
    CommandEnvelope,
    EndpointSnapshotUpsert,
    ErrorGateDecision,
    ErrorGateOpen,
    ExecutionJobCreate,
    ExecutionJobTransition,
)
from unilabos.server.services.runtime import (
    RuntimeConflictError,
    RuntimeService,
)


def _session(service: RuntimeService, session_uuid: str = "session") -> None:
    service.upsert_backend_session(
        BackendSessionUpsert(
            session_uuid=session_uuid,
            edge_uuid="edge",
            backend_uri="wss://backend",
            authority_epoch="authority",
            connection_epoch=f"connection-{session_uuid}",
            state="active",
        )
    )


def _endpoint(service: RuntimeService) -> None:
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


def _command(
    service: RuntimeService,
    sequence: int,
    command_uuid: str,
    command_type: str,
    *,
    job_uuid: str | None,
):
    return service.receive_command(
        CommandEnvelope(
            command_uuid=command_uuid,
            session_uuid="session",
            backend_sequence=sequence,
            command_type=command_type,
            job_uuid=job_uuid,
            payload_sha256=f"sha-{command_uuid}",
            summary={"command": command_uuid},
        )
    )


def test_backend_session_and_endpoint_snapshot_upsert(tmp_path) -> None:
    service = RuntimeService(tmp_path / "runtime.db")
    try:
        _session(service)
        session = service.get_backend_session("session")
        assert session.state == "active"
        assert session.version == 1

        _endpoint(service)
        endpoint = service.get_endpoint_snapshot("endpoint")
        assert endpoint.transport == "hostlink"
        assert endpoint.device_routes[0].route_uuid == "route"
        assert endpoint.version == 1

        unchanged = service.upsert_endpoint_snapshot(
            EndpointSnapshotUpsert(
                endpoint_uuid="endpoint",
                transport="hostlink",
                host_uuid="host",
                instance_name="main",
                authority_epoch="authority",
                adapter_epoch="adapter-1",
                state="online",
                device_routes=endpoint.device_routes,
                observed_at_ms=endpoint.last_seen_at_ms + 1,
            )
        )
        assert unchanged.changed is False
        assert unchanged.endpoint.version == 1

        changed = service.upsert_endpoint_snapshot(
            EndpointSnapshotUpsert(
                endpoint_uuid="endpoint",
                transport="hostlink",
                host_uuid="host",
                instance_name="main",
                authority_epoch="authority",
                adapter_epoch="adapter-2",
                state="reconciling",
                config={"revision": 2},
            )
        )
        assert changed.changed is True
        assert changed.endpoint.version == 2
        assert changed.endpoint.adapter_event_cursor == 0
    finally:
        service.close()


def test_command_inbox_is_idempotent_and_sequence_ordered(tmp_path) -> None:
    service = RuntimeService(tmp_path / "runtime.db")
    try:
        _session(service)
        first = _command(service, 1, "reconcile", "reconcile", job_uuid=None)
        replay = _command(service, 1, "reconcile", "reconcile", job_uuid=None)
        assert first.replayed is False
        assert replay.replayed is True
        assert replay.command_fingerprint == first.command_fingerprint

        with pytest.raises(RuntimeConflictError, match="different content"):
            service.receive_command(
                CommandEnvelope(
                    command_uuid="reconcile",
                    session_uuid="session",
                    backend_sequence=1,
                    command_type="reconcile",
                    payload_sha256="different-sha",
                )
            )
        with pytest.raises(RuntimeConflictError, match="expected 2"):
            _command(service, 3, "gap", "reconcile", job_uuid=None)

        assert service.get_backend_session("session").command_cursor == 1
    finally:
        service.close()


def test_error_gate_release_and_backend_owned_retry(tmp_path) -> None:
    service = RuntimeService(tmp_path / "runtime.db")
    try:
        _session(service)
        _endpoint(service)
        _command(service, 1, "execute-1", "execute_job", job_uuid="job-1")
        job = service.create_execution_job(
            ExecutionJobCreate(
                job_uuid="job-1",
                task_uuid="task",
                node_uuid="node",
                attempt_group_uuid="attempt-group",
                execute_command_uuid="execute-1",
                device_uuid="device",
                action_name="transfer",
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
                ExecutionJobTransition(
                    expected_version=job.version,
                    status=status,
                ),
            )

        waiting = service.open_error_gate(
            job.job_uuid,
            ErrorGateOpen(
                expected_version=job.version,
                error_uuid="error-1",
                error_code="device_error",
                error_summary="device reported a terminal error",
                required_scheduler_revision=8,
                request_event_uuid="event-error-1",
            ),
        )
        assert waiting.status == "terminal_waiting"
        assert waiting.terminal_gate_state == "waiting_backend"

        with pytest.raises(RuntimeConflictError, match="before backend releases"):
            service.transition_execution_job(
                waiting.job_uuid,
                ExecutionJobTransition(
                    expected_version=waiting.version,
                    status="failed",
                ),
            )

        _command(
            service,
            2,
            "release-1",
            "release_failed",
            job_uuid="job-1",
        )
        with pytest.raises(RuntimeConflictError, match="scheduler revision"):
            service.decide_error_gate(
                waiting.job_uuid,
                ErrorGateDecision(
                    expected_version=waiting.version,
                    decision_command_uuid="release-1",
                    action="release_failed",
                    confirmed_scheduler_revision=7,
                    adapter_command_uuid="adapter-release-1",
                ),
            )

        released = service.decide_error_gate(
            waiting.job_uuid,
            ErrorGateDecision(
                expected_version=waiting.version,
                decision_command_uuid="release-1",
                action="release_failed",
                confirmed_scheduler_revision=8,
                adapter_command_uuid="adapter-release-1",
            ),
        )
        assert released.status == "terminal_waiting"
        assert released.terminal_gate_state == "released_failed"
        assert (
            service.repository.get_adapter_command("adapter-release-1").status
            == "pending"
        )

        failed = service.transition_execution_job(
            released.job_uuid,
            ExecutionJobTransition(
                expected_version=released.version,
                status="failed",
                error_code="device_error",
            ),
        )
        assert failed.status == "failed"

        _command(service, 3, "execute-2", "execute_job", job_uuid="job-2")
        retry = service.create_execution_job(
            ExecutionJobCreate(
                job_uuid="job-2",
                task_uuid="task",
                node_uuid="node",
                attempt_group_uuid="attempt-group",
                retry_of_job_uuid="job-1",
                attempt_no=2,
                execute_command_uuid="execute-2",
                device_uuid="device",
                action_name="transfer",
                action_payload_uuid="payload-2",
                scheduler_revision=9,
            )
        )
        assert retry.retry_of_job_uuid == "job-1"
        assert retry.attempt_no == 2
        assert retry.job_uuid != failed.job_uuid
    finally:
        service.close()


def test_adapter_and_backend_outboxes_claim_retry_and_ack(tmp_path) -> None:
    service = RuntimeService(tmp_path / "runtime.db")
    try:
        _session(service)
        _endpoint(service)
        service.enqueue_adapter_command(
            AdapterCommandEnqueue(
                adapter_command_uuid="adapter-reconcile",
                endpoint_uuid="endpoint",
                trigger_event_uuid="endpoint-ready",
                target_adapter_epoch="adapter-1",
                command_type="reconcile_state",
            )
        )
        base = int(time.time() * 1000) + 10_000
        claimed = service.claim_adapter_commands(
            AdapterCommandClaim(endpoint_uuid="endpoint", now_ms=base, lease_ms=100)
        )
        assert [item.adapter_command_uuid for item in claimed] == ["adapter-reconcile"]
        assert claimed[0].delivery_attempt_count == 1
        assert (
            service.claim_adapter_commands(
                AdapterCommandClaim(endpoint_uuid="endpoint", now_ms=base, lease_ms=100)
            )
            == []
        )

        reclaimed = service.claim_adapter_commands(
            AdapterCommandClaim(
                endpoint_uuid="endpoint", now_ms=base + 101, lease_ms=100
            )
        )
        assert reclaimed[0].delivery_attempt_count == 2
        acknowledged = service.acknowledge_adapter_command(
            AdapterCommandAck(
                adapter_command_uuid="adapter-reconcile",
                ack_event_uuid="adapter-ack",
                acknowledged_at_ms=base + 102,
            )
        )
        assert acknowledged.status == "acknowledged"
        assert (
            service.acknowledge_adapter_command(
                AdapterCommandAck(
                    adapter_command_uuid="adapter-reconcile",
                    ack_event_uuid="adapter-ack",
                    acknowledged_at_ms=base + 103,
                )
            ).status
            == "acknowledged"
        )

        event = service.enqueue_backend_event(
            BackendEventEnqueue(
                event_uuid="backend-event",
                event_type="endpoint.snapshot",
                aggregate_type="executor_endpoint",
                aggregate_uuid="endpoint",
                aggregate_version=1,
                summary={"state": "online"},
            )
        )
        assert event.status == "pending"
        sent = service.claim_backend_events(
            BackendEventClaim(session_uuid="session", now_ms=base, lease_ms=100)
        )
        assert [item.event_uuid for item in sent] == ["backend-event"]
        assert sent[0].delivery_attempt_count == 1
        assert (
            service.get_backend_session("session").event_send_cursor == sent[0].sequence
        )
        assert (
            service.claim_backend_events(
                BackendEventClaim(session_uuid="session", now_ms=base, lease_ms=100)
            )
            == []
        )

        resent = service.claim_backend_events(
            BackendEventClaim(session_uuid="session", now_ms=base + 101, lease_ms=100)
        )
        assert resent[0].delivery_attempt_count == 2
        assert (
            service.acknowledge_backend_events(
                BackendEventAck(
                    session_uuid="session",
                    through_sequence=resent[0].sequence,
                    acknowledged_at_ms=base + 102,
                )
            )
            == 1
        )
        assert (
            service.acknowledge_backend_events(
                BackendEventAck(
                    session_uuid="session",
                    through_sequence=resent[0].sequence,
                    acknowledged_at_ms=base + 103,
                )
            )
            == 0
        )
        assert service.repository.get_backend_event("backend-event").status == (
            "acknowledged"
        )
    finally:
        service.close()
