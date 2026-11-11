"""调度派发、实际回执、双库恢复与人工确认共用真实材料事务。"""

from __future__ import annotations

from uuid import uuid4
import pytest

from unilabos.registry.workflows import upsert_workflow
from unilabos.server.backend.scheduler.service import BackendScheduler
from tests.server.test_workflow_inventory_recovery import Stack
from tests.server.test_workflow_parallel_execution import Executor, wait_for
from tests.server.test_scheduler_inventory_authority import _template, _material
from tests.server.test_material_execution import fill, contents


def seed(stack):
    _template(stack.materials, "container-template", "container")
    source, target = [_material(stack.materials, "container") for _ in range(2)]
    fill(stack.materials, source, [("water", 100)])
    return source, target


def make_task(stack, executor, source, target):
    field = {
        "type": "object",
        "properties": {"uuid": {"type": "string"}},
        "required": ["uuid"],
        "x-unilabos-material-lock": True,
    }
    schema = {
        "x-unilabos-action-contract": {
            "version": 2,
            "resource_contract": {
                "version": 1,
                "aliquot": {"source_material_param": "source", "target_material_params": ["target"]},
            },
        },
        "properties": {
            "goal": {
                "type": "object",
                "properties": {"source": field, "target": field},
                "required": ["source", "target"],
            },
            "result": {"type": "object", "properties": {"material_aliquot_receipts": {"type": "array"}}},
        },
    }
    stack.workflow.configure_preflight(materials=stack.client, action_schema_resolver=lambda *_: schema)
    scheduler = BackendScheduler(stack.workflow, executor, materials_gateway=stack.client)
    stack.workflow.set_task_submitter(scheduler.submit)
    record = upsert_workflow(
        stack.workflow,
        {
            "workflow_uuid": str(uuid4()),
            "name": "实际分装",
            "description": "",
            "tags": [],
            "edges": [],
            "nodes": [
                {
                    "uuid": str(uuid4()),
                    "name": "分装",
                    "type": "device_action",
                    "material_uuid": str(uuid4()),
                    "action_name": "aliquot",
                    "meta_data": {"target_device_id": "pipette"},
                    "param": {"source": {"uuid": source}, "target": {"uuid": target}},
                }
            ],
        },
    )
    task = stack.workflow.create_workflow_task(
        workflow_uuid=record["uuid"],
        run_mode="normal",
        target_node_uuid=None,
        input_value={},
        description=None,
        meta_data={},
    )
    wait_for(lambda: len(executor.dispatched) == 1)
    return scheduler, task


def result(target, amount=30):
    return {
        "material_aliquot_receipts": [
            {"target_material_uuid": target, "actual_quantity": amount, "quantity_unit": "ul"}
        ]
    }


def test_success_settles_actual_contents_before_task_completion(tmp_path):
    stack, executor = Stack(tmp_path), Executor()
    source, target = seed(stack)
    scheduler, task = make_task(stack, executor, source, target)
    try:
        assert executor.dispatched[0]["material_execution"]["job_uuid"] == executor.dispatched[0]["job_id"]
        executor.finish(0, result(target))
        wait_for(lambda: stack.workflow.get_workflow_task(task["uuid"])["status"] == "succeeded")
        assert contents(stack.materials, source) == {"water": 70}
        assert contents(stack.materials, target) == {"water": 30}
        assert not scheduler._physical_recovery.snapshot()
    finally:
        scheduler.stop()
        stack.close()


def test_missing_receipt_requires_manual_confirmation_without_reexecuting(tmp_path):
    stack, executor = Stack(tmp_path), Executor()
    source, target = seed(stack)
    scheduler, task = make_task(stack, executor, source, target)
    try:
        executor.finish(0, {})
        wait_for(lambda: bool(scheduler.list_reconciliation_decisions()))
        decision = scheduler.list_reconciliation_decisions()[0]
        assert {item["action"] for item in decision["options"]} == {"operator_intervention", "abort"}
        assert not scheduler.resolve_reconciliation_decision(
            decision["decision_id"], {"action": "retry", "reason": "重试"}
        )
        assert contents(stack.materials, source) == {"water": 100}
        assert scheduler.resolve_reconciliation_decision(
            decision["decision_id"],
            {"action": "operator_intervention", "reason": "已现场核实实际转入 25 ul", "result": result(target, 25)},
        )
        wait_for(lambda: stack.workflow.get_workflow_task(task["uuid"])["status"] == "succeeded")
        assert contents(stack.materials, source) == {"water": 75}
        assert len(executor.dispatched) == 1
    finally:
        scheduler.stop()
        stack.close()


def test_settlement_commit_lost_reply_is_replayed_after_reopen_without_device_call(tmp_path):
    first, executor = Stack(tmp_path), Executor()
    source, target = seed(first)
    scheduler, task = make_task(first, executor, source, target)
    settle = first.client.settle_material_execution

    def lost(value):
        settle(value)
        raise ConnectionError("结算已提交但应答丢失")

    first.client.settle_material_execution = lost
    executor.finish(0, result(target, 20))
    wait_for(lambda: bool(scheduler.list_reconciliation_decisions()))
    assert contents(first.materials, source) == {"water": 80}
    scheduler.stop()
    first.close()
    reopened, second_executor = Stack(tmp_path), Executor()
    resumed = BackendScheduler(reopened.workflow, second_executor, materials_gateway=reopened.client)
    try:
        resumed.submit(task["uuid"])
        wait_for(lambda: reopened.workflow.get_workflow_task(task["uuid"])["status"] == "succeeded")
        assert contents(reopened.materials, source) == {"water": 80}
        assert contents(reopened.materials, target) == {"water": 20}
        assert not second_executor.dispatched
    finally:
        resumed.stop()
        reopened.close()


def test_abort_keeps_physical_hold_and_operator_can_settle_partial_result(tmp_path):
    from unilabos.protocol.runtime.execution_start import execution_identity

    stack, executor = Stack(tmp_path), Executor()
    source, target = seed(stack)
    scheduler, task = make_task(stack, executor, source, target)
    try:
        payload = executor.dispatched[0]
        scheduler._execution_claims.begin_execution(
            payload["job_id"],
            {
                "permit": payload["execution_permit"],
                "invocation_uuid": str(uuid4()),
                "executor_epoch": "test-host",
                "identity": execution_identity(payload),
            },
        )
        executor.finish(0, {})
        wait_for(lambda: bool(scheduler.list_reconciliation_decisions()))
        decision = scheduler.list_reconciliation_decisions()[0]
        assert scheduler.resolve_reconciliation_decision(
            decision["decision_id"], {"action": "abort", "reason": "停止并现场核实部分分装"}
        )
        wait_for(lambda: stack.workflow.get_workflow_task(task["uuid"])["status"] == "failed")
        row = scheduler._physical_recovery.snapshot()[0]
        assert row["can_release"]
        assert scheduler._physical_recovery.blockers("another-job", set(), {"pipette"}) == [payload["job_id"]]
        scheduler._reconcile_inventory()
        assert stack.workflow.get_workflow_task(task["uuid"])["cleanup_status"] == "requires_attention"
        with pytest.raises((ValueError, RuntimeError)):
            scheduler._physical_recovery.confirm_terminal_settlement(
                payload["job_id"], row["version"], "错误目标", result(str(uuid4()))
            )
        job = stack.workflow.get_workflow_node_job(payload["job_id"])
        assert not job["control_data"].get("manual_physical_settlement")
        confirmed = scheduler._physical_recovery.confirm_terminal_settlement(
            payload["job_id"], row["version"], "现场实测完成 12 ul", result(target, 12)
        )
        assert confirmed["state"] == "settled"
        assert not scheduler._physical_recovery.blockers("another-job", set(), {"pipette"})
        assert stack.workflow.get_workflow_task(task["uuid"])["cleanup_status"] == "settled"
        assert contents(stack.materials, source) == {"water": 88}
        assert stack.workflow.get_workflow_task(task["uuid"])["status"] == "failed"
        assert (
            scheduler._physical_recovery.confirm_terminal_settlement(
                payload["job_id"], row["version"], "现场实测完成 12 ul", result(target, 12)
            )["state"]
            == "settled"
        )
        assert contents(stack.materials, source) == {"water": 88}
    finally:
        scheduler.stop()
        stack.close()


def test_manual_settlement_intent_survives_commit_without_reply_and_reopen(tmp_path):
    from unilabos.protocol.runtime.execution_start import execution_identity

    first, executor = Stack(tmp_path), Executor()
    source, target = seed(first)
    scheduler, task = make_task(first, executor, source, target)
    payload = executor.dispatched[0]
    scheduler._execution_claims.begin_execution(
        payload["job_id"],
        {
            "permit": payload["execution_permit"],
            "invocation_uuid": str(uuid4()),
            "executor_epoch": "host",
            "identity": execution_identity(payload),
        },
    )
    executor.finish(0, {})
    wait_for(lambda: bool(scheduler.list_reconciliation_decisions()))
    decision = scheduler.list_reconciliation_decisions()[0]
    assert scheduler.resolve_reconciliation_decision(
        decision["decision_id"], {"action": "abort", "reason": "停止后实测"}
    )
    wait_for(lambda: first.workflow.get_workflow_task(task["uuid"])["status"] == "failed")
    record = scheduler._physical_recovery.snapshot()[0]
    real = first.client.settle_material_execution

    def lost(value):
        real(value)
        raise ConnectionError("人工结算提交后应答丢失")

    first.client.settle_material_execution = lost
    with pytest.raises(ConnectionError):
        scheduler._physical_recovery.confirm_terminal_settlement(
            payload["job_id"], record["version"], "实测 9 ul", result(target, 9)
        )
    assert contents(first.materials, source) == {"water": 91}
    scheduler.stop()
    first.close()
    reopened, executor2 = Stack(tmp_path), Executor()
    resumed = BackendScheduler(reopened.workflow, executor2, materials_gateway=reopened.client)
    try:
        job = reopened.workflow.get_workflow_node_job(payload["job_id"])
        assert job["control_data"]["manual_physical_settlement"]["completed"]
        assert contents(reopened.materials, source) == {"water": 91}
        assert contents(reopened.materials, target) == {"water": 9}
        assert not executor2.dispatched
        assert reopened.workflow.get_workflow_task(task["uuid"])["status"] == "failed"
        assert not resumed._physical_recovery.snapshot()
    finally:
        resumed.stop()
        reopened.close()


def test_host_pending_physical_error_api_requires_reason_and_disallows_retry(tmp_path):
    from fastapi.testclient import TestClient
    from types import SimpleNamespace
    from unilabos.server.api.runtime.diagnostics import create_backend_app

    stack, executor = Stack(tmp_path), Executor()
    source, target = seed(stack)
    scheduler, task = make_task(stack, executor, source, target)
    payload = executor.dispatched[0]
    host = SimpleNamespace(
        list_error_decisions=lambda: [
            {
                "decision_id": "host-pending",
                "job_id": payload["job_id"],
                "options": [{"action": name} for name in ("retry", "abort", "operator_intervention")],
            }
        ]
    )
    try:
        with TestClient(create_backend_app(lambda: scheduler, lambda: host)) as client:
            report = client.get("/api/v1/error-decisions").json()["items"][0]
            assert report["category"] == "physical_settlement"
            assert report["material_execution"]["request"]["aliquot"]["source_material_uuid"] == source
            assert {item["action"] for item in report["options"]} == {"abort", "operator_intervention"}
            for decision in ({"action": "retry", "reason": "重试"}, {"action": "abort"}):
                assert client.post("/api/v1/error-decisions/host-pending", json=decision).status_code == 409
            assert len(executor.dispatched) == 1
    finally:
        scheduler.stop()
        stack.close()
