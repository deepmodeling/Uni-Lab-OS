"""``telemetry.db`` 的严格设备遥测记录。"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import Field, JsonValue, model_validator

from unilabos.server.models.base import (
    JsonObject,
    NonEmptyStr,
    PositiveVersion,
    ServerObject,
    UnixMilliseconds,
)
from unilabos.server.models.runtime import Transport


ConnectionState = Literal["online", "offline", "degraded", "unknown"]
TelemetryQuality = Literal["good", "uncertain", "bad"]
AlarmSeverity = Literal["info", "warning", "error", "critical"]
AlarmState = Literal["active", "acknowledged", "cleared"]
RetentionTarget = Literal[
    "telemetry_ingest_batch",
    "device_state_report",
    "device_property_sample",
    "device_connection_event",
    "device_alarm_event",
]


class TelemetrySourceCursorRecord(ServerObject):
    """一个 adapter endpoint 的 telemetry epoch/sequence 高水位。"""

    endpoint_uuid: NonEmptyStr
    transport: Transport
    adapter_epoch: NonEmptyStr
    epoch_generation: int = Field(ge=0)
    last_adapter_sequence: int = Field(ge=0)
    last_batch_uuid: NonEmptyStr
    updated_at_ms: UnixMilliseconds
    version: PositiveVersion = 1


class TelemetryIngestBatchRecord(ServerObject):
    """adapter 消息在 telemetry 库内的独立幂等凭据。"""

    batch_id: Optional[int] = Field(default=None, ge=1)
    batch_uuid: NonEmptyStr
    source_event_uuid: NonEmptyStr
    endpoint_uuid: NonEmptyStr
    transport: Transport
    adapter_epoch: NonEmptyStr
    epoch_generation: int = Field(ge=0)
    adapter_sequence: int = Field(ge=0)
    status: Literal["committed", "rejected"]
    item_count: int = Field(default=0, ge=0)
    payload_uuid: Optional[NonEmptyStr] = None
    payload_hash: NonEmptyStr
    rejection_reason: Optional[NonEmptyStr] = None
    received_at_ms: UnixMilliseconds

    @model_validator(mode="after")
    def _validate_rejection(self) -> "TelemetryIngestBatchRecord":
        if self.status == "rejected" and self.rejection_reason is None:
            raise ValueError("rejected telemetry batch requires rejection_reason")
        if self.status == "committed" and self.rejection_reason is not None:
            raise ValueError("committed telemetry batch cannot have rejection_reason")
        return self


class DeviceStateReportRecord(ServerObject):
    """一次完整或增量设备状态上报的原子父记录。"""

    report_id: Optional[int] = Field(default=None, ge=1)
    report_uuid: NonEmptyStr
    batch_uuid: NonEmptyStr
    item_index: int = Field(ge=0)
    device_uuid: NonEmptyStr
    source_job_uuid: Optional[NonEmptyStr] = None
    report_mode: Literal["full", "delta"]
    source_state_version: Optional[int] = Field(default=None, ge=0)
    property_count: int = Field(ge=0)
    state_hash: NonEmptyStr
    payload_uuid: Optional[NonEmptyStr] = None
    observed_at_ms: UnixMilliseconds
    received_at_ms: UnixMilliseconds


class DevicePropertyLatestRecord(ServerObject):
    device_uuid: NonEmptyStr
    property_key: NonEmptyStr
    value_type: NonEmptyStr
    value_json: JsonValue
    value_hash: NonEmptyStr
    quality: TelemetryQuality = "good"
    report_uuid: NonEmptyStr
    source_endpoint_uuid: NonEmptyStr
    source_transport: Transport
    source_job_uuid: Optional[NonEmptyStr] = None
    adapter_epoch: NonEmptyStr
    epoch_generation: int = Field(ge=0)
    adapter_sequence: int = Field(ge=0)
    observed_at_ms: UnixMilliseconds
    received_at_ms: UnixMilliseconds
    version: PositiveVersion = 1


class DevicePropertySampleRecord(ServerObject):
    sample_id: Optional[int] = Field(default=None, ge=1)
    report_uuid: NonEmptyStr
    device_uuid: NonEmptyStr
    property_key: NonEmptyStr
    value_type: NonEmptyStr
    value_json: JsonValue
    value_hash: NonEmptyStr
    quality: TelemetryQuality
    observed_at_ms: UnixMilliseconds
    received_at_ms: UnixMilliseconds


class DeviceConnectionLatestRecord(ServerObject):
    """每个 device/endpoint route 的连接投影，而不是设备级单值。"""

    device_uuid: NonEmptyStr
    endpoint_uuid: NonEmptyStr
    transport: Transport
    connection_state: ConnectionState
    session_uuid: Optional[NonEmptyStr] = None
    source_event_uuid: NonEmptyStr
    adapter_epoch: NonEmptyStr
    epoch_generation: int = Field(ge=0)
    adapter_sequence: int = Field(ge=0)
    observed_at_ms: UnixMilliseconds
    last_seen_at_ms: UnixMilliseconds
    updated_at_ms: UnixMilliseconds
    version: PositiveVersion = 1


class DeviceConnectionEventRecord(ServerObject):
    event_id: Optional[int] = Field(default=None, ge=1)
    event_uuid: NonEmptyStr
    batch_uuid: NonEmptyStr
    item_index: int = Field(ge=0)
    device_uuid: NonEmptyStr
    previous_state: Optional[ConnectionState] = None
    new_state: ConnectionState
    reason: Optional[str] = None
    observed_at_ms: UnixMilliseconds
    received_at_ms: UnixMilliseconds

    @model_validator(mode="after")
    def _validate_transition(self) -> "DeviceConnectionEventRecord":
        if self.previous_state == self.new_state:
            raise ValueError("connection event must change connection state")
        return self


class DeviceAlarmRecord(ServerObject):
    """告警当前投影；状态迁移完整保存在 ``device_alarm_event``。"""

    alarm_uuid: NonEmptyStr
    device_uuid: NonEmptyStr
    source_endpoint_uuid: Optional[NonEmptyStr] = None
    source_transport: Optional[Transport] = None
    source_job_uuid: Optional[NonEmptyStr] = None
    alarm_code: NonEmptyStr
    severity: AlarmSeverity
    state: AlarmState
    summary: NonEmptyStr
    payload_json: JsonObject = Field(default_factory=dict)
    last_event_uuid: NonEmptyStr
    opened_at_ms: UnixMilliseconds
    acknowledged_at_ms: Optional[UnixMilliseconds] = None
    cleared_at_ms: Optional[UnixMilliseconds] = None
    updated_at_ms: UnixMilliseconds
    version: PositiveVersion = 1

    @model_validator(mode="after")
    def _validate_alarm_projection(self) -> "DeviceAlarmRecord":
        if (self.source_endpoint_uuid is None) != (self.source_transport is None):
            raise ValueError(
                "source_endpoint_uuid and source_transport must be set together"
            )
        if self.updated_at_ms < self.opened_at_ms:
            raise ValueError("alarm updated_at_ms cannot precede opened_at_ms")
        if (
            self.acknowledged_at_ms is not None
            and self.acknowledged_at_ms < self.opened_at_ms
        ):
            raise ValueError("alarm acknowledged_at_ms cannot precede opened_at_ms")
        if self.cleared_at_ms is not None and self.cleared_at_ms < self.opened_at_ms:
            raise ValueError("alarm cleared_at_ms cannot precede opened_at_ms")
        if self.state == "active" and self.cleared_at_ms is not None:
            raise ValueError("active alarm cannot have cleared_at_ms")
        if self.state == "acknowledged":
            if self.acknowledged_at_ms is None or self.cleared_at_ms is not None:
                raise ValueError(
                    "acknowledged alarm requires acknowledged_at_ms and no clear time"
                )
        if self.state == "cleared" and self.cleared_at_ms is None:
            raise ValueError("cleared alarm requires cleared_at_ms")
        return self


class DeviceAlarmEventRecord(ServerObject):
    event_id: Optional[int] = Field(default=None, ge=1)
    event_uuid: NonEmptyStr
    alarm_uuid: NonEmptyStr
    device_uuid: NonEmptyStr
    batch_uuid: Optional[NonEmptyStr] = None
    item_index: Optional[int] = Field(default=None, ge=0)
    source_kind: Literal["adapter", "backend", "user", "system"]
    source_command_uuid: Optional[NonEmptyStr] = None
    source_actor_uuid: Optional[NonEmptyStr] = None
    source_job_uuid: Optional[NonEmptyStr] = None
    event_type: Literal["opened", "updated", "acknowledged", "cleared", "reopened"]
    previous_state: Optional[AlarmState] = None
    new_state: AlarmState
    severity: AlarmSeverity
    summary: NonEmptyStr
    payload_json: JsonObject = Field(default_factory=dict)
    occurred_at_ms: UnixMilliseconds
    received_at_ms: UnixMilliseconds

    @model_validator(mode="after")
    def _validate_alarm_event(self) -> "DeviceAlarmEventRecord":
        if (self.batch_uuid is None) != (self.item_index is None):
            raise ValueError("batch_uuid and item_index must be set together")
        if self.source_kind == "adapter" and self.batch_uuid is None:
            raise ValueError("adapter alarm event requires telemetry batch")
        if self.source_kind != "adapter" and self.batch_uuid is not None:
            raise ValueError("non-adapter alarm event cannot claim telemetry batch")
        if self.event_type == "opened":
            if self.previous_state is not None or self.new_state != "active":
                raise ValueError("opened alarm event must enter active from no state")
        elif self.event_type == "acknowledged" and self.new_state != "acknowledged":
            raise ValueError("acknowledged event must enter acknowledged state")
        elif self.event_type == "cleared" and self.new_state != "cleared":
            raise ValueError("cleared event must enter cleared state")
        elif self.event_type == "reopened" and self.new_state != "active":
            raise ValueError("reopened event must enter active state")
        return self


class TelemetryMaintenanceRecord(ServerObject):
    maintenance_key: RetentionTarget
    enabled: bool = True
    keep_days: Optional[int] = Field(default=None, ge=1)
    max_rows: Optional[int] = Field(default=None, ge=1)
    delete_batch_size: int = Field(default=1000, ge=1, le=100000)
    last_pruned_row_id: int = Field(default=0, ge=0)
    last_cutoff_at_ms: Optional[UnixMilliseconds] = None
    last_pruned_at_ms: Optional[UnixMilliseconds] = None
    last_deleted_rows: int = Field(default=0, ge=0)
    updated_at_ms: UnixMilliseconds

    @model_validator(mode="after")
    def _require_retention_bound(self) -> "TelemetryMaintenanceRecord":
        if self.keep_days is None and self.max_rows is None:
            raise ValueError("telemetry retention requires keep_days or max_rows")
        return self


__all__ = [
    "AlarmSeverity",
    "AlarmState",
    "ConnectionState",
    "DeviceAlarmEventRecord",
    "DeviceAlarmRecord",
    "DeviceConnectionEventRecord",
    "DeviceConnectionLatestRecord",
    "DevicePropertyLatestRecord",
    "DevicePropertySampleRecord",
    "DeviceStateReportRecord",
    "RetentionTarget",
    "TelemetryIngestBatchRecord",
    "TelemetryMaintenanceRecord",
    "TelemetryQuality",
    "TelemetrySourceCursorRecord",
]
