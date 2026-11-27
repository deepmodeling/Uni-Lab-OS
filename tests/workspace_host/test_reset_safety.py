from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from unilabos.workspace_host.discovery import ensure_local_token
from unilabos.workspace_host.host import WorkspaceHost
from unilabos.workspace_host.model import WorkspaceHostError, WorkspacePaths
from unilabos.workspace_host.reset_safety import (
    LocalResetBlocker,
    LocalResetInspectionError,
    inspect_local_reset_blockers,
)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "deployment" / "graphs").mkdir(parents=True)
    (root / "deployment" / "graphs" / "graph.json").write_text("{}\n")
    (root / "deployment" / "local_config.py").write_text("# fixture\n")
    return root


def _database(path: Path, schema: str) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.executescript(schema)
    return connection


def test_active_workflow_task_blocks_reset_before_processes_stop(
    workspace: Path,
) -> None:
    paths = WorkspacePaths.resolve(workspace)
    connection = _database(
        paths.runtime / "backend" / "local-domain" / "workflow_history.db",
        """
        CREATE TABLE workflow_task(
            uuid TEXT PRIMARY KEY, status TEXT NOT NULL, deleted_at TEXT,
            create_time TEXT NOT NULL
        );
        """,
    )
    connection.execute(
        "INSERT INTO workflow_task VALUES (?, ?, NULL, ?)",
        ("task-active", "running", "2026-08-14T00:00:00Z"),
    )
    connection.commit()
    connection.close()
    host = WorkspaceHost(paths, ensure_local_token(paths), readiness_timeout=0.1)
    stopped: list[str] = []
    host._stop_component = lambda name: stopped.append(name) or {}  # type: ignore[method-assign]

    with pytest.raises(WorkspaceHostError) as raised:
        host._dispatch("local.reset-state", {})

    assert raised.value.code == "local_reset_state_blocked"
    assert raised.value.details == {
        "stage": "before-stop",
        "blockers": [
            {
                "source": "local-domain",
                "kind": "workflow-task",
                "identity": "task-active",
                "status": "running",
            }
        ],
    }
    assert stopped == []
    assert '"event":"local.reset-state.blocked"' in paths.audit.read_text()
    host.close()


def test_unknown_edge_job_blocks_reset_with_command_identity(workspace: Path) -> None:
    paths = WorkspacePaths.resolve(workspace)
    connection = _database(
        paths.runtime / "backend" / "local-domain" / "edge_authority.db",
        """
        CREATE TABLE local_edge_job(
            job_uuid TEXT PRIMARY KEY, status TEXT NOT NULL,
            unknown_command_ids_json TEXT NOT NULL, updated_at REAL NOT NULL
        );
        """,
    )
    connection.execute(
        "INSERT INTO local_edge_job VALUES (?, ?, ?, ?)",
        ("job-unknown", "unknown", json.dumps(["plc-command-7"]), 1.0),
    )
    connection.commit()
    connection.close()

    blockers = inspect_local_reset_blockers(paths)

    assert [blocker.as_dict() for blocker in blockers] == [
        {
            "source": "local-edge-authority",
            "kind": "edge-job",
            "identity": "job-unknown",
            "status": "unknown",
            "unknownCommandIds": ["plc-command-7"],
        }
    ]


def test_failed_edge_job_without_unknown_commands_is_converged(
    workspace: Path,
) -> None:
    """执行进程重启留下的 failed 作业已收敛：结果已写入且没有未对账命令。

    若继续把它当阻塞项，任何一次执行进程重启都会让 ``local.reset-state``
    永久不可用；带 UNKNOWN 的失败作业仍必须阻塞，由下一条用例覆盖。
    """

    paths = WorkspacePaths.resolve(workspace)
    connection = _database(
        paths.runtime / "backend" / "local-domain" / "edge_authority.db",
        """
        CREATE TABLE local_edge_job(
            job_uuid TEXT PRIMARY KEY, status TEXT NOT NULL,
            unknown_command_ids_json TEXT NOT NULL, updated_at REAL NOT NULL
        );
        """,
    )
    connection.execute(
        "INSERT INTO local_edge_job VALUES (?, ?, ?, ?)",
        ("job-restarted", "failed", "[]", 1.0),
    )
    connection.commit()
    connection.close()

    assert inspect_local_reset_blockers(paths) == []


def test_failed_edge_job_with_unknown_commands_still_blocks_reset(
    workspace: Path,
) -> None:
    paths = WorkspacePaths.resolve(workspace)
    connection = _database(
        paths.runtime / "backend" / "local-domain" / "edge_authority.db",
        """
        CREATE TABLE local_edge_job(
            job_uuid TEXT PRIMARY KEY, status TEXT NOT NULL,
            unknown_command_ids_json TEXT NOT NULL, updated_at REAL NOT NULL
        );
        """,
    )
    connection.execute(
        "INSERT INTO local_edge_job VALUES (?, ?, ?, ?)",
        ("job-failed-unknown", "failed", json.dumps(["plc-command-9"]), 1.0),
    )
    connection.commit()
    connection.close()

    assert [blocker.as_dict() for blocker in inspect_local_reset_blockers(paths)] == [
        {
            "source": "local-edge-authority",
            "kind": "edge-job",
            "identity": "job-failed-unknown",
            "status": "failed",
            "unknownCommandIds": ["plc-command-9"],
        }
    ]


def test_pending_edge_outcome_and_unacknowledged_event_block_reset(
    workspace: Path,
) -> None:
    paths = WorkspacePaths.resolve(workspace)
    connection = _database(
        paths.runtime / "edge" / "edge_control.db",
        """
        CREATE TABLE edge_job_runtime(
            job_uuid TEXT PRIMARY KEY, status TEXT NOT NULL, updated_at REAL NOT NULL
        );
        CREATE TABLE edge_job_outcome_pending(
            job_uuid TEXT PRIMARY KEY, unknown_command_ids_json TEXT NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE TABLE edge_event_outbox(
            event_uuid TEXT PRIMARY KEY, type TEXT NOT NULL, created_at TEXT NOT NULL,
            acked_at REAL
        );
        """,
    )
    connection.execute(
        "INSERT INTO edge_job_runtime VALUES ('job-1', 'outcome_pending', 1.0)"
    )
    connection.execute(
        "INSERT INTO edge_job_outcome_pending VALUES ('job-1', '[]', 1.0)"
    )
    connection.execute(
        "INSERT INTO edge_event_outbox VALUES ('event-1', 'job.outcome_committed', 'now', NULL)"
    )
    connection.commit()
    connection.close()

    blockers = [blocker.as_dict() for blocker in inspect_local_reset_blockers(paths)]

    assert blockers == [
        {
            "source": "edge-runtime",
            "kind": "edge-event",
            "identity": "event-1",
            "status": "unacknowledged:job.outcome_committed",
        },
        {
            "source": "edge-runtime",
            "kind": "edge-job",
            "identity": "job-1",
            "status": "outcome_pending",
        },
        {
            "source": "edge-runtime",
            "kind": "edge-outcome",
            "identity": "job-1",
            "status": "outcome-pending",
        },
    ]


def test_folded_outcome_blocks_reset_even_when_runtime_status_is_terminal(
    workspace: Path,
) -> None:
    """待上报结果并进运行行后，状态已经是终态也必须拦住复位。"""

    from unilabos.app.edge_control.store import EdgeControlStore

    paths = WorkspacePaths.resolve(workspace)
    path = paths.runtime / "edge" / "edge_control.db"
    connection = _database(
        path,
        """
        CREATE TABLE edge_job_runtime (
            job_uuid TEXT PRIMARY KEY,
            task_uuid TEXT NOT NULL,
            node_uuid TEXT NOT NULL,
            command_uuid TEXT NOT NULL,
            claim_uuid TEXT NOT NULL,
            attempt INTEGER NOT NULL,
            fences_json TEXT NOT NULL,
            job_access_token TEXT NOT NULL,
            status TEXT NOT NULL,
            feedback_sequence INTEGER NOT NULL DEFAULT 0,
            trace_id TEXT NOT NULL DEFAULT '',
            traceparent TEXT NOT NULL DEFAULT '',
            tracestate TEXT NOT NULL DEFAULT '',
            updated_at REAL NOT NULL
        );
        CREATE TABLE edge_job_outcome_pending (
            job_uuid TEXT PRIMARY KEY,
            outcome TEXT NOT NULL,
            return_info_json TEXT NOT NULL,
            error_info_json TEXT NOT NULL,
            unknown_command_ids_json TEXT NOT NULL,
            updated_at REAL NOT NULL
        );
        INSERT INTO edge_job_runtime VALUES (
            'job-folded', 'task', 'node', 'cmd', 'claim', 1, '[]',
            'token', 'completed', 0, '', '', '', 1
        );
        INSERT INTO edge_job_outcome_pending VALUES (
            'job-folded', 'succeeded', '{}', '[]', '["cmd-9"]', 2
        );
        """,
    )
    connection.execute("PRAGMA user_version = 4")
    connection.commit()
    connection.close()

    store = EdgeControlStore(str(path))
    store.close()

    blockers = [blocker.as_dict() for blocker in inspect_local_reset_blockers(paths)]
    assert blockers == [
        {
            "source": "edge-runtime",
            "kind": "edge-outcome",
            "identity": "job-folded",
            "status": "unknown",
            "unknownCommandIds": ["cmd-9"],
        }
    ]


def test_second_check_restores_previously_ready_components_on_race(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = WorkspacePaths.resolve(workspace)
    host = WorkspaceHost(paths, ensure_local_token(paths), readiness_timeout=0.1)
    host._components["backend"]["phase"] = "ready"
    host._components["edge"]["phase"] = "ready"
    inspections = iter(
        [
            [],
            [
                LocalResetBlocker(
                    source="edge-runtime",
                    kind="edge-job",
                    identity="raced-job",
                    status="running",
                )
            ],
        ]
    )
    monkeypatch.setattr(
        "unilabos.workspace_host.host.inspect_local_reset_blockers",
        lambda _paths: next(inspections),
    )
    calls: list[str] = []
    host._stop_component = lambda name: calls.append(f"stop:{name}") or {}  # type: ignore[method-assign]
    host._start_backend = lambda parameters: calls.append("start:backend") or {}  # type: ignore[method-assign]
    host._start_edge = lambda: calls.append("start:edge") or {}  # type: ignore[method-assign]

    with pytest.raises(WorkspaceHostError) as raised:
        host._dispatch("local.reset-state", {})

    assert raised.value.code == "local_reset_state_blocked"
    assert raised.value.details["stage"] == "after-stop"
    assert calls == [
        "stop:edge",
        "stop:backend",
        "start:backend",
        "start:edge",
    ]
    host.close()


def test_after_stop_check_retries_transient_inspection_error(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backend 停止后的 SQLite WAL 短暂不可读必须等待后重新安全检查。"""

    paths = WorkspacePaths.resolve(workspace)
    host = WorkspaceHost(paths, ensure_local_token(paths), readiness_timeout=0.1)
    inspections: list[list[LocalResetBlocker] | LocalResetInspectionError] = [
        LocalResetInspectionError("disk I/O error"),
        LocalResetInspectionError("database is locked"),
        [],
    ]
    observed_delays: list[float] = []

    def inspect(_paths: WorkspacePaths) -> list[LocalResetBlocker]:
        result = inspections.pop(0)
        if isinstance(result, LocalResetInspectionError):
            raise result
        return result

    monkeypatch.setattr(
        "unilabos.workspace_host.host.inspect_local_reset_blockers",
        inspect,
    )
    monkeypatch.setattr(
        "unilabos.workspace_host.host.time.sleep",
        observed_delays.append,
    )

    host._assert_local_reset_safe("after-stop")

    assert inspections == []
    assert observed_delays == [0.1, 0.1]
    audit = paths.audit.read_text(encoding="utf-8")
    assert audit.count('"event":"local.reset-state.preflight-retry"') == 2
    assert '"event":"local.reset-state.preflight-failed"' not in audit
    host.close()


def test_after_stop_check_still_fails_closed_after_retry_limit(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """持续不可读的本地状态即使经过有限重试也绝不能执行重建。"""

    paths = WorkspacePaths.resolve(workspace)
    host = WorkspaceHost(paths, ensure_local_token(paths), readiness_timeout=0.1)
    attempts: list[Path] = []
    observed_delays: list[float] = []

    def inspect(inspected_paths: WorkspacePaths) -> list[LocalResetBlocker]:
        attempts.append(inspected_paths.workspace)
        raise LocalResetInspectionError("persistent disk I/O error")

    monkeypatch.setattr(
        "unilabos.workspace_host.host.inspect_local_reset_blockers",
        inspect,
    )
    monkeypatch.setattr(
        "unilabos.workspace_host.host.time.sleep",
        observed_delays.append,
    )
    monkeypatch.setattr(
        "unilabos.workspace_host.host._LOCAL_RESET_AFTER_STOP_INSPECTION_ATTEMPTS",
        3,
    )

    with pytest.raises(WorkspaceHostError) as raised:
        host._assert_local_reset_safe("after-stop")

    assert raised.value.code == "local_reset_state_preflight_failed"
    assert raised.value.details == {
        "stage": "after-stop",
        "message": "persistent disk I/O error",
    }
    assert attempts == [paths.workspace, paths.workspace, paths.workspace]
    assert observed_delays == [0.1, 0.1]
    host.close()


def test_unreadable_existing_state_fails_closed_without_stopping_components(
    workspace: Path,
) -> None:
    paths = WorkspacePaths.resolve(workspace)
    state_path = paths.runtime / "backend" / "local-domain" / "workflow_history.db"
    state_path.parent.mkdir(parents=True)
    state_path.write_bytes(b"not-a-sqlite-database")
    host = WorkspaceHost(paths, ensure_local_token(paths), readiness_timeout=0.1)
    stopped: list[str] = []
    host._stop_component = lambda name: stopped.append(name) or {}  # type: ignore[method-assign]

    with pytest.raises(WorkspaceHostError) as raised:
        host._dispatch("local.reset-state", {})

    assert raised.value.code == "local_reset_state_preflight_failed"
    assert raised.value.details["stage"] == "before-stop"
    assert stopped == []
    assert state_path.read_bytes() == b"not-a-sqlite-database"
    host.close()
