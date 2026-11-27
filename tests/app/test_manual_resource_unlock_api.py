"""异常工作流任务人工解锁资源的公开 HTTP 合同。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.scheduler_core.conftest import build_core_runtime
from unilabos.app.workflow_api import create_workflow_app
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.store import utc_now


def _mark_failed_with_retained_resources(runtime, *, task_uuid: str, job_uuid: str) -> None:
    """模拟失败后仍需操作员确认的设备、物料与执行占用。"""

    now = utc_now()
    material_uuid = runtime.device_materials["reactor-a"]
    with runtime.workflow_store.transaction() as connection:
        node_uuid = str(
            connection.execute(
                "SELECT workflow_node_uuid FROM workflow_node_job WHERE uuid=?",
                (job_uuid,),
            ).fetchone()[0]
        )
        connection.execute(
            """
            UPDATE workflow_node_job
            SET status='failed', uncertainty_reason='operator_safety_check_required',
                finished_at=?, update_time=?
            WHERE uuid=?
            """,
            (now, now, job_uuid),
        )
        connection.execute(
            """
            UPDATE workflow_task
            SET status='failed', cleanup_status='requires_attention',
                control_status='waiting_reconciliation',
                attention_reason='operator_safety_check_required',
                finished_at=?, update_time=?
            WHERE uuid=?
            """,
            (now, now, task_uuid),
        )
        connection.execute(
            "UPDATE execution_claim SET state='uncertain', update_time=? "
            "WHERE workflow_task_uuid=? AND state IN ('reserved', 'running')",
            (now, task_uuid),
        )
        connection.execute(
            "UPDATE execution_lock_lease SET state='uncertain', update_time=? "
            "WHERE workflow_task_uuid=? AND state IN ('reserved', 'running')",
            (now, task_uuid),
        )
        connection.execute(
            """
            INSERT INTO workflow_task_material_claim(
                uuid, create_time, update_time, deleted_at, description,
                meta_data, workflow_task_uuid, workflow_node_uuid,
                workflow_node_job_uuid, material_uuid, status, revision,
                acquired_at, released_at
            ) VALUES (
                '71000000-0000-4000-8000-000000000001', ?, ?, NULL, NULL,
                '{}', ?, ?, ?, ?, 'active', 1, ?, NULL
            )
            """,
            (now, now, task_uuid, node_uuid, job_uuid, material_uuid, now),
        )
        connection.execute(
            """
            INSERT INTO task_device_tenancy(
                uuid, create_time, update_time, workflow_task_uuid,
                material_uuid, device_lock_key, acquired_by_job_uuid,
                released_by_job_uuid, state, acquired_at, released_at
            ) VALUES (
                '72000000-0000-4000-8000-000000000001', ?, ?, ?, ?,
                '/devices/reactor-a', ?, NULL, 'active', ?, NULL
            )
            """,
            (now, now, task_uuid, material_uuid, job_uuid, now),
        )


def _unlock_body(*, confirmed: bool = True) -> dict[str, object]:
    return {
        "type": "unlock_resources",
        "target_node_uuid": None,
        "idempotency_key": "manual-unlock-request-1",
        "description": "操作员已确认设备停止，并完成现场物料位置盘点",
        "meta_data": {
            "source": "unilabos-frontend",
            "confirmed_physical_safe": confirmed,
        },
    }


def test_operator_can_unlock_all_resources_retained_by_failed_task(
    tmp_path: Path,
) -> None:
    """一次幂等命令释放双库和内存占用，并唤醒等待同一设备的任务。"""

    runtime = build_core_runtime(tmp_path / "runtime")
    client = TestClient(
        create_workflow_app(
            WorkflowService(
                runtime.workflow_store,
                task_scheduler_bridge=runtime.bridge,
            )
        )
    )
    try:
        failed = runtime.submit(task_name="failed-owner", devices=["reactor-a"])
        task_uuid = str(failed["task"]["uuid"])
        job_uuid = str(failed["jobs"][0]["uuid"])
        _mark_failed_with_retained_resources(
            runtime,
            task_uuid=task_uuid,
            job_uuid=job_uuid,
        )

        waiting = runtime.submit(task_name="waiting-successor", devices=["reactor-a"])
        waiting_task_uuid = str(waiting["task"]["uuid"])
        waiting_job_uuid = str(waiting["jobs"][0]["uuid"])
        assert runtime.workflow_store.get_job(waiting_job_uuid)["status"] == "pending"

        first = client.post(
            f"/api/v1/workflow-tasks/{task_uuid}/commands",
            json=_unlock_body(),
        )
        replay = client.post(
            f"/api/v1/workflow-tasks/{task_uuid}/commands",
            json=_unlock_body(),
        )

        assert first.status_code == 201
        assert first.json()["code"] == 0
        command = first.json()["data"]
        assert command["status"] == "succeeded"
        assert command["result"]["cleanup_status"] == "settled"
        assert command["result"]["released"] == {
            "device_tenancies": 1,
            "execution_claims": 1,
            "execution_locks": 1,
            "task_material_claims": 1,
        }
        assert replay.status_code == 201
        assert replay.json()["data"]["uuid"] == command["uuid"]

        failed_task = client.get(
            f"/api/v1/workflow-tasks/{task_uuid}"
        ).json()["data"]
        assert failed_task["status"] == "failed"
        assert failed_task["cleanup_status"] == "settled"
        assert failed_task["control_status"] == "active"
        assert failed_task.get("attention_reason") is None
        assert runtime.workflow_store.get_job(waiting_job_uuid)["status"] == (
            "running"
        )
        assert runtime.scheduler.snapshot()["inflight_jobs"] == {
            waiting_job_uuid: runtime.scheduler.snapshot()["inflight_jobs"][
                waiting_job_uuid
            ]
        }
        assert runtime.workflow_store.get_task(waiting_task_uuid)["status"] == (
            "running"
        )
    finally:
        client.close()
        runtime.close()


def test_manual_unlock_requires_confirmation_and_abnormal_terminal_task(
    tmp_path: Path,
) -> None:
    """未确认现场安全或任务仍运行时，不允许释放任何资源。"""

    runtime = build_core_runtime(tmp_path / "guard-runtime")
    client = TestClient(
        create_workflow_app(
            WorkflowService(
                runtime.workflow_store,
                task_scheduler_bridge=runtime.bridge,
            )
        )
    )
    try:
        active = runtime.submit(task_name="active-owner", devices=["reactor-a"])
        task_uuid = str(active["task"]["uuid"])
        job_uuid = str(active["jobs"][0]["uuid"])

        unconfirmed = client.post(
            f"/api/v1/workflow-tasks/{task_uuid}/commands",
            json=_unlock_body(confirmed=False),
        )
        assert unconfirmed.status_code == 200
        assert unconfirmed.json()["code"] == 1000

        still_running = client.post(
            f"/api/v1/workflow-tasks/{task_uuid}/commands",
            json={**_unlock_body(), "idempotency_key": "active-unlock-request"},
        )
        assert still_running.status_code == 201
        assert still_running.json()["code"] == 0
        assert still_running.json()["data"]["status"] == "rejected"
        assert still_running.json()["data"]["result"] == {
            "reason": "task_is_not_abnormal_terminal"
        }
        assert runtime.workflow_store.get_job(job_uuid)["status"] == "running"
        assert job_uuid in runtime.scheduler.snapshot()["inflight_jobs"]
    finally:
        client.close()
        runtime.close()


def test_manual_unlock_fails_closed_without_physical_inventory_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实派发模式下缺库存权威时，不能只释放工作流或内存锁。"""

    runtime = build_core_runtime(tmp_path / "missing-authority-runtime")
    client = TestClient(
        create_workflow_app(
            WorkflowService(
                runtime.workflow_store,
                task_scheduler_bridge=runtime.bridge,
            )
        )
    )
    try:
        failed = runtime.submit(task_name="failed-without-authority", devices=["reactor-a"])
        task_uuid = str(failed["task"]["uuid"])
        job_uuid = str(failed["jobs"][0]["uuid"])
        _mark_failed_with_retained_resources(
            runtime,
            task_uuid=task_uuid,
            job_uuid=job_uuid,
        )
        monkeypatch.setattr(runtime.scheduler, "_station_resources", None)

        response = client.post(
            f"/api/v1/workflow-tasks/{task_uuid}/commands",
            json={**_unlock_body(), "idempotency_key": "missing-authority"},
        )

        assert response.status_code == 201
        assert response.json()["data"]["status"] == "rejected"
        assert response.json()["data"]["result"] == {
            "reason": "物理调度未装配库存权威，拒绝人工释放"
        }
        with runtime.workflow_store.transaction() as connection:
            assert connection.execute(
                "SELECT state FROM execution_claim WHERE workflow_task_uuid=?",
                (task_uuid,),
            ).fetchone()["state"] == "uncertain"
            assert connection.execute(
                "SELECT state FROM task_device_tenancy WHERE workflow_task_uuid=?",
                (task_uuid,),
            ).fetchone()["state"] == "active"
        assert job_uuid in runtime.scheduler.snapshot()["inflight_jobs"]
    finally:
        client.close()
        runtime.close()


@pytest.mark.parametrize("execution_process_restarted", [False, True])
def test_canceled_continuous_scope_survives_restart_until_explicit_http_unlock(
    tmp_path: Path,
    execution_process_restarted: bool,
) -> None:
    """连续区间失败后取消并重启，保留整组锁与终态，只有明确命令能释放。"""

    from tests.scheduler_core.test_resource_occupancy_intervals import (
        _three_job_scoped_task,
    )

    runtime = build_core_runtime(tmp_path / "canceled-continuous-scope")
    client = None
    try:
        aggregate, jobs, _plan = _three_job_scoped_task(
            runtime, task_name="canceled-continuous-scope"
        )
        task_uuid = aggregate["task"]["uuid"]
        runtime.scheduler.on_job_finished(jobs[0], False, {"error": "fixture failure"})
        snapshot = runtime.bridge.error_control.snapshot()
        decision = next(
            error for error in snapshot["errors"]
            if error["source_task_uuid"] == task_uuid
        )
        canceled = runtime.bridge.decide_error(
            decision["decision_id"],
            {
                "action": "cancel_task",
                "expected_decision_version": decision["decision_version"],
                "expected_station_version": snapshot["station_version"],
                "observed_error_epoch": snapshot["error_epoch"],
                "resume_requested": False,
            },
            idempotency_key="cancel-continuous-scope",
            operator_id="test-operator",
        )
        assert canceled["status"] == "APPLIED"
        task_before = runtime.workflow_store.get_task(task_uuid)
        jobs_before = runtime.workflow_store.list_jobs(task_uuid)
        assert task_before["status"] == "canceled"
        assert task_before["attention_reason"] == "resource_handoff_required"
        assert [item["status"] for item in jobs_before] == ["failed", "canceled", "canceled"]
        locks_before = runtime.bridge._projection.list_task_execution_locks(task_uuid)
        assert len(locks_before["locks"]) > 1
        assert all(not lock["can_release"] for lock in locks_before["locks"])
        dispatched_before = list(runtime.dispatcher.dispatched)

        runtime.restart_scheduler()
        if execution_process_restarted:
            assert runtime.bridge.recover_active_tasks(execution_process_restarted=True) == []
        assert runtime.workflow_store.get_task(task_uuid) == task_before
        assert runtime.workflow_store.list_jobs(task_uuid) == jobs_before
        assert runtime.bridge._projection.list_task_execution_locks(task_uuid) == locks_before
        assert runtime.dispatcher.dispatched == dispatched_before

        client = TestClient(create_workflow_app(WorkflowService(
            runtime.workflow_store, task_scheduler_bridge=runtime.bridge,
        )))
        response = client.post(
            f"/api/v1/workflow-tasks/{task_uuid}/commands", json=_unlock_body(),
        )
        assert response.status_code == 201
        command = response.json()["data"]
        assert command["status"] == "succeeded"
        assert command["result"]["released"]["execution_locks"] == len(locks_before["locks"])
        assert client.get(f"/api/v1/workflow-tasks/{task_uuid}/execution-locks").json()["data"]["locks"] == []
        restored_task = client.get(f"/api/v1/workflow-tasks/{task_uuid}").json()["data"]
        assert restored_task["status"] == "canceled"
        assert restored_task["cleanup_status"] == "settled"
        assert runtime.dispatcher.dispatched == dispatched_before
        replay = client.post(
            f"/api/v1/workflow-tasks/{task_uuid}/commands", json=_unlock_body(),
        ).json()["data"]
        assert replay["uuid"] == command["uuid"]
        events = client.get(f"/api/v1/workflow-tasks/{task_uuid}/events").json()["data"]["items"]
        assert len([event for event in events if event["kind"] == "lock_operator_released"]) == 1
    finally:
        if client is not None:
            client.close()
        runtime.close()
