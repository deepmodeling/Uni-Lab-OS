"""``runtime.v1`` 业务控制面：Backend/Edge 的轻通知与 HTTP 权威文档协议。

Edge 与 runtime（微后端/Backend）之间只有一个协议版本 ``runtime.v1``：
:mod:`unilabos.protocol.runtime.data` 承载数据/执行边界，本模块承载
业务控制面（命令下发通知、命令正文、事件回收）。替换后端时数据源与
控制源整体切换，不做独立版本协商。
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import Field, JsonValue, model_validator

from unilabos.protocol.base import JsonObject, NonEmptyStr, ServerObject
from unilabos.server.database.tables.runtime import (
    AttemptTrigger,
    MaterialBinding,
    Transport,
    validate_attempt_link,
)
from unilabos.protocol.materials import InventoryRequirement
from unilabos.protocol.runtime.data import (
    RUNTIME_PROTOCOL_VERSION,
    CommandEnvelope,
)

CommandType = Literal[
    "execute_job",
    "cancel_job",
    "release_failed",
    "replace_result",
    "resume_pending",
    "inventory_apply",
    "reconcile",
]


class PingNotice(ServerObject):
    """runtime.v1 控制面应用层 ping 的唯一字段契约。

    这不是 WebSocket 协议自带的 control frame，而是 ``action=ping`` 的
    数据段。它必须保持极小且可直接处理，不能混入命令/业务正文。
    """

    ping_id: NonEmptyStr
    client_timestamp: float = Field(ge=0)


class PongNotice(PingNotice):
    """runtime.v1 ``action=pong`` 的回显字段与服务端时间戳。"""

    server_timestamp: float = Field(ge=0)


class BackendSessionNotice(ServerObject):
    """WS 连接建立后的短握手，用于恢复 durable event outbox。"""

    protocol_version: Literal["runtime.v1"] = RUNTIME_PROTOCOL_VERSION
    session_uuid: NonEmptyStr
    edge_uuid: NonEmptyStr
    authority_epoch: NonEmptyStr
    connection_epoch: NonEmptyStr
    occurred_at_ms: int = Field(default=0, ge=0)


class BackendCommandNotice(ServerObject):
    """WS 只携带“哪个命令变了”，不携带执行参数或决策正文。"""

    protocol_version: Literal["runtime.v1"] = RUNTIME_PROTOCOL_VERSION
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

    protocol_version: Literal["runtime.v1"] = RUNTIME_PROTOCOL_VERSION
    command: CommandEnvelope
    payload: JsonObject


from unilabos.protocol.runtime.execution_start import ExecutionPermit


class ExecuteJobContent(ServerObject):
    """由 Backend scheduler 生成的一次独立执行 attempt。"""

    execution_permit: Optional[ExecutionPermit] = None
    resource_contract: Optional[JsonObject] = None
    material_execution: Optional[JsonObject] = None
    job_uuid: NonEmptyStr
    task_uuid: NonEmptyStr
    node_uuid: NonEmptyStr
    attempt_group_uuid: NonEmptyStr
    retry_of_job_uuid: Optional[NonEmptyStr] = None
    attempt_no: int = Field(default=1, ge=1)
    #: attempt 为何产生；attempt > 1 且无重试链只允许 ``loop_iteration``（循环体下一轮）。
    attempt_trigger: AttemptTrigger = "initial"
    #: 本节点已重试的次数（错误决策报告的 ``retry_count``）；循环下一轮不算重试，所以不能从
    #: ``attempt_no`` 推。缺省由执行面按 ``attempt_no - 1`` 兜底。
    retry_count: Optional[int] = Field(default=None, ge=0)
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
    #: 调度权威解析后的硬超时（秒）：注册表 ``@action(timeout)`` 或节点 ``execution_policy``；
    #: 缺省由执行面按自己的注册表副本解析。
    timeout_seconds: Optional[float] = Field(default=None, gt=0)
    #: 调度权威解析后的业务软超时（秒）：节点 ``execution_policy.execution_timeout_seconds``
    #: 优先，否则注册表 ``@action(execution_timeout)`` 表达式按最终 action_args 求值。
    execution_timeout_seconds: Optional[float] = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _validate_attempt_and_route(self) -> "ExecuteJobContent":
        validate_attempt_link(self.retry_of_job_uuid, self.attempt_no, self.attempt_trigger)
        route = (self.route_uuid, self.endpoint_uuid, self.transport)
        if any(value is None for value in route) and any(
            value is not None for value in route
        ):
            raise ValueError("route, endpoint and transport must be set together")
        return self


class ErrorDecisionContent(ServerObject):
    """Backend 已完成前端询问和调度更新后的终态放行命令。

    ``release_failed`` / ``replace_result`` 放行一个失败 attempt；``resume_pending``
    （``selected_action="wait"``）只用于 ``execution_timeout`` 软超时决策：动作仍在执行，
    Edge 关闭终态闸门、attempt 回到 running 并重新计时。
    """

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

    protocol_version: Literal["runtime.v1"] = RUNTIME_PROTOCOL_VERSION
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
    protocol_version: Literal["runtime.v1"] = RUNTIME_PROTOCOL_VERSION
    session_uuid: NonEmptyStr
    through_sequence: int = Field(ge=0)
    acknowledged_at_ms: int = Field(default=0, ge=0)


class BackendHttpRequest(ServerObject):
    """Backend 经控制 WS（``action=backend_http``）让 Edge 在进程内执行的一条 HTTP 请求。

    Edge 不监听任何端口：Backend 需要 Edge 专有的数据面 / 执行面（遥测、历史正文、
    驱动包、受管进程、设备投影中继、重启）时，把请求下发过去，Edge 对自己的 ASGI 应用
    执行后用 HTTP ``POST /api/v1/edge/http-responses/{request_uuid}`` 把结果送回 Backend。
    """

    protocol_version: Literal["runtime.v1"] = RUNTIME_PROTOCOL_VERSION
    request_uuid: NonEmptyStr
    method: NonEmptyStr
    #: 含 query string 的路径，如 ``/api/v1/telemetry/states?limit=50``
    path: NonEmptyStr
    headers: dict[str, str] = Field(default_factory=dict)
    body_base64: str = ""
    timeout_seconds: float = Field(default=60.0, gt=0)


class EdgeHttpResponse(ServerObject):
    """Edge 对 :class:`BackendHttpRequest` 的执行结果。"""

    protocol_version: Literal["runtime.v1"] = RUNTIME_PROTOCOL_VERSION
    request_uuid: NonEmptyStr
    status_code: int = Field(ge=100, le=599)
    headers: dict[str, str] = Field(default_factory=dict)
    body_base64: str = ""

    def body_bytes(self) -> bytes:
        import base64

        return base64.b64decode(self.body_base64) if self.body_base64 else b""


__all__ = [
    "BackendCommandDocument",
    "BackendCommandNotice",
    "BackendHttpRequest",
    "BackendSessionNotice",
    "CancelJobContent",
    "CommandType",
    "EdgeChangeAck",
    "EdgeChangeNotice",
    "EdgeHttpResponse",
    "ErrorDecisionContent",
    "ExecuteJobContent",
    "PingNotice",
    "PongNotice",
]
