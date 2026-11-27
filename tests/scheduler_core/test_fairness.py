"""多 Task 优先级、稳定排序与非抢占合同。"""

from __future__ import annotations

from tests.scheduler_core.conftest import CoreRuntime, ManualClock, stable_uuid
from unilabos.app.scheduler.models import ReadyTask, WorkflowNode
from unilabos.app.scheduler.ordering import OrderingContext, StableLocalOrderer


def test_high_priority_waiter_wins_next_slot_without_preemption(
    core_runtime: CoreRuntime,
) -> None:
    """高优先级只竞争下一次派发，不取消已经在途的低优先级 Job。"""

    blocker = core_runtime.submit(
        task_name="priority-blocker",
        devices=["reactor-a"],
        priority="normal",
    )
    low = core_runtime.submit(
        task_name="priority-low",
        devices=["reactor-a"],
        priority="normal",
    )
    high = core_runtime.submit(
        task_name="priority-high",
        devices=["reactor-a"],
        priority="high",
    )
    blocker_job = stable_uuid("job:priority-blocker:0")
    low_job = stable_uuid("job:priority-low:0")
    high_job = stable_uuid("job:priority-high:0")

    assert [item["job_id"] for item in core_runtime.dispatcher.dispatched] == [
        blocker_job
    ]
    assert core_runtime.dispatcher.cancel_requests == []

    core_runtime.scheduler.on_job_finished(blocker_job, True, {"done": True})
    assert [item["job_id"] for item in core_runtime.dispatcher.dispatched] == [
        blocker_job,
        high_job,
    ]

    core_runtime.scheduler.on_job_finished(high_job, True, {"done": True})
    core_runtime.scheduler.on_job_finished(low_job, True, {"done": True})
    for aggregate in (blocker, low, high):
        assert (
            core_runtime.workflow_store.get_task(aggregate["task"]["uuid"])["status"]
            == "succeeded"
        )


def test_waiting_age_changes_ready_order_without_real_time() -> None:
    """手动推进六个周期后，较低基础权重的老 Task 可先于新 Task。"""

    clock = ManualClock(initial=0.0)
    orderer = StableLocalOrderer(aging_interval_seconds=30, clock=clock.time)
    node = WorkflowNode(
        id="ready-node",
        device_id="reactor-a",
        action_name="run",
    )
    aged = ReadyTask(
        workflow_id="aged-task",
        node=node,
        priority_weight=50.0,
        submitted_at=clock.time(),
    )
    clock.advance(180)
    fresh = ReadyTask(
        workflow_id="fresh-task",
        node=node,
        priority_weight=55.0,
        submitted_at=clock.time(),
    )

    ordered = orderer.order([fresh, aged], OrderingContext(set()))

    assert [item.workflow_id for item in ordered] == ["aged-task", "fresh-task"]
