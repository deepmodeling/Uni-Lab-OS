"""持久等待环的诊断与关闭式派发冻结。"""

from __future__ import annotations

from tests.scheduler_core.conftest import CoreRuntime, stable_uuid
from unilabos.workflow.task_runtime_projection import TaskRuntimeProjection


def test_detected_wait_cycle_blocks_new_dispatch_without_choosing_a_victim(
    core_runtime: CoreRuntime,
) -> None:
    """整站等待图成环后停止新增派发，同时保留两个原 Task。"""

    first = core_runtime.persist(task_name="deadlock-a", devices=["reactor-a"])
    second = core_runtime.persist(task_name="deadlock-b", devices=["reactor-b"])
    first_task = first["uuid"]
    second_task = second["uuid"]
    first_job = stable_uuid("job:deadlock-a:0")
    second_job = stable_uuid("job:deadlock-b:0")
    projection = TaskRuntimeProjection(core_runtime.workflow_store)
    projection.project_execution_lock_wait(
        task_uuid=first_task,
        job_uuid=first_job,
        execution_locks=[{"lock_key": "/devices/reactor-a", "scope": "device"}],
        blocking_task_uuid=second_task,
        blocking_job_uuid=second_job,
    )
    projection.project_execution_lock_wait(
        task_uuid=second_task,
        job_uuid=second_job,
        execution_locks=[{"lock_key": "/devices/reactor-b", "scope": "device"}],
        blocking_task_uuid=first_task,
        blocking_job_uuid=first_job,
    )

    graph = projection.get_execution_wait_graph()
    assert graph["deadlock_detected"] is True
    assert graph["cycles"] == [[first_job, second_job]]

    third = core_runtime.submit(task_name="deadlock-new", devices=["robot-a"])
    third_job = stable_uuid("job:deadlock-new:0")

    assert core_runtime.dispatcher.dispatched == []
    assert third["task"]["status"] == "running"
    assert core_runtime.workflow_store.get_job(third_job)["wait_reason"]["code"] == (
        "execution_deadlock_detected"
    )
    assert core_runtime.workflow_store.get_task(first_task)["status"] == "running"
    assert core_runtime.workflow_store.get_task(second_task)["status"] == "running"
    assert all(
        core_runtime.workflow_store.get_job(job_uuid)["status"] == "pending"
        for job_uuid in (first_job, second_job, third_job)
    )
