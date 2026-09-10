"""本地工作流存储（Workflow Store）的增量 Schema 迁移。"""

from __future__ import annotations

import sqlite3


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


__all__ = ["ensure_device_action_run_schema"]
