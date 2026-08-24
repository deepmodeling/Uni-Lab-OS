"""Workflow SQLite 的版本化 Backend 契约适配。"""

from __future__ import annotations

import sqlite3


WORKFLOW_SCHEMA_VERSION = 1


class WorkflowSchemaError(RuntimeError):
    """The Workflow Authority database is newer or structurally unsupported."""


_WORKFLOW_TASK_SCHEMA = """
CREATE TABLE workflow_task (
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
    FOREIGN KEY(workflow_uuid) REFERENCES workflow(uuid),
    CHECK (
        (
            execution_kind = 'workflow'
            AND workflow_uuid IS NOT NULL
            AND idempotency_key IS NULL
            AND request_fingerprint = ''
        )
        OR
        (
            execution_kind = 'ad_hoc_device_action'
            AND workflow_uuid IS NULL
            AND idempotency_key IS NOT NULL
            AND request_fingerprint <> ''
        )
    )
);
"""


_WORKFLOW_NODE_JOB_SCHEMA = """
CREATE TABLE workflow_node_job (
    uuid TEXT PRIMARY KEY,
    create_time TEXT NOT NULL,
    update_time TEXT NOT NULL,
    deleted_at TEXT,
    description TEXT,
    meta_data TEXT NOT NULL CHECK (json_valid(meta_data) AND json_type(meta_data) = 'object'),
    workflow_task_uuid TEXT NOT NULL,
    workflow_node_uuid TEXT NOT NULL,
    material_uuid TEXT,
    edge_agent_uuid TEXT,
    edge_command_uuid TEXT,
    job_access_token_hash TEXT NOT NULL DEFAULT '',
    feedback_sequence INTEGER NOT NULL DEFAULT 0 CHECK (feedback_sequence >= 0),
    topological_index INTEGER NOT NULL DEFAULT 0 CHECK (topological_index >= 0),
    executor_kind TEXT NOT NULL DEFAULT 'compute' CHECK (executor_kind IN (
        'device_action', 'compute', 'condition', 'script', 'tool_call',
        'manual_confirm'
    )),
    execution_policy TEXT NOT NULL DEFAULT '{}' CHECK (
        json_valid(execution_policy) AND json_type(execution_policy) = 'object'
    ),
    execution_timeout_seconds INTEGER NOT NULL DEFAULT 0 CHECK (
        execution_timeout_seconds >= 0
    ),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN (
        'pending', 'dispatched', 'running', 'intervention_required',
        'cancel_requested', 'execution_unknown', 'succeeded', 'failed',
        'skipped', 'canceled', 'timeout'
    )),
    attempt INTEGER NOT NULL DEFAULT 1 CHECK (attempt > 0),
    param TEXT NOT NULL DEFAULT '{}' CHECK (
        json_valid(param) AND json_type(param) = 'object'
    ),
    feedback_data TEXT NOT NULL DEFAULT '{}' CHECK (
        json_valid(feedback_data) AND json_type(feedback_data) = 'object'
    ),
    return_info TEXT NOT NULL DEFAULT '{}' CHECK (
        json_valid(return_info) AND json_type(return_info) = 'object'
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
    FOREIGN KEY(workflow_task_uuid) REFERENCES workflow_task(uuid)
);
"""


_RUNTIME_FACT_SCHEMA = """
CREATE INDEX IF NOT EXISTS idx_workflow_created_active
    ON workflow(create_time DESC, uuid DESC) WHERE deleted_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_workflow_node_template_resource_name_active
    ON workflow_node_template(resource_template_uuid, LOWER(name))
    WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_workflow_node_template_type_active
    ON workflow_node_template(type, node_type) WHERE deleted_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_workflow_handle_template_key_active
    ON workflow_handle_template(
        workflow_node_template_uuid, LOWER(handle_key), io_type
    ) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_workflow_handle_template_node_active
    ON workflow_handle_template(
        workflow_node_template_uuid, create_time, uuid
    ) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_workflow_node_workflow_active
    ON workflow_node(workflow_uuid, create_time, uuid)
    WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_workflow_node_template_active
    ON workflow_node(workflow_node_template_uuid)
    WHERE deleted_at IS NULL AND workflow_node_template_uuid IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_workflow_node_parent_active
    ON workflow_node(parent_uuid)
    WHERE deleted_at IS NULL AND parent_uuid IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_workflow_edge_exact_active
    ON workflow_edge(
        source_node_uuid, source_handle_uuid, target_node_uuid,
        target_handle_uuid
    ) WHERE deleted_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_workflow_edge_target_handle_active
    ON workflow_edge(target_node_uuid, target_handle_uuid)
    WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_workflow_edge_source_active
    ON workflow_edge(source_node_uuid) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_workflow_edge_target_active
    ON workflow_edge(target_node_uuid) WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_workflow_task_workflow_created
    ON workflow_task(workflow_uuid, create_time DESC, uuid DESC)
    WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_workflow_task_status_created
    ON workflow_task(status, create_time DESC, uuid DESC)
    WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_workflow_task_control_status_active
    ON workflow_task(control_status, create_time, uuid)
    WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_workflow_task_cleanup_status_active
    ON workflow_task(cleanup_status, create_time, uuid)
    WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_workflow_task_timeout_active
    ON workflow_task(timeout_at, uuid)
    WHERE deleted_at IS NULL AND timeout_at IS NOT NULL
      AND status IN ('pending', 'running', 'canceling');
CREATE INDEX IF NOT EXISTS idx_workflow_task_requires_attention
    ON workflow_task(update_time, uuid)
    WHERE deleted_at IS NULL AND cleanup_status = 'requires_attention';
CREATE INDEX IF NOT EXISTS idx_workflow_task_execution_kind_created
    ON workflow_task(execution_kind, create_time DESC, uuid DESC)
    WHERE deleted_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_workflow_task_execution_idempotency
    ON workflow_task(execution_kind, idempotency_key)
    WHERE deleted_at IS NULL AND idempotency_key IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS ux_workflow_node_job_attempt_active
    ON workflow_node_job(workflow_task_uuid, workflow_node_uuid, attempt)
    WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_workflow_node_job_task_created
    ON workflow_node_job(workflow_task_uuid, create_time, uuid)
    WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_workflow_node_job_task_topology_active
    ON workflow_node_job(workflow_task_uuid, topological_index, uuid)
    WHERE deleted_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_workflow_node_job_edge_command_active
    ON workflow_node_job(edge_command_uuid)
    WHERE deleted_at IS NULL AND edge_command_uuid IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_workflow_node_job_dispatch_deadline
    ON workflow_node_job(dispatch_deadline_at, uuid)
    WHERE deleted_at IS NULL AND dispatch_deadline_at IS NOT NULL
      AND status = 'dispatched';
CREATE INDEX IF NOT EXISTS idx_workflow_node_job_execution_deadline
    ON workflow_node_job(execution_deadline_at, uuid)
    WHERE deleted_at IS NULL AND execution_deadline_at IS NOT NULL
      AND status = 'running';
CREATE INDEX IF NOT EXISTS idx_workflow_node_job_cancel_deadline
    ON workflow_node_job(
        cancel_ack_deadline_at, cancel_complete_deadline_at, uuid
    ) WHERE deleted_at IS NULL AND status = 'cancel_requested';
CREATE INDEX IF NOT EXISTS idx_workflow_node_job_local_recovery
    ON workflow_node_job(update_time, uuid)
    WHERE deleted_at IS NULL
      AND executor_kind IN (
          'compute', 'condition', 'script', 'tool_call', 'manual_confirm'
      )
      AND status IN ('dispatched', 'running');
CREATE INDEX IF NOT EXISTS idx_workflow_node_job_in_flight
    ON workflow_node_job(status)
    WHERE deleted_at IS NULL AND status IN (
        'dispatched', 'running', 'intervention_required',
        'cancel_requested', 'execution_unknown'
    );

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
    type TEXT NOT NULL CHECK (type IN ('step', 'pause', 'resume', 'cancel')),
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
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_workflow_task_command_idempotency_active
    ON workflow_task_command(workflow_task_uuid, idempotency_key)
    WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_workflow_task_command_pending
    ON workflow_task_command(workflow_task_uuid, create_time, uuid)
    WHERE deleted_at IS NULL AND status = 'pending';

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
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_execution_lock_active_key
    ON execution_lock_lease(lock_key)
    WHERE deleted_at IS NULL AND state IN ('reserved', 'running', 'uncertain');
CREATE UNIQUE INDEX IF NOT EXISTS ux_execution_lock_active_job
    ON execution_lock_lease(workflow_node_job_uuid, lock_key)
    WHERE deleted_at IS NULL AND state IN ('reserved', 'running', 'uncertain');
CREATE INDEX IF NOT EXISTS idx_execution_lock_material_state
    ON execution_lock_lease(material_uuid, state, create_time)
    WHERE deleted_at IS NULL;

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
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_workflow_node_job_result_job
    ON workflow_node_job_result(workflow_node_job_uuid);
CREATE UNIQUE INDEX IF NOT EXISTS ux_workflow_node_job_result_idempotency
    ON workflow_node_job_result(workflow_node_job_uuid, idempotency_key);
CREATE INDEX IF NOT EXISTS idx_workflow_node_job_result_unconsumed
    ON workflow_node_job_result(committed_at, uuid)
    WHERE deleted_at IS NULL AND consumed_at IS NULL;

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
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_workflow_node_job_feedback_sequence
    ON workflow_node_job_feedback_history(workflow_node_job_uuid, sequence);
CREATE UNIQUE INDEX IF NOT EXISTS ux_workflow_node_job_feedback_idempotency
    ON workflow_node_job_feedback_history(workflow_node_job_uuid, idempotency_key);
CREATE INDEX IF NOT EXISTS idx_workflow_node_job_feedback_timeline
    ON workflow_node_job_feedback_history(
        workflow_node_job_uuid, observed_at DESC, uuid DESC
    );
CREATE INDEX IF NOT EXISTS idx_workflow_node_job_feedback_retention
    ON workflow_node_job_feedback_history(received_at, uuid);
CREATE INDEX IF NOT EXISTS idx_workflow_node_job_feedback_unpublished
    ON workflow_node_job_feedback_history(workflow_node_job_uuid, sequence)
    WHERE published_at IS NULL;

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
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_workflow_intervention_job_revision
    ON workflow_intervention(workflow_node_job_uuid, revision);
CREATE UNIQUE INDEX IF NOT EXISTS ux_workflow_intervention_job_open
    ON workflow_intervention(workflow_node_job_uuid)
    WHERE deleted_at IS NULL AND status = 'open';
CREATE INDEX IF NOT EXISTS idx_workflow_intervention_status_opened
    ON workflow_intervention(status, opened_at, uuid) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_workflow_intervention_task
    ON workflow_intervention(workflow_task_uuid, opened_at, uuid)
    WHERE deleted_at IS NULL;

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
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_manual_confirmation_job
    ON workflow_manual_confirmation(workflow_node_job_uuid);
CREATE INDEX IF NOT EXISTS idx_manual_confirmation_decision_idempotency
    ON workflow_manual_confirmation(decision_idempotency_key)
    WHERE decision_idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_manual_confirmation_pending_deadline
    ON workflow_manual_confirmation(deadline_at, uuid)
    WHERE deleted_at IS NULL AND status = 'pending';
CREATE INDEX IF NOT EXISTS idx_manual_confirmation_task
    ON workflow_manual_confirmation(workflow_task_uuid, create_time, uuid)
    WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_frontend_event_type_sequence
    ON frontend_event(type, sequence);
CREATE INDEX IF NOT EXISTS idx_frontend_event_feedback_retention
    ON frontend_event(create_time, sequence) WHERE type = 'job.feedback';
"""


def _execute_script(conn: sqlite3.Connection, script: str) -> None:
    """在调用者事务中逐条执行 SQL，避免 ``executescript`` 隐式提交。"""

    pending: list[str] = []
    for line in script.splitlines():
        pending.append(line)
        statement = "\n".join(pending).strip()
        if statement and sqlite3.complete_statement(statement):
            conn.execute(statement)
            pending.clear()
    if "\n".join(pending).strip():
        raise RuntimeError("workflow schema SQL 存在不完整语句")


def _columns(conn: sqlite3.Connection, table: str) -> dict[str, sqlite3.Row]:
    return {
        str(row["name"]): row
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }


def _object_type(conn: sqlite3.Connection, name: str) -> str | None:
    row = conn.execute(
        "SELECT type FROM sqlite_master WHERE name = ? AND type IN ('table', 'view')",
        (name,),
    ).fetchone()
    return None if row is None else str(row["type"])


def _retire_legacy_audit_tables(conn: sqlite3.Connection) -> None:
    """保留旧审计事实，并把旧对象名切成规范表的只读投影。"""

    legacy_tables = {
        "workflow_runs": (
            "workflow_runs_legacy_archive",
            {
                "workflow_id",
                "task_id",
                "lab_id",
                "priority",
                "node_count",
                "state",
                "submitted_at",
                "finished_at",
                "duration_s",
                "spec_json",
            },
        ),
        "job_runs": (
            "job_runs_legacy_archive",
            {
                "id",
                "job_id",
                "workflow_id",
                "node_id",
                "device_id",
                "action_name",
                "device_action_key",
                "started_at",
                "ended_at",
                "actual_s",
                "estimated_s",
                "estimate_source",
                "state",
                "suc_type",
                "ret_json",
            },
        ),
    }
    for public_name, (archive_name, required_columns) in legacy_tables.items():
        public_type = _object_type(conn, public_name)
        archive_type = _object_type(conn, archive_name)
        if archive_type not in (None, "table"):
            raise WorkflowSchemaError(
                f"legacy audit archive {archive_name} has unsupported type {archive_type}"
            )
        if public_type == "table":
            if archive_type is not None:
                raise WorkflowSchemaError(
                    f"both {public_name} and {archive_name} contain legacy audit facts"
                )
            columns = set(_columns(conn, public_name))
            missing = required_columns - columns
            if missing:
                raise WorkflowSchemaError(
                    f"legacy audit table {public_name} is missing columns: {sorted(missing)}"
                )
            if public_name == "workflow_runs" and "started_at" not in columns:
                conn.execute("ALTER TABLE workflow_runs ADD COLUMN started_at REAL")
            conn.execute(f"ALTER TABLE {public_name} RENAME TO {archive_name}")
        elif public_type == "view":
            conn.execute(f"DROP VIEW {public_name}")
        elif public_type is not None:
            raise WorkflowSchemaError(
                f"legacy audit object {public_name} has unsupported type {public_type}"
            )

    workflow_archive_union = ""
    if _object_type(conn, "workflow_runs_legacy_archive") == "table":
        workflow_archive_union = """
        UNION ALL
        SELECT
            legacy.workflow_id, legacy.task_id, legacy.lab_id, legacy.priority,
            legacy.node_count, legacy.state, legacy.submitted_at,
            legacy.started_at, legacy.finished_at, legacy.duration_s,
            legacy.spec_json
        FROM workflow_runs_legacy_archive AS legacy
        WHERE NOT EXISTS (
            SELECT 1 FROM workflow_task AS current
            WHERE current.uuid = legacy.workflow_id
        )
        """
    conn.execute(
        """
        CREATE VIEW workflow_runs AS
        SELECT
            task.uuid AS workflow_id,
            task.uuid AS task_id,
            COALESCE(json_extract(task.meta_data, '$.lab_id'), '') AS lab_id,
            COALESCE(json_extract(task.meta_data, '$.priority'), '') AS priority,
            (
                SELECT COUNT(*) FROM workflow_node_job AS job
                WHERE job.workflow_task_uuid = task.uuid
                  AND job.deleted_at IS NULL
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
        """
        + workflow_archive_union
    )

    job_archive_union = ""
    if _object_type(conn, "job_runs_legacy_archive") == "table":
        job_archive_union = """
        UNION ALL
        SELECT
            legacy.id, legacy.job_id, legacy.workflow_id, legacy.node_id,
            legacy.device_id, legacy.action_name, legacy.device_action_key,
            legacy.started_at, legacy.ended_at, legacy.actual_s,
            legacy.estimated_s, legacy.estimate_source, legacy.state,
            legacy.suc_type, legacy.ret_json
        FROM job_runs_legacy_archive AS legacy
        WHERE NOT EXISTS (
            SELECT 1 FROM workflow_node_job AS current
            WHERE current.uuid = legacy.job_id
        )
        """
    conn.execute(
        """
        CREATE VIEW job_runs AS
        SELECT
            job.rowid AS id,
            job.uuid AS job_id,
            job.workflow_task_uuid AS workflow_id,
            job.workflow_node_uuid AS node_id,
            COALESCE(job.edge_agent_uuid, job.material_uuid, '') AS device_id,
            COALESCE(json_extract(job.param, '$.action'), '') AS action_name,
            CASE
                WHEN COALESCE(job.edge_agent_uuid, job.material_uuid, '') = '' THEN ''
                ELSE COALESCE(job.edge_agent_uuid, job.material_uuid, '') || ':' ||
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
        WHERE job.deleted_at IS NULL
        """
        + job_archive_union
    )


def _rebuild_task_and_job(conn: sqlite3.Connection) -> None:
    conn.execute("ALTER TABLE workflow_node_job RENAME TO workflow_node_job_v0")
    conn.execute("ALTER TABLE workflow_task RENAME TO workflow_task_v0")
    for index_name in (
        "ix_workflow_task_workflow",
        "ix_workflow_task_status",
        "ix_workflow_node_job_task",
        "ix_workflow_node_job_node",
    ):
        conn.execute(f"DROP INDEX IF EXISTS {index_name}")

    _execute_script(conn, _WORKFLOW_TASK_SCHEMA)
    _execute_script(conn, _WORKFLOW_NODE_JOB_SCHEMA)
    conn.execute(
        """
        INSERT INTO workflow_task(
            uuid, create_time, update_time, deleted_at, description, meta_data,
            workflow_uuid, status, workflow_snapshot, execution_plan, run_mode,
            target_node_uuid, control_status, cleanup_status, trace_context,
            input, output, error_info, timeout_at, attention_reason,
            terminal_ghost_detected_at, reconciliation_resume_control_status,
            started_at, finished_at, execution_kind, idempotency_key,
            request_fingerprint
        )
        SELECT
            uuid, create_time, update_time, deleted_at, description, meta_data,
            workflow_uuid,
            CASE status
                WHEN 'success' THEN 'succeeded'
                WHEN 'cancelled' THEN 'canceled'
                ELSE status
            END,
            workflow_snapshot, execution_plan, run_mode,
            target_node_uuid, control_status, cleanup_status, trace_context,
            input, output, error_info, timeout_at, attention_reason,
            terminal_ghost_detected_at, reconciliation_resume_control_status,
            started_at, finished_at, 'workflow', NULL, ''
        FROM workflow_task_v0
        """
    )
    conn.execute(
        """
        INSERT INTO workflow_node_job(
            uuid, create_time, update_time, deleted_at, description, meta_data,
            workflow_task_uuid, workflow_node_uuid, material_uuid,
            edge_agent_uuid, edge_command_uuid, job_access_token_hash,
            feedback_sequence, topological_index, executor_kind,
            execution_policy, execution_timeout_seconds, status, attempt,
            param, feedback_data, return_info, control_data, error_info,
            dispatch_deadline_at, execution_deadline_at, cancel_command_uuid,
            cancel_ack_deadline_at, cancel_complete_deadline_at,
            uncertainty_reason, started_at, finished_at
        )
        SELECT
            uuid, create_time, update_time, deleted_at, description, meta_data,
            workflow_task_uuid, workflow_node_uuid, material_uuid,
            edge_agent_uuid, edge_command_uuid, job_access_token_hash,
            feedback_sequence, topological_index, executor_kind,
            execution_policy, execution_timeout_seconds,
            CASE status
                WHEN 'success' THEN 'succeeded'
                WHEN 'cancelled' THEN 'canceled'
                ELSE status
            END,
            attempt,
            param, feedback_data, return_info, control_data, error_info,
            dispatch_deadline_at, execution_deadline_at, cancel_command_uuid,
            cancel_ack_deadline_at, cancel_complete_deadline_at,
            uncertainty_reason, started_at, finished_at
        FROM workflow_node_job_v0
        """
    )
    conn.execute("DROP TABLE workflow_node_job_v0")
    conn.execute("DROP TABLE workflow_task_v0")


def _rebuild_frontend_event(conn: sqlite3.Connection) -> None:
    conn.execute("ALTER TABLE frontend_event RENAME TO frontend_event_v0")
    conn.execute(
        """
        CREATE TABLE frontend_event (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            uuid TEXT NOT NULL UNIQUE,
            create_time TEXT NOT NULL,
            type TEXT NOT NULL,
            aggregate_uuid TEXT NOT NULL,
            payload TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(payload) AND json_type(payload) = 'object'
            )
        )
        """
    )
    conn.execute(
        """
        INSERT INTO frontend_event(
            sequence, uuid, create_time, type, aggregate_uuid, payload
        )
        SELECT
            id,
            printf('00000000-0000-4000-8000-%012d', id),
            create_time,
            event,
            COALESCE(
                json_extract(data, '$.workflow_uuid'),
                json_extract(data, '$.task_uuid'),
                json_extract(data, '$.uuid'),
                '00000000-0000-0000-0000-000000000000'
            ),
            data
        FROM frontend_event_v0
        """
    )
    conn.execute("DROP TABLE frontend_event_v0")


def migrate_workflow_schema(conn: sqlite3.Connection) -> None:
    """把任意旧 Workflow DB 原地升级到当前 Backend-shaped 结构。"""

    current = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if current > WORKFLOW_SCHEMA_VERSION:
        raise WorkflowSchemaError(
            "workflow database schema version "
            f"{current} is newer than supported {WORKFLOW_SCHEMA_VERSION}"
        )

    task_columns = _columns(conn, "workflow_task")
    workflow_uuid = task_columns.get("workflow_uuid")
    if "execution_kind" not in task_columns or (
        workflow_uuid is not None and bool(workflow_uuid["notnull"])
    ):
        _rebuild_task_and_job(conn)

    event_columns = _columns(conn, "frontend_event")
    if "sequence" not in event_columns:
        _rebuild_frontend_event(conn)

    # v0 的短索引会被兼容 ``_SCHEMA`` 在每次打开时补回；统一删除，避免与
    # Backend-shaped 复合/partial 索引重复占用写放大。
    for index_name in (
        "ix_workflow_task_workflow",
        "ix_workflow_task_status",
        "ix_workflow_node_job_task",
        "ix_workflow_node_job_node",
    ):
        conn.execute(f"DROP INDEX IF EXISTS {index_name}")

    _execute_script(conn, _RUNTIME_FACT_SCHEMA)
    _retire_legacy_audit_tables(conn)
    conn.execute(f"PRAGMA user_version = {WORKFLOW_SCHEMA_VERSION}")


__all__ = [
    "WORKFLOW_SCHEMA_VERSION",
    "WorkflowSchemaError",
    "migrate_workflow_schema",
]
