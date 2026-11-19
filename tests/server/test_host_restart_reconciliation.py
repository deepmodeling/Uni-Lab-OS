"""Host 进程在动作进行中重启：任务不再永久卡在 running，设备锁保持到人工裁决。

复现（修复前）：LockDemo 上 60 秒的动作执行中 kill -9 Host 子进程，Host 被拉起后任务与
Job 一直 running，同设备后续动作永远 pending。
"""

from __future__ import annotations

import time
from typing import Any, Callable

import pytest

from unilabos.registry.action_policy import SUCCESS_TYPE_EXECUTION_UNKNOWN
from unilabos.server.backend.scheduler.service import BackendScheduler
from unilabos.server.services.runtime.workflow.service import WorkflowService


class _Executor:
    def __init__(self) -> None:
        self.dispatched: list[dict] = []
        self.listener: Callable[..., None] | None = None

    def add_job_finished_listener(self, listener) -> None:
        self.listener = listener

    def remove_job_finished_listener(self, _listener) -> None:
        self.listener = None

    def dispatch(self, payload: dict) -> None:
        self.dispatched.append(payload)

    def cancel_task(self, _task_uuid: str) -> list[str]:
        return []

    def resolve_action_error_policy(self, _device_id: str, _action: str) -> dict[str, Any]:
        return {}


def _wait(predicate: Callable[[], Any], description: str, timeout: float = 5.0) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {description}")


@pytest.fixture()
def harness():
    service = WorkflowService(":memory:")
    executor = _Executor()
    scheduler = BackendScheduler(service, executor)  # type: ignore[arg-type]
    service.set_task_submitter(scheduler.submit)
    scheduler.start(recover=False)
    try:
        yield service, executor, scheduler
    finally:
        scheduler.stop()
        service.close()


def _occupy(service: WorkflowService, tag: str) -> dict:
    return service.create_ad_hoc_device_action_task(
        device_id="lock_probe_a",
        action_name="occupy",
        action_type="UniLabJsonCommand",
        param={"tag": tag, "duration_s": 60},
        execution_policy={},
        description=tag,
        meta_data={},
    )


def _lose_first_attempt(service, executor, scheduler) -> tuple[dict, dict, dict]:
    first = _occupy(service, "first")
    dispatched = _wait(lambda: executor.dispatched[0] if executor.dispatched else None, "first dispatch")
    second = _occupy(service, "second")
    time.sleep(0.2)
    assert len(executor.dispatched) == 1  # 同设备同动作：第二个在等动作锁

    executor.listener(
        dispatched["job_id"], False, None, SUCCESS_TYPE_EXECUTION_UNKNOWN,
        {"uncertainty_reason": "Host 进程在动作进行中重启"},
    )
    report = _wait(lambda: next(iter(scheduler.list_reconciliation_decisions()), None), "reconciliation decision")
    return first, second, report


def test_lost_attempt_opens_decision_and_keeps_the_device_locked(harness) -> None:
    service, executor, scheduler = harness
    first, _second, report = _lose_first_attempt(service, executor, scheduler)

    assert report["task_id"] == first["uuid"]
    assert report["exception_type"] == "ExecutionStateUnknown"
    assert report["recovered_status"] == "execution_unknown"
    job = service.get_workflow_node_job(report["job_id"])
    assert job["status"] == "execution_unknown"
    assert "Host" in job["uncertainty_reason"]
    task = service.get_workflow_task(first["uuid"])
    assert (task["status"], task["control_status"]) == ("running", "waiting_reconciliation")
    # 设备实际状态未知：锁保持，第二个动作仍不下发，也没有重放第一个
    time.sleep(0.2)
    assert len(executor.dispatched) == 1


def test_abort_decision_fails_the_task_and_releases_the_device(harness) -> None:
    service, executor, scheduler = harness
    first, second, report = _lose_first_attempt(service, executor, scheduler)

    assert scheduler.resolve_reconciliation_decision(report["decision_id"], {"action": "abort"})
    _wait(lambda: service.get_workflow_task(first["uuid"])["status"] == "failed", "first task failed")
    after = _wait(lambda: executor.dispatched[1] if len(executor.dispatched) > 1 else None, "second dispatch")
    assert after["task_id"] == second["uuid"]


def test_retry_decision_dispatches_a_new_attempt(harness) -> None:
    service, executor, scheduler = harness
    first, second, report = _lose_first_attempt(service, executor, scheduler)

    assert scheduler.resolve_reconciliation_decision(report["decision_id"], {"action": "retry"})
    # attempt 1 记 failed 后锁按排队顺序先交给第二个任务（与普通失败重试相同）；它完成后再下发 attempt 2
    queued = _wait(lambda: executor.dispatched[1] if len(executor.dispatched) > 1 else None, "queued task dispatch")
    assert queued["task_id"] == second["uuid"]
    executor.listener(queued["job_id"], True, {"ok": True}, "normal", {})
    retry = _wait(lambda: executor.dispatched[2] if len(executor.dispatched) > 2 else None, "retry dispatch")
    assert (retry["task_id"], retry["attempt_no"]) == (first["uuid"], 2)
    assert service.get_workflow_task(first["uuid"])["control_status"] == "active"


def test_late_real_result_settles_and_clears_the_decision(harness) -> None:
    service, executor, scheduler = harness
    first, second, report = _lose_first_attempt(service, executor, scheduler)

    executor.listener(report["job_id"], True, {"stopped_at": 60}, "normal", {})
    final = _wait(
        lambda: (t if (t := service.get_workflow_task(first["uuid"]))["status"] == "succeeded" else None),
        "first task succeeded",
    )
    assert final["control_status"] == "active"
    assert scheduler.list_reconciliation_decisions() == []
    after = _wait(lambda: executor.dispatched[1] if len(executor.dispatched) > 1 else None, "second dispatch")
    assert after["task_id"] == second["uuid"]
