"""V1.0 设备与注册表合同（统一分支）：节点模板投影、设备目录、单动作运行、旧版 job、设备状态。

请求示例取自 V1 控制台 ``frontend/src/lib/edgeClient.ts``（``loadActionTemplates`` /
``loadDeviceRuntimeCatalog`` / ``ensureWorkflowSequenceEdges``）与 V1 ``app/web/api.py``。
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from unilabos.registry.template_identity import action_handle_uuid, action_template_uuid

GOAL = {"type": "object", "required": ["volume"], "properties": {
    "volume": {"type": "number", "title": "体积"}, "speed": {"type": "integer"}}}
RESULT = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
ACTION = {
    "type": "UniLabJsonCommand",
    "display_name": "加样",
    "goal": {"volume": "volume", "speed": "speed"},
    "goal_default": {"volume": 1, "speed": 2},
    "schema": {"type": "object", "properties": {"goal": GOAL, "result": RESULT}},
    "handles": {"input": [], "output": []},
}


def _ok(response, status: int = 200):
    assert response.status_code == status, response.text
    body = response.json()
    assert body["code"] == 0, body
    return body.get("data")


def _code(response) -> int:
    assert response.status_code == 200, response.text
    return response.json()["code"]


def endpoint(device_id: str, action: str, *, state: str = "online", availability: str = "free") -> dict:
    return {
        "endpoint_uuid": "ep-1", "host_uuid": "host-1", "instance_name": "lab-pc", "transport": "hostlink",
        "state": state, "device_routes": [],
        "action_capabilities": [{
            "device_uuid": device_id, "action_name": action, "action_type": "UniLabJsonCommand",
            "state": "active", "availability": availability, "descriptor": {},
        }],
    }


@pytest.fixture
def lab(tmp_path, monkeypatch):
    from unilabos.protocol.materials import (
        InventoryMutation, MaterialIdentityWrite, MaterialNodeCreate, MaterialTreeCreate, ResourceTemplateWrite,
    )
    from unilabos.server.api.runtime.registry import install_registry_api
    from unilabos.server.api.runtime.workflow import install_workflow_api
    from unilabos.server.services.materials import MaterialsService
    from unilabos.server.services.runtime.registry import RegistryService
    from unilabos.server.services.runtime.workflow.service import WorkflowService

    materials = MaterialsService(tmp_path / "materials.db")
    registry = RegistryService(tmp_path / "registry.db")
    workflow = WorkflowService(tmp_path / "workflow.db")

    def mutation(operation):
        return InventoryMutation(command_uuid=str(uuid4()), effect_key=operation, operation=operation)

    materials.put_template(mutation("put_template"), ResourceTemplateWrite(
        template_uuid=str(uuid4()), name="pump", display_name="pump", resource_type="device", class_name="Resource"))
    tree = materials.create_tree(mutation("create_material_tree"), MaterialTreeCreate(nodes=[
        MaterialNodeCreate(client_ref="pump", identity=MaterialIdentityWrite(
            resource_id="pump-1", name="pump-1", display_name="一号泵", template_name="pump", resource_type="device")),
    ])).data
    registry.report([{"id": "pump", "registry_type": "device", "class": {
        "module": "lab.pump:Pump", "type": "python", "action_value_mappings": {"dose": ACTION},
        "status_types": {"pressure": "float"},
    }}], edge_uuid="test")
    endpoints = {"value": [endpoint("pump-1", "dose")]}
    monkeypatch.setattr("unilabos.server.backend.composition.get_materials_service", lambda: materials)
    monkeypatch.setattr("unilabos.server.services.runtime.registry.get_registry_service", lambda: registry)
    monkeypatch.setattr("unilabos.server.services.runtime.devices.read_online_endpoints", lambda: endpoints["value"])
    monkeypatch.setattr("unilabos.server.composition.get_server_services", lambda: None)
    monkeypatch.setattr("unilabos.server.api.edge_proxy.edge_proxy_enabled", lambda: False)
    app = FastAPI()
    install_workflow_api(app, workflow)
    install_registry_api(app)
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, workflow=workflow, materials=materials, registry=registry,
                              device_uuid=tree.nodes[0].material.material_uuid, endpoints=endpoints)
    workflow.close()
    registry.close()
    materials.close()


def test_node_templates_project_registry_actions(lab) -> None:
    client = lab.client
    template_uuid = action_template_uuid("pump", "dose")
    page = _ok(client.get("/api/v1/workflow-node-templates", params={"page": 1, "page_size": 100}))
    assert set(page) == {"items", "has_more", "page", "page_size"}
    assert page["has_more"] is False
    summary = next(item for item in page["items"] if item["uuid"] == template_uuid)
    assert summary["name"] == "dose" and summary["display_name"] == "加样"
    assert summary["node_type"] == "ILab" and summary["type"] == "UniLabJsonCommand"
    assert summary["resource_template"]["name"] == "pump"
    assert _ok(client.get("/api/v1/workflow-node-templates", params={"keyword": "nothing"}))["items"] == []
    assert _ok(client.get("/api/v1/workflow-node-templates", params={"node_type": "condition"}))["items"] == []

    detail = _ok(client.get(f"/api/v1/workflow-node-templates/{template_uuid}"))
    assert detail["template"]["uuid"] == template_uuid
    assert detail["template"]["meta_data"]["unilab"]["parameter_schema"] == GOAL
    handles = {(item["io_type"], item["handle_key"]): item for item in detail["handles"]}
    assert set(handles) == {("target", "ready"), ("source", "ready"), ("target", "volume"),
                            ("target", "speed"), ("source", "ok")}
    assert handles[("target", "volume")]["required"] is True
    assert handles[("target", "volume")]["meta_data"]["unilab"]["value_schema"]["type"] == "number"
    assert handles[("source", "ready")]["uuid"] == action_handle_uuid(template_uuid, io_type="source", handle_key="ready")
    assert _code(client.get(f"/api/v1/workflow-node-templates/{uuid4()}")) == 3002
    assert _code(client.get("/api/v1/workflow-node-templates/not-a-uuid")) == 1000


def test_template_nodes_and_ready_edges_are_executable_graphs(lab) -> None:
    client = lab.client
    template_uuid = action_template_uuid("pump", "dose")
    workflow = _ok(client.post("/api/v1/workflows", json={"name": "模板编排"}), 201)
    first = _ok(client.post(f"/api/v1/workflows/{workflow['uuid']}/nodes", json={
        "workflow_node_template_uuid": template_uuid, "material_uuid": lab.device_uuid, "pose": {"x": 0, "y": 0},
    }), 201)
    second = _ok(client.post(f"/api/v1/workflows/{workflow['uuid']}/nodes", json={
        "workflow_node_template_uuid": template_uuid, "material_uuid": lab.device_uuid, "param": {"volume": 5},
    }), 201)
    nodes = second["nodes"]
    assert [node["type"] for node in nodes] == ["ILab", "ILab"]
    assert {node["action_name"] for node in nodes} == {"dose"}
    assert nodes[0]["param"] == {"volume": 1, "speed": 2} and nodes[1]["param"] == {"volume": 5}
    assert all(node["meta_data"]["target_device_id"] == "pump-1" for node in nodes)
    assert first["node_templates"][0]["uuid"] == template_uuid
    edge = _ok(client.post(f"/api/v1/workflows/{workflow['uuid']}/edges", json={
        "source_node_uuid": nodes[0]["uuid"], "target_node_uuid": nodes[1]["uuid"],
        "source_handle_uuid": action_handle_uuid(template_uuid, io_type="source", handle_key="ready"),
        "target_handle_uuid": action_handle_uuid(template_uuid, io_type="target", handle_key="ready"),
        "description": "实验操作顺序依赖", "meta_data": {"unilab": {"generated_by": "operation-builder"}},
    }), 201)
    assert len(edge["edges"]) == 1 and edge["workflow"]["revision"] == 4
    plan = lab.workflow.preflight_workflow(
        workflow_uuid=workflow["uuid"], run_mode="normal", target_node_uuid=None, input_value={}, expected_revision=None,
    )
    assert plan["workflow_uuid"] == workflow["uuid"]
    assert _code(client.post(f"/api/v1/workflows/{workflow['uuid']}/nodes", json={
        "workflow_node_template_uuid": str(uuid4()),
    })) == 3002


def test_device_catalog_routes(lab) -> None:
    client = lab.client
    devices = _ok(client.get("/api/v1/devices"))
    assert len(devices) == 1
    device = devices[0]
    assert device["binding"]["local_id"] == "pump-1" and device["binding"]["material_uuid"] == lab.device_uuid
    assert device["material"]["uuid"] == lab.device_uuid and device["material"]["class"] == "pump"
    assert device["online"] is True and device["edge_status"] == "online" and device["dispatchable"] is True
    assert device["actions"] == [{"name": "dose", "type": "UniLabJsonCommand",
                                  "workflow_node_template_uuid": action_template_uuid("pump", "dose")}]

    catalog = _ok(client.get("/api/v1/authoring/device-catalog"))
    assert catalog["schemaVersion"] == "device-catalog/v1"
    item = catalog["items"][0]
    assert item["id"] == "pump-1" and item["materialUuid"] == lab.device_uuid and item["online"] is True
    assert item["stateSchema"] == {"pressure": {"type": "number"}}
    assert item["actions"][0]["id"] == "dose" and item["actions"][0]["inputSchema"] == GOAL

    online = _ok(client.get("/api/v1/online-devices"))
    assert online["total_count"] == 1 and online["online_devices"]["pump-1"]["uuid"] == lab.device_uuid
    actions = _ok(client.get("/api/v1/devices/pump-1/actions"))
    assert actions["action_count"] == 1 and actions["actions"]["dose"]["is_busy"] is False
    assert _ok(client.get(f"/api/v1/devices/{lab.device_uuid}/actions"))["device_id"] == "pump-1"
    schema = _ok(client.get("/api/v1/devices/pump-1/actions/dose/schema"))
    assert schema["schema"] == ACTION["schema"] and schema["goal_default"] == {"volume": 1, "speed": 2}
    assert _code(client.get("/api/v1/devices/ghost/actions")) == 4001
    assert _code(client.get("/api/v1/devices/pump-1/actions/ghost/schema")) == 4001
    every = _ok(client.get("/api/v1/actions"))
    assert every["device_count"] == 1 and every["total_action_count"] == 1

    lab.endpoints["value"] = [endpoint("pump-1", "dose", state="offline")]
    offline = _ok(client.get("/api/v1/devices"))[0]
    assert offline["online"] is False and offline["edge_status"] == "offline" and offline["dispatchable"] is False
    assert _ok(client.get("/api/v1/online-devices"))["total_count"] == 0


def test_device_action_runs_and_legacy_job_routes(lab) -> None:
    client = lab.client
    body = {
        "material_uuid": lab.device_uuid, "workflow_node_template_uuid": action_template_uuid("pump", "dose"),
        "param": {"volume": 3}, "idempotency_key": "run-1",
    }
    created = client.post("/api/v1/device-action-runs", json=body)
    data = _ok(created, 201)
    assert data["created"] is True and data["task"]["execution_kind"] == "ad_hoc_device_action"
    assert data["job"]["workflow_task_uuid"] == data["task"]["uuid"]
    replay = _ok(client.post("/api/v1/device-action-runs", json=body))
    assert replay["created"] is False and replay["task"]["uuid"] == data["task"]["uuid"]
    assert _code(client.post("/api/v1/device-action-runs", json={**body, "param": {"volume": 9}})) == 3003
    assert _code(client.post("/api/v1/device-action-runs", json={**body, "material_uuid": str(uuid4())})) == 1000
    assert _code(client.post("/api/v1/device-action-runs", json={**body, "device_id": "pump-1"})) == 1000

    job = _ok(client.post("/api/v1/job/add", json={"device_id": "pump-1", "action": "dose", "action_args": {"volume": 1}}))
    assert job["status"] == 1 and job["jobId"]
    status = _ok(client.get(f"/api/v1/job/{job['jobId']}/status"))
    assert status["jobId"] == job["jobId"] and status["status"] in {1, 2}
    assert _ok(client.get(f"/api/v1/job/{uuid4()}/status"))["status"] == 0
    assert _code(client.post("/api/v1/job/add", json={"action": "dose"})) == 1000


def test_device_state_routes_read_the_telemetry_projection(tmp_path) -> None:
    from unilabos.server.api.runtime.diagnostics import create_backend_router
    from unilabos.server.backend.telemetry import TelemetryDeviceStateProjection
    from unilabos.server.services.telemetry import TelemetryService

    telemetry = TelemetryService(tmp_path / "telemetry.db")
    projection = TelemetryDeviceStateProjection(telemetry, endpoint_uuid="ep-1")
    backend = SimpleNamespace(device_state=projection)
    app = FastAPI()
    app.include_router(create_backend_router(lambda: None, lambda: backend))
    try:
        with TestClient(app) as client:
            report = _ok(client.post("/api/v1/device-state/report", json={
                "device_id": "pump-1", "properties": {"pressure": 1.5, "on": True}}))
            assert report == {"device_id": "pump-1", "changed": {"pressure": True, "on": True}}
            assert _ok(client.post("/api/v1/device-state/report", json={
                "device_id": "pump-1", "properties": {"pressure": 1.5}}))["changed"] == {"pressure": False}
            current = _ok(client.get("/api/v1/device-state"))
            assert current["devices"]["pump-1"]["pressure"]["value"] == 1.5 and "stats" in current
            one = _ok(client.get("/api/v1/device-state/pump-1"))
            assert one["properties"]["on"]["value"] is True
            history = _ok(client.get("/api/v1/device-state/pump-1/history", params={"property": "pressure"}))
            assert [entry["value"] for entry in history["entries"]] == [1.5]
            assert len(_ok(client.get("/api/v1/device-state/history"))["entries"]) == 2
            assert _code(client.get("/api/v1/device-state/ghost")) == 4001
            assert _code(client.post("/api/v1/device-state/report", json={
                "device_id": "pump-1", "properties": {"bad": [1]}})) == 1000
        no_backend = FastAPI()
        no_backend.include_router(create_backend_router(lambda: None, lambda: None))
        with TestClient(no_backend) as client:
            assert _code(client.get("/api/v1/device-state")) == 5001
    finally:
        telemetry.close()
