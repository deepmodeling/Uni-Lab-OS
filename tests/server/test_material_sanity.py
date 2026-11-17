"""派发前物料健全性检查：节点就绪、申请资源之前确认引用的物料此刻仍能用（负责人 2026-11-17）。

物料没了或不能用、但操作员能处理（恢复、替换、挪回原位）→ 动作执行前预检决策（``material_sanity``）；
会自己解除的（被另一作业未结算的物理动作占着、被其他任务的有效预留占着）→ 照已有资源条件排队；
参数引用了从未登记的物料这类合同错误照常失败。没有物料参数的节点不做这次读取。
"""
from __future__ import annotations

import json
from uuid import uuid4

import pytest

from tests.server.test_scheduler_inventory_authority import _inbound, _material, _mutation, _template
from tests.server.test_workflow_deferred_inventory import _preflight_decision
from tests.server.test_workflow_inventory_recovery import Stack
from tests.server.test_workflow_parallel_execution import Executor, wait_for
from unilabos.protocol.material_execution import MaterialExecutionPrepare, MaterialExecutionRelease, PhysicalAliquot
from unilabos.protocol.materials import (
    InventoryRequirement,
    InventoryReservationCreate,
    InventoryReservationTransition,
    MaterialDelete,
    MaterialIdentityWrite,
    MaterialMove,
    MaterialNodeCreate,
    MaterialPatch,
    MaterialTreeCreate,
    ResourceTemplateWrite,
    SiteCreate,
)
from unilabos.registry.workflows import upsert_workflow
from unilabos.server.backend.scheduler.material_sanity import MaterialSanity, material_references
from unilabos.server.backend.scheduler.resource_wait_policy import ResourceConditionWait
from unilabos.server.backend.scheduler.service import BackendScheduler
from unilabos.server.services.materials.store import MaterialsRepository

_LOCK = {"type": "object", "properties": {"uuid": {"type": "string"}}, "required": ["uuid"],
         "x-unilabos-material-lock": True}


def _schema(goal, contract=None):
    return {
        "x-unilabos-action-contract": {"version": 2, **({"resource_contract": contract} if contract else {})},
        "properties": {
            "goal": {"type": "object", "properties": goal, "required": sorted(goal)},
            "result": {"type": "object", "properties": {}},
        },
    }


_PROCESS = _schema({"plate": _LOCK})
_PROCESS_TWO = _schema({"plate": _LOCK, "tube": _LOCK})
_MOVE = _schema(
    {"sample": _LOCK, "source": _LOCK, "source_site": {"type": "string"}, "target": _LOCK,
     "target_site": {"type": "string"}},
    {"version": 1, "transfer": {
        "material_param": "sample", "source_owner_param": "source", "source_site_name_param": "source_site",
        "target_owner_param": "target", "target_site_name_param": "target_site", "gripper_site_role": "robot.gripper"}},
)


def _node(name, action, device, param=None, *, after=None, meta=None):
    node = {"uuid": str(uuid4()), "name": name, "type": "device_action", "material_uuid": str(uuid4()),
            "action_name": action, "meta_data": {"target_device_id": device, **(meta or {})}, "param": param or {}}
    if after is not None:
        node["execution_policy"] = {"depends_on": [after["uuid"]]}
    return node


def _run(stack, executor, nodes, schemas):
    stack.workflow.configure_preflight(materials=stack.client,
                                       action_schema_resolver=lambda _device, action: schemas.get(action))
    scheduler = BackendScheduler(stack.workflow, executor, materials_gateway=stack.client)
    stack.workflow.set_task_submitter(scheduler.submit)
    record = upsert_workflow(stack.workflow, {"workflow_uuid": str(uuid4()), "name": "物料健全性", "description": "",
                                              "tags": [], "edges": [], "nodes": nodes})
    task = stack.workflow.create_workflow_task(workflow_uuid=record["uuid"], run_mode="normal", target_node_uuid=None,
                                               input_value={}, description=None, meta_data={})
    return scheduler, task


def _job(stack, task, node):
    return next(job for job in stack.workflow.list_workflow_node_jobs(task["uuid"])
                if job["workflow_node_uuid"] == node["uuid"])


def _status(stack, task):
    return stack.workflow.get_workflow_task(task["uuid"])["status"]


def _containers(stack, count):
    _template(stack.materials, "container-template", "container")
    return [_material(stack.materials, "container") for _ in range(count)]


def _lose(stack, material_uuid, how):
    if how == "deleted":
        stack.materials.delete_material(_mutation("delete_material"), MaterialDelete(material_uuid=material_uuid))
    else:
        stack.materials.patch_material(_mutation("patch_material"), material_uuid,
                                       MaterialPatch(lifecycle_status=how))


def _restore(stack, material_uuid, how):
    if how == "deleted":
        # 物料权威没有撤销删除的接口：直接把记录恢复为在用，代表操作员把同一件物料重新登记回来
        record = MaterialsRepository.get_material(stack.materials, material_uuid, include_deleted=True)
        with stack.materials.write():
            stack.materials.update_material(record.model_copy(update={
                "deleted_at_ms": None, "lifecycle_status": "active", "version": record.version + 1}))
    else:
        stack.materials.patch_material(_mutation("patch_material"), material_uuid,
                                       MaterialPatch(lifecycle_status="active"))


@pytest.mark.parametrize(("how", "reason"), [("deleted", "deleted"), ("quarantined", "unusable"),
                                             ("consumed", "unusable"), ("retired", "unusable")])
def test_material_gone_after_planning_is_a_preflight_decision_and_retry_after_restore_runs(tmp_path, how, reason):
    stack, executor = Stack(tmp_path), Executor()
    [plate] = _containers(stack, 1)
    first = _node("准备", "prepare", "bench")
    second = _node("处理", "process", "robot", {"plate": {"uuid": plate}}, after=first)
    scheduler, task = _run(stack, executor, [first, second], {"process": _PROCESS})
    try:
        wait_for(lambda: len(executor.dispatched) == 1)
        _lose(stack, plate, how)  # 规划之后、节点就绪之前物料没了
        executor.finish(0, {})
        wait_for(lambda: _preflight_decision(scheduler, task["uuid"]) is not None)
        decision = _preflight_decision(scheduler, task["uuid"])
        assert (decision["category"], decision["stage"]) == ("preflight", "preflight")
        assert [option["action"] for option in decision["options"]] == ["retry", "skip", "operator_intervention", "abort"]
        [error] = decision["error_info"]
        assert (error["code"], error["stage"], error["scope"]) == ("material_sanity", "preflight", "action")
        [problem] = error["materials"]
        assert (problem["reason"], problem["material_uuid"], problem["sources"]) == (reason, plate, ["plate"])
        assert len(executor.dispatched) == 1  # 没有派发，任务也没失败
        assert _job(stack, task, second)["status"] == "intervention_required"
        assert _status(stack, task) == "running"
        assert not scheduler.resources.held_by(decision["job_id"])

        # 还没恢复就重试：同一 attempt 重新预检，仍挂起
        assert scheduler.resolve_reconciliation_decision(decision["decision_id"], {"action": "retry", "reason": "再查"})
        wait_for(lambda: _preflight_decision(scheduler, task["uuid"]) is not None)
        assert len(executor.dispatched) == 1

        _restore(stack, plate, how)
        decision = _preflight_decision(scheduler, task["uuid"])
        assert scheduler.resolve_reconciliation_decision(decision["decision_id"], {"action": "retry", "reason": "已恢复"})
        wait_for(lambda: len(executor.dispatched) == 2)
        assert executor.dispatched[1]["action_args"]["plate"]["uuid"] == plate
        assert executor.dispatched[1]["attempt_no"] == 1  # 预检重试不消耗 attempt
        executor.finish(1, {})
        wait_for(lambda: _status(stack, task) == "succeeded")
    finally:
        scheduler.stop()
        stack.close()


def test_abandoning_a_material_sanity_decision_fails_the_task_as_preflight(tmp_path):
    stack, executor = Stack(tmp_path), Executor()
    [plate] = _containers(stack, 1)
    _lose(stack, plate, "deleted")
    node = _node("处理", "process", "robot", {"plate": {"uuid": plate}})
    scheduler, task = _run(stack, executor, [node], {"process": _PROCESS})
    try:
        wait_for(lambda: _preflight_decision(scheduler, task["uuid"]) is not None)
        decision = _preflight_decision(scheduler, task["uuid"])
        assert scheduler.resolve_reconciliation_decision(decision["decision_id"], {"action": "abort", "reason": "不做了"})
        wait_for(lambda: _status(stack, task) == "failed")
        assert stack.workflow.get_workflow_task(task["uuid"])["error_info"] == [
            {"code": "node_preflight_failed", "stage": "preflight", "scope": "action"}]
        [error] = _job(stack, task, node)["error_info"]
        assert (error["code"], error["materials"][0]["reason"]) == ("material_sanity", "deleted")
        assert executor.dispatched == []
    finally:
        scheduler.stop()
        stack.close()


def _identity(name, template="container", resource_type="container", class_name="Container"):
    return MaterialIdentityWrite(resource_id=f"{name}-{uuid4()}", name=f"{name}-{uuid4()}", template_name=template,
                                 resource_type=resource_type, class_name=class_name)


def _rack(stack, labels):
    tree = stack.materials.create_tree(_mutation("create_material_tree"), MaterialTreeCreate(nodes=[
        MaterialNodeCreate(client_ref="rack", identity=_identity("rack"), sites=[
            SiteCreate(template_name="slot", site_index=index, label=label) for index, label in enumerate(labels)])]))
    rack = tree.data.client_ref_map["rack"]
    return rack, {site.label: site.site_uuid for site in stack.materials.get_material(rack).sites}


def _arm(stack):
    stack.materials.put_template(_mutation("put_template"), ResourceTemplateWrite(
        name="arm", display_name="arm", resource_type="device", class_name="arm"))
    stack.materials.create_tree(_mutation("create_material_tree"), MaterialTreeCreate(nodes=[MaterialNodeCreate(
        client_ref="arm", identity=MaterialIdentityWrite(resource_id="arm", name="arm", resource_type="device",
                                                         class_name="arm", template_name="arm"),
        sites=[SiteCreate(template_name="arm", site_index=0, label="grip", meta_data={"role": "robot.gripper"})])]))


def _move(stack, material_uuid, site_uuid):
    stack.materials.move_material(_mutation("move_material"),
                                  MaterialMove(material_uuid=material_uuid, destination_site_uuid=site_uuid))


def test_material_moved_away_from_the_declared_source_site_is_a_preflight_decision(tmp_path):
    """搬运合同声明了来源库位，样品规划后被挪到别处、也没有作业在搬它：不会自己回来，预检报错；挪回后重试即派发。"""
    stack, executor = Stack(tmp_path), Executor()
    [sample] = _containers(stack, 1)
    _arm(stack)
    rack, sites = _rack(stack, ["A", "B"])
    target, _target_sites = _rack(stack, ["T"])
    _move(stack, sample, sites["A"])
    first = _node("准备", "prepare", "bench")
    second = _node("搬运", "move_sample", "arm", {"sample": {"uuid": sample}, "source": {"uuid": rack}, "source_site": "A",
                                                "target": {"uuid": target}, "target_site": "T"}, after=first)
    scheduler, task = _run(stack, executor, [first, second], {"move_sample": _MOVE})
    try:
        wait_for(lambda: len(executor.dispatched) == 1)
        _move(stack, sample, sites["B"])  # 规划之后被人挪到了 B
        executor.finish(0, {})
        wait_for(lambda: _preflight_decision(scheduler, task["uuid"]) is not None)
        decision = _preflight_decision(scheduler, task["uuid"])
        [error] = decision["error_info"]
        assert error["code"] == "material_sanity"
        [problem] = error["materials"]
        assert (problem["reason"], problem["material_uuid"], problem["sources"]) == ("moved", sample, ["sample"])
        assert problem["details"]["site_uuid"] == sites["B"]
        assert "wait_reason" not in _job(stack, task, second)["control_data"]
        assert len(executor.dispatched) == 1

        _move(stack, sample, sites["A"])  # 操作员挪回原位
        assert scheduler.resolve_reconciliation_decision(decision["decision_id"], {"action": "retry", "reason": "已挪回"})
        wait_for(lambda: len(executor.dispatched) == 2)
        [move] = stack.client.get_material_execution(executor.dispatched[1]["job_id"])["request"]["moves"]
        assert (move["material_uuid"], move["source_site_uuid"]) == (sample, sites["A"])
    finally:
        scheduler.stop()
        stack.close()


def _hold(stack, material_uuid, other_uuid):
    """不属于本任务的一个作业准备了物理动作（分装）且尚未结算：物料被它占着。"""
    job_uuid = str(uuid4())
    return stack.materials.executions.prepare(MaterialExecutionPrepare(
        job_uuid=job_uuid, task_uuid=str(uuid4()), claim_uuid=str(uuid4()), attempt_no=1,
        parameter_hash="sha256:test",
        fences={json.dumps(["material", item], separators=(",", ":")): 1 for item in (material_uuid, other_uuid)},
        aliquot=PhysicalAliquot(source_material_uuid=material_uuid, target_material_uuids=[other_uuid]),
    ))


def test_material_held_by_another_unsettled_physical_action_waits_without_holding_anything(tmp_path):
    stack, executor = Stack(tmp_path), Executor()
    plate, other = _containers(stack, 2)
    record = _hold(stack, plate, other)
    node = _node("处理", "process", "robot", {"plate": {"uuid": plate}})
    scheduler, task = _run(stack, executor, [node], {"process": _PROCESS})
    try:
        wait_for(lambda: _job(stack, task, node)["control_data"].get("wait_reason"))
        reason = _job(stack, task, node)["control_data"]["wait_reason"]
        assert reason["code"] == "site_claimed" and record["job_uuid"] in reason["message"]
        job = _job(stack, task, node)
        assert job["status"] == "pending" and executor.dispatched == []
        assert not scheduler.list_reconciliation_decisions()
        assert not scheduler.resources.held_by(job["uuid"])
        scheduler.resume_pending_dispatches()  # 仍被占着：继续等
        assert executor.dispatched == [] and _job(stack, task, node)["status"] == "pending"

        stack.materials.executions.release(record["job_uuid"], MaterialExecutionRelease(
            expected_version=record["version"], reason="现场确认没有开始", outcome="not_started"))
        scheduler.resume_pending_dispatches()
        wait_for(lambda: len(executor.dispatched) == 1)
        assert "wait_reason" not in _job(stack, task, node)["control_data"]
        executor.finish(0, {})
        wait_for(lambda: _status(stack, task) == "succeeded")
    finally:
        scheduler.stop()
        stack.close()


def test_material_reserved_by_another_task_waits_until_that_reservation_is_released(tmp_path):
    stack, executor = Stack(tmp_path), Executor()
    [plate] = _containers(stack, 1)
    other_job = str(uuid4())
    reservation = stack.materials.reserve_inventory(
        _mutation("reserve_inventory", job_uuid=other_job), InventoryReservationCreate(
            task_uuid=str(uuid4()), node_uuid=str(uuid4()), job_uuid=other_job, scheduler_revision=1,
            requirements=[InventoryRequirement(key="plate", kind="material", material_uuid=plate)])).data
    node = _node("处理", "process", "robot", {"plate": {"uuid": plate}})
    scheduler, task = _run(stack, executor, [node], {"process": _PROCESS})
    try:
        wait_for(lambda: _job(stack, task, node)["control_data"].get("wait_reason"))
        assert _job(stack, task, node)["control_data"]["wait_reason"]["code"] == "inventory_reserved_by_other_tasks"
        assert executor.dispatched == [] and not scheduler.list_reconciliation_decisions()

        stack.materials.release_inventory_reservation(
            _mutation("release_inventory_reservation", job_uuid=other_job),
            InventoryReservationTransition(reservation_uuid=reservation.reservation_uuid, reason="对方取消"))
        scheduler.resume_pending_dispatches()
        wait_for(lambda: len(executor.dispatched) == 1)
        executor.finish(0, {})
        wait_for(lambda: _status(stack, task) == "succeeded")
    finally:
        scheduler.stop()
        stack.close()


def test_released_reservation_is_a_preflight_decision_instead_of_a_stale_dispatch(tmp_path):
    """整任务启动时预留的库存在节点就绪前被释放：注入的分配已不归本作业，不能照旧派发。"""
    stack, executor = Stack(tmp_path), Executor()
    _template(stack.materials, "reagent-template", "reagent")
    _inbound(stack.materials, lot_uuid="lot-a", quantity=20)
    requirement = {"key": "solvent", "kind": "lot", "template_uuid": "reagent-template", "quantity": 5, "unit": "ul"}
    first = _node("准备", "prepare", "bench")
    second = _node("加液", "operate", "reactor", after=first, meta={"inventory_requirements": [requirement]})
    scheduler, task = _run(stack, executor, [first, second], {})
    try:
        wait_for(lambda: len(executor.dispatched) == 1)
        [reservation] = stack.client.list_inventory_reservations(task_uuid=task["uuid"])
        stack.materials.release_inventory_reservation(
            _mutation("release_inventory_reservation", job_uuid=reservation.job_uuid),
            InventoryReservationTransition(reservation_uuid=reservation.reservation_uuid, reason="操作员误释放"))
        executor.finish(0, {})
        wait_for(lambda: _preflight_decision(scheduler, task["uuid"]) is not None)
        [error] = _preflight_decision(scheduler, task["uuid"])["error_info"]
        [problem] = error["materials"]
        assert (error["code"], problem["reason"]) == ("material_sanity", "reservation_inactive")
        assert problem["details"] == {"reservation_uuid": reservation.reservation_uuid, "status": "released"}
        assert len(executor.dispatched) == 1
    finally:
        scheduler.stop()
        stack.close()


def test_normal_path_reads_once_per_node_and_skips_nodes_without_materials(tmp_path):
    stack, executor = Stack(tmp_path), Executor()
    plate, tube, stock = _containers(stack, 3)
    calls = []
    original = stack.client.material_sanity_facts

    def spy(payload):
        calls.append(payload)
        return original(payload)

    stack.client.material_sanity_facts = spy
    requirement = {"key": "spare", "kind": "material", "material_uuid": stock}
    first = _node("准备", "prepare", "bench")  # 没有物料参数、没有预留：不读
    second = _node("处理", "process_two", "robot", {"plate": {"uuid": plate}, "tube": {"uuid": tube}}, after=first,
                   meta={"inventory_requirements": [requirement]})
    scheduler, task = _run(stack, executor, [first, second], {"process_two": _PROCESS_TWO})
    try:
        wait_for(lambda: len(executor.dispatched) == 1)
        assert calls == []
        executor.finish(0, {})
        wait_for(lambda: len(executor.dispatched) == 2)
        # 一个节点一次批量读取；本作业预留选中的物料是 reserved，归本作业，照常放行
        [payload] = calls
        assert sorted(payload["material_uuids"]) == sorted([plate, tube, stock])
        [reservation] = stack.client.list_inventory_reservations(task_uuid=task["uuid"])
        assert payload["reservation_uuid"] == reservation.reservation_uuid
        assert stack.materials.get_material(stock).material.lifecycle_status == "reserved"
        assert "wait_reason" not in _job(stack, task, second)["control_data"]
        assert not scheduler.list_reconciliation_decisions()
        executor.finish(1, {})
        wait_for(lambda: _status(stack, task) == "succeeded")
    finally:
        scheduler.stop()
        stack.close()


def test_reference_to_a_never_registered_material_still_fails_as_a_contract_error(tmp_path):
    stack, executor = Stack(tmp_path), Executor()
    node = _node("处理", "process", "robot", {"plate": {"uuid": str(uuid4())}})
    scheduler, task = _run(stack, executor, [node], {"process": _PROCESS})
    try:
        wait_for(lambda: _status(stack, task) == "failed")
        assert not scheduler.list_reconciliation_decisions()
        assert executor.dispatched == []
    finally:
        scheduler.stop()
        stack.close()


def _sanity(fact, **kwargs):
    return MaterialSanity(job_uuid="job", task_uuid="task", references={"m": ("sample",)},
                          facts={"materials": {"m": {"material_uuid": "m", "exists": True, "deleted": False,
                                                     "lifecycle_status": "active", **fact}}}, **kwargs)


def test_relocation_keeps_waiting_while_another_attempt_holds_the_material():
    moved = ResourceConditionWait("transfer_source_site_missing", "声明的搬运来源与权威库位不一致", material_uuid="m")
    assert _sanity({"held_by_job_uuid": "other"}).relocation_problem(moved, lambda _m: []) is None
    assert _sanity({}).relocation_problem(moved, lambda _m: ["running-job"]) is None  # 调度侧另一 attempt 正在用它
    problem = _sanity({"site_uuid": "site-b"}).relocation_problem(moved, lambda _m: [])
    assert (problem.reason, problem.wait_code, problem.details["site_uuid"]) == ("moved", None, "site-b")
    assert _sanity({}).relocation_problem(moved, lambda _m: []).reason == "off_site"
    # 别的临时条件不归这里判
    occupied = ResourceConditionWait("gripper_site_occupied", "夹爪忙", material_uuid="m")
    assert _sanity({}).relocation_problem(occupied, lambda _m: []) is None


def test_lifecycle_claimed_by_this_task_passes_and_by_others_is_classified():
    finished = {"done"}

    def claimed(status, reservations):
        return _sanity({"lifecycle_status": status, "reservations": reservations},
                       task_finished=lambda task_uuid: task_uuid in finished).problems()

    assert claimed("in_use", [{"task_uuid": "task", "job_uuid": "earlier", "status": "consumed"}]) == []
    # 其他任务还没结束：不管预留是 active 还是已开始（consumed），都排队等它
    [waiting] = claimed("reserved", [{"task_uuid": "other", "job_uuid": "j", "status": "active"}])
    assert (waiting.reason, waiting.wait_code) == ("reserved_by_other_task", "inventory_reserved_by_other_tasks")
    [busy] = claimed("in_use", [{"task_uuid": "other", "job_uuid": "j", "status": "consumed"}])
    assert (busy.reason, busy.wait_code) == ("in_use_by_other_task", "inventory_reserved_by_other_tasks")
    # 已结束任务留下的 in_use（dev 不改回 active）、没有任何预留的 in_use：视为可用
    assert claimed("in_use", [{"task_uuid": "done", "job_uuid": "j", "status": "consumed"}]) == []
    assert claimed("in_use", []) == []
    # reserved 却没有未结束任务的预留：状态与预留对不上，交给操作员
    [orphan] = claimed("reserved", [])
    assert (orphan.reason, orphan.wait_code) == ("lifecycle_conflict", None)
    [stale] = claimed("reserved", [{"task_uuid": "done", "job_uuid": "j", "status": "active"}])
    assert (stale.reason, stale.wait_code) == ("lifecycle_conflict", None)
    # 查不到任务（调度器以外、或任务记录已不在）：有效预留仍算占着，已开始的预留按用完
    unknown = _sanity({"lifecycle_status": "in_use", "reservations": [
        {"task_uuid": "gone", "job_uuid": "j", "status": "active"}]}).problems()
    assert [item.wait_code for item in unknown] == ["inventory_reserved_by_other_tasks"]
    assert _sanity({"lifecycle_status": "in_use", "reservations": [
        {"task_uuid": "gone", "job_uuid": "j", "status": "consumed"}]}).problems() == []


def test_material_left_in_use_by_a_finished_task_dispatches_without_a_decision(tmp_path):
    """用过的物料停在 in_use（dev 不改回 active）：后来的任务显式引用它照常派发，不弹预检决策。"""

    stack, executor = Stack(tmp_path), Executor()
    [plate] = _containers(stack, 1)
    stack.materials.patch_material(_mutation("patch_material"), plate, MaterialPatch(lifecycle_status="in_use"))
    node = _node("处理", "process", "robot", {"plate": {"uuid": plate}})
    scheduler, task = _run(stack, executor, [node], {"process": _PROCESS})
    try:
        wait_for(lambda: len(executor.dispatched) == 1)
        assert not scheduler.list_reconciliation_decisions()
        executor.finish(0, {})
        wait_for(lambda: _status(stack, task) == "succeeded")
    finally:
        scheduler.stop()
        stack.close()


def test_references_cover_typed_slots_lock_parameters_and_reservation_items():
    schema = _schema({"plate": _LOCK, "tips": {"type": "array", "items": {**_LOCK, "x-unilabos-material-lock": False}},
                      "volume": {"type": "number"}})
    args = {"plate": {"uuid": "p"}, "tips": [{"uuid": "t1"}, {"uuid": "t2"}], "volume": 3,
            "legacy": {"id": "x", "uuid": "l"}}
    items = [{"key": "buffer", "kind": "content", "material_uuid": "bottle"}, {"key": "solvent", "kind": "lot"}]
    assert material_references(schema, args, ["legacy"], items) == {
        "p": ("plate",), "t1": ("tips",), "t2": ("tips",), "l": ("legacy",), "bottle": ("inventory:buffer",)}
    assert material_references(None, {"volume": 1}, [], []) == {}
