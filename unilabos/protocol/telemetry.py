"""``telemetry.v1`` 的设备状态和高频事件传输模型。"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import Field, model_validator

from unilabos.protocol.base import JsonObject, NonEmptyStr, ServerObject
from unilabos.server.database.tables.telemetry import (
    DeviceStateLatestRecord,
    TelemetryEventRecord,
    TelemetrySourceCursorRecord,
)


TELEMETRY_PROTOCOL_VERSION = "telemetry.v1"


class TelemetryEventWrite(ServerObject):
    """由一个 endpoint 产生的、带稳定来源位置的事件。"""

    event_uuid: NonEmptyStr
    endpoint_uuid: NonEmptyStr
    device_uuid: Optional[NonEmptyStr] = None
    source_epoch: NonEmptyStr
    source_generation: int = Field(ge=0)
    source_sequence: int = Field(ge=0)
    event_type: Literal["state", "property_sample", "connection", "alarm"]
    event_key: Optional[NonEmptyStr] = None
    payload: object
    payload_hash: Optional[NonEmptyStr] = None
    severity: Optional[str] = None
    source_job_uuid: Optional[NonEmptyStr] = None
    source_command_uuid: Optional[NonEmptyStr] = None
    observed_at_ms: int = Field(ge=0)
    received_at_ms: int = Field(ge=0)


class DeviceStateSnapshot(ServerObject):
    """随事件提交的完整设备快照；来源位置由外层事件提供。"""

    device_uuid: NonEmptyStr
    state: JsonObject = Field(default_factory=dict)
    properties: JsonObject = Field(default_factory=dict)
    connection_state: Literal["online", "offline", "degraded", "unknown"] = "unknown"
    alarms: list[JsonObject] = Field(default_factory=list)
    state_hash: Optional[NonEmptyStr] = None
    observed_at_ms: int = Field(ge=0)


class TelemetryIngestRequest(ServerObject):
    protocol_version: Literal["telemetry.v1"] = TELEMETRY_PROTOCOL_VERSION
    event: TelemetryEventWrite
    device_state: Optional[DeviceStateSnapshot] = None

    @model_validator(mode="after")
    def _bind_device_state(self) -> "TelemetryIngestRequest":
        if self.device_state is None:
            return self
        if self.event.device_uuid is None:
            raise ValueError("an event carrying device_state requires device_uuid")
        if self.device_state.device_uuid != self.event.device_uuid:
            raise ValueError("event and device_state refer to different devices")
        return self


class TelemetryIngestResult(ServerObject):
    protocol_version: Literal["telemetry.v1"] = TELEMETRY_PROTOCOL_VERSION
    accepted: bool = True
    replayed: bool = False
    event: TelemetryEventRecord
    cursor: TelemetrySourceCursorRecord
    device_state: Optional[DeviceStateLatestRecord] = None


class TelemetryEventQuery(ServerObject):
    after_sequence: int = Field(default=0, ge=0)
    endpoint_uuid: Optional[NonEmptyStr] = None
    device_uuid: Optional[NonEmptyStr] = None
    event_type: Optional[Literal["state", "property_sample", "connection", "alarm"]] = (
        None
    )
    event_key: Optional[NonEmptyStr] = None
    source_epoch: Optional[NonEmptyStr] = None
    source_generation: Optional[int] = Field(default=None, ge=0)
    observed_from_ms: Optional[int] = Field(default=None, ge=0)
    observed_to_ms: Optional[int] = Field(default=None, ge=0)
    order: Literal["asc", "desc"] = "asc"
    limit: int = Field(default=100, ge=1, le=1000)

    @model_validator(mode="after")
    def _validate_observed_range(self) -> "TelemetryEventQuery":
        if (
            self.observed_from_ms is not None
            and self.observed_to_ms is not None
            and self.observed_to_ms < self.observed_from_ms
        ):
            raise ValueError("observed telemetry range is reversed")
        return self


__all__ = [
    "DeviceStateSnapshot",
    "TELEMETRY_PROTOCOL_VERSION",
    "TelemetryEventQuery",
    "TelemetryEventWrite",
    "TelemetryIngestRequest",
    "TelemetryIngestResult",
]
