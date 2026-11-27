"""本地工作流存储（Workflow Store）的增量 Schema 迁移。"""

from __future__ import annotations

import sqlite3

WORKFLOW_DEFINITION_TABLES = (
    "workflow",
    "workflow_node",
    "workflow_edge",
    "workflow_node_template",
    "workflow_handle_template",
    "workflow_source_registration",
    "workflow_authoring",
    # 需求定义跟随工作流图进入进程内目录，运行事实库不读取它。
    "workflow_inventory_requirement",
)


def _execute_script_in_transaction(
    connection: sqlite3.Connection,
    script: str,
) -> None:
    """在调用方现有事务内逐条执行一段 SQLite DDL。

    参数：``connection`` 是已经 ``BEGIN`` 的连接；``script`` 是可含触发器的完整
    DDL。返回无。异常：任一语句失败时原样传播，由调用方回滚。这里不使用
    ``executescript``，因为后者会隐式提交并破坏初始化事务边界。
    """

    statement_lines: list[str] = []
    for line in script.splitlines():
        statement_lines.append(line)
        statement = "\n".join(statement_lines).strip()
        if statement and sqlite3.complete_statement(statement):
            connection.execute(statement)
            statement_lines.clear()
    if "\n".join(statement_lines).strip():
        raise sqlite3.OperationalError("SQLite 迁移包含不完整语句")


def ensure_device_action_run_schema(connection: sqlite3.Connection) -> None:
    """补齐设备单动作运行（DeviceActionRun）所需 Task 身份字段。

    参数：``connection`` 是 ``WorkflowStore`` 初始化期间持有的唯一写连接。
    返回：无返回值；函数幂等增加 ``execution_kind``、幂等键和请求指纹，并把
    ``workflow_uuid`` 调整为可空，使直接设备动作不伪造工作流（Workflow）。
    异常会交给调用方回滚整个初始化事务。
    """

    # ``task_columns`` 是当前数据库已经持久化的 Task 列集合，用于兼容原地升级。
    task_columns = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(workflow_task)").fetchall()
    }
    if "execution_kind" not in task_columns:
        connection.execute(
            """
            ALTER TABLE workflow_task
            ADD COLUMN execution_kind TEXT NOT NULL DEFAULT 'workflow'
                CHECK (execution_kind IN ('workflow', 'ad_hoc_device_action'))
            """
        )
    if "idempotency_key" not in task_columns:
        connection.execute("ALTER TABLE workflow_task ADD COLUMN idempotency_key TEXT")
    if "request_fingerprint" not in task_columns:
        connection.execute(
            """
            ALTER TABLE workflow_task
            ADD COLUMN request_fingerprint TEXT NOT NULL DEFAULT ''
            """
        )

    # ``table_sql`` 是 SQLite 保存的建表合同；旧版本把 workflow_uuid 声明为
    # NOT NULL，必须只改这一段才能容纳不创建 Workflow 的直接设备动作。
    table_row = connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type = 'table' AND name = 'workflow_task'"
    ).fetchone()
    table_sql = str(table_row["sql"] or "") if table_row is not None else ""
    if "workflow_uuid TEXT NOT NULL" in table_sql:
        # SQLite 不能直接 DROP NOT NULL；采用 Backend 000045 已验证的
        # writable_schema 技术，只替换精确片段并推进 schema_version。
        current_schema_version = int(
            connection.execute("PRAGMA schema_version").fetchone()[0]
        )
        connection.execute("PRAGMA writable_schema = ON")
        try:
            connection.execute(
                """
                UPDATE sqlite_schema
                SET sql = replace(
                    sql,
                    'workflow_uuid TEXT NOT NULL,',
                    'workflow_uuid TEXT,'
                )
                WHERE type = 'table' AND name = 'workflow_task'
                """
            )
            connection.execute(
                f"PRAGMA schema_version = {current_schema_version + 1}"
            )
        finally:
            connection.execute("PRAGMA writable_schema = OFF")

    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_workflow_task_execution_kind
        ON workflow_task(execution_kind, create_time DESC, uuid DESC)
        WHERE deleted_at IS NULL
        """
    )
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_workflow_task_execution_idempotency
        ON workflow_task(execution_kind, idempotency_key)
        WHERE deleted_at IS NULL AND idempotency_key IS NOT NULL
        """
    )


def ensure_station_task_submission_schema(connection: sqlite3.Connection) -> None:
    """补齐 Backend 工站调用的关联、幂等与优先级字段。

    参数：``connection`` 是工作流运行库初始化事务的唯一写连接。返回无；幂等
    增加全局任务身份、调用键和优先级，并为同一 Backend 调用建立活动唯一索引。
    新建数据库的优先级默认值为 ``normal``；旧库中的数值优先级继续兼容。异常由
    初始化事务回滚，禁止在无法证明调用幂等时开放工站任务接口。
    """

    task_columns = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(workflow_task)").fetchall()
    }
    if "backend_task_uuid" not in task_columns:
        connection.execute(
            "ALTER TABLE workflow_task ADD COLUMN backend_task_uuid TEXT"
        )
    if "invocation_key" not in task_columns:
        connection.execute(
            "ALTER TABLE workflow_task ADD COLUMN invocation_key TEXT"
        )
    if "priority" not in task_columns:
        connection.execute(
            "ALTER TABLE workflow_task ADD COLUMN priority TEXT NOT NULL DEFAULT 'normal'"
        )
    if "revision_fingerprint" not in task_columns:
        connection.execute(
            "ALTER TABLE workflow_task ADD COLUMN revision_fingerprint TEXT"
        )
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_workflow_task_station_invocation
        ON workflow_task(backend_task_uuid, invocation_key)
        WHERE deleted_at IS NULL
          AND backend_task_uuid IS NOT NULL
          AND invocation_key IS NOT NULL
        """
    )
    ensure_station_invocation_identity_is_permanent(connection)


def ensure_station_invocation_identity_is_permanent(
    connection: sqlite3.Connection,
) -> None:
    """把工站调用身份的唯一约束改为永久占用。

    参数：``connection`` 是工作流运行库初始化事务的唯一写连接。返回无。

    调用身份 ``(backend_task_uuid, invocation_key)`` 一经使用就不能释放：任务
    软删除、日志归档和清理历史结果都只表示"不再执行"，不表示"可以再创建一次"。
    旧索引带 ``WHERE deleted_at IS NULL``，会让软删除后的同一调用键再建出一个
    任务，即同一次调用被执行两次；中心重投会直接踩中。这里重建为不带软删谓词
    的索引，并让调用键在任务删除后继续占用。

    异常：若旧库中已经存在软删除后的重复调用键，重建唯一索引会失败并回滚整个
    初始化事务，由运维人工核实后再启动；这里拒绝静默改写既有任务身份。
    """

    connection.execute("DROP INDEX IF EXISTS ux_workflow_task_station_invocation")
    try:
        connection.execute(
            """
            CREATE UNIQUE INDEX ux_workflow_task_station_invocation
            ON workflow_task(backend_task_uuid, invocation_key)
            WHERE backend_task_uuid IS NOT NULL
              AND invocation_key IS NOT NULL
            """
        )
    except sqlite3.IntegrityError as error:
        duplicates = connection.execute(
            """
            SELECT backend_task_uuid, invocation_key, COUNT(*) AS task_count
            FROM workflow_task
            WHERE backend_task_uuid IS NOT NULL AND invocation_key IS NOT NULL
            GROUP BY backend_task_uuid, invocation_key
            HAVING COUNT(*) > 1
            """
        ).fetchall()
        rendered = ", ".join(
            f"{row['backend_task_uuid']}/{row['invocation_key']}×{row['task_count']}"
            for row in duplicates
        )
        raise sqlite3.IntegrityError(
            "存在被软删除释放的重复工站调用键，无法建立永久唯一索引；"
            "请人工核实这些调用是否已重复执行后再启动："
            f"{rendered}"
        ) from error


def ensure_workflow_task_control_schema(connection: sqlite3.Connection) -> None:
    """补齐 WorkflowTask 可变执行控制模式与普通派发闸门。

    ``run_mode`` 继续保存创建时冻结模式；``execution_mode`` 只表示当前调度
    当前单步执行状态；``dispatch_gate`` 独立保存普通 Task 的暂停意图。旧库中的 step
    或已暂停任务按已有事实初始化为 paused，其余任务保持 active。
    """

    task_columns = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(workflow_task)").fetchall()
    }
    if "execution_mode" not in task_columns:
        connection.execute(
            """
            ALTER TABLE workflow_task
            ADD COLUMN execution_mode TEXT NOT NULL DEFAULT 'normal'
                CHECK (execution_mode IN ('normal', 'switching_to_step', 'step'))
            """
        )
        task_columns.add("execution_mode")
        connection.execute(
            """
            UPDATE workflow_task
            SET execution_mode = 'step'
            WHERE run_mode = 'step'
            """
        )

    if "dispatch_gate" not in task_columns:
        connection.execute(
            """
            ALTER TABLE workflow_task
            ADD COLUMN dispatch_gate TEXT NOT NULL DEFAULT 'active'
                CHECK (dispatch_gate IN ('active', 'paused'))
            """
        )
        connection.execute(
            """
            UPDATE workflow_task
            SET dispatch_gate = CASE
                WHEN run_mode = 'step'
                  OR execution_mode IN ('step', 'switching_to_step')
                  OR control_status = 'paused'
                THEN 'paused'
                ELSE 'active'
            END
            """
        )


def ensure_workflow_task_debug_configuration_schema(
    connection: sqlite3.Connection,
) -> None:
    """把旧版调试配置子表折叠到 ``workflow_task``。

    新库直接在任务表中保存调试配置。旧库首次启动时先补齐可空字段，再在同一
    初始化事务内搬运旧表数据并删除子表；如果任务行不存在或目标字段已有冲突
    内容则拒绝启动，避免静默丢失调试状态。
    """

    task_columns = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(workflow_task)").fetchall()
    }
    columns = (
        ("debug_create_time", "TEXT"),
        ("debug_update_time", "TEXT"),
        ("debug_start_node_uuids", "TEXT"),
        ("debug_breakpoint_node_uuids", "TEXT"),
        (
            "debug_execution_policy",
            "TEXT CHECK (debug_execution_policy IS NULL OR debug_execution_policy IN ('step', 'continue'))",
        ),
        (
            "debug_status",
            "TEXT CHECK (debug_status IS NULL OR debug_status IN ('paused', 'running', 'completed', 'stopped'))",
        ),
    )
    for name, definition in columns:
        if name not in task_columns:
            connection.execute(
                f"ALTER TABLE workflow_task ADD COLUMN {name} {definition}"
            )

    legacy = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='workflow_task_debug_configuration'"
    ).fetchone()
    if legacy is None:
        return

    legacy_rows = connection.execute(
        """
        SELECT workflow_task_uuid, create_time, update_time,
               start_node_uuids, breakpoint_node_uuids,
               execution_policy, status
        FROM workflow_task_debug_configuration
        """
    ).fetchall()
    for legacy_row in legacy_rows:
        task_uuid = str(legacy_row["workflow_task_uuid"])
        task = connection.execute(
            """
            SELECT uuid, debug_create_time, debug_update_time,
                   debug_start_node_uuids, debug_breakpoint_node_uuids,
                   debug_execution_policy, debug_status
            FROM workflow_task
            WHERE uuid = ?
            """,
            (task_uuid,),
        ).fetchone()
        if task is None:
            raise sqlite3.IntegrityError(
                "workflow_task_debug_configuration 引用了不存在的任务"
            )
        legacy_values = (
            legacy_row["create_time"],
            legacy_row["update_time"],
            legacy_row["start_node_uuids"],
            legacy_row["breakpoint_node_uuids"],
            legacy_row["execution_policy"],
            legacy_row["status"],
        )
        current_values = (
            task["debug_create_time"],
            task["debug_update_time"],
            task["debug_start_node_uuids"],
            task["debug_breakpoint_node_uuids"],
            task["debug_execution_policy"],
            task["debug_status"],
        )
        if any(value is not None for value in current_values):
            if current_values != legacy_values:
                raise sqlite3.IntegrityError(
                    "workflow_task 调试配置与旧子表数据冲突"
                )
            continue
        connection.execute(
            """
            UPDATE workflow_task
            SET debug_create_time = ?, debug_update_time = ?,
                debug_start_node_uuids = ?, debug_breakpoint_node_uuids = ?,
                debug_execution_policy = ?, debug_status = ?
            WHERE uuid = ?
            """,
            (*legacy_values, task_uuid),
        )
    connection.execute("DROP TABLE workflow_task_debug_configuration")


def ensure_task_resource_unlock_command_schema(
    connection: sqlite3.Connection,
) -> None:
    """为旧工作流库开放异常终态 Task 人工释放命令。

    参数：``connection`` 是 WorkflowStore 初始化事务。返回无。异常：
    建表合同不是精确旧版或新版时失败关闭，避免宽松改写未知 Schema。
    SQLite 不支持直接修改 CHECK，因此保持表、索引和外键原样，只替换
    ``workflow_task_command.type`` 的精确约束片段。
    """

    old = "type IN ('step', 'pause', 'resume', 'cancel')"
    intermediate = "type IN ('step', 'pause', 'resume', 'cancel', 'unlock_resources')"
    step_intermediate = "type IN ('step', 'switch_to_step', 'pause', 'resume', 'cancel', 'unlock_resources')"
    legacy_new = "type IN ('step', 'pause', 'resume', 'cancel', 'unlock_resources', 'retry_node', 'execute_manual_action', 'stop_manual_action', 'resource_disposition', 'inventory_correction')"
    new = "type IN ('step', 'switch_to_step', 'pause', 'resume', 'cancel', 'unlock_resources', 'retry_node', 'execute_manual_action', 'stop_manual_action', 'resource_disposition', 'inventory_correction')"
    row = connection.execute(
        "SELECT sql FROM sqlite_schema "
        "WHERE type='table' AND name='workflow_task_command'"
    ).fetchone()
    table_sql = str(row["sql"] or "") if row is not None else ""
    if new in table_sql:
        return
    if legacy_new in table_sql:
        old = legacy_new
    elif step_intermediate in table_sql:
        old = step_intermediate
    elif intermediate in table_sql:
        old = intermediate
    if table_sql.count(old) != 1 or new in table_sql:
        raise sqlite3.OperationalError(
            "workflow_task_command 定义无法安全增加人工释放命令"
        )
    schema_version = int(connection.execute("PRAGMA schema_version").fetchone()[0])
    connection.execute("PRAGMA writable_schema = ON")
    try:
        changed = connection.execute(
            "UPDATE sqlite_schema SET sql=replace(sql, ?, ?) "
            "WHERE type='table' AND name='workflow_task_command' "
            "AND instr(sql, ?) > 0",
            (old, new, old),
        ).rowcount
        if changed != 1:
            raise sqlite3.OperationalError(
                "workflow_task_command 人工释放命令迁移未命中"
            )
        connection.execute(f"PRAGMA schema_version = {schema_version + 1}")
    finally:
        connection.execute("PRAGMA writable_schema = OFF")
    migrated = connection.execute(
        "SELECT sql FROM sqlite_schema "
        "WHERE type='table' AND name='workflow_task_command'"
    ).fetchone()
    if migrated is None or new not in str(migrated["sql"] or ""):
        raise sqlite3.OperationalError(
            "workflow_task_command 人工释放命令迁移未生效"
        )


def ensure_ephemeral_workflow_reference_schema(
    connection: sqlite3.Connection,
) -> None:
    """解除持久任务对可消失工作流定义的数据库外键依赖。

    参数：``connection`` 是 ``WorkflowStore`` 初始化事务持有的唯一写连接。
    返回：无；函数只移除 ``workflow_task.workflow_uuid`` 到 ``workflow`` 的旧外键，
    字段本身继续保存来源工作流身份。异常：表结构不符合已知合同或改写失败时关闭式
    失败，由调用方回滚初始化事务。

    Local 模式的工作流定义由领域包源码在进程内重建，而 Task/Job 必须跨重启保留。
    因此运行事实可以引用一个当前进程已经不存在的定义身份，但不能因此被 SQLite
    拒绝恢复。这里沿用本文件既有的精确 ``writable_schema`` 迁移方式，不新增表。
    """

    row = connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type = 'table' AND name = 'workflow_task'"
    ).fetchone()
    table_sql = str(row["sql"] or "") if row is not None else ""
    foreign_key_clause = (
        ",\n    FOREIGN KEY(workflow_uuid) REFERENCES workflow(uuid)"
    )
    if foreign_key_clause not in table_sql:
        # 已迁移的 Local 运行库不再含该约束；其他 workflow_task 外键由子表承担，
        # 不能在这里做模糊字符串删除。
        if any(
            str(item["table"]) == "workflow"
            and str(item["from"]) == "workflow_uuid"
            for item in connection.execute(
                "PRAGMA foreign_key_list(workflow_task)"
            ).fetchall()
        ):
            raise sqlite3.OperationalError("workflow_task 定义外键结构无法安全迁移")
        return

    schema_version = int(connection.execute("PRAGMA schema_version").fetchone()[0])
    connection.execute("PRAGMA writable_schema = ON")
    try:
        connection.execute(
            """
            UPDATE sqlite_schema
            SET sql = replace(sql, ?, '')
            WHERE type = 'table' AND name = 'workflow_task'
            """,
            (foreign_key_clause,),
        )
        connection.execute(f"PRAGMA schema_version = {schema_version + 1}")
    finally:
        connection.execute("PRAGMA writable_schema = OFF")

    if any(
        str(item["table"]) == "workflow"
        and str(item["from"]) == "workflow_uuid"
        for item in connection.execute(
            "PRAGMA foreign_key_list(workflow_task)"
        ).fetchall()
    ):
        raise sqlite3.OperationalError("workflow_task 定义外键迁移未生效")


def drop_empty_workflow_definition_tables(
    connection: sqlite3.Connection,
) -> bool:
    """清理运行事实库中旧版本遗留的空工作流定义表。

    旧版本的公共 schema 会在运行事实库中留下定义表空壳。只有确认所有定义表
    都没有行时才删除；发现任意历史行则保留整组表，避免升级过程静默丢失旧数据。
    运行事实表的 workflow_uuid 不依赖这些表，因此清理不会影响 Task/Job 恢复。
    """

    existing = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'table'"
        ).fetchall()
    }
    present = [name for name in WORKFLOW_DEFINITION_TABLES if name in existing]
    if not present:
        return True
    for name in present:
        if connection.execute(f"SELECT 1 FROM {name} LIMIT 1").fetchone() is not None:
            return False

    # 先删带定义外键的需求/图表，再删工作流头，最后删模板表。
    for name in (
        "workflow_inventory_requirement",
        "workflow_authoring",
        "workflow_source_registration",
        "workflow_edge",
        "workflow_node",
        "workflow",
        "workflow_handle_template",
        "workflow_node_template",
    ):
        if name in existing:
            connection.execute(f"DROP TABLE {name}")
    return True


def ensure_task_material_admission_schema(connection: sqlite3.Connection) -> None:
    """补齐本地任务物料准入（TaskMaterialAdmission）的持久事实。

    参数：``connection`` 是 ``WorkflowStore`` 初始化事务持有的唯一写连接。
    返回：无返回值；函数幂等创建准入、绑定和任务物料占有（TaskMaterialClaim）
    表，并为旧任务表补充结构化等待原因。异常由调用方回滚初始化事务。

    这些表对齐 Backend 的公开运行语义；本地 ``inventory_reservation`` 仍是 Edge
    库存实现细节，不替代这里面向任务聚合的持久事实。
    """

    task_columns = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(workflow_task)").fetchall()
    }
    if "wait_reason" not in task_columns:
        connection.execute(
            """
            ALTER TABLE workflow_task
            ADD COLUMN wait_reason TEXT NOT NULL DEFAULT '{}'
            """
        )

    _execute_script_in_transaction(
        connection,
        """
        CREATE TABLE IF NOT EXISTS workflow_task_material_admission (
            uuid TEXT PRIMARY KEY,
            create_time TEXT NOT NULL,
            update_time TEXT NOT NULL,
            deleted_at TEXT,
            description TEXT,
            meta_data TEXT NOT NULL DEFAULT '{}',
            workflow_task_uuid TEXT NOT NULL,
            status TEXT NOT NULL
                CHECK (status IN ('blocked', 'admitted', 'rejected')),
            attempt INTEGER NOT NULL CHECK (attempt > 0),
            revision INTEGER NOT NULL CHECK (revision > 0),
            reason TEXT,
            wait_reason TEXT NOT NULL DEFAULT '{}',
            evaluated_at TEXT NOT NULL,
            admitted_at TEXT,
            CHECK (
                (status = 'admitted' AND admitted_at IS NOT NULL AND reason IS NULL)
                OR (status IN ('blocked', 'rejected')
                    AND admitted_at IS NULL AND reason IS NOT NULL)
            ),
            FOREIGN KEY(workflow_task_uuid) REFERENCES workflow_task(uuid)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_workflow_task_material_admission_task
            ON workflow_task_material_admission(workflow_task_uuid)
            WHERE deleted_at IS NULL;

        CREATE TABLE IF NOT EXISTS workflow_task_material_binding (
            uuid TEXT PRIMARY KEY,
            create_time TEXT NOT NULL,
            update_time TEXT NOT NULL,
            deleted_at TEXT,
            description TEXT,
            meta_data TEXT NOT NULL DEFAULT '{}',
            workflow_task_uuid TEXT NOT NULL,
            workflow_node_uuid TEXT NOT NULL,
            workflow_node_job_uuid TEXT NOT NULL,
            resource_template_uuid TEXT NOT NULL,
            material_uuid TEXT NOT NULL,
            site_uuid TEXT,
            flow_role TEXT NOT NULL
                CHECK (flow_role IN (
                    'primary_sample', 'aliquot_sample', 'reagent', 'consumable'
                )),
            custody_policy TEXT NOT NULL
                CHECK (custody_policy IN ('task_exclusive', 'shared_source')),
            FOREIGN KEY(workflow_task_uuid) REFERENCES workflow_task(uuid),
            FOREIGN KEY(workflow_node_job_uuid) REFERENCES workflow_node_job(uuid)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_workflow_task_material_binding_node
            ON workflow_task_material_binding(workflow_task_uuid, workflow_node_uuid)
            WHERE deleted_at IS NULL;
        CREATE UNIQUE INDEX IF NOT EXISTS ux_workflow_task_material_binding_job
            ON workflow_task_material_binding(
                workflow_task_uuid, workflow_node_job_uuid
            ) WHERE deleted_at IS NULL;
        CREATE INDEX IF NOT EXISTS ix_workflow_task_material_binding_material
            ON workflow_task_material_binding(material_uuid, workflow_task_uuid);

        CREATE TABLE IF NOT EXISTS workflow_task_material_claim (
            uuid TEXT PRIMARY KEY,
            create_time TEXT NOT NULL,
            update_time TEXT NOT NULL,
            deleted_at TEXT,
            description TEXT,
            meta_data TEXT NOT NULL DEFAULT '{}',
            workflow_task_uuid TEXT NOT NULL,
            workflow_node_uuid TEXT NOT NULL,
            workflow_node_job_uuid TEXT NOT NULL,
            material_uuid TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('active', 'released')),
            revision INTEGER NOT NULL CHECK (revision > 0),
            acquired_at TEXT NOT NULL,
            released_at TEXT,
            CHECK (
                (status = 'active' AND released_at IS NULL)
                OR (status = 'released' AND released_at IS NOT NULL)
            ),
            FOREIGN KEY(workflow_task_uuid) REFERENCES workflow_task(uuid),
            FOREIGN KEY(workflow_node_job_uuid) REFERENCES workflow_node_job(uuid)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_workflow_task_material_claim_node
            ON workflow_task_material_claim(workflow_task_uuid, workflow_node_uuid)
            WHERE deleted_at IS NULL;
        CREATE UNIQUE INDEX IF NOT EXISTS ux_workflow_task_material_claim_job
            ON workflow_task_material_claim(
                workflow_task_uuid, workflow_node_job_uuid
            ) WHERE deleted_at IS NULL;
        CREATE UNIQUE INDEX IF NOT EXISTS ux_workflow_task_material_claim_active_material
            ON workflow_task_material_claim(material_uuid)
            WHERE deleted_at IS NULL AND status = 'active';
        CREATE INDEX IF NOT EXISTS ix_workflow_task_material_claim_task_status
            ON workflow_task_material_claim(workflow_task_uuid, status, uuid);

        CREATE TRIGGER IF NOT EXISTS trg_release_terminal_task_material_claims
        AFTER UPDATE OF status, cleanup_status ON workflow_task
        FOR EACH ROW
        WHEN (
            NEW.status = 'succeeded'
            OR (
                NEW.status IN ('failed', 'canceled', 'timeout')
                AND NEW.cleanup_status = 'settled'
            )
        ) AND (
            OLD.status IS NOT NEW.status
            OR OLD.cleanup_status IS NOT NEW.cleanup_status
        )
        BEGIN
            UPDATE workflow_task_material_claim
            SET status = 'released',
                released_at = COALESCE(NEW.finished_at, CURRENT_TIMESTAMP),
                revision = revision + 1,
                update_time = CURRENT_TIMESTAMP
            WHERE workflow_task_uuid = NEW.uuid
              AND status = 'active'
              AND deleted_at IS NULL;
        END;
        """,
    )


def ensure_execution_lock_schema(connection: sqlite3.Connection) -> None:
    """补齐本地作业执行占用（ExecutionLockLease）与等待事实。

    参数：``connection`` 是 ``WorkflowStore`` 初始化事务持有的唯一写连接。
    返回无；函数幂等补充作业等待原因，并创建执行占用与公平等待表。异常由
    调用方回滚初始化事务。

    本地工作流库只保存跨重启安全所需的锁身份和状态；物料与库位实体仍属于
    库存库，因此这里故意不声明跨库外键，也不伪造跨库原子提交能力。
    """

    job_columns = {
        str(row["name"])
        for row in connection.execute(
            "PRAGMA table_info(workflow_node_job)"
        ).fetchall()
    }
    if "wait_reason" not in job_columns:
        connection.execute(
            """
            ALTER TABLE workflow_node_job
            ADD COLUMN wait_reason TEXT NOT NULL DEFAULT '{}'
            """
        )
    if "dispatch_effect_uuid" not in job_columns:
        connection.execute(
            "ALTER TABLE workflow_node_job ADD COLUMN dispatch_effect_uuid TEXT"
        )
    if "dispatch_parameter_hash" not in job_columns:
        connection.execute(
            "ALTER TABLE workflow_node_job ADD COLUMN dispatch_parameter_hash TEXT"
        )
    if "expected_change_set" not in job_columns:
        connection.execute(
            "ALTER TABLE workflow_node_job "
            "ADD COLUMN expected_change_set TEXT NOT NULL DEFAULT '{}'"
        )
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_workflow_node_job_dispatch_effect
        ON workflow_node_job(dispatch_effect_uuid)
        WHERE dispatch_effect_uuid IS NOT NULL
        """
    )

    _execute_script_in_transaction(
        connection,
        """
        CREATE TABLE IF NOT EXISTS execution_lock_lease (
            uuid TEXT PRIMARY KEY,
            create_time TEXT NOT NULL,
            update_time TEXT NOT NULL,
            deleted_at TEXT,
            description TEXT,
            meta_data TEXT NOT NULL DEFAULT '{}',
            workflow_task_uuid TEXT NOT NULL,
            workflow_node_job_uuid TEXT NOT NULL,
            lock_key TEXT NOT NULL,
            scope TEXT NOT NULL
                CHECK (scope IN ('device', 'material', 'material_site', 'resource')),
            material_uuid TEXT,
            site_uuid TEXT,
            state TEXT NOT NULL
                CHECK (state IN ('reserved', 'running', 'released', 'uncertain')),
            acquired_at TEXT NOT NULL,
            released_at TEXT,
            CHECK (
                (state IN ('reserved', 'running', 'uncertain')
                    AND released_at IS NULL)
                OR (state = 'released' AND released_at IS NOT NULL)
            ),
            FOREIGN KEY(workflow_task_uuid) REFERENCES workflow_task(uuid),
            FOREIGN KEY(workflow_node_job_uuid) REFERENCES workflow_node_job(uuid)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_execution_lock_lease_active_key
            ON execution_lock_lease(lock_key)
            WHERE deleted_at IS NULL
              AND state IN ('reserved', 'running', 'uncertain');
        CREATE UNIQUE INDEX IF NOT EXISTS ux_execution_lock_lease_active_job_key
            ON execution_lock_lease(workflow_node_job_uuid, lock_key)
            WHERE deleted_at IS NULL
              AND state IN ('reserved', 'running', 'uncertain');
        CREATE INDEX IF NOT EXISTS ix_execution_lock_lease_job_state
            ON execution_lock_lease(workflow_node_job_uuid, state, lock_key);

        CREATE TABLE IF NOT EXISTS execution_lock_waiter (
            uuid TEXT PRIMARY KEY,
            create_time TEXT NOT NULL,
            update_time TEXT NOT NULL,
            deleted_at TEXT,
            description TEXT,
            meta_data TEXT NOT NULL DEFAULT '{}',
            workflow_task_uuid TEXT NOT NULL,
            workflow_node_job_uuid TEXT NOT NULL,
            lock_key TEXT NOT NULL,
            scope TEXT NOT NULL
                CHECK (scope IN ('device', 'material', 'material_site', 'resource')),
            material_uuid TEXT,
            site_uuid TEXT,
            state TEXT NOT NULL CHECK (state IN ('waiting', 'released')),
            enqueued_at TEXT NOT NULL,
            released_at TEXT,
            CHECK (
                (state = 'waiting' AND released_at IS NULL)
                OR (state = 'released' AND released_at IS NOT NULL)
            ),
            FOREIGN KEY(workflow_task_uuid) REFERENCES workflow_task(uuid),
            FOREIGN KEY(workflow_node_job_uuid) REFERENCES workflow_node_job(uuid)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_execution_lock_waiter_active_job_key
            ON execution_lock_waiter(workflow_node_job_uuid, lock_key)
            WHERE deleted_at IS NULL AND state = 'waiting';
        CREATE INDEX IF NOT EXISTS ix_execution_lock_waiter_fairness
            ON execution_lock_waiter(enqueued_at, workflow_task_uuid,
                                     workflow_node_job_uuid, lock_key)
            WHERE deleted_at IS NULL AND state = 'waiting';

        CREATE TABLE IF NOT EXISTS execution_claim (
            claim_uuid TEXT PRIMARY KEY,
            create_time TEXT NOT NULL,
            update_time TEXT NOT NULL,
            workflow_task_uuid TEXT NOT NULL,
            workflow_node_job_uuid TEXT NOT NULL,
            attempt INTEGER NOT NULL CHECK (attempt > 0),
            resource_keys TEXT NOT NULL,
            state TEXT NOT NULL
                CHECK (state IN ('reserved', 'running', 'released', 'uncertain')),
            acquired_at TEXT NOT NULL,
            released_at TEXT,
            UNIQUE(workflow_node_job_uuid, attempt),
            FOREIGN KEY(workflow_task_uuid) REFERENCES workflow_task(uuid),
            FOREIGN KEY(workflow_node_job_uuid) REFERENCES workflow_node_job(uuid)
        );
        CREATE INDEX IF NOT EXISTS ix_execution_claim_job_state
            ON execution_claim(workflow_node_job_uuid, state);

        CREATE TABLE IF NOT EXISTS execution_fence_counter (
            lock_key TEXT PRIMARY KEY,
            last_fencing_token INTEGER NOT NULL
                CHECK (last_fencing_token > 0),
            update_time TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS task_device_tenancy (
            uuid TEXT PRIMARY KEY,
            create_time TEXT NOT NULL,
            update_time TEXT NOT NULL,
            workflow_task_uuid TEXT NOT NULL,
            material_uuid TEXT NOT NULL,
            device_lock_key TEXT NOT NULL,
            acquired_by_job_uuid TEXT NOT NULL,
            released_by_job_uuid TEXT,
            state TEXT NOT NULL CHECK (state IN ('active', 'released')),
            acquired_at TEXT NOT NULL,
            released_at TEXT,
            CHECK (
                (state = 'active' AND released_at IS NULL
                    AND released_by_job_uuid IS NULL)
                OR (state = 'released' AND released_at IS NOT NULL
                    AND released_by_job_uuid IS NOT NULL)
            ),
            FOREIGN KEY(workflow_task_uuid) REFERENCES workflow_task(uuid),
            FOREIGN KEY(acquired_by_job_uuid) REFERENCES workflow_node_job(uuid),
            FOREIGN KEY(released_by_job_uuid) REFERENCES workflow_node_job(uuid)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_task_device_tenancy_active_device
            ON task_device_tenancy(device_lock_key)
            WHERE state = 'active';
        DROP INDEX IF EXISTS ux_task_device_tenancy_active_material;
        CREATE INDEX IF NOT EXISTS ix_task_device_tenancy_task_state
            ON task_device_tenancy(workflow_task_uuid, state, device_lock_key);

        CREATE TABLE IF NOT EXISTS job_device_tenancy_transition (
            workflow_node_job_uuid TEXT PRIMARY KEY,
            workflow_task_uuid TEXT NOT NULL,
            material_uuid TEXT NOT NULL,
            acquire_device_lock_key TEXT,
            release_device_lock_key TEXT,
            status TEXT NOT NULL
                CHECK (status IN ('prepared', 'settled', 'retained', 'reverted')),
            create_time TEXT NOT NULL,
            update_time TEXT NOT NULL,
            settled_at TEXT,
            CHECK (
                acquire_device_lock_key IS NOT NULL
                OR release_device_lock_key IS NOT NULL
            ),
            FOREIGN KEY(workflow_node_job_uuid) REFERENCES workflow_node_job(uuid),
            FOREIGN KEY(workflow_task_uuid) REFERENCES workflow_task(uuid)
        );
        CREATE INDEX IF NOT EXISTS ix_job_device_tenancy_transition_task_status
            ON job_device_tenancy_transition(workflow_task_uuid, status);

        CREATE TRIGGER IF NOT EXISTS trg_release_inactive_execution_lock_waiters
        AFTER UPDATE OF status ON workflow_node_job
        FOR EACH ROW
        WHEN NEW.status <> 'pending' AND OLD.status IS NOT NEW.status
        BEGIN
            UPDATE execution_lock_waiter
            SET state = 'released',
                released_at = CURRENT_TIMESTAMP,
                update_time = CURRENT_TIMESTAMP
            WHERE workflow_node_job_uuid = NEW.uuid
              AND state = 'waiting'
              AND deleted_at IS NULL;
        END;
        """,
    )
    if "release_tenancy_uuid" not in {row[1] for row in connection.execute("PRAGMA table_info(job_device_tenancy_transition)")}:
        connection.execute("ALTER TABLE job_device_tenancy_transition ADD COLUMN release_tenancy_uuid TEXT")
    from unilabos.persistence.migrations import _backfill_release_tenancy_uuid

    _backfill_release_tenancy_uuid(connection)
    _ensure_execution_lock_resource_scope(connection)
    _backfill_execution_lock_identities(connection)
    lease_columns = {
        str(row["name"])
        for row in connection.execute(
            "PRAGMA table_info(execution_lock_lease)"
        ).fetchall()
    }
    if "claim_uuid" not in lease_columns:
        connection.execute(
            "ALTER TABLE execution_lock_lease ADD COLUMN claim_uuid TEXT"
        )
    if "fencing_token" not in lease_columns:
        connection.execute(
            "ALTER TABLE execution_lock_lease ADD COLUMN fencing_token INTEGER"
        )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_execution_lock_lease_claim
        ON execution_lock_lease(claim_uuid, fencing_token)
        """
    )
    _execute_script_in_transaction(
        connection,
        """
        CREATE TABLE IF NOT EXISTS execution_lock_operator_action (
            uuid TEXT PRIMARY KEY,
            create_time TEXT NOT NULL,
            update_time TEXT NOT NULL,
            workflow_task_uuid TEXT NOT NULL,
            workflow_node_job_uuid TEXT NOT NULL,
            lease_uuid TEXT NOT NULL,
            claim_uuid TEXT NOT NULL,
            expected_claim_uuid TEXT NOT NULL,
            expected_fencing_token INTEGER NOT NULL CHECK (expected_fencing_token > 0),
            action TEXT NOT NULL CHECK (action IN ('force_release')),
            result TEXT NOT NULL CHECK (result IN ('released', 'already_released')),
            reason TEXT NOT NULL,
            physical_settlement_confirmed INTEGER NOT NULL
                CHECK (physical_settlement_confirmed IN (0, 1)),
            released_lock_uuids TEXT NOT NULL DEFAULT '[]',
            meta_data TEXT NOT NULL DEFAULT '{}',
            FOREIGN KEY(workflow_task_uuid) REFERENCES workflow_task(uuid),
            FOREIGN KEY(workflow_node_job_uuid) REFERENCES workflow_node_job(uuid),
            FOREIGN KEY(lease_uuid) REFERENCES execution_lock_lease(uuid)
        );
        CREATE INDEX IF NOT EXISTS ix_execution_lock_operator_action_task
            ON execution_lock_operator_action(workflow_task_uuid, create_time, uuid);
        """,
    )


def _backfill_execution_lock_identities(connection: sqlite3.Connection) -> None:
    """由规范键补齐旧 Lease 的冗余身份，并拒绝损坏的活动事实。"""

    from unilabos.persistence.migrations import backfill_execution_lock_identities

    backfill_execution_lock_identities(connection)


def _ensure_execution_lock_resource_scope(connection: sqlite3.Connection) -> None:
    """原地放宽旧锁表 scope CHECK，保持行、索引、触发器与反向外键不变。"""

    tables = ("execution_lock_lease", "execution_lock_waiter")
    old = "scope IN ('device', 'material', 'material_site')"
    new = "scope IN ('device', 'material', 'material_site', 'resource')"
    legacy_tables: list[str] = []
    for table in tables:
        row = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        sql = str(row["sql"] or "") if row is not None else ""
        if new in sql and old not in sql:
            continue
        if sql.count(old) == 1 and new not in sql:
            legacy_tables.append(table)
            continue
        raise sqlite3.OperationalError(f"{table} 定义无法安全增加通用资源 scope")
    if legacy_tables:
        schema_version = int(connection.execute("PRAGMA schema_version").fetchone()[0])
        connection.execute("PRAGMA writable_schema = ON")
        try:
            for table in legacy_tables:
                cursor = connection.execute(
                    "UPDATE sqlite_schema SET sql=replace(sql, ?, ?) "
                    "WHERE type='table' AND name=? AND instr(sql, ?) > 0",
                    (old, new, table, old),
                )
                if cursor.rowcount != 1:
                    raise sqlite3.OperationalError(f"{table} 通用资源 scope 迁移未命中")
            connection.execute(f"PRAGMA schema_version = {schema_version + 1}")
        finally:
            connection.execute("PRAGMA writable_schema = OFF")
    for table in tables:
        row = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        if row is None or new not in str(row["sql"] or ""):
            raise sqlite3.OperationalError(f"{table} 通用资源 scope 迁移未生效")
    integrity = connection.execute("PRAGMA integrity_check").fetchone()
    if integrity is None or str(integrity[0]).lower() != "ok":
        raise sqlite3.IntegrityError("执行锁 scope 迁移后完整性检查失败")
    if connection.execute("PRAGMA foreign_key_check").fetchall():
        raise sqlite3.IntegrityError("执行锁 scope 迁移后外键检查失败")


def ensure_workflow_runtime_journal_schema(connection: sqlite3.Connection) -> None:
    """为运行日志增加人工执行锁释放事件类型，并兼容既有数据库。"""

    row = connection.execute(
        "SELECT sql FROM sqlite_schema "
        "WHERE type = 'table' AND name = 'workflow_runtime_journal'"
    ).fetchone()
    table_sql = str(row["sql"] or "") if row is not None else ""
    if "'lock_operator_released'" in table_sql:
        return
    old = "'uncertainty_resolved',\n            'startup_recovered'"
    new = "'uncertainty_resolved',\n            'lock_operator_released',\n            'startup_recovered'"
    if old not in table_sql:
        raise sqlite3.OperationalError(
            "workflow_runtime_journal 定义无法安全增加人工锁事件类型"
        )
    schema_version = int(connection.execute("PRAGMA schema_version").fetchone()[0])
    connection.execute("PRAGMA writable_schema = ON")
    try:
        connection.execute(
            """
            UPDATE sqlite_schema
            SET sql = replace(sql, ?, ?)
            WHERE type = 'table' AND name = 'workflow_runtime_journal'
            """,
            (old, new),
        )
        connection.execute(f"PRAGMA schema_version = {schema_version + 1}")
    finally:
        connection.execute("PRAGMA writable_schema = OFF")
    refreshed = connection.execute(
        "SELECT sql FROM sqlite_schema "
        "WHERE type = 'table' AND name = 'workflow_runtime_journal'"
    ).fetchone()
    if refreshed is None or "'lock_operator_released'" not in str(refreshed["sql"]):
        raise sqlite3.OperationalError(
            "workflow_runtime_journal 人工锁事件类型迁移未生效"
        )


def ensure_local_cancellation_schema(connection: sqlite3.Connection) -> None:
    """补齐 Local 模式设备取消受理事实与超时扫描索引。

    参数：``connection`` 是 ``WorkflowStore`` 初始化事务持有的唯一写连接。
    返回无；函数幂等增加 ``cancel_accepted_at`` 并建立待取消作业截止时间索引。
    异常由调用方回滚初始化事务。该字段只表示本地执行器已受理停止请求，不表示
    设备已经安全停止；最终结算仍以作业终态为准。
    """

    job_columns = {
        str(row["name"])
        for row in connection.execute(
            "PRAGMA table_info(workflow_node_job)"
        ).fetchall()
    }
    if "cancel_accepted_at" not in job_columns:
        connection.execute(
            "ALTER TABLE workflow_node_job ADD COLUMN cancel_accepted_at TEXT"
        )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_workflow_node_job_local_cancel_deadline
        ON workflow_node_job(
            cancel_ack_deadline_at,
            cancel_complete_deadline_at,
            uuid
        )
        WHERE deleted_at IS NULL AND status = 'cancel_requested'
        """
    )


def ensure_workflow_inventory_schema(connection: sqlite3.Connection) -> None:
    """创建逻辑库存需求、运行分配投影与跨库 Saga 状态。

    工作流库只保存定义和审计投影；试剂/当前内容物的数量仍由
    ``inventory.db`` 裁决。``workflow_inventory_saga`` 是两个 SQLite
    事务域之间的可重放意图，不伪装跨库原子事务。
    """

    _execute_script_in_transaction(
        connection,
        """
        CREATE TABLE IF NOT EXISTS workflow_inventory_requirement (
            uuid TEXT PRIMARY KEY,
            create_time TEXT NOT NULL,
            update_time TEXT NOT NULL,
            deleted_at TEXT,
            description TEXT,
            meta_data TEXT NOT NULL DEFAULT '{}',
            workflow_uuid TEXT NOT NULL,
            consume_node_uuid TEXT NOT NULL,
            requirement_key TEXT NOT NULL,
            target_type TEXT NOT NULL
                CHECK (target_type IN ('reagent_info', 'current_substance')),
            reagent_info_uuid TEXT,
            required_quantity REAL NOT NULL CHECK (required_quantity > 0),
            quantity_unit TEXT NOT NULL CHECK (length(trim(quantity_unit)) > 0),
            allow_split INTEGER NOT NULL DEFAULT 0 CHECK (allow_split IN (0, 1)),
            sort_order INTEGER NOT NULL DEFAULT 0 CHECK (sort_order >= 0),
            CHECK (
                (target_type = 'reagent_info' AND reagent_info_uuid IS NOT NULL)
                OR (target_type = 'current_substance' AND reagent_info_uuid IS NULL)
            ),
            FOREIGN KEY(workflow_uuid) REFERENCES workflow(uuid),
            FOREIGN KEY(consume_node_uuid) REFERENCES workflow_node(uuid)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_workflow_inventory_requirement_active
            ON workflow_inventory_requirement(workflow_uuid, requirement_key)
            WHERE deleted_at IS NULL;
        CREATE INDEX IF NOT EXISTS ix_workflow_inventory_requirement_node
            ON workflow_inventory_requirement(consume_node_uuid, sort_order, uuid)
            WHERE deleted_at IS NULL;

        CREATE TABLE IF NOT EXISTS workflow_inventory_allocation (
            uuid TEXT PRIMARY KEY,
            workflow_task_uuid TEXT NOT NULL,
            workflow_node_job_uuid TEXT NOT NULL,
            requirement_key TEXT NOT NULL,
            inventory_type TEXT NOT NULL
                CHECK (inventory_type IN ('reagent', 'current_substance')),
            inventory_uuid TEXT NOT NULL,
            material_uuid TEXT NOT NULL,
            reserved_quantity REAL NOT NULL CHECK (reserved_quantity > 0),
            quantity_unit TEXT NOT NULL CHECK (length(trim(quantity_unit)) > 0),
            status TEXT NOT NULL
                CHECK (status IN ('reserved', 'consumed', 'released')),
            revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
            reserved_at TEXT NOT NULL,
            consumed_at TEXT,
            released_at TEXT,
            CHECK (
                (status = 'reserved' AND consumed_at IS NULL AND released_at IS NULL)
                OR (status = 'consumed' AND consumed_at IS NOT NULL AND released_at IS NULL)
                OR (status = 'released' AND consumed_at IS NULL AND released_at IS NOT NULL)
            ),
            FOREIGN KEY(workflow_task_uuid) REFERENCES workflow_task(uuid),
            FOREIGN KEY(workflow_node_job_uuid) REFERENCES workflow_node_job(uuid)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_workflow_inventory_allocation_subject
            ON workflow_inventory_allocation(
                workflow_task_uuid, requirement_key, inventory_type, inventory_uuid
            );
        CREATE INDEX IF NOT EXISTS ix_workflow_inventory_allocation_task_status
            ON workflow_inventory_allocation(workflow_task_uuid, status, uuid);
        CREATE INDEX IF NOT EXISTS ix_workflow_inventory_allocation_job_status
            ON workflow_inventory_allocation(workflow_node_job_uuid, status, uuid);

        CREATE TABLE IF NOT EXISTS workflow_inventory_saga (
            workflow_task_uuid TEXT PRIMARY KEY,
            status TEXT NOT NULL CHECK (
                status IN (
                    'reserve_pending', 'reserved', 'consume_pending',
                    'release_pending', 'settled', 'compensation_pending'
                )
            ),
            operation_key TEXT NOT NULL,
            payload TEXT NOT NULL DEFAULT '{}',
            last_error TEXT,
            attempt INTEGER NOT NULL DEFAULT 1 CHECK (attempt > 0),
            update_time TEXT NOT NULL,
            FOREIGN KEY(workflow_task_uuid) REFERENCES workflow_task(uuid)
        );
        CREATE INDEX IF NOT EXISTS ix_workflow_inventory_saga_status
            ON workflow_inventory_saga(status, update_time, workflow_task_uuid);
        """,
    )


__all__ = [
    "ensure_device_action_run_schema",
    "ensure_execution_lock_schema",
    "ensure_local_cancellation_schema",
    "ensure_task_material_admission_schema",
    "ensure_task_resource_unlock_command_schema",
    "drop_empty_workflow_definition_tables",
    "ensure_workflow_runtime_journal_schema",
    "ensure_workflow_task_debug_configuration_schema",
    "ensure_workflow_task_control_schema",
    "ensure_workflow_inventory_schema",
]
