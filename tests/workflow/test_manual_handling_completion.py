"""人工处置结束门禁和结果核对的状态、范围与事务保证。"""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.workflow.test_error_control import decision_request, seed_job
from unilabos.app.error_handling_api import create_error_handling_router
from unilabos.workflow.error_control import StationExecutionControl
from unilabos.workflow.store import StoreConflict, WorkflowStore, utc_now


@pytest.fixture
def handling(tmp_path):
    store = WorkflowStore(tmp_path / "workflow.db")
    control = StationExecutionControl(store)
    source_task, source_job = seed_job(store)
    error = control.report_error(source_job, phase="execution", message="设备异常")
    entered = control.decide(
        error["decision_id"], decision_request(control, error, "enter_manual_handling"),
        idempotency_key="enter", operator_id="operator",
    )
    session_id = entered["station"]["active_session"]["session_id"]
    yield store, control, session_id, source_task
    store.close()


def seed_manual(store, session_id, *, status="running", uncertainty=None,
                start_state="NOT_STARTED", deleted=False, source="error_manual"):
    task, job = seed_job(store)
    command = str(uuid4())
    with store.transaction() as conn:
        conn.execute(
            "UPDATE workflow_node_job SET execution_source=?,meta_data=?,status=?,"
            "uncertainty_reason=?,start_state=?,edge_command_uuid=?,deleted_at=? WHERE uuid=?",
            (source, json.dumps({"execution_source": source, "session_id": session_id}),
             status, uncertainty, start_state, command, utc_now() if deleted else None, job),
        )
    return task, job, command


def request_for(control):
    station = control.snapshot()
    return {
        "expected_station_version": station["station_version"],
        "expected_session_version": station["active_session"]["version"],
        "summary": "已完成现场检查",
    }


def complete(control, session_id):
    return control.complete(session_id, request_for(control), idempotency_key="complete",
                            operator_id="operator")


def review_request(control, session_id, command):
    station = control.snapshot()
    return {
        "session_id": session_id,
        "expected_session_version": station["active_session"]["version"],
        "expected_station_version": station["station_version"],
        "expected_review_version": control.manual_action(command)["execution_job"]["review_version"],
        "operator_confirmed": True,
        "reason": "现场确认搅拌已停止，设备无继续动作",
    }


@pytest.mark.parametrize("status", ["pending", "dispatched", "running", "cancel_requested", "unknown", "skipped"])
@pytest.mark.parametrize("start_state", ["NOT_STARTED", "STARTED"])
def test_active_manual_action_blocks_complete_without_any_side_effect(handling, status, start_state):
    store, control, session_id, _ = handling
    task, job, _ = seed_manual(store, session_id, status=status, start_state=start_state)
    before = control.snapshot(), store.get_task(task), store.get_job(job)
    with pytest.raises(StoreConflict, match="^manual_actions_running$"):
        complete(control, session_id)
    assert (control.snapshot(), store.get_task(task), store.get_job(job)) == before
    assert control.get_session(session_id)["stage"] == "MANUAL_HANDLING"
    assert control.snapshot()["mode"] == "PAUSED"


@pytest.mark.parametrize("status", ["succeeded", "failed", "canceled", "timeout"])
def test_terminal_action_without_uncertainty_allows_complete(handling, status):
    store, control, session_id, source_task = handling
    seed_manual(store, session_id, status=status)
    result = complete(control, session_id)
    assert result["station"]["active_session"]["stage"] == "DECISION_REQUIRED"
    assert result["station"]["mode"] == "PAUSED"
    assert store.get_task(source_task)["control_status"] == "error_waiting"
    assert "canceled_before_start" not in result["result"]


@pytest.mark.parametrize("status", ["succeeded", "failed", "canceled", "timeout"])
def test_uncertain_terminal_action_requires_review_and_preserves_evidence(handling, status):
    store, control, session_id, _ = handling
    task, job, command = seed_manual(store, session_id, status=status, uncertainty="edge_result_unknown")
    task_before, job_before = store.get_task(task), store.get_job(job)
    with pytest.raises(StoreConflict, match="^manual_actions_unresolved$"):
        complete(control, session_id)
    request = review_request(control, session_id, command)
    reviewed = control.review_manual(command, request, idempotency_key="review", operator_id="operator")
    replay = control.review_manual(command, request, idempotency_key="review", operator_id="operator")
    assert reviewed["command_uuid"] == replay["command_uuid"]
    assert store.get_task(task) == task_before
    after = store.get_job(job)
    audit = after["control_data"].pop("manual_review")
    assert after["manual_review"] == audit
    assert after["result_reviewed"] is True
    derived_fields = {"manual_review", "result_reviewed"}
    assert {key: value for key, value in after.items() if key not in derived_fields} == {
        key: value for key, value in job_before.items() if key not in derived_fields
    }
    assert audit["reason"] == request["reason"]
    assert audit["operator_id"] == "operator"
    assert control.manual_action(command)["execution_job"]["result_reviewed"] is True
    assert complete(control, session_id)["station"]["active_session"]["stage"] == "DECISION_REQUIRED"


@pytest.mark.parametrize("changed_field,changed_value", [
    ("return_info", '{"late_result": true}'),
    ("error_info", '[{"message":"迟到故障"}]'),
    ("uncertainty_reason", "new_unknown_evidence"),
    ("status", "timeout"),
    ("feedback_data", '{"moving":true}'),
    ("start_state", "UNKNOWN"),
])
def test_late_result_invalidates_review_and_old_review_request(handling, changed_field, changed_value):
    store, control, session_id, _ = handling
    _, job, command = seed_manual(store, session_id, status="canceled", uncertainty="cancel_unknown")
    request = review_request(control, session_id, command)
    control.review_manual(command, request, idempotency_key="review", operator_id="operator")
    with store.transaction() as conn:
        conn.execute(f"UPDATE workflow_node_job SET {changed_field}=? WHERE uuid=?", (changed_value, job))
    assert not control.manual_action(command)["execution_job"]["result_reviewed"]
    with pytest.raises(StoreConflict, match="^manual_actions_unresolved$"):
        complete(control, session_id)
    request["expected_station_version"] = control.snapshot()["station_version"]
    with pytest.raises(StoreConflict, match="^manual_review_version_conflict$"):
        control.review_manual(command, request, idempotency_key="review-new", operator_id="operator")


@pytest.mark.parametrize("status", ["pending", "running", "cancel_requested", "unknown"])
def test_review_cannot_confirm_an_unfinished_action(handling, status):
    store, control, session_id, _ = handling
    _, job, command = seed_manual(store, session_id, status=status, uncertainty="unknown")
    before = store.get_job(job), control.snapshot()
    with pytest.raises(StoreConflict, match="^manual_action_not_finished$"):
        control.review_manual(command, review_request(control, session_id, command),
                              idempotency_key="review", operator_id="operator")
    assert (store.get_job(job), control.snapshot()) == before


def test_complete_checks_only_current_session_and_valid_manual_jobs(handling):
    store, control, session_id, _ = handling
    for station_id in ("local", "other-station"):
        other_session = str(uuid4())
        with store.transaction() as conn:
            conn.execute("INSERT INTO workflow_error_session(session_id,station_id,decision_id,stage,created_by,created_at) "
                         "VALUES(?,?,?,'RESOLVED','operator',?)",
                         (other_session, station_id, str(uuid4()), utc_now()))
        seed_manual(store, other_session)
    seed_manual(store, session_id, deleted=True)
    seed_manual(store, session_id, source="workflow")
    seed_manual(store, session_id, status="succeeded")
    assert len(control.get_session(session_id)["manual_actions"]) == 1
    assert complete(control, session_id)["status"] == "APPLIED"


def test_complete_does_not_hide_older_running_action_after_200_finished_actions(handling):
    store, control, session_id, _ = handling
    seed_manual(store, session_id)
    for _ in range(200):
        seed_manual(store, session_id, status="succeeded")
    with pytest.raises(StoreConflict, match="^manual_actions_running$"):
        complete(control, session_id)


@pytest.mark.parametrize("action,resume", [("retry_current_node", False), ("cancel_task", True), ("cancel_task", False)])
def test_old_completed_session_cannot_retry_or_resume_while_manual_action_runs(handling, action, resume):
    store, control, session_id, task = handling
    completed = complete(control, session_id)
    seed_manual(store, session_id)
    error = control.get_session(session_id)["decision"]
    station = control.snapshot()
    before = station, store.list_jobs(task), store.get_task(task)
    with pytest.raises(StoreConflict, match="^manual_actions_running$"):
        control.decide(error["decision_id"], decision_request(
            control, error, action, session_id=session_id,
            expected_session_version=completed["station"]["active_session"]["version"],
            resume_requested=resume, operator_confirmed=True, confirm_snapshot_id=station["snapshot_id"],
        ), idempotency_key="old-page-decision", operator_id="operator")
    assert (control.snapshot(), store.list_jobs(task), store.get_task(task)) == before
    returned = control.return_to_manual(session_id, request_for(control),
                                        idempotency_key="return", operator_id="operator")
    assert returned["station"]["active_session"]["stage"] == "MANUAL_HANDLING"


def test_legacy_resolved_session_running_action_blocks_resume(handling):
    store, control, session_id, _ = handling
    seed_manual(store, session_id)
    # 旧版本已关闭的会话不能通过恢复工站绕开仍在执行的手动动作。
    with store.transaction() as conn:
        conn.execute("UPDATE workflow_error_session SET stage='RESOLVED' WHERE session_id=?", (session_id,))
        conn.execute("UPDATE workflow_station_control SET active_session_id=NULL WHERE station_id='local'")
    station = control.snapshot()
    with pytest.raises(StoreConflict, match="^manual_actions_running$"):
        control.resume({"expected_station_version": station["station_version"],
                        "operator_confirmed": True, "confirm_snapshot_id": station["snapshot_id"]},
                       idempotency_key="resume", operator_id="operator")
    assert control.snapshot()["mode"] == "PAUSED"


@pytest.mark.parametrize("action", ["cancel_task", "retry_current_node"])
def test_late_uncertainty_reopens_unapplied_decision_for_review(handling, action):
    store, control, session_id, source_task = handling
    _, job, command = seed_manual(store, session_id, status="succeeded")
    complete(control, session_id)
    error = control.get_session(session_id)["decision"]
    accepted = control.decide(error["decision_id"], decision_request(
        control, error, action, session_id=session_id,
        expected_session_version=control.get_session(session_id)["version"], resume_requested=False,
    ), idempotency_key="decision", operator_id="operator")
    with store.transaction() as conn:
        conn.execute("UPDATE workflow_node_job SET uncertainty_reason='late_unknown' WHERE uuid=?", (job,))
    before = control.snapshot()
    with pytest.raises(StoreConflict, match="^manual_actions_unresolved$"):
        control.mark_applied(accepted["command_uuid"])
    assert control.snapshot() == before
    with pytest.raises(StoreConflict, match="^manual_actions_unresolved$"):
        control.validate_pending_decision(accepted["command_uuid"])
    assert control.get_command(accepted["command_uuid"])["status"] == "FAILED"
    assert control.get_session(session_id)["stage"] == "MANUAL_HANDLING"
    assert control.snapshot()["mode"] == "PAUSED"
    assert len(store.list_jobs(source_task)) == 1
    assert store.get_task(source_task)["status"] == "running"
    assert store.get_job(error["source_job_uuid"])["current_attempt"] is True
    control.review_manual(command, review_request(control, session_id, command),
                          idempotency_key="review-late-result", operator_id="operator")
    completed = control.complete(session_id, request_for(control), idempotency_key="complete-again", operator_id="operator")
    assert completed["station"]["active_session"]["stage"] == "DECISION_REQUIRED"


def test_public_api_rejects_completion_and_requires_explicit_versioned_review(handling):
    store, control, session_id, _ = handling
    _, _, command = seed_manual(store, session_id, status="canceled", uncertainty="cancel_unknown")
    app = FastAPI()
    app.include_router(create_error_handling_router(SimpleNamespace(control=control, complete=control.complete)), prefix="/api/v1")
    base = "/api/v1/stations/local"
    with TestClient(app) as client:
        response = client.post(base + f"/sessions/{session_id}/complete", json=request_for(control),
                               headers={"Idempotency-Key": "complete"})
        assert response.status_code == 409
        assert response.json()["detail"] == "manual_actions_unresolved"
        request = review_request(control, session_id, command)
        url = base + f"/manual-actions/{command}/review"
        for changes, code in [({"operator_confirmed": False}, 409), ({"reason": "   "}, 422),
                              ({"expected_review_version": "stale"}, 409),
                              ({"expected_session_version": 999}, 409)]:
            response = client.post(url, json={**request, **changes}, headers={"Idempotency-Key": "review"})
            assert response.status_code == code, response.text
        response = client.post(url, json=request, headers={"Idempotency-Key": "review"})
        assert response.status_code == 200, response.text
        assert response.json()["data"]["station"]["manual_actions"][0]["result_reviewed"] is True



@pytest.mark.parametrize("action", ["cancel_task", "retry_current_node"])
def test_bridge_revalidates_before_any_execution_side_effect(handling, monkeypatch, action):
    from unilabos.app.scheduler.dispatch import RecordingDispatcher
    from unilabos.app.scheduler.service import EdgeScheduler
    from unilabos.workflow.task_scheduler_bridge import TaskSchedulerBridge

    store, control, session_id, _ = handling
    _, job, _ = seed_manual(store, session_id, status="succeeded")
    complete(control, session_id)
    error = control.get_session(session_id)["decision"]
    accepted = control.decide(error["decision_id"], decision_request(
        control, error, action, session_id=session_id,
        expected_session_version=control.get_session(session_id)["version"], resume_requested=False,
    ), idempotency_key="decision", operator_id="operator")
    with store.transaction() as conn:
        conn.execute("UPDATE workflow_node_job SET uncertainty_reason='late_unknown' WHERE uuid=?", (job,))
    scheduler = EdgeScheduler(dispatcher=RecordingDispatcher())
    bridge = TaskSchedulerBridge(store, scheduler=scheduler)
    calls = []
    monkeypatch.setattr(bridge, "cancel", lambda *args, **kwargs: calls.append("cancel"))
    monkeypatch.setattr(scheduler, "retry_node", lambda *args, **kwargs: calls.append("retry"))
    try:
        result = bridge.apply_error_command(accepted)
        assert result["status"] == "FAILED"
        assert result["application_error"] == "manual_actions_unresolved"
        assert result["station"]["active_session"]["stage"] == "MANUAL_HANDLING"
        assert calls == []
    finally:
        bridge.close()


def test_started_decision_keeps_original_effect_and_can_review_before_finishing(handling, monkeypatch):
    from unilabos.app.scheduler.dispatch import RecordingDispatcher
    from unilabos.app.scheduler.service import EdgeScheduler
    from unilabos.workflow.task_scheduler_bridge import TaskSchedulerBridge

    store, control, session_id, source_task = handling
    _, job, command = seed_manual(store, session_id, status="succeeded")
    complete(control, session_id)
    error = control.get_session(session_id)["decision"]
    accepted = control.decide(error["decision_id"], decision_request(
        control, error, "cancel_task", session_id=session_id,
        expected_session_version=control.get_session(session_id)["version"], resume_requested=False,
    ), idempotency_key="decision", operator_id="operator")
    control.validate_pending_decision(accepted["command_uuid"])
    with store.transaction() as conn:
        conn.execute("UPDATE workflow_task SET status='canceled' WHERE uuid=?", (source_task,))
    control.mark_decision_effects_applied(accepted["command_uuid"])
    with store.transaction() as conn:
        conn.execute("UPDATE workflow_node_job SET uncertainty_reason='late_unknown' WHERE uuid=?", (job,))
    with pytest.raises(StoreConflict, match="^manual_actions_unresolved$"):
        control.validate_pending_decision(accepted["command_uuid"])
    assert control.get_command(accepted["command_uuid"])["status"] == "PENDING"
    assert control.get_session(session_id)["decision"]["stage"] == "APPLYING"
    assert store.get_task(source_task)["status"] == "canceled"
    control.review_manual(command, review_request(control, session_id, command),
                          idempotency_key="review-after-application", operator_id="operator")
    scheduler = EdgeScheduler(dispatcher=RecordingDispatcher())
    bridge = TaskSchedulerBridge(store, scheduler=scheduler)
    calls = []
    monkeypatch.setattr(bridge, "cancel", lambda *args, **kwargs: calls.append("cancel"))
    try:
        result = bridge.apply_error_command(accepted)
        assert result["status"] == "APPLIED"
        assert result["station"]["active_session"] is None
        assert calls == []
        assert store.get_task(source_task)["status"] == "canceled"
    finally:
        bridge.close()


def test_legacy_pending_decision_without_application_boundary_is_not_rolled_back(handling):
    store, control, session_id, source_task = handling
    _, job, command = seed_manual(store, session_id, status="succeeded")
    complete(control, session_id)
    error = control.get_session(session_id)["decision"]
    accepted = control.decide(error["decision_id"], decision_request(
        control, error, "cancel_task", session_id=session_id,
        expected_session_version=control.get_session(session_id)["version"], resume_requested=False,
    ), idempotency_key="legacy-decision", operator_id="operator")
    with store.transaction() as conn:
        result = dict(accepted["result"])
        result.pop("application_started")
        conn.execute("UPDATE workflow_station_control_command SET snapshot_json=? WHERE command_uuid=?",
                     (json.dumps(result), accepted["command_uuid"]))
        conn.execute("UPDATE workflow_node_job SET uncertainty_reason='late_unknown' WHERE uuid=?", (job,))
    with pytest.raises(StoreConflict, match="^manual_actions_unresolved$"):
        control.validate_pending_decision(accepted["command_uuid"])
    assert control.get_command(accepted["command_uuid"])["status"] == "PENDING"
    assert store.get_task(source_task)["status"] == "canceling"
    control.review_manual(command, review_request(control, session_id, command),
                          idempotency_key="review-legacy", operator_id="operator")
    assert control.manual_action(command)["execution_job"]["result_reviewed"] is True


def test_retry_of_legacy_manual_action_does_not_block_its_own_resume(handling):
    store, control, old_session_id, _ = handling
    _, manual_job, _ = seed_manual(store, old_session_id, status="failed")
    with store.transaction() as conn:
        conn.execute("UPDATE workflow_error_session SET stage='RESOLVED' WHERE session_id=?", (old_session_id,))
        conn.execute("UPDATE workflow_station_control SET active_session_id=NULL,mode='RUNNING' WHERE station_id='local'")
    error = control.report_error(manual_job, phase="execution", message="旧手动动作迟到失败")
    entered = control.decide(error["decision_id"], decision_request(control, error, "enter_manual_handling"),
                             idempotency_key="enter-manual-error", operator_id="operator")
    session_id = entered["station"]["active_session"]["session_id"]
    complete(control, session_id)
    station = control.snapshot()
    error = control.get_session(session_id)["decision"]
    accepted = control.decide(error["decision_id"], decision_request(
        control, error, "retry_current_node", session_id=session_id,
        expected_session_version=station["active_session"]["version"], resume_requested=True,
        operator_confirmed=True, confirm_snapshot_id=station["snapshot_id"],
    ), idempotency_key="retry-manual-error", operator_id="operator")
    control.validate_pending_decision(accepted["command_uuid"])
    assert control.mark_applied(accepted["command_uuid"])["station"]["mode"] == "RUNNING"
    assert store.get_job(accepted["result"]["retry_job_uuid"])["status"] == "pending"
