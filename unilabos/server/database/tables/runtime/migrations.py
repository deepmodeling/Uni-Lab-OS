"""runtime.db 的迁移链：每一步把库从 ``version - 1`` 升到 ``version``。

迁移里的建表 / 索引语句是该版本冻结的字面量：之后表规格继续演进时，旧迁移仍然
生成它那个版本的结构，后续迁移在此之上继续。
"""

from __future__ import annotations

from unilabos.server.database.schema import Migration, rebuild_table_migration, sql_migration

# v2：整图任务也可携带幂等键（与单点设备动作同一套 idempotency_key + request_fingerprint）。
_WORKFLOW_TASK_V2 = """
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
        """
_WORKFLOW_TASK_V2_INDEXES = (
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
)

# v3：工作流显式发布为不可变修订（工作流即节点）；纯新增表，旧表不动。
_WORKFLOW_PUBLICATION_V3 = """
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
        """
_WORKFLOW_PUBLICATION_V3_INDEXES = (
    """
            CREATE INDEX IF NOT EXISTS idx_workflow_publication_created
            ON workflow_publication(create_time DESC, uuid)
            """,
)

# v4：任务命令新增 edit_input（暂停时修改入口参数的审计记录，推进 control_revision）。
_WORKFLOW_TASK_COMMAND_V4 = """
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
            type TEXT NOT NULL CHECK (type IN ('step', 'pause', 'resume', 'cancel', 'edit_input')),
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
        """
_WORKFLOW_TASK_COMMAND_V4_INDEXES = (
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
)

RUNTIME_MIGRATIONS: tuple[Migration, ...] = (
    rebuild_table_migration(
        2,
        "workflow_task 允许整图任务携带幂等键",
        table="workflow_task",
        create_sql=_WORKFLOW_TASK_V2,
        indexes=_WORKFLOW_TASK_V2_INDEXES,
    ),
    sql_migration(
        3,
        "新增 workflow_publication：工作流发布的不可变修订快照",
        _WORKFLOW_PUBLICATION_V3,
        *_WORKFLOW_PUBLICATION_V3_INDEXES,
    ),
    rebuild_table_migration(
        4,
        "workflow_task_command 允许 edit_input（暂停时修改入口参数）",
        table="workflow_task_command",
        create_sql=_WORKFLOW_TASK_COMMAND_V4,
        indexes=_WORKFLOW_TASK_COMMAND_V4_INDEXES,
    ),
)
RUNTIME_SCHEMA_VERSION = RUNTIME_MIGRATIONS[-1].version
