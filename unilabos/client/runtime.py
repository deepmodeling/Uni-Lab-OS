"""``runtime.v1`` 的 Local/HTTP 等价客户端。"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Optional
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from unilabos.server.database.tables.runtime import (
    AdapterCommandOutboxRecord,
    BackendEventOutboxRecord,
    BackendSessionRecord,
    CommandInboxRecord,
    ExecutionJobRecord,
    ExecutorEndpointRecord,
)
from unilabos.server.protocol.runtime import (
    AdapterCommandAck,
    AdapterCommandClaim,
    AdapterCommandEnqueue,
    BackendEventAck,
    BackendEventClaim,
    BackendEventEnqueue,
    BackendSessionUpsert,
    CommandEnvelope,
    CommandReceipt,
    EndpointSnapshotResult,
    EndpointSnapshotUpsert,
    ErrorGateDecision,
    ErrorGateOpen,
    ExecutionJobCancel,
    ExecutionJobCreate,
    ExecutionJobFeedback,
    ExecutionJobTransition,
)
from unilabos.server.services.runtime import RuntimeService


def _query(path: str, **values: Any) -> str:
    query = urlencode(
        {key: value for key, value in values.items() if value is not None}
    )
    return f"{path}?{query}" if query else path


class LocalRuntimeClient:
    """同进程调用入口；方法和 HTTP client 保持一致。"""

    def __init__(self, service: RuntimeService):
        self.service = service

    def upsert_backend_session(
        self, value: BackendSessionUpsert
    ) -> BackendSessionRecord:
        return self.service.upsert_backend_session(value)

    def get_backend_session(self, session_uuid: str) -> BackendSessionRecord:
        return self.service.get_backend_session(session_uuid)

    def list_backend_sessions(
        self,
        *,
        edge_uuid: Optional[str] = None,
        state: Optional[str] = None,
        limit: int = 100,
    ) -> list[BackendSessionRecord]:
        return self.service.list_backend_sessions(
            edge_uuid=edge_uuid, state=state, limit=limit
        )

    def upsert_endpoint_snapshot(
        self, value: EndpointSnapshotUpsert
    ) -> EndpointSnapshotResult:
        return self.service.upsert_endpoint_snapshot(value)

    def get_endpoint_snapshot(self, endpoint_uuid: str) -> ExecutorEndpointRecord:
        return self.service.get_endpoint_snapshot(endpoint_uuid)

    def list_endpoint_snapshots(
        self,
        *,
        transport: Optional[str] = None,
        state: Optional[str] = None,
        host_uuid: Optional[str] = None,
        limit: int = 100,
    ) -> list[ExecutorEndpointRecord]:
        return self.service.list_endpoint_snapshots(
            transport=transport,
            state=state,
            host_uuid=host_uuid,
            limit=limit,
        )

    def receive_command(self, value: CommandEnvelope) -> CommandReceipt:
        return self.service.receive_command(value)

    def get_command(self, command_uuid: str) -> CommandInboxRecord:
        return self.service.get_command(command_uuid)

    def list_commands(
        self,
        *,
        session_uuid: Optional[str] = None,
        status: Optional[str] = None,
        job_uuid: Optional[str] = None,
        command_type: Optional[str] = None,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> list[CommandInboxRecord]:
        return self.service.list_commands(
            session_uuid=session_uuid,
            status=status,
            job_uuid=job_uuid,
            command_type=command_type,
            after_sequence=after_sequence,
            limit=limit,
        )

    def create_execution_job(self, value: ExecutionJobCreate) -> ExecutionJobRecord:
        return self.service.create_execution_job(value)

    def get_execution_job(self, job_uuid: str) -> ExecutionJobRecord:
        return self.service.get_execution_job(job_uuid)

    def list_execution_jobs(
        self,
        *,
        status: Optional[str] = None,
        device_uuid: Optional[str] = None,
        endpoint_uuid: Optional[str] = None,
        retry_of_job_uuid: Optional[str] = None,
        attempt_group_uuid: Optional[str] = None,
        limit: int = 100,
    ) -> list[ExecutionJobRecord]:
        return self.service.list_execution_jobs(
            status=status,
            device_uuid=device_uuid,
            endpoint_uuid=endpoint_uuid,
            retry_of_job_uuid=retry_of_job_uuid,
            attempt_group_uuid=attempt_group_uuid,
            limit=limit,
        )

    def transition_execution_job(
        self, job_uuid: str, value: ExecutionJobTransition
    ) -> ExecutionJobRecord:
        return self.service.transition_execution_job(job_uuid, value)

    def record_execution_feedback(
        self, job_uuid: str, value: ExecutionJobFeedback
    ) -> ExecutionJobRecord:
        return self.service.record_execution_feedback(job_uuid, value)

    def request_execution_cancel(
        self, job_uuid: str, value: ExecutionJobCancel
    ) -> ExecutionJobRecord:
        return self.service.request_execution_cancel(job_uuid, value)

    def open_error_gate(
        self, job_uuid: str, value: ErrorGateOpen
    ) -> ExecutionJobRecord:
        return self.service.open_error_gate(job_uuid, value)

    def decide_error_gate(
        self, job_uuid: str, value: ErrorGateDecision
    ) -> ExecutionJobRecord:
        return self.service.decide_error_gate(job_uuid, value)

    def enqueue_adapter_command(
        self, value: AdapterCommandEnqueue
    ) -> AdapterCommandOutboxRecord:
        return self.service.enqueue_adapter_command(value)

    def get_adapter_command(
        self, adapter_command_uuid: str
    ) -> AdapterCommandOutboxRecord:
        return self.service.get_adapter_command(adapter_command_uuid)

    def list_adapter_commands(
        self,
        *,
        endpoint_uuid: Optional[str] = None,
        status: Optional[str] = None,
        job_uuid: Optional[str] = None,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> list[AdapterCommandOutboxRecord]:
        return self.service.list_adapter_commands(
            endpoint_uuid=endpoint_uuid,
            status=status,
            job_uuid=job_uuid,
            after_sequence=after_sequence,
            limit=limit,
        )

    def claim_adapter_commands(
        self, value: AdapterCommandClaim
    ) -> list[AdapterCommandOutboxRecord]:
        return self.service.claim_adapter_commands(value)

    def acknowledge_adapter_command(
        self, value: AdapterCommandAck
    ) -> AdapterCommandOutboxRecord:
        return self.service.acknowledge_adapter_command(value)

    def enqueue_backend_event(
        self, value: BackendEventEnqueue
    ) -> BackendEventOutboxRecord:
        return self.service.enqueue_backend_event(value)

    def get_backend_event(self, event_uuid: str) -> BackendEventOutboxRecord:
        return self.service.get_backend_event(event_uuid)

    def list_backend_events(
        self,
        *,
        status: Optional[str] = None,
        job_uuid: Optional[str] = None,
        aggregate_type: Optional[str] = None,
        aggregate_uuid: Optional[str] = None,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> list[BackendEventOutboxRecord]:
        return self.service.list_backend_events(
            status=status,
            job_uuid=job_uuid,
            aggregate_type=aggregate_type,
            aggregate_uuid=aggregate_uuid,
            after_sequence=after_sequence,
            limit=limit,
        )

    def claim_backend_events(
        self, value: BackendEventClaim
    ) -> list[BackendEventOutboxRecord]:
        return self.service.claim_backend_events(value)

    def acknowledge_backend_events(self, value: BackendEventAck) -> int:
        return self.service.acknowledge_backend_events(value)


class RuntimeHTTPError(RuntimeError):
    def __init__(self, status_code: int, detail: str):
        super().__init__(f"runtime API returned {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


class HTTPRuntimeClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 30.0,
        opener: Optional[Callable[..., Any]] = None,
    ):
        base = base_url.rstrip("/")
        if base.endswith("/api/v1/runtime"):
            self.base_url = base
        elif base.endswith("/api/v1"):
            self.base_url = base + "/runtime"
        else:
            self.base_url = base + "/api/v1/runtime"
        self.timeout = timeout
        self._opener = opener or urlopen

    def _request(self, method: str, path: str, body: Optional[Any] = None) -> Any:
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            if hasattr(body, "model_dump"):
                body = body.model_dump(mode="json", exclude_none=False)
            data = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(
            self.base_url + path,
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with self._opener(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raw = exc.read().decode("utf-8")
            try:
                detail = json.loads(raw).get("detail", raw)
            except ValueError:
                detail = raw
            raise RuntimeHTTPError(exc.code, str(detail)) from exc

    def upsert_backend_session(
        self, value: BackendSessionUpsert
    ) -> BackendSessionRecord:
        return BackendSessionRecord.model_validate(
            self._request("PUT", f"/sessions/{value.session_uuid}", value)
        )

    def get_backend_session(self, session_uuid: str) -> BackendSessionRecord:
        return BackendSessionRecord.model_validate(
            self._request("GET", f"/sessions/{session_uuid}")
        )

    def list_backend_sessions(
        self,
        *,
        edge_uuid: Optional[str] = None,
        state: Optional[str] = None,
        limit: int = 100,
    ) -> list[BackendSessionRecord]:
        response = self._request(
            "GET",
            _query("/sessions", edge_uuid=edge_uuid, state=state, limit=limit),
        )
        return [BackendSessionRecord.model_validate(item) for item in response]

    def upsert_endpoint_snapshot(
        self, value: EndpointSnapshotUpsert
    ) -> EndpointSnapshotResult:
        return EndpointSnapshotResult.model_validate(
            self._request("PUT", f"/endpoints/{value.endpoint_uuid}/snapshot", value)
        )

    def get_endpoint_snapshot(self, endpoint_uuid: str) -> ExecutorEndpointRecord:
        return ExecutorEndpointRecord.model_validate(
            self._request("GET", f"/endpoints/{endpoint_uuid}")
        )

    def list_endpoint_snapshots(
        self,
        *,
        transport: Optional[str] = None,
        state: Optional[str] = None,
        host_uuid: Optional[str] = None,
        limit: int = 100,
    ) -> list[ExecutorEndpointRecord]:
        response = self._request(
            "GET",
            _query(
                "/endpoints",
                transport=transport,
                state=state,
                host_uuid=host_uuid,
                limit=limit,
            ),
        )
        return [ExecutorEndpointRecord.model_validate(item) for item in response]

    def receive_command(self, value: CommandEnvelope) -> CommandReceipt:
        return CommandReceipt.model_validate(self._request("POST", "/commands", value))

    def get_command(self, command_uuid: str) -> CommandInboxRecord:
        return CommandInboxRecord.model_validate(
            self._request("GET", f"/commands/{command_uuid}")
        )

    def list_commands(
        self,
        *,
        session_uuid: Optional[str] = None,
        status: Optional[str] = None,
        job_uuid: Optional[str] = None,
        command_type: Optional[str] = None,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> list[CommandInboxRecord]:
        response = self._request(
            "GET",
            _query(
                "/commands",
                session_uuid=session_uuid,
                status=status,
                job_uuid=job_uuid,
                command_type=command_type,
                after_sequence=after_sequence,
                limit=limit,
            ),
        )
        return [CommandInboxRecord.model_validate(item) for item in response]

    def create_execution_job(self, value: ExecutionJobCreate) -> ExecutionJobRecord:
        return ExecutionJobRecord.model_validate(self._request("POST", "/jobs", value))

    def get_execution_job(self, job_uuid: str) -> ExecutionJobRecord:
        return ExecutionJobRecord.model_validate(
            self._request("GET", f"/jobs/{job_uuid}")
        )

    def list_execution_jobs(
        self,
        *,
        status: Optional[str] = None,
        device_uuid: Optional[str] = None,
        endpoint_uuid: Optional[str] = None,
        retry_of_job_uuid: Optional[str] = None,
        attempt_group_uuid: Optional[str] = None,
        limit: int = 100,
    ) -> list[ExecutionJobRecord]:
        response = self._request(
            "GET",
            _query(
                "/jobs",
                status=status,
                device_uuid=device_uuid,
                endpoint_uuid=endpoint_uuid,
                retry_of_job_uuid=retry_of_job_uuid,
                attempt_group_uuid=attempt_group_uuid,
                limit=limit,
            ),
        )
        return [ExecutionJobRecord.model_validate(item) for item in response]

    def transition_execution_job(
        self, job_uuid: str, value: ExecutionJobTransition
    ) -> ExecutionJobRecord:
        return ExecutionJobRecord.model_validate(
            self._request("POST", f"/jobs/{job_uuid}/transitions", value)
        )

    def record_execution_feedback(
        self, job_uuid: str, value: ExecutionJobFeedback
    ) -> ExecutionJobRecord:
        return ExecutionJobRecord.model_validate(
            self._request("POST", f"/jobs/{job_uuid}/feedback", value)
        )

    def request_execution_cancel(
        self, job_uuid: str, value: ExecutionJobCancel
    ) -> ExecutionJobRecord:
        return ExecutionJobRecord.model_validate(
            self._request("POST", f"/jobs/{job_uuid}/cancel", value)
        )

    def open_error_gate(
        self, job_uuid: str, value: ErrorGateOpen
    ) -> ExecutionJobRecord:
        return ExecutionJobRecord.model_validate(
            self._request("POST", f"/jobs/{job_uuid}/error-gate/open", value)
        )

    def decide_error_gate(
        self, job_uuid: str, value: ErrorGateDecision
    ) -> ExecutionJobRecord:
        return ExecutionJobRecord.model_validate(
            self._request("POST", f"/jobs/{job_uuid}/error-gate/decision", value)
        )

    def enqueue_adapter_command(
        self, value: AdapterCommandEnqueue
    ) -> AdapterCommandOutboxRecord:
        return AdapterCommandOutboxRecord.model_validate(
            self._request("POST", "/adapter-commands", value)
        )

    def get_adapter_command(
        self, adapter_command_uuid: str
    ) -> AdapterCommandOutboxRecord:
        return AdapterCommandOutboxRecord.model_validate(
            self._request("GET", f"/adapter-commands/{adapter_command_uuid}")
        )

    def list_adapter_commands(
        self,
        *,
        endpoint_uuid: Optional[str] = None,
        status: Optional[str] = None,
        job_uuid: Optional[str] = None,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> list[AdapterCommandOutboxRecord]:
        response = self._request(
            "GET",
            _query(
                "/adapter-commands",
                endpoint_uuid=endpoint_uuid,
                status=status,
                job_uuid=job_uuid,
                after_sequence=after_sequence,
                limit=limit,
            ),
        )
        return [AdapterCommandOutboxRecord.model_validate(item) for item in response]

    def claim_adapter_commands(
        self, value: AdapterCommandClaim
    ) -> list[AdapterCommandOutboxRecord]:
        response = self._request("POST", "/adapter-commands/claim", value)
        return [AdapterCommandOutboxRecord.model_validate(item) for item in response]

    def acknowledge_adapter_command(
        self, value: AdapterCommandAck
    ) -> AdapterCommandOutboxRecord:
        return AdapterCommandOutboxRecord.model_validate(
            self._request("POST", "/adapter-commands/ack", value)
        )

    def enqueue_backend_event(
        self, value: BackendEventEnqueue
    ) -> BackendEventOutboxRecord:
        return BackendEventOutboxRecord.model_validate(
            self._request("POST", "/backend-events", value)
        )

    def get_backend_event(self, event_uuid: str) -> BackendEventOutboxRecord:
        return BackendEventOutboxRecord.model_validate(
            self._request("GET", f"/backend-events/{event_uuid}")
        )

    def list_backend_events(
        self,
        *,
        status: Optional[str] = None,
        job_uuid: Optional[str] = None,
        aggregate_type: Optional[str] = None,
        aggregate_uuid: Optional[str] = None,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> list[BackendEventOutboxRecord]:
        response = self._request(
            "GET",
            _query(
                "/backend-events",
                status=status,
                job_uuid=job_uuid,
                aggregate_type=aggregate_type,
                aggregate_uuid=aggregate_uuid,
                after_sequence=after_sequence,
                limit=limit,
            ),
        )
        return [BackendEventOutboxRecord.model_validate(item) for item in response]

    def claim_backend_events(
        self, value: BackendEventClaim
    ) -> list[BackendEventOutboxRecord]:
        response = self._request("POST", "/backend-events/claim", value)
        return [BackendEventOutboxRecord.model_validate(item) for item in response]

    def acknowledge_backend_events(self, value: BackendEventAck) -> int:
        response = self._request("POST", "/backend-events/ack", value)
        return int(response["acknowledged"])


__all__ = ["HTTPRuntimeClient", "LocalRuntimeClient", "RuntimeHTTPError"]
