"""Workflow Authority 的 SQLModel 表记录与表规格（落 ``runtime.db``）。

本文件是 16 张表的行模型与建表 DDL 的唯一来源；调度产物与运行控制
同域同库，表规格由 ``RUNTIME_DATABASE`` 聚合，打开时按 ``schema_identity``
checksum 校验，并遵循数据库重建策略。
``workflow_runs`` / ``job_runs`` 是供 hyper-data 审计读取的只读投影视图，
与 ``frontend_event`` 的语句序列一并创建。
"""

from __future__ import annotations

from typing import ClassVar, Optional

from sqlalchemy import Column, Text
from sqlmodel import Field

from unilabos.server.database.schema import (
    SCHEMA_IDENTITY_TABLE,
    TableSpec,
)
from unilabos.server.database.tables.base import (
    JsonObject,
    NonEmptyStr,
    TableObject,
    json_text_column,
)
from unilabos.protocol.base import JsonArray


class WorkflowRecord(TableObject, table=True):
    __tablename__: ClassVar[str] = "workflow"

    uuid: NonEmptyStr = Field(primary_key=True)
    create_time: NonEmptyStr
    update_time: NonEmptyStr
    deleted_at: Optional[str] = None
    description: Optional[str] = None
    meta_data: JsonObject = Field(
        default_factory=dict,
        sa_column=json_text_column("meta_data", default_json="{}"),
    )
    name: NonEmptyStr
    tags: JsonArray = Field(
        default_factory=list,
        sa_column=json_text_column("tags", default_json="[]"),
    )
    revision: int = Field(default=1, ge=1)


class WorkflowNodeTemplateRecord(TableObject, table=True):
    __tablename__: ClassVar[str] = "workflow_node_template"

    uuid: NonEmptyStr = Field(primary_key=True)
    create_time: NonEmptyStr
    update_time: NonEmptyStr
    deleted_at: Optional[str] = None
    description: Optional[str] = None
    meta_data: JsonObject = Field(
        default_factory=dict,
        sa_column=json_text_column("meta_data", default_json="{}"),
    )
    authority_id: NonEmptyStr
    resource_template_uuid: NonEmptyStr
    name: NonEmptyStr
    display_name: str
    class_: Optional[str] = Field(default=None, sa_column=Column("class", Text, nullable=True))
    goal: JsonObject = Field(
        default_factory=dict,
        sa_column=json_text_column("goal", default_json="{}"),
    )
    goal_default: JsonObject = Field(
        default_factory=dict,
        sa_column=json_text_column("goal_default", default_json="{}"),
    )
    feedback: JsonObject = Field(
        default_factory=dict,
        sa_column=json_text_column("feedback", default_json="{}"),
    )
    result: JsonObject = Field(
        default_factory=dict,
        sa_column=json_text_column("result", default_json="{}"),
    )
    schema_: Optional[JsonObject] = Field(
        default=None,
        sa_column=json_text_column("schema", default_json="{}", nullable=True),
    )
    type: NonEmptyStr
    icon: Optional[str] = None
    header: Optional[str] = None
    footer: Optional[str] = None
    node_type: NonEmptyStr


class WorkflowHandleTemplateRecord(TableObject, table=True):
    __tablename__: ClassVar[str] = "workflow_handle_template"

    uuid: NonEmptyStr = Field(primary_key=True)
    create_time: NonEmptyStr
    update_time: NonEmptyStr
    deleted_at: Optional[str] = None
    description: Optional[str] = None
    meta_data: JsonObject = Field(
        default_factory=dict,
        sa_column=json_text_column("meta_data", default_json="{}"),
    )
    authority_id: NonEmptyStr
    workflow_node_template_uuid: NonEmptyStr
    handle_key: NonEmptyStr
    io_type: NonEmptyStr
    display_name: str
    type: NonEmptyStr
    required: int = Field(ge=0, le=1)
    data_source: Optional[str] = None
    data_key: Optional[str] = None


class WorkflowNodeRecord(TableObject, table=True):
    __tablename__: ClassVar[str] = "workflow_node"

    uuid: NonEmptyStr = Field(primary_key=True)
    create_time: NonEmptyStr
    update_time: NonEmptyStr
    deleted_at: Optional[str] = None
    description: Optional[str] = None
    meta_data: JsonObject = Field(
        default_factory=dict,
        sa_column=json_text_column("meta_data", default_json="{}"),
    )
    workflow_uuid: NonEmptyStr
    workflow_node_template_uuid: Optional[str] = None
    parent_uuid: Optional[str] = None
    material_uuid: Optional[str] = None
    name: NonEmptyStr
    status: NonEmptyStr
    type: NonEmptyStr
    icon: Optional[str] = None
    pose: JsonObject = Field(
        default_factory=dict,
        sa_column=json_text_column("pose", default_json="{}"),
    )
    param: JsonObject = Field(
        default_factory=dict,
        sa_column=json_text_column("param", default_json="{}"),
    )
    footer: Optional[str] = None
    action_name: Optional[str] = None
    action_type: Optional[str] = None
    execution_policy: JsonObject = Field(
        default_factory=dict,
        sa_column=json_text_column("execution_policy", default_json="{}"),
    )
    disabled: int = Field(ge=0, le=1)
    minimized: int = Field(ge=0, le=1)
    script: Optional[str] = None


class WorkflowEdgeRecord(TableObject, table=True):
    __tablename__: ClassVar[str] = "workflow_edge"

    uuid: NonEmptyStr = Field(primary_key=True)
    create_time: NonEmptyStr
    update_time: NonEmptyStr
    deleted_at: Optional[str] = None
    description: Optional[str] = None
    meta_data: JsonObject = Field(
        default_factory=dict,
        sa_column=json_text_column("meta_data", default_json="{}"),
    )
    workflow_uuid: NonEmptyStr
    source_node_uuid: NonEmptyStr
    target_node_uuid: NonEmptyStr
    source_handle_uuid: NonEmptyStr
    target_handle_uuid: NonEmptyStr


class WorkflowTaskRecord(TableObject, table=True):
    __tablename__: ClassVar[str] = "workflow_task"

    uuid: NonEmptyStr = Field(primary_key=True)
    create_time: NonEmptyStr
    update_time: NonEmptyStr
    deleted_at: Optional[str] = None
    description: Optional[str] = None
    meta_data: JsonObject = Field(
        default_factory=dict,
        sa_column=json_text_column("meta_data", default_json="{}"),
    )
    workflow_uuid: Optional[str] = None
    status: str = "pending"
    workflow_snapshot: JsonObject = Field(
        default_factory=dict,
        sa_column=json_text_column("workflow_snapshot", default_json="{}"),
    )
    execution_plan: JsonObject = Field(
        default_factory=dict,
        sa_column=json_text_column("execution_plan", default_json="{}"),
    )
    run_mode: str = "normal"
    target_node_uuid: Optional[str] = None
    control_status: str = "active"
    cleanup_status: str = "none"
    trace_context: JsonObject = Field(
        default_factory=dict,
        sa_column=json_text_column("trace_context", default_json="{}"),
    )
    input: JsonObject = Field(
        default_factory=dict,
        sa_column=json_text_column("input", default_json="{}"),
    )
    output: JsonObject = Field(
        default_factory=dict,
        sa_column=json_text_column("output", default_json="{}"),
    )
    error_info: JsonArray = Field(
        default_factory=list,
        sa_column=json_text_column("error_info", default_json="[]"),
    )
    timeout_at: Optional[str] = None
    attention_reason: Optional[str] = None
    terminal_ghost_detected_at: Optional[str] = None
    reconciliation_resume_control_status: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    execution_kind: str = "workflow"
    idempotency_key: Optional[str] = None
    request_fingerprint: str = ""
    #: V1 任务优先级（normal / high），只随任务持久化，排序由调度器负责
    priority: str = "normal"
    #: V1 库存绑定：逻辑需求键 → 具体库存实例与预留量（创建时冻结）
    inventory_bindings: JsonArray = Field(
        default_factory=list,
        sa_column=json_text_column("inventory_bindings", default_json="[]"),
    )
    #: V1 普通模式的派发闸门：pause 关闸但不改执行模式
    dispatch_gate: str = "active"


class WorkflowNodeRunRecord(TableObject, table=True):
    """节点运行：任务内每个工作流节点唯一的执行单元（≡ runtime.v1 ``attempt_group_uuid``）。

    定义列在建任务时写死；``status / return_info / error_info / current_job_uuid /
    attempt_count`` 等是当前 attempt 的投影，只由 store 在同一事务里随 attempt 变更同步写。
    DAG、画布节点、任务 output 都以本表的 uuid 为稳定身份。
    """

    __tablename__: ClassVar[str] = "workflow_node_run"

    uuid: NonEmptyStr = Field(primary_key=True)
    create_time: NonEmptyStr
    update_time: NonEmptyStr
    deleted_at: Optional[str] = None
    description: Optional[str] = None
    meta_data: JsonObject = Field(
        default_factory=dict,
        sa_column=json_text_column("meta_data", default_json="{}"),
    )
    workflow_task_uuid: NonEmptyStr
    workflow_node_uuid: NonEmptyStr
    topological_index: int = Field(default=0, ge=0)
    executor_kind: str = "compute"
    execution_policy: JsonObject = Field(
        default_factory=dict,
        sa_column=json_text_column("execution_policy", default_json="{}"),
    )
    execution_timeout_seconds: int = Field(default=0, ge=0)
    param: JsonObject = Field(
        default_factory=dict,
        sa_column=json_text_column("param", default_json="{}"),
    )
    material_uuid: Optional[str] = None
    status: str = "pending"
    current_job_uuid: Optional[str] = None
    attempt_count: int = Field(default=0, ge=0)
    return_info: JsonObject = Field(
        default_factory=dict,
        sa_column=json_text_column("return_info", default_json="{}"),
    )
    error_info: JsonArray = Field(
        default_factory=list,
        sa_column=json_text_column("error_info", default_json="[]"),
    )
    feedback_data: JsonObject = Field(
        default_factory=dict,
        sa_column=json_text_column("feedback_data", default_json="{}"),
    )
    control_data: JsonObject = Field(
        default_factory=dict,
        sa_column=json_text_column("control_data", default_json="{}"),
    )
    started_at: Optional[str] = None
    finished_at: Optional[str] = None


class WorkflowNodeJobRecord(TableObject, table=True):
    """节点运行的一次物理执行（attempt，≡ runtime.v1 ``job_uuid`` = 执行器 job_id）。

    结果、反馈历史、干预记录都挂在 attempt 上；``retry_of_job_uuid`` 把重试链接成链，
    ``error_resolution`` 记录该 attempt 失败后的决策（abort / retry / operator_intervention）。
    """

    __tablename__: ClassVar[str] = "workflow_node_job"

    uuid: NonEmptyStr = Field(primary_key=True)
    create_time: NonEmptyStr
    update_time: NonEmptyStr
    deleted_at: Optional[str] = None
    description: Optional[str] = None
    meta_data: JsonObject = Field(
        default_factory=dict,
        sa_column=json_text_column("meta_data", default_json="{}"),
    )
    workflow_node_run_uuid: NonEmptyStr
    workflow_task_uuid: NonEmptyStr
    workflow_node_uuid: NonEmptyStr
    attempt_no: int = Field(default=1, ge=1)
    retry_of_job_uuid: Optional[str] = None
    trigger: str = "initial"
    edge_agent_uuid: Optional[str] = None
    edge_command_uuid: Optional[str] = None
    job_access_token_hash: str = ""
    feedback_sequence: int = Field(default=0, ge=0)
    status: str = "pending"
    param: JsonObject = Field(
        default_factory=dict,
        sa_column=json_text_column("param", default_json="{}"),
    )
    feedback_data: JsonObject = Field(
        default_factory=dict,
        sa_column=json_text_column("feedback_data", default_json="{}"),
    )
    return_info: JsonObject = Field(
        default_factory=dict,
        sa_column=json_text_column("return_info", default_json="{}"),
    )
    error_resolution: JsonObject = Field(
        default_factory=dict,
        sa_column=json_text_column("error_resolution", default_json="{}"),
    )
    control_data: JsonObject = Field(
        default_factory=dict,
        sa_column=json_text_column("control_data", default_json="{}"),
    )
    error_info: JsonArray = Field(
        default_factory=list,
        sa_column=json_text_column("error_info", default_json="[]"),
    )
    dispatch_deadline_at: Optional[str] = None
    execution_deadline_at: Optional[str] = None
    cancel_command_uuid: Optional[str] = None
    cancel_ack_deadline_at: Optional[str] = None
    cancel_complete_deadline_at: Optional[str] = None
    uncertainty_reason: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None


class WorkflowTaskCommandRecord(TableObject, table=True):
    __tablename__: ClassVar[str] = "workflow_task_command"

    uuid: NonEmptyStr = Field(primary_key=True)
    create_time: NonEmptyStr
    update_time: NonEmptyStr
    deleted_at: Optional[str] = None
    description: Optional[str] = None
    meta_data: JsonObject = Field(
        default_factory=dict,
        sa_column=json_text_column("meta_data", default_json="{}"),
    )
    workflow_task_uuid: NonEmptyStr
    type: NonEmptyStr
    target_node_uuid: Optional[str] = None
    idempotency_key: NonEmptyStr
    status: NonEmptyStr
    result: JsonObject = Field(
        default_factory=dict,
        sa_column=json_text_column("result", default_json="{}"),
    )
    trace_context: JsonObject = Field(
        default_factory=dict,
        sa_column=json_text_column("trace_context", default_json="{}"),
    )
    consumed_at: Optional[str] = None


class ExecutionLockLeaseRecord(TableObject, table=True):
    __tablename__: ClassVar[str] = "execution_lock_lease"

    uuid: NonEmptyStr = Field(primary_key=True)
    create_time: NonEmptyStr
    update_time: NonEmptyStr
    deleted_at: Optional[str] = None
    description: Optional[str] = None
    meta_data: JsonObject = Field(
        default_factory=dict,
        sa_column=json_text_column("meta_data", default_json="{}"),
    )
    lock_key: NonEmptyStr
    material_uuid: NonEmptyStr
    workflow_task_uuid: NonEmptyStr
    workflow_node_job_uuid: NonEmptyStr
    state: NonEmptyStr
    acquired_at: NonEmptyStr
    released_at: Optional[str] = None


class WorkflowNodeJobResultRecord(TableObject, table=True):
    __tablename__: ClassVar[str] = "workflow_node_job_result"

    uuid: NonEmptyStr = Field(primary_key=True)
    create_time: NonEmptyStr
    update_time: NonEmptyStr
    deleted_at: Optional[str] = None
    description: Optional[str] = None
    meta_data: JsonObject = Field(
        default_factory=dict,
        sa_column=json_text_column("meta_data", default_json="{}"),
    )
    workflow_node_job_uuid: NonEmptyStr
    edge_command_uuid: NonEmptyStr
    job_access_token_hash: str
    idempotency_key: NonEmptyStr
    outcome: NonEmptyStr
    return_info: JsonObject = Field(
        default_factory=dict,
        sa_column=json_text_column("return_info", default_json="{}"),
    )
    error_info: JsonArray = Field(
        default_factory=list,
        sa_column=json_text_column("error_info", default_json="[]"),
    )
    committed_at: NonEmptyStr
    consumed_at: Optional[str] = None


class WorkflowNodeJobFeedbackHistoryRecord(TableObject, table=True):
    __tablename__: ClassVar[str] = "workflow_node_job_feedback_history"

    uuid: NonEmptyStr = Field(primary_key=True)
    create_time: NonEmptyStr
    update_time: NonEmptyStr
    deleted_at: Optional[str] = None
    description: Optional[str] = None
    meta_data: JsonObject = Field(
        default_factory=dict,
        sa_column=json_text_column("meta_data", default_json="{}"),
    )
    workflow_node_job_uuid: NonEmptyStr
    sequence: int = Field(ge=1)
    feedback_type: NonEmptyStr
    data: JsonObject = Field(
        default_factory=dict,
        sa_column=json_text_column("data", default_json="{}"),
    )
    observed_at: NonEmptyStr
    received_at: NonEmptyStr
    published_at: Optional[str] = None
    idempotency_key: NonEmptyStr


class WorkflowInterventionRecord(TableObject, table=True):
    __tablename__: ClassVar[str] = "workflow_intervention"

    uuid: NonEmptyStr = Field(primary_key=True)
    create_time: NonEmptyStr
    update_time: NonEmptyStr
    deleted_at: Optional[str] = None
    description: Optional[str] = None
    meta_data: JsonObject = Field(
        default_factory=dict,
        sa_column=json_text_column("meta_data", default_json="{}"),
    )
    workflow_task_uuid: NonEmptyStr
    workflow_node_job_uuid: NonEmptyStr
    edge_agent_uuid: NonEmptyStr
    revision: int = Field(ge=1)
    status: NonEmptyStr
    options: JsonArray = Field(
        default_factory=list,
        sa_column=json_text_column("options", default_json="[]"),
    )
    resume_control_status: NonEmptyStr
    selected_option_id: Optional[str] = None
    selected_option: JsonObject = Field(
        default_factory=dict,
        sa_column=json_text_column("selected_option", default_json="{}"),
    )
    decision_idempotency_key: Optional[str] = None
    edge_command_uuid: Optional[str] = None
    opened_at: NonEmptyStr
    decided_at: Optional[str] = None


class WorkflowManualConfirmationRecord(TableObject, table=True):
    __tablename__: ClassVar[str] = "workflow_manual_confirmation"

    uuid: NonEmptyStr = Field(primary_key=True)
    create_time: NonEmptyStr
    update_time: NonEmptyStr
    deleted_at: Optional[str] = None
    description: Optional[str] = None
    meta_data: JsonObject = Field(
        default_factory=dict,
        sa_column=json_text_column("meta_data", default_json="{}"),
    )
    workflow_task_uuid: NonEmptyStr
    workflow_node_job_uuid: NonEmptyStr
    status: NonEmptyStr
    assignee_user_ids: JsonArray = Field(
        default_factory=list,
        sa_column=json_text_column("assignee_user_ids", default_json="[]"),
    )
    confirmed_by: Optional[str] = None
    comment: Optional[str] = None
    param: JsonObject = Field(
        default_factory=dict,
        sa_column=json_text_column("param", default_json="{}"),
    )
    decision_idempotency_key: Optional[str] = None
    opened_at: NonEmptyStr
    deadline_at: Optional[str] = None
    decided_at: Optional[str] = None


class WorkflowSourceRegistrationRecord(TableObject, table=True):
    __tablename__: ClassVar[str] = "workflow_source_registration"

    workflow_uuid: NonEmptyStr = Field(primary_key=True)
    package_id: NonEmptyStr
    package_root: NonEmptyStr
    relative_path: NonEmptyStr
    source_uri: NonEmptyStr
    create_time: NonEmptyStr
    update_time: NonEmptyStr


class WorkflowAuthoringRecord(TableObject, table=True):
    __tablename__: ClassVar[str] = "workflow_authoring"

    workflow_uuid: NonEmptyStr = Field(primary_key=True)
    observed_draft_hash: Optional[str] = None
    draft_update_time: Optional[str] = None
    diagnostics: JsonArray = Field(
        default_factory=list,
        sa_column=json_text_column("diagnostics", default_json="[]"),
    )
    candidate_hash: Optional[str] = None
    candidate: Optional[JsonObject] = Field(
        default=None,
        sa_column=json_text_column("candidate", default_json="{}", nullable=True),
    )
    applied_source: Optional[str] = None
    writeback_status: str = "settled"
    writeback_source: Optional[str] = None
    writeback_expected_hash: Optional[str] = None
    writeback_generation: Optional[str] = None
    update_time: NonEmptyStr


class WorkflowPublicationRecord(TableObject, table=True):
    """工作流的一次显式发布：冻结的不可变修订快照（工作流即节点的复用单位）。"""

    __tablename__: ClassVar[str] = "workflow_publication"

    uuid: NonEmptyStr = Field(primary_key=True)
    create_time: NonEmptyStr
    workflow_uuid: NonEmptyStr
    revision: int = Field(ge=1)
    name: NonEmptyStr
    source_hash: NonEmptyStr
    contract_digest: NonEmptyStr
    snapshot: JsonObject = Field(
        default_factory=dict,
        sa_column=json_text_column("snapshot", default_json="{}"),
    )


class FrontendEventRecord(TableObject, table=True):
    __tablename__: ClassVar[str] = "frontend_event"

    sequence: Optional[int] = Field(default=None, ge=1, primary_key=True)
    uuid: NonEmptyStr
    create_time: NonEmptyStr
    type: NonEmptyStr
    aggregate_uuid: NonEmptyStr
    payload: JsonObject = Field(
        default_factory=dict,
        sa_column=json_text_column("payload", default_json="{}"),
    )


WORKFLOW_TABLE_MODELS = (
    WorkflowRecord,
    WorkflowNodeTemplateRecord,
    WorkflowHandleTemplateRecord,
    WorkflowNodeRecord,
    WorkflowEdgeRecord,
    WorkflowTaskRecord,
    WorkflowNodeRunRecord,
    WorkflowNodeJobRecord,
    WorkflowTaskCommandRecord,
    ExecutionLockLeaseRecord,
    WorkflowNodeJobResultRecord,
    WorkflowNodeJobFeedbackHistoryRecord,
    WorkflowInterventionRecord,
    WorkflowManualConfirmationRecord,
    WorkflowSourceRegistrationRecord,
    WorkflowAuthoringRecord,
    WorkflowPublicationRecord,
    FrontendEventRecord,
)


_LEGACY_PROJECTION_VIEWS = (
    """
    CREATE VIEW IF NOT EXISTS workflow_runs AS
    SELECT
        task.uuid AS workflow_id,
        task.uuid AS task_id,
        COALESCE(json_extract(task.meta_data, '$.lab_id'), '') AS lab_id,
        COALESCE(json_extract(task.meta_data, '$.priority'), '') AS priority,
        (
            SELECT COUNT(*) FROM workflow_node_run AS run
            WHERE run.workflow_task_uuid = task.uuid
              AND run.deleted_at IS NULL
        ) AS node_count,
        CASE task.status WHEN 'succeeded' THEN 'success' ELSE task.status END AS state,
        (julianday(REPLACE(task.create_time, 'Z', '+00:00')) - 2440587.5)
            * 86400.0 AS submitted_at,
        CASE WHEN task.started_at IS NULL THEN NULL ELSE
            (julianday(REPLACE(task.started_at, 'Z', '+00:00')) - 2440587.5)
                * 86400.0 END AS started_at,
        CASE WHEN task.finished_at IS NULL THEN NULL ELSE
            (julianday(REPLACE(task.finished_at, 'Z', '+00:00')) - 2440587.5)
                * 86400.0 END AS finished_at,
        CASE WHEN task.finished_at IS NULL THEN NULL ELSE
            (julianday(REPLACE(task.finished_at, 'Z', '+00:00'))
             - julianday(REPLACE(task.create_time, 'Z', '+00:00'))) * 86400.0
            END AS duration_s,
        task.workflow_snapshot AS spec_json
    FROM workflow_task AS task
    WHERE task.deleted_at IS NULL
    """,
    """
    CREATE VIEW IF NOT EXISTS job_runs AS
    SELECT
        job.rowid AS id,
        job.uuid AS job_id,
        job.workflow_task_uuid AS workflow_id,
        job.workflow_node_uuid AS node_id,
        COALESCE(job.edge_agent_uuid, run.material_uuid, '') AS device_id,
        COALESCE(json_extract(job.param, '$.action'), '') AS action_name,
        CASE
            WHEN COALESCE(job.edge_agent_uuid, run.material_uuid, '') = '' THEN ''
            ELSE COALESCE(job.edge_agent_uuid, run.material_uuid, '') || ':' ||
                 COALESCE(json_extract(job.param, '$.action'), '')
        END AS device_action_key,
        COALESCE(
            (julianday(REPLACE(job.started_at, 'Z', '+00:00')) - 2440587.5)
                * 86400.0,
            (julianday(REPLACE(job.create_time, 'Z', '+00:00')) - 2440587.5)
                * 86400.0
        ) AS started_at,
        COALESCE(
            (julianday(REPLACE(job.finished_at, 'Z', '+00:00')) - 2440587.5)
                * 86400.0,
            (julianday(REPLACE(job.update_time, 'Z', '+00:00')) - 2440587.5)
                * 86400.0
        ) AS ended_at,
        CASE WHEN job.finished_at IS NULL OR job.started_at IS NULL THEN 0.0 ELSE
            (julianday(REPLACE(job.finished_at, 'Z', '+00:00'))
             - julianday(REPLACE(job.started_at, 'Z', '+00:00'))) * 86400.0
            END AS actual_s,
        0.0 AS estimated_s,
        'canonical_projection' AS estimate_source,
        CASE job.status WHEN 'succeeded' THEN 'success' ELSE job.status END AS state,
        CASE job.status WHEN 'skipped' THEN 'skip' ELSE 'normal' END AS suc_type,
        job.return_info AS ret_json
    FROM workflow_node_job AS job
    LEFT JOIN workflow_node_run AS run ON run.uuid = job.workflow_node_run_uuid
    WHERE job.deleted_at IS NULL
    """,
)


WORKFLOW_TABLES = (
    SCHEMA_IDENTITY_TABLE,
    TableSpec(
        "workflow",
        """
        CREATE TABLE IF NOT EXISTS workflow (
            uuid TEXT PRIMARY KEY,
            create_time TEXT NOT NULL,
            update_time TEXT NOT NULL,
            deleted_at TEXT,
            description TEXT,
            meta_data TEXT NOT NULL,
            name TEXT NOT NULL,
            tags TEXT NOT NULL,
            revision INTEGER NOT NULL DEFAULT 1
        )
        """,
        (
            """
            CREATE INDEX IF NOT EXISTS idx_workflow_created_active
            ON workflow(create_time DESC, uuid DESC) WHERE deleted_at IS NULL
            """,
        ),
    ),
    TableSpec(
        "workflow_node_template",
        """
        CREATE TABLE IF NOT EXISTS workflow_node_template (
            uuid TEXT PRIMARY KEY,
            create_time TEXT NOT NULL,
            update_time TEXT NOT NULL,
            deleted_at TEXT,
            description TEXT,
            meta_data TEXT NOT NULL,
            authority_id TEXT NOT NULL,
            resource_template_uuid TEXT NOT NULL,
            name TEXT NOT NULL,
            display_name TEXT NOT NULL,
            class TEXT,
            goal TEXT NOT NULL,
            goal_default TEXT NOT NULL,
            feedback TEXT NOT NULL,
            result TEXT NOT NULL,
            schema TEXT,
            type TEXT NOT NULL,
            icon TEXT,
            header TEXT,
            footer TEXT,
            node_type TEXT NOT NULL
        )
        """,
        (
            """
            CREATE INDEX IF NOT EXISTS ix_workflow_node_template_authority
            ON workflow_node_template(authority_id)
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_workflow_node_template_resource_name_active
            ON workflow_node_template(resource_template_uuid, LOWER(name))
            WHERE deleted_at IS NULL
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_workflow_node_template_type_active
            ON workflow_node_template(type, node_type) WHERE deleted_at IS NULL
            """,
        ),
    ),
    TableSpec(
        "workflow_handle_template",
        """
        CREATE TABLE IF NOT EXISTS workflow_handle_template (
            uuid TEXT PRIMARY KEY,
            create_time TEXT NOT NULL,
            update_time TEXT NOT NULL,
            deleted_at TEXT,
            description TEXT,
            meta_data TEXT NOT NULL,
            authority_id TEXT NOT NULL,
            workflow_node_template_uuid TEXT NOT NULL,
            handle_key TEXT NOT NULL,
            io_type TEXT NOT NULL,
            display_name TEXT NOT NULL,
            type TEXT NOT NULL,
            required INTEGER NOT NULL,
            data_source TEXT,
            data_key TEXT
        )
        """,
        (
            """
            CREATE INDEX IF NOT EXISTS ix_workflow_handle_template_node
            ON workflow_handle_template(workflow_node_template_uuid)
            """,
            """
            CREATE INDEX IF NOT EXISTS ix_workflow_handle_template_authority
            ON workflow_handle_template(authority_id)
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_workflow_handle_template_key_active
            ON workflow_handle_template(
                workflow_node_template_uuid, LOWER(handle_key), io_type
            ) WHERE deleted_at IS NULL
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_workflow_handle_template_node_active
            ON workflow_handle_template(
                workflow_node_template_uuid, create_time, uuid
            ) WHERE deleted_at IS NULL
            """,
        ),
    ),
    TableSpec(
        "workflow_node",
        """
        CREATE TABLE IF NOT EXISTS workflow_node (
            uuid TEXT PRIMARY KEY,
            create_time TEXT NOT NULL,
            update_time TEXT NOT NULL,
            deleted_at TEXT,
            description TEXT,
            meta_data TEXT NOT NULL,
            workflow_uuid TEXT NOT NULL,
            workflow_node_template_uuid TEXT,
            parent_uuid TEXT,
            material_uuid TEXT,
            name TEXT NOT NULL,
            status TEXT NOT NULL,
            type TEXT NOT NULL,
            icon TEXT,
            pose TEXT NOT NULL,
            param TEXT NOT NULL,
            footer TEXT,
            action_name TEXT,
            action_type TEXT,
            execution_policy TEXT NOT NULL,
            disabled INTEGER NOT NULL,
            minimized INTEGER NOT NULL,
            script TEXT,
            FOREIGN KEY(workflow_uuid) REFERENCES workflow(uuid)
        )
        """,
        (
            """
            CREATE INDEX IF NOT EXISTS ix_workflow_node_workflow
            ON workflow_node(workflow_uuid)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_workflow_node_workflow_active
            ON workflow_node(workflow_uuid, create_time, uuid)
            WHERE deleted_at IS NULL
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_workflow_node_template_active
            ON workflow_node(workflow_node_template_uuid)
            WHERE deleted_at IS NULL AND workflow_node_template_uuid IS NOT NULL
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_workflow_node_parent_active
            ON workflow_node(parent_uuid)
            WHERE deleted_at IS NULL AND parent_uuid IS NOT NULL
            """,
        ),
    ),
    TableSpec(
        "workflow_edge",
        """
        CREATE TABLE IF NOT EXISTS workflow_edge (
            uuid TEXT PRIMARY KEY,
            create_time TEXT NOT NULL,
            update_time TEXT NOT NULL,
            deleted_at TEXT,
            description TEXT,
            meta_data TEXT NOT NULL,
            workflow_uuid TEXT NOT NULL,
            source_node_uuid TEXT NOT NULL,
            target_node_uuid TEXT NOT NULL,
            source_handle_uuid TEXT NOT NULL,
            target_handle_uuid TEXT NOT NULL,
            FOREIGN KEY(workflow_uuid) REFERENCES workflow(uuid)
        )
        """,
        (
            """
            CREATE INDEX IF NOT EXISTS ix_workflow_edge_workflow
            ON workflow_edge(workflow_uuid)
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_workflow_edge_exact_active
            ON workflow_edge(
                source_node_uuid, source_handle_uuid, target_node_uuid,
                target_handle_uuid
            ) WHERE deleted_at IS NULL
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_workflow_edge_target_handle_active
            ON workflow_edge(target_node_uuid, target_handle_uuid)
            WHERE deleted_at IS NULL
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_workflow_edge_source_active
            ON workflow_edge(source_node_uuid) WHERE deleted_at IS NULL
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_workflow_edge_target_active
            ON workflow_edge(target_node_uuid) WHERE deleted_at IS NULL
            """,
        ),
    ),
    TableSpec(
        "workflow_task",
        """
        CREATE TABLE IF NOT EXISTS workflow_task (
            uuid TEXT PRIMARY KEY,
            create_time TEXT NOT NULL,
            update_time TEXT NOT NULL,
            deleted_at TEXT,
            description TEXT,
            meta_data TEXT NOT NULL CHECK (json_valid(meta_data) AND json_type(meta_data) = 'object'),
            workflow_uuid TEXT,
            status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN (
                'pending', 'running', 'canceling', 'succeeded', 'failed',
                'canceled', 'timeout'
            )),
            workflow_snapshot TEXT NOT NULL CHECK (
                json_valid(workflow_snapshot) AND json_type(workflow_snapshot) = 'object'
            ),
            execution_plan TEXT NOT NULL CHECK (
                json_valid(execution_plan) AND json_type(execution_plan) = 'object'
            ),
            run_mode TEXT NOT NULL DEFAULT 'normal' CHECK (
                run_mode IN ('normal', 'step', 'single_node')
            ),
            target_node_uuid TEXT,
            control_status TEXT NOT NULL DEFAULT 'active' CHECK (control_status IN (
                'active', 'paused', 'waiting_intervention', 'waiting_reconciliation'
            )),
            cleanup_status TEXT NOT NULL DEFAULT 'none' CHECK (cleanup_status IN (
                'none', 'pending', 'canceling', 'settled', 'requires_attention'
            )),
            trace_context TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(trace_context) AND json_type(trace_context) = 'object'
            ),
            input TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(input) AND json_type(input) = 'object'
            ),
            output TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(output) AND json_type(output) = 'object'
            ),
            error_info TEXT NOT NULL DEFAULT '[]' CHECK (
                json_valid(error_info) AND json_type(error_info) = 'array'
            ),
            timeout_at TEXT,
            attention_reason TEXT,
            terminal_ghost_detected_at TEXT,
            reconciliation_resume_control_status TEXT,
            started_at TEXT,
            finished_at TEXT,
            execution_kind TEXT NOT NULL DEFAULT 'workflow' CHECK (
                execution_kind IN ('workflow', 'ad_hoc_device_action')
            ),
            idempotency_key TEXT,
            request_fingerprint TEXT NOT NULL DEFAULT '',
            priority TEXT NOT NULL DEFAULT 'normal' CHECK (priority IN ('normal', 'high')),
            inventory_bindings TEXT NOT NULL DEFAULT '[]' CHECK (
                json_valid(inventory_bindings) AND json_type(inventory_bindings) = 'array'
            ),
            dispatch_gate TEXT NOT NULL DEFAULT 'active' CHECK (dispatch_gate IN ('active', 'paused')),
            FOREIGN KEY(workflow_uuid) REFERENCES workflow(uuid),
            CHECK (
                (
                    execution_kind = 'workflow'
                    AND workflow_uuid IS NOT NULL
                    AND (
                        (idempotency_key IS NULL AND request_fingerprint = '')
                        OR (idempotency_key IS NOT NULL AND request_fingerprint <> '')
                    )
                )
                OR
                (
                    execution_kind = 'ad_hoc_device_action'
                    AND workflow_uuid IS NULL
                    AND idempotency_key IS NOT NULL
                    AND request_fingerprint <> ''
                )
            )
        )
        """,
        (
            """
            CREATE INDEX IF NOT EXISTS idx_workflow_task_workflow_created
            ON workflow_task(workflow_uuid, create_time DESC, uuid DESC)
            WHERE deleted_at IS NULL
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_workflow_task_status_created
            ON workflow_task(status, create_time DESC, uuid DESC)
            WHERE deleted_at IS NULL
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_workflow_task_control_status_active
            ON workflow_task(control_status, create_time, uuid)
            WHERE deleted_at IS NULL
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_workflow_task_cleanup_status_active
            ON workflow_task(cleanup_status, create_time, uuid)
            WHERE deleted_at IS NULL
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_workflow_task_timeout_active
            ON workflow_task(timeout_at, uuid)
            WHERE deleted_at IS NULL AND timeout_at IS NOT NULL
              AND status IN ('pending', 'running', 'canceling')
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_workflow_task_requires_attention
            ON workflow_task(update_time, uuid)
            WHERE deleted_at IS NULL AND cleanup_status = 'requires_attention'
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_workflow_task_execution_kind_created
            ON workflow_task(execution_kind, create_time DESC, uuid DESC)
            WHERE deleted_at IS NULL
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_workflow_task_execution_idempotency
            ON workflow_task(execution_kind, idempotency_key)
            WHERE deleted_at IS NULL AND idempotency_key IS NOT NULL
            """,
        ),
    ),
    TableSpec(
        "workflow_node_run",
        """
        CREATE TABLE IF NOT EXISTS workflow_node_run (
            uuid TEXT PRIMARY KEY,
            create_time TEXT NOT NULL,
            update_time TEXT NOT NULL,
            deleted_at TEXT,
            description TEXT,
            meta_data TEXT NOT NULL CHECK (json_valid(meta_data) AND json_type(meta_data) = 'object'),
            workflow_task_uuid TEXT NOT NULL,
            workflow_node_uuid TEXT NOT NULL,
            topological_index INTEGER NOT NULL DEFAULT 0 CHECK (topological_index >= 0),
            executor_kind TEXT NOT NULL DEFAULT 'compute' CHECK (executor_kind IN (
                'device_action', 'compute', 'condition', 'script', 'tool_call',
                'manual_confirm', 'loop'
            )),
            execution_policy TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(execution_policy) AND json_type(execution_policy) = 'object'
            ),
            execution_timeout_seconds INTEGER NOT NULL DEFAULT 0 CHECK (
                execution_timeout_seconds >= 0
            ),
            param TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(param) AND json_type(param) = 'object'
            ),
            material_uuid TEXT,
            status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN (
                'pending', 'dispatched', 'running', 'intervention_required',
                'cancel_requested', 'execution_unknown', 'succeeded', 'failed',
                'skipped', 'canceled', 'timeout'
            )),
            current_job_uuid TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
            return_info TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(return_info) AND json_type(return_info) = 'object'
            ),
            error_info TEXT NOT NULL DEFAULT '[]' CHECK (
                json_valid(error_info) AND json_type(error_info) = 'array'
            ),
            feedback_data TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(feedback_data) AND json_type(feedback_data) = 'object'
            ),
            control_data TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(control_data) AND json_type(control_data) = 'object'
            ),
            started_at TEXT,
            finished_at TEXT,
            FOREIGN KEY(workflow_task_uuid) REFERENCES workflow_task(uuid)
        )
        """,
        (
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_workflow_node_run_task_node_active
            ON workflow_node_run(workflow_task_uuid, workflow_node_uuid)
            WHERE deleted_at IS NULL
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_workflow_node_run_task_topology_active
            ON workflow_node_run(workflow_task_uuid, topological_index, uuid)
            WHERE deleted_at IS NULL
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_workflow_node_run_local_recovery
            ON workflow_node_run(update_time, uuid)
            WHERE deleted_at IS NULL
              AND executor_kind IN (
                  'compute', 'condition', 'script', 'tool_call', 'manual_confirm', 'loop'
              )
              AND status IN ('dispatched', 'running')
            """,
        ),
    ),
    TableSpec(
        "workflow_node_job",
        """
        CREATE TABLE IF NOT EXISTS workflow_node_job (
            uuid TEXT PRIMARY KEY,
            create_time TEXT NOT NULL,
            update_time TEXT NOT NULL,
            deleted_at TEXT,
            description TEXT,
            meta_data TEXT NOT NULL CHECK (json_valid(meta_data) AND json_type(meta_data) = 'object'),
            workflow_node_run_uuid TEXT NOT NULL,
            workflow_task_uuid TEXT NOT NULL,
            workflow_node_uuid TEXT NOT NULL,
            attempt_no INTEGER NOT NULL DEFAULT 1 CHECK (attempt_no > 0),
            retry_of_job_uuid TEXT,
            trigger TEXT NOT NULL DEFAULT 'initial' CHECK (trigger IN (
                'initial', 'retry_decision', 'recovery', 'loop_iteration'
            )),
            edge_agent_uuid TEXT,
            edge_command_uuid TEXT,
            job_access_token_hash TEXT NOT NULL DEFAULT '',
            feedback_sequence INTEGER NOT NULL DEFAULT 0 CHECK (feedback_sequence >= 0),
            status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN (
                'pending', 'dispatched', 'running', 'intervention_required',
                'cancel_requested', 'execution_unknown', 'succeeded', 'failed',
                'skipped', 'canceled', 'timeout'
            )),
            param TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(param) AND json_type(param) = 'object'
            ),
            feedback_data TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(feedback_data) AND json_type(feedback_data) = 'object'
            ),
            return_info TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(return_info) AND json_type(return_info) = 'object'
            ),
            error_resolution TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(error_resolution) AND json_type(error_resolution) = 'object'
            ),
            control_data TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(control_data) AND json_type(control_data) = 'object'
            ),
            error_info TEXT NOT NULL DEFAULT '[]' CHECK (
                json_valid(error_info) AND json_type(error_info) = 'array'
            ),
            dispatch_deadline_at TEXT,
            execution_deadline_at TEXT,
            cancel_command_uuid TEXT,
            cancel_ack_deadline_at TEXT,
            cancel_complete_deadline_at TEXT,
            uncertainty_reason TEXT,
            started_at TEXT,
            finished_at TEXT,
            CHECK (retry_of_job_uuid IS NULL OR retry_of_job_uuid <> uuid),
            CHECK (
                (retry_of_job_uuid IS NULL AND attempt_no = 1)
                OR (retry_of_job_uuid IS NOT NULL AND attempt_no > 1)
                OR (trigger = 'loop_iteration' AND retry_of_job_uuid IS NULL AND attempt_no > 1)
            ),
            FOREIGN KEY(workflow_node_run_uuid) REFERENCES workflow_node_run(uuid),
            FOREIGN KEY(workflow_task_uuid) REFERENCES workflow_task(uuid)
        )
        """,
        (
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_workflow_node_job_attempt_active
            ON workflow_node_job(workflow_node_run_uuid, attempt_no)
            WHERE deleted_at IS NULL
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_workflow_node_job_task_created
            ON workflow_node_job(workflow_task_uuid, create_time, uuid)
            WHERE deleted_at IS NULL
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_workflow_node_job_retry_of
            ON workflow_node_job(retry_of_job_uuid)
            WHERE deleted_at IS NULL AND retry_of_job_uuid IS NOT NULL
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_workflow_node_job_edge_command_active
            ON workflow_node_job(edge_command_uuid)
            WHERE deleted_at IS NULL AND edge_command_uuid IS NOT NULL
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_workflow_node_job_dispatch_deadline
            ON workflow_node_job(dispatch_deadline_at, uuid)
            WHERE deleted_at IS NULL AND dispatch_deadline_at IS NOT NULL
              AND status = 'dispatched'
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_workflow_node_job_execution_deadline
            ON workflow_node_job(execution_deadline_at, uuid)
            WHERE deleted_at IS NULL AND execution_deadline_at IS NOT NULL
              AND status = 'running'
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_workflow_node_job_cancel_deadline
            ON workflow_node_job(
                cancel_ack_deadline_at, cancel_complete_deadline_at, uuid
            ) WHERE deleted_at IS NULL AND status = 'cancel_requested'
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_workflow_node_job_in_flight
            ON workflow_node_job(status)
            WHERE deleted_at IS NULL AND status IN (
                'dispatched', 'running', 'intervention_required',
                'cancel_requested', 'execution_unknown'
            )
            """,
        ),
    ),
    TableSpec(
        "workflow_task_command",
        """
        CREATE TABLE IF NOT EXISTS workflow_task_command (
            uuid TEXT PRIMARY KEY,
            create_time TEXT NOT NULL,
            update_time TEXT NOT NULL,
            deleted_at TEXT,
            description TEXT,
            meta_data TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(meta_data) AND json_type(meta_data) = 'object'
            ),
            workflow_task_uuid TEXT NOT NULL,
            type TEXT NOT NULL CHECK (type IN (
                'step', 'pause', 'resume', 'cancel', 'edit_input', 'switch_to_step', 'unlock_resources'
            )),
            target_node_uuid TEXT,
            idempotency_key TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('pending', 'succeeded', 'rejected')),
            result TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(result) AND json_type(result) = 'object'
            ),
            trace_context TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(trace_context) AND json_type(trace_context) = 'object'
            ),
            consumed_at TEXT,
            FOREIGN KEY(workflow_task_uuid) REFERENCES workflow_task(uuid) ON DELETE CASCADE
        )
        """,
        (
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_workflow_task_command_idempotency_active
            ON workflow_task_command(workflow_task_uuid, idempotency_key)
            WHERE deleted_at IS NULL
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_workflow_task_command_pending
            ON workflow_task_command(workflow_task_uuid, create_time, uuid)
            WHERE deleted_at IS NULL AND status = 'pending'
            """,
        ),
    ),
    TableSpec(
        "execution_lock_lease",
        """
        CREATE TABLE IF NOT EXISTS execution_lock_lease (
            uuid TEXT PRIMARY KEY,
            create_time TEXT NOT NULL,
            update_time TEXT NOT NULL,
            deleted_at TEXT,
            description TEXT,
            meta_data TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(meta_data) AND json_type(meta_data) = 'object'
            ),
            lock_key TEXT NOT NULL,
            material_uuid TEXT NOT NULL,
            workflow_task_uuid TEXT NOT NULL,
            workflow_node_job_uuid TEXT NOT NULL,
            state TEXT NOT NULL CHECK (
                state IN ('reserved', 'running', 'released', 'uncertain')
            ),
            acquired_at TEXT NOT NULL,
            released_at TEXT,
            FOREIGN KEY(workflow_task_uuid) REFERENCES workflow_task(uuid),
            FOREIGN KEY(workflow_node_job_uuid) REFERENCES workflow_node_job(uuid)
        )
        """,
        (
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_execution_lock_active_key
            ON execution_lock_lease(lock_key)
            WHERE deleted_at IS NULL AND state IN ('reserved', 'running', 'uncertain')
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_execution_lock_active_job
            ON execution_lock_lease(workflow_node_job_uuid, lock_key)
            WHERE deleted_at IS NULL AND state IN ('reserved', 'running', 'uncertain')
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_execution_lock_material_state
            ON execution_lock_lease(material_uuid, state, create_time)
            WHERE deleted_at IS NULL
            """,
        ),
    ),
    TableSpec(
        "workflow_node_job_result",
        """
        CREATE TABLE IF NOT EXISTS workflow_node_job_result (
            uuid TEXT PRIMARY KEY,
            create_time TEXT NOT NULL,
            update_time TEXT NOT NULL,
            deleted_at TEXT,
            description TEXT,
            meta_data TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(meta_data) AND json_type(meta_data) = 'object'
            ),
            workflow_node_job_uuid TEXT NOT NULL,
            edge_command_uuid TEXT NOT NULL,
            job_access_token_hash TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            outcome TEXT NOT NULL CHECK (
                outcome IN ('succeeded', 'failed', 'canceled', 'timeout')
            ),
            return_info TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(return_info) AND json_type(return_info) = 'object'
            ),
            error_info TEXT NOT NULL DEFAULT '[]' CHECK (
                json_valid(error_info) AND json_type(error_info) = 'array'
            ),
            committed_at TEXT NOT NULL,
            consumed_at TEXT,
            FOREIGN KEY(workflow_node_job_uuid) REFERENCES workflow_node_job(uuid)
        )
        """,
        (
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_workflow_node_job_result_job
            ON workflow_node_job_result(workflow_node_job_uuid)
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_workflow_node_job_result_idempotency
            ON workflow_node_job_result(workflow_node_job_uuid, idempotency_key)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_workflow_node_job_result_unconsumed
            ON workflow_node_job_result(committed_at, uuid)
            WHERE deleted_at IS NULL AND consumed_at IS NULL
            """,
        ),
    ),
    TableSpec(
        "workflow_node_job_feedback_history",
        """
        CREATE TABLE IF NOT EXISTS workflow_node_job_feedback_history (
            uuid TEXT PRIMARY KEY,
            create_time TEXT NOT NULL,
            update_time TEXT NOT NULL,
            deleted_at TEXT,
            description TEXT,
            meta_data TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(meta_data) AND json_type(meta_data) = 'object'
            ),
            workflow_node_job_uuid TEXT NOT NULL,
            sequence INTEGER NOT NULL CHECK (sequence > 0),
            feedback_type TEXT NOT NULL,
            data TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(data) AND json_type(data) = 'object'
            ),
            observed_at TEXT NOT NULL,
            received_at TEXT NOT NULL,
            published_at TEXT,
            idempotency_key TEXT NOT NULL,
            FOREIGN KEY(workflow_node_job_uuid) REFERENCES workflow_node_job(uuid)
        )
        """,
        (
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_workflow_node_job_feedback_sequence
            ON workflow_node_job_feedback_history(workflow_node_job_uuid, sequence)
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_workflow_node_job_feedback_idempotency
            ON workflow_node_job_feedback_history(workflow_node_job_uuid, idempotency_key)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_workflow_node_job_feedback_timeline
            ON workflow_node_job_feedback_history(
                workflow_node_job_uuid, observed_at DESC, uuid DESC
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_workflow_node_job_feedback_retention
            ON workflow_node_job_feedback_history(received_at, uuid)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_workflow_node_job_feedback_unpublished
            ON workflow_node_job_feedback_history(workflow_node_job_uuid, sequence)
            WHERE published_at IS NULL
            """,
        ),
    ),
    TableSpec(
        "workflow_intervention",
        """
        CREATE TABLE IF NOT EXISTS workflow_intervention (
            uuid TEXT PRIMARY KEY,
            create_time TEXT NOT NULL,
            update_time TEXT NOT NULL,
            deleted_at TEXT,
            description TEXT,
            meta_data TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(meta_data) AND json_type(meta_data) = 'object'
            ),
            workflow_task_uuid TEXT NOT NULL,
            workflow_node_job_uuid TEXT NOT NULL,
            edge_agent_uuid TEXT NOT NULL,
            revision INTEGER NOT NULL CHECK (revision > 0),
            status TEXT NOT NULL CHECK (status IN ('open', 'selected', 'superseded')),
            options TEXT NOT NULL CHECK (json_valid(options) AND json_type(options) = 'array'),
            resume_control_status TEXT NOT NULL CHECK (
                resume_control_status IN ('active', 'paused')
            ),
            selected_option_id TEXT,
            selected_option TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(selected_option) AND json_type(selected_option) = 'object'
            ),
            decision_idempotency_key TEXT,
            edge_command_uuid TEXT,
            opened_at TEXT NOT NULL,
            decided_at TEXT,
            FOREIGN KEY(workflow_task_uuid) REFERENCES workflow_task(uuid),
            FOREIGN KEY(workflow_node_job_uuid) REFERENCES workflow_node_job(uuid),
            CHECK (
                (
                    status = 'open' AND selected_option_id IS NULL
                    AND decision_idempotency_key IS NULL AND edge_command_uuid IS NULL
                    AND decided_at IS NULL
                )
                OR
                (
                    status = 'selected' AND selected_option_id IS NOT NULL
                    AND decision_idempotency_key IS NOT NULL
                    AND edge_command_uuid IS NOT NULL AND decided_at IS NOT NULL
                )
                OR status = 'superseded'
            )
        )
        """,
        (
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_workflow_intervention_job_revision
            ON workflow_intervention(workflow_node_job_uuid, revision)
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_workflow_intervention_job_open
            ON workflow_intervention(workflow_node_job_uuid)
            WHERE deleted_at IS NULL AND status = 'open'
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_workflow_intervention_status_opened
            ON workflow_intervention(status, opened_at, uuid) WHERE deleted_at IS NULL
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_workflow_intervention_task
            ON workflow_intervention(workflow_task_uuid, opened_at, uuid)
            WHERE deleted_at IS NULL
            """,
        ),
    ),
    TableSpec(
        "workflow_manual_confirmation",
        """
        CREATE TABLE IF NOT EXISTS workflow_manual_confirmation (
            uuid TEXT PRIMARY KEY,
            create_time TEXT NOT NULL,
            update_time TEXT NOT NULL,
            deleted_at TEXT,
            description TEXT,
            meta_data TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(meta_data) AND json_type(meta_data) = 'object'
            ),
            workflow_task_uuid TEXT NOT NULL,
            workflow_node_job_uuid TEXT NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN ('pending', 'approved', 'rejected', 'timed_out', 'canceled')
            ),
            assignee_user_ids TEXT NOT NULL DEFAULT '[]' CHECK (
                json_valid(assignee_user_ids) AND json_type(assignee_user_ids) = 'array'
            ),
            confirmed_by TEXT,
            comment TEXT,
            param TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(param) AND json_type(param) = 'object'
            ),
            decision_idempotency_key TEXT,
            opened_at TEXT NOT NULL,
            deadline_at TEXT,
            decided_at TEXT,
            FOREIGN KEY(workflow_task_uuid) REFERENCES workflow_task(uuid),
            FOREIGN KEY(workflow_node_job_uuid) REFERENCES workflow_node_job(uuid)
        )
        """,
        (
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_manual_confirmation_job
            ON workflow_manual_confirmation(workflow_node_job_uuid)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_manual_confirmation_decision_idempotency
            ON workflow_manual_confirmation(decision_idempotency_key)
            WHERE decision_idempotency_key IS NOT NULL
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_manual_confirmation_pending_deadline
            ON workflow_manual_confirmation(deadline_at, uuid)
            WHERE deleted_at IS NULL AND status = 'pending'
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_manual_confirmation_task
            ON workflow_manual_confirmation(workflow_task_uuid, create_time, uuid)
            WHERE deleted_at IS NULL
            """,
        ),
    ),
    TableSpec(
        "workflow_source_registration",
        """
        CREATE TABLE IF NOT EXISTS workflow_source_registration (
            workflow_uuid TEXT PRIMARY KEY,
            package_id TEXT NOT NULL,
            package_root TEXT NOT NULL,
            relative_path TEXT NOT NULL,
            source_uri TEXT NOT NULL,
            create_time TEXT NOT NULL,
            update_time TEXT NOT NULL,
            FOREIGN KEY(workflow_uuid) REFERENCES workflow(uuid)
        )
        """,
        (
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_workflow_source_registration_path
            ON workflow_source_registration(package_root, relative_path)
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_workflow_source_registration_uri
            ON workflow_source_registration(source_uri)
            """,
        ),
    ),
    TableSpec(
        "workflow_authoring",
        """
        CREATE TABLE IF NOT EXISTS workflow_authoring (
            workflow_uuid TEXT PRIMARY KEY,
            observed_draft_hash TEXT,
            draft_update_time TEXT,
            diagnostics TEXT NOT NULL,
            candidate_hash TEXT,
            candidate TEXT,
            applied_source TEXT,
            writeback_status TEXT NOT NULL DEFAULT 'settled',
            writeback_source TEXT,
            writeback_expected_hash TEXT,
            writeback_generation TEXT,
            update_time TEXT NOT NULL,
            FOREIGN KEY(workflow_uuid) REFERENCES workflow(uuid)
        )
        """,
    ),
    TableSpec(
        "workflow_publication",
        """
        CREATE TABLE IF NOT EXISTS workflow_publication (
            uuid TEXT PRIMARY KEY,
            create_time TEXT NOT NULL,
            workflow_uuid TEXT NOT NULL,
            revision INTEGER NOT NULL CHECK (revision >= 1),
            name TEXT NOT NULL,
            source_hash TEXT NOT NULL,
            contract_digest TEXT NOT NULL,
            snapshot TEXT NOT NULL CHECK (
                json_valid(snapshot) AND json_type(snapshot) = 'object'
            ),
            FOREIGN KEY(workflow_uuid) REFERENCES workflow(uuid),
            UNIQUE(workflow_uuid, revision)
        )
        """,
        (
            """
            CREATE INDEX IF NOT EXISTS idx_workflow_publication_created
            ON workflow_publication(create_time DESC, uuid)
            """,
        ),
    ),
    TableSpec(
        "frontend_event",
        """
        CREATE TABLE IF NOT EXISTS frontend_event (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            uuid TEXT NOT NULL UNIQUE,
            create_time TEXT NOT NULL,
            type TEXT NOT NULL,
            aggregate_uuid TEXT NOT NULL,
            payload TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(payload) AND json_type(payload) = 'object'
            )
        )
        """,
        (
            """
            CREATE INDEX IF NOT EXISTS idx_frontend_event_type_sequence
            ON frontend_event(type, sequence)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_frontend_event_feedback_retention
            ON frontend_event(create_time, sequence) WHERE type = 'job.feedback'
            """,
            *_LEGACY_PROJECTION_VIEWS,
        ),
    ),
)


__all__ = [
    "ExecutionLockLeaseRecord",
    "FrontendEventRecord",
    "WORKFLOW_TABLES",
    "WORKFLOW_TABLE_MODELS",
    "WorkflowAuthoringRecord",
    "WorkflowEdgeRecord",
    "WorkflowHandleTemplateRecord",
    "WorkflowInterventionRecord",
    "WorkflowManualConfirmationRecord",
    "WorkflowNodeJobFeedbackHistoryRecord",
    "WorkflowNodeJobRecord",
    "WorkflowNodeJobResultRecord",
    "WorkflowNodeRecord",
    "WorkflowNodeRunRecord",
    "WorkflowNodeTemplateRecord",
    "WorkflowPublicationRecord",
    "WorkflowRecord",
    "WorkflowSourceRegistrationRecord",
    "WorkflowTaskCommandRecord",
    "WorkflowTaskRecord",
]
