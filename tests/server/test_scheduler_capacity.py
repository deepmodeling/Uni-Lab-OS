"""调度容量（V1 ``workflow/scheduler_capacity.py``）：全局在途作业 / 全局运行任务 / 每工作流运行任务上限。

超出的任务保持 ``pending``、作业保持排队，按先后放行，不失败。默认值与 V1 相同，可经 BasicConfig 配置。
"""
from __future__ import annotations

import pytest

from tests.server.v1_lifecycle_support import Stack, wait_for
from unilabos.config.config import BasicConfig
from unilabos.server.backend.scheduler.service import BackendScheduler


def _stack(**limits) -> Stack:
    stack = Stack(with_scheduler=False)
    stack.scheduler = BackendScheduler(stack.service, stack.executor, **limits)
    stack.service.set_task_submitter(stack.scheduler.submit)
    return stack


def _status(stack, task) -> str:
    return stack.task(task)["status"]


def _dispatched_tasks(stack) -> list[str]:
    return [payload["task_id"] for payload in stack.executor.dispatched]


def test_defaults_match_v1_and_come_from_basic_config(monkeypatch) -> None:
    assert (BasicConfig.scheduler_max_in_flight_jobs, BasicConfig.scheduler_max_active_tasks,
            BasicConfig.scheduler_max_tasks_per_workflow) == (100, 500, 100)
    monkeypatch.setattr(BasicConfig, "scheduler_max_active_tasks", 7)
    stack = _stack()
    try:
        assert stack.scheduler._max_active_tasks == 7  # noqa: SLF001
        assert stack.scheduler._max_in_flight_jobs == 100 and stack.scheduler._max_tasks_per_workflow == 100  # noqa: SLF001
    finally:
        stack.close()
    with pytest.raises(ValueError, match="正整数"):
        BackendScheduler(stack.service, stack.executor, max_active_tasks=0)


def test_tasks_beyond_the_global_limit_stay_pending_and_start_in_order() -> None:
    stack = _stack(max_active_tasks=1)
    try:
        first = stack.create_task(["device-a"])
        wait_for(lambda: len(stack.executor.dispatched) == 1)
        second = stack.create_task(["device-b"])
        third = stack.create_task(["device-c"])
        wait_for(lambda: len(stack.scheduler._capacity_waiting_tasks) == 2)  # noqa: SLF001
        assert (_status(stack, second), _status(stack, third)) == ("pending", "pending")
        assert stack.task(second)["error_info"] == [] and "started_at" not in stack.task(second)
        assert _dispatched_tasks(stack) == [first["uuid"]]

        stack.executor.finish(0)
        wait_for(lambda: len(stack.executor.dispatched) == 2)
        assert _dispatched_tasks(stack)[1] == second["uuid"]  # 先提交的先走
        assert _status(stack, third) == "pending"
        stack.executor.finish(1)
        wait_for(lambda: len(stack.executor.dispatched) == 3)
        assert _dispatched_tasks(stack)[2] == third["uuid"]
        stack.executor.finish(2)
        wait_for(lambda: _status(stack, third) == "succeeded")
    finally:
        stack.close()


def test_per_workflow_limit_queues_only_that_workflow() -> None:
    stack = _stack(max_tasks_per_workflow=1)
    try:
        workflow = stack.create_workflow(["device-a"])

        def run(workflow_uuid):
            response = stack.client.post("/api/v1/workflow-tasks", json={"workflow_uuid": workflow_uuid})
            assert response.json()["code"] == 0, response.text
            return response.json()["data"]

        first = run(workflow["uuid"])
        wait_for(lambda: len(stack.executor.dispatched) == 1)
        second = run(workflow["uuid"])
        other = stack.create_task(["device-b"])  # 另一工作流不受影响
        wait_for(lambda: len(stack.executor.dispatched) == 2)
        assert _dispatched_tasks(stack) == [first["uuid"], other["uuid"]]
        assert _status(stack, second) == "pending"
        stack.executor.finish(0)
        wait_for(lambda: len(stack.executor.dispatched) == 3)
        assert _dispatched_tasks(stack)[2] == second["uuid"]
    finally:
        stack.close()


def test_in_flight_job_limit_queues_ready_jobs_in_order() -> None:
    stack = _stack(max_in_flight_jobs=1)
    try:
        task = stack.create_task(["device-a", "device-b", "device-c"], parallel=True)

        def waiting():
            return [job for job in stack.service.list_workflow_node_jobs(task["uuid"])
                    if (job["control_data"].get("wait_reason") or {}).get("code") == "job_dispatch_capacity"]

        wait_for(lambda: len(stack.executor.dispatched) == 1 and len(waiting()) == 2)
        assert all(job["status"] == "pending" for job in waiting())
        assert _status(stack, task) == "running"
        since = stack.scheduler._waiting_interval_since  # noqa: SLF001 - 开始排队的先后
        queued = sorted((job["uuid"] for job in waiting()), key=lambda uuid: since[uuid])
        for index in range(2):
            stack.executor.finish(index)
            wait_for(lambda: len(stack.executor.dispatched) == index + 2)
            assert len(waiting()) == 1 - index  # 每次只放行一个
        stack.executor.finish(2)
        wait_for(lambda: _status(stack, task) == "succeeded")
        assert [payload["job_id"] for payload in stack.executor.dispatched[1:]] == queued  # 按排队先后放行
        assert not waiting()
    finally:
        stack.close()
