"""``telemetry.db`` 的 SQLModel 表记录。"""

from __future__ import annotations

from typing import ClassVar, List, Literal, Optional

from sqlalchemy import Text
from sqlmodel import Field

from unilabos.server.database.tables.base import (
    JsonObject,
    NonEmptyStr,
    PositiveVersion,
    TableObject,
    UnixMilliseconds,
    json_text_column,
)
from unilabos.server.database.migrations.v1.telemetry import (
    TELEMETRY_DATABASE,
    TELEMETRY_TABLES,
)


class TelemetrySourceCursorRecord(TableObject, table=True):
    __tablename__: ClassVar[str] = "telemetry_source_cursor"

    endpoint_uuid: NonEmptyStr = Field(primary_key=True)
    source_epoch: NonEmptyStr
    source_generation: int = Field(default=0, ge=0)
    source_sequence: int = Field(default=0, ge=0)
    last_event_uuid: Optional[NonEmptyStr] = None
    last_received_at_ms: UnixMilliseconds
    version: PositiveVersion = 1


class DeviceStateLatestRecord(TableObject, table=True):
    """一个 endpoint/device 的完整最新设备状态。"""

    __tablename__: ClassVar[str] = "device_state_latest"

    endpoint_uuid: NonEmptyStr = Field(primary_key=True)
    device_uuid: NonEmptyStr = Field(primary_key=True)
    source_event_uuid: NonEmptyStr
    source_epoch: NonEmptyStr
    source_generation: int = Field(ge=0)
    source_sequence: int = Field(ge=0)
    state: JsonObject = Field(
        default_factory=dict,
        sa_column=json_text_column("state_json", default_json="{}"),
    )
    properties: JsonObject = Field(
        default_factory=dict,
        sa_column=json_text_column("properties_json", default_json="{}"),
    )
    connection_state: Literal["online", "offline", "degraded", "unknown"] = Field(
        default="unknown", sa_type=Text
    )
    alarms: List[JsonObject] = Field(
        default_factory=list,
        sa_column=json_text_column("alarms_json", default_json="[]"),
    )
    state_hash: NonEmptyStr
    observed_at_ms: UnixMilliseconds
    received_at_ms: UnixMilliseconds
    version: PositiveVersion = 1


class TelemetryEventRecord(TableObject, table=True):
    __tablename__: ClassVar[str] = "telemetry_event"

    sequence: Optional[int] = Field(default=None, ge=1, primary_key=True)
    event_uuid: NonEmptyStr
    endpoint_uuid: NonEmptyStr
    device_uuid: Optional[NonEmptyStr] = None
    source_epoch: NonEmptyStr
    source_generation: int = Field(ge=0)
    source_sequence: int = Field(ge=0)
    event_type: Literal["state", "property_sample", "connection", "alarm"] = Field(
        sa_type=Text
    )
    event_key: Optional[NonEmptyStr] = None
    payload: object = Field(
        sa_column=json_text_column("payload_json", default_json="{}")
    )
    payload_hash: NonEmptyStr
    severity: Optional[str] = None
    source_job_uuid: Optional[NonEmptyStr] = None
    source_command_uuid: Optional[NonEmptyStr] = None
    observed_at_ms: UnixMilliseconds
    received_at_ms: UnixMilliseconds


TELEMETRY_TABLE_MODELS = (
    TelemetrySourceCursorRecord,
    DeviceStateLatestRecord,
    TelemetryEventRecord,
)


__all__ = [
    "DeviceStateLatestRecord",
    "TELEMETRY_DATABASE",
    "TELEMETRY_TABLE_MODELS",
    "TELEMETRY_TABLES",
    "TelemetryEventRecord",
    "TelemetrySourceCursorRecord",
]
