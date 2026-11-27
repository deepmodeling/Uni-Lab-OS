"""任务列表的小型持久投影；定义版本只随冻结图内容变化。"""

from __future__ import annotations

from typing import Any

from unilabos.persistence.database import PostgreSQLDatabase

_SCHEMA = """CREATE TABLE IF NOT EXISTS workflow_task_read_model (
    task_uuid TEXT PRIMARY KEY REFERENCES workflow_task(uuid) ON DELETE CASCADE,
    definition_version INTEGER NOT NULL DEFAULT 1,
    workflow_name TEXT NOT NULL DEFAULT ''
)"""


def initialize_task_read_model(connection: Any) -> None:
    """在业务事务内维护版本与名称；旧任务只回填一次，不读取图到 Python。"""
    connection.execute(_SCHEMA)
    name = "COALESCE(json_extract(NEW.workflow_snapshot, '$.workflow.name'), '')"
    triggers = (
        f"""CREATE TRIGGER IF NOT EXISTS task_read_model_insert
        AFTER INSERT ON workflow_task BEGIN
        INSERT INTO workflow_task_read_model(task_uuid, definition_version, workflow_name)
        VALUES (NEW.uuid, 1, {name}); END;""",
        f"""CREATE TRIGGER IF NOT EXISTS task_read_model_definition_update
        AFTER UPDATE OF workflow_snapshot, execution_plan ON workflow_task
        WHEN OLD.workflow_snapshot IS NOT NEW.workflow_snapshot
          OR OLD.execution_plan IS NOT NEW.execution_plan BEGIN
        UPDATE workflow_task_read_model
        SET definition_version=definition_version+1, workflow_name={name}
        WHERE task_uuid=NEW.uuid; END;""",
    )
    for statement in triggers:
        if isinstance(connection, PostgreSQLDatabase):
            from unilabos.persistence.schema import trigger_sql

            connection._connection.execute(trigger_sql(statement))
        else:
            connection.execute(statement)
    connection.execute("""
        INSERT INTO workflow_task_read_model(task_uuid, definition_version, workflow_name)
        SELECT uuid, 1, COALESCE(json_extract(workflow_snapshot, '$.workflow.name'), '')
        FROM workflow_task WHERE NOT EXISTS (
            SELECT 1 FROM workflow_task_read_model WHERE task_uuid=workflow_task.uuid
        )
    """)


def task_filter_sql(
    *, workflow_uuid: str | None = None, execution_kind: str = "",
    status: str = "", cleanup_status: str = "", prefix: str = "",
) -> tuple[str, list[Any]]:
    """计数、摘要和旧列表使用相同的软删除及业务筛选。"""
    clauses = [f"{prefix}deleted_at IS NULL"]
    values: list[Any] = []
    for field, value in (
        ("workflow_uuid", workflow_uuid), ("execution_kind", execution_kind),
        ("status", status), ("cleanup_status", cleanup_status),
    ):
        if value:
            clauses.append(f"{prefix}{field} = ?")
            values.append(value)
    return " AND ".join(clauses), values


def task_projection_sql(*, state: bool) -> str:
    """只选择显示状态；摘要不带输入输出，任何模式都不读取冻结大图。"""
    columns = (
        "uuid", "create_time", "update_time", "description", "meta_data",
        "workflow_uuid", "execution_kind", "priority", "status", "run_mode",
        "execution_mode", "dispatch_gate", "control_status", "cleanup_status",
        "wait_reason", "target_node_uuid", "timeout_at", "attention_reason",
        "terminal_ghost_detected_at", "reconciliation_resume_control_status",
        "started_at", "finished_at", "backend_task_uuid", "invocation_key",
        "revision_fingerprint",
    )
    selected = [f"task.{column}" for column in columns]
    selected.extend(
        f"task.{column}" if state else f"'{fallback}' AS {column}"
        for column, fallback in (
            ("trace_context", "{}"), ("input", "{}"),
            ("output", "{}"), ("error_info", "[]"),
        )
    )
    selected.extend((
        "'{}' AS workflow_snapshot", "'{}' AS execution_plan",
        "model.definition_version", "model.workflow_name",
    ))
    return "SELECT " + ", ".join(selected) + """
        FROM workflow_task AS task
        JOIN workflow_task_read_model AS model ON model.task_uuid=task.uuid
    """


def task_job_counts(connection: Any, identities: list[str]) -> dict[str, dict[str, int]]:
    """一次聚合本页 Job；与现有展示列表一样保留历史尝试的完成统计。"""
    if not identities:
        return {}
    placeholders = ",".join("?" for _ in identities)
    rows = connection.execute(f"""
        SELECT workflow_task_uuid, COUNT(*) AS total,
            SUM(CASE WHEN status IN ('succeeded','failed','canceled','timeout','skipped') THEN 1 ELSE 0 END) AS completed,
            SUM(CASE WHEN status='succeeded' THEN 1 ELSE 0 END) AS succeeded,
            SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed,
            SUM(CASE WHEN status='canceled' THEN 1 ELSE 0 END) AS canceled
        FROM workflow_node_job
        WHERE deleted_at IS NULL AND workflow_task_uuid IN ({placeholders})
        GROUP BY workflow_task_uuid
    """, identities).fetchall()
    return {
        row["workflow_task_uuid"]: {
            key: int(row[key] or 0)
            for key in ("total", "completed", "succeeded", "failed", "canceled")
        }
        for row in rows
    }
