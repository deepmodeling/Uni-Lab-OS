"""``runtime.v1`` 数据/执行边界：后端、微后端与执行 adapter 间的请求对象。

会话、endpoint 快照、Job 生命周期与命令/事件队列等运行数据的稳定边界；
业务控制面见同包 :mod:`unilabos.protocol.runtime.control`，数据库 Record
由 :mod:`unilabos.server.database.tables.runtime` 定义。
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import Field, model_validator

from unilabos.protocol.base import JsonObject, NonEmptyStr, ServerObject
from unilabos.server.database.tables.runtime import (
    AttemptTrigger,
    DeviceActionCapability,
    DeviceRoute,
    ExecutorEndpointRecord,
    MaterialBinding,
    Transport,
    validate_attempt_link,
)


RUNTIME_PROTOCOL_VERSION = "runtime.v1"


class BackendSessionUpsert(ServerObject):
    session_uuid: NonEmptyStr
    edge_uuid: NonEmptyStr
    backend_uri: NonEmptyStr
    authority_epoch: NonEmptyStr
    connection_epoch: NonEmptyStr
    state: Literal["connecting", "active", "reconciling", "disconnected"]
    command_cursor: int = Field(default=0, ge=0)
    event_send_cursor: int = Field(default=0, ge=0)
    event_ack_sequence: int = Field(default=0, ge=0)
    connected_at_ms: Optional[int] = Field(default=None, ge=0)
    disconnected_at_ms: Optional[int] = Field(default=None, ge=0)
    observed_at_ms: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _validate_state(self) -> "BackendSessionUpsert":
        if self.event_ack_sequence > self.event_send_cursor:
            raise ValueError("event ACK cannot exceed send cursor")
        if self.state == "disconnected" and self.disconnected_at_ms is None:
            raise ValueError("disconnected session requires disconnected_at_ms")
        if self.state != "disconnected" and self.disconnected_at_ms is not None:
            raise ValueError("connected session cannot have disconnected_at_ms")
        return self


class EndpointSnapshotUpsert(ServerObject):
    endpoint_uuid: NonEmptyStr
    transport: Transport
    host_uuid: NonEmptyStr
    instance_name: NonEmptyStr
    authority_epoch: NonEmptyStr
    adapter_epoch: Optional[NonEmptyStr] = None
    reconciliation_generation: int = Field(default=0, ge=0)
    state: Literal["online", "offline", "reconciling"]
    device_routes: list[DeviceRoute] = Field(default_factory=list)
    action_capabilities: list[DeviceActionCapability] = Field(default_factory=list)
    config: JsonObject = Field(default_factory=dict)
    reconciled_at_ms: Optional[int] = Field(default=None, ge=0)
    observed_at_ms: int = Field(default=0, ge=0)


class EndpointSnapshotResult(ServerObject):
    endpoint: ExecutorEndpointRecord
    changed: bool


class CommandEnvelope(ServerObject):
    command_uuid: NonEmptyStr
    session_uuid: NonEmptyStr
    backend_sequence: int = Field(ge=1)
    command_type: Literal[
        "execute_job",
        "cancel_job",
        "release_failed",
        "replace_result",
        "resume_pending",
        "inventory_apply",
        "reconcile",
    ]
    job_uuid: Optional[NonEmptyStr] = None
    payload_uuid: Optional[NonEmptyStr] = None
    payload_sha256: NonEmptyStr
    summary: JsonObject = Field(default_factory=dict)
    traceparent: Optional[str] = None
    received_at_ms: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _validate_job_identity(self) -> "CommandEnvelope":
        requires_job = self.command_type in {
            "execute_job",
            "cancel_job",
            "release_failed",
            "replace_result",
            "resume_pending",
        }
        if requires_job != (self.job_uuid is not None):
            raise ValueError("command_type and job_uuid do not agree")
        return self


class CommandReceipt(ServerObject):
    command_uuid: NonEmptyStr
    backend_sequence: int = Field(ge=1)
    command_fingerprint: NonEmptyStr
    replayed: bool = False


class ExecutionJobCreate(ServerObject):
    job_uuid: NonEmptyStr
    task_uuid: NonEmptyStr
    node_uuid: NonEmptyStr
    attempt_group_uuid: NonEmptyStr
    retry_of_job_uuid: Optional[NonEmptyStr] = None
    attempt_no: int = Field(default=1, ge=1)
    #: attempt > 1 且无重试链只允许 ``loop_iteration``（工作流循环体的下一轮）。
    attempt_trigger: AttemptTrigger = "initial"
    execute_command_uuid: NonEmptyStr
    device_uuid: NonEmptyStr
    action_name: NonEmptyStr
    action_payload_uuid: NonEmptyStr
    route_uuid: Optional[NonEmptyStr] = None
    endpoint_uuid: Optional[NonEmptyStr] = None
    transport: Optional[Transport] = None
    material_bindings: list[MaterialBinding] = Field(default_factory=list)
    scheduler_revision: int = Field(ge=0)
    accepted_at_ms: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _validate_attempt_and_route(self) -> "ExecutionJobCreate":
        validate_attempt_link(self.retry_of_job_uuid, self.attempt_no, self.attempt_trigger)
        route = (self.route_uuid, self.endpoint_uuid, self.transport)
        if any(value is None for value in route) and any(
            value is not None for value in route
        ):
            raise ValueError("route, endpoint and transport must be set together")
        return self


class ExecutionJobTransition(ServerObject):
    expected_version: int = Field(ge=1)
    status: Literal[
        "dispatch_pending",
        "dispatched",
        "running",
        "terminal_waiting",
        "succeeded",
        "failed",
        "canceled",
        "execution_unknown",
        "rejected",
    ]
    scheduler_status_version: Optional[int] = Field(default=None, ge=0)
    feedback_sequence: Optional[int] = Field(default=None, ge=0)
    result_uuid: Optional[NonEmptyStr] = None
    error_code: Optional[str] = None
    error_summary: Optional[str] = None
    occurred_at_ms: int = Field(default=0, ge=0)


class ExecutionJobFeedback(ServerObject):
    expected_version: int = Field(ge=1)
    feedback_sequence: int = Field(ge=1)
    observed_at_ms: int = Field(default=0, ge=0)


class ExecutionJobCancel(ServerObject):
    expected_version: int = Field(ge=1)
    cancel_command_uuid: NonEmptyStr
    adapter_command_uuid: NonEmptyStr
    payload_uuid: Optional[NonEmptyStr] = None
    requested_at_ms: int = Field(default=0, ge=0)


class ErrorGateOpen(ServerObject):
    expected_version: int = Field(ge=1)
    error_uuid: NonEmptyStr
    error_code: NonEmptyStr
    error_summary: NonEmptyStr
    required_scheduler_revision: int = Field(ge=0)
    request_event_uuid: NonEmptyStr
    detail_payload_uuid: Optional[NonEmptyStr] = None
    summary: JsonObject = Field(default_factory=dict)
    opened_at_ms: int = Field(default=0, ge=0)


class ErrorGateDecision(ServerObject):
    expected_version: int = Field(ge=1)
    decision_command_uuid: NonEmptyStr
    action: Literal["release_failed", "replace_result", "cancel"]
    confirmed_scheduler_revision: int = Field(ge=0)
    adapter_command_uuid: NonEmptyStr
    payload_uuid: Optional[NonEmptyStr] = None
    result_uuid: Optional[NonEmptyStr] = None
    decision: JsonObject = Field(default_factory=dict)
    resolved_at_ms: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _validate_result(self) -> "ErrorGateDecision":
        if self.action == "replace_result" and self.result_uuid is None:
            raise ValueError("replace_result requires result_uuid")
        if self.action != "replace_result" and self.result_uuid is not None:
            raise ValueError("result_uuid is only valid for replace_result")
        return self


class ErrorGateResume(ServerObject):
    """关闭一个 ``execution_timeout`` 软超时打开的终态闸门：动作仍在执行，attempt 回到 running。

    Backend 决策 ``wait`` 时带 ``decision_command_uuid``（``resume_pending`` 命令），并向执行面
    投递同名 adapter 命令；动作在等待期间真实完成、执行面自行收回决策时不带命令。
    """

    expected_version: int = Field(ge=1)
    error_uuid: NonEmptyStr
    reason: str = ""
    decision_command_uuid: Optional[NonEmptyStr] = None
    adapter_command_uuid: Optional[NonEmptyStr] = None
    payload_uuid: Optional[NonEmptyStr] = None
    decision: JsonObject = Field(default_factory=dict)
    resolved_at_ms: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _validate_command(self) -> "ErrorGateResume":
        if (self.decision_command_uuid is None) != (self.adapter_command_uuid is None):
            raise ValueError("decision and adapter command uuids must be set together")
        return self


class AdapterCommandEnqueue(ServerObject):
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
        "resume_pending",
        "reconcile_state",
    ]
    payload_uuid: Optional[NonEmptyStr] = None
    available_at_ms: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _validate_source(self) -> "AdapterCommandEnqueue":
        if self.command_type == "reconcile_state":
            valid = (
                self.job_uuid is None
                and self.source_command_uuid is None
                and self.trigger_event_uuid is not None
            )
        else:
            valid = self.job_uuid is not None and self.source_command_uuid is not None
        if not valid:
            raise ValueError("adapter command identity fields do not agree")
        return self


class AdapterCommandClaim(ServerObject):
    endpoint_uuid: NonEmptyStr
    now_ms: int = Field(default=0, ge=0)
    lease_ms: int = Field(default=30_000, ge=1)
    limit: int = Field(default=100, ge=1, le=1000)


class AdapterCommandAck(ServerObject):
    adapter_command_uuid: NonEmptyStr
    ack_event_uuid: NonEmptyStr
    acknowledged_at_ms: int = Field(default=0, ge=0)


class BackendEventEnqueue(ServerObject):
    event_uuid: NonEmptyStr
    event_type: NonEmptyStr
    aggregate_type: NonEmptyStr
    aggregate_uuid: NonEmptyStr
    aggregate_version: int = Field(ge=1)
    job_uuid: Optional[NonEmptyStr] = None
    summary: JsonObject = Field(default_factory=dict)
    detail_payload_uuid: Optional[NonEmptyStr] = None
    traceparent: Optional[str] = None
    tracestate: Optional[str] = None
    available_at_ms: int = Field(default=0, ge=0)


class BackendEventClaim(ServerObject):
    session_uuid: NonEmptyStr
    now_ms: int = Field(default=0, ge=0)
    lease_ms: int = Field(default=30_000, ge=1)
    limit: int = Field(default=100, ge=1, le=1000)


class BackendEventAck(ServerObject):
    session_uuid: NonEmptyStr
    through_sequence: int = Field(ge=0)
    acknowledged_at_ms: int = Field(default=0, ge=0)


__all__ = [
    "AdapterCommandAck",
    "AdapterCommandClaim",
    "AdapterCommandEnqueue",
    "BackendEventAck",
    "BackendEventClaim",
    "BackendEventEnqueue",
    "BackendSessionUpsert",
    "CommandEnvelope",
    "CommandReceipt",
    "EndpointSnapshotResult",
    "EndpointSnapshotUpsert",
    "ErrorGateDecision",
    "ErrorGateOpen",
    "ErrorGateResume",
    "ExecutionJobCreate",
    "ExecutionJobCancel",
    "ExecutionJobFeedback",
    "ExecutionJobTransition",
    "RUNTIME_PROTOCOL_VERSION",
]
