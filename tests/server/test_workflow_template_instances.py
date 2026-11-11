"""浏览器模板实例：独立身份、设备换绑、参数覆盖、失败清理与 dev 目录合同。"""
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from unilabos.registry.workflows import workflow, build_workflow_template_payload, get_registered_workflows
from unilabos.server.api.runtime.workflow import create_workflow_app
from unilabos.server.services.runtime.workflow.service import WorkflowService


@pytest.fixture()
def stack(monkeypatch):
    from unilabos.registry.workflows import _registered_workflows
    snapshot = dict(_registered_workflows)

    @workflow(display_name="条件流程", compiler="ast")
    def flow(ctx):
        quality = ctx.run("reactor/check_quality", {"threshold": 0.85})
        if quality.ready:
            ctx.run("reactor/report", {})
        else:
            ctx.run("reactor/polish", {"passes": 2})
        with ctx.loop_while(ctx.device_state("sensor", "ready", "==", False), interval_seconds=0.5):
            pass

    definition = next(item for item in get_registered_workflows().values() if item.source_path.endswith("flow"))
    template = build_workflow_template_payload(definition)
    registry = SimpleNamespace(get_workflow_template=lambda identity: template if identity == template["uuid"] else None,
                               list_workflow_templates=lambda: [template])
    monkeypatch.setattr("unilabos.server.services.runtime.registry.get_registry_service", lambda: registry)
    materials = SimpleNamespace(list_materials=lambda **kwargs: [SimpleNamespace(
        resource_type="device", resource_id="lab/second-reactor", template_name="demo.reactor", material_uuid=str(uuid4()))])
    monkeypatch.setattr("unilabos.server.backend.composition.get_materials_service", lambda: materials)
    service = WorkflowService(":memory:")
    try:
        with TestClient(create_workflow_app(service)) as client:
            yield client, service, template
    finally:
        service.close()
        _registered_workflows.clear()
        _registered_workflows.update(snapshot)


def test_create_independent_instances_with_rebound_conditions_and_parameters(stack):
    client, service, template = stack
    original = deepcopy(template)
    body = {"template_uuid": template["uuid"], "name": "前端副本", "bindings": {"reactor": "lab/second-reactor", "sensor": "slave-sensor"},
            "node_params": {"step-0": {"threshold": 0.95}}, "site_binding_mode": "preserve"}
    created = [client.post("/api/v1/workflows/template-instances", json=body) for _ in range(2)]
    assert all(item.status_code == 201 for item in created), [item.text for item in created]
    ids = [item.json()["data"]["workflow"]["uuid"] for item in created]
    assert ids[0] != ids[1]
    graphs = [service.get_graph(identity) for identity in ids]
    assert set(node["uuid"] for node in graphs[0]["nodes"]).isdisjoint(node["uuid"] for node in graphs[1]["nodes"])
    for graph in graphs:
        nodes = graph["nodes"]
        assert nodes[0]["param"] == {"threshold": 0.95}
        assert all(node["meta_data"]["target_device_id"] == "lab/second-reactor" for node in nodes if node["type"] == "device_action")
        condition = next(node for node in nodes if node["type"] == "condition")
        assert nodes[0]["uuid"] in str(condition["param"])
        assert all(node["execution_policy"]["condition_guards"][0]["node_uuid"] == condition["uuid"] for node in nodes if node["execution_policy"].get("condition_guards"))
        assert nodes[-1]["param"]["condition"]["device_id"] == "slave-sensor"
    assert template == original
    listing = client.get("/api/v1/workflows?page_size=1").json()["data"]
    assert listing["has_more"] is True
    assert listing["items"][0]["workflow_type"] == "workflow"
    assert listing["items"][0]["status"] == "source"
    assert client.get("/api/v1/workflows?page_size=1&page=2").json()["data"]["has_more"] is False


@pytest.mark.parametrize("changes", [
    {"node_params": {"ghost": {}}}, {"node_params": {"step-1": {"condition": True}}},
    {"node_params": {"step-0": []}}, {"bindings": {"reactor": 123}},
    {"bindings": {"ghost": "reactor"}}, {"bindings": {"reactor": " "}},
    {"site_binding_mode": "resolve"}, {"unexpected": True},
])
def test_reject_invalid_edits_without_creating_workflow(stack, changes):
    client, service, template = stack
    response = client.post("/api/v1/workflows/template-instances", json={"template_uuid": template["uuid"], **changes})
    assert response.json()["code"] != 0, response.text
    assert service.list_workflows()["total"] == 0


def test_failed_graph_save_cleans_up_new_instance(stack, monkeypatch):
    client, service, template = stack
    from unilabos.server.services.runtime.workflow.service import WorkflowError
    def reject(*args, **kwargs):
        raise WorkflowError("invalid_input")
    monkeypatch.setattr(service, "save_graph", reject)
    response = client.post("/api/v1/workflows/template-instances", json={"template_uuid": template["uuid"]})
    assert response.json()["code"] != 0
    assert service.list_workflows()["total"] == 0


def test_catalog_preserves_device_ids_and_script_endpoint_stays_idempotent(stack):
    client, service, template = stack
    catalog = client.get("/api/v1/workflows/template-catalog").json()["data"]
    assert catalog["devices"] == [{"device_id": "lab/second-reactor", "device_class": "demo.reactor"}]
    body = {"template_uuid": template["uuid"], "site_binding_mode": "preserve"}
    results = [client.post("/api/v1/workflows/from-template", json=body).json() for _ in range(2)]
    assert results[0]["data"]["workflow"]["uuid"] == results[1]["data"]["workflow"]["uuid"]
    assert service.list_workflows()["total"] == 1


@pytest.mark.parametrize("change", ["delete", "replace", "new"])
def test_instance_params_cannot_rewire_dynamic_results(stack, change):
    client, service, template = stack
    reference = {"kind": "node_output", "node_key": "step-0", "data_key": "count"}
    template["nodes"][2]["param"] = {"value": reference, "label": "默认"}
    override = {"value": reference, "label": "新值"}
    if change == "delete":
        override.pop("value")
    elif change == "replace":
        override["value"] = 5
    else:
        override["label"] = reference
    response = client.post("/api/v1/workflows/template-instances", json={
        "template_uuid": template["uuid"], "node_params": {"step-2": override}})
    assert response.json()["code"] != 0, response.text
    assert service.list_workflows()["total"] == 0


def test_instance_can_adjust_defaults_while_preserving_result_bindings(stack):
    client, service, template = stack
    reference = {"kind": "node_output", "node_key": "step-0", "data_key": "count"}
    template["nodes"][2]["param"] = {"value": reference, "label": "默认"}
    response = client.post("/api/v1/workflows/template-instances", json={
        "template_uuid": template["uuid"], "node_params": {"step-2": {"value": reference, "label": "新值"}}})
    assert response.status_code == 201, response.text
    graph = service.get_graph(response.json()["data"]["workflow"]["uuid"])
    assert graph["nodes"][2]["param"] == {"value": {"kind": "node_output",
        "workflow_node_uuid": graph["nodes"][0]["uuid"], "data_key": "count"}, "label": "新值"}
