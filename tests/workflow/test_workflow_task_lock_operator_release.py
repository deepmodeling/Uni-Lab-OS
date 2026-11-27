"""工作流任务执行锁人工释放的模块与公开接口契约测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from unilabos.app.workflow_api import create_workflow_app
from unilabos.workflow.service import WorkflowConflict, WorkflowService
from unilabos.workflow.store import WorkflowStore
from unilabos.workflow.task_runtime_projection import TaskRuntimeProjection

WORKFLOW_UUID = "11000000-0000-4000-8000-000000000001"
TASK_UUID = "21000000-0000-4000-8000-000000000001"
NODE_UUID = "31000000-0000-4000-8000-000000000001"
JOB_UUID = "41000000-0000-4000-8000-000000000001"
LEASE_A_UUID = "51000000-0000-4000-8000-000000000001"
LEASE_B_UUID = "51000000-0000-4000-8000-000000000002"
CLAIM_UUID = "61000000-0000-4000-8000-000000000001"
CREATED_AT = "2026-09-04T00:00:00Z"


@pytest.fixture()
def store(tmp_path: Path):
    """创建一次测试专用的工作流 SQLite 存储。"""

    opened = WorkflowStore(tmp_path / "workflow.db")
    try:
        yield opened
    finally:
        opened.close()


def _seed_lock_task(
    store: WorkflowStore,
    *,
    task_status: str = "failed",
    job_status: str = "failed",
    lease_state: str = "running",
    uncertainty_reason: str | None = None,
) -> None:
    """写入最小任务、作业、Claim 和双锁租约事实。"""

    store.create_workflow(
        workflow_uuid=WORKFLOW_UUID,
        name="锁处置测试工作流",
        tags=[],
        description=None,
        meta_data={},
    )
    with store.transaction() as connection:
        connection.execute(
            """
            INSERT INTO workflow_task(
                uuid, create_time, update_time, deleted_at, description,
                meta_data, workflow_uuid, status, workflow_snapshot,
                execution_plan, run_mode, execution_mode, target_node_uuid,
                control_status, cleanup_status, trace_context, input, output,
                error_info
            ) VALUES (?, ?, ?, NULL, NULL, '{}', ?, ?, '{}', '{}',
                      'normal', 'normal', NULL, 'active', 'pending', '{}',
                      '{}', '{}', '[]')
            """,
            (TASK_UUID, CREATED_AT, CREATED_AT, WORKFLOW_UUID, task_status),
        )
        connection.execute(
            """
            INSERT INTO workflow_node_job(
                uuid, create_time, update_time, deleted_at, description,
                meta_data, workflow_task_uuid, workflow_node_uuid,
                feedback_sequence, topological_index, executor_kind,
                execution_policy, execution_timeout_seconds, status, attempt,
                param, feedback_data, return_info, control_data, error_info,
                uncertainty_reason
            ) VALUES (?, ?, ?, NULL, NULL, '{}', ?, ?, 0, 0, 'device_action',
                      '{}', 0, ?, 1, '{}', '{}', '{}', '{}', '[]', ?)
            """,
            (
                JOB_UUID,
                CREATED_AT,
                CREATED_AT,
                TASK_UUID,
                NODE_UUID,
                job_status,
                uncertainty_reason,
            ),
        )
        connection.execute(
            """
            INSERT INTO execution_claim(
                claim_uuid, create_time, update_time, workflow_task_uuid,
                workflow_node_job_uuid, attempt, resource_keys, state,
                acquired_at, released_at
            ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, NULL)
            """,
            (
                CLAIM_UUID,
                CREATED_AT,
                CREATED_AT,
                TASK_UUID,
                JOB_UUID,
                json.dumps(["/devices/reactor-a", "/materials/sample"]),
                "uncertain" if lease_state == "uncertain" else "running",
                CREATED_AT,
            ),
        )
        for lease_uuid, lock_key, scope, fencing_token in (
            (LEASE_A_UUID, "/devices/reactor-a", "device", 7),
            (LEASE_B_UUID, "/materials/sample", "material", 11),
        ):
            connection.execute(
                """
                INSERT INTO execution_lock_lease(
                    uuid, create_time, update_time, deleted_at, description,
                    meta_data, workflow_task_uuid, workflow_node_job_uuid,
                    lock_key, scope, material_uuid, site_uuid, state,
                    acquired_at, released_at, claim_uuid, fencing_token
                ) VALUES (?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?, NULL, NULL,
                          ?, ?, NULL, ?, ?)
                """,
                (
                    lease_uuid,
                    CREATED_AT,
                    CREATED_AT,
                    json.dumps({"acquired_by_job_uuid": JOB_UUID}),
                    TASK_UUID,
                    JOB_UUID,
                    lock_key,
                    scope,
                    lease_state,
                    CREATED_AT,
                    CLAIM_UUID,
                    fencing_token,
                ),
            )


def test_task_lock_listing_projects_release_eligibility(store: WorkflowStore) -> None:
    """任务锁列表应返回两把锁及可人工释放的安全资格。"""

    _seed_lock_task(store)
    result = TaskRuntimeProjection(store).list_task_execution_locks(TASK_UUID)

    assert result["workflow_task_uuid"] == TASK_UUID
    assert len(result["locks"]) == 2
    assert all(item["can_release"] for item in result["locks"])
    assert {item["fencing_token"] for item in result["locks"]} == {7, 11}
    assert all(item["claim_uuid"] == CLAIM_UUID for item in result["locks"])


def test_job_level_force_release_rejects_explicit_continuous_interval(
    store: WorkflowStore,
) -> None:
    """连续区间不能按单个 Job 解锁，只能走任务级整组人工释放。"""

    _seed_lock_task(store)
    execution_plan = {
        "resource_plan": {
            "intervals": [
                {
                    "interval_id": "continuous-interval",
                    "explicit_boundary": True,
                    "node_uuids": [NODE_UUID],
                }
            ]
        }
    }
    with store.transaction() as connection:
        connection.execute(
            "UPDATE workflow_task SET execution_plan=? WHERE uuid=?",
            (json.dumps(execution_plan), TASK_UUID),
        )

    listed = TaskRuntimeProjection(store).list_task_execution_locks(TASK_UUID)
    assert all(not item["can_release"] for item in listed["locks"])
    assert all("任务级人工解锁" in item["release_block_reason"] for item in listed["locks"])
    with pytest.raises(WorkflowConflict, match="任务级人工解锁"):
        WorkflowService(store).force_release_workflow_task_execution_lock(
            TASK_UUID,
            LEASE_A_UUID,
            expected_claim_uuid=CLAIM_UUID,
            expected_fencing_token=7,
            reason="不能拆开连续区间",
            physical_settlement_confirmed=True,
        )
    assert {
        item["state"]
        for item in TaskRuntimeProjection(store).list_execution_locks(JOB_UUID)
    } == {"running"}


def test_force_release_is_atomic_audited_and_idempotent(store: WorkflowStore) -> None:
    """人工释放应整组释放锁/Claim、写审计事件，并支持重复点击。"""

    _seed_lock_task(store)
    class _Bridge:
        """记录人工释放后调度唤醒次数的最小桥接替身。"""

        def __init__(self) -> None:
            self.reschedule_calls = 0

        def reschedule(self) -> None:
            """模拟共享调度器重排。"""

            self.reschedule_calls += 1

    bridge = _Bridge()
    service = WorkflowService(store, task_scheduler_bridge=bridge)  # type: ignore[arg-type]
    first = service.force_release_workflow_task_execution_lock(
        TASK_UUID,
        LEASE_A_UUID,
        expected_claim_uuid=CLAIM_UUID,
        expected_fencing_token=7,
        reason="设备已断电并由现场人员确认安全",
        physical_settlement_confirmed=True,
    )

    assert first["status"] == "released"
    assert bridge.reschedule_calls == 1
    assert len(first["released_lock_uuids"]) == 2
    assert all(
        item["state"] == "released"
        for item in TaskRuntimeProjection(store).list_execution_locks(JOB_UUID)
    )
    claim = store._conn.execute(
        "SELECT state FROM execution_claim WHERE claim_uuid = ?",
        (CLAIM_UUID,),
    ).fetchone()
    assert claim["state"] == "released"
    event = store._conn.execute(
        "SELECT kind FROM workflow_runtime_journal WHERE workflow_task_uuid = ? "
        "ORDER BY sequence DESC LIMIT 1",
        (TASK_UUID,),
    ).fetchone()
    assert event["kind"] == "lock_operator_released"
    audit = store._conn.execute(
        "SELECT result, reason FROM execution_lock_operator_action "
        "WHERE workflow_task_uuid = ? ORDER BY create_time DESC LIMIT 1",
        (TASK_UUID,),
    ).fetchone()
    assert audit["result"] == "released"
    assert audit["reason"].startswith("设备已断电")
    assert (
        TaskRuntimeProjection(store).list_task_execution_locks(TASK_UUID)["locks"]
        == []
    )

    replay = service.force_release_workflow_task_execution_lock(
        TASK_UUID,
        LEASE_A_UUID,
        expected_claim_uuid=CLAIM_UUID,
        expected_fencing_token=7,
        reason="重复点击确认现场仍安全",
        physical_settlement_confirmed=True,
    )
    assert replay["status"] == "already_released"
    assert bridge.reschedule_calls == 1


def test_force_release_rejects_uncertain_and_stale_cas(store: WorkflowStore) -> None:
    """结果不确定或页面快照陈旧时必须拒绝释放且保持锁不变。"""

    _seed_lock_task(store, lease_state="uncertain")
    service = WorkflowService(store)
    with pytest.raises(WorkflowConflict):
        service.force_release_workflow_task_execution_lock(
            TASK_UUID,
            LEASE_A_UUID,
            expected_claim_uuid=CLAIM_UUID,
            expected_fencing_token=7,
            reason="不应绕过不确定处置",
            physical_settlement_confirmed=True,
        )
    state = store._conn.execute(
        "SELECT state FROM execution_lock_lease WHERE uuid = ?",
        (LEASE_A_UUID,),
    ).fetchone()
    assert state["state"] == "uncertain"

    with store.transaction() as connection:
        connection.execute(
            "UPDATE execution_lock_lease SET state = 'running' WHERE uuid = ?",
            (LEASE_A_UUID,),
        )
        connection.execute(
            "UPDATE execution_claim SET state = 'running' WHERE claim_uuid = ?",
            (CLAIM_UUID,),
        )
    with pytest.raises(WorkflowConflict):
        service.force_release_workflow_task_execution_lock(
            TASK_UUID,
            LEASE_A_UUID,
            expected_claim_uuid="61000000-0000-4000-8000-000000000099",
            expected_fencing_token=7,
            reason="陈旧页面不应释放",
            physical_settlement_confirmed=True,
        )
    state = store._conn.execute(
        "SELECT state FROM execution_lock_lease WHERE uuid = ?",
        (LEASE_A_UUID,),
    ).fetchone()
    assert state["state"] == "running"


def test_force_release_commits_even_if_scheduler_wakeup_fails(
    store: WorkflowStore,
) -> None:
    """调度器唤醒异常不能回滚已经提交的锁释放事实。"""

    _seed_lock_task(store)

    class _BrokenBridge:
        def reschedule(self) -> None:
            raise RuntimeError("scheduler unavailable")

    service = WorkflowService(store, task_scheduler_bridge=_BrokenBridge())  # type: ignore[arg-type]
    result = service.force_release_workflow_task_execution_lock(
        TASK_UUID,
        LEASE_A_UUID,
        expected_claim_uuid=CLAIM_UUID,
        expected_fencing_token=7,
        reason="调度器恢复前先完成现场锁清理",
        physical_settlement_confirmed=True,
    )
    assert result["status"] == "released"
    state = store._conn.execute(
        "SELECT state FROM execution_lock_lease WHERE uuid = ?",
        (LEASE_A_UUID,),
    ).fetchone()
    assert state["state"] == "released"


def test_lock_listing_and_release_block_claim_uncertainty_and_device_tenancy(
    store: WorkflowStore,
) -> None:
    """Claim 不确定或仍有设备托管时，列表和写入口都必须关闭释放能力。"""

    _seed_lock_task(store)
    with store.transaction() as connection:
        connection.execute(
            "UPDATE execution_claim SET state = 'uncertain' WHERE claim_uuid = ?",
            (CLAIM_UUID,),
        )
    listed = TaskRuntimeProjection(store).list_task_execution_locks(TASK_UUID)
    assert all(not item["can_release"] for item in listed["locks"])
    assert all("Claim" in item["release_block_reason"] for item in listed["locks"])
    service = WorkflowService(store)
    with pytest.raises(WorkflowConflict):
        service.force_release_workflow_task_execution_lock(
            TASK_UUID,
            LEASE_A_UUID,
            expected_claim_uuid=CLAIM_UUID,
            expected_fencing_token=7,
            reason="不应绕过 Claim 不确定状态",
            physical_settlement_confirmed=True,
        )

    with store.transaction() as connection:
        connection.execute(
            "UPDATE execution_claim SET state = 'running' WHERE claim_uuid = ?",
            (CLAIM_UUID,),
        )
        connection.execute(
            """
            INSERT INTO task_device_tenancy(
                uuid, create_time, update_time, workflow_task_uuid,
                material_uuid, device_lock_key, acquired_by_job_uuid,
                released_by_job_uuid, state, acquired_at, released_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 'active', ?, NULL)
            """,
            (
                "71000000-0000-4000-8000-000000000001",
                CREATED_AT,
                CREATED_AT,
                TASK_UUID,
                "81000000-0000-4000-8000-000000000001",
                "/devices/reactor-a",
                JOB_UUID,
                CREATED_AT,
            ),
        )
    listed = TaskRuntimeProjection(store).list_task_execution_locks(TASK_UUID)
    assert listed["active_device_tenancy_count"] == 1
    assert all(not item["can_release"] for item in listed["locks"])
    assert all("设备托管" in item["release_block_reason"] for item in listed["locks"])
    with pytest.raises(WorkflowConflict):
        service.force_release_workflow_task_execution_lock(
            TASK_UUID,
            LEASE_A_UUID,
            expected_claim_uuid=CLAIM_UUID,
            expected_fencing_token=7,
            reason="不应跳过设备物理托管结算",
            physical_settlement_confirmed=True,
        )


def test_force_release_http_route_requires_strict_confirmation(
    store: WorkflowStore,
) -> None:
    """公开路由应严格接收 fencing token、原因和物理确认字段。"""

    _seed_lock_task(store)
    client = TestClient(create_workflow_app(WorkflowService(store)))
    listing = client.get(
        f"/api/v1/workflow-tasks/{TASK_UUID}/execution-locks"
    )
    assert listing.status_code == 200
    assert listing.json()["code"] == 0
    assert len(listing.json()["data"]["locks"]) == 2
    strict_rejection = client.post(
        f"/api/v1/workflow-tasks/{TASK_UUID}/execution-locks/"
        f"{LEASE_A_UUID}/force-release",
        json={
            "expected_claim_uuid": CLAIM_UUID,
            "expected_fencing_token": 7,
            "reason": "现场已确认安全",
            # StrictBool must not silently coerce an integer into confirmation.
            "physical_settlement_confirmed": 1,
        },
    )
    assert strict_rejection.status_code == 200
    assert strict_rejection.json()["code"] == 1000
    response = client.post(
        f"/api/v1/workflow-tasks/{TASK_UUID}/execution-locks/"
        f"{LEASE_A_UUID}/force-release",
        json={
            "expected_claim_uuid": CLAIM_UUID,
            "expected_fencing_token": 7,
            "reason": "现场已确认安全",
            "physical_settlement_confirmed": True,
        },
    )
    assert response.status_code == 200
    assert response.json()["code"] == 0
    assert response.json()["data"]["status"] == "released"
