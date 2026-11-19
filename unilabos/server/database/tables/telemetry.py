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
from unilabos.server.database.schema import (
    SCHEMA_IDENTITY_TABLE,
    DatabaseSpec,
    TableSpec,
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


TELEMETRY_TABLES = (
    SCHEMA_IDENTITY_TABLE,
    TableSpec(
        "telemetry_source_cursor",
        """
        CREATE TABLE IF NOT EXISTS telemetry_source_cursor (
            endpoint_uuid TEXT PRIMARY KEY CHECK (TRIM(endpoint_uuid) <> ''),
            source_epoch TEXT NOT NULL CHECK (TRIM(source_epoch) <> ''),
            source_generation INTEGER NOT NULL DEFAULT 0
                CHECK (source_generation >= 0),
            source_sequence INTEGER NOT NULL DEFAULT 0 CHECK (source_sequence >= 0),
            last_event_uuid TEXT,
            last_received_at_ms INTEGER NOT NULL CHECK (last_received_at_ms >= 0),
            version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0)
        )
        """,
    ),
    TableSpec(
        "device_state_latest",
        """
        CREATE TABLE IF NOT EXISTS device_state_latest (
            endpoint_uuid TEXT NOT NULL CHECK (TRIM(endpoint_uuid) <> ''),
            device_uuid TEXT NOT NULL CHECK (TRIM(device_uuid) <> ''),
            source_event_uuid TEXT NOT NULL CHECK (TRIM(source_event_uuid) <> ''),
            source_epoch TEXT NOT NULL CHECK (TRIM(source_epoch) <> ''),
            source_generation INTEGER NOT NULL CHECK (source_generation >= 0),
            source_sequence INTEGER NOT NULL CHECK (source_sequence >= 0),
            state_json TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(state_json) AND json_type(state_json) = 'object'
            ),
            properties_json TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(properties_json) AND json_type(properties_json) = 'object'
            ),
            connection_state TEXT NOT NULL DEFAULT 'unknown' CHECK (
                connection_state IN ('online','offline','degraded','unknown')
            ),
            alarms_json TEXT NOT NULL DEFAULT '[]' CHECK (
                json_valid(alarms_json) AND json_type(alarms_json) = 'array'
            ),
            state_hash TEXT NOT NULL CHECK (TRIM(state_hash) <> ''),
            observed_at_ms INTEGER NOT NULL CHECK (observed_at_ms >= 0),
            received_at_ms INTEGER NOT NULL CHECK (received_at_ms >= 0),
            version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
            PRIMARY KEY(endpoint_uuid, device_uuid),
            UNIQUE(source_event_uuid, device_uuid)
        )
        """,
        (
            """
            CREATE INDEX IF NOT EXISTS idx_device_state_connection
            ON device_state_latest(connection_state, received_at_ms DESC)
            """,
            """
            CREATE TRIGGER IF NOT EXISTS trg_device_state_latest_reject_stale
            BEFORE UPDATE ON device_state_latest
            WHEN NEW.source_epoch = OLD.source_epoch AND (
                NEW.source_generation < OLD.source_generation
                OR (
                    NEW.source_generation = OLD.source_generation
                    AND NEW.source_sequence <= OLD.source_sequence
                )
            )
            BEGIN
                SELECT RAISE(ABORT, 'stale device state');
            END
            """,
        ),
    ),
    TableSpec(
        "telemetry_event",
        """
        CREATE TABLE IF NOT EXISTS telemetry_event (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            event_uuid TEXT NOT NULL UNIQUE CHECK (TRIM(event_uuid) <> ''),
            endpoint_uuid TEXT NOT NULL CHECK (TRIM(endpoint_uuid) <> ''),
            device_uuid TEXT,
            source_epoch TEXT NOT NULL CHECK (TRIM(source_epoch) <> ''),
            source_generation INTEGER NOT NULL CHECK (source_generation >= 0),
            source_sequence INTEGER NOT NULL CHECK (source_sequence >= 0),
            event_type TEXT NOT NULL CHECK (event_type IN (
                'state','property_sample','connection','alarm'
            )),
            event_key TEXT,
            payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
            payload_hash TEXT NOT NULL CHECK (TRIM(payload_hash) <> ''),
            severity TEXT,
            source_job_uuid TEXT,
            source_command_uuid TEXT,
            observed_at_ms INTEGER NOT NULL CHECK (observed_at_ms >= 0),
            received_at_ms INTEGER NOT NULL CHECK (received_at_ms >= 0),
            CHECK (device_uuid IS NULL OR TRIM(device_uuid) <> ''),
            CHECK (event_key IS NULL OR TRIM(event_key) <> '')
        )
        """,
        (
            """
            CREATE INDEX IF NOT EXISTS idx_telemetry_event_device_time
            ON telemetry_event(device_uuid, event_type, observed_at_ms DESC)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_telemetry_event_source
            ON telemetry_event(
                endpoint_uuid, source_epoch, source_generation, source_sequence
            )
            """,
        ),
    ),
)


TELEMETRY_DATABASE = DatabaseSpec(
    key="telemetry",
    filename="telemetry.db",
    role="high-frequency device state and telemetry",
    synchronous="NORMAL",
    tables=TELEMETRY_TABLES,
)

__all__ = [
    "DeviceStateLatestRecord",
    "TELEMETRY_DATABASE",
    "TELEMETRY_TABLE_MODELS",
    "TELEMETRY_TABLES",
    "TelemetryEventRecord",
    "TelemetrySourceCursorRecord",
]
