"""主物料驱动的装载期间设备托管（Device Tenancy）合同测试。"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

from unilabos.workflow.store import StoreConflict, WorkflowStore
from unilabos.workflow.task_runtime_projection import TaskRuntimeProjection

WORKFLOW_UUID = "81000000-0000-4000-8000-000000000001"
TASK_A_UUID = "82000000-0000-4000-8000-000000000001"
TASK_B_UUID = "82000000-0000-4000-8000-000000000002"
MATERIAL_A_UUID = "83000000-0000-4000-8000-000000000001"
MATERIAL_B_UUID = "83000000-0000-4000-8000-000000000002"
DEVICE_A_UUID = "84000000-0000-4000-8000-000000000001"
DEVICE_B_UUID = "84000000-0000-4000-8000-000000000002"
TASK_A_JOBS = (
    "85000000-0000-4000-8000-000000000001",
    "85000000-0000-4000-8000-000000000002",
    "85000000-0000-4000-8000-000000000003",
)
TASK_B_JOB = "85000000-0000-4000-8000-000000000004"
TASK_B_SECOND_JOB = "85000000-0000-4000-8000-000000000005"
_CREATED_AT = "2026-08-30T00:00:00Z"


@pytest.fixture()
def store(tmp_path: Path) -> Iterator[WorkflowStore]:
    """创建隔离的本地工作流/调度存储（Local Workflow/Scheduler Store）。

    参数：``tmp_path`` 是 pytest 提供的临时目录。产生：本测试唯一可写的工作流
    任务权威（WorkflowTask Authority）；用例结束时关闭连接。异常：建库错误
    原样传播。
    """

    opened = WorkflowStore(tmp_path / "workflow_history.db")
    try:
        yield opened
    finally:
        opened.close()


def _seed_task(
    store: WorkflowStore,
    *,
    task_uuid: str,
    job_uuids: Sequence[str],
) -> None:
    """写入一个任务及其拓扑有序的待处理工作流节点作业。

    参数：``store`` 是唯一写权威，``task_uuid`` 是工作流任务稳定身份，
    ``job_uuids`` 是按装载、转运、卸载顺序排列的作业稳定身份。返回：无。
    异常：重复身份或数据库约束错误原样传播。
    """

    plan = {
        "version": 1,
        "nodes": [
            {"uuid": job_uuid, "topological_index": index}
            for index, job_uuid in enumerate(job_uuids)
        ],
        "edges": [],
        "handles": [],
    }
    with store.transaction() as connection:
        connection.execute(
            """
            INSERT INTO workflow_task(
                uuid, create_time, update_time, deleted_at, description,
                meta_data, workflow_uuid, status, workflow_snapshot,
                execution_plan, run_mode, target_node_uuid, control_status,
                cleanup_status, trace_context, input, output, error_info
            ) VALUES (?, ?, ?, NULL, NULL, '{}', ?, 'pending', '{}', ?,
                      'normal', NULL, 'active', 'none', '{}', '{}', '{}', '[]')
            """,
            (
                task_uuid,
                _CREATED_AT,
                _CREATED_AT,
                WORKFLOW_UUID,
                json.dumps(plan),
            ),
        )
        for topological_index, job_uuid in enumerate(job_uuids):
            # ``node_uuid`` 只标识冻结计划中的逻辑节点，不替代作业尝试身份。
            node_uuid = f"86000000-0000-4000-8000-{int(job_uuid[-12:]):012d}"
            connection.execute(
                """
                INSERT INTO workflow_node_job(
                    uuid, create_time, update_time, deleted_at, description,
                    meta_data, workflow_task_uuid, workflow_node_uuid,
                    feedback_sequence, topological_index, executor_kind,
                    execution_policy, execution_timeout_seconds, status,
                    attempt, param, feedback_data, return_info, control_data,
                    error_info
                ) VALUES (?, ?, ?, NULL, NULL, '{}', ?, ?, 0, ?,
                          'device_action', '{}', 0, 'pending', 1, '{}', '{}',
                          '{}', '{}', '[]')
                """,
                (
                    job_uuid,
                    _CREATED_AT,
                    _CREATED_AT,
                    task_uuid,
                    node_uuid,
                    topological_index,
                ),
            )


def _transition(
    *,
    acquire_device_uuid: str = "",
    release_device_uuid: str = "",
    material_uuid: str = MATERIAL_A_UUID,
) -> dict[str, str]:
    """构造一个主物料装载期间设备托管转换。

    参数：两个可选设备 UUID 分别表示装载前取得与成功卸载后释放的设备。
    返回：可交给调度门禁的规范转换对象。异常：无；至少一个设备由调用测试保证。
    """

    return {
        "mode": "task_while_loaded",
        "material_uuid": material_uuid,
        "acquire_device_lock_key": (
            f"/devices/{acquire_device_uuid}" if acquire_device_uuid else ""
        ),
        "release_device_lock_key": (
            f"/devices/{release_device_uuid}" if release_device_uuid else ""
        ),
    }


def _device_lock(device_uuid: str) -> list[dict[str, str]]:
    """把设备物料身份转换为一个完整设备执行占用请求。

    参数：``device_uuid`` 是库存权威中的设备物料 UUID。返回：单成员设备占用
    请求数组。异常：无；UUID 合法性由测试常量保证。
    """

    return [{"lock_key": f"/devices/{device_uuid}", "scope": "device"}]


def test_device_tenancy_blocks_other_task_until_successful_unload(
    store: WorkflowStore,
) -> None:
    """装载成功后设备应持续归原任务，直到最后卸载明确成功。

    参数：``store`` 是隔离工作流任务权威。返回：无。断言任务 A 装载作业释放
    短期作业执行占用（JobExecutionClaim）后，任务 B 仍因长期设备托管保持
    ``pending``；任务 A 的卸载物理结算完成后，任务 B 才能取得设备占用。
    """

    store.create_workflow(
        workflow_uuid=WORKFLOW_UUID,
        name="设备托管测试",
        tags=[],
        description=None,
        meta_data={},
    )
    _seed_task(store, task_uuid=TASK_A_UUID, job_uuids=TASK_A_JOBS[:2])
    _seed_task(store, task_uuid=TASK_B_UUID, job_uuids=(TASK_B_JOB,))
    projection = TaskRuntimeProjection(store)

    projection.project_pre_dispatch(
        task_uuid=TASK_A_UUID,
        job_uuid=TASK_A_JOBS[0],
        execution_locks=_device_lock(DEVICE_A_UUID),
        device_tenancy=_transition(acquire_device_uuid=DEVICE_A_UUID),
    )
    projection.project_job_finished(
        job_uuid=TASK_A_JOBS[0],
        scheduler_state="success",
        return_info={"loaded": True},
    )

    projection.project_pre_dispatch(
        task_uuid=TASK_B_UUID,
        job_uuid=TASK_B_JOB,
        execution_locks=_device_lock(DEVICE_A_UUID),
    )
    assert store.get_job(TASK_B_JOB)["status"] == "pending"
    assert store.get_task(TASK_B_UUID)["status"] == "pending"
    active = projection.list_device_tenancies(task_uuid=TASK_A_UUID)
    assert [item["device_lock_key"] for item in active] == [f"/devices/{DEVICE_A_UUID}"]

    projection.project_pre_dispatch(
        task_uuid=TASK_A_UUID,
        job_uuid=TASK_A_JOBS[1],
        execution_locks=_device_lock(DEVICE_A_UUID),
        device_tenancy=_transition(release_device_uuid=DEVICE_A_UUID),
    )
    projection.project_job_finished(
        job_uuid=TASK_A_JOBS[1],
        scheduler_state="success",
        return_info={"unloaded": True},
    )
    assert projection.list_device_tenancies(task_uuid=TASK_A_UUID) == []

    projection.project_pre_dispatch(
        task_uuid=TASK_B_UUID,
        job_uuid=TASK_B_JOB,
        execution_locks=_device_lock(DEVICE_A_UUID),
    )
    assert store.get_job(TASK_B_JOB)["status"] == "dispatched"


def test_operate_in_place_requires_matching_active_task_tenancy(
    store: WorkflowStore,
) -> None:
    """原位操作除库存位置外还必须证明同一 Task 正托管该设备与物料。"""

    store.create_workflow(
        workflow_uuid=WORKFLOW_UUID,
        name="原位操作托管测试",
        tags=[],
        description=None,
        meta_data={},
    )
    _seed_task(store, task_uuid=TASK_A_UUID, job_uuids=TASK_A_JOBS[:2])
    projection = TaskRuntimeProjection(store)
    projection.project_pre_dispatch(
        task_uuid=TASK_A_UUID,
        job_uuid=TASK_A_JOBS[0],
        execution_locks=_device_lock(DEVICE_A_UUID),
        device_tenancy=_transition(acquire_device_uuid=DEVICE_A_UUID),
    )
    projection.project_job_finished(
        job_uuid=TASK_A_JOBS[0], scheduler_state="success", return_info={}
    )

    projection.project_pre_dispatch(
        task_uuid=TASK_A_UUID,
        job_uuid=TASK_A_JOBS[1],
        execution_locks=_device_lock(DEVICE_A_UUID),
        required_device_tenancy={
            "material_uuid": MATERIAL_A_UUID,
            "device_lock_key": f"/devices/{DEVICE_A_UUID}",
        },
    )
    assert store.get_job(TASK_A_JOBS[1])["status"] == "dispatched"


def test_operate_in_place_rejects_missing_or_foreign_task_tenancy(
    store: WorkflowStore,
) -> None:
    """库存中的同位事实不能替代工作流 Task 的长期设备托管权。"""

    store.create_workflow(
        workflow_uuid=WORKFLOW_UUID,
        name="原位操作拒绝测试",
        tags=[],
        description=None,
        meta_data={},
    )
    _seed_task(store, task_uuid=TASK_A_UUID, job_uuids=(TASK_A_JOBS[0],))
    projection = TaskRuntimeProjection(store)

    with pytest.raises(Exception, match="托管"):
        projection.project_pre_dispatch(
            task_uuid=TASK_A_UUID,
            job_uuid=TASK_A_JOBS[0],
            execution_locks=_device_lock(DEVICE_A_UUID),
            required_device_tenancy={
                "material_uuid": MATERIAL_A_UUID,
                "device_lock_key": f"/devices/{DEVICE_A_UUID}",
            },
        )

    assert store.get_job(TASK_A_JOBS[0])["status"] == "pending"


def test_runtime_rejects_h_to_n_device_tenancy_wait_cycle(
    store: WorkflowStore,
) -> None:
    """两个 Task 各持一台设备并交叉等待时，第二条等待边必须被拒绝。"""

    store.create_workflow(
        workflow_uuid=WORKFLOW_UUID,
        name="运行时托管环路测试",
        tags=[],
        description=None,
        meta_data={},
    )
    _seed_task(store, task_uuid=TASK_A_UUID, job_uuids=TASK_A_JOBS[:2])
    _seed_task(
        store,
        task_uuid=TASK_B_UUID,
        job_uuids=(TASK_B_JOB, TASK_B_SECOND_JOB),
    )
    projection = TaskRuntimeProjection(store)
    projection.project_pre_dispatch(
        task_uuid=TASK_A_UUID,
        job_uuid=TASK_A_JOBS[0],
        execution_locks=_device_lock(DEVICE_A_UUID),
        device_tenancy=_transition(acquire_device_uuid=DEVICE_A_UUID),
    )
    projection.project_job_finished(
        job_uuid=TASK_A_JOBS[0], scheduler_state="success", return_info={}
    )
    projection.project_pre_dispatch(
        task_uuid=TASK_B_UUID,
        job_uuid=TASK_B_JOB,
        execution_locks=_device_lock(DEVICE_B_UUID),
        device_tenancy=_transition(
            acquire_device_uuid=DEVICE_B_UUID, material_uuid=MATERIAL_B_UUID
        ),
    )
    projection.project_job_finished(
        job_uuid=TASK_B_JOB, scheduler_state="success", return_info={}
    )

    blocked = projection.project_pre_dispatch(
        task_uuid=TASK_A_UUID,
        job_uuid=TASK_A_JOBS[1],
        execution_locks=[*_device_lock(DEVICE_A_UUID), *_device_lock(DEVICE_B_UUID)],
        device_tenancy=_transition(
            acquire_device_uuid=DEVICE_B_UUID,
            release_device_uuid=DEVICE_A_UUID,
        ),
    )
    assert next(job for job in blocked["jobs"] if job["uuid"] == TASK_A_JOBS[1])[
        "status"
    ] == "pending"

    with pytest.raises(Exception, match="环路"):
        projection.project_pre_dispatch(
            task_uuid=TASK_B_UUID,
            job_uuid=TASK_B_SECOND_JOB,
            execution_locks=[
                *_device_lock(DEVICE_A_UUID),
                *_device_lock(DEVICE_B_UUID),
            ],
            device_tenancy=_transition(
                acquire_device_uuid=DEVICE_A_UUID,
            release_device_uuid=DEVICE_B_UUID,
            material_uuid=MATERIAL_B_UUID,
        ),
        )


def test_transfer_holds_source_and_target_until_settlement(
    store: WorkflowStore,
) -> None:
    """设备到设备转运应先同时托管两端，再在成功结算后释放来源端。

    参数：``store`` 是隔离工作流任务权威。返回：无。断言转运作业派发后同一主
    物料同时绑定来源与目标设备，明确成功后只保留目标设备，最终卸载后全部释放。
    """

    store.create_workflow(
        workflow_uuid=WORKFLOW_UUID,
        name="跨设备托管测试",
        tags=[],
        description=None,
        meta_data={},
    )
    _seed_task(store, task_uuid=TASK_A_UUID, job_uuids=TASK_A_JOBS)
    projection = TaskRuntimeProjection(store)

    projection.project_pre_dispatch(
        task_uuid=TASK_A_UUID,
        job_uuid=TASK_A_JOBS[0],
        execution_locks=_device_lock(DEVICE_A_UUID),
        device_tenancy=_transition(acquire_device_uuid=DEVICE_A_UUID),
    )
    projection.project_job_finished(
        job_uuid=TASK_A_JOBS[0], scheduler_state="success", return_info={}
    )

    projection.project_pre_dispatch(
        task_uuid=TASK_A_UUID,
        job_uuid=TASK_A_JOBS[1],
        execution_locks=[
            *_device_lock(DEVICE_A_UUID),
            *_device_lock(DEVICE_B_UUID),
        ],
        device_tenancy=_transition(
            acquire_device_uuid=DEVICE_B_UUID,
            release_device_uuid=DEVICE_A_UUID,
        ),
    )
    assert {
        item["device_lock_key"]
        for item in projection.list_device_tenancies(task_uuid=TASK_A_UUID)
    } == {f"/devices/{DEVICE_A_UUID}", f"/devices/{DEVICE_B_UUID}"}

    projection.project_job_finished(
        job_uuid=TASK_A_JOBS[1], scheduler_state="success", return_info={}
    )
    assert [
        item["device_lock_key"]
        for item in projection.list_device_tenancies(task_uuid=TASK_A_UUID)
    ] == [f"/devices/{DEVICE_B_UUID}"]

    projection.project_pre_dispatch(
        task_uuid=TASK_A_UUID,
        job_uuid=TASK_A_JOBS[2],
        execution_locks=_device_lock(DEVICE_B_UUID),
        device_tenancy=_transition(release_device_uuid=DEVICE_B_UUID),
    )
    projection.project_job_finished(
        job_uuid=TASK_A_JOBS[2], scheduler_state="success", return_info={}
    )
    assert projection.list_device_tenancies(task_uuid=TASK_A_UUID) == []
    assert store.get_task(TASK_A_UUID)["status"] == "succeeded"


def test_failed_load_retains_tenancy_for_manual_physical_settlement(
    store: WorkflowStore,
) -> None:
    """装载结果失败但无法证明未开始时必须保留托管并转人工清理。

    参数：``store`` 是隔离工作流任务权威。返回：无。断言业务作业和父任务
    ``failed`` 不会释放设备托管，父任务清理状态进入 ``requires_attention``。
    """

    store.create_workflow(
        workflow_uuid=WORKFLOW_UUID,
        name="失败托管测试",
        tags=[],
        description=None,
        meta_data={},
    )
    _seed_task(store, task_uuid=TASK_A_UUID, job_uuids=(TASK_A_JOBS[0],))
    projection = TaskRuntimeProjection(store)

    projection.project_pre_dispatch(
        task_uuid=TASK_A_UUID,
        job_uuid=TASK_A_JOBS[0],
        execution_locks=_device_lock(DEVICE_A_UUID),
        device_tenancy=_transition(acquire_device_uuid=DEVICE_A_UUID),
    )
    projection.project_job_finished(
        job_uuid=TASK_A_JOBS[0],
        scheduler_state="failed",
        error_info=[{"code": "device_error"}],
    )

    task = store.get_task(TASK_A_UUID)
    assert store.get_job(TASK_A_JOBS[0])["status"] == "failed"
    assert task["status"] == "failed"
    assert task["cleanup_status"] == "requires_attention"
    assert projection.list_device_tenancies(task_uuid=TASK_A_UUID)

    with pytest.raises(StoreConflict, match="仍有活动设备托管"):
        projection.project_cleanup_settled(TASK_A_UUID)

    assert store.get_task(TASK_A_UUID)["cleanup_status"] == "requires_attention"
    assert projection.list_device_tenancies(task_uuid=TASK_A_UUID)


def test_late_unload_does_not_release_new_tenancy_after_manual_disposition(store):
    """人工释放后重新取得设备，旧卸载成功只能结算它原本关联的托管行。"""
    from unilabos.workflow.device_tenancy import prepare_device_tenancy, settle_device_tenancy
    from unilabos.workflow.error_control import StationExecutionControl
    store.create_workflow(workflow_uuid=WORKFLOW_UUID, name="人工释放托管", tags=[], description=None, meta_data={})
    _seed_task(store, task_uuid=TASK_A_UUID, job_uuids=TASK_A_JOBS)
    with store.transaction() as connection:
        prepare_device_tenancy(connection, task_uuid=TASK_A_UUID, job_uuid=TASK_A_JOBS[0], transition=_transition(acquire_device_uuid=DEVICE_A_UUID))
        prepare_device_tenancy(connection, task_uuid=TASK_A_UUID, job_uuid=TASK_A_JOBS[1], transition=_transition(release_device_uuid=DEVICE_A_UUID))
    control = StationExecutionControl(store)
    decision = control.report_error(TASK_A_JOBS[1], phase="execution_unknown", message="卸载结果未知")
    before = control.occupancies()[0]
    command = control.accept_auxiliary("resource:release", {
        "expected_station_version": control.snapshot()["station_version"], "decision_id": decision["decision_id"],
        "occupancy_ids": [before["uuid"]], "expected_versions": {before["uuid"]: before["version"]},
        "operation": "release", "reason": "人工确认已取走物料",
    }, idempotency_key="release", operator_id="tester")
    control.apply_resource_facts(command)
    with store.transaction() as connection:
        prepare_device_tenancy(connection, task_uuid=TASK_A_UUID, job_uuid=TASK_A_JOBS[2], transition=_transition(acquire_device_uuid=DEVICE_A_UUID))
        settle_device_tenancy(connection, job_uuid=TASK_A_JOBS[1], succeeded=True)
    active = TaskRuntimeProjection(store).list_device_tenancies(task_uuid=TASK_A_UUID)
    assert len(active) == 1
    assert active[0]["uuid"] != before["uuid"]
    assert active[0]["acquired_by_job_uuid"] == TASK_A_JOBS[2]
