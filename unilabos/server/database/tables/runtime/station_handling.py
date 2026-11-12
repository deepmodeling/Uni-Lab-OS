"""V1 工站错误处置的持久事实（runtime.db）：工站控制、控制命令、处置会话与工站错误。"""

from __future__ import annotations

from typing import ClassVar, Optional

from sqlmodel import Field

from unilabos.server.database.schema import TableSpec
from unilabos.server.database.tables.base import JsonObject, NonEmptyStr, TableObject, json_text_column
from unilabos.protocol.base import JsonArray


class WorkflowStationControlRecord(TableObject, table=True):
    __tablename__: ClassVar[str] = "workflow_station_control"
    station_id: NonEmptyStr = Field(primary_key=True)
    mode: str = "RUNNING"
    version: int = 1
    error_epoch: int = 0
    active_session_id: Optional[str] = None
    last_control_command_uuid: Optional[str] = None
    updated_at: NonEmptyStr


class WorkflowStationControlCommandRecord(TableObject, table=True):
    __tablename__: ClassVar[str] = "workflow_station_control_command"
    command_uuid: NonEmptyStr = Field(primary_key=True)
    station_id: NonEmptyStr
    operation: NonEmptyStr
    idempotency_key: NonEmptyStr
    request_hash: NonEmptyStr
    expected_version: int
    applied_version: Optional[int] = None
    error_epoch: int
    operator_id: NonEmptyStr
    operator_confirmed: int = 0
    snapshot_json: JsonObject = Field(sa_column=json_text_column("snapshot_json", default_json="{}"))
    status: str = "PENDING"
    created_at: NonEmptyStr
    applied_at: Optional[str] = None


class WorkflowErrorSessionRecord(TableObject, table=True):
    __tablename__: ClassVar[str] = "workflow_error_session"
    session_id: NonEmptyStr = Field(primary_key=True)
    station_id: NonEmptyStr
    decision_id: NonEmptyStr
    stage: NonEmptyStr
    version: int = 1
    summary: str = ""
    created_by: NonEmptyStr
    completed_by: Optional[str] = None
    created_at: NonEmptyStr
    completed_at: Optional[str] = None
    resolved_at: Optional[str] = None


class WorkflowStationErrorRecord(TableObject, table=True):
    __tablename__: ClassVar[str] = "workflow_station_error"
    decision_id: NonEmptyStr = Field(primary_key=True)
    station_id: NonEmptyStr
    intervention_uuid: Optional[str] = None
    workflow_task_uuid: NonEmptyStr
    workflow_node_job_uuid: NonEmptyStr
    revision: int = 1
    stage: str = "DECISION_REQUIRED"
    status: str = "open"
    session_id: Optional[str] = None
    initial_action: Optional[str] = None
    final_action: Optional[str] = None
    meta_data: JsonObject = Field(sa_column=json_text_column("meta_data", default_json="{}"))
    options: JsonArray = Field(sa_column=json_text_column("options", default_json="[]"))
    opened_at: NonEmptyStr
    update_time: NonEmptyStr
    decided_at: Optional[str] = None
    resolved_at: Optional[str] = None


STATION_HANDLING_MODELS = (
    WorkflowStationControlRecord,
    WorkflowStationControlCommandRecord,
    WorkflowErrorSessionRecord,
    WorkflowStationErrorRecord,
)
STATION_HANDLING_TABLES = (
    TableSpec("workflow_station_control", """
        CREATE TABLE IF NOT EXISTS workflow_station_control (
            station_id TEXT PRIMARY KEY,
            mode TEXT NOT NULL DEFAULT 'RUNNING' CHECK (mode IN ('RUNNING', 'PAUSED')),
            version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
            error_epoch INTEGER NOT NULL DEFAULT 0 CHECK (error_epoch >= 0),
            active_session_id TEXT,
            last_control_command_uuid TEXT,
            updated_at TEXT NOT NULL
        )
    """),
    TableSpec("workflow_station_control_command", """
        CREATE TABLE IF NOT EXISTS workflow_station_control_command (
            command_uuid TEXT PRIMARY KEY,
            station_id TEXT NOT NULL,
            operation TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            request_hash TEXT NOT NULL,
            expected_version INTEGER NOT NULL,
            applied_version INTEGER,
            error_epoch INTEGER NOT NULL,
            operator_id TEXT NOT NULL,
            operator_confirmed INTEGER NOT NULL DEFAULT 0,
            snapshot_json TEXT NOT NULL CHECK (json_valid(snapshot_json) AND json_type(snapshot_json) = 'object'),
            status TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING', 'APPLIED', 'FAILED')),
            created_at TEXT NOT NULL,
            applied_at TEXT,
            UNIQUE(station_id, operation, idempotency_key)
        )
    """),
    TableSpec("workflow_error_session", """
        CREATE TABLE IF NOT EXISTS workflow_error_session (
            session_id TEXT PRIMARY KEY,
            station_id TEXT NOT NULL,
            decision_id TEXT NOT NULL UNIQUE,
            stage TEXT NOT NULL CHECK (stage IN ('MANUAL_HANDLING', 'DECISION_REQUIRED', 'RESOLVED')),
            version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
            summary TEXT NOT NULL DEFAULT '',
            created_by TEXT NOT NULL,
            completed_by TEXT,
            created_at TEXT NOT NULL,
            completed_at TEXT,
            resolved_at TEXT
        )
    """),
    TableSpec("workflow_station_error", """
        CREATE TABLE IF NOT EXISTS workflow_station_error (
            decision_id TEXT PRIMARY KEY,
            station_id TEXT NOT NULL,
            intervention_uuid TEXT,
            workflow_task_uuid TEXT NOT NULL REFERENCES workflow_task(uuid),
            workflow_node_job_uuid TEXT NOT NULL REFERENCES workflow_node_job(uuid),
            revision INTEGER NOT NULL CHECK (revision >= 1),
            stage TEXT NOT NULL CHECK (stage IN ('DECISION_REQUIRED', 'MANUAL_HANDLING', 'APPLYING', 'RESOLVED')),
            status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'selected', 'superseded')),
            session_id TEXT,
            initial_action TEXT,
            final_action TEXT,
            meta_data TEXT NOT NULL CHECK (json_valid(meta_data) AND json_type(meta_data) = 'object'),
            options TEXT NOT NULL CHECK (json_valid(options) AND json_type(options) = 'array'),
            opened_at TEXT NOT NULL,
            update_time TEXT NOT NULL,
            decided_at TEXT,
            resolved_at TEXT
        )
    """, (
        """CREATE INDEX IF NOT EXISTS idx_station_error_open ON workflow_station_error(station_id, stage, opened_at)""",
        """CREATE INDEX IF NOT EXISTS idx_station_error_job ON workflow_station_error(workflow_node_job_uuid)""",
    )),
)

__all__ = [
    "STATION_HANDLING_MODELS",
    "STATION_HANDLING_TABLES",
]
