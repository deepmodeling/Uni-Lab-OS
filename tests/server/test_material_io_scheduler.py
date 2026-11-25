"""调度器本地执行人工装入 / 取走 / 补量节点：锁定库位与物料、等人工确认、确认后一次库存命令。

不派发到 Host：执行器始终收不到这一步；锁随 attempt 持久，重启后接管；库存命令身份由 attempt 决定，
确认后、写库前后崩溃都只写一次。
"""

from __future__ import annotations

import textwrap
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

from tests.server.test_material_io_inventory import _ensure_template, container, deck
from tests.server.test_workflow_inventory_recovery import Stack
from tests.server.test_workflow_parallel_execution import Executor, wait_for
from unilabos.protocol.materials import MaterialMove
from unilabos.registry.workflow_ast import compile_workflow_source
from unilabos.registry.workflows import (
    DeviceCatalog,
    build_workflow_template_payload,
    materialize_workflow_template,
    upsert_workflow,
)
from unilabos.server.scheduler.service import BackendScheduler
from tests.server.test_scheduler_inventory_authority import _mutation


def _stack(tmp_path) -> Stack:
    stack = Stack(tmp_path)
    _ensure_template(stack.materials, "tip-box-template", "tip_box")
    return stack


def _scheduler(stack: Stack, executor: Executor) -> BackendScheduler:
    # robot/place 把 plate 参数当物料锁（与默认物料锁同一路径）
    scheduler = BackendScheduler(stack.workflow, executor, materials_gateway=stack.client,
                                 materials_need_lock_resolver=lambda _device, _action: ["plate"])
    stack.workflow.set_task_submitter(scheduler.submit)
    return scheduler


def _task(stack: Stack, body: str) -> dict:
    program = compile_workflow_source("def flow(ctx):\n" + textwrap.indent(textwrap.dedent(body), "    "))
    definition = SimpleNamespace(collect=lambda: program.ctx, source_path="test:material_io", uuid=str(uuid4()),
                                 display_name="人工物料", description="", tags=[], guide=None)
    catalog = DeviceCatalog()
    catalog.add("robot", "arm", str(uuid4()))
    record = upsert_workflow(stack.workflow, materialize_workflow_template(build_workflow_template_payload(definition),
                                                                           catalog))
    return stack.workflow.create_workflow_task(workflow_uuid=record["uuid"], run_mode="normal", target_node_uuid=None,
                                               input_value={}, description=None, meta_data={})


def _material_io_jobs(stack: Stack, task: dict) -> list[dict]:
    runs = {run["uuid"]: run for run in stack.workflow.list_workflow_node_runs(task["uuid"])}
    return [job for job in stack.workflow.list_workflow_node_jobs(task["uuid"])
            if runs[job["workflow_node_run_uuid"]]["executor_kind"] == "material_io"]


def _confirmation(stack: Stack, task: dict, index: int = 0):
    jobs = _material_io_jobs(stack, task)
    return stack.workflow.get_manual_confirmation_for_job(jobs[index]["uuid"]) if len(jobs) > index else None


def _pending(stack: Stack, task: dict, index: int = 0) -> dict:
    wait_for(lambda: (_confirmation(stack, task, index) or {}).get("status") == "pending")
    return _confirmation(stack, task, index)


def _decide(stack: Stack, confirmation: dict, action: str = "approve") -> None:
    stack.workflow.decide_workflow_manual_confirmation(confirmation["uuid"], action=action, confirmed_by="operator",
                                                       decision_idempotency_key=str(uuid4()))


def _status(stack: Stack, task: dict) -> str:
    return stack.workflow.get_workflow_task(task["uuid"])["status"]


def _tip_boxes(stack: Stack) -> list:
    return [item for item in stack.materials.list_materials() if item.material.template_name == "tip_box"]


def test_load_waits_for_confirmation_then_places_the_material_and_feeds_the_next_step(tmp_path) -> None:
    stack, executor = _stack(tmp_path), Executor()
    mount, sites = deck(stack.materials)
    scheduler = _scheduler(stack, executor)
    try:
        task = _task(stack, f'''
            loaded = ctx.material_io(mode="load", mount="{mount}", site="A1", resource_template="tip_box",
                                     material_name="新吸头盒", assignee_user_ids=["alice"])
            ctx.run("robot/pick", {{"box": loaded.material}})
        ''')
        confirmation = _pending(stack, task)
        [job] = _material_io_jobs(stack, task)
        assert "人工装入" in confirmation["description"] and confirmation["assignee_user_ids"] == ["alice"]
        assert confirmation["param"]["site_uuid"] == sites["A1"] and confirmation["meta_data"]["executor_kind"] == "material_io"
        # 确认前不改库存、不派发；attempt 持有挂载物料的锁
        assert executor.dispatched == [] and _tip_boxes(stack) == []
        assert job["status"] == "pending" and job["control_data"]["material_io"]["plan"]["lock_material_uuids"] == [mount]
        assert scheduler.resources.held_by(job["uuid"])
        assert scheduler.active_device_job_ids() == []  # 不阻止 runtime 排空

        stack.workflow.decide_workflow_manual_confirmation(confirmation["uuid"], action="approve", confirmed_by="alice",
                                                           decision_idempotency_key="load-1")
        wait_for(lambda: len(executor.dispatched) == 1)
        [box] = _tip_boxes(stack)
        new = box.material.material_uuid
        assert box.material.display_name == "新吸头盒" and stack.materials.occupied_site(new).site_uuid == sites["A1"]
        # 节点输出 material，后面的动作像引用动作结果一样拿到它
        assert executor.dispatched[0]["action"] == "pick" and executor.dispatched[0]["action_args"]["box"] == {"uuid": new}
        run = next(item for item in stack.workflow.list_workflow_node_runs(task["uuid"])
                   if item["executor_kind"] == "material_io")
        assert run["status"] == "succeeded" and run["return_info"]["return_value"]["material"] == {"uuid": new}
        assert run["return_info"]["return_value"]["confirmation"]["confirmed_by"] == "alice"
        assert not scheduler.resources.held_by(job["uuid"])
        executor.finish(0, {})
        wait_for(lambda: _status(stack, task) == "succeeded")
    finally:
        scheduler.stop()
        stack.close()


def test_refill_then_unload_after_approval(tmp_path) -> None:
    stack, executor = _stack(tmp_path), Executor()
    mount, sites = deck(stack.materials)
    bottle = container(stack.materials, mount, sites["B1"], water_ml=20)
    scheduler = _scheduler(stack, executor)
    try:
        task = _task(stack, f'''
            refilled = ctx.material_io(mode="refill", mount="{mount}", site="B1", material="{bottle}",
                                       reagent={{"quantity": 5, "quantity_unit": "mL"}})
            ctx.material_io(mode="unload", mount="{mount}", site="B1", material=refilled.material,
                            disposition="soft_delete", scope="material_with_contents")
        ''')
        refill = _pending(stack, task)
        assert "人工补量" in refill["description"]
        _decide(stack, refill)
        unload = _pending(stack, task, 1)
        water = stack.materials.get_material(bottle).data.substances
        assert [(item.quantity, item.quantity_unit) for item in water] == [(25, "mL")]
        assert stack.materials.occupied_site(bottle).site_uuid == sites["B1"]  # 补量不换位置
        assert "人工取走" in unload["description"] and unload["param"]["material_uuid"] == bottle
        _decide(stack, unload)
        wait_for(lambda: _status(stack, task) == "succeeded")
        outputs = [run["return_info"]["return_value"] for run in sorted(
            stack.workflow.list_workflow_node_runs(task["uuid"]), key=lambda run: run["topological_index"])]
        assert (outputs[0]["quantity_added"], outputs[0]["quantity"], outputs[0]["material"]) == (5, 25, {"uuid": bottle})
        assert outputs[1]["affected_material_uuids"] == [bottle] and outputs[1]["disposition"] == "soft_delete"
        assert stack.materials.get_site(sites["B1"]).occupied_material_uuid is None
        assert bottle not in {item.material.material_uuid for item in stack.materials.list_materials()}
        assert executor.dispatched == []  # 整个任务没有一步交给 Host
    finally:
        scheduler.stop()
        stack.close()


def test_reject_and_timeout_leave_the_inventory_as_it_was(tmp_path) -> None:
    stack, executor = _stack(tmp_path), Executor()
    mount, sites = deck(stack.materials)
    scheduler = _scheduler(stack, executor)
    body = f'ctx.material_io(mode="load", mount="{mount}", site="A1", resource_template="tip_box", timeout_seconds=60)'
    try:
        # 拒绝按取消处理：步骤 canceled（保留拒绝原因），任务 canceled；超时仍是 timeout，任务 failed
        rejected = _task(stack, body)
        _decide(stack, _pending(stack, rejected), "reject")
        wait_for(lambda: _status(stack, rejected) == "canceled")
        [job] = _material_io_jobs(stack, rejected)
        assert job["status"] == "canceled" and job["error_info"] == [{"code": "manual_confirmation_rejected"}]
        assert stack.workflow.get_workflow_task(rejected["uuid"])["error_info"][0]["code"] == "manual_confirmation_rejected"
        assert _tip_boxes(stack) == [] and not scheduler.resources.held_by(job["uuid"])

        timed_out = _task(stack, body)
        _pending(stack, timed_out)
        future = (datetime.now(timezone.utc) + timedelta(seconds=120)).isoformat().replace("+00:00", "Z")
        stack.workflow.expire_workflow_manual_confirmations(now=future)
        wait_for(lambda: _status(stack, timed_out) == "failed")
        [job] = _material_io_jobs(stack, timed_out)
        assert job["status"] == "timeout" and job["error_info"] == [{"code": "manual_confirmation_timed_out"}]
        assert _tip_boxes(stack) == [] and stack.materials.get_site(sites["A1"]).occupied_material_uuid is None
        assert not scheduler.resources.held_by(job["uuid"])
    finally:
        scheduler.stop()
        stack.close()


def test_another_task_using_the_site_queues_until_the_step_ends(tmp_path) -> None:
    stack, executor = _stack(tmp_path), Executor()
    mount, sites = deck(stack.materials)
    scheduler = _scheduler(stack, executor)
    try:
        loading = _task(stack, f'ctx.material_io(mode="load", mount="{mount}", site="A1", resource_template="tip_box")')
        confirmation = _pending(stack, loading)
        placing = _task(stack, f'ctx.run("robot/place", {{"plate": {{"uuid": "{mount}"}}, "site": "B1"}})')
        [job] = stack.workflow.list_workflow_node_jobs(placing["uuid"])
        wait_for(lambda: job["uuid"] in scheduler._waiting_interval_jobs)  # noqa: SLF001 - 排在库位锁后面
        scheduler.resume_pending_dispatches()
        assert executor.dispatched == [] and stack.workflow.get_workflow_node_job(job["uuid"])["status"] == "pending"
        assert _status(stack, placing) == "running"  # 等待而不是失败

        _decide(stack, confirmation)
        wait_for(lambda: _status(stack, loading) == "succeeded")
        wait_for(lambda: len(executor.dispatched) == 1)
        assert executor.dispatched[0]["task_id"] == placing["uuid"]
        executor.finish(0, {})
        wait_for(lambda: _status(stack, placing) == "succeeded")
    finally:
        scheduler.stop()
        stack.close()


def test_load_onto_an_occupied_site_waits_before_and_fails_after_confirmation(tmp_path) -> None:
    stack, executor = _stack(tmp_path), Executor()
    mount, sites = deck(stack.materials)
    blocker = container(stack.materials, mount, sites["A1"])
    scheduler = _scheduler(stack, executor)
    body = f'ctx.material_io(mode="load", mount="{mount}", site="A1", resource_template="tip_box")'
    try:
        waiting = _task(stack, body)
        wait_for(lambda: (_material_io_jobs(stack, waiting)[0]["control_data"].get("wait_reason") or {}).get("code"))
        [job] = _material_io_jobs(stack, waiting)
        # 确认前库位被占：不开确认单、不持有任何锁，排队等库位空出来
        assert job["control_data"]["wait_reason"]["code"] == "site_occupied" and _confirmation(stack, waiting) is None
        assert not scheduler.resources.held_by(job["uuid"]) and _status(stack, waiting) == "running"
        stack.materials.move_material(_mutation("move_material"),
                                      MaterialMove(material_uuid=blocker, destination_site_uuid=sites["B1"]))
        scheduler.resume_pending_dispatches()
        confirmation = _pending(stack, waiting)
        assert "wait_reason" not in _material_io_jobs(stack, waiting)[0]["control_data"]

        # 确认期间有人手工把物料放进了库位：批准后写库被拒，节点失败带物料错误码，不覆盖原物料
        intruder = container(stack.materials, mount, sites["A1"])
        _decide(stack, confirmation)
        wait_for(lambda: _status(stack, waiting) == "failed")
        [job] = _material_io_jobs(stack, waiting)
        [error] = job["error_info"]
        assert (error["code"], error["material_error_code"], error["business_code"]) == (
            "material_io_rejected", "conflict:site_occupied", 6007)
        assert stack.materials.occupied_site(intruder).site_uuid == sites["A1"] and _tip_boxes(stack) == []
        assert executor.dispatched == []
    finally:
        scheduler.stop()
        stack.close()


def test_contract_errors_fail_the_step_before_asking_anyone(tmp_path) -> None:
    stack, executor = _stack(tmp_path), Executor()
    mount, _sites = deck(stack.materials)
    scheduler = _scheduler(stack, executor)
    try:
        task = _task(stack, f'ctx.material_io(mode="load", mount="{mount}", site="Z9", resource_template="tip_box")')
        wait_for(lambda: _status(stack, task) == "failed")
        [job] = _material_io_jobs(stack, task)
        [error] = job["error_info"]
        assert error["code"] == "material_io_invalid" and error["material_error_code"] == "not_found:site"
        assert _confirmation(stack, task) is None
    finally:
        scheduler.stop()
        stack.close()


def test_cancel_closes_the_pending_confirmation(tmp_path) -> None:
    stack, executor = _stack(tmp_path), Executor()
    mount, sites = deck(stack.materials)
    scheduler = _scheduler(stack, executor)
    try:
        task = _task(stack, f'ctx.material_io(mode="load", mount="{mount}", site="A1", resource_template="tip_box")')
        confirmation = _pending(stack, task)
        stack.workflow.command_workflow_task(task["uuid"], command_type="cancel", idempotency_key=str(uuid4()))
        wait_for(lambda: _status(stack, task) == "canceled")
        assert stack.workflow.get_workflow_manual_confirmation(confirmation["uuid"])["status"] == "canceled"
        [job] = _material_io_jobs(stack, task)
        assert job["status"] == "canceled" and not scheduler.resources.held_by(job["uuid"])
        assert _tip_boxes(stack) == [] and executor.dispatched == []
    finally:
        scheduler.stop()
        stack.close()


def _restart(stack: Stack, scheduler: BackendScheduler, tmp_path) -> tuple[Stack, Executor, BackendScheduler]:
    scheduler.stop()
    stack.close()
    reopened, executor = Stack(tmp_path), Executor()
    resumed = _scheduler(reopened, executor)
    resumed.start(recover=True)
    return reopened, executor, resumed


def test_restart_with_a_pending_confirmation_keeps_the_lock_and_converges_once(tmp_path) -> None:
    stack, executor = _stack(tmp_path), Executor()
    mount, sites = deck(stack.materials)
    scheduler = _scheduler(stack, executor)
    task = _task(stack, f'ctx.material_io(mode="load", mount="{mount}", site="A1", resource_template="tip_box")')
    confirmation = _pending(stack, task)
    [job] = _material_io_jobs(stack, task)
    stack, executor, scheduler = _restart(stack, scheduler, tmp_path)
    try:
        # 持久 Claim 先于任何任务恢复：重启后锁仍在，另一个任务的动作照样排队
        assert scheduler.resources.held_by(job["uuid"])
        placing = _task(stack, f'ctx.run("robot/place", {{"plate": {{"uuid": "{mount}"}}}})')
        [other] = stack.workflow.list_workflow_node_jobs(placing["uuid"])
        wait_for(lambda: other["uuid"] in scheduler._waiting_interval_jobs)  # noqa: SLF001
        assert _confirmation(stack, task)["uuid"] == confirmation["uuid"]  # 同一张确认单，不重开
        _decide(stack, confirmation)
        wait_for(lambda: _status(stack, task) == "succeeded")
        assert len(_tip_boxes(stack)) == 1
        wait_for(lambda: len(executor.dispatched) == 1)
        executor.finish(0, {})
        wait_for(lambda: _status(stack, placing) == "succeeded")
    finally:
        scheduler.stop()
        stack.close()


def _flaky(stack: Stack, *, after_write: bool) -> list[str]:
    """库存命令第一次失败：写库前抛错（命令没到权威），或写库后抛错（应答丢失）。"""

    calls: list[str] = []
    real = stack.client.inventory_command

    def flaky(command):
        calls.append(command["command_id"])
        if len(calls) > 1:
            return real(command)
        if after_write:
            real(command)
        raise ConnectionError("物料权威暂时不可达")

    stack.client.inventory_command = flaky
    return calls


def test_restart_after_approval_writes_exactly_once(tmp_path) -> None:
    for after_write in (False, True):
        root = tmp_path / ("after" if after_write else "before")
        root.mkdir()
        stack, executor = _stack(root), Executor()
        mount, sites = deck(stack.materials)
        scheduler = _scheduler(stack, executor)
        calls = _flaky(stack, after_write=after_write)
        task = _task(stack, f'ctx.material_io(mode="load", mount="{mount}", site="A1", resource_template="tip_box")')
        _decide(stack, _pending(stack, task))
        wait_for(lambda: calls)
        [job] = _material_io_jobs(stack, task)
        # 决策已持久、库存写入没确认：attempt 仍是 pending，锁不放
        assert job["status"] == "pending" and job["uuid"] in scheduler._material_io_retry  # noqa: SLF001
        assert len(_tip_boxes(stack)) == (1 if after_write else 0)
        stack, executor, scheduler = _restart(stack, scheduler, root)
        try:
            wait_for(lambda: _status(stack, task) == "succeeded")
            [box] = _tip_boxes(stack)  # 重放同一命令：只有一件
            assert stack.materials.occupied_site(box.material.material_uuid).site_uuid == sites["A1"]
            [job] = _material_io_jobs(stack, task)
            assert job["return_info"]["return_value"]["material_uuid"] == box.material.material_uuid
            replays = stack.materials.connection.execute(
                "SELECT COUNT(*) FROM inventory_processed_command WHERE command_type='material.io'").fetchone()[0]
            assert replays == 1
        finally:
            scheduler.stop()
            stack.close()


def test_a_transient_write_failure_is_retried_without_a_restart(tmp_path) -> None:
    stack, executor = _stack(tmp_path), Executor()
    mount, _sites = deck(stack.materials)
    scheduler = _scheduler(stack, executor)
    calls = _flaky(stack, after_write=False)
    try:
        task = _task(stack, f'ctx.material_io(mode="load", mount="{mount}", site="A1", resource_template="tip_box")')
        _decide(stack, _pending(stack, task))
        wait_for(lambda: calls)
        assert _status(stack, task) == "running"
        scheduler.resume_pending_dispatches()  # 等价于 5 秒兜底重算
        wait_for(lambda: _status(stack, task) == "succeeded")
        assert len(calls) == 2 and calls[0] == calls[1]  # 同一命令身份
        assert len(_tip_boxes(stack)) == 1
    finally:
        scheduler.stop()
        stack.close()
