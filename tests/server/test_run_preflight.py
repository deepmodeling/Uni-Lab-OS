"""运行预检复用当前图，查询真实库存，不创建运行记录。"""
from uuid import uuid4
from contextlib import closing
from unittest.mock import Mock

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


def test_preflight_authority_failure_is_a_blocked_report():
    with closing(WorkflowService(":memory:")) as service:
        workflow = service.create_workflow(name="离线预检", tags=[], description=None, meta_data={})
        service.save_graph(workflow["uuid"], revision=1, nodes=[{
            "uuid": str(uuid4()), "name": "动作", "type": "device_action", "action_name": "run",
            "material_uuid": str(uuid4()), "param": {}, "pose": {"x": 0, "y": 0},
        }], edges=[])
        materials = Mock()
        materials.get_material.side_effect = RuntimeError("库存权威暂不可达")
        service.configure_preflight(materials=materials, endpoints=lambda: [])
        report = service.preflight_workflow(
            workflow_uuid=workflow["uuid"], run_mode="normal", target_node_uuid=None, input_value={},
        )
        assert report["status"] == "temporarily_unavailable"
        assert any(item["code"] == "device_identity_unavailable" and item["blocking"] for item in report["checks"])


def test_submission_checks_preflight_revision_without_writes_and_replays_original_task():
    with closing(WorkflowService(":memory:")) as service:
        workflow = service.create_workflow(name="修订检查", tags=[], description=None, meta_data={})
        nodes = [{
            "uuid": str(uuid4()), "name": "动作", "type": "device_action", "action_name": "run",
            "material_uuid": str(uuid4()), "param": {}, "pose": {"x": 0, "y": 0},
        }]
        service.save_graph(workflow["uuid"], revision=1, nodes=nodes, edges=[])
        submitted = []
        service.set_task_submitter(submitted.append)
        client = TestClient(create_workflow_app(service))
        report = client.post(f'/api/v1/workflows/{workflow["uuid"]}/run-preflight', json={}).json()["data"]
        checked_revision = report["workflow_revision"]
        service.save_graph(workflow["uuid"], revision=checked_revision, nodes=nodes, edges=[])
        before = service.connection.execute("SELECT total_changes()").fetchone()[0]
        body = {"workflow_uuid": workflow["uuid"], "expected_revision": checked_revision, "idempotency_key": "reviewed-run"}
        response = client.post("/api/v1/workflow-tasks", json=body)
        assert response.json()["code"] == 3003, response.text
        assert service.connection.execute("SELECT total_changes()").fetchone()[0] == before
        assert submitted == []
        assert service.list_tasks(page=1, page_size=10)["items"] == []
        body["expected_revision"] = checked_revision + 1
        response = client.post("/api/v1/workflow-tasks", json=body)
        assert response.status_code == 201, response.text
        task = response.json()["data"]
        service.save_graph(workflow["uuid"], revision=checked_revision + 1, nodes=nodes, edges=[])
        replay = client.post("/api/v1/workflow-tasks", json=body)
        assert replay.json()["data"]["uuid"] == task["uuid"]
        assert submitted == [task["uuid"]]
        body["expected_revision"] = checked_revision + 2
        assert client.post("/api/v1/workflow-tasks", json=body).json()["code"] == 3003


def test_preflight_names_why_a_skipped_device_is_unavailable():
    """FRAMEWORK_GAPS #22：Host 因 backend 不支持跳过的设备，预检写明原因而不是笼统的"离线"。"""

    with closing(WorkflowService(":memory:")) as service:
        workflow = service.create_workflow(name="跳过设备", tags=[], description=None, meta_data={})
        service.save_graph(workflow["uuid"], revision=1, nodes=[{
            "uuid": str(uuid4()), "name": "规划", "type": "device_action", "action_name": "plan",
            "material_uuid": str(uuid4()), "param": {}, "pose": {"x": 0, "y": 0},
            "meta_data": {"target_device_id": "arm"},
        }], edges=[])
        reason = "设备类只支持 backend ros2，当前 backend 为 hostlink，已跳过"
        endpoints = [{"state": "online", "action_capabilities": [], "device_routes": [{
            "device_uuid": "arm", "enabled": False,
            "config": {"unavailable": {"reason": reason, "supported_backends": ["ros2"]}}}]}]
        service.configure_preflight(materials=None, endpoints=lambda: endpoints)
        report = service.preflight_workflow(workflow_uuid=workflow["uuid"], run_mode="normal",
                                            target_node_uuid=None, input_value={})
        device = next(item for item in report["checks"] if item["type"] == "device")
        assert device["code"] == "device_action_unavailable"
        assert reason in device["message"]
        assert device["details"]["unavailable"]["supported_backends"] == ["ros2"]
