"""任务计数、摘要和定义缓存的双数据库合同。"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from tests.workflow.test_f05_task_runtime_projection import JOB_UUIDS, TASK_UUID, _seed_task
from unilabos.app.workflow_api import create_workflow_app
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.store import WorkflowStore


def test_task_count_summary_and_state_do_not_read_definition_blobs(database_mode, tmp_path, monkeypatch):
    store = WorkflowStore(tmp_path / "tasks.db")
    try:
        _seed_task(store, job_count=3)
        snapshot = {"workflow": {"name": "冻结任务名称"}, "nodes": [{"schema": "s" * 220_000}]}
        plan = {"nodes": [{"schema": "p" * 450_000}]}
        with store.transaction() as connection:
            connection.execute(
                "UPDATE workflow_task SET workflow_snapshot=?,execution_plan=?,status='running',description='批次 A',"
                "input=?,output=?,error_info=? WHERE uuid=?",
                (json.dumps(snapshot), json.dumps(plan), '{"sample":1}', '{"result":2}', '[{"message":"x"}]', TASK_UUID),
            )
            for identity, status in zip(JOB_UUIDS, ("succeeded", "failed", "running")):
                connection.execute("UPDATE workflow_node_job SET status=? WHERE uuid=?", (status, identity))
        statements = []
        execute = store.db.execute

        def record(sql, parameters=()):
            statements.append(sql)
            return execute(sql, parameters)

        monkeypatch.setattr(store.db, "execute", record)
        with TestClient(create_workflow_app(WorkflowService(store))) as client:
            count = client.get("/api/v1/workflow-tasks/count?status=running&execution_kind=workflow")
            assert count.json() == {"code": 0, "data": {"total": 1}}
            task_queries = [sql for sql in statements if "workflow_task" in sql]
            assert len(task_queries) == 1
            assert "COUNT(*)" in task_queries[0]
            assert "workflow_snapshot" not in task_queries[0] and "execution_plan" not in task_queries[0]
            statements.clear()

            response = client.get("/api/v1/workflow-tasks?projection=summary&page_size=1")
            summary = response.json()["data"]["items"][0]
            assert len(response.content) < 3_000
            assert summary["workflow_name"] == "冻结任务名称"
            assert summary["description"] == "批次 A"
            assert "name" not in summary
            assert summary["progress"] == {"total": 3, "completed": 2, "percent": 67}
            assert summary["job_counts"] == {"succeeded": 1, "failed": 1, "canceled": 0}
            assert not {"workflow_snapshot", "execution_plan", "jobs"} & summary.keys()
            assert all("task.workflow_snapshot" not in sql and "task.execution_plan" not in sql for sql in statements)

            statements.clear()
            state = client.get(f"/api/v1/workflow-tasks/{TASK_UUID}?projection=state").json()["data"]
            assert not {"workflow_snapshot", "execution_plan"} & state.keys()
            assert "name" not in state
            assert state["description"] == "批次 A"
            assert state["input"] == {"sample": 1}
            assert state["output"] == {"result": 2}
            assert state["error_info"] == [{"message": "x"}]
            assert all("SELECT * FROM workflow_task" not in sql for sql in statements)
            full = client.get(f"/api/v1/workflow-tasks/{TASK_UUID}").json()["data"]
            assert full["workflow_snapshot"] == snapshot
            assert full["execution_plan"] == plan
            assert full["definition_version"] == state["definition_version"]
            assert all(state[k] == v for k, v in full.items() if k not in {"workflow_snapshot", "execution_plan"})
            legacy = client.get("/api/v1/workflow-tasks?page_size=1").json()["data"]["items"][0]
            assert legacy["execution_plan"] == plan
            assert client.get("/api/v1/workflow-tasks/count?status=invalid").json()["code"] != 0
            assert client.get("/api/v1/workflow-tasks?projection=invalid").json()["code"] != 0
            assert client.get(f"/api/v1/workflow-tasks/{TASK_UUID}?projection=invalid").json()["code"] != 0
        with store.transaction() as connection:
            connection.execute("UPDATE workflow_task SET deleted_at='now' WHERE uuid=?", (TASK_UUID,))
        assert store.count_tasks() == 0
    finally:
        store.close()


def test_definition_version_tracks_only_committed_definition_changes(database_mode, tmp_path):
    path = tmp_path / "tasks.db"
    store = WorkflowStore(path)
    try:
        _seed_task(store)
        initial = store.get_task_state(TASK_UUID)["definition_version"]
        with store.transaction() as connection:
            connection.execute("UPDATE workflow_task SET status='running',update_time='new' WHERE uuid=?", (TASK_UUID,))
        assert store.get_task(TASK_UUID)["definition_version"] == initial
        with store.transaction() as connection:
            connection.execute("UPDATE workflow_task SET execution_plan=? WHERE uuid=?", ('{"binding":"resolved"}', TASK_UUID))
        changed = store.get_task_state(TASK_UUID)["definition_version"]
        assert changed != initial
        with store.transaction() as connection:
            connection.execute("UPDATE workflow_task SET execution_plan=execution_plan WHERE uuid=?", (TASK_UUID,))
        assert store.get_task_state(TASK_UUID)["definition_version"] == changed
        with pytest.raises(RuntimeError):
            with store.transaction() as connection:
                connection.execute("UPDATE workflow_task SET workflow_snapshot=? WHERE uuid=?", ('{"workflow":{"name":"rollback"}}', TASK_UUID))
                raise RuntimeError("rollback")
        assert store.get_task_state(TASK_UUID)["definition_version"] == changed
        store.close()
        store = WorkflowStore(path)
        assert store.get_task_state(TASK_UUID)["definition_version"] == changed
    finally:
        store.close()


def test_existing_tasks_are_backfilled_without_loading_blobs(database_mode, tmp_path):
    path = tmp_path / "tasks.db"
    store = WorkflowStore(path)
    try:
        _seed_task(store)
        with store.transaction() as connection:
            connection.execute("UPDATE workflow_task SET workflow_snapshot=? WHERE uuid=?", ('{"workflow":{"name":"旧任务"}}', TASK_UUID))
            connection.execute("DELETE FROM workflow_task_read_model WHERE task_uuid=?", (TASK_UUID,))
        store.close()
        store = WorkflowStore(path)
        state = store.get_task_state(TASK_UUID)
        assert state["workflow_name"] == "旧任务"
        assert state["definition_version"] == "1"
    finally:
        store.close()
