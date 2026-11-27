"""WorkflowTask 软删除的终态与资源门禁合同。"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from unilabos.app.workflow_api import create_workflow_app
from unilabos.workflow.intervention import ensure_intervention_schema
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.store import WorkflowStore, utc_now


def _client(tmp_path):
    store = WorkflowStore(tmp_path / "workflow_history.db")
    return TestClient(create_workflow_app(WorkflowService(store))), store


def _task(client: TestClient, store: WorkflowStore) -> str:
    workflow = client.post(
        "/api/v1/workflows", json={"name": "delete test", "tags": [], "meta_data": {}}
    ).json()["data"]
    task = client.post(
        "/api/v1/workflow-tasks",
        json={"workflow_uuid": workflow["uuid"], "run_mode": "normal", "meta_data": {}},
    ).json()["data"]
    return task["uuid"]


def _mark_terminal(store: WorkflowStore, task_uuid: str, *, status: str = "succeeded", cleanup: str = "none") -> None:
    now = utc_now()
    with store.transaction() as connection:
        connection.execute(
            "UPDATE workflow_task SET status=?, cleanup_status=?, finished_at=?, update_time=? WHERE uuid=?",
            (status, cleanup, now, now, task_uuid),
        )


def test_terminal_task_with_no_resources_is_soft_deleted_and_idempotent(tmp_path):
    client, store = _client(tmp_path)
    task_uuid = _task(client, store)
    _mark_terminal(store, task_uuid)

    detail = client.get(f"/api/v1/workflow-tasks/{task_uuid}").json()["data"]
    assert detail["can_delete"] is True
    assert detail["delete_blockers"] == []

    deleted = client.delete(f"/api/v1/workflow-tasks/{task_uuid}")
    assert deleted.status_code == 200
    assert deleted.json()["data"]["status"] == "deleted"
    assert client.get(f"/api/v1/workflow-tasks/{task_uuid}").json()["code"] == 3002
    assert client.get("/api/v1/workflow-tasks").json()["data"]["total"] == 0
    assert store._conn.execute(
        "SELECT deleted_at FROM workflow_task WHERE uuid=?", (task_uuid,)
    ).fetchone()["deleted_at"]
    audit = store._conn.execute(
        """
        SELECT kind, from_status, to_status, data
        FROM workflow_runtime_journal
        WHERE workflow_task_uuid = ?
        ORDER BY sequence DESC LIMIT 1
        """,
        (task_uuid,),
    ).fetchone()
    assert audit["kind"] == "task_transition"
    assert audit["from_status"] == audit["to_status"] == "succeeded"
    assert json.loads(audit["data"])["event"] == "task_deleted"

    repeated = client.delete(f"/api/v1/workflow-tasks/{task_uuid}")
    assert repeated.status_code == 200
    assert repeated.json()["data"]["status"] == "already_deleted"
    store.close()


def test_non_terminal_task_exposes_blocker_and_delete_returns_conflict(tmp_path):
    client, store = _client(tmp_path)
    task_uuid = _task(client, store)

    detail = client.get(f"/api/v1/workflow-tasks/{task_uuid}").json()["data"]
    assert detail["can_delete"] is False
    assert any("尚未进入终态" in value for value in detail["delete_blockers"])

    response = client.delete(f"/api/v1/workflow-tasks/{task_uuid}")
    assert response.status_code == 200
    assert response.json()["code"] == 3003
    assert store._conn.execute(
        "SELECT deleted_at FROM workflow_task WHERE uuid=?", (task_uuid,)
    ).fetchone()["deleted_at"] is None
    store.close()


def test_cleanup_residue_blocks_delete_even_when_task_is_terminal(tmp_path):
    client, store = _client(tmp_path)
    task_uuid = _task(client, store)
    _mark_terminal(store, task_uuid, status="failed", cleanup="requires_attention")

    detail = client.get(f"/api/v1/workflow-tasks/{task_uuid}").json()["data"]
    assert detail["can_delete"] is False
    assert any("清理状态" in value for value in detail["delete_blockers"])
    response = client.delete(f"/api/v1/workflow-tasks/{task_uuid}")
    assert response.json()["code"] == 3003
    store.close()


def test_delete_eligibility_endpoint_reports_terminal_task_as_deletable(tmp_path):
    client, store = _client(tmp_path)
    task_uuid = _task(client, store)

    blocked = client.get(f"/api/v1/workflow-tasks/{task_uuid}/delete-eligibility")
    assert blocked.status_code == 200
    blocked_data = blocked.json()["data"]
    assert blocked_data["workflow_task_uuid"] == task_uuid
    assert blocked_data["can_delete"] is False
    assert blocked_data["already_deleted"] is False
    assert any("尚未进入终态" in value for value in blocked_data["delete_blockers"])

    _mark_terminal(store, task_uuid, status="canceled", cleanup="settled")
    ready = client.get(f"/api/v1/workflow-tasks/{task_uuid}/delete-eligibility").json()["data"]
    assert ready["can_delete"] is True
    assert ready["delete_blockers"] == []
    assert ready["status"] == "canceled"
    assert ready["cleanup_status"] == "settled"

    # 列表读契约保持紧凑：资格只在按任务的资格接口上提供。
    listed = client.get("/api/v1/workflow-tasks").json()["data"]["items"][0]
    assert "can_delete" not in listed
    store.close()


def test_delete_eligibility_endpoint_is_readable_after_soft_delete(tmp_path):
    client, store = _client(tmp_path)
    task_uuid = _task(client, store)
    _mark_terminal(store, task_uuid)
    assert client.delete(f"/api/v1/workflow-tasks/{task_uuid}").json()["data"]["status"] == "deleted"

    eligibility = client.get(f"/api/v1/workflow-tasks/{task_uuid}/delete-eligibility").json()["data"]
    assert eligibility["already_deleted"] is True
    assert eligibility["can_delete"] is False
    assert eligibility["delete_blockers"] == []
    assert eligibility["deleted_at"]
    store.close()


def test_delete_eligibility_endpoint_rejects_unknown_task(tmp_path):
    client, store = _client(tmp_path)
    missing = client.get(f"/api/v1/workflow-tasks/{uuid4()}/delete-eligibility")
    assert missing.json()["code"] == 3002
    invalid = client.get("/api/v1/workflow-tasks/not-a-uuid/delete-eligibility")
    assert invalid.json()["code"] != 0
    store.close()


def test_open_intervention_is_reported_as_delete_blocker(tmp_path):
    client, store = _client(tmp_path)
    task_uuid = _task(client, store)
    _mark_terminal(store, task_uuid, status="failed", cleanup="settled")
    now = utc_now()
    with store.transaction() as connection:
        ensure_intervention_schema(connection)
        job_uuid = str(uuid4())
        connection.execute(
            """
            INSERT INTO workflow_node_job(
                uuid, create_time, update_time, meta_data, workflow_task_uuid,
                workflow_node_uuid, feedback_sequence, topological_index,
                executor_kind, execution_policy, execution_timeout_seconds,
                status, attempt, param, feedback_data, return_info,
                control_data, error_info, finished_at
            ) VALUES (?, ?, ?, '{}', ?, 'node-1', 0, 0, 'robot', '{}', 0,
                      'succeeded', 1, '{}', '{}', '{}', '{}', '[]', ?)
            """,
            (job_uuid, now, now, task_uuid, now),
        )
        connection.execute(
            """
            INSERT INTO workflow_intervention(
                uuid, create_time, update_time, workflow_task_uuid,
                workflow_node_job_uuid, decision_id, revision, status,
                options, resume_control_status, opened_at
            ) VALUES (?, ?, ?, ?, ?, ?, 1, 'open', '[]', 'paused', ?)
            """,
            (str(uuid4()), now, now, task_uuid, job_uuid, str(uuid4()), now),
        )

    detail = client.get(f"/api/v1/workflow-tasks/{task_uuid}").json()["data"]
    assert detail["can_delete"] is False
    assert any("人工干预" in value for value in detail["delete_blockers"])
    store.close()


def _edge_error_task(tmp_path, *, resolution="cancel_task"):
    """通过原生控制器产生已解决异常，保留原始干预状态供删除门禁判断。"""
    from tests.workflow.test_error_control import decision_request, seed_job
    from unilabos.workflow.error_control import StationExecutionControl
    from unilabos.workflow.task_runtime_projection import TaskRuntimeProjection

    store = WorkflowStore(tmp_path / "edge-error.db")
    task_uuid, job_uuid = seed_job(store)
    control = StationExecutionControl(store)
    projection = TaskRuntimeProjection(store)
    if resolution == "proved_success":
        control.report_error(
            job_uuid, phase="execution", message=f"执行结果未知：workflow-node-job:{job_uuid}",
        )
        assert control.accept_proved_success(job_uuid) is True
        projection.project_job_finished(job_uuid=job_uuid, scheduler_state="success", return_info={})
    else:
        error = control.report_error(job_uuid, phase="execution", message="设备故障")
        command = control.decide(
            error["decision_id"], decision_request(control, error, "cancel_task"),
            idempotency_key="delete-resolved-error", operator_id="test-operator",
        )
        projection.project_job_finished(job_uuid=job_uuid, scheduler_state="canceled")
        control.mark_applied(command["command_uuid"])
    if resolution != "proved_success":
        projection.project_cleanup_settled(task_uuid)
    return TestClient(create_workflow_app(WorkflowService(store))), store, task_uuid, job_uuid, control


@pytest.mark.parametrize("resolution,expected_status", [("cancel_task", "selected"), ("proved_success", "open")])
def test_resolved_native_error_no_longer_blocks_task_deletion(tmp_path, resolution, expected_status):
    """已解决的原生错误允许删除；不改写错误历史，重复删除仍幂等。"""
    client, store, task_uuid, job_uuid, control = _edge_error_task(tmp_path, resolution=resolution)
    try:
        assert control.snapshot()["errors"] == []
        assert store.center_attention_snapshot()["exception_count"] == 0
        with store.read() as connection:
            before = dict(connection.execute(
                "SELECT * FROM workflow_intervention WHERE workflow_node_job_uuid=?", (job_uuid,),
            ).fetchone())
        assert before["status"] == expected_status
        assert before["stage"] == "RESOLVED"
        detail = client.get(f"/api/v1/workflow-tasks/{task_uuid}").json()["data"]
        eligibility = client.get(f"/api/v1/workflow-tasks/{task_uuid}/delete-eligibility").json()["data"]
        assert detail["can_delete"] is True
        assert eligibility["can_delete"] is True
        assert eligibility["delete_blockers"] == []
        assert client.delete(f"/api/v1/workflow-tasks/{task_uuid}").json()["data"]["status"] == "deleted"
        assert client.delete(f"/api/v1/workflow-tasks/{task_uuid}").json()["data"]["status"] == "already_deleted"
        with store.read() as connection:
            after = dict(connection.execute(
                "SELECT * FROM workflow_intervention WHERE workflow_node_job_uuid=?", (job_uuid,),
            ).fetchone())
        assert after == before
    finally:
        store.close()


@pytest.mark.parametrize("stage", ["DECISION_REQUIRED", "MANUAL_HANDLING", "APPLYING", "UNKNOWN"])
def test_unresolved_or_unknown_native_error_still_blocks_deletion(tmp_path, stage):
    """只放行明确已解决的异常；未解决及无法识别的阶段继续阻止删除。"""
    client, store, task_uuid, job_uuid, _ = _edge_error_task(tmp_path)
    try:
        with store.transaction() as connection:
            connection.execute(
                "UPDATE workflow_intervention SET stage=? WHERE workflow_node_job_uuid=?", (stage, job_uuid),
            )
        eligibility = client.get(f"/api/v1/workflow-tasks/{task_uuid}/delete-eligibility").json()["data"]
        assert eligibility["can_delete"] is False
        assert any("人工干预" in item for item in eligibility["delete_blockers"])
        assert client.delete(f"/api/v1/workflow-tasks/{task_uuid}").json()["code"] == 3003
    finally:
        store.close()


@pytest.mark.parametrize("status", ["open", "selected"])
def test_device_intervention_keeps_original_delete_guard(tmp_path, status):
    """设备干预仍按原有 status 判断，不套用节点错误的 RESOLVED 规则。"""
    client, store, task_uuid, job_uuid, _ = _edge_error_task(tmp_path)
    try:
        with store.transaction() as connection:
            connection.execute(
                "UPDATE workflow_intervention SET decision_kind='device_intervention', status=? WHERE workflow_node_job_uuid=?",
                (status, job_uuid),
            )
        eligibility = client.get(f"/api/v1/workflow-tasks/{task_uuid}/delete-eligibility").json()["data"]
        assert eligibility["can_delete"] is False
        assert any("人工干预" in item for item in eligibility["delete_blockers"])
        assert client.delete(f"/api/v1/workflow-tasks/{task_uuid}").json()["code"] == 3003
    finally:
        store.close()


def test_resolved_error_does_not_bypass_unsettled_cleanup(tmp_path):
    """异常已解决不代表资源已收敛；其他删除门禁继续有效。"""
    client, store, task_uuid, _, _ = _edge_error_task(tmp_path)
    try:
        _mark_terminal(store, task_uuid, status="canceled", cleanup="requires_attention")
        eligibility = client.get(f"/api/v1/workflow-tasks/{task_uuid}/delete-eligibility").json()["data"]
        assert eligibility["can_delete"] is False
        assert any("清理状态" in item for item in eligibility["delete_blockers"])
        assert not any("人工干预" in item for item in eligibility["delete_blockers"])
        assert client.delete(f"/api/v1/workflow-tasks/{task_uuid}").json()["code"] == 3003
    finally:
        store.close()



def test_approved_completed_manual_task_can_be_deleted_without_rewriting_history(tmp_path):
    """人工批准是历史决定，动作成功收尾后允许软删除且保留批准记录。"""
    from tests.scheduler_core.conftest import build_core_runtime

    runtime = build_core_runtime(tmp_path / "manual-completed")
    try:
        pending, job_uuid = runtime.submit_manual(task_name="approved-completed-delete")
        service = WorkflowService(runtime.workflow_store, task_scheduler_bridge=runtime.bridge)
        service.decide_manual_confirmation(job_uuid, action="approve")
        runtime.scheduler.on_job_finished(job_uuid, True, {}, "normal")
        task_uuid = pending["task"]["uuid"]
        store = runtime.workflow_store
        assert store.get_task(task_uuid)["status"] == "succeeded"
        assert store.get_job(job_uuid)["status"] == "succeeded"
        assert service.center_attention_snapshot()["manual_confirmation_count"] == 0
        with store.read() as connection:
            before = dict(connection.execute(
                "SELECT * FROM workflow_manual_confirmation WHERE workflow_node_job_uuid=?",
                (job_uuid,),
            ).fetchone())
        assert before["status"] == "approved"
        with TestClient(create_workflow_app(service)) as client:
            detail = client.get(f"/api/v1/workflow-tasks/{task_uuid}").json()["data"]
            eligibility = client.get(f"/api/v1/workflow-tasks/{task_uuid}/delete-eligibility").json()["data"]
            assert detail["can_delete"] is True
            assert eligibility["can_delete"] is True
            assert eligibility["delete_blockers"] == []
            assert client.delete(f"/api/v1/workflow-tasks/{task_uuid}").json()["data"]["status"] == "deleted"
            assert client.delete(f"/api/v1/workflow-tasks/{task_uuid}").json()["data"]["status"] == "already_deleted"
        with store.read() as connection:
            after = dict(connection.execute(
                "SELECT * FROM workflow_manual_confirmation WHERE workflow_node_job_uuid=?",
                (job_uuid,),
            ).fetchone())
        assert after == before
    finally:
        runtime.close()


@pytest.mark.parametrize("approved", [False, True])
def test_manual_confirmation_does_not_bypass_unfinished_job_guard(tmp_path, approved):
    """尚未批准或已批准但动作未完成时，都不能因父任务终态而跳过 Job 门禁。"""
    from tests.scheduler_core.conftest import build_core_runtime

    runtime = build_core_runtime(tmp_path / "manual-unfinished")
    try:
        pending, job_uuid = runtime.submit_manual(task_name="manual-unfinished-delete")
        service = WorkflowService(runtime.workflow_store, task_scheduler_bridge=runtime.bridge)
        if approved:
            service.decide_manual_confirmation(job_uuid, action="approve")
        task_uuid = pending["task"]["uuid"]
        _mark_terminal(runtime.workflow_store, task_uuid)
        with TestClient(create_workflow_app(service)) as client:
            eligibility = client.get(f"/api/v1/workflow-tasks/{task_uuid}/delete-eligibility").json()["data"]
            assert eligibility["can_delete"] is False
            assert any("Job 未进入终态" in item for item in eligibility["delete_blockers"])
            assert any("人工确认未完成" in item for item in eligibility["delete_blockers"]) is (not approved)
            assert client.delete(f"/api/v1/workflow-tasks/{task_uuid}").json()["code"] == 3003
    finally:
        runtime.close()
