"""Fail-closed inspection for destructive Local Domain state rebuilds."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from unilabos.persistence.database import open_readonly_database
from unilabos.persistence.inspection import table_exists
from unilabos.persistence.workspace import use_workspace_database

from .model import WorkspacePaths

_TERMINAL_WORKFLOW_TASK_STATUSES = frozenset(
    {"succeeded", "failed", "canceled", "timeout"}
)
_TERMINAL_EDGE_RUNTIME_STATUSES = frozenset(
    {"completed", "outcome_committed", "outcome_retired"}
)
# 本地 Edge 权威在这些状态下已经写入终态结果；只有仍带 UNKNOWN 命令时才算未收敛。
_CONVERGED_LOCAL_EDGE_JOB_STATUSES = frozenset({"failed"})


class LocalResetInspectionError(RuntimeError):
    """The safety facts could not be read, so reset must fail closed."""


@dataclass(frozen=True)
class LocalResetBlocker:
    source: str
    kind: str
    identity: str
    status: str
    unknown_command_ids: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "source": self.source,
            "kind": self.kind,
            "identity": self.identity,
            "status": self.status,
        }
        if self.unknown_command_ids:
            payload["unknownCommandIds"] = list(self.unknown_command_ids)
        return payload


def inspect_local_reset_blockers(paths: WorkspacePaths) -> list[LocalResetBlocker]:
    """Return every durable fact that makes ``local.reset-state`` unsafe.

    The inspector never creates a database and never mutates live state. Missing
    databases mean that component has no durable facts yet; an unreadable or
    incompatible existing database fails closed via ``LocalResetInspectionError``.
    """

    with use_workspace_database(paths.workspace):
        return _inspect_local_reset_blockers(paths)


def _inspect_local_reset_blockers(paths: WorkspacePaths) -> list[LocalResetBlocker]:
    local_domain = paths.runtime / "backend" / "local-domain"
    try:
        blockers = [
            *_workflow_task_blockers(local_domain / "workflow_history.db"),
            *_local_edge_authority_blockers(local_domain / "edge_authority.db"),
            *_edge_runtime_blockers(paths.runtime / "edge" / "edge_control.db"),
        ]
    except sqlite3.Error as error:
        raise LocalResetInspectionError(
            f"无法检查本地状态是否安全：{error}"
        ) from error
    return sorted(
        blockers,
        key=lambda item: (item.source, item.kind, item.identity, item.status),
    )


def _workflow_task_blockers(path: Path) -> list[LocalResetBlocker]:
    with _read_database(path) as connection:
        if connection is None or not _table_exists(connection, "workflow_task"):
            return []
        placeholders = ",".join("?" for _ in _TERMINAL_WORKFLOW_TASK_STATUSES)
        rows = connection.execute(
            f"""
            SELECT uuid, status FROM workflow_task
            WHERE deleted_at IS NULL AND status NOT IN ({placeholders})
            ORDER BY create_time, uuid
            """,
            tuple(sorted(_TERMINAL_WORKFLOW_TASK_STATUSES)),
        ).fetchall()
    return [
        LocalResetBlocker(
            source="local-domain",
            kind="workflow-task",
            identity=str(row["uuid"]),
            status=str(row["status"]),
        )
        for row in rows
    ]


def _local_edge_authority_blockers(path: Path) -> list[LocalResetBlocker]:
    with _read_database(path) as connection:
        if connection is None or not _table_exists(connection, "local_edge_job"):
            return []
        rows = connection.execute(
            """
            SELECT job_uuid, status, unknown_command_ids_json
            FROM local_edge_job
            WHERE status != 'completed'
            ORDER BY updated_at, job_uuid
            """
        ).fetchall()
    blockers: list[LocalResetBlocker] = []
    for row in rows:
        unknown_command_ids = _unknown_command_ids(
            row["unknown_command_ids_json"], path
        )
        status = str(row["status"])
        # ``failed`` 是本地 Edge 权威记录终态结果后的收敛状态：结果已写入且没有
        # 未对账的 UNKNOWN 命令时，重建可重建状态不会丢失任何在途事实。执行进程
        # 重启会留下这种记录，若继续阻塞则 ``local.reset-state`` 永久不可用。
        # 仍带 UNKNOWN 的失败作业必须继续阻塞，不能靠状态名放过。
        if status in _CONVERGED_LOCAL_EDGE_JOB_STATUSES and not unknown_command_ids:
            continue
        blockers.append(
            LocalResetBlocker(
                source="local-edge-authority",
                kind="edge-job",
                identity=str(row["job_uuid"]),
                status=status,
                unknown_command_ids=unknown_command_ids,
            )
        )
    return blockers


def _edge_runtime_blockers(path: Path) -> list[LocalResetBlocker]:
    with _read_database(path) as connection:
        if connection is None:
            return []
        blockers: list[LocalResetBlocker] = []
        if _table_exists(connection, "edge_job_runtime"):
            placeholders = ",".join("?" for _ in _TERMINAL_EDGE_RUNTIME_STATUSES)
            rows = connection.execute(
                f"""
                SELECT job_uuid, status FROM edge_job_runtime
                WHERE status NOT IN ({placeholders})
                ORDER BY updated_at, job_uuid
                """,
                tuple(sorted(_TERMINAL_EDGE_RUNTIME_STATUSES)),
            ).fetchall()
            blockers.extend(
                LocalResetBlocker(
                    source="edge-runtime",
                    kind="edge-job",
                    identity=str(row["job_uuid"]),
                    status=str(row["status"]),
                )
                for row in rows
            )
        reported_outcomes: set[str] = set()
        runtime_columns = _column_names(connection, "edge_job_runtime")
        if "outcome" in runtime_columns and "unknown_command_ids_json" in runtime_columns:
            order = (
                "outcome_updated_at, job_uuid"
                if "outcome_updated_at" in runtime_columns
                else "job_uuid"
            )
            rows = connection.execute(
                f"""
                SELECT job_uuid, unknown_command_ids_json
                FROM edge_job_runtime
                WHERE outcome IS NOT NULL
                ORDER BY {order}
                """
            ).fetchall()
            for row in rows:
                identity = str(row["job_uuid"])
                reported_outcomes.add(identity)
                unknown_command_ids = _unknown_command_ids(
                    row["unknown_command_ids_json"], path
                )
                blockers.append(
                    LocalResetBlocker(
                        source="edge-runtime",
                        kind="edge-outcome",
                        identity=identity,
                        status="unknown" if unknown_command_ids else "outcome-pending",
                        unknown_command_ids=unknown_command_ids,
                    )
                )
        if _table_exists(connection, "edge_job_outcome_pending"):
            rows = connection.execute(
                """
                SELECT job_uuid, unknown_command_ids_json
                FROM edge_job_outcome_pending
                ORDER BY updated_at, job_uuid
                """
            ).fetchall()
            for row in rows:
                identity = str(row["job_uuid"])
                if identity in reported_outcomes:
                    continue
                unknown_command_ids = _unknown_command_ids(
                    row["unknown_command_ids_json"], path
                )
                blockers.append(
                    LocalResetBlocker(
                        source="edge-runtime",
                        kind="edge-outcome",
                        identity=identity,
                        status="unknown" if unknown_command_ids else "outcome-pending",
                        unknown_command_ids=unknown_command_ids,
                    )
                )
        if _table_exists(connection, "edge_event_outbox"):
            rows = connection.execute(
                """
                SELECT event_uuid, type FROM edge_event_outbox
                WHERE acked_at IS NULL
                ORDER BY created_at, event_uuid
                """
            ).fetchall()
            blockers.extend(
                LocalResetBlocker(
                    source="edge-runtime",
                    kind="edge-event",
                    identity=str(row["event_uuid"]),
                    status=f"unacknowledged:{row['type']}",
                )
                for row in rows
            )
        return blockers


class _ReadDatabase:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.connection: sqlite3.Connection | None = None

    def __enter__(self) -> sqlite3.Connection | None:
        try:
            self.connection = open_readonly_database(self.path)
            return self.connection
        except sqlite3.Error as error:
            raise LocalResetInspectionError(
                f"无法读取本地状态库 {self.path}: {error}"
            ) from error

    def __exit__(self, *_args: Any) -> None:
        if self.connection is not None:
            self.connection.close()


def _read_database(path: Path) -> _ReadDatabase:
    return _ReadDatabase(path)


def _column_names(connection: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(connection, table):
        return set()
    return {
        str(row[1])
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    try:
        return table_exists(connection, table)
    except sqlite3.Error as error:
        raise LocalResetInspectionError(f"无法检查本地状态表 {table}: {error}") from error


def _unknown_command_ids(value: object, path: Path) -> tuple[str, ...]:
    try:
        decoded = json.loads(str(value or "[]"))
    except (TypeError, ValueError) as error:
        raise LocalResetInspectionError(
            f"本地状态库 {path} 的 UNKNOWN 命令不是合法 JSON"
        ) from error
    if not isinstance(decoded, list) or any(not isinstance(item, str) for item in decoded):
        raise LocalResetInspectionError(
            f"本地状态库 {path} 的 UNKNOWN 命令格式无效"
        )
    return tuple(decoded)
