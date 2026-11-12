"""V1 作业 / 库存接缝合同：``inventory-consumptions``（任务 / 作业 / 试剂）、``reconcile-quantity``、
``resolve-uncertain``。

数量核对与物理回执结算共用同一幂等效果键，验证两者先后任意时都不会重复扣减。
"""

from __future__ import annotations

from typing import Any, Dict, List
from uuid import uuid4

import pytest

from tests.api_envelope import error_of, ok_data
from tests.server.v1_lifecycle_support import Stack, wait_for
from unilabos.protocol.material_execution import (
    InventoryConsumption,
    MaterialExecutionPrepare,
    MaterialExecutionReceipt,
)
from unilabos.protocol.materials import (
    InventoryLotInbound,
    InventoryMutation,
    InventoryRequirement,
    InventoryReservationCreate,
    InventoryReservationTransition,
    ResourceTemplateWrite,
)
from unilabos.server.services.materials import MaterialsService
from unilabos.server.services.materials.contents import ContentContract
from unilabos.server.services.materials.core import MaterialConflictError
from unilabos.server.services.materials.resource_contract import ResourceContract
from unilabos.server.services.runtime.error_decisions import ErrorDecisionDesk


def _mutation(operation: str, **extra: Any) -> InventoryMutation:
    return InventoryMutation(command_uuid=str(uuid4()), effect_key=operation, operation=operation, **extra)


@pytest.fixture
def seam(monkeypatch, tmp_path):
    stack = Stack()
    materials = MaterialsService(tmp_path / "materials.db")
    monkeypatch.setattr("unilabos.server.backend.composition.get_materials_service", lambda: materials)
    desk = ErrorDecisionDesk(lambda: stack.scheduler, lambda: None, lambda: None)
    monkeypatch.setattr("unilabos.server.services.runtime.error_decisions.default_desk", lambda: desk)
    materials.put_template(_mutation("put_template"), ResourceTemplateWrite(
        template_uuid="naoh-template", name="naoh", display_name="氢氧化钠", resource_type="reagent",
        class_name="Reagent"))
    materials.inbound_inventory_lot(_mutation("inbound_inventory_lot"), InventoryLotInbound(
        lot_uuid="lot-naoh", template_uuid="naoh-template", unit="ml", quantity=100))
    yield stack, materials
    stack.close()
    materials.close()


def _running_job(stack: Stack) -> Dict[str, Any]:
    before = len(stack.executor.dispatched)
    task = stack.create_task(["device-a"])
    wait_for(lambda: len(stack.executor.dispatched) == before + 1)
    job = stack.service.get_workflow_node_job(stack.executor.dispatched[before]["job_id"])
    return {"task": task, "job": job, "index": before}


def _reserve_lot(materials: MaterialsService, job: Dict[str, Any], quantity: float) -> str:
    reservation = materials.reserve_inventory(
        _mutation("reserve_inventory", job_uuid=job["uuid"]),
        InventoryReservationCreate(task_uuid=job["workflow_task_uuid"], node_uuid=job["workflow_node_uuid"],
                                   job_uuid=job["uuid"], scheduler_revision=1, requirements=[
                                       InventoryRequirement(key="naoh", kind="lot", lot_uuid="lot-naoh",
                                                            quantity=quantity, unit="ml")]),
    ).data
    # Host 在动作开始时扣减（ExecutionInventoryCoordinator 的效果键）
    materials.consume_inventory_reservation(
        InventoryMutation(command_uuid=job["uuid"], effect_key="inventory.consume",
                          operation="consume_inventory_reservation", actor_type="edge", job_uuid=job["uuid"]),
        InventoryReservationTransition(reservation_uuid=reservation.reservation_uuid),
    )
    return reservation.reservation_uuid


def _finish(stack: Stack, run: Dict[str, Any]) -> None:
    stack.executor.finish(run["index"])
    wait_for(lambda: stack.service.get_workflow_node_job(run["job"]["uuid"])["status"] == "succeeded")


def _reconcile(stack: Stack, job_uuid: str, items: List[Dict[str, Any]], reason: str = "称量复核"):
    return stack.client.post(f"/api/v1/workflow-node-jobs/{job_uuid}/reconcile-quantity",
                             json={"consumptions": items, "reason": reason})


def _lot_item(quantity: float, unit: str = "ml") -> Dict[str, Any]:
    return {"inventory_type": "lot", "inventory_uuid": "lot-naoh", "actual_quantity": quantity, "quantity_unit": unit}


def _total(materials: MaterialsService) -> float:
    return materials.get_inventory_lot("lot-naoh").quantity_total


def _physical(materials: MaterialsService, run: Dict[str, Any], reservation_uuid: str, actual: float) -> None:
    job = run["job"]
    record = materials.executions.prepare(MaterialExecutionPrepare(
        job_uuid=job["uuid"], task_uuid=job["workflow_task_uuid"], claim_uuid="claim-" + job["uuid"], attempt_no=1,
        parameter_hash="params", fences={}, inventory_reservation_uuid=reservation_uuid))
    materials.executions.settle(MaterialExecutionReceipt(
        job_uuid=job["uuid"], claim_uuid=record["claim_uuid"], plan_hash=record["plan_hash"],
        inventory_consumptions=[InventoryConsumption(key="naoh", lot_uuid="lot-naoh", actual_quantity=actual,
                                                     quantity_unit="ml")]))


def test_job_and_task_consumptions_follow_reconciliation_without_double_deduction(seam) -> None:
    stack, materials = seam
    run = _running_job(stack)
    job_uuid = run["job"]["uuid"]
    error_of(_reconcile(stack, job_uuid, [_lot_item(6)]), 3003)  # 作业仍在执行
    reservation_uuid = _reserve_lot(materials, run["job"], 10)
    assert _total(materials) == 90
    _finish(stack, run)

    records = ok_data(stack.client.get(f"/api/v1/workflow-node-jobs/{job_uuid}/inventory-consumptions"))
    assert len(records) == 1
    record = records[0]
    assert {key: record[key] for key in ("workflow_task_uuid", "workflow_node_job_uuid", "inventory_type",
                                         "inventory_uuid", "planned_quantity", "actual_quantity", "quantity_unit",
                                         "balance_before", "balance_after", "status")} == {
        "workflow_task_uuid": run["task"]["uuid"], "workflow_node_job_uuid": job_uuid, "inventory_type": "lot",
        "inventory_uuid": "lot-naoh", "planned_quantity": 10, "actual_quantity": 10, "quantity_unit": "ml",
        "balance_before": 100, "balance_after": 90, "status": "consumed"}
    assert record["node_name"] == "动作0" and record["workflow_name"] == "生命周期测试"
    assert record["display_name"] == "氢氧化钠" and record["consumed_at"].endswith("Z")
    assert record["meta_data"]["reservation_uuid"] == reservation_uuid

    reconciled = _reconcile(stack, job_uuid, [_lot_item(6)])
    assert reconciled.status_code == 200
    job = ok_data(reconciled)
    assert job["uuid"] == job_uuid and job["status"] == "succeeded"
    assert job["control_data"]["quantity_reconciliation"]["reason"] == "称量复核"
    [corrected] = job["inventory_consumptions"]
    assert corrected["actual_quantity"] == 6 and corrected["balance_after"] == 94
    assert corrected["meta_data"]["reconciliation"]["reason"] == "称量复核"
    assert _total(materials) == 94
    # 同一核对重放、换单位的同一用量：不再改账；不同用量：冲突，余量不变
    assert ok_data(_reconcile(stack, job_uuid, [_lot_item(6)]))["inventory_consumptions"][0]["actual_quantity"] == 6
    assert ok_data(_reconcile(stack, job_uuid, [_lot_item(0.006, "l")]))["uuid"] == job_uuid
    error_of(_reconcile(stack, job_uuid, [_lot_item(7)]), 3003)
    assert _total(materials) == 94
    # 已核对后物理回执给出不同用量：结算被拒绝，不会再扣一次；同一用量重放
    with pytest.raises(MaterialConflictError):
        _physical(materials, run, reservation_uuid, 7)
    assert _total(materials) == 94
    task_records = ok_data(stack.client.get(f"/api/v1/workflow-tasks/{run['task']['uuid']}/inventory-consumptions"))
    assert [item["uuid"] for item in task_records] == [corrected["uuid"]]
    error_of(stack.client.get(f"/api/v1/workflow-node-jobs/{uuid4()}/inventory-consumptions"), 3002)
    error_of(stack.client.get(f"/api/v1/workflow-tasks/{uuid4()}/inventory-consumptions"), 3002)


def test_physical_settlement_first_then_reconcile_replays_only_the_same_quantity(seam) -> None:
    stack, materials = seam
    run = _running_job(stack)
    reservation_uuid = _reserve_lot(materials, run["job"], 10)
    _physical(materials, run, reservation_uuid, 3)
    assert _total(materials) == 97
    _finish(stack, run)
    job_uuid = run["job"]["uuid"]
    [record] = ok_data(stack.client.get(f"/api/v1/workflow-node-jobs/{job_uuid}/inventory-consumptions"))
    assert record["actual_quantity"] == 3 and record["meta_data"]["actual_quantity_reported"] is True
    assert record["meta_data"]["physical_settlement_entry_uuid"]
    error_of(_reconcile(stack, job_uuid, [_lot_item(5)]), 3003)
    assert _total(materials) == 97
    assert ok_data(_reconcile(stack, job_uuid, [_lot_item(3)]))["inventory_consumptions"][0]["actual_quantity"] == 3
    assert _total(materials) == 97


def test_reconcile_rejects_unsettled_or_unknown_inventory_and_is_atomic(seam) -> None:
    stack, materials = seam
    run = _running_job(stack)
    job = run["job"]
    reservation = materials.reserve_inventory(
        _mutation("reserve_inventory", job_uuid=job["uuid"]),
        InventoryReservationCreate(task_uuid=job["workflow_task_uuid"], node_uuid=job["workflow_node_uuid"],
                                   job_uuid=job["uuid"], scheduler_revision=1, requirements=[
                                       InventoryRequirement(key="naoh", kind="lot", lot_uuid="lot-naoh",
                                                            quantity=10, unit="ml")]),
    ).data
    _finish(stack, run)
    # 预留还没按生命周期结算（动作没有开始扣减）：不能凭核对扣量
    error_of(_reconcile(stack, job["uuid"], [_lot_item(4)]), 3003)
    materials.consume_inventory_reservation(
        InventoryMutation(command_uuid=job["uuid"], effect_key="inventory.recovery.consume",
                          operation="consume_inventory_reservation", actor_type="scheduler", job_uuid=job["uuid"]),
        InventoryReservationTransition(reservation_uuid=reservation.reservation_uuid))
    # 一项合法、一项不在预留里：整体拒绝，合法项也不落账
    error_of(_reconcile(stack, job["uuid"], [_lot_item(4), {
        "inventory_type": "reagent", "inventory_uuid": str(uuid4()), "actual_quantity": 1, "quantity_unit": "ml"}]), 3003)
    assert _total(materials) == 90
    # 超出可用量的超耗：批次不变量拒绝，余量不变
    error_of(_reconcile(stack, job["uuid"], [_lot_item(500)]), 4002)
    assert _total(materials) == 90
    for body in ({"consumptions": [], "reason": "x"}, {"consumptions": [_lot_item(1)], "reason": ""},
                 {"consumptions": [_lot_item(-1)], "reason": "x"},
                 {"consumptions": [{**_lot_item(1), "inventory_type": "reagent", "inventory_uuid": "bad"}], "reason": "x"}):
        error_of(stack.client.post(f"/api/v1/workflow-node-jobs/{job['uuid']}/reconcile-quantity", json=body), 1000)
    error_of(_reconcile(stack, str(uuid4()), [_lot_item(1)]), 3002)


def test_content_reservation_reconcile_and_reagent_lineage(seam) -> None:
    stack, materials = seam
    contents = ContentContract(materials, compound_source=None)
    contract = ResourceContract(materials)
    template = contract.sync_resource_templates([{"id": "bottle", "registry_type": "resource"}])["templates"][0]
    bottle = contract.create_material({"resource_template_uuid": template["uuid"], "name": "试剂瓶"})
    info = contents.create_reagent_info({"name": "乙醇", "cas": "64-17-5", "physical_state": "liquid"})
    reagent = contents.create_reagent({"material_uuid": bottle["uuid"], "reagent_info_uuid": info["uuid"],
                                       "quantity": 100, "quantity_unit": "ml"})
    runs = []
    for _ in range(2):  # 同一设备的动作锁：逐个派发、结束
        run = _running_job(stack)
        runs.append(run)
        job = run["job"]
        contents.reserve_content({"substance_uuid": reagent["uuid"], "task_uuid": job["workflow_task_uuid"],
                                  "node_uuid": job["workflow_node_uuid"], "job_uuid": job["uuid"],
                                  "quantity": 20, "quantity_unit": "ml"})
        _finish(stack, run)
    first, second = (run["job"]["uuid"] for run in runs)
    item = {"inventory_type": "reagent", "inventory_uuid": reagent["uuid"], "actual_quantity": 15, "quantity_unit": "ml"}
    [record] = ok_data(_reconcile(stack, first, [item]))["inventory_consumptions"]
    assert {key: record[key] for key in ("inventory_type", "planned_quantity", "actual_quantity", "quantity_unit",
                                         "balance_before", "balance_after", "container_barcode")} == {
        "inventory_type": "reagent", "planned_quantity": 20, "actual_quantity": 15, "quantity_unit": "ml",
        "balance_before": 100, "balance_after": 85, "container_barcode": ""}
    assert record["display_name"] == "试剂瓶" and record["meta_data"]["reconciliation"]["reason"] == "称量复核"
    assert contents.get_reagent(reagent["uuid"])["quantity"] == 85
    assert ok_data(_reconcile(stack, first, [item]))["uuid"] == first  # 重放
    error_of(_reconcile(stack, first, [{**item, "actual_quantity": 16}]), 3003)
    # 超过容器现有量：拒绝且不扣减
    error_of(_reconcile(stack, second, [{**item, "actual_quantity": 500}]), 3003)
    assert contents.get_reagent(reagent["uuid"])["quantity"] == 85
    ok_data(_reconcile(stack, second, [{**item, "actual_quantity": 0.01, "quantity_unit": "l"}]))
    assert contents.get_reagent(reagent["uuid"])["quantity"] == 75

    lineage = ok_data(stack.client.get(f"/api/v1/reagents/{reagent['uuid']}/inventory-consumptions"))
    assert [entry["workflow_node_job_uuid"] for entry in lineage] == [second, first]  # 新在前
    assert [entry["actual_quantity"] for entry in lineage] == [10, 15]
    assert all(entry["node_name"] == "动作0" for entry in lineage)
    assert ok_data(stack.client.get(f"/api/v1/reagents/{uuid4()}/inventory-consumptions")) == []
    error_of(stack.client.get("/api/v1/reagents/not-a-uuid/inventory-consumptions"), 1000)


def test_consumptions_are_empty_without_a_materials_authority(seam, monkeypatch) -> None:
    stack, _materials = seam
    run = _running_job(stack)
    monkeypatch.setattr("unilabos.server.backend.composition.get_materials_service", lambda: None)
    assert ok_data(stack.client.get(f"/api/v1/workflow-node-jobs/{run['job']['uuid']}/inventory-consumptions")) == []
    assert ok_data(stack.client.get(f"/api/v1/reagents/{uuid4()}/inventory-consumptions")) == []
    error_of(_reconcile(stack, run["job"]["uuid"], [_lot_item(1)]), 5001)


def test_resolve_uncertain_aborts_through_the_reconciliation_decision(seam) -> None:
    stack, _materials = seam
    run = _running_job(stack)
    job_uuid = run["job"]["uuid"]
    url = f"/api/v1/workflow-node-jobs/{job_uuid}/resolve-uncertain"
    error_of(stack.client.post(url, json={"resolution": "canceled", "reason": "核实"}), 3003)  # 结果并非未知
    stack.executor.callback(job_uuid, False, None, "execution_unknown", {"uncertainty_reason": "Host 重启"})
    wait_for(lambda: stack.service.get_workflow_node_job(job_uuid)["status"] == "execution_unknown")
    error_of(stack.client.post(url, json={"resolution": "retry", "reason": "核实"}), 1000)
    error_of(stack.client.post(url, json={"resolution": "canceled", "reason": " "}), 1000)
    response = stack.client.post(url, json={"resolution": "canceled", "reason": "设备已停止"})
    assert response.status_code == 200, response.text
    result = ok_data(response)
    assert result["created"] is True and result["pending_edge_confirmation"] is False
    assert result["job"]["status"] == "failed" and result["resolution_command_uuid"]
    manual = result["job"]["control_data"]["manual_resolution"]
    assert manual["resolution"] == "canceled" and manual["selected_action"] == "abort"
    assert manual["device_command_id"] == f"workflow-node-job:{job_uuid}"
    replay = ok_data(stack.client.post(url, json={"resolution": "canceled", "reason": "设备已停止"}))
    assert replay["created"] is False and replay["resolution_command_uuid"] == result["resolution_command_uuid"]
    error_of(stack.client.post(url, json={"resolution": "canceled", "reason": "另一结论"}), 3003)
    error_of(stack.client.post(f"/api/v1/workflow-node-jobs/{uuid4()}/resolve-uncertain",
                               json={"resolution": "canceled", "reason": "x"}), 3002)


def test_resolve_uncertain_waits_for_the_executor_and_keeps_physical_rule(seam, monkeypatch) -> None:
    stack, _materials = seam
    run = _running_job(stack)
    job_uuid = run["job"]["uuid"]

    class Desk:
        """执行面持有的物理结算类决策：放行后由执行面异步收敛，作业暂不终态。"""

        def __init__(self) -> None:
            self.delivered: List[Any] = []

        def items(self):
            return [{"decision_id": "decision-1", "job_id": job_uuid, "category": "physical_settlement",
                     "options": [{"action": "operator_intervention"}, {"action": "abort"}]}]

        def resolve(self, decision_id, decision, *, idempotency_key="", operator_id=""):
            self.delivered.append((decision_id, decision, idempotency_key))
            return {"decision_id": decision_id, "status": "delivered"}

    desk = Desk()
    monkeypatch.setattr("unilabos.server.services.runtime.error_decisions.default_desk", lambda: desk)
    response = stack.client.post(f"/api/v1/workflow-node-jobs/{job_uuid}/resolve-uncertain",
                                 json={"resolution": "canceled", "reason": "夹爪位置待核", "device_command_id": "cmd-9"})
    assert response.status_code == 202, response.text
    result = ok_data(response, status=202)
    assert result["pending_edge_confirmation"] is True and result["resolution_command_uuid"] == "decision-1"
    # 物理结果未知只投递终止（附核实说明），不会请求释放占用
    assert desk.delivered == [("decision-1", {"action": "abort", "reason": "夹爪位置待核"},
                               f"resolve-uncertain:{job_uuid}")]
    assert result["job"]["control_data"]["manual_resolution"]["device_command_id"] == "cmd-9"


def test_settled_aliquot_is_a_consumption_of_the_source_contents(tmp_path) -> None:
    from unilabos.protocol.material_execution import AliquotReceipt, PhysicalAliquot
    from unilabos.server.services.materials.consumption import InventoryConsumptions

    materials = MaterialsService(tmp_path / "aliquot.db")
    try:
        contents = ContentContract(materials, compound_source=None)
        contract = ResourceContract(materials)
        template = contract.sync_resource_templates([{"id": "tube", "registry_type": "resource"}])["templates"][0]
        source, target = (contract.create_material({"resource_template_uuid": template["uuid"], "name": name})
                          for name in ("母液", "子管"))
        info = contents.create_reagent_info({"name": "乙醇", "cas": "64-17-5", "physical_state": "liquid"})
        reagent = contents.create_reagent({"material_uuid": source["uuid"], "reagent_info_uuid": info["uuid"],
                                           "quantity": 100, "quantity_unit": "ml"})
        job_uuid, task_uuid = str(uuid4()), str(uuid4())
        fences = {f'["material","{identity}"]': 1 for identity in (source["uuid"], target["uuid"])}
        record = materials.executions.prepare(MaterialExecutionPrepare(
            job_uuid=job_uuid, task_uuid=task_uuid, claim_uuid="claim-a", attempt_no=1, parameter_hash="p",
            fences=fences, aliquot=PhysicalAliquot(source_material_uuid=source["uuid"],
                                                   target_material_uuids=[target["uuid"]])))
        materials.executions.settle(MaterialExecutionReceipt(
            job_uuid=job_uuid, claim_uuid="claim-a", plan_hash=record["plan_hash"],
            material_aliquot_receipts=[AliquotReceipt(target_material_uuid=target["uuid"], actual_quantity=10.0,
                                                      quantity_unit="ml")]))
        reads = InventoryConsumptions(materials)
        [fact] = reads.list_job(job_uuid)
        assert {key: fact[key] for key in ("workflow_task_uuid", "inventory_type", "inventory_uuid", "planned_quantity",
                                           "actual_quantity", "balance_before", "balance_after", "status")} == {
            "workflow_task_uuid": task_uuid, "inventory_type": "reagent", "inventory_uuid": reagent["uuid"],
            "planned_quantity": None, "actual_quantity": 10, "balance_before": 100, "balance_after": 90,
            "status": "consumed"}
        assert fact["meta_data"]["physical_settlement_entry_uuid"]
        assert [item["uuid"] for item in reads.list_substance(reagent["uuid"])] == [fact["uuid"]]
    finally:
        materials.close()
