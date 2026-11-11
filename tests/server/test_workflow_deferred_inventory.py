"""条件选料、补料及双库恢复走实际工作流计划和库存事务。"""
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


def test_dynamic_source_waits_for_replenishment_without_dispatch_or_device_lock(tmp_path):
    def flow(ctx):
        source = ctx.run("reactor/select", {})
        ctx.run("reactor/operate", {}, inventory=[{"key": "solvent", "kind": "lot",
            "lot_uuid": source.lot, "quantity": source.amount, "unit": "ul"}])
    stack = Stack(tmp_path)
    _template(stack.materials, "reagent-template", "reagent")
    _inbound(stack.materials, lot_uuid="lot-a", quantity=2)
    executor = Executor()
    scheduler, task = create(stack, executor, flow)
    try:
        wait_for(lambda: len(executor.dispatched) == 1)
        executor.finish(0, {"lot": "lot-a", "amount": 5})
        wait_for(lambda: bool(scheduler._inventory_recovery.reconcile()["waiting"]))
        report = scheduler._inventory_recovery.snapshot()
        assert report["waiting"][0]["requirements"][0]["lot_uuid"] == "lot-a"
        assert len(executor.dispatched) == 1
        assert not stack.client.list_inventory_reservations()
        _inbound(stack.materials, lot_uuid="lot-a", quantity=4)
        scheduler._reconcile_resources()
        wait_for(lambda: len(executor.dispatched) == 2)
        assert executor.dispatched[1]["inventory_requirements"][0]["quantity"] == 5
        executor.finish(1, {})
        wait_for(lambda: stack.workflow.get_workflow_task(task["uuid"])["status"] == "succeeded")
        scheduler._reconcile_inventory()
        assert stack.quantities() == (1, 1, 0)
        assert not scheduler._inventory_recovery.reconcile()["waiting"]
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


def test_waiting_source_survives_reopen_and_new_input_cannot_rebind_it(tmp_path):
    def flow(ctx):
        ctx.run("reactor/operate", {}, inventory_mode="on_ready", inventory=[{"key": "solvent", "kind": "lot",
            "lot_uuid": "lot-a", "quantity": 5, "unit": "ul"}])
    stack = Stack(tmp_path)
    _template(stack.materials, "reagent-template", "reagent")
    _inbound(stack.materials, lot_uuid="lot-a", quantity=1)
    executor = Executor()
    scheduler, task = create(stack, executor, flow)
    wait_for(lambda: bool(scheduler._inventory_recovery.reconcile()["waiting"]))
    job = scheduler._inventory_recovery.snapshot()["waiting"][0]["job_uuid"]
    scheduler.stop()
    stack.close()
    reopened = Stack(tmp_path)
    resumed = BackendScheduler(reopened.workflow, executor, materials_gateway=reopened.client)
    try:
        assert resumed._inventory_recovery.reconcile()["waiting"][0]["job_uuid"] == job
        actual = freeze_inventory(reopened.workflow, resumed._authority_epoch, job,
            lambda: pytest.fail("恢复不得重新求值来源"))
        assert actual[0]["lot_uuid"] == "lot-a"
        resumed.submit(task["uuid"])
        _inbound(reopened.materials, lot_uuid="lot-a", quantity=4)
        resumed._reconcile_resources()
        wait_for(lambda: len(executor.dispatched) == 1)
        executor.finish(0, {})
        wait_for(lambda: reopened.workflow.get_workflow_task(task["uuid"])["status"] == "succeeded")
        resumed._reconcile_inventory()
        assert reopened.quantities() == (0, 0, 0)
    finally:
        resumed.stop()
        reopened.close()
