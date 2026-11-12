"""V1 任务生命周期新增的持久事实（runtime.db v9）。

- ``workflow_human_interaction``：设备 / 工作流命令向操作员发起的结构化人工交互
  （choose / text / numbers / sketch / review）及其回复，V1 ``human_commands.py`` 同形。

``workflow_task`` 的 ``priority`` / ``inventory_bindings`` / ``dispatch_gate`` 三列与
``workflow_task_command`` 新增的 ``switch_to_step`` / ``unlock_resources`` 类型在
``tables/runtime/workflow.py`` 的表规格里，迁移见 ``migrations.py`` 的 v9。
"""

from __future__ import annotations

from typing import Any, ClassVar, Optional

from sqlmodel import Field

from unilabos.server.database.schema import TableSpec
from unilabos.server.database.tables.base import JsonObject, NonEmptyStr, TableObject, json_text_column


class WorkflowHumanInteractionRecord(TableObject, table=True):
    __tablename__: ClassVar[str] = "workflow_human_interaction"

    uuid: NonEmptyStr = Field(primary_key=True)
    job_uuid: NonEmptyStr
    task_uuid: NonEmptyStr
    kind: NonEmptyStr
    arguments: JsonObject = Field(sa_column=json_text_column("arguments", default_json="{}"))
    status: NonEmptyStr
    result: Optional[Any] = Field(default=None, sa_column=json_text_column("result", default_json="null", nullable=True))
    opened_at: NonEmptyStr
    deadline_at: NonEmptyStr
    decided_at: Optional[str] = None


#: v9 冻结的建表语句：迁移与当前规格共用（之后演进时迁移里改为冻结副本）。
HUMAN_INTERACTION_V9 = """
        CREATE TABLE IF NOT EXISTS workflow_human_interaction (
            uuid TEXT PRIMARY KEY,
            job_uuid TEXT NOT NULL UNIQUE,
            task_uuid TEXT NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('choose', 'text', 'numbers', 'sketch', 'review')),
            arguments TEXT NOT NULL CHECK (json_valid(arguments) AND json_type(arguments) = 'object'),
            status TEXT NOT NULL CHECK (status IN ('pending', 'answered', 'timed_out', 'canceled')),
            result TEXT CHECK (result IS NULL OR json_valid(result)),
            opened_at TEXT NOT NULL,
            deadline_at TEXT NOT NULL,
            decided_at TEXT,
            FOREIGN KEY(job_uuid) REFERENCES workflow_node_job(uuid),
            FOREIGN KEY(task_uuid) REFERENCES workflow_task(uuid)
        )
        """
HUMAN_INTERACTION_V9_INDEXES = (
    """
            CREATE INDEX IF NOT EXISTS idx_workflow_human_interaction_task
            ON workflow_human_interaction(task_uuid, opened_at)
            """,
)

TASK_LIFECYCLE_MODELS = (WorkflowHumanInteractionRecord,)
TASK_LIFECYCLE_TABLES = (
    TableSpec("workflow_human_interaction", HUMAN_INTERACTION_V9, HUMAN_INTERACTION_V9_INDEXES),
)

__all__ = [
    "HUMAN_INTERACTION_V9",
    "HUMAN_INTERACTION_V9_INDEXES",
    "TASK_LIFECYCLE_MODELS",
    "TASK_LIFECYCLE_TABLES",
    "WorkflowHumanInteractionRecord",
]
