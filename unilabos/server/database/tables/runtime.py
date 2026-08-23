"""``runtime.db`` 的 SQLModel 表记录与内嵌值对象。"""

from __future__ import annotations

from typing import Annotated, ClassVar, List, Literal, Optional

from pydantic import model_validator
from sqlalchemy import Column, LargeBinary, Text
from sqlmodel import Field

from unilabos.server.database.tables.base import (
    JsonObject,
    NonEmptyStr,
    PositiveVersion,
    ServerObject,
    TableObject,
    UnixMilliseconds,
    json_text_column,
)
from unilabos.server.database.migrations.v1.runtime import (
    RUNTIME_DATABASE,
    RUNTIME_TABLES,
)


Transport = Annotated[Literal["hostlink", "ros2"], Field(sa_type=Text)]


class DeviceRoute(ServerObject):
    """Endpoint 快照内的设备 route，不是独立表记录。"""

    route_uuid: NonEmptyStr
    device_uuid: NonEmptyStr
    driver_key: NonEmptyStr
    priority: int = 0
    enabled: bool = True
    selected: bool = False
    config_hash: NonEmptyStr
    config: JsonObject = Field(default_factory=dict)


class DeviceActionCapability(ServerObject):
    """Endpoint 快照内的 action 能力及当前可用性。"""

    device_uuid: NonEmptyStr
    action_name: NonEmptyStr
    action_type: Optional[str] = None
    concurrency_mode: Literal["exclusive", "unbounded"]
    state: Literal["active", "retired"] = "active"
    availability: Literal["free", "busy", "unknown"] = "unknown"
    active_job_uuid: Optional[NonEmptyStr] = None
    descriptor: JsonObject = Field(default_factory=dict)
    descriptor_hash: NonEmptyStr
    observed_at_ms: UnixMilliseconds

    @model_validator(mode="after")
    def _validate_availability(self) -> "DeviceActionCapability":
        if self.availability == "free" and self.active_job_uuid is not None:
            raise ValueError("free action cannot reference an active job")
        return self


class MaterialBinding(ServerObject):
    """Job 接收时固化的物料绑定快照。"""

    key: NonEmptyStr
    role: NonEmptyStr
    material_uuid: Optional[NonEmptyStr] = None
    site_uuid: Optional[NonEmptyStr] = None
    reservation_uuid: Optional[NonEmptyStr] = None
    quantity: Optional[float] = Field(default=None, ge=0)
    unit: Optional[str] = None
    snapshot: JsonObject = Field(default_factory=dict)
    snapshot_hash: NonEmptyStr


class BackendSessionRecord(TableObject, table=True):
    __tablename__: ClassVar[str] = "backend_session"

    session_uuid: NonEmptyStr = Field(primary_key=True)
    edge_uuid: NonEmptyStr
    backend_uri: NonEmptyStr
    authority_epoch: NonEmptyStr
    connection_epoch: NonEmptyStr
    state: Literal["connecting", "active", "reconciling", "disconnected"] = Field(
        sa_type=Text
    )
    command_cursor: int = Field(default=0, ge=0)
    event_send_cursor: int = Field(default=0, ge=0)
    event_ack_sequence: int = Field(default=0, ge=0)
    connected_at_ms: Optional[UnixMilliseconds] = None
    disconnected_at_ms: Optional[UnixMilliseconds] = None
    last_seen_at_ms: UnixMilliseconds
    version: PositiveVersion = 1

    @model_validator(mode="after")
    def _validate_session(self) -> "BackendSessionRecord":
        if self.event_ack_sequence > self.event_send_cursor:
            raise ValueError("event ACK cannot exceed send cursor")
        if (self.state == "disconnected") != (self.disconnected_at_ms is not None):
            raise ValueError("session state and disconnected_at_ms must agree")
        return self


class ExecutorEndpointRecord(TableObject, table=True):
    __tablename__: ClassVar[str] = "executor_endpoint"

    endpoint_uuid: NonEmptyStr = Field(primary_key=True)
    transport: Transport
    host_uuid: NonEmptyStr
    instance_name: NonEmptyStr
    authority_epoch: NonEmptyStr
    adapter_epoch: Optional[NonEmptyStr] = None
    adapter_event_cursor: int = Field(default=0, ge=0)
    reconciliation_generation: int = Field(default=0, ge=0)
    state: Literal["online", "offline", "reconciling"] = Field(sa_type=Text)
    device_routes: List[DeviceRoute] = Field(
        default_factory=list,
        sa_column=json_text_column("device_routes_json", default_json="[]"),
    )
    action_capabilities: List[DeviceActionCapability] = Field(
        default_factory=list,
        sa_column=json_text_column("action_capabilities_json", default_json="[]"),
    )
    config: JsonObject = Field(
        default_factory=dict,
        sa_column=json_text_column("config_json", default_json="{}"),
    )
    snapshot_hash: str = ""
    registered_at_ms: UnixMilliseconds
    last_seen_at_ms: UnixMilliseconds
    reconciled_at_ms: Optional[UnixMilliseconds] = None
    version: PositiveVersion = 1


class CommandInboxRecord(TableObject, table=True):
    __tablename__: ClassVar[str] = "command_inbox"

    command_uuid: NonEmptyStr = Field(primary_key=True)
    session_uuid: NonEmptyStr
    backend_sequence: int = Field(ge=1)
    command_type: Literal[
        "execute_job",
        "cancel_job",
        "release_failed",
        "replace_result",
        "inventory_apply",
        "reconcile",
    ] = Field(sa_type=Text)
    job_uuid: Optional[NonEmptyStr] = None
    payload_uuid: Optional[NonEmptyStr] = None
    payload_sha256: NonEmptyStr
    command_fingerprint: NonEmptyStr
    summary: JsonObject = Field(
        default_factory=dict,
        sa_column=json_text_column("summary_json", default_json="{}"),
    )
    traceparent: Optional[str] = None
    status: Literal["received", "applying", "applied", "rejected"] = Field(
        sa_type=Text
    )
    received_at_ms: UnixMilliseconds
    applied_at_ms: Optional[UnixMilliseconds] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    version: PositiveVersion = 1

    @model_validator(mode="after")
    def _validate_command(self) -> "CommandInboxRecord":
        requires_job = self.command_type in {
            "execute_job",
            "cancel_job",
            "release_failed",
            "replace_result",
        }
        if requires_job != (self.job_uuid is not None):
            raise ValueError("command_type and job_uuid do not agree")
        terminal = self.status in {"applied", "rejected"}
        if terminal != (self.applied_at_ms is not None):
            raise ValueError("command status and applied_at_ms must agree")
        return self


class ExecutionJobRecord(TableObject, table=True):
    __tablename__: ClassVar[str] = "execution_job"

    job_uuid: NonEmptyStr = Field(primary_key=True)
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
    # SQLModel 0.0.x 无法从 Optional[Annotated[Literal, Field]] 推断列类型。
    transport: Optional[Transport] = Field(default=None, sa_type=Text)
    material_bindings: List[MaterialBinding] = Field(
        default_factory=list,
        sa_column=json_text_column("material_bindings_json", default_json="[]"),
    )
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
    ] = Field(sa_type=Text)
    feedback_sequence: int = Field(default=0, ge=0)
    job_access_token_ciphertext: Optional[bytes] = Field(
        default=None,
        exclude=True,
        repr=False,
        sa_column=Column(LargeBinary, nullable=True),
    )
    token_key_id: Optional[str] = Field(
        default=None,
        exclude=True,
        repr=False,
        sa_column=Column(Text, nullable=True),
    )
    result_uuid: Optional[NonEmptyStr] = None
    error_code: Optional[str] = None
    error_summary: Optional[str] = None
    terminal_gate_state: Literal[
        "none",
        "waiting_backend",
        "backend_confirmed",
        "released_failed",
        "result_replaced",
        "canceled",
    ] = Field(default="none", sa_type=Text)
    terminal_error_uuid: Optional[NonEmptyStr] = None
    terminal_required_scheduler_revision: Optional[int] = Field(default=None, ge=0)
    terminal_confirmed_scheduler_revision: Optional[int] = Field(default=None, ge=0)
    terminal_request_event_uuid: Optional[NonEmptyStr] = None
    terminal_decision_command_uuid: Optional[NonEmptyStr] = None
    terminal_decision: JsonObject = Field(
        default_factory=dict,
        sa_column=json_text_column("terminal_decision_json", default_json="{}"),
    )
    terminal_opened_at_ms: Optional[UnixMilliseconds] = None
    terminal_resolved_at_ms: Optional[UnixMilliseconds] = None
    accepted_at_ms: UnixMilliseconds
    dispatched_at_ms: Optional[UnixMilliseconds] = None
    started_at_ms: Optional[UnixMilliseconds] = None
    finished_at_ms: Optional[UnixMilliseconds] = None
    version: PositiveVersion = 1

    @model_validator(mode="after")
    def _validate_job(self) -> "ExecutionJobRecord":
        if (self.retry_of_job_uuid is None) != (self.attempt_no == 1):
            raise ValueError("retry link and attempt number must agree")
        route_values = (self.route_uuid, self.endpoint_uuid, self.transport)
        if any(value is None for value in route_values) and any(
            value is not None for value in route_values
        ):
            raise ValueError("route, endpoint and transport must be set together")
        terminal = self.status in {"succeeded", "failed", "canceled", "rejected"}
        if terminal != (self.finished_at_ms is not None):
            raise ValueError("job status and finished_at_ms must agree")
        gate_open = self.terminal_gate_state != "none"
        gate_identity = (
            self.terminal_error_uuid,
            self.terminal_request_event_uuid,
            self.terminal_opened_at_ms,
        )
        if gate_open != all(value is not None for value in gate_identity):
            raise ValueError("terminal gate state and identity fields must agree")
        resolved = self.terminal_gate_state in {
            "released_failed",
            "result_replaced",
            "canceled",
        }
        if resolved != (self.terminal_resolved_at_ms is not None):
            raise ValueError("terminal gate resolution fields must agree")
        return self


class AdapterCommandOutboxRecord(TableObject, table=True):
    __tablename__: ClassVar[str] = "adapter_command_outbox"

    sequence: Optional[int] = Field(default=None, ge=1, primary_key=True)
    adapter_command_uuid: NonEmptyStr
    job_uuid: Optional[NonEmptyStr] = None
    endpoint_uuid: NonEmptyStr
    source_command_uuid: Optional[NonEmptyStr] = None
    trigger_event_uuid: Optional[NonEmptyStr] = None
    target_adapter_epoch: Optional[NonEmptyStr] = None
    command_type: Literal[
        "execute", "cancel", "release_failed", "replace_result", "reconcile_state"
    ] = Field(sa_type=Text)
    payload_uuid: Optional[NonEmptyStr] = None
    status: Literal["pending", "sent", "acknowledged", "failed"] = Field(
        sa_type=Text
    )
    delivery_attempt_count: int = Field(default=0, ge=0)
    created_at_ms: UnixMilliseconds
    available_at_ms: UnixMilliseconds = 0
    last_sent_at_ms: Optional[UnixMilliseconds] = None
    acked_at_ms: Optional[UnixMilliseconds] = None
    ack_event_uuid: Optional[NonEmptyStr] = None
    last_error: Optional[str] = None


class AdapterEventInboxRecord(TableObject, table=True):
    __tablename__: ClassVar[str] = "adapter_event_inbox"

    adapter_event_uuid: NonEmptyStr = Field(primary_key=True)
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
        "endpoint_snapshot",
        "endpoint_offline",
        "command_ack",
    ] = Field(sa_type=Text)
    payload_uuid: Optional[NonEmptyStr] = None
    payload_sha256: NonEmptyStr
    status: Literal["received", "processing", "processed", "rejected"] = Field(
        sa_type=Text
    )
    occurred_at_ms: Optional[UnixMilliseconds] = None
    received_at_ms: UnixMilliseconds
    processed_at_ms: Optional[UnixMilliseconds] = None
    error_message: Optional[str] = None


class BackendEventOutboxRecord(TableObject, table=True):
    __tablename__: ClassVar[str] = "backend_event_outbox"

    sequence: Optional[int] = Field(default=None, ge=1, primary_key=True)
    event_uuid: NonEmptyStr
    event_type: NonEmptyStr
    aggregate_type: NonEmptyStr
    aggregate_uuid: NonEmptyStr
    aggregate_version: PositiveVersion
    job_uuid: Optional[NonEmptyStr] = None
    summary: JsonObject = Field(
        default_factory=dict,
        sa_column=json_text_column("summary_json", default_json="{}"),
    )
    detail_payload_uuid: Optional[NonEmptyStr] = None
    traceparent: Optional[str] = None
    tracestate: Optional[str] = None
    status: Literal["pending", "sent", "acknowledged", "dead_letter"] = Field(
        sa_type=Text
    )
    created_at_ms: UnixMilliseconds
    available_at_ms: UnixMilliseconds
    last_sent_at_ms: Optional[UnixMilliseconds] = None
    acked_at_ms: Optional[UnixMilliseconds] = None
    delivery_attempt_count: int = Field(default=0, ge=0)
    last_error: Optional[str] = None


RUNTIME_TABLE_MODELS = (
    BackendSessionRecord,
    ExecutorEndpointRecord,
    CommandInboxRecord,
    ExecutionJobRecord,
    AdapterCommandOutboxRecord,
    AdapterEventInboxRecord,
    BackendEventOutboxRecord,
)


__all__ = [
    "AdapterCommandOutboxRecord",
    "AdapterEventInboxRecord",
    "BackendEventOutboxRecord",
    "BackendSessionRecord",
    "CommandInboxRecord",
    "DeviceActionCapability",
    "DeviceRoute",
    "ExecutionJobRecord",
    "ExecutorEndpointRecord",
    "MaterialBinding",
    "RUNTIME_DATABASE",
    "RUNTIME_TABLE_MODELS",
    "RUNTIME_TABLES",
    "Transport",
]
