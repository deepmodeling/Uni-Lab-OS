"""区间所有权独立于单次动作；每个动作另有实际使用清单。"""
from __future__ import annotations

from typing import ClassVar
from sqlmodel import Field
from unilabos.server.database.schema import TableSpec
from unilabos.server.database.tables.base import TableObject, NonEmptyStr, JsonObject, json_text_column


class WorkflowResourceIntervalRecord(TableObject, table=True):
    __tablename__: ClassVar[str] = "workflow_resource_interval"
    owner_uuid: NonEmptyStr = Field(primary_key=True)
    task_uuid: NonEmptyStr
    lock_key: NonEmptyStr
    definition: JsonObject = Field(sa_column=json_text_column("definition", default_json="{}"))
    state: NonEmptyStr
    version: int
    acquired_job_uuid: NonEmptyStr
    acquired_at: NonEmptyStr
    released_at: str | None = None
    release_reason: str | None = None


class WorkflowResourceAdmissionRecord(TableObject, table=True):
    __tablename__: ClassVar[str] = "workflow_resource_admission"
    job_uuid: NonEmptyStr = Field(primary_key=True)
    request: JsonObject = Field(sa_column=json_text_column("request", default_json="{}"))
    ownerships: JsonObject = Field(sa_column=json_text_column("ownerships", default_json="{}"))
    uses: JsonObject = Field(sa_column=json_text_column("uses", default_json="{}"))
    state: NonEmptyStr
    admitted_at: NonEmptyStr


class WorkflowResourceUseRecord(TableObject, table=True):
    __tablename__: ClassVar[str] = "workflow_resource_use"
    lock_key: NonEmptyStr = Field(primary_key=True)
    job_uuid: NonEmptyStr
    owner_uuid: NonEmptyStr


RESOURCE_INTERVAL_MODELS = (WorkflowResourceIntervalRecord, WorkflowResourceAdmissionRecord, WorkflowResourceUseRecord)
RESOURCE_INTERVAL_TABLES = (
    TableSpec("workflow_resource_interval", """
        CREATE TABLE IF NOT EXISTS workflow_resource_interval (
            owner_uuid TEXT PRIMARY KEY,
            task_uuid TEXT NOT NULL REFERENCES workflow_task(uuid),
            lock_key TEXT NOT NULL,
            definition TEXT NOT NULL CHECK(json_valid(definition) AND json_type(definition)='object'),
            state TEXT NOT NULL CHECK(state IN ('held','retained','released')),
            version INTEGER NOT NULL CHECK(version >= 1),
            acquired_job_uuid TEXT NOT NULL REFERENCES workflow_node_job(uuid),
            acquired_at TEXT NOT NULL,
            released_at TEXT,
            release_reason TEXT
        )
    """, indexes=("CREATE UNIQUE INDEX IF NOT EXISTS idx_resource_interval_active ON workflow_resource_interval(lock_key) WHERE state!='released'",)),
    TableSpec("workflow_resource_admission", """
        CREATE TABLE IF NOT EXISTS workflow_resource_admission (
            job_uuid TEXT PRIMARY KEY REFERENCES workflow_node_job(uuid),
            request TEXT NOT NULL CHECK(json_valid(request) AND json_type(request)='object'),
            ownerships TEXT NOT NULL CHECK(json_valid(ownerships) AND json_type(ownerships)='object'),
            uses TEXT NOT NULL CHECK(json_valid(uses) AND json_type(uses)='object'),
            state TEXT NOT NULL CHECK(state IN ('active','released')),
            admitted_at TEXT NOT NULL
        )
    """),
    TableSpec("workflow_resource_use", """
        CREATE TABLE IF NOT EXISTS workflow_resource_use (
            lock_key TEXT PRIMARY KEY,
            job_uuid TEXT NOT NULL REFERENCES workflow_resource_admission(job_uuid),
            owner_uuid TEXT NOT NULL REFERENCES workflow_resource_interval(owner_uuid)
        )
    """),
)
