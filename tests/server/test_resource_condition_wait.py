"""临时资源条件让 attempt 继续排队、资源变化后重试，而不是记 ``dispatch_not_sent`` 失败（V1 ``resource_wait_policy``）。

目标库位被占、夹爪忙、来源暂不在库位、另一作业的物理动作尚未结算都会随其他作业或物料移动解除；
库位不属于所选容器之类的合同错误照常失败。
"""
from __future__ import annotations

from uuid import uuid4

import pytest

from tests.server.test_material_execution import fill
from tests.server.test_scheduler_inventory_authority import _material, _mutation, _template
from tests.server.test_workflow_inventory_recovery import Stack
from tests.server.test_workflow_parallel_execution import Executor, wait_for
from tests.server.test_workflow_physical_recovery import make_task, result, seed
from unilabos.client.materials.core import MaterialsHTTPError
from unilabos.protocol.materials import MaterialIdentityWrite, MaterialMove, MaterialNodeCreate, MaterialTreeCreate, SiteCreate
from unilabos.registry.workflows import upsert_workflow
from unilabos.server.scheduler.resource_wait_policy import temporary_resource_condition
from unilabos.server.scheduler.service import BackendScheduler
from unilabos.server.services.materials.core import MaterialConflictError, MaterialValidationError

_LOCK = {"type": "object", "properties": {"uuid": {"type": "string"}}, "required": ["uuid"],
         "x-unilabos-material-lock": True}
_SCHEMA = {
    "x-unilabos-action-contract": {"version": 2},
    "properties": {
        "goal": {
            "type": "object",
            "properties": {
                "plate": _LOCK,
                "sample": _LOCK,
                "site": {"type": "string", "format": "uuid", "x-unilabos-site-selector": {
                    "version": 1, "owner": "plate", "occupant": "sample", "show_occupied": True, "allow_occupied": False}},
            },
            "required": ["plate", "sample", "site"],
        },
        "result": {"type": "object", "properties": {}},
    },
}


def _plate(stack):
    """一块两孔板：A 孔上已有一只挡路的管子。"""
    from unilabos.server.services.materials.core import MaterialNoChangeError

    try:
        _template(stack.materials, "container-template", "container")
    except MaterialNoChangeError:
        pass

    def identity(name):
        return MaterialIdentityWrite(resource_id=f"{name}-{uuid4()}", name=f"{name}-{uuid4()}", template_name="container",
                                     resource_type="container", class_name="Container")

    tree = stack.materials.create_tree(_mutation("create_material_tree"), MaterialTreeCreate(nodes=[
        MaterialNodeCreate(client_ref="plate", identity=identity("plate"), sites=[
            SiteCreate(template_name="slot", site_index=0, label="A", occupied_client_ref="blocker"),
            SiteCreate(template_name="slot", site_index=1, label="B")]),
        MaterialNodeCreate(client_ref="blocker", parent_client_ref="plate", identity=identity("blocker")),
    ])).data
    plate = tree.client_ref_map["plate"]
    sites = {site.label: site.site_uuid for site in stack.materials.get_material(plate).sites}
    return plate, tree.client_ref_map["blocker"], sites


def _place_task(stack, executor, plate, sample, site):
    stack.workflow.configure_preflight(materials=stack.client, action_schema_resolver=lambda *_: _SCHEMA)
    scheduler = BackendScheduler(stack.workflow, executor, materials_gateway=stack.client)
    stack.workflow.set_task_submitter(scheduler.submit)
    record = upsert_workflow(stack.workflow, {"workflow_uuid": str(uuid4()), "name": "放样", "description": "",
                                              "tags": [], "edges": [], "nodes": [{
        "uuid": str(uuid4()), "name": "放到 A 孔", "type": "device_action", "material_uuid": str(uuid4()),
        "action_name": "place", "meta_data": {"target_device_id": "robot"},
        "param": {"plate": {"uuid": plate}, "sample": {"uuid": sample}, "site": site}}]})
    task = stack.workflow.create_workflow_task(workflow_uuid=record["uuid"], run_mode="normal", target_node_uuid=None,
                                               input_value={}, description=None, meta_data={})
    return scheduler, task


def _job(stack, task):
    [job] = stack.workflow.list_workflow_node_jobs(task["uuid"])
    return job


def test_occupied_target_site_waits_until_the_site_is_free(tmp_path):
    stack, executor = Stack(tmp_path), Executor()
    plate, blocker, sites = _plate(stack)
    sample = _material(stack.materials, "container")
    scheduler, task = _place_task(stack, executor, plate, sample, sites["A"])
    try:
        wait_for(lambda: _job(stack, task)["control_data"].get("wait_reason"))
        reason = _job(stack, task)["control_data"]["wait_reason"]
        assert reason["code"] == "site_occupied" and reason["waiting_since"]
        assert executor.dispatched == []
        assert _job(stack, task)["status"] == "pending"
        assert stack.workflow.get_workflow_task(task["uuid"])["status"] == "running"
        scheduler.resume_pending_dispatches()  # 条件仍在：继续等，不失败
        assert executor.dispatched == [] and _job(stack, task)["status"] == "pending"

        stack.materials.move_material(_mutation("move_material"),
                                      MaterialMove(material_uuid=blocker, destination_site_uuid=sites["B"]))
        scheduler.resume_pending_dispatches()  # 资源变化后重试
        wait_for(lambda: len(executor.dispatched) == 1)
        assert "wait_reason" not in _job(stack, task)["control_data"]
        executor.finish(0, {})
        wait_for(lambda: stack.workflow.get_workflow_task(task["uuid"])["status"] == "succeeded")
    finally:
        scheduler.stop()
        stack.close()


def test_a_site_outside_the_selected_plate_still_fails(tmp_path):
    stack, executor = Stack(tmp_path), Executor()
    plate, _blocker, _sites = _plate(stack)
    other, _other_blocker, other_sites = _plate(stack)
    sample = _material(stack.materials, "container")
    scheduler, task = _place_task(stack, executor, plate, sample, other_sites["B"])
    try:
        wait_for(lambda: stack.workflow.get_workflow_task(task["uuid"])["status"] == "failed")
        assert executor.dispatched == []
    finally:
        scheduler.stop()
        stack.close()


def test_conflict_found_while_preparing_the_physical_action_withdraws_and_waits(tmp_path):
    """物料权威准备物理动作时才发现目标被占：撤回 Claim、区间与内存锁回到排队，下次重试成功即派发。"""
    stack, executor = Stack(tmp_path), Executor()
    source, target = seed(stack)
    prepare = stack.client.prepare_material_execution
    attempts = []

    def busy_once(value):
        attempts.append(value.job_uuid)
        if len(attempts) == 1:
            raise MaterialConflictError("搬运目标库位已被占用", conflict="site_occupied")
        return prepare(value)

    stack.client.prepare_material_execution = busy_once
    scheduler = None
    try:
        scheduler, task = make_task(stack, executor, source, target, dispatched=False)
        wait_for(lambda: _job(stack, task)["control_data"].get("wait_reason"))
        job = _job(stack, task)
        assert job["status"] == "pending" and job["control_data"]["wait_reason"]["code"] == "site_occupied"
        assert not job["error_info"]
        assert scheduler._execution_claims.get(job["uuid"]) is None  # noqa: SLF001 - 撤回了未派发的 Claim
        assert not scheduler.resources.held_by(job["uuid"])
        assert not scheduler._intervals.snapshot(task["uuid"])  # noqa: SLF001
        scheduler.resume_pending_dispatches()
        wait_for(lambda: len(executor.dispatched) == 1)
        assert attempts == [job["uuid"], job["uuid"]]  # 同一 attempt 重试，不新建 attempt
        executor.finish(0, result(target))
        wait_for(lambda: stack.workflow.get_workflow_task(task["uuid"])["status"] == "succeeded")
    finally:
        if scheduler is not None:
            scheduler.stop()
        stack.close()


def test_contract_errors_from_the_physical_preparation_still_fail(tmp_path):
    stack, executor = Stack(tmp_path), Executor()
    source, target = seed(stack)

    def invalid(value):
        raise MaterialValidationError("搬运目标库位不属于目标容器")

    stack.client.prepare_material_execution = invalid
    scheduler = None
    try:
        scheduler, task = make_task(stack, executor, source, target, dispatched=False)
        wait_for(lambda: stack.workflow.get_workflow_task(task["uuid"])["status"] == "failed")
        [error] = _job(stack, task)["error_info"]
        assert error["code"] == "dispatch_not_sent"
    finally:
        if scheduler is not None:
            scheduler.stop()
        stack.close()


@pytest.mark.parametrize(("error", "code"), [
    (MaterialConflictError("x", conflict="site_occupied"), "site_occupied"),
    (MaterialConflictError("物理动作尚未结算", conflict="active_claim"), "site_claimed"),
    (MaterialConflictError("x", conflict="transfer_source_site_missing"), "transfer_source_site_missing"),
    (MaterialConflictError("x", conflict="state"), None),
    (MaterialConflictError("x"), None),
    (MaterialValidationError("x"), None),
    (ValueError("库位选择必须唯一且可见"), None),
    # HTTP 物料网关只带信封业务码
    (MaterialsHTTPError(409, "库位已被入口预留", business_code=6011), "site_claimed"),
    (MaterialsHTTPError(409, "库位已有物料", business_code=6007), "site_occupied"),
    (MaterialsHTTPError(409, "模板不允许放在该库位", business_code=6008), None),
    (MaterialsHTTPError(409, "冲突"), None),
])
def test_wait_policy_classifies_conditions(error, code):
    assert temporary_resource_condition(error) == code


def test_physical_contract_marks_temporary_conditions():
    from unilabos.server.scheduler.physical_contract import resolve_physical_contract
    from unilabos.server.scheduler.resource_wait_policy import ResourceConditionWait

    class Materials:
        def get_material(self, identity):
            from types import SimpleNamespace
            return SimpleNamespace(material=SimpleNamespace(parent_material_uuid=None, resource_type="container",
                                                            resource_id=identity), sites=[])

    schema = {"x-unilabos-action-contract": {"resource_contract": {"transfer": {
        "material_param": "sample", "target_owner_param": "target", "target_site_name_param": "slot",
        "gripper_site_role": "robot.gripper"}}}}
    with pytest.raises(ResourceConditionWait) as caught:  # 样品此刻不在任何库位上
        resolve_physical_contract(schema, {"sample": {"uuid": "s"}, "target": {"uuid": "t"}, "slot": "A"}, "arm",
                                  Materials())
    assert caught.value.wait_code == "transfer_source_site_missing"
