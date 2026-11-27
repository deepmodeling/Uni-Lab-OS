"""并发及重复完成回调下的幂等派发合同。"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from tests.scheduler_core.conftest import CoreRuntime, stable_uuid
from unilabos.app.scheduler.dispatch import CommittedJobOutcome


def finish_concurrently(runtime: CoreRuntime, job_uuids: list[str]) -> None:
    """让给定完成回调在同一屏障后并发进入调度器。"""

    barrier = threading.Barrier(len(job_uuids))
    outcome = CommittedJobOutcome(
        outcome="succeeded",
        return_info={"return_value": {"ok": True}},
        error_info=[],
        unknown_command_ids=[],
    )

    def finish(job_uuid: str) -> dict[str, object]:
        barrier.wait()
        return runtime.scheduler.on_job_outcome(job_uuid, outcome)

    with ThreadPoolExecutor(max_workers=len(job_uuids)) as executor:
        futures = [executor.submit(finish, job_uuid) for job_uuid in job_uuids]
        for future in futures:
            future.result()


def test_concurrent_duplicate_callbacks_dispatch_each_successor_once(
    core_runtime: CoreRuntime,
) -> None:
    """两个 Task 的重复回调交叉到达时不重复派发或重复写终态。"""

    first = core_runtime.submit(
        task_name="concurrent-a",
        devices=["reactor-a", "robot-a"],
        edges=[(0, 1)],
    )
    second = core_runtime.submit(
        task_name="concurrent-b",
        devices=["reactor-b", "warehouse-a"],
        edges=[(0, 1)],
    )
    first_roots = [
        stable_uuid("job:concurrent-a:0"),
        stable_uuid("job:concurrent-b:0"),
    ]
    successors = [
        stable_uuid("job:concurrent-a:1"),
        stable_uuid("job:concurrent-b:1"),
    ]

    finish_concurrently(
        core_runtime,
        [first_roots[0], first_roots[1], first_roots[0], first_roots[1]],
    )

    dispatched = [item["job_id"] for item in core_runtime.dispatcher.dispatched]
    assert sorted(dispatched) == sorted([*first_roots, *successors])
    assert len(dispatched) == len(set(dispatched)) == 4

    finish_concurrently(
        core_runtime,
        [successors[0], successors[1], successors[0], successors[1]],
    )

    assert (
        core_runtime.workflow_store.get_task(first["task"]["uuid"])["status"]
        == "succeeded"
    )
    assert (
        core_runtime.workflow_store.get_task(second["task"]["uuid"])["status"]
        == "succeeded"
    )
    assert (
        core_runtime.inventory_store.query_all(
            "SELECT state FROM station_execution_claim ORDER BY job_uuid"
        )
        == [{"state": "released"}] * 4
    )
