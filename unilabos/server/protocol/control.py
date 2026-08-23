"""Backend/Edge 业务控制面的轻通知与 HTTP 权威文档协议。"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import Field, JsonValue, model_validator

from unilabos.server.database.tables.base import JsonObject, NonEmptyStr, ServerObject
from unilabos.server.database.tables.runtime import MaterialBinding, Transport
from unilabos.server.protocol.materials import InventoryRequirement
from unilabos.server.protocol.runtime import CommandEnvelope


CONTROL_PROTOCOL_VERSION = "control.v1"

CommandType = Literal[
    "execute_job",
    "cancel_job",
    "release_failed",
    "replace_result",
    "inventory_apply",
    "reconcile",
]


class BackendSessionNotice(ServerObject):
    """WS 连接建立后的短握手，用于恢复 durable event outbox。"""

    protocol_version: Literal["control.v1"] = CONTROL_PROTOCOL_VERSION
    session_uuid: NonEmptyStr
    edge_uuid: NonEmptyStr
    authority_epoch: NonEmptyStr
    connection_epoch: NonEmptyStr
    occurred_at_ms: int = Field(default=0, ge=0)


class BackendCommandNotice(ServerObject):
    """WS 只携带“哪个命令变了”，不携带执行参数或决策正文。"""

    protocol_version: Literal["control.v1"] = CONTROL_PROTOCOL_VERSION
    notice_uuid: NonEmptyStr
    change_type: Literal["command.available"] = "command.available"
    command_uuid: NonEmptyStr
    command_type: CommandType
    session_uuid: NonEmptyStr
    backend_sequence: int = Field(ge=1)
    edge_uuid: NonEmptyStr
    authority_epoch: NonEmptyStr
    connection_epoch: NonEmptyStr
    content_sha256: NonEmptyStr
    occurred_at_ms: int = Field(default=0, ge=0)


class BackendCommandDocument(ServerObject):
    """Edge 经 HTTP 拉取的完整、权威命令。"""

    protocol_version: Literal["control.v1"] = CONTROL_PROTOCOL_VERSION
    command: CommandEnvelope
    payload: JsonObject


class ExecuteJobContent(ServerObject):
    """由 Backend scheduler 生成的一次独立执行 attempt。"""

    job_uuid: NonEmptyStr
    task_uuid: NonEmptyStr
    node_uuid: NonEmptyStr
    attempt_group_uuid: NonEmptyStr
    retry_of_job_uuid: Optional[NonEmptyStr] = None
    attempt_no: int = Field(default=1, ge=1)
    device_uuid: NonEmptyStr
    action_name: NonEmptyStr
    action_type: str = ""
    action_args: JsonObject = Field(default_factory=dict)
    materials_need_lock: list[NonEmptyStr] = Field(default_factory=list)
    sample_material: JsonObject = Field(default_factory=dict)
    server_info: Optional[JsonObject] = None
    notebook_uuid: str = ""
    route_uuid: Optional[NonEmptyStr] = None
    endpoint_uuid: Optional[NonEmptyStr] = None
    transport: Optional[Transport] = None
    material_bindings: list[MaterialBinding] = Field(default_factory=list)
    inventory_requirements: list[InventoryRequirement] = Field(default_factory=list)
    inventory_reservation_uuid: Optional[NonEmptyStr] = None
    scheduler_revision: int = Field(ge=0)

    @model_validator(mode="after")
    def _validate_attempt_and_route(self) -> "ExecuteJobContent":
        if (self.retry_of_job_uuid is None) != (self.attempt_no == 1):
            raise ValueError("retry link and attempt number must agree")
        route = (self.route_uuid, self.endpoint_uuid, self.transport)
        if any(value is None for value in route) and any(
            value is not None for value in route
        ):
            raise ValueError("route, endpoint and transport must be set together")
        return self


class ErrorDecisionContent(ServerObject):
    """Backend 已完成前端询问和调度更新后的终态放行命令。"""

    decision_uuid: NonEmptyStr
    confirmed_scheduler_revision: int = Field(ge=0)
    adapter_command_uuid: NonEmptyStr
    selected_action: NonEmptyStr
    reason: str = ""
    result: Optional[JsonValue] = None
    actor_uuid: Optional[NonEmptyStr] = None


class CancelJobContent(ServerObject):
    adapter_command_uuid: NonEmptyStr
    reason: str = ""


class EdgeChangeNotice(ServerObject):
    """Edge 出站 WS 通知；正文由 Backend 从 Edge HTTP API 拉取。"""

    protocol_version: Literal["control.v1"] = CONTROL_PROTOCOL_VERSION
    change_type: Literal["runtime.event"] = "runtime.event"
    session_uuid: NonEmptyStr
    event_uuid: NonEmptyStr
    event_sequence: int = Field(ge=1)
    event_type: NonEmptyStr
    aggregate_type: NonEmptyStr
    aggregate_uuid: NonEmptyStr
    aggregate_version: int = Field(ge=1)
    job_uuid: Optional[NonEmptyStr] = None
    detail_payload_uuid: Optional[NonEmptyStr] = None


class EdgeChangeAck(ServerObject):
    protocol_version: Literal["control.v1"] = CONTROL_PROTOCOL_VERSION
    session_uuid: NonEmptyStr
    through_sequence: int = Field(ge=0)
    acknowledged_at_ms: int = Field(default=0, ge=0)


__all__ = [
    "BackendCommandDocument",
    "BackendCommandNotice",
    "BackendSessionNotice",
    "CancelJobContent",
    "CONTROL_PROTOCOL_VERSION",
    "CommandType",
    "EdgeChangeAck",
    "EdgeChangeNotice",
    "ErrorDecisionContent",
    "ExecuteJobContent",
]
