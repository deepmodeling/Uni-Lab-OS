"""单 Runtime 生命周期内的多 Task 交叉调度合同。"""

from __future__ import annotations

from tests.scheduler_core.conftest import CoreRuntime, stable_uuid


def test_two_durable_tasks_interleave_without_cross_device_blocking(
    core_runtime: CoreRuntime,
) -> None:
    """两个交叉使用设备的 Task 应并行起步并在资源释放后共同补位。"""

    first = core_runtime.submit(
        task_name="cross-a",
        devices=["reactor-a", "reactor-b"],
        edges=[(0, 1)],
    )
    second = core_runtime.submit(
        task_name="cross-b",
        devices=["reactor-b", "reactor-a"],
        edges=[(0, 1)],
    )
    first_jobs = [stable_uuid(f"job:cross-a:{index}") for index in range(2)]
    second_jobs = [stable_uuid(f"job:cross-b:{index}") for index in range(2)]

    assert [item["job_id"] for item in core_runtime.dispatcher.dispatched] == [
        first_jobs[0],
        second_jobs[0],
    ]

    core_runtime.scheduler.on_job_finished(first_jobs[0], True, {"step": "a0"})
    assert [item["job_id"] for item in core_runtime.dispatcher.dispatched] == [
        first_jobs[0],
        second_jobs[0],
    ]

    core_runtime.scheduler.on_job_finished(second_jobs[0], True, {"step": "b0"})
    assert [item["job_id"] for item in core_runtime.dispatcher.dispatched] == [
        first_jobs[0],
        second_jobs[0],
        first_jobs[1],
        second_jobs[1],
    ]

    core_runtime.scheduler.on_job_finished(first_jobs[1], True, {"step": "a1"})
    core_runtime.scheduler.on_job_finished(second_jobs[1], True, {"step": "b1"})

    first_task_uuid = first["task"]["uuid"]
    second_task_uuid = second["task"]["uuid"]
    assert (
        core_runtime.workflow_store.get_task(first_task_uuid)["status"] == "succeeded"
    )
    assert (
        core_runtime.workflow_store.get_task(second_task_uuid)["status"] == "succeeded"
    )
    assert {
        job["status"]
        for task_uuid in (first_task_uuid, second_task_uuid)
        for job in core_runtime.workflow_store.list_jobs(task_uuid)
    } == {"succeeded"}
    claims = core_runtime.inventory_store.query_all(
        "SELECT state FROM station_execution_claim ORDER BY job_uuid"
    )
    assert claims == [{"state": "released"}] * 4
