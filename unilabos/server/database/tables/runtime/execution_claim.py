"""派发前资源声明、当前租约和不可回退的栅栏计数。"""
from __future__ import annotations

from typing import ClassVar
from sqlmodel import Field
from unilabos.server.database.schema import TableSpec
from unilabos.server.database.tables.base import TableObject, NonEmptyStr, JsonObject, json_text_column


class WorkflowExecutionClaimRecord(TableObject, table=True):
    __tablename__: ClassVar[str] = "workflow_execution_claim"
    claim_uuid: NonEmptyStr = Field(primary_key=True)
    job_uuid: NonEmptyStr
    task_uuid: NonEmptyStr
    attempt_no: int
    request: JsonObject = Field(sa_column=json_text_column("request", default_json="{}"))
    fences: JsonObject = Field(sa_column=json_text_column("fences", default_json="{}"))
    state: NonEmptyStr
    acquired_at: NonEmptyStr
    released_at: str | None = None


class WorkflowExecutionFenceRecord(TableObject, table=True):
    __tablename__: ClassVar[str] = "workflow_execution_fence"
    lock_key: NonEmptyStr = Field(primary_key=True)
    token: int


class WorkflowExecutionLeaseRecord(TableObject, table=True):
    __tablename__: ClassVar[str] = "workflow_execution_lease"
    lock_key: NonEmptyStr = Field(primary_key=True)
    claim_uuid: NonEmptyStr
    token: int


EXECUTION_CLAIM_MODELS = (WorkflowExecutionClaimRecord, WorkflowExecutionFenceRecord, WorkflowExecutionLeaseRecord)
EXECUTION_CLAIM_TABLES = (
    TableSpec("workflow_execution_claim", """
        CREATE TABLE IF NOT EXISTS workflow_execution_claim (
            claim_uuid TEXT PRIMARY KEY,
            job_uuid TEXT NOT NULL UNIQUE,
            task_uuid TEXT NOT NULL,
            attempt_no INTEGER NOT NULL CHECK (attempt_no >= 1),
            request TEXT NOT NULL CHECK (json_valid(request) AND json_type(request) = 'object'),
            fences TEXT NOT NULL CHECK (json_valid(fences) AND json_type(fences) = 'object'),
            state TEXT NOT NULL CHECK (state IN ('reserved', 'running', 'uncertain', 'timed_out', 'released')),
            acquired_at TEXT NOT NULL,
            released_at TEXT,
            FOREIGN KEY(job_uuid) REFERENCES workflow_node_job(uuid),
            FOREIGN KEY(task_uuid) REFERENCES workflow_task(uuid)
        )
    """),
    TableSpec("workflow_execution_fence", """
        CREATE TABLE IF NOT EXISTS workflow_execution_fence (
            lock_key TEXT PRIMARY KEY,
            token INTEGER NOT NULL CHECK (token >= 1)
        )
    """),
    TableSpec("workflow_execution_lease", """
        CREATE TABLE IF NOT EXISTS workflow_execution_lease (
            lock_key TEXT PRIMARY KEY,
            claim_uuid TEXT NOT NULL,
            token INTEGER NOT NULL CHECK (token >= 1),
            FOREIGN KEY(claim_uuid) REFERENCES workflow_execution_claim(claim_uuid),
            FOREIGN KEY(lock_key) REFERENCES workflow_execution_fence(lock_key)
        )
    """),
)


class WorkflowExecutionAuthorityRecord(TableObject, table=True):
    __tablename__: ClassVar[str] = "workflow_execution_authority"
    singleton: int = Field(primary_key=True)
    epoch: NonEmptyStr


class WorkflowExecutionDispatchRecord(TableObject, table=True):
    __tablename__: ClassVar[str] = "workflow_execution_dispatch"
    job_uuid: NonEmptyStr = Field(primary_key=True)
    claim_uuid: NonEmptyStr
    authority_epoch: NonEmptyStr
    payload_hash: NonEmptyStr
    identity: JsonObject = Field(sa_column=json_text_column("identity", default_json="{}"))
    invocation_uuid: str | None = None
    executor_epoch: str | None = None
    authorized_at: str | None = None
    receipt: str | None = None


EXECUTION_CLAIM_MODELS += (WorkflowExecutionAuthorityRecord, WorkflowExecutionDispatchRecord)
EXECUTION_CLAIM_TABLES += (
    TableSpec("workflow_execution_authority", """
        CREATE TABLE IF NOT EXISTS workflow_execution_authority (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            epoch TEXT NOT NULL
        )
    """),
    TableSpec("workflow_execution_dispatch", """
        CREATE TABLE IF NOT EXISTS workflow_execution_dispatch (
            job_uuid TEXT PRIMARY KEY,
            claim_uuid TEXT NOT NULL UNIQUE,
            authority_epoch TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            identity TEXT NOT NULL CHECK (json_valid(identity) AND json_type(identity) = 'object'),
            invocation_uuid TEXT,
            executor_epoch TEXT,
            authorized_at TEXT,
            receipt TEXT CHECK (receipt IS NULL OR (json_valid(receipt) AND json_type(receipt) = 'object')),
            FOREIGN KEY(job_uuid) REFERENCES workflow_node_job(uuid),
            FOREIGN KEY(claim_uuid) REFERENCES workflow_execution_claim(claim_uuid)
        )
    """),
)
