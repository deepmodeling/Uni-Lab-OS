"""真实持久库重开：计划失败与取消都不能覆盖旧设备副作用。"""
from __future__ import annotations

import asyncio
import json
import threading
from typing import TypedDict
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from tests.server.test_workflow_execution_claims import make_task
from tests.server.test_workflow_parallel_execution import Executor, wait_for
from tests.server.test_workflow_task_cancel import command, create_task
from unilabos.server.api.runtime.workflow import create_workflow_app
from unilabos.server.backend.scheduler.service import BackendScheduler
from unilabos.server.services.runtime.workflow.execution_claim import ExecutionClaims
from unilabos.server.services.runtime.workflow.errors import StoreConflict
from unilabos.server.services.runtime.workflow.service import WorkflowService


@pytest.mark.parametrize("status", ["running", "execution_unknown", "intervention_required", "cancel_requested"])
def test_generic_cleanup_cannot_close_unresolved_physical_job(tmp_path, status):
    service = WorkflowService(tmp_path / "runtime.db")
    try:
        task, job = make_task(service)
        service.mark_workflow_node_job_running(job)
        if status == "execution_unknown":
            service.mark_workflow_node_job_execution_unknown(job, "连接断开")
        elif status == "intervention_required":
            service.mark_workflow_node_job_decision_pending(job, {})
        elif status == "cancel_requested":
            service.mark_workflow_node_job_cancel_requested(job)
        run = service.list_workflow_node_runs(task["uuid"])[0]
        with pytest.raises(StoreConflict, match="核实"):
            service.close_workflow_node_run(run["uuid"], status="canceled")
        with pytest.raises(StoreConflict, match="核实"):
            service.finish_workflow_task(task["uuid"], status="failed")
        assert service.get_workflow_node_job(job)["status"] == status
    finally:
        service.close()


def test_recovery_keeps_claim_despite_invalid_plan_until_late_result(tmp_path):
    path = tmp_path / "runtime.db"
    service = WorkflowService(path)
    executor = Executor()
    scheduler = BackendScheduler(service, executor)
    client = TestClient(create_workflow_app(service))
    task = create_task(client, devices=["reactor", "reactor"])
    scheduler.submit(task["uuid"])
    wait_for(lambda: len(executor.dispatched) == 1)
    job = executor.dispatched[0]["job_id"]
    scheduler.stop()
    snapshot = service.get_workflow_task(task["uuid"])["workflow_snapshot"]
    for node in snapshot["nodes"]:
        node["action_name"] = ""
    with service.transaction() as conn:
        conn.execute("UPDATE workflow_task SET workflow_snapshot=? WHERE uuid=?", (json.dumps(snapshot), task["uuid"]))
    service.close()

    service = WorkflowService(path)
    executor = Executor()
    scheduler = BackendScheduler(service, executor)
    try:
        scheduler.submit(task["uuid"])
        wait_for(lambda: bool(scheduler.list_reconciliation_decisions()))
        report = scheduler.list_reconciliation_decisions()[0]
        assert report["action_name"] == "record"
        assert service.get_workflow_node_job(job)["status"] == "execution_unknown"
        assert service.get_workflow_task(task["uuid"])["status"] not in {"failed", "canceled"}
        assert scheduler.resources.held_by(job)
        assert ExecutionClaims(service).get(job)["state"] == "uncertain"
        assert executor.dispatched == []
        # 直到真实迟到结果到达，才能释放这一尝试；之后不可执行的后续计划才可失败。
        scheduler._on_executor_finished(job, True, {"done": True})
        wait_for(lambda: service.get_workflow_task(task["uuid"])["status"] == "failed")
        assert service.get_workflow_node_job(job)["status"] == "succeeded"
        assert ExecutionClaims(service).get(job)["state"] == "released"
        assert executor.dispatched == []
        assert service.get_workflow_task(task["uuid"])["error_info"][0]["code"] == "plan_not_executable"
    finally:
        scheduler.stop()
        service.close()


def test_cancel_then_restart_waits_for_every_old_parallel_action(tmp_path):
    path = tmp_path / "runtime.db"
    service = WorkflowService(path)
    executor = Executor()
    scheduler = BackendScheduler(service, executor)
    client = TestClient(create_workflow_app(service))
    task = create_task(client, devices=["reactor", "camera"], parallel=True)
    scheduler.submit(task["uuid"])
    wait_for(lambda: len(executor.dispatched) == 2)
    jobs = [payload["job_id"] for payload in executor.dispatched]
    assert command(client, task, "cancel").status_code == 201
    scheduler.stop()
    service.close()
    service = WorkflowService(path)
    executor = Executor()
    scheduler = BackendScheduler(service, executor)
    try:
        scheduler.submit(task["uuid"])
        wait_for(lambda: len(scheduler.list_reconciliation_decisions()) == 2)
        first, second = scheduler.list_reconciliation_decisions()
        assert scheduler.resolve_reconciliation_decision(first["decision_id"], {"action": "abort"})
        assert service.get_workflow_task(task["uuid"])["status"] == "canceling"
        assert scheduler.resources.held_by(second["job_id"])
        assert service.get_workflow_node_job(second["job_id"])["status"] == "execution_unknown"
        assert scheduler.resolve_reconciliation_decision(second["decision_id"], {"action": "operator_intervention"})
        wait_for(lambda: service.get_workflow_task(task["uuid"])["status"] == "failed")
        assert all(ExecutionClaims(service).get(job)["state"] == "released" for job in jobs)
        assert executor.dispatched == []
    finally:
        scheduler.stop()
        service.close()


class RecoveryOutput(TypedDict):
    value: int


def test_hostlink_late_result_after_authority_restart_preserves_outputs_and_no_replay():
    from tests.server.test_workflow_loop_execution import _Stack
    from unilabos.registry.workflows import (DeviceCatalog, WorkflowDefinition,
        build_workflow_template_payload, materialize_workflow_template, upsert_workflow)

    def flow(ctx) -> RecoveryOutput:
        ctx.run("loop-device/record", {"value": 7})
        result = ctx.run("loop-device/record", {"value": 8})
        return {"value": result.value}

    stack = _Stack()
    release = threading.Event()
    original = stack.runtime.call_action_async
    first = True
    async def delay_first_receipt(*args, **kwargs):
        nonlocal first
        result = await original(*args, **kwargs)
        if first:
            first = False
            while not release.is_set():
                await asyncio.sleep(0.01)
        return result
    stack.runtime.call_action_async = delay_first_receipt
    try:
        definition = WorkflowDefinition(fn=flow, uuid=str(uuid4()), source_path="test:recovery-output",
            display_name="真实设备回执恢复", description="", tags=[], compiler="ast")
        catalog = DeviceCatalog()
        catalog.add("loop-device", "loop_demo_class", str(uuid4()))
        record = upsert_workflow(stack.service, materialize_workflow_template(build_workflow_template_payload(definition), catalog))
        task = stack.service.create_workflow_task(workflow_uuid=record["uuid"], run_mode="normal",
            target_node_uuid=None, input_value={}, description=None, meta_data={})
        wait_for(lambda: stack.driver.calls == [7])
        stack.scheduler.stop()
        stack.scheduler = BackendScheduler(stack.service, stack.microbackend)
        stack.service.set_task_submitter(stack.scheduler.submit)
        stack.scheduler.start(recover=True)
        wait_for(lambda: bool(stack.scheduler.list_reconciliation_decisions()))
        decision = stack.scheduler.list_reconciliation_decisions()[0]
        assert stack.scheduler.resources.held_by(decision["job_id"])
        assert stack.driver.calls == [7]
        release.set()
        wait_for(lambda: stack.service.get_workflow_task(task["uuid"])["status"] == "succeeded")
        assert stack.driver.calls == [7, 8]
        assert stack.service.get_workflow_task(task["uuid"])["output"] == {"value": 8}
        assert stack.scheduler.list_reconciliation_decisions() == []
    finally:
        release.set()
        stack.close()
