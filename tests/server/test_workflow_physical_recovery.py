"""调度派发、实际回执、双库恢复与人工确认共用真实材料事务。"""

from __future__ import annotations

from uuid import uuid4
import pytest

from tests.api_envelope import error_of, ok_data
from unilabos.registry.workflows import upsert_workflow
from unilabos.server.scheduler.service import BackendScheduler
from tests.server.test_workflow_inventory_recovery import Stack
from tests.server.test_workflow_parallel_execution import Executor, wait_for
from tests.server.test_scheduler_inventory_authority import _template, _material
from tests.server.test_material_execution import fill, contents


def seed(stack):
    _template(stack.materials, "container-template", "container")
    source, target = [_material(stack.materials, "container") for _ in range(2)]
    fill(stack.materials, source, [("water", 100)])
    return source, target


def make_task(stack, executor, source, target, *, inventory=None, dispatched=True):
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
                    "meta_data": {"target_device_id": "pipette", **({"inventory_requirements": inventory} if inventory else {})},
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
    if dispatched:
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


def _water(source, quantity=30):
    """分装来源上的内容物需求：同一节点既预留来源内容物、又执行分装。"""
    return [{"key": "water", "kind": "content", "material_uuid": source, "substance_name": "water",
             "quantity": quantity, "unit": "ul"}]


def _consumptions(stack, job_uuid):
    from unilabos.server.services.materials.consumption import InventoryConsumptions

    return [(item["inventory_type"], item["planned_quantity"], item["actual_quantity"])
            for item in InventoryConsumptions(stack.materials).list_job(job_uuid)]


def test_aliquot_source_reserved_as_content_is_deducted_once_when_the_scheduler_settles(tmp_path):
    """调度器终态对账结算预留（Host 没有在动作开始时消费）：分装回执已经扣过来源，预留结算不能再扣一次。"""
    stack, executor = Stack(tmp_path), Executor()
    source, target = seed(stack)
    scheduler, task = make_task(stack, executor, source, target, inventory=_water(source))
    try:
        job = executor.dispatched[0]["job_id"]
        [reservation] = stack.client.list_inventory_reservations(task_uuid=task["uuid"])
        executor.finish(0, result(target))
        wait_for(lambda: stack.workflow.get_workflow_task(task["uuid"])["status"] == "succeeded")
        scheduler._reconcile_inventory()
        assert stack.client.get_inventory_reservation(reservation.reservation_uuid).status == "consumed"
        assert contents(stack.materials, source) == {"water": 70}
        assert contents(stack.materials, target) == {"water": 30}
        # 读模型：内容物预留的实际量取分装台账，不再另列一条分装消耗
        assert _consumptions(stack, job) == [("current_substance", 30.0, 30.0)]
    finally:
        scheduler.stop()
        stack.close()


def test_aliquot_source_consumed_at_action_start_is_settled_by_the_actual_aliquot(tmp_path):
    """Host 在动作开始时消费预留：来源内容物等分装回执按实际量扣一次，期间预留仍挡住其他任务。"""
    from unilabos.server.inventory import ExecutionInventoryCoordinator
    from unilabos.protocol.materials import InventoryRequirement

    stack, executor = Stack(tmp_path), Executor()
    source, target = seed(stack)
    scheduler, task = make_task(stack, executor, source, target, inventory=_water(source))
    try:
        payload = executor.dispatched[0]
        host = ExecutionInventoryCoordinator(stack.client)
        host.prepare(payload)
        assert host.consume(payload["job_id"]).status == "consumed"  # 动作开始
        assert contents(stack.materials, source) == {"water": 100}  # 实际量以分装回执为准
        shortage = stack.materials.preview_inventory([InventoryRequirement(
            key="other", kind="content", material_uuid=source, substance_name="water", quantity=80, unit="ul")])
        assert not shortage["available"]  # 分装结算前这 30 ul 仍归本作业
        executor.finish(0, result(target, 25))
        wait_for(lambda: stack.workflow.get_workflow_task(task["uuid"])["status"] == "succeeded")
        scheduler._reconcile_inventory()
        assert contents(stack.materials, source) == {"water": 75}
        assert contents(stack.materials, target) == {"water": 25}
        assert _consumptions(stack, payload["job_id"]) == [("current_substance", 30.0, 25.0)]
    finally:
        scheduler.stop()
        stack.close()


def test_released_aliquot_releases_the_source_content_reserved_at_action_start(tmp_path):
    """动作开始后失败、现场核实恢复原状（不结算分装）：留给分装结算的来源预留按未取用释放。"""
    from unilabos.protocol.runtime.execution_start import execution_identity
    from unilabos.server.inventory import ExecutionInventoryCoordinator

    stack, executor = Stack(tmp_path), Executor()
    source, target = seed(stack)
    scheduler, task = make_task(stack, executor, source, target, inventory=_water(source))
    try:
        payload = executor.dispatched[0]
        scheduler._execution_claims.begin_execution(payload["job_id"], {  # Host 取得开始许可后才消费
            "permit": payload["execution_permit"], "invocation_uuid": str(uuid4()),
            "executor_epoch": "test-host", "identity": execution_identity(payload)})
        host = ExecutionInventoryCoordinator(stack.client)
        host.prepare(payload)
        host.consume(payload["job_id"])
        executor.finish(0, {})  # 没有分装回执
        wait_for(lambda: bool(scheduler.list_reconciliation_decisions()))
        decision = scheduler.list_reconciliation_decisions()[0]
        assert scheduler.resolve_reconciliation_decision(decision["decision_id"], {"action": "abort", "reason": "停止"})
        wait_for(lambda: stack.workflow.get_workflow_task(task["uuid"])["status"] == "failed")
        scheduler._reconcile_inventory()
        [content] = stack.materials._content_contract().list_content_reservations(task_uuid=task["uuid"])
        assert content["status"] == "active"  # 分装是否发生只有现场能确认，结算前不扣也不放
        row = scheduler._physical_recovery.snapshot()[0]
        scheduler._physical_recovery.release_verified(payload["job_id"], row["version"], "现场确认未分装")
        [content] = stack.materials._content_contract().list_content_reservations(task_uuid=task["uuid"])
        assert content["status"] == "released"
        assert contents(stack.materials, source) == {"water": 100}
        assert _consumptions(stack, payload["job_id"]) == []
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
            report = ok_data(client.get("/api/v1/error-decisions"))["decisions"][0]
            assert report["category"] == "physical_settlement"
            assert report["material_execution"]["request"]["aliquot"]["source_material_uuid"] == source
            assert {item["action"] for item in report["options"]} == {"abort", "operator_intervention"}
            for decision in ({"action": "retry", "reason": "重试"}, {"action": "abort"}):
                error_of(client.post("/api/v1/error-decisions/host-pending", json=decision), 3003)
            assert len(executor.dispatched) == 1
    finally:
        scheduler.stop()
        stack.close()
