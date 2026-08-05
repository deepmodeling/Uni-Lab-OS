"""Task workspace sidecar persistence tests."""

from __future__ import annotations

import fcntl
import json
import threading

import pytest

from task_orchestration.models import Workspace
from task_orchestration.store import (
    SidecarCorruptionError,
    SidecarSymlinkError,
    VersionConflictError,
    WorkspaceStore,
    WorkflowPathError,
)


def test_get_returns_default_workspace_for_workflow(tmp_path):
    workflow = tmp_path / "workflows" / "demo.json"
    workflow.parent.mkdir()
    workflow.write_text("{}", encoding="utf-8")
    store = WorkspaceStore(tmp_path)

    result = store.get("workflows/demo.json")

    assert result.version == 0
    assert result.workspace.workflow_path == "workflows/demo.json"
    assert result.workspace.templates == []
    assert result.workspace.task_instances == []


def test_get_accepts_legacy_plural_templates_deleted_event(tmp_path):
    workflow = tmp_path / "demo.json"
    workflow.write_text("{}", encoding="utf-8")
    sidecar = tmp_path / "demo.json.task-workspace.json"
    sidecar.write_text(
        json.dumps({
            "version": 1,
            "workspace": {
                "workflow_path": "demo.json",
                "events": [{
                    "kind": "templates_deleted",
                    "payload": {"deleted_template_ids": ["template-a"]},
                }],
            },
        }),
        encoding="utf-8",
    )

    result = WorkspaceStore(tmp_path).get("demo.json")

    assert result.workspace.events[0].kind == "templates_deleted"


def test_put_writes_sidecar_atomically_and_increments_version(tmp_path):
    workflow = tmp_path / "demo.json"
    workflow.write_text("{}", encoding="utf-8")
    store = WorkspaceStore(tmp_path)
    workspace = Workspace(
        workflow_path="demo.json",
        templates=[],
        task_instances=[],
    )

    result = store.put(workspace, expected_version=0)

    sidecar = tmp_path / "demo.json.task-workspace.json"
    assert result.version == 1
    assert json.loads(sidecar.read_text(encoding="utf-8"))["version"] == 1
    assert not list(tmp_path.glob("*.tmp"))


def test_put_rejects_stale_expected_version(tmp_path):
    workflow = tmp_path / "demo.json"
    workflow.write_text("{}", encoding="utf-8")
    store = WorkspaceStore(tmp_path)
    workspace = Workspace(workflow_path="demo.json")
    store.put(workspace, expected_version=0)

    with pytest.raises(VersionConflictError):
        store.put(workspace, expected_version=0)


def test_independent_stores_use_file_lock_and_preserve_version_conflict(tmp_path):
    workflow = tmp_path / "demo.json"
    workflow.write_text("{}", encoding="utf-8")
    first_store = WorkspaceStore(tmp_path)
    second_store = WorkspaceStore(tmp_path)
    workspace = Workspace(workflow_path="demo.json")
    lock_path = first_store._lock_path(  # noqa: SLF001
        tmp_path / "demo.json.task-workspace.json"
    )
    completed = threading.Event()
    result: list[object] = []

    with lock_path.open("a+") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)

        def write_from_first_store() -> None:
            result.append(first_store.put(workspace, expected_version=0))
            completed.set()

        thread = threading.Thread(target=write_from_first_store)
        thread.start()
        assert not completed.wait(timeout=0.1)
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

    thread.join(timeout=1)
    assert completed.is_set()
    assert result[0].version == 1
    with pytest.raises(VersionConflictError):
        second_store.put(workspace, expected_version=0)


def test_get_and_put_reject_sidecar_symlink(tmp_path):
    workflow = tmp_path / "demo.json"
    workflow.write_text("{}", encoding="utf-8")
    outside = tmp_path.parent / "outside-sidecar.json"
    outside.write_text('{"version": 7, "workspace": {"workflow_path": "outside.json"}}', encoding="utf-8")
    sidecar = tmp_path / "demo.json.task-workspace.json"
    sidecar.symlink_to(outside)
    store = WorkspaceStore(tmp_path)

    with pytest.raises(SidecarSymlinkError):
        store.get("demo.json")
    with pytest.raises(SidecarSymlinkError):
        store.put(Workspace(workflow_path="demo.json"), expected_version=0)


@pytest.mark.parametrize(
    "contents",
    [
        "{not json",
        '{"version": "invalid", "workspace": {"workflow_path": "demo.json"}}',
    ],
)
def test_corrupt_sidecar_raises_dedicated_error(tmp_path, contents):
    workflow = tmp_path / "demo.json"
    workflow.write_text("{}", encoding="utf-8")
    (tmp_path / "demo.json.task-workspace.json").write_text(contents, encoding="utf-8")

    with pytest.raises(SidecarCorruptionError):
        WorkspaceStore(tmp_path).get("demo.json")


def test_atomic_write_removes_temporary_file_after_write_error(tmp_path, monkeypatch):
    workflow = tmp_path / "demo.json"
    workflow.write_text("{}", encoding="utf-8")
    store = WorkspaceStore(tmp_path)

    def fail_dump(*_args, **_kwargs) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("task_orchestration.store.json.dump", fail_dump)

    with pytest.raises(OSError, match="disk full"):
        store.put(Workspace(workflow_path="demo.json"), expected_version=0)
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("workflow_path", ["../outside.json", "/tmp/outside.json"])
def test_store_rejects_workflow_paths_outside_workspace(tmp_path, workflow_path):
    store = WorkspaceStore(tmp_path)

    with pytest.raises(WorkflowPathError):
        store.get(workflow_path)
