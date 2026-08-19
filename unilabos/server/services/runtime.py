"""以新 ``runtime.db`` 为唯一权威的微后端控制服务。"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from unilabos.server.models.runtime import (
    AdapterCommandOutboxRecord,
    BackendEventOutboxRecord,
    BackendSessionRecord,
    CommandInboxRecord,
    ExecutionJobRecord,
    ExecutorEndpointRecord,
)
from unilabos.server.protocol.common import canonical_hash
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
    ExecutionJobCreate,
    ExecutionJobTransition,
)
from unilabos.server.repositories.runtime import RuntimeRepository


class RuntimeServiceError(RuntimeError):
    code = "runtime_error"


class RuntimeNotFoundError(RuntimeServiceError):
    code = "not_found"


class RuntimeConflictError(RuntimeServiceError):
    code = "conflict"


class RuntimeValidationError(RuntimeServiceError):
    code = "invalid_runtime_request"


class RuntimeService:
    """Session、endpoint、命令、job 和可靠 outbox 的唯一写入口。"""

    def __init__(self, repository: RuntimeRepository | str | Path):
        self.repository = (
            repository
            if isinstance(repository, RuntimeRepository)
            else RuntimeRepository(repository)
        )

    def close(self) -> None:
        self.repository.close()

    @staticmethod
    def _now_ms(observed_at_ms: int = 0) -> int:
        return max(int(time.time() * 1000), observed_at_ms)

    @staticmethod
    def _require_version(actual: int, expected: int, aggregate: str) -> None:
        if actual != expected:
            raise RuntimeConflictError(
                f"{aggregate} version is {actual}, expected {expected}"
            )

    # -- Backend session -------------------------------------------------

    def upsert_backend_session(
        self, value: BackendSessionUpsert
    ) -> BackendSessionRecord:
        timestamp = self._now_ms(value.observed_at_ms)
        with self.repository.write():
            current = self.repository.get_session(value.session_uuid)
            if current is not None:
                current_identity = (
                    current.edge_uuid,
                    current.backend_uri,
                    current.authority_epoch,
                    current.connection_epoch,
                )
                requested_identity = (
                    value.edge_uuid,
                    value.backend_uri,
                    value.authority_epoch,
                    value.connection_epoch,
                )
                if current_identity != requested_identity:
                    raise RuntimeConflictError(
                        "session_uuid was already bound to another backend connection"
                    )

            if value.state == "active":
                self.repository.disconnect_other_active_sessions(
                    value.edge_uuid,
                    value.session_uuid,
                    disconnected_at_ms=timestamp,
                )

            connected_at_ms = value.connected_at_ms
            if current is not None and connected_at_ms is None:
                connected_at_ms = current.connected_at_ms
            if connected_at_ms is None and value.state in {"active", "reconciling"}:
                connected_at_ms = timestamp

            command_cursor = max(
                value.command_cursor,
                current.command_cursor if current is not None else 0,
            )
            event_send_cursor = max(
                value.event_send_cursor,
                current.event_send_cursor if current is not None else 0,
            )
            event_ack_sequence = max(
                value.event_ack_sequence,
                current.event_ack_sequence if current is not None else 0,
            )
            record = BackendSessionRecord(
                session_uuid=value.session_uuid,
                edge_uuid=value.edge_uuid,
                backend_uri=value.backend_uri,
                authority_epoch=value.authority_epoch,
                connection_epoch=value.connection_epoch,
                state=value.state,
                command_cursor=command_cursor,
                event_send_cursor=event_send_cursor,
                event_ack_sequence=event_ack_sequence,
                connected_at_ms=connected_at_ms,
                disconnected_at_ms=value.disconnected_at_ms,
                last_seen_at_ms=timestamp,
                version=1 if current is None else current.version + 1,
            )
            if current is None:
                self.repository.insert_session(record)
            else:
                self.repository.update_session(record, expected_version=current.version)
            return record

    def get_backend_session(self, session_uuid: str) -> BackendSessionRecord:
        record = self.repository.get_session(session_uuid)
        if record is None:
            raise RuntimeNotFoundError(f"backend session {session_uuid!r} not found")
        return record

    # -- Endpoint snapshot ----------------------------------------------

    @staticmethod
    def _endpoint_snapshot_hash(value: EndpointSnapshotUpsert) -> str:
        data = value.model_dump(
            mode="json",
            exclude={"observed_at_ms", "reconciled_at_ms"},
            exclude_none=False,
        )
        return canonical_hash(data)

    def upsert_endpoint_snapshot(
        self, value: EndpointSnapshotUpsert
    ) -> EndpointSnapshotResult:
        timestamp = self._now_ms(value.observed_at_ms)
        snapshot_hash = self._endpoint_snapshot_hash(value)
        with self.repository.write():
            current = self.repository.get_endpoint(value.endpoint_uuid)
            by_identity = self.repository.get_endpoint_by_identity(
                value.transport, value.host_uuid, value.instance_name
            )
            if (
                by_identity is not None
                and by_identity.endpoint_uuid != value.endpoint_uuid
            ):
                raise RuntimeConflictError(
                    "transport/host/instance identity belongs to another endpoint_uuid"
                )
            if current is not None:
                identity = (
                    current.transport,
                    current.host_uuid,
                    current.instance_name,
                )
                requested = (value.transport, value.host_uuid, value.instance_name)
                if identity != requested:
                    raise RuntimeConflictError(
                        "endpoint_uuid was already bound to another executor identity"
                    )

            changed = current is None or current.snapshot_hash != snapshot_hash
            same_adapter_epoch = (
                current is not None and current.adapter_epoch == value.adapter_epoch
            )
            record = ExecutorEndpointRecord(
                endpoint_uuid=value.endpoint_uuid,
                transport=value.transport,
                host_uuid=value.host_uuid,
                instance_name=value.instance_name,
                authority_epoch=value.authority_epoch,
                adapter_epoch=value.adapter_epoch,
                adapter_event_cursor=(
                    current.adapter_event_cursor if same_adapter_epoch else 0
                ),
                reconciliation_generation=value.reconciliation_generation,
                state=value.state,
                device_routes=value.device_routes,
                action_capabilities=value.action_capabilities,
                config=value.config,
                snapshot_hash=snapshot_hash,
                registered_at_ms=(
                    current.registered_at_ms if current is not None else timestamp
                ),
                last_seen_at_ms=timestamp,
                reconciled_at_ms=value.reconciled_at_ms,
                version=(
                    1
                    if current is None
                    else current.version + 1
                    if changed
                    else current.version
                ),
            )
            if current is None:
                self.repository.insert_endpoint(record)
            else:
                self.repository.update_endpoint(
                    record, expected_version=current.version
                )
            return EndpointSnapshotResult(endpoint=record, changed=changed)

    def get_endpoint_snapshot(self, endpoint_uuid: str) -> ExecutorEndpointRecord:
        record = self.repository.get_endpoint(endpoint_uuid)
        if record is None:
            raise RuntimeNotFoundError(f"endpoint {endpoint_uuid!r} not found")
        return record

    # -- Command inbox ---------------------------------------------------

    @staticmethod
    def _command_fingerprint(value: CommandEnvelope) -> str:
        return canonical_hash(value.model_dump(mode="json", exclude={"received_at_ms"}))

    def receive_command(self, value: CommandEnvelope) -> CommandReceipt:
        fingerprint = self._command_fingerprint(value)
        timestamp = self._now_ms(value.received_at_ms)
        with self.repository.write():
            current = self.repository.get_command(value.command_uuid)
            if current is not None:
                if (
                    current.command_fingerprint != fingerprint
                    or current.session_uuid != value.session_uuid
                    or current.backend_sequence != value.backend_sequence
                ):
                    raise RuntimeConflictError(
                        "command_uuid was replayed with different content"
                    )
                return CommandReceipt(
                    command_uuid=current.command_uuid,
                    backend_sequence=current.backend_sequence,
                    command_fingerprint=current.command_fingerprint,
                    replayed=True,
                )

            occupied = self.repository.get_command_by_sequence(
                value.session_uuid, value.backend_sequence
            )
            if occupied is not None:
                raise RuntimeConflictError(
                    "backend sequence was already used by another command"
                )
            session = self.repository.get_session(value.session_uuid)
            if session is None:
                raise RuntimeNotFoundError(
                    f"backend session {value.session_uuid!r} not found"
                )
            expected_sequence = session.command_cursor + 1
            if value.backend_sequence != expected_sequence:
                raise RuntimeConflictError(
                    f"backend sequence is {value.backend_sequence}, "
                    f"expected {expected_sequence}"
                )

            record = CommandInboxRecord(
                command_uuid=value.command_uuid,
                session_uuid=value.session_uuid,
                backend_sequence=value.backend_sequence,
                command_type=value.command_type,
                job_uuid=value.job_uuid,
                payload_uuid=value.payload_uuid,
                payload_sha256=value.payload_sha256,
                command_fingerprint=fingerprint,
                summary=value.summary,
                traceparent=value.traceparent,
                status="received",
                received_at_ms=timestamp,
            )
            self.repository.insert_command(record)
            self.repository.update_session(
                session.model_copy(
                    update={
                        "command_cursor": value.backend_sequence,
                        "last_seen_at_ms": timestamp,
                        "version": session.version + 1,
                    }
                ),
                expected_version=session.version,
            )
            return CommandReceipt(
                command_uuid=value.command_uuid,
                backend_sequence=value.backend_sequence,
                command_fingerprint=fingerprint,
            )

    def _complete_command(
        self,
        command: CommandInboxRecord,
        *,
        timestamp: int,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> CommandInboxRecord:
        status = "rejected" if error_code is not None else "applied"
        if command.status in {"applied", "rejected"}:
            if command.status != status:
                raise RuntimeConflictError(
                    "command already has another terminal status"
                )
            return command
        updated = command.model_copy(
            update={
                "status": status,
                "applied_at_ms": timestamp,
                "error_code": error_code,
                "error_message": error_message,
                "version": command.version + 1,
            }
        )
        self.repository.update_command(updated, expected_version=command.version)
        return updated

    # -- Execution jobs --------------------------------------------------

    def create_execution_job(self, value: ExecutionJobCreate) -> ExecutionJobRecord:
        timestamp = self._now_ms(value.accepted_at_ms)
        with self.repository.write():
            command = self.repository.get_command(value.execute_command_uuid)
            if command is None:
                raise RuntimeNotFoundError(
                    f"execute command {value.execute_command_uuid!r} not found"
                )
            if (
                command.command_type != "execute_job"
                or command.job_uuid != value.job_uuid
            ):
                raise RuntimeValidationError(
                    "execute command type/job_uuid does not match execution job"
                )

            current = self.repository.get_job(value.job_uuid)
            if current is not None:
                if current.execute_command_uuid != value.execute_command_uuid:
                    raise RuntimeConflictError(
                        "job_uuid was already created by another command"
                    )
                return current
            if command.status in {"applied", "rejected"}:
                raise RuntimeConflictError(
                    "terminal execute command has no matching execution job"
                )

            if value.retry_of_job_uuid is not None:
                previous = self.repository.get_job(value.retry_of_job_uuid)
                if previous is None:
                    raise RuntimeNotFoundError(
                        f"retry source {value.retry_of_job_uuid!r} not found"
                    )
                if previous.status not in {"failed", "canceled", "rejected"}:
                    raise RuntimeConflictError("retry source is not terminal")
                if (
                    previous.attempt_group_uuid != value.attempt_group_uuid
                    or previous.task_uuid != value.task_uuid
                    or previous.node_uuid != value.node_uuid
                    or value.attempt_no != previous.attempt_no + 1
                ):
                    raise RuntimeValidationError(
                        "retry must continue the same task/node attempt group"
                    )

            if value.endpoint_uuid is not None:
                endpoint = self.repository.get_endpoint(value.endpoint_uuid)
                if endpoint is None:
                    raise RuntimeNotFoundError(
                        f"endpoint {value.endpoint_uuid!r} not found"
                    )
                if endpoint.transport != value.transport:
                    raise RuntimeValidationError(
                        "job transport does not match executor endpoint"
                    )

            record = ExecutionJobRecord(
                **value.model_dump(mode="json", exclude={"accepted_at_ms"}),
                status="accepted",
                accepted_at_ms=timestamp,
            )
            try:
                self.repository.insert_job(record)
            except sqlite3.IntegrityError as exc:
                raise RuntimeConflictError(str(exc)) from exc
            self._complete_command(command, timestamp=timestamp)
            return record

    def get_execution_job(self, job_uuid: str) -> ExecutionJobRecord:
        record = self.repository.get_job(job_uuid)
        if record is None:
            raise RuntimeNotFoundError(f"execution job {job_uuid!r} not found")
        return record

    _TRANSITIONS = {
        "accepted": {"dispatch_pending", "rejected", "canceled"},
        "dispatch_pending": {"dispatched", "rejected", "canceled"},
        "dispatched": {"running", "execution_unknown", "canceled"},
        "running": {"succeeded", "execution_unknown", "canceled"},
        "failure_waiting": {"execution_unknown", "canceled"},
        "terminal_waiting": {"succeeded", "failed", "canceled"},
        "execution_unknown": {"running", "succeeded", "canceled"},
    }

    def transition_execution_job(
        self, job_uuid: str, value: ExecutionJobTransition
    ) -> ExecutionJobRecord:
        timestamp = self._now_ms(value.occurred_at_ms)
        with self.repository.write():
            current = self.repository.get_job(job_uuid)
            if current is None:
                raise RuntimeNotFoundError(f"execution job {job_uuid!r} not found")
            self._require_version(current.version, value.expected_version, "job")
            if value.status not in self._TRANSITIONS.get(current.status, set()):
                raise RuntimeConflictError(
                    f"cannot transition job from {current.status} to {value.status}"
                )
            if (
                value.status == "failed"
                and current.terminal_gate_state != "released_failed"
            ):
                raise RuntimeConflictError(
                    "failed cannot be persisted before backend releases the error gate"
                )
            if value.status == "succeeded" and current.terminal_gate_state not in {
                "none",
                "result_replaced",
            }:
                raise RuntimeConflictError("open error gate does not allow succeeded")
            if value.status == "canceled" and current.terminal_gate_state not in {
                "none",
                "canceled",
            }:
                raise RuntimeConflictError("open error gate does not allow canceled")

            updates: dict[str, Any] = {
                "status": value.status,
                "version": current.version + 1,
            }
            for field in (
                "scheduler_status_version",
                "feedback_sequence",
                "result_uuid",
                "error_code",
                "error_summary",
            ):
                supplied = getattr(value, field)
                if supplied is not None:
                    updates[field] = supplied
            if value.status == "dispatched":
                updates["dispatched_at_ms"] = timestamp
            elif value.status == "running":
                updates["started_at_ms"] = timestamp
            elif value.status in {"succeeded", "failed", "canceled", "rejected"}:
                updates["finished_at_ms"] = timestamp
            updated = current.model_copy(update=updates)
            self.repository.update_job(updated, expected_version=current.version)
            return updated

    # -- Backend-controlled terminal error gate -------------------------

    def open_error_gate(
        self, job_uuid: str, value: ErrorGateOpen
    ) -> ExecutionJobRecord:
        timestamp = self._now_ms(value.opened_at_ms)
        with self.repository.write():
            current = self.repository.get_job(job_uuid)
            if current is None:
                raise RuntimeNotFoundError(f"execution job {job_uuid!r} not found")
            if current.terminal_gate_state != "none":
                if (
                    current.terminal_error_uuid == value.error_uuid
                    and current.terminal_request_event_uuid == value.request_event_uuid
                ):
                    return current
                raise RuntimeConflictError(
                    "job already has another terminal error gate"
                )
            self._require_version(current.version, value.expected_version, "job")
            if current.status not in {"dispatched", "running", "execution_unknown"}:
                raise RuntimeConflictError(
                    f"job status {current.status!r} cannot open an error gate"
                )

            updated = current.model_copy(
                update={
                    "status": "terminal_waiting",
                    "error_code": value.error_code,
                    "error_summary": value.error_summary,
                    "terminal_gate_state": "waiting_backend",
                    "terminal_error_uuid": value.error_uuid,
                    "terminal_required_scheduler_revision": (
                        value.required_scheduler_revision
                    ),
                    "terminal_request_event_uuid": value.request_event_uuid,
                    "terminal_opened_at_ms": timestamp,
                    "version": current.version + 1,
                }
            )
            self.repository.update_job(updated, expected_version=current.version)
            summary = {
                **value.summary,
                "error_uuid": value.error_uuid,
                "error_code": value.error_code,
                "error_summary": value.error_summary,
                "required_scheduler_revision": value.required_scheduler_revision,
            }
            self._insert_backend_event(
                BackendEventEnqueue(
                    event_uuid=value.request_event_uuid,
                    event_type="execution.error_pending",
                    aggregate_type="execution_job",
                    aggregate_uuid=job_uuid,
                    aggregate_version=updated.version,
                    job_uuid=job_uuid,
                    summary=summary,
                    available_at_ms=timestamp,
                ),
                timestamp=timestamp,
            )
            return updated

    def decide_error_gate(
        self, job_uuid: str, value: ErrorGateDecision
    ) -> ExecutionJobRecord:
        timestamp = self._now_ms(value.resolved_at_ms)
        with self.repository.write():
            current = self.repository.get_job(job_uuid)
            if current is None:
                raise RuntimeNotFoundError(f"execution job {job_uuid!r} not found")
            self._require_version(current.version, value.expected_version, "job")
            if current.terminal_gate_state not in {
                "waiting_backend",
                "backend_confirmed",
            }:
                raise RuntimeConflictError("job has no backend-waiting error gate")
            required = current.terminal_required_scheduler_revision or 0
            if value.confirmed_scheduler_revision < required:
                raise RuntimeConflictError(
                    "scheduler revision has not reached the terminal gate requirement"
                )
            command = self.repository.get_command(value.decision_command_uuid)
            if command is None:
                raise RuntimeNotFoundError(
                    f"decision command {value.decision_command_uuid!r} not found"
                )
            expected_type = {
                "release_failed": "release_failed",
                "replace_result": "replace_result",
                "cancel": "cancel_job",
            }[value.action]
            if command.command_type != expected_type or command.job_uuid != job_uuid:
                raise RuntimeValidationError(
                    "decision command type/job_uuid does not match terminal action"
                )
            if current.endpoint_uuid is None:
                raise RuntimeValidationError(
                    "routed endpoint is required to release an execution error gate"
                )

            gate_state = {
                "release_failed": "released_failed",
                "replace_result": "result_replaced",
                "cancel": "canceled",
            }[value.action]
            decision = {**value.decision, "action": value.action}
            updated = current.model_copy(
                update={
                    "terminal_gate_state": gate_state,
                    "terminal_confirmed_scheduler_revision": (
                        value.confirmed_scheduler_revision
                    ),
                    "terminal_decision_command_uuid": value.decision_command_uuid,
                    "terminal_decision": decision,
                    "terminal_resolved_at_ms": timestamp,
                    "result_uuid": value.result_uuid or current.result_uuid,
                    "version": current.version + 1,
                }
            )
            self.repository.update_job(updated, expected_version=current.version)
            endpoint = self.repository.get_endpoint(current.endpoint_uuid)
            if endpoint is None:
                raise RuntimeNotFoundError(
                    f"endpoint {current.endpoint_uuid!r} not found"
                )
            self._insert_adapter_command(
                AdapterCommandEnqueue(
                    adapter_command_uuid=value.adapter_command_uuid,
                    job_uuid=job_uuid,
                    endpoint_uuid=current.endpoint_uuid,
                    source_command_uuid=value.decision_command_uuid,
                    target_adapter_epoch=endpoint.adapter_epoch,
                    command_type=(
                        "cancel" if value.action == "cancel" else value.action
                    ),
                    payload_uuid=value.payload_uuid,
                    available_at_ms=timestamp,
                ),
                timestamp=timestamp,
            )
            self._complete_command(command, timestamp=timestamp)
            return updated

    # -- Adapter command outbox -----------------------------------------

    @staticmethod
    def _same_adapter_command(
        current: AdapterCommandOutboxRecord, value: AdapterCommandEnqueue
    ) -> bool:
        fields = (
            "job_uuid",
            "endpoint_uuid",
            "source_command_uuid",
            "trigger_event_uuid",
            "target_adapter_epoch",
            "command_type",
            "payload_uuid",
        )
        return all(getattr(current, field) == getattr(value, field) for field in fields)

    def _insert_adapter_command(
        self, value: AdapterCommandEnqueue, *, timestamp: int
    ) -> AdapterCommandOutboxRecord:
        current = self.repository.get_adapter_command(value.adapter_command_uuid)
        if current is not None:
            if not self._same_adapter_command(current, value):
                raise RuntimeConflictError(
                    "adapter_command_uuid was replayed with different content"
                )
            return current
        record = AdapterCommandOutboxRecord(
            **value.model_dump(mode="json"),
            status="pending",
            created_at_ms=timestamp,
        )
        try:
            sequence = self.repository.insert_adapter_command(record)
        except sqlite3.IntegrityError as exc:
            raise RuntimeConflictError(str(exc)) from exc
        return record.model_copy(update={"sequence": sequence})

    def enqueue_adapter_command(
        self, value: AdapterCommandEnqueue
    ) -> AdapterCommandOutboxRecord:
        timestamp = self._now_ms(value.available_at_ms)
        with self.repository.write():
            return self._insert_adapter_command(value, timestamp=timestamp)

    def claim_adapter_commands(
        self, value: AdapterCommandClaim
    ) -> list[AdapterCommandOutboxRecord]:
        timestamp = self._now_ms(value.now_ms)
        with self.repository.write():
            if self.repository.get_endpoint(value.endpoint_uuid) is None:
                raise RuntimeNotFoundError(
                    f"endpoint {value.endpoint_uuid!r} not found"
                )
            return self.repository.claim_adapter_commands(
                value.endpoint_uuid,
                now_ms=timestamp,
                lease_until_ms=timestamp + value.lease_ms,
                limit=value.limit,
            )

    def acknowledge_adapter_command(
        self, value: AdapterCommandAck
    ) -> AdapterCommandOutboxRecord:
        timestamp = self._now_ms(value.acknowledged_at_ms)
        with self.repository.write():
            current = self.repository.get_adapter_command(value.adapter_command_uuid)
            if current is None:
                raise RuntimeNotFoundError(
                    f"adapter command {value.adapter_command_uuid!r} not found"
                )
            if current.status == "acknowledged":
                if current.ack_event_uuid != value.ack_event_uuid:
                    raise RuntimeConflictError(
                        "adapter command was ACKed by another event"
                    )
                return current
            if current.status != "sent":
                raise RuntimeConflictError("adapter command must be claimed before ACK")
            self.repository.acknowledge_adapter_command(
                value.adapter_command_uuid,
                ack_event_uuid=value.ack_event_uuid,
                acked_at_ms=timestamp,
            )
            acknowledged = self.repository.get_adapter_command(
                value.adapter_command_uuid
            )
            assert acknowledged is not None
            return acknowledged

    # -- Backend event outbox -------------------------------------------

    @staticmethod
    def _same_backend_event(
        current: BackendEventOutboxRecord, value: BackendEventEnqueue
    ) -> bool:
        fields = (
            "event_type",
            "aggregate_type",
            "aggregate_uuid",
            "aggregate_version",
            "job_uuid",
            "summary",
            "detail_payload_uuid",
            "traceparent",
            "tracestate",
        )
        return all(getattr(current, field) == getattr(value, field) for field in fields)

    def _insert_backend_event(
        self, value: BackendEventEnqueue, *, timestamp: int
    ) -> BackendEventOutboxRecord:
        current = self.repository.get_backend_event(value.event_uuid)
        if current is not None:
            if not self._same_backend_event(current, value):
                raise RuntimeConflictError(
                    "event_uuid was replayed with different content"
                )
            return current
        record = BackendEventOutboxRecord(
            **value.model_dump(mode="json"),
            status="pending",
            created_at_ms=timestamp,
        )
        try:
            sequence = self.repository.insert_backend_event(record)
        except sqlite3.IntegrityError as exc:
            raise RuntimeConflictError(str(exc)) from exc
        return record.model_copy(update={"sequence": sequence})

    def enqueue_backend_event(
        self, value: BackendEventEnqueue
    ) -> BackendEventOutboxRecord:
        timestamp = self._now_ms(value.available_at_ms)
        with self.repository.write():
            return self._insert_backend_event(value, timestamp=timestamp)

    def claim_backend_events(
        self, value: BackendEventClaim
    ) -> list[BackendEventOutboxRecord]:
        timestamp = self._now_ms(value.now_ms)
        with self.repository.write():
            session = self.repository.get_session(value.session_uuid)
            if session is None:
                raise RuntimeNotFoundError(
                    f"backend session {value.session_uuid!r} not found"
                )
            if session.state == "disconnected":
                raise RuntimeConflictError("disconnected session cannot claim events")
            events = self.repository.claim_backend_events(
                now_ms=timestamp,
                lease_until_ms=timestamp + value.lease_ms,
                limit=value.limit,
            )
            if events:
                last_sequence = max(item.sequence or 0 for item in events)
                self.repository.update_session(
                    session.model_copy(
                        update={
                            "event_send_cursor": max(
                                session.event_send_cursor, last_sequence
                            ),
                            "last_seen_at_ms": timestamp,
                            "version": session.version + 1,
                        }
                    ),
                    expected_version=session.version,
                )
            return events

    def acknowledge_backend_events(self, value: BackendEventAck) -> int:
        timestamp = self._now_ms(value.acknowledged_at_ms)
        with self.repository.write():
            session = self.repository.get_session(value.session_uuid)
            if session is None:
                raise RuntimeNotFoundError(
                    f"backend session {value.session_uuid!r} not found"
                )
            if value.through_sequence > session.event_send_cursor:
                raise RuntimeConflictError("ACK exceeds the event send cursor")
            if value.through_sequence <= session.event_ack_sequence:
                return 0
            count = self.repository.acknowledge_backend_events(
                through_sequence=value.through_sequence,
                acked_at_ms=timestamp,
            )
            self.repository.update_session(
                session.model_copy(
                    update={
                        "event_ack_sequence": value.through_sequence,
                        "last_seen_at_ms": timestamp,
                        "version": session.version + 1,
                    }
                ),
                expected_version=session.version,
            )
            return count


__all__ = [
    "RuntimeConflictError",
    "RuntimeNotFoundError",
    "RuntimeService",
    "RuntimeServiceError",
    "RuntimeValidationError",
]
