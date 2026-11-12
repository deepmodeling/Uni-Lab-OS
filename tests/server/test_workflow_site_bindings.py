"""Site 标签导入：目标作用域、草稿确认以及失败的事务边界。"""
import json
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from unilabos.server.services.runtime.workflow.site_bindings import SiteBindingError, resolve_workflow_sites


ACTION = {
    "placeholder_keys": {"site": "unilabos_sites"},
    "goal_default": {"site": "T1"},
    "schema": {"properties": {"goal": {"type": "object", "required": [], "properties": {"site": {"type": "string"}}}}},
}
REGISTRY = SimpleNamespace(action_definition=lambda cls, name: ACTION if cls == "bench" and name == "load" else None)


def aggregate(uuid, name, *, parent=None, device=False, site=None):
    return {"material": {"material_uuid": uuid, "name": name, "resource_id": name,
                         "template_name": "bench" if device else "deck", "resource_type": "device" if device else "resource",
                         "parent_material_uuid": parent},
            "sites": [{"site_uuid": site, "label": "T1"}] if site else []}


@pytest.fixture
def materials():
    return [aggregate("dev", "bench", device=True), aggregate("deck", "deck", parent="dev", site="s1"),
            aggregate("other", "other", device=True, site="s2")]


def node(value="T1"):
    return {"uuid": str(uuid4()), "type": "device_action", "name": "装载", "action_name": "load",
            "material_uuid": "dev", "meta_data": {"target_device_id": "bench"}, "param": {"site": value, "tips_site": "T1"}}


def resolve(nodes, materials, **kwargs):
    return resolve_workflow_sites(nodes, registry=REGISTRY, materials=materials, **kwargs)


def test_unique_match_scoped_to_device_and_preserves_plain_string(materials):
    draft = node()
    before = deepcopy(draft)
    result = resolve([draft], materials)[0]
    assert result["param"] == {"site": "s1", "tips_site": "T1"}
    assert result["meta_data"]["site_bindings"]["site"] == {
        "site_uuid": "s1", "owner_material_uuid": "deck", "device_id": "bench", "action_name": "load"}
    assert draft == before


def test_ambiguous_labels_do_not_pick_first(materials):
    materials.append(aggregate("deck2", "deck2", parent="dev", site="s3"))
    with pytest.raises(SiteBindingError, match="匹配到 2 项"):
        resolve([node()], materials)
    explicit = node()
    explicit["meta_data"]["site_binding_owners"] = {"site": "deck2"}
    assert resolve([explicit], materials)[0]["param"]["site"] == "s3"


def test_target_mount_resource_not_source_resource(materials):
    draft = node()
    draft["param"].update(mount_resource={"uuid": "other"}, resource={"uuid": "deck"})
    assert resolve([draft], materials)[0]["param"]["site"] == "s2"
    draft["param"]["mount_resource"] = {"uuid": "deleted", "id": "other"}
    with pytest.raises(SiteBindingError, match="目标物料 UUID"):
        resolve([draft], materials)


@pytest.mark.parametrize("value", ["missing", "s2", str(uuid4()), 12])
def test_missing_wrong_owner_and_invalid_type_fail(materials, value):
    with pytest.raises(SiteBindingError, match="节点 '装载' 参数 'site'"):
        resolve([node(value)], materials)


def test_optional_empty_required_empty_and_upstream_mapping(materials):
    omitted = node()
    del omitted["param"]["site"]
    assert resolve([omitted], materials)[0]["param"]["site"] == "s1"
    assert resolve([node("")], materials)[0]["param"]["site"] == ""
    required = deepcopy(ACTION)
    required["schema"]["properties"]["goal"]["required"] = ["site"]
    with pytest.raises(SiteBindingError, match="请绑定"):
        resolve_workflow_sites([node("")], materials=materials,
                               registry=SimpleNamespace(action_definition=lambda *_: required))
    draft = node("upstream-value")
    assert resolve([draft], materials, mapped_paths={draft["uuid"]: ["site"]})[0]["param"]["site"] == "upstream-value"


def test_disabled_parent_and_schema_marker_not_field_name(materials):
    draft = node("missing")
    parent = {"uuid": str(uuid4()), "disabled": True}
    draft["parent_uuid"] = parent["uuid"]
    assert resolve([parent, draft], materials)[1]["param"]["site"] == "missing"
    draft = node("T1")
    draft["action_name"] = "plain_string"
    assert resolve_workflow_sites([draft], materials=materials, registry=SimpleNamespace(action_definition=lambda *_: {}))[0]["param"]["site"] == "T1"


def test_unknown_device_or_schema_is_not_reported_as_resolved(materials):
    draft = node()
    draft["meta_data"]["target_device_id"] = "not-registered"
    with pytest.raises(SiteBindingError, match="尚未就绪"):
        resolve([draft], materials)
    with pytest.raises(SiteBindingError, match="尚未就绪"):
        resolve_workflow_sites([node()], registry=None, materials=materials)


def endpoint(device_id, action_name, descriptor):
    return {"state": "online", "action_capabilities": [{
        "device_uuid": device_id, "action_name": action_name, "state": "active", "descriptor": descriptor,
    }]}


def test_subdevice_action_uses_runtime_identity_not_material_path(materials):
    materials.append(aggregate("sensor-uuid", "bench/sensor", device=True, parent="dev"))
    draft = node()
    draft.update(action_name="probe", material_uuid="placeholder-uuid", param={"value": 3})
    draft["meta_data"]["target_device_id"] = "sensor"
    snapshots = [endpoint("sensor", "probe", {"schema": {}, "goal_default": {"value": 1}})]
    assert resolve([draft], materials, endpoints=snapshots)[0]["param"] == {"value": 3}


def test_endpoint_site_declaration_still_requires_authoritative_owner(materials):
    draft = node()
    draft["meta_data"]["target_device_id"] = "sensor"
    snapshots = [endpoint("sensor", "load", ACTION)]
    with pytest.raises(SiteBindingError, match="目标物料/设备尚未登记"):
        resolve([draft], materials, endpoints=snapshots)
    draft["meta_data"]["site_binding_owners"] = {"site": "deck"}
    assert resolve([draft], materials, endpoints=snapshots)[0]["param"]["site"] == "s1"


@pytest.mark.parametrize("retired", [True, False])
def test_inactive_endpoint_capabilities_do_not_enable_import(materials, retired):
    draft = node()
    draft["meta_data"]["target_device_id"] = "sensor"
    snapshot = endpoint("sensor", "load", {})
    if retired:
        snapshot["action_capabilities"][0]["state"] = "retired"
    else:
        snapshot["state"] = "offline"
    with pytest.raises(SiteBindingError, match="尚未就绪"):
        resolve([draft], materials, endpoints=[snapshot])


def test_conflicting_endpoint_declarations_do_not_pick_first(materials):
    with pytest.raises(SiteBindingError, match="不一致"):
        resolve([node()], materials, endpoints=[endpoint("bench", "load", {}), endpoint("bench", "load", ACTION)])


@pytest.fixture
def authority(tmp_path, monkeypatch):
    from unilabos.protocol.materials import (
        InventoryMutation, MaterialTreeCreate, MaterialNodeCreate, MaterialIdentityWrite,
        ResourceTemplateWrite, SiteCreate,
    )
    from unilabos.server.services.materials import MaterialsService
    from unilabos.server.services.runtime.registry import RegistryService
    from unilabos.server.services.runtime.workflow.service import WorkflowService
    from unilabos.server.api.runtime.workflow import install_workflow_api

    mat = MaterialsService(tmp_path / "materials.db")
    registry = RegistryService(tmp_path / "registry.db")
    workflow = WorkflowService(tmp_path / "workflow.db")
    def mutation(operation):
        return InventoryMutation(command_uuid=str(uuid4()), effect_key=operation, operation=operation)
    for name in ["bench", "deck"]:
        mat.put_template(mutation("put_template"), ResourceTemplateWrite(
            template_uuid=str(uuid4()), name=name, display_name=name, resource_type="device" if name == "bench" else "resource", class_name="Resource"))
    tree = mat.create_tree(mutation("create_material_tree"), MaterialTreeCreate(nodes=[
        MaterialNodeCreate(client_ref="bench", identity=MaterialIdentityWrite(
            resource_id="bench", name="bench", template_name="bench", resource_type="device")),
        MaterialNodeCreate(client_ref="deck", parent_client_ref="bench", identity=MaterialIdentityWrite(
            resource_id="deck", name="deck", template_name="deck"),
            sites=[SiteCreate(template_name="deck", site_index=0, label="T1")]),
    ])).data
    template = {"id": "test:site", "uuid": str(uuid4()), "registry_type": "workflow", "display_name": "Site demo",
                "roles": [{"role": "bench", "label": "bench", "kind": "device", "device_id": "bench", "matches": ["bench"]}],
                "nodes": [{"key": "step", "kind": "action", "role": "bench", "action_name": "load", "name": "装载", "param": {"site": "T1"}}], "edges": []}
    registry.report([{"id": "bench", "registry_type": "device", "class": {"module": "test:Bench", "type": "python", "action_value_mappings": {"load": ACTION}}}, template], edge_uuid="test")
    monkeypatch.setattr("unilabos.server.backend.composition.get_materials_service", lambda: mat)
    monkeypatch.setattr("unilabos.server.services.runtime.registry.get_registry_service", lambda: registry)
    monkeypatch.setattr("unilabos.server.composition.get_server_services", lambda: None)
    monkeypatch.setattr("unilabos.server.api.edge_proxy.edge_proxy_enabled", lambda: False)
    app = FastAPI()
    install_workflow_api(app, workflow)
    with TestClient(app) as api:
        yield api, workflow, mat, tree, template
    workflow.close()
    registry.close()
    mat.close()


def test_api_template_and_graph_import_resolve_but_ui_preserves(authority):
    api, service, materials, tree, template = authority
    before = [item.model_dump() for item in materials.list_materials()]
    created = api.post("/api/v1/workflows/from-template", json={"template_uuid": template["uuid"]}).json()
    assert created["code"] == 0, created
    graph = service.get_graph(created["data"]["workflow"]["uuid"])
    site_uuid = next(item.sites[0].site_uuid for item in tree.nodes if item.sites)
    assert graph["nodes"][0]["param"]["site"] == site_uuid
    assert graph["nodes"][0]["meta_data"]["site_bindings"]["site"]["site_uuid"] == site_uuid
    graph["nodes"][0]["param"]["site"] = "T1"
    body = {"revision": graph["workflow"]["revision"], "nodes": graph["nodes"], "edges": [], "site_binding_mode": "preserve"}
    url = f'/api/v1/workflows/{graph["workflow"]["uuid"]}/graph'
    saved = api.put(url, json=body).json()
    assert saved["code"] == 0, saved
    assert saved["data"]["nodes"][0]["param"]["site"] == "T1"
    body["revision"] = saved["data"]["workflow"]["revision"]
    body.pop("site_binding_mode")
    resolved = api.put(url, json=body).json()
    assert resolved["code"] == 0, resolved
    assert resolved["data"]["nodes"][0]["param"]["site"] == site_uuid
    assert before == [item.model_dump() for item in materials.list_materials()]


def test_failed_import_leaves_existing_graph_unchanged(authority):
    api, service, _materials, _tree, template = authority
    created = api.post("/api/v1/workflows/from-template", json={"template_uuid": template["uuid"]}).json()
    workflow_uuid = created["data"]["workflow"]["uuid"]
    before = service.get_graph(workflow_uuid)
    nodes = deepcopy(before["nodes"])
    nodes[0]["param"]["site"] = "T404"
    failed = api.put(f"/api/v1/workflows/{workflow_uuid}/graph", json={
        "revision": before["workflow"]["revision"], "nodes": nodes, "edges": []}).json()
    assert failed["code"] == 1000 and "T404" in failed["error"]["msg"], failed
    assert service.get_graph(workflow_uuid) == before


@pytest.mark.parametrize("split", [False, True])
def test_http_import_reads_subdevice_endpoint_capabilities(authority, monkeypatch, split):
    api, service, _materials, _tree, template = authority
    snapshots = [endpoint("sensor", "probe", {"schema": {}, "goal_default": {"value": 1}})]
    runtime = SimpleNamespace(list_endpoint_snapshots=lambda **_: snapshots)
    monkeypatch.setattr("unilabos.server.composition.get_server_services", lambda: SimpleNamespace(runtime=runtime))
    if split:
        def forward(method, path, **kwargs):
            assert method == "GET" and path == "/api/v1/runtime/endpoints?state=online&limit=1000"
            # Host 自己的 runtime 路由输出统一信封，权威解包 data
            return SimpleNamespace(status_code=200, body_bytes=lambda: json.dumps({"code": 0, "data": snapshots}).encode())
        monkeypatch.setattr("unilabos.server.api.edge_proxy.edge_proxy_enabled", lambda: True)
        monkeypatch.setattr("unilabos.server.api.edge_proxy.edge_http", forward)
        runtime.list_endpoint_snapshots = lambda **_: pytest.fail("分进程不能读取调度权威自己的 endpoint 表")
    created = api.post("/api/v1/workflows/from-template", json={"template_uuid": template["uuid"]}).json()
    before = service.get_graph(created["data"]["workflow"]["uuid"])
    draft = deepcopy(before["nodes"][0])
    draft.update(action_name="probe", material_uuid=str(uuid4()), param={"value": 3})
    draft["meta_data"] = {"target_device_id": "sensor"}
    saved = api.put(f"/api/v1/workflows/{before['workflow']['uuid']}/graph", json={
        "revision": before["workflow"]["revision"], "nodes": [draft], "edges": [],
    }).json()
    assert saved["code"] == 0, saved
    assert saved["data"]["nodes"][0]["param"] == {"value": 3}


@pytest.mark.parametrize("response", [None,
    # Host 路由暂不可用：信封业务码 5001（HTTP 200），或没有信封正文的 HTTP 503
    SimpleNamespace(status_code=200, body_bytes=lambda: b'{"code":5001,"error":{"msg":"Host unavailable"}}'),
    SimpleNamespace(status_code=503, body_bytes=lambda: b''),
    # 不是信封 / data 不是列表 / 不是 JSON：一律当作无效响应
    SimpleNamespace(status_code=200, body_bytes=lambda: b'{}'),
    SimpleNamespace(status_code=200, body_bytes=lambda: b'{"code":0,"data":{}}'),
    SimpleNamespace(status_code=200, body_bytes=lambda: b'invalid-json'),
])
def test_unavailable_host_capabilities_do_not_modify_workflows(authority, monkeypatch, response):
    api, service, _materials, _tree, template = authority
    created = api.post("/api/v1/workflows/from-template", json={"template_uuid": template["uuid"]}).json()
    before = service.get_graph(created["data"]["workflow"]["uuid"])
    monkeypatch.setattr("unilabos.server.api.edge_proxy.edge_proxy_enabled", lambda: True)
    monkeypatch.setattr("unilabos.server.api.edge_proxy.edge_http", lambda *_args, **_kwargs: response)
    failed = api.post("/api/v1/workflows/from-template", json={"template_uuid": template["uuid"]}).json()
    assert failed["code"] == 1000 and "Host 动作能力" in failed["error"]["msg"], failed
    assert service.get_graph(before["workflow"]["uuid"]) == before
