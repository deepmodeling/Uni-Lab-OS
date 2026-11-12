"""runtime.db 的迁移链：每一步把库从 ``version - 1`` 升到 ``version``。

迁移里的建表 / 索引语句是该版本冻结的字面量：之后表规格继续演进时，旧迁移仍然
生成它那个版本的结构，后续迁移在此之上继续。
"""

from __future__ import annotations

import sqlite3

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

_EXECUTION_CLAIM_V5 = (
    """
        CREATE TABLE IF NOT EXISTS workflow_execution_claim (
            claim_uuid TEXT PRIMARY KEY,
            job_uuid TEXT NOT NULL UNIQUE,
            task_uuid TEXT NOT NULL,
            attempt_no INTEGER NOT NULL CHECK (attempt_no >= 1),
            request TEXT NOT NULL CHECK (json_valid(request) AND json_type(request) = 'object'),
            fences TEXT NOT NULL CHECK (json_valid(fences) AND json_type(fences) = 'object'),
            state TEXT NOT NULL CHECK (state IN ('reserved', 'running', 'uncertain', 'released')),
            acquired_at TEXT NOT NULL,
            released_at TEXT,
            FOREIGN KEY(job_uuid) REFERENCES workflow_node_job(uuid),
            FOREIGN KEY(task_uuid) REFERENCES workflow_task(uuid)
        )
    """,
    """
        CREATE TABLE IF NOT EXISTS workflow_execution_fence (
            lock_key TEXT PRIMARY KEY,
            token INTEGER NOT NULL CHECK (token >= 1)
        )
    """,
    """
        CREATE TABLE IF NOT EXISTS workflow_execution_lease (
            lock_key TEXT PRIMARY KEY,
            claim_uuid TEXT NOT NULL,
            token INTEGER NOT NULL CHECK (token >= 1),
            FOREIGN KEY(claim_uuid) REFERENCES workflow_execution_claim(claim_uuid),
            FOREIGN KEY(lock_key) REFERENCES workflow_execution_fence(lock_key)
        )
    """,
)

_EXECUTION_START_V6 = (
    """
        CREATE TABLE IF NOT EXISTS workflow_execution_authority (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            epoch TEXT NOT NULL
        )
    """,
    """
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
    """,
)

_RESOURCE_INTERVAL_V7 = (
    """
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
    """,
    """
        CREATE TABLE IF NOT EXISTS workflow_resource_admission (
            job_uuid TEXT PRIMARY KEY REFERENCES workflow_node_job(uuid),
            request TEXT NOT NULL CHECK(json_valid(request) AND json_type(request)='object'),
            ownerships TEXT NOT NULL CHECK(json_valid(ownerships) AND json_type(ownerships)='object'),
            uses TEXT NOT NULL CHECK(json_valid(uses) AND json_type(uses)='object'),
            state TEXT NOT NULL CHECK(state IN ('active','released')),
            admitted_at TEXT NOT NULL
        )
    """,
    """
        CREATE TABLE IF NOT EXISTS workflow_resource_use (
            lock_key TEXT PRIMARY KEY,
            job_uuid TEXT NOT NULL REFERENCES workflow_resource_admission(job_uuid),
            owner_uuid TEXT NOT NULL REFERENCES workflow_resource_interval(owner_uuid)
        )
    """,
    """CREATE UNIQUE INDEX IF NOT EXISTS idx_resource_interval_active ON workflow_resource_interval(lock_key) WHERE state!='released'""",
)

_RESOURCE_CUSTODY_V8 = (
    """
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
    """,
    """CREATE UNIQUE INDEX IF NOT EXISTS idx_resource_loan_active ON workflow_resource_loan(lender_owner_uuid) WHERE state!='returned'""",
    """
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
    """,
    """CREATE UNIQUE INDEX IF NOT EXISTS idx_device_tenancy_active ON workflow_device_tenancy(device_key) WHERE state!='released'""",
    """
        CREATE TABLE IF NOT EXISTS workflow_tenancy_transition (
            job_uuid TEXT PRIMARY KEY REFERENCES workflow_node_job(uuid),
            contract TEXT NOT NULL CHECK(json_valid(contract) AND json_type(contract)='object'),
            acquire_uuid TEXT REFERENCES workflow_device_tenancy(tenancy_uuid),
            release_uuid TEXT REFERENCES workflow_device_tenancy(tenancy_uuid),
            state TEXT NOT NULL CHECK(state IN ('prepared','settled','retained','reverted'))
        )
    """,
    """
        CREATE TABLE IF NOT EXISTS workflow_resource_waiter (
            job_uuid TEXT PRIMARY KEY REFERENCES workflow_node_job(uuid),
            task_uuid TEXT NOT NULL REFERENCES workflow_task(uuid),
            lock_keys TEXT NOT NULL CHECK(json_valid(lock_keys) AND json_type(lock_keys)='array'),
            blockers TEXT NOT NULL CHECK(json_valid(blockers) AND json_type(blockers)='array')
        )
    """,
)

# v9：V1 工作流作者合同——工作流类型 / 实验操作类别、节点人工确认配置、类别表。
#: (表, 列, ADD COLUMN 定义)；已存在的列跳过，迁移可在部分已升级的库上重放。
_WORKFLOW_AUTHORING_V9_COLUMNS = (
    (
        "workflow",
        "workflow_type",
        "workflow_type TEXT NOT NULL DEFAULT 'normal' CHECK ("
        " workflow_type IN ('normal', 'experiment_operation'))",
    ),
    ("workflow", "operation_category_uuid", "operation_category_uuid TEXT"),
    ("workflow_node", "manual_confirmation", "manual_confirmation TEXT NOT NULL DEFAULT '{}'"),
)
_WORKFLOW_AUTHORING_V9_STATEMENTS = (
    """
            CREATE INDEX IF NOT EXISTS idx_workflow_type_active
            ON workflow(workflow_type, operation_category_uuid) WHERE deleted_at IS NULL
    """,
    """
        CREATE TABLE IF NOT EXISTS workflow_operation_category (
            uuid TEXT PRIMARY KEY,
            create_time TEXT NOT NULL,
            update_time TEXT NOT NULL,
            deleted_at TEXT,
            name TEXT NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 100 CHECK (sort_order BETWEEN 0 AND 10000)
        )
    """,
)


def _workflow_authoring_v9(connection: sqlite3.Connection) -> None:
    for table, column, definition in _WORKFLOW_AUTHORING_V9_COLUMNS:
        existing = {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
        if column not in existing:
            connection.execute(f'ALTER TABLE "{table}" ADD COLUMN {definition}')
    for statement in _WORKFLOW_AUTHORING_V9_STATEMENTS:
        connection.execute(statement)


# v10：V1 任务生命周期（与 v9 作者合同分支合并后重编号）。workflow_task 增加优先级、库存绑定与普通模式派发闸门；任务命令增加
# switch_to_step / unlock_resources；新增结构化人工交互表。
_WORKFLOW_TASK_COMMAND_V10 = """
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
        """
_WORKFLOW_TASK_V10_COLUMNS = (
    "ALTER TABLE workflow_task ADD COLUMN priority TEXT NOT NULL DEFAULT 'normal' "
    "CHECK (priority IN ('normal', 'high'))",
    "ALTER TABLE workflow_task ADD COLUMN inventory_bindings TEXT NOT NULL DEFAULT '[]' "
    "CHECK (json_valid(inventory_bindings) AND json_type(inventory_bindings) = 'array')",
    "ALTER TABLE workflow_task ADD COLUMN dispatch_gate TEXT NOT NULL DEFAULT 'active' "
    "CHECK (dispatch_gate IN ('active', 'paused'))",
)


def _task_lifecycle_v10(connection) -> None:
    from unilabos.server.database.tables.runtime.task_lifecycle import (
        HUMAN_INTERACTION_V10,
        HUMAN_INTERACTION_V10_INDEXES,
    )

    columns = {row[1] for row in connection.execute('PRAGMA table_info("workflow_task")')}
    for column, statement in zip(("priority", "inventory_bindings", "dispatch_gate"), _WORKFLOW_TASK_V10_COLUMNS):
        # 重放安全：从更早版本整段重放迁移链时列可能已存在
        if column not in columns:
            connection.execute(statement)
    rebuild_table_migration(
        10,
        "",
        table="workflow_task_command",
        create_sql=_WORKFLOW_TASK_COMMAND_V10,
        indexes=_WORKFLOW_TASK_COMMAND_V4_INDEXES,
    ).apply(connection)
    connection.execute(HUMAN_INTERACTION_V10)
    for statement in HUMAN_INTERACTION_V10_INDEXES:
        connection.execute(statement)


def _station_handling_v11(connection) -> None:
    from unilabos.server.database.tables.runtime.station_handling import (
        STATION_HANDLING_V11,
        STATION_HANDLING_V11_INDEXES,
    )

    for statement in (*STATION_HANDLING_V11, *STATION_HANDLING_V11_INDEXES):
        connection.execute(statement)


def _workspace_host_v12(connection) -> None:
    from unilabos.server.database.tables.runtime.workspace_host import (
        WORKSPACE_HOST_V12,
        WORKSPACE_HOST_V12_INDEXES,
    )

    for statement in (*WORKSPACE_HOST_V12, *WORKSPACE_HOST_V12_INDEXES):
        connection.execute(statement)


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
    sql_migration(5, "持久化派发资源声明、租约与栅栏计数", *_EXECUTION_CLAIM_V5),
    sql_migration(6, "执行代次、一次开始许可与不可变开始回执", *_EXECUTION_START_V6),
    sql_migration(7, "区间资源所有权和单次动作使用清单", *_RESOURCE_INTERVAL_V7),
    sql_migration(8, "设备托管、资源借还和等待环路", *_RESOURCE_CUSTODY_V8),
    Migration(9, "工作流类型、实验操作类别与节点人工确认配置（V1 作者合同）", _workflow_authoring_v9),
    Migration(10, "V1 任务生命周期：优先级、库存绑定、派发闸门、命令类型与人工交互", _task_lifecycle_v10),
    Migration(11, "V1 工站错误处置：工站控制、控制命令、处置会话与工站错误", _station_handling_v11),
    Migration(12, "V1 Workspace Host：工作区操作记录与快照修订号", _workspace_host_v12),
)
RUNTIME_SCHEMA_VERSION = RUNTIME_MIGRATIONS[-1].version
