"""库存只是被其他任务预留（实物够）时排队等待，不算缺料（负责人决定）。

真缺料保持原行为：任务启动时整任务预留不足 → 任务失败 ``plan_not_executable``（stage=preflight）；动作就绪时
才预留的不足 → ``category="preflight"`` 决策。被其他任务占着 → 任务（启动时）或 attempt（就绪时）排队，
不失败、不开决策、不持有任何东西，预留释放 / 结算后重试；重启后照样排队。
"""
from __future__ import annotations

from uuid import uuid4

from tests.server.test_scheduler_inventory_authority import _inbound, _mutation, _template
from tests.server.test_workflow_deferred_inventory import _preflight_decision, create
from tests.server.test_workflow_inventory_recovery import Stack
from tests.server.test_workflow_parallel_execution import Executor, wait_for
from unilabos.protocol.materials import InventoryRequirement, InventoryReservationCreate, InventoryReservationTransition
from unilabos.server.scheduler.service import BackendScheduler


def _start_flow(ctx):
    ctx.run("reactor/operate", {}, inventory=[{"key": "solvent", "kind": "lot", "lot_uuid": "lot-a",
                                               "quantity": 5, "unit": "ul"}])


def _ready_flow(ctx):
    ctx.run("reactor/operate", {}, inventory_mode="on_ready", inventory=[{"key": "solvent", "kind": "lot",
                                                                          "lot_uuid": "lot-a", "quantity": 5, "unit": "ul"}])


def _hold(stack, quantity=5):
    """不属于任何工作流任务的外部预留占着 lot-a。"""
    job = str(uuid4())
    return stack.materials.reserve_inventory(_mutation("reserve_inventory", job_uuid=job), InventoryReservationCreate(
        task_uuid=str(uuid4()), node_uuid=str(uuid4()), job_uuid=job, scheduler_revision=1,
        requirements=[InventoryRequirement(key="solvent", kind="lot", lot_uuid="lot-a", quantity=quantity, unit="ul")],
    )).data


def _release(stack, reservation):
    stack.materials.release_inventory_reservation(
        _mutation("release_inventory_reservation", job_uuid=reservation.job_uuid),
        InventoryReservationTransition(reservation_uuid=reservation.reservation_uuid, reason="test"))


def _jobs(stack, task):
    return stack.workflow.list_workflow_node_jobs(task["uuid"])


def _waiting(stack, task):
    return [job for job in _jobs(stack, task)
            if (job["control_data"].get("wait_reason") or {}).get("code") == "inventory_reserved_by_other_tasks"]


def _another_task(stack, flow):
    """在已经接好调度器的栈上再建一个任务（自动提交给同一个调度器）。"""
    from unilabos.registry.workflows import (DeviceCatalog, WorkflowDefinition, build_workflow_template_payload,
                                             materialize_workflow_template, upsert_workflow)

    catalog = DeviceCatalog()
    catalog.add("reactor", "unknown", str(uuid4()))
    definition = WorkflowDefinition(fn=flow, uuid=str(uuid4()), source_path="test:inventory", display_name="排队",
                                    description="", tags=[])
    record = upsert_workflow(stack.workflow, materialize_workflow_template(build_workflow_template_payload(definition), catalog))
    return stack.workflow.create_workflow_task(workflow_uuid=record["uuid"], run_mode="normal", target_node_uuid=None,
                                               input_value={}, description=None, meta_data={})


def _seed(stack, quantity=5):
    _template(stack.materials, "reagent-template", "reagent")
    _inbound(stack.materials, lot_uuid="lot-a", quantity=quantity)


def test_task_waits_at_start_while_another_task_holds_the_inventory(tmp_path):
    stack, executor = Stack(tmp_path), Executor()
    _seed(stack)
    scheduler, first = create(stack, executor, _start_flow)
    try:
        wait_for(lambda: len(executor.dispatched) == 1)  # 第一个任务预留了全部 5 ul
        second = _another_task(stack, _start_flow)
        wait_for(lambda: _waiting(stack, second))
        task = stack.workflow.get_workflow_task(second["uuid"])
        assert task["status"] == "pending" and task["error_info"] == []  # 不失败，也不算开始
        assert stack.client.list_inventory_reservations(task_uuid=second["uuid"]) == []  # 不持有任何东西
        assert not scheduler._intervals.snapshot(second["uuid"])  # noqa: SLF001
        assert len(executor.dispatched) == 1
        assert "被其他任务预留" in _waiting(stack, second)[0]["control_data"]["wait_reason"]["message"]

        # 第一个任务的动作没开始就失败：它的预留释放，排队的任务随之重试、取得库存并运行
        executor.callback(executor.dispatched[0]["job_id"], False, {}, "normal", {})
        wait_for(lambda: stack.workflow.get_workflow_task(first["uuid"])["status"] == "failed")
        wait_for(lambda: len(executor.dispatched) == 2)
        assert executor.dispatched[1]["task_id"] == second["uuid"]
        assert stack.workflow.get_workflow_task(second["uuid"])["status"] == "running"
        assert not _waiting(stack, second)  # 取得库存后清掉排队原因
        executor.finish(1, {})
        wait_for(lambda: stack.workflow.get_workflow_task(second["uuid"])["status"] == "succeeded")
    finally:
        scheduler.stop()
        stack.close()


def _big_flow(ctx):
    ctx.run("reactor/operate", {}, inventory=[{"key": "solvent", "kind": "lot", "lot_uuid": "lot-a",
                                               "quantity": 10, "unit": "ul"}])


def test_truly_short_inventory_still_fails_the_task_at_start(tmp_path):
    stack, executor = Stack(tmp_path), Executor()
    _seed(stack, quantity=8)
    _hold(stack, quantity=4)  # 别人占着 4，但实物一共只有 8 < 10：真缺料，等也没用
    scheduler, task = create(stack, executor, _big_flow)
    try:
        wait_for(lambda: stack.workflow.get_workflow_task(task["uuid"])["status"] == "failed")
        [error] = stack.workflow.get_workflow_task(task["uuid"])["error_info"]
        assert (error["code"], error["stage"], error["reason"]) == ("plan_not_executable", "preflight", "inventory_shortage")
        assert executor.dispatched == []
    finally:
        scheduler.stop()
        stack.close()


def test_start_wait_survives_restart_and_resumes_when_the_hold_is_released(tmp_path):
    stack, executor = Stack(tmp_path), Executor()
    _seed(stack)
    hold = _hold(stack)
    scheduler, task = create(stack, executor, _start_flow)
    wait_for(lambda: _waiting(stack, task))
    scheduler.stop()
    stack.close()
    reopened, executor2 = Stack(tmp_path), Executor()
    resumed = BackendScheduler(reopened.workflow, executor2, materials_gateway=reopened.client)
    try:
        resumed.start(recover=True)
        wait_for(lambda: task["uuid"] in resumed._inventory_waiting_tasks)  # noqa: SLF001
        assert reopened.workflow.get_workflow_task(task["uuid"])["status"] == "pending"
        assert executor2.dispatched == []
        _release(reopened, hold)
        resumed._retry_inventory_waiting_tasks()  # noqa: SLF001 - 等价于下一轮兜底重算
        wait_for(lambda: len(executor2.dispatched) == 1)
        executor2.finish(0, {})
        wait_for(lambda: reopened.workflow.get_workflow_task(task["uuid"])["status"] == "succeeded")
    finally:
        resumed.stop()
        reopened.close()


def test_ready_attempt_waits_instead_of_opening_a_preflight_decision(tmp_path):
    stack, executor = Stack(tmp_path), Executor()
    _seed(stack)
    hold = _hold(stack)
    scheduler, task = create(stack, executor, _ready_flow)
    try:
        wait_for(lambda: _waiting(stack, task))
        [job] = _waiting(stack, task)
        assert job["status"] == "pending" and job["control_data"]["inventory_wait"]
        assert _preflight_decision(scheduler, task["uuid"]) is None  # 不开预检决策
        assert stack.workflow.get_workflow_task(task["uuid"])["status"] == "running"
        assert stack.client.list_inventory_reservations(task_uuid=task["uuid"]) == []
        assert not scheduler._intervals.snapshot(task["uuid"])  # noqa: SLF001 - 不占设备
        _release(stack, hold)
        scheduler.resume_pending_dispatches()
        wait_for(lambda: len(executor.dispatched) == 1)
        assert not _waiting(stack, task)
        executor.finish(0, {})
        wait_for(lambda: stack.workflow.get_workflow_task(task["uuid"])["status"] == "succeeded")
    finally:
        scheduler.stop()
        stack.close()


def test_ready_wait_survives_restart(tmp_path):
    stack, executor = Stack(tmp_path), Executor()
    _seed(stack)
    hold = _hold(stack)
    scheduler, task = create(stack, executor, _ready_flow)
    wait_for(lambda: _waiting(stack, task))
    scheduler.stop()
    stack.close()
    reopened, executor2 = Stack(tmp_path), Executor()
    resumed = BackendScheduler(reopened.workflow, executor2, materials_gateway=reopened.client)
    try:
        resumed.submit(task["uuid"])
        wait_for(lambda: resumed._condition_waits)  # noqa: SLF001
        assert _preflight_decision(resumed, task["uuid"]) is None and executor2.dispatched == []
        _release(reopened, hold)
        resumed.resume_pending_dispatches()
        wait_for(lambda: len(executor2.dispatched) == 1)
        executor2.finish(0, {})
        wait_for(lambda: reopened.workflow.get_workflow_task(task["uuid"])["status"] == "succeeded")
    finally:
        resumed.stop()
        reopened.close()


def test_preview_tells_reserved_from_insufficient(tmp_path):
    stack = Stack(tmp_path)
    try:
        _seed(stack, quantity=8)
        _hold(stack, quantity=6)

        def cause(quantity):
            report = stack.materials.preview_inventory([InventoryRequirement(
                key="solvent", kind="lot", template_uuid="reagent-template", quantity=quantity, unit="ul")])
            return [item["cause"] for item in report["shortages"]]

        assert cause(2) == []
        assert cause(5) == ["reserved_by_other_tasks"]  # 实物 8 够，被占 6
        assert cause(9) == ["insufficient"]
    finally:
        stack.close()


def _loop_flow(ctx):
    with ctx.loop_for(2):
        ctx.run("reactor/operate", {}, inventory=[{"key": "solvent", "kind": "lot", "lot_uuid": "lot-a",
                                                   "quantity": 5, "unit": "ul"}])


def test_next_loop_round_waits_while_another_task_holds_the_inventory(tmp_path):
    """循环下一轮重新预留时被其他任务占着：这一轮的循环体排队等释放，循环不失败。"""
    stack, executor = Stack(tmp_path), Executor()
    _seed(stack, quantity=10)
    scheduler, task = create(stack, executor, _loop_flow)
    try:
        wait_for(lambda: len(executor.dispatched) == 1)  # 第一轮在任务启动时已预留 5
        hold = _hold(stack)  # 剩下的 5 被别人占着
        executor.finish(0, {})
        wait_for(lambda: _waiting(stack, task))
        assert len(executor.dispatched) == 1
        assert stack.workflow.get_workflow_task(task["uuid"])["status"] == "running"
        assert _preflight_decision(scheduler, task["uuid"]) is None
        _release(stack, hold)
        scheduler.resume_pending_dispatches()
        wait_for(lambda: len(executor.dispatched) == 2)
        executor.finish(1, {})
        wait_for(lambda: stack.workflow.get_workflow_task(task["uuid"])["status"] == "succeeded")
        scheduler._reconcile_inventory()  # noqa: SLF001
        assert stack.quantities() == (0, 0, 0)
    finally:
        scheduler.stop()
        stack.close()
