"""历史手动动作结果核对的 HTTP 身份、证据版本与无副作用约束。"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from tests.workflow.test_error_control import seed_job
from tests.workflow.test_manual_handling_completion import seed_manual
from unilabos.app.scheduler.dispatch import RecordingDispatcher
from unilabos.app.scheduler.service import EdgeScheduler
from unilabos.app.workflow_api import create_workflow_app
from unilabos.workflow.execution_lock_lease import try_acquire_execution_locks
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.store import WorkflowStore, utc_now
from unilabos.workflow.task_scheduler_bridge import TaskSchedulerBridge


@pytest.fixture
def history(tmp_path):
    store = WorkflowStore(tmp_path / "workflow.db")
    bridge = TaskSchedulerBridge(store, scheduler=EdgeScheduler(dispatcher=RecordingDispatcher()))
    service = WorkflowService(store, task_scheduler_bridge=bridge)
    session_id = str(uuid4())
    with store.transaction() as conn:
        conn.execute("INSERT INTO workflow_error_session(session_id,station_id,decision_id,stage,created_by,created_at) "
                     "VALUES(?,'local',?,'RESOLVED','old-operator',?)", (session_id, str(uuid4()), utc_now()))
    task, job, command = seed_manual(store, session_id, status="canceled", uncertainty="runtime_restarted")
    with store.transaction() as conn:
        conn.execute("UPDATE workflow_task SET status='canceled' WHERE uuid=?", (task,))
        acquired = try_acquire_execution_locks(conn, task_uuid=task, job_uuid=job,
            requests=[{"lock_key": "/devices/history-review", "scope": "device"}])
        assert acquired.acquired
    with TestClient(create_workflow_app(service)) as client:
        yield client, store, bridge, session_id, task, job, command
    bridge.close()
    store.close()


def url(job):
    return f"/api/v1/stations/local/manual-action-jobs/{job}/review"


def context(client, job):
    response = client.get(url(job))
    assert response.status_code == 200, response.text
    return response.json()["data"]


def body(current):
    return {"expected_review_version": current["review_version"],
            "reason": "已在现场核对设备停止及物料现状", "operator_confirmed": True}


def durable_rows(store, table):
    with store.read() as conn:
        return sorted(
            (dict(row) for row in conn.execute(f"SELECT * FROM {table}")),
            key=lambda row: json.dumps(row, sort_keys=True),
        )


@pytest.mark.parametrize("status", ["succeeded", "failed", "canceled", "timeout"])
@pytest.mark.parametrize("station_state", ["running", "paused", "another_session"])
def test_closed_history_review_does_not_change_execution_or_station(history, status, station_state):
    client, store, bridge, session_id, task, job, _ = history
    with store.transaction() as conn:
        conn.execute("UPDATE workflow_node_job SET status=? WHERE uuid=?", (status, job))
        conn.execute("UPDATE workflow_station_control SET mode=? WHERE station_id='local'",
                     ("RUNNING" if station_state == "running" else "PAUSED",))
        if station_state == "another_session":
            other_id = str(uuid4())
            conn.execute("INSERT INTO workflow_error_session(session_id,station_id,decision_id,stage,created_by,created_at) "
                         "VALUES(?,'local',?,'MANUAL_HANDLING','other-operator',?)", (other_id, str(uuid4()), utc_now()))
            conn.execute("UPDATE workflow_station_control SET active_session_id=? WHERE station_id='local'", (other_id,))
    tables = ["workflow_task", "workflow_error_session", "execution_claim", "execution_lock_lease"]
    unchanged = {table: durable_rows(store, table) for table in tables}
    before_station = bridge.error_control.snapshot()
    before_job = store.get_job(job)
    current = context(client, job)
    assert current["session_stage"] == "RESOLVED"
    response = client.post(url(job), json=body(current), headers={"Idempotency-Key": "history-review"})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["review_context"]["result_reviewed"] is True
    assert {table: durable_rows(store, table) for table in tables} == unchanged
    station = bridge.error_control.snapshot()
    assert station["mode"] == before_station["mode"]
    assert station["active_session"] == before_station["active_session"]
    after_job = store.get_job(job)
    for field in ("status", "uncertainty_reason", "return_info", "error_info", "start_state"):
        assert after_job.get(field) == before_job.get(field)
    assert after_job["manual_review"]["operator_id"] == "local-workspace"
    assert after_job["manual_review"]["reason"] == body(current)["reason"]
    assert after_job["result_reviewed"] is True
    detail = client.get(f"/api/v1/workflow-node-jobs/{job}").json()["data"]
    compact = store.list_jobs_for_tasks([task])[task][0]
    for projected in (detail, compact, store.list_jobs(task)[0]):
        assert projected["review_version"] == current["review_version"]
        assert projected["result_reviewed"] is True
        assert projected["manual_review"] == after_job["manual_review"]


@pytest.mark.parametrize("status", ["pending", "dispatched", "running", "cancel_requested", "unknown", "skipped"])
def test_unfinished_historical_manual_job_cannot_be_reviewed(history, status):
    client, store, _, _, _, job, _ = history
    current = context(client, job)
    with store.transaction() as conn:
        conn.execute("UPDATE workflow_node_job SET status=? WHERE uuid=?", (status, job))
    for response in (client.get(url(job)), client.post(url(job), json=body(current), headers={"Idempotency-Key": "review"})):
        assert response.status_code == 409, response.text
        assert response.json()["detail"] == "manual_action_not_finished"


@pytest.mark.parametrize("change", ["workflow_source", "deleted_job", "deleted_task", "missing_session", "foreign_station"])
def test_historical_review_is_scoped_to_live_manual_job_and_station(history, change):
    client, store, _, session_id, task, job, _ = history
    current = context(client, job)
    with store.transaction() as conn:
        if change == "workflow_source":
            conn.execute("UPDATE workflow_node_job SET execution_source='workflow' WHERE uuid=?", (job,))
        elif change == "deleted_job":
            conn.execute("UPDATE workflow_node_job SET deleted_at=? WHERE uuid=?", (utc_now(), job))
        elif change == "deleted_task":
            conn.execute("UPDATE workflow_task SET deleted_at=? WHERE uuid=?", (utc_now(), task))
        elif change == "missing_session":
            conn.execute("DELETE FROM workflow_error_session WHERE session_id=?", (session_id,))
        else:
            conn.execute("UPDATE workflow_error_session SET station_id='different' WHERE session_id=?", (session_id,))
    for response in (client.get(url(job)), client.post(url(job), json=body(current), headers={"Idempotency-Key": "review"})):
        assert response.status_code == 404, response.text


@pytest.mark.parametrize("changes,code,detail", [
    ({"operator_confirmed": False}, 409, "operator_confirmation_required"),
    ({"operator_confirmed": "true"}, 422, None),
    ({"reason": "   "}, 422, "请填写现场核对说明"),
    ({"expected_review_version": "stale"}, 409, "manual_review_version_conflict"),
])
def test_historical_review_requires_explicit_confirmation_reason_and_current_result(history, changes, code, detail):
    client, store, _, _, _, job, _ = history
    before = store.get_job(job)
    current = context(client, job)
    response = client.post(url(job), json={**body(current), **changes}, headers={"Idempotency-Key": "review"})
    assert response.status_code == code, response.text
    if detail:
        assert response.json()["detail"] == detail
    assert store.get_job(job) == before


def test_history_review_is_idempotent_and_late_evidence_invalidates_every_projection(history):
    client, store, _, _, task, job, _ = history
    current = context(client, job)
    request = body(current)
    first = client.post(url(job), json=request, headers={"Idempotency-Key": "same-review"})
    assert first.status_code == 200, first.text
    repeated = client.post(url(job), json=request, headers={"Idempotency-Key": "same-review"})
    assert repeated.json()["data"]["command_uuid"] == first.json()["data"]["command_uuid"]
    conflict = client.post(url(job), json={**request, "reason": "改了理由"}, headers={"Idempotency-Key": "same-review"})
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "idempotency_conflict"
    with store.transaction() as conn:
        conn.execute("UPDATE workflow_node_job SET feedback_data=? WHERE uuid=?", (json.dumps({"moving": True}), job))
    for projected in (context(client, job), store.get_job(job), store.list_jobs_for_tasks([task])[task][0]):
        assert projected["review_version"] != current["review_version"]
        assert projected["result_reviewed"] is False
        assert projected["manual_review"]["review_version"] == current["review_version"]
    stale = client.post(url(job), json=request, headers={"Idempotency-Key": "new-review"})
    assert stale.status_code == 409
    assert stale.json()["detail"] == "manual_review_version_conflict"


def test_normal_workflow_manual_history_does_not_gain_review_projection(history):
    _, store, _, _, task, job, _ = history
    with store.transaction() as conn:
        conn.execute("UPDATE workflow_node_job SET execution_source='workflow' WHERE uuid=?", (job,))
    for projected in (store.get_job(job), store.list_jobs_for_tasks([task])[task][0]):
        assert "review_version" not in projected
        assert "result_reviewed" not in projected


def reviewed_task_without_resources(history):
    client, store, _, _, task, job, _ = history
    with store.transaction() as conn:
        conn.execute("UPDATE execution_claim SET state='released',released_at=? WHERE workflow_task_uuid=?", (utc_now(), task))
        conn.execute("UPDATE execution_lock_lease SET state='released',released_at=? WHERE workflow_task_uuid=?", (utc_now(), task))
        conn.execute("UPDATE workflow_task SET control_status='waiting_reconciliation',cleanup_status='requires_attention',"
                     "attention_reason='runtime_restarted' WHERE uuid=?", (task,))
    current = context(client, job)
    response = client.post(url(job), json=body(current), headers={"Idempotency-Key": "review-for-task"})
    assert response.status_code == 200, response.text
    return task, job


def task_views(client, task):
    detail = client.get(f"/api/v1/workflow-tasks/{task}")
    assert detail.status_code == 200, detail.text
    regular = client.get("/api/v1/workflow-tasks")
    assert regular.status_code == 200, regular.text
    compact = client.get("/api/v1/workflow-task-presentations")
    assert compact.status_code == 200, compact.text
    return [detail.json()["data"],
            next(item for item in regular.json()["data"]["items"] if item["uuid"] == task),
            next(item for item in compact.json()["data"]["items"] if item["uuid"] == task)]


def test_reviewed_single_manual_task_exposes_display_eligibility_without_changing_state(history):
    client, store, _, _, _, _, _ = history
    task, job = reviewed_task_without_resources(history)
    before = durable_rows(store, "workflow_task")
    for projected in task_views(client, task):
        assert projected["manual_review_attention_resolved"] is True
        assert projected["control_status"] == "waiting_reconciliation"
        assert projected["cleanup_status"] == "requires_attention"
        assert projected["status"] == "canceled"
    assert durable_rows(store, "workflow_task") == before
    assert store.get_job(job)["uncertainty_reason"] == "runtime_restarted"


@pytest.mark.parametrize("blocker", ["claim", "lease", "tenancy", "material_claim", "pending_command",
    "pending_confirmation", "error", "other_job", "other_reason", "error_waiting", "waiting_intervention", "running_task"])
def test_review_does_not_hide_other_execution_or_resource_attention(history, blocker):
    client, store, bridge, _, _, _, _ = history
    task, job = reviewed_task_without_resources(history)
    now = utc_now()
    with store.transaction() as conn:
        if blocker == "claim":
            conn.execute("UPDATE execution_claim SET state='uncertain',released_at=NULL WHERE workflow_task_uuid=?", (task,))
        elif blocker == "lease":
            conn.execute("UPDATE execution_lock_lease SET state='uncertain',released_at=NULL WHERE workflow_task_uuid=?", (task,))
        elif blocker == "tenancy":
            conn.execute("INSERT INTO task_device_tenancy(uuid,create_time,update_time,workflow_task_uuid,material_uuid,"
                         "device_lock_key,acquired_by_job_uuid,state,acquired_at) VALUES(?,?,?,?,?,?,?,'active',?)",
                         (str(uuid4()), now, now, task, str(uuid4()), '/devices/other', job, now))
        elif blocker == "material_claim":
            conn.execute("INSERT INTO workflow_task_material_claim(uuid,create_time,update_time,meta_data,workflow_task_uuid,"
                         "workflow_node_uuid,workflow_node_job_uuid,material_uuid,status,revision,acquired_at) "
                         "VALUES(?,?,?,'{}',?,?,?,?,'active',1,?)",
                         (str(uuid4()), now, now, task, store.get_job(job)["workflow_node_uuid"], job, str(uuid4()), now))
        elif blocker == "pending_command":
            conn.execute("INSERT INTO workflow_task_command(uuid,create_time,update_time,meta_data,workflow_task_uuid,type,"
                         "idempotency_key,status,result,trace_context) VALUES(?,?,?,'{}',?,'cancel','pending-other','pending','{}','{}')",
                         (str(uuid4()), now, now, task))
        elif blocker == "pending_confirmation":
            conn.execute("INSERT INTO workflow_manual_confirmation(workflow_node_job_uuid,workflow_task_uuid,status,opened_at,deadline_at) "
                         "VALUES(?,?,'pending',?,?)", (job, task, now, now))
        elif blocker == "other_reason":
            conn.execute("UPDATE workflow_task SET attention_reason='resource_handoff_required' WHERE uuid=?", (task,))
        elif blocker in {"error_waiting", "waiting_intervention"}:
            conn.execute("UPDATE workflow_task SET control_status=? WHERE uuid=?", (blocker, task))
        elif blocker == "running_task":
            conn.execute("UPDATE workflow_task SET status='running' WHERE uuid=?", (task,))
    if blocker == "other_job":
        _, other_job = seed_job(store)
        with store.transaction() as conn:
            conn.execute("UPDATE workflow_node_job SET workflow_task_uuid=?,status='succeeded' WHERE uuid=?", (task, other_job))
    elif blocker == "error":
        bridge.error_control.report_error(job, phase="execution", message="另一项错误尚未处置")
        with store.transaction() as conn:
            conn.execute("UPDATE workflow_task SET control_status='waiting_reconciliation' WHERE uuid=?", (task,))
    for projected in task_views(client, task):
        assert projected["manual_review_attention_resolved"] is False, blocker


def test_late_result_removes_task_display_eligibility(history):
    client, store, _, _, _, _, _ = history
    task, job = reviewed_task_without_resources(history)
    assert all(item["manual_review_attention_resolved"] for item in task_views(client, task))
    with store.transaction() as conn:
        conn.execute("UPDATE workflow_node_job SET return_info=? WHERE uuid=?", (json.dumps({"late_feedback": "unknown"}), job))
    assert all(not item["manual_review_attention_resolved"] for item in task_views(client, task))


def test_reviewed_history_no_longer_blocks_mode_switch_until_evidence_changes(history):
    client, store, _, _, _, _, _ = history
    task, job = reviewed_task_without_resources(history)
    before = durable_rows(store, "workflow_task")
    assert store.list_execution_admission_blockers() == []
    assert durable_rows(store, "workflow_task") == before
    with store.transaction() as conn:
        conn.execute("UPDATE workflow_node_job SET feedback_data=? WHERE uuid=?", (json.dumps({"new_evidence": True}), job))
    assert [item["task_uuid"] for item in store.list_execution_admission_blockers()] == [task]


def test_mode_switch_finds_real_blockers_after_more_than_20_reviewed_history_rows(history):
    client, store, _, session_id, _, _, _ = history
    first_task, _ = reviewed_task_without_resources(history)
    reviewed = [first_task]
    for index in range(21):
        task, job, _ = seed_manual(store, session_id, status="canceled", uncertainty="runtime_restarted")
        with store.transaction() as conn:
            conn.execute("UPDATE workflow_task SET status='canceled',cleanup_status='requires_attention',"
                         "control_status='waiting_reconciliation',attention_reason='runtime_restarted' WHERE uuid=?", (task,))
        response = client.post(url(job), json=body(context(client, job)), headers={"Idempotency-Key": f"review-{index}"})
        assert response.status_code == 200, response.text
        reviewed.append(task)
    with store.transaction() as conn:
        conn.executemany("UPDATE workflow_task SET create_time='2000-01-01T00:00:00Z' WHERE uuid=?", [(task,) for task in reviewed])
    actual, _ = seed_job(store)
    second, _ = seed_job(store)
    assert [item["task_uuid"] for item in store.list_execution_admission_blockers(limit=1)] == [actual]
    assert [item["task_uuid"] for item in store.list_execution_admission_blockers(limit=20)] == [actual, second]
