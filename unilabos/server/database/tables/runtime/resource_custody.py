"""设备托管、显式借还和等待关系的持久事实。"""
from typing import ClassVar
from sqlmodel import Field
from unilabos.server.database.schema import TableSpec
from unilabos.server.database.tables.base import TableObject, NonEmptyStr, JsonObject, json_text_column


class WorkflowResourceLoanRecord(TableObject, table=True):
    __tablename__: ClassVar[str] = "workflow_resource_loan"
    loan_uuid: NonEmptyStr = Field(primary_key=True)
    lender_owner_uuid: NonEmptyStr
    borrower_task_uuid: NonEmptyStr
    borrower_owner_uuid: str | None = None
    definition: JsonObject = Field(sa_column=json_text_column("definition", default_json="{}"))
    state: NonEmptyStr
    version: int
    reason: NonEmptyStr
    created_at: NonEmptyStr
    returned_at: str | None = None
    return_reason: str | None = None


class WorkflowDeviceTenancyRecord(TableObject, table=True):
    __tablename__: ClassVar[str] = "workflow_device_tenancy"
    tenancy_uuid: NonEmptyStr = Field(primary_key=True)
    task_uuid: NonEmptyStr
    material_uuid: NonEmptyStr
    device_key: NonEmptyStr
    acquired_job_uuid: NonEmptyStr
    state: NonEmptyStr
    version: int
    created_at: NonEmptyStr
    released_at: str | None = None
    release_reason: str | None = None


class WorkflowTenancyTransitionRecord(TableObject, table=True):
    __tablename__: ClassVar[str] = "workflow_tenancy_transition"
    job_uuid: NonEmptyStr = Field(primary_key=True)
    contract: JsonObject = Field(sa_column=json_text_column("contract", default_json="{}"))
    acquire_uuid: str | None = None
    release_uuid: str | None = None
    state: NonEmptyStr


class WorkflowResourceWaiterRecord(TableObject, table=True):
    __tablename__: ClassVar[str] = "workflow_resource_waiter"
    job_uuid: NonEmptyStr = Field(primary_key=True)
    task_uuid: NonEmptyStr
    lock_keys: list[str] = Field(sa_column=json_text_column("lock_keys", default_json="[]"))
    blockers: list[str] = Field(sa_column=json_text_column("blockers", default_json="[]"))


RESOURCE_CUSTODY_MODELS = (WorkflowResourceLoanRecord, WorkflowDeviceTenancyRecord,
                           WorkflowTenancyTransitionRecord, WorkflowResourceWaiterRecord)
RESOURCE_CUSTODY_TABLES = (
    TableSpec("workflow_resource_loan", """
        CREATE TABLE IF NOT EXISTS workflow_resource_loan (
            loan_uuid TEXT PRIMARY KEY,
            lender_owner_uuid TEXT NOT NULL REFERENCES workflow_resource_interval(owner_uuid),
            borrower_task_uuid TEXT NOT NULL REFERENCES workflow_task(uuid),
            borrower_owner_uuid TEXT,
            definition TEXT NOT NULL CHECK(json_valid(definition) AND json_type(definition)='object'),
            state TEXT NOT NULL CHECK(state IN ('lent','retained','returned')),
            version INTEGER NOT NULL CHECK(version>=1),
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL,
            returned_at TEXT,
            return_reason TEXT
        )
    """, indexes=("CREATE UNIQUE INDEX IF NOT EXISTS idx_resource_loan_active ON workflow_resource_loan(lender_owner_uuid) WHERE state!='returned'",)),
    TableSpec("workflow_device_tenancy", """
        CREATE TABLE IF NOT EXISTS workflow_device_tenancy (
            tenancy_uuid TEXT PRIMARY KEY,
            task_uuid TEXT NOT NULL REFERENCES workflow_task(uuid),
            material_uuid TEXT NOT NULL,
            device_key TEXT NOT NULL,
            acquired_job_uuid TEXT NOT NULL REFERENCES workflow_node_job(uuid),
            state TEXT NOT NULL CHECK(state IN ('reserved','loaded','retained','released')),
            version INTEGER NOT NULL CHECK(version>=1),
            created_at TEXT NOT NULL,
            released_at TEXT,
            release_reason TEXT
        )
    """, indexes=("CREATE UNIQUE INDEX IF NOT EXISTS idx_device_tenancy_active ON workflow_device_tenancy(device_key) WHERE state!='released'",)),
    TableSpec("workflow_tenancy_transition", """
        CREATE TABLE IF NOT EXISTS workflow_tenancy_transition (
            job_uuid TEXT PRIMARY KEY REFERENCES workflow_node_job(uuid),
            contract TEXT NOT NULL CHECK(json_valid(contract) AND json_type(contract)='object'),
            acquire_uuid TEXT REFERENCES workflow_device_tenancy(tenancy_uuid),
            release_uuid TEXT REFERENCES workflow_device_tenancy(tenancy_uuid),
            state TEXT NOT NULL CHECK(state IN ('prepared','settled','retained','reverted'))
        )
    """),
    TableSpec("workflow_resource_waiter", """
        CREATE TABLE IF NOT EXISTS workflow_resource_waiter (
            job_uuid TEXT PRIMARY KEY REFERENCES workflow_node_job(uuid),
            task_uuid TEXT NOT NULL REFERENCES workflow_task(uuid),
            lock_keys TEXT NOT NULL CHECK(json_valid(lock_keys) AND json_type(lock_keys)='array'),
            blockers TEXT NOT NULL CHECK(json_valid(blockers) AND json_type(blockers)='array')
        )
    """),
)
