"""派发时按 ``@action(estimate_duration_*)`` 求出预计时长写到 attempt 上，时间线优先用它（A01）。"""

from __future__ import annotations

from typing import Any, Dict

from tests.server.test_workflow_resource_intervals import make_task
from tests.server.test_workflow_task_cancel import Executor, wait_for
from unilabos.registry.action_duration import resolve_action_estimate
from unilabos.server.backend.scheduler.service import BackendScheduler
from unilabos.server.services.runtime.workflow.service import WorkflowService
from unilabos.server.services.runtime.workflow.timeline import ExecutionTimeline


class _EstimatingExecutor(Executor):
    def __init__(self, mapping: Dict[str, Any] | None, *, broken: bool = False) -> None:
        super().__init__()
        self.mapping = mapping
        self.broken = broken
        self.estimate_args: list[dict] = []

    def resolve_action_estimate(self, device_id: str, action: str, args: Dict[str, Any]) -> Dict[str, Any]:
        self.estimate_args.append(dict(args))
        if self.broken:
            raise RuntimeError("注册表不可用")
        return resolve_action_estimate(self.mapping, args, action_name=f"{device_id}.{action}")


def _dispatch_one(tmp_path, executor):
    service = WorkflowService(tmp_path / "runtime.db")
    scheduler = BackendScheduler(service, executor)
    task = make_task(service, ("reactor",))
    scheduler.submit(task["uuid"])
    wait_for(lambda: len(executor.dispatched) == 1)
    return service, scheduler, task, executor.dispatched[0]["job_id"]


def test_dispatch_records_the_estimate_and_the_timeline_uses_it(tmp_path) -> None:
    executor = _EstimatingExecutor({"estimate_duration_express": "cycles * 2", "goal_default": {"cycles": 21}})
    service, scheduler, task, job_uuid = _dispatch_one(tmp_path, executor)
    try:
        job = service.get_workflow_node_job(job_uuid)
        assert (job["estimated_s"], job["estimate_source"]) == (42.0, "expression")
        running = next(item for item in ExecutionTimeline(service).timeline()["running"] if item["job_id"] == job_uuid)
        assert (running["estimated_s"], running["estimate_source"]) == (42.0, "expression")

        executor.finish(0)
        wait_for(lambda: service.get_workflow_task(task["uuid"])["status"] == "succeeded")
        completed = next(item for item in ExecutionTimeline(service).timeline()["completed"] if item["job_id"] == job_uuid)
        assert (completed["estimated_s"], completed["estimate_source"]) == (42.0, "expression")
    finally:
        scheduler.stop()
        service.close()


def test_an_estimate_that_cannot_be_resolved_never_blocks_dispatch(tmp_path) -> None:
    executor = _EstimatingExecutor(None, broken=True)
    service, scheduler, _task, job_uuid = _dispatch_one(tmp_path, executor)
    try:
        job = service.get_workflow_node_job(job_uuid)
        assert "estimated_s" not in job and executor.estimate_args == [{}]
        running = next(item for item in ExecutionTimeline(service).timeline()["running"] if item["job_id"] == job_uuid)
        assert (running["estimated_s"], running["estimate_source"]) == (None, "unknown")
    finally:
        scheduler.stop()
        service.close()


def test_timeline_prefers_declared_then_observed_then_default() -> None:
    stats = {"reactor:operate": {"count": 3, "mean_s": 12.5, "min_s": 10.0, "max_s": 15.0}}
    estimate = ExecutionTimeline._estimate  # noqa: SLF001
    assert estimate(stats, "reactor:operate", 42.0, "expression") == (42.0, "expression")
    assert estimate(stats, "reactor:operate", 30.0, "fixed") == (30.0, "fixed")
    assert estimate(stats, "reactor:operate", 60.0, "default") == (12.5, "observed")  # 实测优于笼统兜底
    assert estimate({}, "reactor:operate", 60.0, "default") == (60.0, "default")
    assert estimate({}, "reactor:operate", None, None) == (None, "unknown")
    assert estimate({}, "reactor:operate", 0.0, "unknown") == (None, "unknown")  # 旧行在 job_runs 里的投影
