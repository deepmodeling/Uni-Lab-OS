"""运行预检复用当前图，查询真实库存，不创建运行记录。"""
from uuid import uuid4

from fastapi.testclient import TestClient

from unilabos.client.materials import LocalMaterialsClient
from unilabos.protocol.materials import InventoryLotInbound, InventoryMutation, ResourceTemplateWrite
from unilabos.server.api.runtime.workflow import create_workflow_app
from unilabos.server.services.materials import MaterialsService
from unilabos.server.services.runtime.workflow.service import WorkflowService


def test_preflight_reports_shortage_offline_and_replenishment_without_writes(tmp_path):
    with MaterialsService(tmp_path / "materials.db") as materials:
        workflow = WorkflowService(":memory:")
        try:
            materials.put_template(InventoryMutation(command_uuid=str(uuid4()), effect_key="template", operation="put_template"),
                                   ResourceTemplateWrite(template_uuid="stock", name="stock"))
            created = workflow.create_workflow(name="预检", tags=[], description=None, meta_data={})
            node_uuid = str(uuid4())
            workflow.save_graph(created["uuid"], revision=created["revision"], nodes=[{
                "uuid": node_uuid, "name": "注液", "type": "device_action", "material_uuid": str(uuid4()),
                "action_name": "dose", "action_type": "UniLabJsonCommand", "param": {}, "pose": {"x": 0, "y": 0},
                "meta_data": {"target_device_id": "pump", "inventory_requirements": [
                    {"key": "stock", "kind": "lot", "template_uuid": "stock", "quantity": 1, "unit": "ml"},
                ]},
            }], edges=[])
            endpoints = []
            workflow.configure_preflight(materials=LocalMaterialsClient(materials), endpoints=lambda: endpoints)
            submitted = []
            workflow.set_task_submitter(submitted.append)
            client = TestClient(create_workflow_app(workflow))
            path = f'/api/v1/workflows/{created["uuid"]}/run-preflight'
            before = (workflow.connection.execute("SELECT total_changes()").fetchone()[0], materials.connection.execute("SELECT total_changes()").fetchone()[0])
            response = client.post(path, json={})
            assert response.status_code == 200, response.text
            report = response.json()["data"]
            assert report["status"] == "temporarily_unavailable"
            assert {item["code"] for item in report["checks"] if item["blocking"]} == {"inventory_shortage", "device_action_unavailable"}
            assert before == (workflow.connection.execute("SELECT total_changes()").fetchone()[0], materials.connection.execute("SELECT total_changes()").fetchone()[0])
            assert submitted == []
            assert workflow.list_tasks(page=1, page_size=10)["items"] == []
            endpoints.append({"state": "online", "action_capabilities": [{"device_uuid": "pump", "action_name": "dose", "state": "active"}]})
            materials.inbound_inventory_lot(InventoryMutation(command_uuid=str(uuid4()), effect_key="stock", operation="inbound_inventory_lot"),
                                           InventoryLotInbound(template_uuid="stock", quantity=1000, unit="ul"))
            report = client.post(path, json={}).json()["data"]
            assert report["status"] == "runnable_now"
            assert report["summary"]["deferred_check_count"] >= 1
            assert not materials.list_inventory_reservations()
            assert client.post(path, json={"expected_revision": 999}).json()["data"]["status"] == "invalid"
        finally:
            workflow.close()


def test_preflight_missing_entry_input_is_invalid_and_single_node_selection_matches_plan():
    service = WorkflowService(":memory:")
    try:
        workflow = service.create_workflow(name="输入", tags=[], description=None, meta_data={})
        node_uuid = str(uuid4())
        service.save_graph(workflow["uuid"], revision=1, nodes=[{
            "uuid": node_uuid, "name": "动作", "type": "device_action", "action_name": "run",
            "material_uuid": str(uuid4()), "param": {}, "pose": {"x": 0, "y": 0},
        }], edges=[])
        result = service.preflight_workflow(workflow_uuid=workflow["uuid"], run_mode="single_node", target_node_uuid=str(uuid4()), input_value={})
        assert result["status"] == "invalid"
        assert result["checks"][0]["code"] == "invalid_execution_plan"
    finally:
        service.close()
