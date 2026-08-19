"""``runtime.db`` 的微后端控制记录。"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import Field, model_validator

from unilabos.server.models.base import (
    JsonObject,
    NonEmptyStr,
    PositiveVersion,
    ServerObject,
    UnixMilliseconds,
)


Transport = Literal["hostlink", "ros2"]


class BackendSessionRecord(ServerObject):
    session_uuid: NonEmptyStr
    edge_uuid: NonEmptyStr
    backend_uri: NonEmptyStr
    authority_epoch: NonEmptyStr
    connection_epoch: NonEmptyStr = "initial"
    state: Literal["connecting", "active", "reconciling", "disconnected"]
    command_cursor: int = Field(default=0, ge=0)
    event_send_cursor: int = Field(default=0, ge=0)
    event_ack_sequence: int = Field(default=0, ge=0)
    connected_at_ms: Optional[UnixMilliseconds] = None
    disconnected_at_ms: Optional[UnixMilliseconds] = None
    last_seen_at_ms: UnixMilliseconds
    version: PositiveVersion = 1

    @model_validator(mode="after")
    def _validate_session_cursors(self) -> "BackendSessionRecord":
        if self.event_ack_sequence > self.event_send_cursor:
            raise ValueError("event acknowledgment cannot pass the send cursor")
        disconnected = self.state == "disconnected"
        if disconnected != (self.disconnected_at_ms is not None):
            raise ValueError("disconnected session must have disconnected_at_ms")
        return self


class ExecutorEndpointRecord(ServerObject):
    endpoint_uuid: NonEmptyStr
    transport: Transport
    host_uuid: NonEmptyStr
    instance_name: NonEmptyStr
    authority_epoch: NonEmptyStr
    adapter_epoch: Optional[NonEmptyStr] = None
    adapter_event_cursor: int = Field(default=0, ge=0)
    reconciliation_generation: int = Field(default=0, ge=0)
    state: Literal["online", "offline", "reconciling"]
    capabilities_json: JsonObject = Field(default_factory=dict)
    registered_at_ms: UnixMilliseconds
    last_seen_at_ms: UnixMilliseconds
    reconciled_at_ms: Optional[UnixMilliseconds] = None
    version: PositiveVersion = 1


class DeviceRouteRecord(ServerObject):
    route_uuid: NonEmptyStr
    device_uuid: NonEmptyStr
    endpoint_uuid: NonEmptyStr
    transport: Transport
    driver_key: NonEmptyStr
    priority: int = 0
    enabled: bool = True
    selected: bool = False
    config_hash: NonEmptyStr
    created_at_ms: UnixMilliseconds
    updated_at_ms: UnixMilliseconds
    version: PositiveVersion = 1

    @model_validator(mode="after")
    def _selected_route_must_be_enabled(self) -> "DeviceRouteRecord":
        if self.selected and not self.enabled:
            raise ValueError("selected device route must be enabled")
        return self


class DeviceActionCapabilityRecord(ServerObject):
    capability_uuid: NonEmptyStr
    endpoint_uuid: NonEmptyStr
    device_uuid: NonEmptyStr
    action_name: NonEmptyStr
    action_type: Optional[str] = None
    concurrency_mode: Literal["exclusive", "unbounded"]
    state: Literal["active", "retired"]
    descriptor_json: JsonObject = Field(default_factory=dict)
    descriptor_hash: NonEmptyStr
    discovery_epoch: NonEmptyStr
    discovery_generation: int = Field(ge=0)
    discovered_at_ms: UnixMilliseconds
    last_seen_at_ms: UnixMilliseconds
    version: PositiveVersion = 1


class CommandInboxRecord(ServerObject):
    command_uuid: NonEmptyStr
    session_uuid: NonEmptyStr
    backend_sequence: int = Field(ge=1)
    command_type: Literal[
        "execute_job",
        "cancel_job",
        "release_failed",
        "replace_result",
        "inventory_apply",
        "reconcile",
    ]
    job_uuid: Optional[NonEmptyStr] = None
    payload_uuid: Optional[NonEmptyStr] = None
    payload_sha256: NonEmptyStr
    command_fingerprint: NonEmptyStr
    summary_json: JsonObject = Field(default_factory=dict)
    traceparent: Optional[str] = None
    status: Literal["received", "applying", "applied", "rejected"]
    received_at_ms: UnixMilliseconds
    applied_at_ms: Optional[UnixMilliseconds] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    version: PositiveVersion = 1

    @model_validator(mode="after")
    def _validate_command_scope_and_status(self) -> "CommandInboxRecord":
        job_commands = {
            "execute_job",
            "cancel_job",
            "release_failed",
            "replace_result",
        }
        if self.command_type in job_commands and self.job_uuid is None:
            raise ValueError("job command requires job_uuid")
        if self.command_type == "reconcile" and self.job_uuid is not None:
            raise ValueError("reconcile command is not job-scoped")
        completed = self.status in {"applied", "rejected"}
        if completed != (self.applied_at_ms is not None):
            raise ValueError("completed command must have applied_at_ms")
        return self


class ExecutionJobRecord(ServerObject):
    job_uuid: NonEmptyStr
    task_uuid: NonEmptyStr
    node_uuid: NonEmptyStr
    attempt_group_uuid: NonEmptyStr
    retry_of_job_uuid: Optional[NonEmptyStr] = None
    attempt_no: int = Field(default=1, ge=1)
    execute_command_uuid: NonEmptyStr
    device_uuid: NonEmptyStr
    action_name: NonEmptyStr
    action_payload_uuid: NonEmptyStr
    route_uuid: Optional[NonEmptyStr] = None
    endpoint_uuid: Optional[NonEmptyStr] = None
    transport: Optional[Transport] = None
    scheduler_revision: int = Field(ge=0)
    scheduler_status_version: int = Field(default=0, ge=0)
    status: Literal[
        "accepted",
        "dispatch_pending",
        "dispatched",
        "running",
        "failure_waiting",
        "terminal_waiting",
        "succeeded",
        "failed",
        "canceled",
        "execution_unknown",
        "rejected",
    ]
    feedback_sequence: int = Field(default=0, ge=0)
    job_access_token_ciphertext: Optional[bytes] = None
    token_key_id: Optional[str] = None
    result_uuid: Optional[NonEmptyStr] = None
    error_code: Optional[str] = None
    error_summary: Optional[str] = None
    accepted_at_ms: UnixMilliseconds
    dispatched_at_ms: Optional[UnixMilliseconds] = None
    started_at_ms: Optional[UnixMilliseconds] = None
    finished_at_ms: Optional[UnixMilliseconds] = None
    version: PositiveVersion = 1

    @model_validator(mode="after")
    def _validate_job_references(self) -> "ExecutionJobRecord":
        if self.retry_of_job_uuid == self.job_uuid:
            raise ValueError("retry_of_job_uuid cannot reference the same job")
        if self.retry_of_job_uuid is None and self.attempt_no != 1:
            raise ValueError("initial job must be attempt 1")
        if self.retry_of_job_uuid is not None and self.attempt_no <= 1:
            raise ValueError("retry job must use a later attempt number")
        route_fields = (self.route_uuid, self.endpoint_uuid, self.transport)
        if any(value is None for value in route_fields) and not all(
            value is None for value in route_fields
        ):
            raise ValueError("route_uuid, endpoint_uuid and transport move together")
        if (self.job_access_token_ciphertext is None) != (self.token_key_id is None):
            raise ValueError("encrypted job token requires token_key_id")
        terminal = self.status in {"succeeded", "failed", "canceled", "rejected"}
        if terminal != (self.finished_at_ms is not None):
            raise ValueError("terminal job must have finished_at_ms")
        timestamps = (
            self.accepted_at_ms,
            self.dispatched_at_ms,
            self.started_at_ms,
            self.finished_at_ms,
        )
        present_timestamps = [value for value in timestamps if value is not None]
        if present_timestamps != sorted(present_timestamps):
            raise ValueError("job timestamps cannot move backwards")
        return self


class DeviceActionAvailabilityRecord(ServerObject):
    endpoint_uuid: NonEmptyStr
    device_uuid: NonEmptyStr
    action_name: NonEmptyStr
    state: Literal["free", "busy", "unknown"]
    active_job_uuid: Optional[NonEmptyStr] = None
    source: NonEmptyStr
    source_event_uuid: NonEmptyStr
    state_epoch: NonEmptyStr = "initial"
    state_sequence: int = Field(default=0, ge=0)
    discovery_epoch: NonEmptyStr
    discovery_generation: int = Field(ge=0)
    observed_at_ms: UnixMilliseconds
    received_at_ms: UnixMilliseconds
    version: PositiveVersion = 1

    @model_validator(mode="after")
    def _free_action_cannot_have_active_job(self) -> "DeviceActionAvailabilityRecord":
        if self.state == "free" and self.active_job_uuid is not None:
            raise ValueError("free action cannot reference an active job")
        return self


class JobMaterialBindingRecord(ServerObject):
    binding_uuid: NonEmptyStr
    job_uuid: NonEmptyStr
    binding_key: NonEmptyStr
    binding_role: NonEmptyStr
    material_uuid: Optional[NonEmptyStr] = None
    site_uuid: Optional[NonEmptyStr] = None
    reservation_uuid: Optional[NonEmptyStr] = None
    quantity: Optional[float] = Field(default=None, ge=0)
    unit: Optional[str] = None
    snapshot_json: JsonObject = Field(default_factory=dict)
    snapshot_hash: NonEmptyStr
    created_at_ms: UnixMilliseconds


class TerminalGateRecord(ServerObject):
    gate_uuid: NonEmptyStr
    job_uuid: NonEmptyStr
    error_uuid: NonEmptyStr
    state: Literal[
        "waiting_backend",
        "backend_confirmed",
        "released_failed",
        "result_replaced",
        "canceled",
    ]
    required_scheduler_revision: int = Field(ge=0)
    confirmed_scheduler_revision: Optional[int] = Field(default=None, ge=0)
    request_event_uuid: NonEmptyStr
    decision_command_uuid: Optional[NonEmptyStr] = None
    opened_at_ms: UnixMilliseconds
    resolved_at_ms: Optional[UnixMilliseconds] = None
    version: PositiveVersion = 1

    @model_validator(mode="after")
    def _validate_resolution(self) -> "TerminalGateRecord":
        closed = self.state in {
            "released_failed",
            "result_replaced",
            "canceled",
        }
        if closed != (self.resolved_at_ms is not None):
            raise ValueError("closed terminal gate must have resolved_at_ms")
        if (
            self.confirmed_scheduler_revision is not None
            and self.confirmed_scheduler_revision < self.required_scheduler_revision
        ):
            raise ValueError("confirmed scheduler revision cannot move backwards")
        if self.state == "waiting_backend":
            if self.confirmed_scheduler_revision is not None:
                raise ValueError("waiting gate cannot already be scheduler-confirmed")
            if self.decision_command_uuid is not None:
                raise ValueError("waiting gate cannot already have a decision")
        elif self.state == "backend_confirmed":
            if self.confirmed_scheduler_revision is None:
                raise ValueError("confirmed gate requires scheduler revision")
            if self.decision_command_uuid is not None:
                raise ValueError("confirmed gate cannot already have a decision")
        elif self.state == "released_failed":
            if self.confirmed_scheduler_revision is None:
                raise ValueError("failed release requires scheduler confirmation")
            if self.decision_command_uuid is None:
                raise ValueError("released gate requires decision command")
        elif self.state == "result_replaced":
            if self.decision_command_uuid is None:
                raise ValueError("replaced result requires decision command")
        elif self.decision_command_uuid is None:
            raise ValueError("canceled gate requires decision command")
        return self


class TerminalDecisionRecord(ServerObject):
    decision_uuid: NonEmptyStr
    gate_uuid: NonEmptyStr
    job_uuid: NonEmptyStr
    command_uuid: NonEmptyStr
    action: Literal["release_failed", "replace_result"]
    trusted_actor_type: Literal["backend", "user"]
    trusted_actor_uuid: Optional[str] = None
    scheduler_revision: Optional[int] = Field(default=None, ge=0)
    replacement_result_uuid: Optional[NonEmptyStr] = None
    reason: Optional[str] = None
    request_fingerprint: NonEmptyStr
    decided_at_ms: UnixMilliseconds

    @model_validator(mode="after")
    def _validate_replacement(self) -> "TerminalDecisionRecord":
        if self.action == "replace_result" and self.replacement_result_uuid is None:
            raise ValueError("replace_result requires replacement_result_uuid")
        if self.action == "release_failed" and self.replacement_result_uuid is not None:
            raise ValueError("release_failed cannot carry replacement_result_uuid")
        if self.action == "release_failed" and self.scheduler_revision is None:
            raise ValueError("release_failed requires scheduler_revision")
        return self


class AdapterCommandOutboxRecord(ServerObject):
    sequence: Optional[int] = Field(default=None, ge=1)
    adapter_command_uuid: NonEmptyStr
    job_uuid: Optional[NonEmptyStr] = None
    endpoint_uuid: NonEmptyStr
    source_command_uuid: Optional[NonEmptyStr] = None
    trigger_event_uuid: Optional[NonEmptyStr] = None
    target_adapter_epoch: Optional[NonEmptyStr] = None
    command_type: Literal[
        "execute",
        "cancel",
        "release_failed",
        "replace_result",
        "reconcile_state",
    ]
    payload_uuid: Optional[NonEmptyStr] = None
    status: Literal["pending", "sent", "acknowledged", "failed"]
    delivery_attempt_count: int = Field(default=0, ge=0)
    created_at_ms: UnixMilliseconds
    available_at_ms: UnixMilliseconds = 0
    last_sent_at_ms: Optional[UnixMilliseconds] = None
    acked_at_ms: Optional[UnixMilliseconds] = None
    ack_event_uuid: Optional[NonEmptyStr] = None
    last_error: Optional[str] = None

    @model_validator(mode="after")
    def _validate_scope_and_origin(self) -> "AdapterCommandOutboxRecord":
        if self.command_type == "reconcile_state":
            if self.job_uuid is not None:
                raise ValueError("reconcile_state is endpoint-scoped, not job-scoped")
            if self.source_command_uuid is not None or self.trigger_event_uuid is None:
                raise ValueError(
                    "reconcile_state requires exactly an adapter event origin"
                )
        elif self.job_uuid is None or self.source_command_uuid is None:
            raise ValueError(
                "job adapter command requires job_uuid and source_command_uuid"
            )
        if self.status == "pending":
            if any(
                value is not None
                for value in (
                    self.last_sent_at_ms,
                    self.acked_at_ms,
                    self.ack_event_uuid,
                )
            ):
                raise ValueError(
                    "pending adapter command cannot have delivery timestamps"
                )
        elif self.status == "sent":
            if self.target_adapter_epoch is None or self.last_sent_at_ms is None:
                raise ValueError("sent adapter command must bind an adapter epoch")
            if self.acked_at_ms is not None or self.ack_event_uuid is not None:
                raise ValueError("sent adapter command cannot already be acknowledged")
        elif self.status == "acknowledged":
            if any(
                value is None
                for value in (
                    self.target_adapter_epoch,
                    self.last_sent_at_ms,
                    self.acked_at_ms,
                    self.ack_event_uuid,
                )
            ):
                raise ValueError(
                    "acknowledged adapter command is missing delivery proof"
                )
        elif self.acked_at_ms is not None or self.ack_event_uuid is not None:
            raise ValueError("failed adapter command cannot be acknowledged")
        return self


class AdapterEventInboxRecord(ServerObject):
    adapter_event_uuid: NonEmptyStr
    endpoint_uuid: NonEmptyStr
    adapter_epoch: NonEmptyStr
    job_uuid: Optional[NonEmptyStr] = None
    adapter_command_uuid: Optional[NonEmptyStr] = None
    adapter_sequence: int = Field(ge=0)
    event_type: Literal[
        "accepted",
        "running",
        "feedback",
        "error_pending",
        "succeeded",
        "failed",
        "canceled",
        "endpoint_ready",
        "capability_snapshot",
        "action_availability_snapshot",
        "action_availability_changed",
        "endpoint_offline",
        "command_ack",
    ]
    payload_uuid: Optional[NonEmptyStr] = None
    payload_sha256: NonEmptyStr = (
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )
    status: Literal["received", "processing", "processed", "rejected"]
    occurred_at_ms: Optional[UnixMilliseconds] = None
    received_at_ms: UnixMilliseconds
    processed_at_ms: Optional[UnixMilliseconds] = None
    error_message: Optional[str] = None

    @model_validator(mode="after")
    def _validate_event_scope(self) -> "AdapterEventInboxRecord":
        job_events = {
            "accepted",
            "running",
            "feedback",
            "error_pending",
            "succeeded",
            "failed",
            "canceled",
        }
        endpoint_events = {
            "endpoint_ready",
            "capability_snapshot",
            "action_availability_snapshot",
            "action_availability_changed",
            "endpoint_offline",
        }
        if self.event_type in job_events and self.job_uuid is None:
            raise ValueError("adapter event job scope does not match event_type")
        if self.event_type in endpoint_events and self.job_uuid is not None:
            raise ValueError("adapter event job scope does not match event_type")
        if self.event_type == "command_ack" and self.adapter_command_uuid is None:
            raise ValueError("command_ack requires adapter_command_uuid")
        completed = self.status in {"processed", "rejected"}
        if completed != (self.processed_at_ms is not None):
            raise ValueError("processed adapter event must have processed_at_ms")
        return self


class BackendEventOutboxRecord(ServerObject):
    sequence: Optional[int] = Field(default=None, ge=1)
    event_uuid: NonEmptyStr
    event_type: NonEmptyStr
    aggregate_type: NonEmptyStr
    aggregate_uuid: NonEmptyStr
    aggregate_version: PositiveVersion
    job_uuid: Optional[NonEmptyStr] = None
    summary_json: JsonObject = Field(default_factory=dict)
    detail_payload_uuid: Optional[NonEmptyStr] = None
    traceparent: Optional[str] = None
    tracestate: Optional[str] = None
    status: Literal["pending", "sent", "acknowledged", "dead_letter"]
    created_at_ms: UnixMilliseconds
    available_at_ms: UnixMilliseconds
    last_sent_at_ms: Optional[UnixMilliseconds] = None
    acked_at_ms: Optional[UnixMilliseconds] = None
    last_session_uuid: Optional[NonEmptyStr] = None
    acked_session_uuid: Optional[NonEmptyStr] = None
    delivery_attempt_count: int = Field(default=0, ge=0)
    last_error: Optional[str] = None

    @model_validator(mode="after")
    def _validate_delivery_state(self) -> "BackendEventOutboxRecord":
        if self.status == "pending":
            if self.acked_at_ms is not None or self.acked_session_uuid is not None:
                raise ValueError("pending backend event cannot be acknowledged")
        elif self.status == "sent":
            if self.last_sent_at_ms is None or self.last_session_uuid is None:
                raise ValueError("sent backend event requires sending session")
            if self.acked_at_ms is not None or self.acked_session_uuid is not None:
                raise ValueError("sent backend event cannot already be acknowledged")
        elif self.status == "acknowledged":
            required = (
                self.last_sent_at_ms,
                self.last_session_uuid,
                self.acked_at_ms,
                self.acked_session_uuid,
            )
            if any(value is None for value in required):
                raise ValueError("acknowledged backend event is missing delivery proof")
        elif self.acked_at_ms is not None or self.acked_session_uuid is not None:
            raise ValueError("dead-letter backend event cannot be acknowledged")
        return self


__all__ = [
    "AdapterCommandOutboxRecord",
    "AdapterEventInboxRecord",
    "BackendEventOutboxRecord",
    "BackendSessionRecord",
    "CommandInboxRecord",
    "DeviceActionAvailabilityRecord",
    "DeviceActionCapabilityRecord",
    "DeviceRouteRecord",
    "ExecutionJobRecord",
    "ExecutorEndpointRecord",
    "JobMaterialBindingRecord",
    "TerminalDecisionRecord",
    "TerminalGateRecord",
    "Transport",
]
