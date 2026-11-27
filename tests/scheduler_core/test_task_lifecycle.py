"""调度核心 Task 生命周期控制合同。"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from tests.scheduler_core.conftest import CoreRuntime, stable_uuid


def test_trace_id_only_is_silent_when_signoz_is_disabled(
    core_runtime: CoreRuntime,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """无远端 Trace carrier 时跳过持久化，且不为每个 Task 打异常日志。"""

    original_submit = core_runtime.scheduler.submit_workflow

    def submit_with_local_trace(spec: Any) -> dict[str, Any]:
        result = original_submit(spec)
        return {
            **result,
            "trace_context": {
                "trace_id": "0123456789abcdef0123456789abcdef",
                "span_id": "",
            },
        }

    monkeypatch.setattr(
        core_runtime.scheduler,
        "submit_workflow",
        submit_with_local_trace,
    )
    caplog.set_level(
        logging.ERROR,
        logger="unilabos.workflow.task_scheduler_bridge",
    )

    task = core_runtime.persist(task_name="no-signoz", devices=["reactor-a"])
    submitted = core_runtime.bridge.submit(task)

    assert submitted["task"]["trace_context"] == {}
    assert core_runtime.workflow_store.get_task(task["uuid"])["trace_context"] == {}
    assert not any(
        "Trace Context 持久化失败" in record.getMessage() for record in caplog.records
    )


def test_step_mode_dispatches_exactly_one_node_per_command(
    core_runtime: CoreRuntime,
) -> None:
    """单步任务必须由每次 step 命令各放行一个 DAG 节点。"""

    task = core_runtime.persist(
        task_name="step-task",
        devices=["reactor-a", "reactor-b"],
        edges=[(0, 1)],
        run_mode="step",
    )
    first_job = stable_uuid("job:step-task:0")
    second_job = stable_uuid("job:step-task:1")

    submitted = core_runtime.bridge.submit(task)
    assert submitted["task"]["status"] == "pending"
    assert core_runtime.dispatcher.dispatched == []

    core_runtime.bridge.step(task["uuid"])
    assert [item["job_id"] for item in core_runtime.dispatcher.dispatched] == [
        first_job
    ]
    core_runtime.scheduler.on_job_finished(first_job, True, {"step": 1})
    assert [item["job_id"] for item in core_runtime.dispatcher.dispatched] == [
        first_job
    ]

    core_runtime.bridge.step(task["uuid"])
    assert [item["job_id"] for item in core_runtime.dispatcher.dispatched] == [
        first_job,
        second_job,
    ]
    core_runtime.scheduler.on_job_finished(second_job, True, {"step": 2})
    assert core_runtime.workflow_store.get_task(task["uuid"])["status"] == "succeeded"


def test_pause_blocks_successor_until_resume(core_runtime: CoreRuntime) -> None:
    """暂停只阻止后继派发，当前在途 Job 仍可正常结算。"""

    task = core_runtime.persist(
        task_name="pause-task",
        devices=["reactor-a", "reactor-b"],
        edges=[(0, 1)],
    )
    first_job = stable_uuid("job:pause-task:0")
    second_job = stable_uuid("job:pause-task:1")
    core_runtime.bridge.submit(task)

    paused = core_runtime.bridge.pause(task["uuid"])
    assert paused["task"]["control_status"] == "paused"
    core_runtime.scheduler.on_job_finished(first_job, True, {"paused": True})
    assert [item["job_id"] for item in core_runtime.dispatcher.dispatched] == [
        first_job
    ]

    core_runtime.bridge.resume(task["uuid"])
    assert [item["job_id"] for item in core_runtime.dispatcher.dispatched] == [
        first_job,
        second_job,
    ]
    core_runtime.scheduler.on_job_finished(second_job, True, {"resumed": True})
    assert core_runtime.workflow_store.get_task(task["uuid"])["status"] == "succeeded"


def test_cancel_waiting_task_never_crosses_dispatch_boundary(
    core_runtime: CoreRuntime,
) -> None:
    """资源等待中的 Task 被取消后，其未派发 Job 永远不进入执行器。"""

    blocker = core_runtime.submit(task_name="cancel-blocker", devices=["reactor-a"])
    waiting_task = core_runtime.persist(
        task_name="cancel-waiting",
        devices=["reactor-a"],
    )
    core_runtime.bridge.submit(waiting_task)
    blocker_job = stable_uuid("job:cancel-blocker:0")
    waiting_job = stable_uuid("job:cancel-waiting:0")
    assert [item["job_id"] for item in core_runtime.dispatcher.dispatched] == [
        blocker_job
    ]

    canceled = core_runtime.bridge.cancel(
        waiting_task["uuid"],
        command_uuid=stable_uuid("command:cancel-waiting"),
    )
    assert canceled["task"]["status"] == "canceled"
    assert core_runtime.workflow_store.get_job(waiting_job)["status"] == "canceled"

    core_runtime.scheduler.on_job_finished(blocker_job, True, {"done": True})
    assert [item["job_id"] for item in core_runtime.dispatcher.dispatched] == [
        blocker_job
    ]
    assert (
        core_runtime.workflow_store.get_task(blocker["task"]["uuid"])["status"]
        == "succeeded"
    )


def test_drain_settles_inflight_work_but_holds_new_dispatch(
    core_runtime: CoreRuntime,
) -> None:
    """Drain 期间已有 Job 可结算，新 Task 只在恢复后派发。"""

    active = core_runtime.submit(task_name="drain-active", devices=["reactor-a"])
    active_job = stable_uuid("job:drain-active:0")
    assert core_runtime.scheduler.begin_drain()["phase"] == "draining"

    waiting = core_runtime.submit(task_name="drain-waiting", devices=["reactor-b"])
    waiting_job = stable_uuid("job:drain-waiting:0")
    assert [item["job_id"] for item in core_runtime.dispatcher.dispatched] == [
        active_job
    ]

    core_runtime.scheduler.on_job_finished(active_job, True, {"done": True})
    assert core_runtime.scheduler.drain_status()["phase"] == "drained"
    assert (
        core_runtime.workflow_store.get_task(active["task"]["uuid"])["status"]
        == "succeeded"
    )
    assert (
        core_runtime.workflow_store.get_task(waiting["task"]["uuid"])["status"]
        == "pending"
    )

    resumed = core_runtime.scheduler.resume_from_drain()
    assert [item["job_id"] for item in resumed["dispatched"]] == [waiting_job]
    core_runtime.scheduler.on_job_finished(waiting_job, True, {"done": True})
    assert (
        core_runtime.workflow_store.get_task(waiting["task"]["uuid"])["status"]
        == "succeeded"
    )
