"""``telemetry.db`` 的设备快照与事件模型。"""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import Field

from unilabos.server.models.base import (
    JsonObject,
    NonEmptyStr,
    PositiveVersion,
    ServerObject,
    UnixMilliseconds,
)


class TelemetrySourceCursorRecord(ServerObject):
    endpoint_uuid: NonEmptyStr
    source_epoch: NonEmptyStr
    source_generation: int = Field(default=0, ge=0)
    source_sequence: int = Field(default=0, ge=0)
    last_event_uuid: Optional[NonEmptyStr] = None
    last_received_at_ms: UnixMilliseconds
    version: PositiveVersion = 1


class DeviceStateLatestRecord(ServerObject):
    """一个 endpoint/device 的完整最新设备状态。"""

    endpoint_uuid: NonEmptyStr
    device_uuid: NonEmptyStr
    source_event_uuid: NonEmptyStr
    source_epoch: NonEmptyStr
    source_generation: int = Field(ge=0)
    source_sequence: int = Field(ge=0)
    state: JsonObject = Field(default_factory=dict)
    properties: JsonObject = Field(default_factory=dict)
    connection_state: Literal["online", "offline", "degraded", "unknown"] = "unknown"
    alarms: List[JsonObject] = Field(default_factory=list)
    state_hash: NonEmptyStr
    observed_at_ms: UnixMilliseconds
    received_at_ms: UnixMilliseconds
    version: PositiveVersion = 1


class TelemetryEventRecord(ServerObject):
    sequence: Optional[int] = Field(default=None, ge=1)
    event_uuid: NonEmptyStr
    endpoint_uuid: NonEmptyStr
    device_uuid: Optional[NonEmptyStr] = None
    source_epoch: NonEmptyStr
    source_generation: int = Field(ge=0)
    source_sequence: int = Field(ge=0)
    event_type: Literal["state", "property_sample", "connection", "alarm"]
    event_key: Optional[NonEmptyStr] = None
    payload: object
    payload_hash: NonEmptyStr
    severity: Optional[str] = None
    source_job_uuid: Optional[NonEmptyStr] = None
    source_command_uuid: Optional[NonEmptyStr] = None
    observed_at_ms: UnixMilliseconds
    received_at_ms: UnixMilliseconds


__all__ = [
    "DeviceStateLatestRecord",
    "TelemetryEventRecord",
    "TelemetrySourceCursorRecord",
]
