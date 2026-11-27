"""按任务读取物料变化历史：搬运登记、数量消耗、标为已使用，并标注产生它的步骤。"""

from __future__ import annotations

import json
import time
from pathlib import Path

from fastapi.testclient import TestClient

from unilabos.app.scheduler.inventory.service import InventoryService
from unilabos.app.workflow_api import create_workflow_app
from unilabos.workflow.quantity_inventory import WorkflowQuantityInventory
from unilabos.workflow.service import WorkflowService

from .quantity_inventory_test_support import JOB_UUID, NODE_UUID, TASK_UUID, _inventory, _workflow
from .test_quantity_inventory_runtime import _create_task, _InventoryReadBridge

OTHER_TASK = "30000000-0000-4000-8000-0000000000ff"
OTHER_JOB = "40000000-0000-4000-8000-0000000000ff"


class _Bridge(_InventoryReadBridge):
    def list_task_material_events(self, task_uuid, job_uuids, *, since_ms=0):
        return self._coordinator.list_task_material_events(task_uuid, job_uuids, since_ms=since_ms)


def _ledger(store, *, op_type, aggregate_id, delta, task=None, job=None, causation="", at_ms):
    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO inventory_ledger(occurred_at, op_type, aggregate_type, aggregate_id, delta_json, "
            "actor, reason, causation_id, entry_uuid, material_uuid, subject_type, "
            "workflow_task_uuid, workflow_node_job_uuid) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (at_ms, op_type, "material", aggregate_id, json.dumps(delta, ensure_ascii=False), "test",
             delta.get("reason", ""), causation, f"{op_type}:{at_ms}", aggregate_id, "material", task, job),
        )


def test_task_material_events_list_moves_consumption_and_used_with_step(tmp_path: Path) -> None:
    inventory_store, material_uuid, info_uuid, reagent_uuid = _inventory(tmp_path)
    workflow_store, graph, prepared = _workflow(
        tmp_path / "workflow.db", reagent_info_uuid=info_uuid, material_uuid=material_uuid
    )
    coordinator = WorkflowQuantityInventory(workflow_store, InventoryService(inventory_store))
    service = WorkflowService(workflow_store, task_scheduler_bridge=_Bridge(coordinator))  # type: ignore[arg-type]
    try:
        _create_task(workflow_store, coordinator, graph=graph, prepared=prepared, reagent_uuid=reagent_uuid)
        coordinator.consume_successful_job(task_uuid=TASK_UUID, job_uuid=JOB_UUID, consumptions=[])
        later = int(time.time() * 1000) + 5_000
        move = {"changes": {"name": "薄层板 3", "site_name_before": "上料仓 / L03",
                            "site_name_after": "sampling / spotting",
                            "parent_uuid_before": "owner-a", "parent_uuid_after": "owner-b"}}
        _ledger(inventory_store, op_type="material.transfer", aggregate_id=material_uuid, delta=move,
                task=TASK_UUID, job=JOB_UUID, at_ms=later)
        _ledger(inventory_store, op_type="material.transfer", aggregate_id=material_uuid, delta=move,
                task=OTHER_TASK, job=OTHER_JOB, at_ms=later + 1)
        _ledger(inventory_store, op_type="instance.used", aggregate_id=material_uuid,
                delta={"reason": "已装样品粉末", "previous_status": "reserved"}, causation=JOB_UUID,
                at_ms=later + 2)
        _ledger(inventory_store, op_type="instance.used", aggregate_id=material_uuid,
                delta={"reason": "别的任务", "previous_status": "reserved"}, causation=OTHER_JOB,
                at_ms=later + 3)

        events = service.list_task_material_events(TASK_UUID)

        assert [event["kind"] for event in events] == ["consumed", "moved", "used"]
        consumed, moved, used = events
        assert consumed["quantity"] == 2 and consumed["quantity_unit"] == "mL"
        assert consumed["balance_after"] == 8
        assert moved["material_name"] == "薄层板 3"
        assert (moved["from_site"], moved["to_site"]) == ("上料仓 / L03", "sampling / spotting")
        assert (moved["from_owner_uuid"], moved["to_owner_uuid"]) == ("owner-a", "owner-b")
        assert used["reason"] == "已装样品粉末"
        for event in events:
            assert event["workflow_task_uuid"] == TASK_UUID
            assert event["workflow_node_job_uuid"] == JOB_UUID
            assert event["workflow_node_uuid"] == NODE_UUID
            assert event["node_name"] == "分液"
            assert event["step_path"] == []
            assert event["occurred_at"].endswith("Z")

        response = TestClient(create_workflow_app(service)).get(
            f"/api/v1/workflow-tasks/{TASK_UUID}/material-events"
        )
        assert response.status_code == 200
        assert response.json() == {"code": 0, "data": events}
        missing = TestClient(create_workflow_app(service)).get(
            f"/api/v1/workflow-tasks/{OTHER_TASK}/material-events"
        )
        # 与本组接口一致：不存在以业务码 3002 返回。
        assert missing.json()["code"] == 3002
    finally:
        service.close()
        inventory_store.close()
