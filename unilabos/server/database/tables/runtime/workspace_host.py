"""V1 Workspace Host 的持久事实（runtime.db）：工作区操作记录与快照修订号。

V1 把每个操作写成 ``.unilabos/runtime/workbench/operations/<id>.json``、修订号只在内存里；
dev 的监督者是常驻的调度权威进程，操作记录与修订号落 runtime.db：

- ``workspace_host_operation``：``operationId`` 幂等记录（请求指纹、阶段、结果 / 错误），
  ``progress`` 挂接 dev 已有事实（驱动包操作台账的 operation_id、重启协调器代次等），
  ``preview_id`` 供 ``material.layout.apply`` 找回对应的 preview；
- ``workspace_host_state``：单行，快照 ``revision`` 与上次观测到的进程状态指纹。
"""

from __future__ import annotations

from typing import Any, ClassVar, Optional

from sqlmodel import Field

from unilabos.server.database.schema import TableSpec
from unilabos.server.database.tables.base import JsonObject, NonEmptyStr, TableObject, json_text_column


class WorkspaceHostOperationRecord(TableObject, table=True):
    __tablename__: ClassVar[str] = "workspace_host_operation"
    operation_id: NonEmptyStr = Field(primary_key=True)
    command: NonEmptyStr
    parameters: JsonObject = Field(sa_column=json_text_column("parameters", default_json="{}"))
    expected_revision: Optional[int] = None
    request_hash: NonEmptyStr
    phase: str = "pending"
    submitted_at: NonEmptyStr
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    progress: JsonObject = Field(sa_column=json_text_column("progress", default_json="{}"))
    result: Any = Field(default=None, sa_column=json_text_column("result", default_json="null", nullable=True))
    error: Optional[JsonObject] = Field(
        default=None, sa_column=json_text_column("error", default_json="null", nullable=True)
    )
    preview_id: Optional[str] = None


class WorkspaceHostStateRecord(TableObject, table=True):
    __tablename__: ClassVar[str] = "workspace_host_state"
    singleton: int = Field(default=1, primary_key=True)
    revision: int = 0
    fingerprint: str = ""
    updated_at: NonEmptyStr


WORKSPACE_HOST_MODELS = (WorkspaceHostOperationRecord, WorkspaceHostStateRecord)
WORKSPACE_HOST_TABLES = (
    TableSpec("workspace_host_operation", """
        CREATE TABLE IF NOT EXISTS workspace_host_operation (
            operation_id TEXT PRIMARY KEY CHECK (length(operation_id) BETWEEN 1 AND 200),
            command TEXT NOT NULL CHECK (length(command) >= 1),
            parameters TEXT NOT NULL CHECK (json_valid(parameters) AND json_type(parameters) = 'object'),
            expected_revision INTEGER,
            request_hash TEXT NOT NULL,
            phase TEXT NOT NULL DEFAULT 'pending' CHECK (phase IN ('pending', 'running', 'succeeded', 'failed')),
            submitted_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            progress TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(progress) AND json_type(progress) = 'object'),
            result TEXT CHECK (result IS NULL OR json_valid(result)),
            error TEXT CHECK (error IS NULL OR (json_valid(error) AND json_type(error) = 'object')),
            preview_id TEXT
        )
    """, (
        """CREATE INDEX IF NOT EXISTS idx_workspace_host_operation_phase ON workspace_host_operation(phase, submitted_at)""",
        """CREATE INDEX IF NOT EXISTS idx_workspace_host_operation_preview ON workspace_host_operation(preview_id) """
        """WHERE preview_id IS NOT NULL""",
    )),
    TableSpec("workspace_host_state", """
        CREATE TABLE IF NOT EXISTS workspace_host_state (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
            fingerprint TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        )
    """),
)

__all__ = [
    "WORKSPACE_HOST_MODELS",
    "WORKSPACE_HOST_TABLES",
    "WorkspaceHostOperationRecord",
    "WorkspaceHostStateRecord",
]
