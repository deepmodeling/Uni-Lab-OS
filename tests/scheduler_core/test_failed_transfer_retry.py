"""失败搬运的重试、取消必须处理旧尝试占用并保留审计。"""

import json

import pytest

from tests.scheduler_core.conftest import stable_uuid
from tests.scheduler_core.test_failure_safety import outcome
from tests.scheduler_core.test_resource_admission import transfer_task
from unilabos.app.scheduler.inventory.backend_contract import BackendResourceService


def _failed_transfer(runtime, name):
    backend = BackendResourceService(runtime.inventory_store)
    template = backend.sync_resource_templates([{
        "id": "retry.transfer-item", "display_name": "重试测试物料", "registry_type": "material", "class": {},
    }])["templates"][0]
    material = backend.create_material({"resource_template_uuid": template["uuid"], "name": "测试烧杯", "barcode": name})
    sites = [("source", "warehouse-a", material["uuid"], {}),
             ("target", "reactor-a", None, {}),
             ("gripper", "robot-a", None, {"unilab": {"resource_role": "robot.gripper"}})]
    with runtime.inventory_store.transaction() as conn:
        for label, owner, occupied, metadata in sites:
            conn.execute("""INSERT INTO site(uuid,create_time,update_time,meta_data,material_uuid,name,
                sort_order,allowed_resource_template_uuids,occupied_material_uuid,
                position_x,position_y,position_z,depth,length,width)
                VALUES (?,?,?,?,?,?,0,'[]',?,0,0,0,0,0,0)""", (
                stable_uuid(f"{name}:{label}"), "2026-09-10", "2026-09-10", json.dumps(metadata),
                runtime.device_materials[owner], label, occupied))
    aggregate, identity = transfer_task(runtime, task_name=name, executor="robot-a",
        material_uuid=material["uuid"], target_owner_uuid=runtime.device_materials["reactor-a"],
        site_uuids=[stable_uuid(f"{name}:target")], named_group=False)
    runtime.scheduler.on_job_outcome(identity, outcome("failed"))
    assert runtime.workflow_store.get_job(identity)["uncertainty_reason"]
    return aggregate["task"]["uuid"], identity


def test_cancel_preserves_failed_transfer_attention_without_raising(core_runtime):
    runtime = core_runtime
    task_uuid, failed = _failed_transfer(runtime, "cancel-failed-transfer")
    result = runtime.bridge.cancel(task_uuid, command_uuid=stable_uuid("cancel-transfer"))
    assert result["task"]["status"] == "canceled"
    assert result["task"]["cleanup_status"] == "requires_attention"
    assert runtime.inventory_store.query_one("SELECT state FROM station_execution_claim WHERE job_uuid=?", (failed,))["state"] == "uncertain"
    released = runtime.bridge.unlock_resources(task_uuid, command_uuid=stable_uuid("unlock-transfer"), reason="操作员核对现场后复位")
    assert released["cleanup_status"] == "settled"


@pytest.mark.parametrize("resume_immediately", [False, True])
def test_operator_retry_releases_only_failed_attempt_and_can_dispatch(core_runtime, resume_immediately):
    runtime = core_runtime
    runtime.submit(task_name="unrelated-running", devices=["reactor-b"])
    other_job = stable_uuid("job:unrelated-running:0")
    task_uuid, failed = _failed_transfer(runtime, "retry-failed-transfer")
    snapshot = runtime.bridge.error_control.snapshot()
    error = snapshot["errors"][0]
    request = {
        "action": "retry_current_node", "expected_decision_version": error["decision_version"],
        "expected_station_version": snapshot["station_version"], "observed_error_epoch": snapshot["error_epoch"],
        "resume_requested": resume_immediately, "operator_confirmed": resume_immediately, "confirm_snapshot_id": snapshot["snapshot_id"],
    }
    accepted = runtime.bridge.decide_error(error["decision_id"], request, idempotency_key="retry-transfer", operator_id="tester")
    retry = accepted["result"]["retry_job_uuid"]
    assert accepted["status"] == "APPLIED", accepted
    if not resume_immediately:
        paused = runtime.bridge.error_control.snapshot()
        assert paused["mode"] == "PAUSED"
        assert all(item["job_id"] != retry for item in runtime.dispatcher.dispatched)
        runtime.bridge.error_control.resume({
            "expected_station_version": paused["station_version"], "observed_error_epoch": paused["error_epoch"],
            "operator_confirmed": True, "confirm_snapshot_id": paused["snapshot_id"],
        }, idempotency_key="resume-transfer", operator_id="tester")
        runtime.scheduler.reschedule()
    assert runtime.dispatcher.dispatched[-1]["job_id"] == retry, runtime.scheduler.snapshot()
    previous = runtime.workflow_store.get_job(failed)
    assert previous["status"] == "failed"
    assert not previous.get("uncertainty_reason")
    assert previous["control_data"]["operator_retry"]["previous_uncertainty_reason"] == "material_transfer_inventory_reconciliation_required"
    assert runtime.inventory_store.query_one("SELECT state FROM station_execution_claim WHERE job_uuid=?", (failed,))["state"] == "released"
    replay = runtime.bridge.decide_error(error["decision_id"], request, idempotency_key="retry-transfer", operator_id="tester")
    assert replay["result"]["retry_job_uuid"] == retry
    assert sum(item["job_id"] == retry for item in runtime.dispatcher.dispatched) == 1
    runtime.scheduler.on_job_outcome(retry, outcome("succeeded"))
    assert runtime.workflow_store.get_job(other_job)["status"] == "running"
    assert runtime.inventory_store.query_one("SELECT state FROM station_execution_claim WHERE job_uuid=?", (other_job,))["state"] == "running"
    moved = runtime.inventory_store.query_one("SELECT occupied_material_uuid FROM site WHERE uuid=?", (stable_uuid("retry-failed-transfer:target"),))
    assert moved["occupied_material_uuid"]
    assert runtime.inventory_store.query_one("SELECT occupied_material_uuid FROM site WHERE uuid=?", (stable_uuid("retry-failed-transfer:source"),))["occupied_material_uuid"] is None
    assert runtime.workflow_store.get_task(task_uuid)["status"] == "succeeded"
