"""条件选料、缺料的预检报错（任务级直接失败、动作级挂起待补料重试）及双库恢复走实际工作流计划和库存事务。"""
from __future__ import annotations

from uuid import uuid4
import pytest

from unilabos.registry.workflows import WorkflowDefinition, DeviceCatalog, build_workflow_template_payload, materialize_workflow_template, upsert_workflow
from unilabos.server.backend.scheduler.service import BackendScheduler
from unilabos.server.backend.scheduler.attempt_inventory import freeze_inventory
from tests.server.test_workflow_inventory_recovery import Stack
from tests.server.test_workflow_parallel_execution import Executor, wait_for
from tests.server.test_scheduler_inventory_authority import _template, _inbound


def create(stack, executor, flow, *, submit=True):
    catalog = DeviceCatalog()
    catalog.add("reactor", "unknown", str(uuid4()))
    definition = WorkflowDefinition(fn=flow, uuid=str(uuid4()), source_path="test:inventory",
        display_name="分支选料", description="", tags=[], compiler="ast")
    payload = materialize_workflow_template(build_workflow_template_payload(definition), catalog)
    record = upsert_workflow(stack.workflow, payload)
    scheduler = BackendScheduler(stack.workflow, executor, materials_gateway=stack.client)
    if submit:
        stack.workflow.set_task_submitter(scheduler.submit)
    task = stack.workflow.create_workflow_task(workflow_uuid=record["uuid"], run_mode="normal",
        target_node_uuid=None, input_value={}, description=None, meta_data={})
    return scheduler, task


def test_only_selected_branch_reserves_and_consumes(tmp_path):
    def flow(ctx):
        if True:
            ctx.run("reactor/operate", {}, inventory=[{"key": "solvent", "kind": "lot",
                "template_uuid": "reagent-template", "quantity": 5, "unit": "ul"}])
        else:
            ctx.run("reactor/operate", {}, inventory=[{"key": "solvent", "kind": "lot",
                "template_uuid": "nonexistent-template", "quantity": 1000, "unit": "ul"}])
    stack = Stack(tmp_path)
    _template(stack.materials, "reagent-template", "reagent")
    _inbound(stack.materials, lot_uuid="lot-a", quantity=5)
    executor = Executor()
    scheduler, task = create(stack, executor, flow)
    try:
        wait_for(lambda: len(executor.dispatched) == 1)
        assert len(stack.client.list_inventory_reservations()) == 1
        executor.finish(0, {})
        wait_for(lambda: stack.workflow.get_workflow_task(task["uuid"])["status"] == "succeeded")
        scheduler._reconcile_inventory()
        assert stack.quantities() == (0, 0, 0)
        assert len(executor.dispatched) == 1
    finally:
        scheduler.stop()
        stack.close()


def _failed_run(stack, task_uuid):
    return next(run for run in stack.workflow.list_workflow_node_runs(task_uuid) if run["status"] == "failed")


def _dynamic_flow(ctx):
    source = ctx.run("reactor/select", {})
    ctx.run("reactor/operate", {}, inventory=[{"key": "solvent", "kind": "lot",
        "lot_uuid": source.lot, "quantity": source.amount, "unit": "ul"}])


def _preflight_decision(scheduler, task_uuid):
    decisions = [item for item in scheduler.list_reconciliation_decisions() if item["task_id"] == task_uuid]
    return decisions[0] if decisions else None


def test_runtime_shortage_is_an_action_preflight_error_handled_by_replenish_and_retry(tmp_path):
    """动作执行前才预留的需求不足：动作预检报错、挂起待决策（不静默等待）；补料后重试，同一 attempt 重新预检通过即执行。"""
    stack = Stack(tmp_path)
    _template(stack.materials, "reagent-template", "reagent")
    _inbound(stack.materials, lot_uuid="lot-a", quantity=2)
    executor = Executor()
    scheduler, task = create(stack, executor, _dynamic_flow)
    try:
        wait_for(lambda: len(executor.dispatched) == 1)
        executor.finish(0, {"lot": "lot-a", "amount": 5})
        wait_for(lambda: _preflight_decision(scheduler, task["uuid"]) is not None)
        decision = _preflight_decision(scheduler, task["uuid"])
        assert (decision["category"], decision["stage"]) == ("preflight", "preflight")
        assert [option["action"] for option in decision["options"]] == ["retry", "skip", "operator_intervention", "abort"]
        [error] = decision["error_info"]
        assert (error["code"], error["stage"], error["scope"]) == ("inventory_shortage", "preflight", "action")
        assert "动作执行前预检" in error["message"] and error["shortages"]
        job = stack.workflow.get_workflow_node_job(decision["job_id"])
        assert job["status"] == "intervention_required"
        assert job["control_data"]["pending_decision"]["category"] == "preflight"
        assert len(executor.dispatched) == 1  # 缺料的动作没有派发
        assert not stack.client.list_inventory_reservations()
        assert stack.workflow.get_workflow_task(task["uuid"])["status"] == "running"

        _inbound(stack.materials, lot_uuid="lot-a", quantity=4)  # 补料
        assert scheduler.resolve_reconciliation_decision(decision["decision_id"], {"action": "retry", "reason": "已补料"})
        wait_for(lambda: len(executor.dispatched) == 2)
        assert executor.dispatched[1]["inventory_requirements"][0]["quantity"] == 5
        executor.finish(1, {})
        wait_for(lambda: stack.workflow.get_workflow_task(task["uuid"])["status"] == "succeeded")
        scheduler._reconcile_inventory()
        assert stack.quantities() == (1, 1, 0)
        attempts = [item for item in stack.workflow.list_workflow_node_jobs(task["uuid"])
                    if item["workflow_node_uuid"] == decision["node_id"]]
        assert [item["status"] for item in attempts] == ["succeeded"]  # 动作没尝试过：预检重试不消耗 attempt
        [resumed] = attempts[0]["control_data"]["resumed_decisions"]
        assert (resumed["category"], resumed["decision_id"]) == ("preflight", decision["decision_id"])
    finally:
        scheduler.stop()
        stack.close()


def test_abandoning_an_action_preflight_error_fails_the_task_as_preflight(tmp_path):
    stack = Stack(tmp_path)
    _template(stack.materials, "reagent-template", "reagent")
    _inbound(stack.materials, lot_uuid="lot-a", quantity=2)
    executor = Executor()
    scheduler, task = create(stack, executor, _dynamic_flow)
    try:
        wait_for(lambda: len(executor.dispatched) == 1)
        executor.finish(0, {"lot": "lot-a", "amount": 5})
        wait_for(lambda: _preflight_decision(scheduler, task["uuid"]) is not None)
        decision = _preflight_decision(scheduler, task["uuid"])
        assert scheduler.resolve_reconciliation_decision(decision["decision_id"], {"action": "abort", "reason": "不补了"})
        wait_for(lambda: stack.workflow.get_workflow_task(task["uuid"])["status"] == "failed")
        assert stack.workflow.get_workflow_task(task["uuid"])["error_info"] == [
            {"code": "node_preflight_failed", "stage": "preflight", "scope": "action"}]
        [error] = _failed_run(stack, task["uuid"])["error_info"]
        assert (error["code"], error["stage"]) == ("inventory_shortage", "preflight")
        assert len(executor.dispatched) == 1
    finally:
        scheduler.stop()
        stack.close()


@pytest.mark.parametrize(("action", "status"), [("skip", "skipped"), ("operator_intervention", "succeeded")])
def test_action_preflight_error_can_be_skipped_or_done_by_hand(tmp_path, action, status):
    """跳过 / 人工替换为成功：缺料的动作不派发、不扣库存，下游照常执行。"""
    def flow(ctx):
        source = ctx.run("reactor/select", {})
        ctx.run("reactor/operate", {}, inventory=[{"key": "solvent", "kind": "lot",
            "lot_uuid": source.lot, "quantity": source.amount, "unit": "ul"}])
        ctx.run("reactor/report", {})
    stack = Stack(tmp_path)
    _template(stack.materials, "reagent-template", "reagent")
    _inbound(stack.materials, lot_uuid="lot-a", quantity=2)
    executor = Executor()
    scheduler, task = create(stack, executor, flow)
    try:
        wait_for(lambda: len(executor.dispatched) == 1)
        executor.finish(0, {"lot": "lot-a", "amount": 5})
        wait_for(lambda: _preflight_decision(scheduler, task["uuid"]) is not None)
        decision = _preflight_decision(scheduler, task["uuid"])
        assert scheduler.resolve_reconciliation_decision(
            decision["decision_id"], {"action": action, "reason": "操作员处置", "result": {"done_by": "operator"}})
        wait_for(lambda: len(executor.dispatched) == 2)
        assert executor.dispatched[1]["action"] == "report"  # 缺料的动作没派发，直接到下游
        executor.finish(1, {})
        wait_for(lambda: stack.workflow.get_workflow_task(task["uuid"])["status"] == "succeeded")
        run = next(item for item in stack.workflow.list_workflow_node_runs(task["uuid"])
                   if item["workflow_node_uuid"] == decision["node_id"])
        assert run["status"] == status
        assert not stack.client.list_inventory_reservations()
    finally:
        scheduler.stop()
        stack.close()


def test_action_preflight_decision_survives_restart_without_holding_resources(tmp_path):
    def flow(ctx):
        ctx.run("reactor/operate", {}, inventory_mode="on_ready", inventory=[{"key": "solvent", "kind": "lot",
            "lot_uuid": "lot-a", "quantity": 5, "unit": "ul"}])
    stack = Stack(tmp_path)
    _template(stack.materials, "reagent-template", "reagent")
    _inbound(stack.materials, lot_uuid="lot-a", quantity=1)
    executor = Executor()
    scheduler, task = create(stack, executor, flow)
    wait_for(lambda: _preflight_decision(scheduler, task["uuid"]) is not None)
    before = _preflight_decision(scheduler, task["uuid"])
    scheduler.stop()
    stack.close()
    reopened = Stack(tmp_path)
    resumed = BackendScheduler(reopened.workflow, executor, materials_gateway=reopened.client)
    try:
        resumed.submit(task["uuid"])
        wait_for(lambda: _preflight_decision(resumed, task["uuid"]) is not None)
        after = _preflight_decision(resumed, task["uuid"])
        assert after["decision_id"] == before["decision_id"] and after["category"] == "preflight"
        assert [option["action"] for option in after["options"]] == ["retry", "skip", "operator_intervention", "abort"]
        assert after["error_info"][0]["code"] == "inventory_shortage"
        assert not resumed._intervals.snapshot(task["uuid"])  # noqa: SLF001 - 没派发过的动作不保留设备区间
        _inbound(reopened.materials, lot_uuid="lot-a", quantity=4)
        assert resumed.resolve_reconciliation_decision(after["decision_id"], {"action": "retry", "reason": "已补料"})
        wait_for(lambda: len(executor.dispatched) == 1)
        executor.finish(0, {})
        wait_for(lambda: reopened.workflow.get_workflow_task(task["uuid"])["status"] == "succeeded")
    finally:
        resumed.stop()
        reopened.close()


def test_start_shortage_is_a_preflight_error_before_any_dispatch(tmp_path):
    """任务启动时整任务预留不足是预检报错：任务失败，不派发任何动作。"""
    def flow(ctx):
        ctx.run("reactor/prepare", {})
        ctx.run("reactor/operate", {}, inventory=[{"key": "solvent", "kind": "lot",
            "lot_uuid": "lot-a", "quantity": 5, "unit": "ul"}])
    stack = Stack(tmp_path)
    _template(stack.materials, "reagent-template", "reagent")
    _inbound(stack.materials, lot_uuid="lot-a", quantity=1)
    executor = Executor()
    scheduler, task = create(stack, executor, flow)
    try:
        wait_for(lambda: stack.workflow.get_workflow_task(task["uuid"])["status"] == "failed")
        [error] = stack.workflow.get_workflow_task(task["uuid"])["error_info"]
        assert (error["code"], error["stage"], error["scope"], error["reason"]) == (
            "plan_not_executable", "preflight", "task", "inventory_shortage")
        assert executor.dispatched == []
    finally:
        scheduler.stop()
        stack.close()


def test_frozen_source_survives_materials_commit_without_reply(tmp_path):
    def flow(ctx):
        ctx.run("reactor/operate", {}, inventory_mode="on_ready", inventory=[{"key": "solvent", "kind": "lot",
            "lot_uuid": "lot-a", "quantity": 5, "unit": "ul"}])
    first = Stack(tmp_path)
    _template(first.materials, "reagent-template", "reagent")
    _inbound(first.materials, lot_uuid="lot-a", quantity=10)
    scheduler, task = create(first, Executor(), flow, submit=False)
    dag, specs = scheduler._build_dag(task, first.workflow.list_workflow_node_runs(task["uuid"]))
    run_uuid, spec = next(iter(specs.items()))
    scheduler._run_specs.update(specs)
    spec["inventory_requirements"] = freeze_inventory(first.workflow, scheduler._authority_epoch,
        spec["current_job_uuid"], lambda: spec["inventory_template"])
    scheduler._reserve_inventory(task, [(spec["current_job_uuid"], spec)], ready=True)
    original = first.client.list_inventory_reservations()[0]
    scheduler.stop()
    first.close()
    reopened = Stack(tmp_path)
    executor = Executor()
    resumed = BackendScheduler(reopened.workflow, executor, materials_gateway=reopened.client)
    try:
        assert not resumed._inventory_recovery.reconcile()["issues"]
        resumed.submit(task["uuid"])
        wait_for(lambda: len(executor.dispatched) == 1)
        assert executor.dispatched[0]["inventory_reservation_uuid"] == original.reservation_uuid
        assert reopened.quantities() == (10, 5, 5)
        executor.finish(0, {})
        wait_for(lambda: reopened.workflow.get_workflow_task(task["uuid"])["status"] == "succeeded")
        resumed._reconcile_inventory()
        assert reopened.quantities() == (5, 5, 0)
    finally:
        resumed.stop()
        reopened.close()


def test_on_ready_loop_uses_fresh_reservation_per_iteration(tmp_path):
    def flow(ctx):
        with ctx.loop_for(2):
            ctx.run("reactor/operate", {}, inventory_mode="on_ready", inventory=[{"key": "solvent", "kind": "lot",
                "lot_uuid": "lot-a", "quantity": 5, "unit": "ul"}])
    stack = Stack(tmp_path)
    _template(stack.materials, "reagent-template", "reagent")
    _inbound(stack.materials, lot_uuid="lot-a", quantity=10)
    executor = Executor()
    scheduler, task = create(stack, executor, flow)
    try:
        wait_for(lambda: len(executor.dispatched) == 1)
        executor.finish(0, {})
        wait_for(lambda: len(executor.dispatched) == 2)
        assert executor.dispatched[0]["inventory_reservation_uuid"] != executor.dispatched[1]["inventory_reservation_uuid"]
        executor.finish(1, {})
        wait_for(lambda: stack.workflow.get_workflow_task(task["uuid"])["status"] == "succeeded")
        scheduler._reconcile_inventory()
        assert stack.quantities() == (0, 0, 0)
    finally:
        scheduler.stop()
        stack.close()


def test_published_child_inventory_references_follow_expansion_identity_and_input():
    from unilabos.server.services.runtime.workflow.composite_expansion import _remap_depends_on, _materialize_param_references
    source, expanded = str(uuid4()), str(uuid4())
    node = {"type": "device_action", "param": {}, "meta_data": {"inventory_requirements": [{"key": "solvent", "kind": "lot",
        "lot_uuid": {"kind": "node_output", "workflow_node_uuid": source, "data_key": "lot"},
        "quantity": {"kind": "workflow_input", "parameter": "amount"}, "unit": "ul"}]}}
    _remap_depends_on(node, {source: expanded})
    _materialize_param_references([node], input_names={"amount"}, keyword_arguments={"amount": 5})
    inventory = node["meta_data"]["inventory_requirements"][0]
    assert inventory["lot_uuid"]["workflow_node_uuid"] == expanded
    assert inventory["quantity"] == 5
