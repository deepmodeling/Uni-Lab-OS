"""中心调用和工站本地创建使用相同的普通优先级。"""

from copy import deepcopy
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from tests.app.center_client.test_atomic_workflow_events import _events, _submit, _workflow
from tests.workflow.test_f07_task_input_execution_plan import _client, _create_workflow
from unilabos.app.center_client.runtime import submission
from unilabos.app.scheduler.models import priority_weight
from unilabos.app.workflow_api import create_workflow_app
from unilabos.config.config import EdgeControlConfig
from unilabos.workflow.service import WorkflowConflict
from unilabos.workflow.station_workflow_submission import prepare_station_workflow_submission
from unilabos.workflow.workflow_spec_compiler import WorkflowSpecCompiler


@pytest.mark.parametrize("priority", [None, "normal", "high"])
def test_http_local_and_station_creation_persist_same_priority(tmp_path, monkeypatch, priority):
    """两个 HTTP 入口默认写入 normal，显式枚举也保持一致。"""
    client, store = _client(tmp_path / "priority.db")
    try:
        workflow = _create_workflow(client, store)
        monkeypatch.setattr(EdgeControlConfig, "api_key", "priority-test-key")
        supplied = {} if priority is None else {"priority": priority}
        local = client.post("/api/v1/workflow-tasks", json={
            "workflow_uuid": workflow, "input": {"count": 1}, **supplied,
        })
        station = client.post("/api/v1/station/workflow-invocations", json={
            "backend_task_uuid": str(uuid4()), "invocation_key": "priority-test",
            "workflow_name": "task input", "input": {"count": 1}, **supplied,
        }, headers={"Authorization": "Bearer priority-test-key"})
        assert local.status_code == 201, local.text
        assert station.status_code == 201, station.text
        expected = "normal" if priority is None else priority
        for response in (local, station):
            task = response.json()["data"]
            assert task["priority"] == expected
            assert store.get_task(task["uuid"])["priority"] == expected
            assert priority_weight(task["priority"]) == (200.0 if priority == "high" else 100.0)
    finally:
        client.close()
        store.close()


def test_center_submission_default_replay_recovery_and_scheduler_use_normal(tmp_path):
    """真实中心任务以 normal 落盘，重放和恢复沿用同一指纹且不增建作业。"""
    with _workflow(tmp_path) as (store, service, durable, package, _):
        args = submission(package)
        args.pop("priority", None)
        task = service.submit_station_workflow(**args, center_invocation_uuid=package.station_invocation_uuid)
        assert task["priority"] == "normal"
        row = store.db.execute("SELECT priority,request_fingerprint FROM workflow_task WHERE uuid=?", (task["uuid"],)).fetchone()
        assert row["priority"] == "normal"
        original_fingerprint = row["request_fingerprint"]
        jobs = [job["uuid"] for job in store.list_jobs(task["uuid"])]
        events = _events(store)
        spec = WorkflowSpecCompiler().compile(task, store.list_jobs(task["uuid"]))
        assert spec.priority == "normal"
        assert priority_weight(spec.priority) == 100.0
        assert _submit(service, package)["uuid"] == task["uuid"]
        assert service.find_station_workflow_submission(**submission(package))["uuid"] == task["uuid"]
        assert service.recover_center_workflow_invocation(package.station_invocation_uuid)["uuid"] == task["uuid"]
        proof = service.inspect_existing_center_task({"payload": dict(package.raw), "payload_digest": package.payload_digest})
        assert proof["local_task_uuid"] == task["uuid"]
        assert [job["uuid"] for job in store.list_jobs(task["uuid"])] == jobs
        assert _events(store) == events
        assert store.db.execute("SELECT request_fingerprint FROM workflow_task WHERE uuid=?", (task["uuid"],)).fetchone()[0] == original_fingerprint


@pytest.mark.parametrize("change", [
    {"input_value": {"value": 8.5}},
    {"revision_fingerprint": "sha256:" + "0" * 64},
    {"priority": "high"},
    {"deadline": "2030-01-01T00:00:00Z"},
])
def test_center_changed_replay_still_conflicts(tmp_path, change):
    """统一默认值不放宽输入、发布修订、优先级或截止时间的重放校验。"""
    with _workflow(tmp_path) as (store, service, durable, package, _):
        task = _submit(service, package)
        original_jobs = store.list_jobs(task["uuid"])
        original_events = _events(store)
        with pytest.raises(WorkflowConflict):
            service.submit_station_workflow(**{**submission(package), **change},
                center_invocation_uuid=package.station_invocation_uuid)
        assert store.list_jobs(task["uuid"]) == original_jobs
        assert _events(store) == original_events


def test_legacy_center_test_task_is_not_rewritten_or_recreated(tmp_path):
    """旧数值测试记录原样保留，新规则拒绝旧指纹而不替换或重复执行。"""
    with _workflow(tmp_path) as (store, service, durable, package, _):
        task = _submit(service, package)
        legacy = prepare_station_workflow_submission(**{**submission(package), "priority": 1.0},
            inventory_bindings=None, description=None, meta_data=None)
        with store.transaction() as connection:
            connection.execute("UPDATE workflow_task SET priority=?,request_fingerprint=? WHERE uuid=?",
                ("1.0", legacy.request_fingerprint, task["uuid"]))
        original_jobs = deepcopy(store.list_jobs(task["uuid"]))
        client = TestClient(create_workflow_app(service))
        try:
            detail = client.get(f"/api/v1/workflow-tasks/{task['uuid']}")
            assert detail.status_code == 200, detail.text
            historical = detail.json()["data"]
            assert historical["priority"] == 1.0
            assert historical["status"] == task["status"]
            assert historical["input"] == task["input"]
            listed = client.get("/api/v1/workflow-tasks")
            assert listed.status_code == 200, listed.text
            assert listed.json()["data"]["total"] == 1
        finally:
            client.close()
        with pytest.raises(WorkflowConflict):
            service.find_station_workflow_submission(**submission(package))
        with pytest.raises(WorkflowConflict):
            _submit(service, package)
        row = store.db.execute("SELECT priority,request_fingerprint FROM workflow_task WHERE uuid=?", (task["uuid"],)).fetchone()
        assert row["priority"] == "1.0"
        assert row["request_fingerprint"] == legacy.request_fingerprint
        assert store.list_jobs(task["uuid"]) == original_jobs
        assert store.db.execute("SELECT COUNT(*) FROM workflow_task").fetchone()[0] == 1
