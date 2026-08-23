"""runtime.v1 HTTP router 与 Local/HTTP client 契约测试。"""

from __future__ import annotations

import time
from io import BytesIO
from urllib.error import HTTPError
from urllib.parse import urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from unilabos.server.database.repositories.runtime import RuntimeRepository
from unilabos.server.api.runtime import (
    create_runtime_router,
    install_runtime_api,
)
from unilabos.client.runtime import (
    HTTPRuntimeClient,
    LocalRuntimeClient,
    RuntimeHTTPError,
)
from unilabos.server.database.tables.runtime import DeviceRoute
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
from unilabos.server.services.runtime import RuntimeService


class _URLResponse:
    def __init__(self, content: bytes):
        self.content = content

    def read(self) -> bytes:
        return self.content

    def __enter__(self) -> "_URLResponse":
        return self

    def __exit__(self, *_: object) -> None:
        return None


def _test_client_opener(client: TestClient):
    def open_request(request, *, timeout: float):
        del timeout
        url = urlsplit(request.full_url)
        response = client.request(
            request.get_method(),
            url.path + (f"?{url.query}" if url.query else ""),
            content=request.data,
            headers=dict(request.header_items()),
        )
        if response.status_code >= 400:
            raise HTTPError(
                request.full_url,
                response.status_code,
                response.reason_phrase,
                response.headers,
                BytesIO(response.content),
            )
        return _URLResponse(response.content)

    return open_request


def _session() -> BackendSessionUpsert:
    return BackendSessionUpsert(
        session_uuid="session",
        edge_uuid="edge",
        backend_uri="wss://backend",
        authority_epoch="authority",
        connection_epoch="connection",
        state="active",
    )


def _endpoint() -> EndpointSnapshotUpsert:
    return EndpointSnapshotUpsert(
        endpoint_uuid="endpoint",
        transport="hostlink",
        host_uuid="host",
        instance_name="main",
        authority_epoch="authority",
        adapter_epoch="adapter-epoch",
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


def _command(
    sequence: int,
    command_uuid: str,
    command_type: str,
    *,
    job_uuid: str | None,
) -> CommandEnvelope:
    return CommandEnvelope(
        command_uuid=command_uuid,
        session_uuid="session",
        backend_sequence=sequence,
        command_type=command_type,
        job_uuid=job_uuid,
        payload_sha256=f"sha-{command_uuid}",
    )


def test_http_client_exposes_runtime_state_machine_and_queries(tmp_path) -> None:
    service = RuntimeService(RuntimeRepository(tmp_path / "runtime.db"))
    app = FastAPI()
    install_runtime_api(app, service)
    try:
        with TestClient(app) as test_client:
            client = HTTPRuntimeClient(
                "http://runtime.test",
                opener=_test_client_opener(test_client),
            )
            local = LocalRuntimeClient(service)

            session = client.upsert_backend_session(_session())
            assert session.state == "active"
            assert local.get_backend_session("session") == session
            assert client.list_backend_sessions(edge_uuid="edge") == [session]

            snapshot = client.upsert_endpoint_snapshot(_endpoint())
            assert snapshot.changed is True
            assert snapshot.endpoint.device_routes[0].route_uuid == "route"
            assert local.list_endpoint_snapshots(transport="hostlink") == [
                snapshot.endpoint
            ]

            execute = _command(1, "execute-1", "execute_job", job_uuid="job-1")
            receipt = client.receive_command(execute)
            assert receipt.replayed is False
            assert client.receive_command(execute).replayed is True
            assert client.get_command("execute-1").status == "received"
            assert [
                item.command_uuid
                for item in client.list_commands(session_uuid="session")
            ] == ["execute-1"]
            invalid_cursor = test_client.get(
                "/api/v1/runtime/commands",
                params={"after_sequence": 1},
            )
            assert invalid_cursor.status_code == 422

            job = client.create_execution_job(
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
            assert client.get_command("execute-1").status == "applied"
            assert client.get_execution_job("job-1") == job
            assert client.list_execution_jobs(device_uuid="device") == [job]

            for status in ("dispatch_pending", "dispatched", "running"):
                job = client.transition_execution_job(
                    job.job_uuid,
                    ExecutionJobTransition(
                        expected_version=job.version,
                        status=status,
                    ),
                )

            waiting = client.open_error_gate(
                job.job_uuid,
                ErrorGateOpen(
                    expected_version=job.version,
                    error_uuid="error-1",
                    error_code="device_error",
                    error_summary="device failed",
                    required_scheduler_revision=8,
                    request_event_uuid="error-event",
                ),
            )
            assert waiting.terminal_gate_state == "waiting_backend"
            assert client.get_backend_event("error-event").status == "pending"
            assert [
                item.event_uuid for item in client.list_backend_events(job_uuid="job-1")
            ] == ["error-event"]

            client.receive_command(
                _command(
                    2,
                    "release-1",
                    "release_failed",
                    job_uuid="job-1",
                )
            )
            released = client.decide_error_gate(
                waiting.job_uuid,
                ErrorGateDecision(
                    expected_version=waiting.version,
                    decision_command_uuid="release-1",
                    action="release_failed",
                    confirmed_scheduler_revision=8,
                    adapter_command_uuid="adapter-release",
                ),
            )
            assert released.terminal_gate_state == "released_failed"
            assert client.get_adapter_command("adapter-release").status == "pending"
            assert [
                item.adapter_command_uuid
                for item in client.list_adapter_commands(job_uuid="job-1")
            ] == ["adapter-release"]

            now = int(time.time() * 1000) + 10_000
            adapter_batch = client.claim_adapter_commands(
                AdapterCommandClaim(endpoint_uuid="endpoint", now_ms=now)
            )
            assert [item.adapter_command_uuid for item in adapter_batch] == [
                "adapter-release"
            ]
            assert (
                client.acknowledge_adapter_command(
                    AdapterCommandAck(
                        adapter_command_uuid="adapter-release",
                        ack_event_uuid="adapter-ack",
                        acknowledged_at_ms=now + 1,
                    )
                ).status
                == "acknowledged"
            )

            backend_batch = client.claim_backend_events(
                BackendEventClaim(session_uuid="session", now_ms=now)
            )
            assert [item.event_uuid for item in backend_batch] == ["error-event"]
            assert (
                client.acknowledge_backend_events(
                    BackendEventAck(
                        session_uuid="session",
                        through_sequence=backend_batch[0].sequence,
                        acknowledged_at_ms=now + 1,
                    )
                )
                == 1
            )

            failed = client.transition_execution_job(
                released.job_uuid,
                ExecutionJobTransition(
                    expected_version=released.version,
                    status="failed",
                ),
            )
            client.receive_command(
                _command(3, "execute-2", "execute_job", job_uuid="job-2")
            )
            retry = client.create_execution_job(
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
            assert failed.status == "failed"
            assert client.list_execution_jobs(retry_of_job_uuid="job-1") == [retry]
    finally:
        service.repository.close()


def test_explicit_outbox_commands_work_through_local_and_http_clients(
    tmp_path,
) -> None:
    service = RuntimeService(RuntimeRepository(tmp_path / "runtime.db"))
    app = FastAPI()
    app.include_router(create_runtime_router(service))
    local = LocalRuntimeClient(service)
    try:
        local.upsert_backend_session(_session())
        local.upsert_endpoint_snapshot(_endpoint())
        with TestClient(app) as test_client:
            http = HTTPRuntimeClient(
                "http://runtime.test/api/v1",
                opener=_test_client_opener(test_client),
            )
            adapter = http.enqueue_adapter_command(
                AdapterCommandEnqueue(
                    adapter_command_uuid="reconcile-command",
                    endpoint_uuid="endpoint",
                    trigger_event_uuid="endpoint-ready",
                    target_adapter_epoch="adapter-epoch",
                    command_type="reconcile_state",
                )
            )
            event = http.enqueue_backend_event(
                BackendEventEnqueue(
                    event_uuid="endpoint-event",
                    event_type="endpoint.snapshot",
                    aggregate_type="executor_endpoint",
                    aggregate_uuid="endpoint",
                    aggregate_version=1,
                )
            )
            assert local.get_adapter_command(adapter.adapter_command_uuid) == adapter
            assert local.get_backend_event(event.event_uuid) == event
            assert local.list_adapter_commands(status="pending") == [adapter]
            assert local.list_backend_events(status="pending") == [event]
    finally:
        service.repository.close()


def test_runtime_http_errors_and_router_do_not_expose_delete(tmp_path) -> None:
    service = RuntimeService(RuntimeRepository(tmp_path / "runtime.db"))
    app = FastAPI()
    install_runtime_api(app, service)
    try:
        runtime_routes = [
            route
            for route in app.routes
            if getattr(route, "path", "").startswith("/api/v1/runtime")
        ]
        assert runtime_routes
        assert all("DELETE" not in route.methods for route in runtime_routes)

        with TestClient(app) as test_client:
            client = HTTPRuntimeClient(
                "http://runtime.test/api/v1/runtime",
                opener=_test_client_opener(test_client),
            )
            with pytest.raises(RuntimeHTTPError) as error:
                client.get_execution_job("missing")
            assert error.value.status_code == 404

            mismatch = test_client.put(
                "/api/v1/runtime/sessions/path-session",
                json=_session().model_dump(mode="json"),
            )
            assert mismatch.status_code == 422
            assert mismatch.json()["detail"] == "session UUID path mismatch"
    finally:
        service.repository.close()
