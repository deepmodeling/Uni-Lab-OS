"""持久容量门禁与 50×8 有界压力验收。"""

from __future__ import annotations

import time
from pathlib import Path

from tests.scheduler_core.conftest import (
    CoreRuntime,
    build_core_runtime,
    stable_uuid,
)


def test_global_capacity_holds_third_task_until_a_slot_is_released(
    tmp_path: Path,
) -> None:
    """全局活动 Task/在途 Job 均以持久事实限制为二。"""

    runtime = build_core_runtime(
        tmp_path / "capacity",
        max_in_flight_jobs=2,
        max_active_tasks=2,
        max_tasks_per_workflow=2,
    )
    try:
        aggregates = [
            runtime.submit(task_name=f"capacity-{index}", devices=[device])
            for index, device in enumerate(("reactor-a", "reactor-b", "robot-a"))
        ]
        jobs = [stable_uuid(f"job:capacity-{index}:0") for index in range(3)]

        assert [item["job_id"] for item in runtime.dispatcher.dispatched] == jobs[:2]
        assert [
            runtime.workflow_store.get_task(item["task"]["uuid"])["status"]
            for item in aggregates
        ] == ["running", "running", "pending"]

        runtime.scheduler.on_job_finished(jobs[0], True, {})

        assert [item["job_id"] for item in runtime.dispatcher.dispatched] == jobs
        assert (
            runtime.workflow_store.get_task(aggregates[2]["task"]["uuid"])["status"]
            == "running"
        )
        runtime.scheduler.on_job_finished(jobs[1], True, {})
        runtime.scheduler.on_job_finished(jobs[2], True, {})
    finally:
        runtime.close()


def test_fifty_tasks_with_eight_jobs_finish_without_duplicate_dispatch(
    core_runtime: CoreRuntime,
) -> None:
    """400 个冻结 Job 在四台虚拟设备上交叉推进且完整持久化。"""

    started = time.monotonic()
    devices = ["reactor-a", "reactor-b", "robot-a", "warehouse-a"]
    task_uuids: list[str] = []
    for task_index in range(50):
        task_devices = [
            devices[(task_index + job_index) % len(devices)] for job_index in range(8)
        ]
        aggregate = core_runtime.submit(
            task_name=f"pressure-{task_index:02d}",
            devices=task_devices,
            edges=[(index, index + 1) for index in range(7)],
        )
        task_uuids.append(aggregate["task"]["uuid"])

    cursor = 0
    max_in_flight = 0
    while cursor < len(core_runtime.dispatcher.dispatched):
        snapshot = core_runtime.scheduler.snapshot()
        max_in_flight = max(max_in_flight, len(snapshot["inflight_jobs"]))
        job_uuid = core_runtime.dispatcher.dispatched[cursor]["job_id"]
        core_runtime.scheduler.on_job_finished(job_uuid, True, {"ok": True})
        cursor += 1

    dispatched = [item["job_id"] for item in core_runtime.dispatcher.dispatched]
    assert len(dispatched) == len(set(dispatched)) == 400
    assert max_in_flight <= len(devices)
    assert core_runtime.workflow_store.count_rows("workflow_task") == 50
    assert core_runtime.workflow_store.count_rows("workflow_node_job") == 400
    assert all(
        core_runtime.workflow_store.get_task(task_uuid)["status"] == "succeeded"
        for task_uuid in task_uuids
    )
    assert core_runtime.inventory_store.query_one(
        "SELECT COUNT(*) AS amount FROM station_execution_claim WHERE state='released'"
    ) == {"amount": 400}
    assert time.monotonic() - started < 120.0
