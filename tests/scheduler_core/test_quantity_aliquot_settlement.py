"""真实分装 Permit/Fence 与通用数量消费的双库集成合同。"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tests.app.test_inventory_dispatch_admission import station_inventory
from tests.workflow.quantity_inventory_test_support import (
    JOB_UUID,
    NODE_UUID,
    TASK_UUID,
    WORKFLOW_UUID,
)
from unilabos.app.scheduler.inventory.backend_contract import BackendResourceService
from unilabos.app.scheduler.inventory.content_contract import BackendContainerContentService
from unilabos.app.scheduler.inventory.dispatch_admission import (
    AliquotDispatchCondition,
    DispatchAdmissionRequest,
    DispatchResource,
)
from unilabos.app.scheduler.inventory.service import InventoryService
from unilabos.app.scheduler.inventory.station_resource import AliquotReceipt, MaterialAliquotCommand
from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.app.scheduler.inventory.workflow_quantity import WorkflowQuantityReservationError
from unilabos.workflow.models import WorkflowInventoryRequirementWrite, WorkflowNodeWrite
from unilabos.workflow.quantity_inventory import WorkflowQuantityInventory
from unilabos.workflow.store import StoreConflict, WorkflowStore
from unilabos.workflow.task_input import PreparedTaskInput


@dataclass
class _AliquotScenario:
    """持有同一真实任务的库存、工作流以及分装来源和目标身份。"""

    inventory: InventoryStore
    service: InventoryService
    workflow: WorkflowStore
    coordinator: WorkflowQuantityInventory
    source_material_uuid: str
    source_content_uuid: str
    target_material_uuids: tuple[str, str]

    def settle(self, quantities: tuple[float, float]) -> MaterialAliquotCommand:
        """通过真实派发准入获得 Permit/Fence 并提交原子分装回执。"""

        request = DispatchAdmissionRequest(
            effect_uuid="50000000-0000-4000-8000-000000000399",
            task_uuid=TASK_UUID,
            job_uuid=JOB_UUID,
            attempt=1,
            parameter_hash="sha256:quantity-aliquot",
            expected_change_set={
                "kind": "material_content_aliquot",
                "source_material_uuid": self.source_material_uuid,
                "target_material_uuids": list(self.target_material_uuids),
            },
            resources=tuple(
                DispatchResource(
                    lock_key=f"material/{material_uuid}/exclusive",
                    scope="material",
                    material_uuid=material_uuid,
                )
                for material_uuid in (self.source_material_uuid, *self.target_material_uuids)
            ),
            aliquot=AliquotDispatchCondition(
                source_material_uuid=self.source_material_uuid,
                target_material_uuids=self.target_material_uuids,
            ),
        )
        decision = self.service.station_resources.acquire_dispatch_permit(request)
        assert decision.permit is not None
        permit = decision.permit
        self.service.station_resources.transition_dispatch_permit(
            permit.claim_uuid, target_state="reserved"
        )
        command = MaterialAliquotCommand(
            source_material_uuid=self.source_material_uuid,
            receipts=tuple(
                AliquotReceipt(target, quantity, "mL")
                for target, quantity in zip(self.target_material_uuids, quantities)
            ),
            effect_uuid=permit.effect_uuid,
            claim_uuid=permit.claim_uuid,
            job_uuid=request.job_uuid,
            attempt=request.attempt,
            parameter_hash=request.parameter_hash,
            expected_change_set=request.expected_change_set,
            fences=permit.fences,
        )
        self.service.station_resources.settle_material_aliquot(command)
        return command

    def reported(self, quantity: float, unit: str = "mL") -> list[dict[str, Any]]:
        """构造来源内容物的显式实际数量回执。"""

        return [{
            "inventory_type": "current_substance",
            "inventory_uuid": self.source_content_uuid,
            "actual_quantity": quantity,
            "quantity_unit": unit,
        }]

    def balances(self) -> dict[str, dict[str, Any]]:
        """读取所有内容物的数量与修订，用于检查任何额外写入。"""

        return {
            row["material_uuid"]: row
            for row in self.inventory.query_all(
                "SELECT material_uuid,quantity,revision FROM current_substance"
            )
        }


@pytest.fixture()
def aliquot_scenario(
    station_inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
    tmp_path: Path,
) -> Iterator[_AliquotScenario]:
    """创建 10 mL 来源、两个空瓶及预留 4 mL 的真实工作流任务。"""

    inventory, service, identities = station_inventory
    with inventory.transaction() as connection:
        connection.execute(
            "UPDATE resource_template SET tags='[\"container\"]' WHERE uuid=?",
            (identities["vessel_template"],),
        )
    backend = BackendResourceService(inventory)
    targets = tuple(
        backend.create_material({
            "resource_template_uuid": identities["vessel_template"],
            "barcode": f"QUANTITY-ALIQUOT-{index}",
            "name": f"分装目标 {index}",
        })["uuid"]
        for index in (1, 2)
    )
    content = BackendContainerContentService(inventory).create_current_substance({
        "material_uuid": identities["vessel"],
        "name": "母液",
        "components": [],
        "quantity": 10,
        "quantity_unit": "mL",
        "physical_state": "liquid",
    })
    workflow = WorkflowStore(tmp_path / "aliquot-workflow.db")
    try:
        workflow.create_workflow(
            workflow_uuid=WORKFLOW_UUID, name="分装数量结算", tags=[],
            description=None, meta_data={},
        )
        graph = workflow.save_graph(
            WORKFLOW_UUID,
            revision=1,
            nodes=[WorkflowNodeWrite(
                uuid=NODE_UUID, name="分装", type="device_action", action_name="aliquot",
                action_type="UniLabJsonCommand", param={}, material_uuid=identities["vessel"],
            )],
            edges=[],
            inventory_requirements=[WorkflowInventoryRequirementWrite(
                consume_node_uuid=NODE_UUID, requirement_key="source-content",
                target_type="current_substance", required_quantity=4, quantity_unit="mL",
            )],
        )
        prepared = PreparedTaskInput(
            workflow_snapshot=graph,
            resolved_input={},
            execution_plan={
                "version": 1, "run_mode": "normal", "target_node_uuid": None,
                "nodes": [{"uuid": NODE_UUID}], "handles": [], "edges": [],
            },
            jobs=[{
                "uuid": JOB_UUID, "workflow_node_uuid": NODE_UUID, "topological_index": 0,
                "executor_kind": "device_action", "execution_policy": {},
                "execution_timeout_seconds": 60, "param": {},
            }],
        )
        coordinator = WorkflowQuantityInventory(workflow, service)
        workflow.create_task_with_jobs(
            workflow_uuid=WORKFLOW_UUID, task_uuid=TASK_UUID, run_mode="normal",
            target_node_uuid=None, description=None, meta_data={},
            plan_builder=lambda _: prepared,
            inventory_allocation_builder=lambda connection, frozen_graph, frozen: (
                coordinator.prepare_task_allocations(
                    connection, graph=frozen_graph, prepared=frozen, task_uuid=TASK_UUID,
                    bindings=[{
                        "requirement_key": "source-content", "inventory_type": "current_substance",
                        "inventory_uuid": content["uuid"], "reserved_quantity": 4,
                        "quantity_unit": "mL",
                    }],
                )
            ),
        )
        yield _AliquotScenario(
            inventory=inventory, service=service, workflow=workflow, coordinator=coordinator,
            source_material_uuid=identities["vessel"], source_content_uuid=content["uuid"],
            target_material_uuids=targets,
        )
    finally:
        workflow.close()


@pytest.mark.parametrize("quantities", [(2.5, 1.5), (2.0, 1.0), (3.0, 2.0)])
@pytest.mark.parametrize("explicit_report", [False, True])
def test_physical_aliquot_receipt_is_consumed_once_without_rededucting(
    aliquot_scenario: _AliquotScenario,
    quantities: tuple[float, float],
    explicit_report: bool,
) -> None:
    """完整、少量和物理超耗回执均按已提交数量归属；重复消费不再扣减源瓶。"""

    scenario = aliquot_scenario
    total = sum(quantities)
    command = scenario.settle(quantities)
    settled_balances = scenario.balances()
    assert settled_balances[scenario.source_material_uuid]["quantity"] == 10 - total
    for target, quantity in zip(scenario.target_material_uuids, quantities):
        assert settled_balances[target]["quantity"] == quantity
    physical_event = scenario.inventory.query_one(
        "SELECT entry_uuid FROM inventory_ledger WHERE op_type='current_substance.aliquot_source'"
    )
    reported = scenario.reported(total * 1000, "uL") if explicit_report else []
    facts = scenario.coordinator.consume_successful_job(
        task_uuid=TASK_UUID, job_uuid=JOB_UUID, consumptions=reported
    )

    assert len(facts) == 1
    assert facts[0]["planned_quantity"] == 4
    assert facts[0]["actual_quantity"] == total
    assert facts[0]["quantity_unit"] == "mL"
    assert facts[0]["balance_before"] == 10
    assert facts[0]["balance_after"] == 10 - total
    assert facts[0]["meta_data"]["physical_settlement_entry_uuid"] == physical_event["entry_uuid"]
    if total > 4:
        assert facts[0]["meta_data"]["overrun_quantity"] == total - 4
        assert facts[0]["meta_data"]["overrun_authority"] == "physical_settlement"
    assert scenario.balances() == settled_balances
    assert scenario.inventory.query_one(
        "SELECT status FROM inventory_reservation WHERE workflow_id=?",
        (TASK_UUID,),
    ) == {"status": "consumed"}
    with scenario.workflow.transaction() as connection:
        assert connection.execute(
            "SELECT status FROM workflow_inventory_allocation WHERE workflow_task_uuid=?",
            (TASK_UUID,),
        ).fetchone()["status"] == "consumed"

    scenario.service.station_resources.transition_dispatch_permit(
        command.claim_uuid, target_state="released"
    )
    scenario.service.station_resources.settle_material_aliquot(command)
    assert scenario.coordinator.consume_successful_job(
        task_uuid=TASK_UUID, job_uuid=JOB_UUID, consumptions=reported
    ) == facts
    assert scenario.balances() == settled_balances
    assert scenario.inventory.query_one(
        "SELECT COUNT(*) AS count, SUM(quantity_delta) AS delta FROM inventory_ledger "
        "WHERE op_type='current_substance.consume'"
    ) == {"count": 1, "delta": 0.0}
    assert scenario.inventory.query_one(
        "SELECT COUNT(*) AS count FROM inventory_ledger "
        "WHERE op_type LIKE 'current_substance.aliquot_%'"
    ) == {"count": 3}
    assert scenario.coordinator.pending_consumption_reconciliations() == []


def test_explicit_conflicting_receipt_stays_pending_until_reconciled_to_physical_fact(
    aliquot_scenario: _AliquotScenario,
) -> None:
    """实际分装 3 mL 却上报 4 mL 时冻结预留；纠正必须符合已提交物理事实。"""

    scenario = aliquot_scenario
    scenario.settle((2.0, 1.0))
    settled_balances = scenario.balances()
    coordinator = scenario.coordinator
    with pytest.raises(WorkflowQuantityReservationError, match="分装事实不一致"):
        coordinator.consume_successful_job(
            task_uuid=TASK_UUID, job_uuid=JOB_UUID, consumptions=scenario.reported(4)
        )
    assert coordinator.list_task_consumptions(TASK_UUID) == []
    assert scenario.balances() == settled_balances
    assert scenario.inventory.query_one(
        "SELECT status FROM inventory_reservation WHERE workflow_id=?",
        (TASK_UUID,),
    ) == {"status": "active"}
    pending = coordinator.pending_consumption_reconciliations()
    assert pending[0]["job_uuid"] == JOB_UUID
    assert "分装事实不一致" in pending[0]["reason"]
    with pytest.raises(StoreConflict, match="必须完成对账"):
        coordinator.release_task(TASK_UUID, reason="workflow_failed")
    with pytest.raises(WorkflowQuantityReservationError, match="分装事实不一致"):
        coordinator.consume_successful_job(task_uuid=TASK_UUID, job_uuid=JOB_UUID, consumptions=[])
    assert TASK_UUID in coordinator.recover_pending()
    assert scenario.balances() == settled_balances

    with pytest.raises(WorkflowQuantityReservationError, match="分装事实不一致"):
        coordinator.reconcile_consumption(
            task_uuid=TASK_UUID, job_uuid=JOB_UUID, consumptions=scenario.reported(2),
            reason="人工回填仍与分装回执不符",
        )
    facts = coordinator.reconcile_consumption(
        task_uuid=TASK_UUID, job_uuid=JOB_UUID, consumptions=scenario.reported(3000, "uL"),
        reason="已核对两个目标瓶回执合计 3 mL",
    )
    assert facts[0]["actual_quantity"] == 3
    assert facts[0]["balance_after"] == 7
    assert facts[0]["meta_data"]["reconciliation"]["reason"] == "已核对两个目标瓶回执合计 3 mL"
    assert coordinator.consume_successful_job(
        task_uuid=TASK_UUID, job_uuid=JOB_UUID, consumptions=scenario.reported(4)
    ) == facts
    assert scenario.balances() == settled_balances
    assert coordinator.pending_consumption_reconciliations() == []
    assert scenario.inventory.query_one(
        "SELECT COUNT(*) AS count, SUM(quantity_delta) AS delta FROM inventory_ledger "
        "WHERE op_type='current_substance.consume'"
    ) == {"count": 1, "delta": 0.0}


def test_aliquot_witness_preserves_live_calibration_in_outbox_and_historical_consumption(
    aliquot_scenario: _AliquotScenario,
) -> None:
    """分装后另行校准库存时，消费审计保留历史余额，Outbox 保留最新库存。"""

    scenario = aliquot_scenario
    command = scenario.settle((2.5, 1.5))
    historical_revision = scenario.balances()[scenario.source_material_uuid]["revision"]
    scenario.service.station_resources.transition_dispatch_permit(
        command.claim_uuid, target_state="released"
    )
    calibrated = BackendContainerContentService(scenario.inventory).update_current_substance(
        scenario.source_content_uuid,
        {
            "material_uuid": scenario.source_material_uuid,
            "name": "母液",
            "components": [],
            "quantity": 8,
            "quantity_unit": "mL",
            "physical_state": "liquid",
            "expected_revision": historical_revision,
            "reason": "分装后独立校准当前库存",
        },
    )
    assert calibrated["quantity"] == 8
    assert calibrated["revision"] == historical_revision + 1
    live_balances = scenario.balances()

    facts = scenario.coordinator.consume_successful_job(
        task_uuid=TASK_UUID, job_uuid=JOB_UUID, consumptions=[]
    )
    assert facts[0]["actual_quantity"] == 4
    assert facts[0]["balance_before"] == 10
    assert facts[0]["balance_after"] == 6
    assert scenario.balances() == live_balances
    outbox = scenario.inventory.query_all(
        "SELECT aggregate_version,payload_json FROM sync_outbox "
        "WHERE event_type='current_substance.consume' AND aggregate_id=?",
        (scenario.source_content_uuid,),
    )
    assert len(outbox) == 1
    result = json.loads(outbox[0]["payload_json"])["changes"]["result"]
    assert result["quantity"] == 8
    assert result["quantity_unit"] == "mL"
    assert result["revision"] == calibrated["revision"]
    assert outbox[0]["aggregate_version"] == calibrated["revision"]
    assert result["consumption"]["balance_before"] == 10
    assert result["consumption"]["balance_after"] == 6
    assert scenario.coordinator.consume_successful_job(
        task_uuid=TASK_UUID, job_uuid=JOB_UUID, consumptions=[]
    ) == facts
    assert scenario.balances() == live_balances
    assert scenario.inventory.query_all(
        "SELECT aggregate_version,payload_json FROM sync_outbox "
        "WHERE event_type='current_substance.consume' AND aggregate_id=?",
        (scenario.source_content_uuid,),
    ) == outbox
