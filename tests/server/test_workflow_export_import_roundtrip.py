"""导入与复制换掉节点身份时，图里所有指向节点的引用一并改写。"""

from __future__ import annotations

import json
import uuid
from typing import Any, TypedDict

import pytest
from fastapi.testclient import TestClient

from unilabos.registry.workflows import (
    DeviceCatalog,
    build_workflow_template_payload,
    clear_registered_workflows,
    get_registered_workflows,
    materialize_workflow_template,
    upsert_workflow,
    workflow,
)
from unilabos.server.api.runtime.workflow import create_workflow_app
from unilabos.server.services.runtime.workflow.definition_edit import duplicate_graph
from unilabos.server.services.runtime.workflow.service import WorkflowService

WATER = "33333333-3333-4333-8333-333333333333"
DEVICE = "44444444-4444-4444-8444-444444444444"


@pytest.fixture(autouse=True)
def _isolated_workflow_registry():
    from unilabos.registry.workflows import _registered_workflows

    snapshot = get_registered_workflows()
    clear_registered_workflows()
    yield
    clear_registered_workflows()
    for definition in snapshot.values():
        _registered_workflows[definition.uuid] = definition


@pytest.fixture
def api():
    service = WorkflowService(":memory:")
    try:
        yield TestClient(create_workflow_app(service)), service
    finally:
        service.close()


def _ok(response, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    body = response.json()
    assert body["code"] == 0, body
    return body.get("data")


def _store(service: WorkflowService, display_name: str) -> dict[str, Any]:
    definition = next(item for item in get_registered_workflows().values() if item.display_name == display_name)
    catalog = DeviceCatalog()
    catalog.add("carry-device", "carry_demo_class", DEVICE)
    return upsert_workflow(service, materialize_workflow_template(build_workflow_template_payload(definition), catalog))


def _import(client: TestClient, exported: dict[str, Any], name: str) -> str:
    imported = _ok(client.post("/api/v1/workflows/import", json={**exported, "name": name}), 201)
    return imported["workflow"]["uuid"]


def test_duplicate_graph_remaps_every_node_reference() -> None:
    """图里每一种指向节点的引用都换成新身份；旧身份一个不剩。"""

    ids = {name: str(uuid.uuid4()) for name in ("source", "loop", "body", "cond", "call", "group", "member")}
    handle = str(uuid.uuid4())
    graph = {
        "nodes": [
            {"uuid": ids["source"], "name": "source", "param": {}, "meta_data": {}},
            {"uuid": ids["group"], "name": "group", "type": "group", "meta_data": {}},
            {"uuid": ids["member"], "name": "member", "parent_uuid": ids["group"],
             "execution_policy": {"depends_on": [ids["source"]],
                                  "condition_guards": [{"node_uuid": ids["cond"], "equals": True}]},
             "param": {"value": {"kind": "node_output", "workflow_node_uuid": ids["source"], "data_key": "v"}},
             "meta_data": {"inventory_requirements": [
                 {"key": "buffer", "kind": "content", "substance_name": "PBS", "quantity": 4, "unit": "ml",
                  "material_uuid": {"kind": "node_output", "workflow_node_uuid": ids["source"], "data_key": "plate"}},
             ]}},
            {"uuid": ids["loop"], "name": "loop", "type": "loop",
             "param": {"expression": {"op": "lt", "args": [{"field": {"node_uuid": ids["loop"]}, "name": "n"}, {"lit": 3}]},
                       "condition": {"source": "node_output", "node_uuid": ids["body"], "field": "ok"},
                       "carry_next": {"n": {"kind": "node_output", "workflow_node_uuid": ids["body"], "data_key": "v"}}},
             "meta_data": {}},
            {"uuid": ids["body"], "name": "body", "parent_uuid": ids["loop"], "meta_data": {}},
            {"uuid": ids["cond"], "name": "cond", "type": "condition",
             "param": {"version": 1, "expression": {"field": {"node_uuid": ids["source"]}, "name": "ok"}}, "meta_data": {}},
            {"uuid": ids["call"], "name": "call", "type": "workflow",
             "param": {"x": {"kind": "node_output", "workflow_node_uuid": ids["source"], "source_handle_uuid": handle}},
             "meta_data": {"unilab": {"composite": {
                 "target_mappings": {handle: [{"workflow_node_uuid": ids["body"], "target_handle_uuid": handle}]},
                 "resource_scopes": [{"node_uuids": [ids["body"]], "entry_node_uuid": ids["body"]}],
             }}}},
        ],
        "edges": [{"uuid": str(uuid.uuid4()), "source_node_uuid": ids["source"], "target_node_uuid": ids["cond"],
                   "source_handle_uuid": handle, "target_handle_uuid": handle}],
    }

    nodes, edges, identity = duplicate_graph(graph)

    text = json.dumps({"nodes": nodes, "edges": edges})
    assert not any(old in text for old in ids.values())
    new = {name: identity[old] for name, old in ids.items()}
    by_name = {node["name"]: node for node in nodes}
    member, loop, call = by_name["member"], by_name["loop"], by_name["call"]
    assert member["parent_uuid"] == new["group"] and by_name["body"]["parent_uuid"] == new["loop"]
    assert member["execution_policy"] == {"depends_on": [new["source"]],
                                          "condition_guards": [{"node_uuid": new["cond"], "equals": True}]}
    assert member["param"]["value"]["workflow_node_uuid"] == new["source"]
    assert member["meta_data"]["inventory_requirements"][0]["material_uuid"]["workflow_node_uuid"] == new["source"]
    assert loop["param"]["expression"]["args"][0]["field"] == {"node_uuid": new["loop"]}
    assert loop["param"]["condition"]["node_uuid"] == new["body"]
    assert loop["param"]["carry_next"]["n"]["workflow_node_uuid"] == new["body"]
    assert by_name["cond"]["param"]["expression"]["field"] == {"node_uuid": new["source"]}
    assert call["param"]["x"]["workflow_node_uuid"] == new["source"]
    composite = call["meta_data"]["unilab"]["composite"]
    assert composite["target_mappings"][handle][0]["workflow_node_uuid"] == new["body"]
    assert composite["resource_scopes"][0] == {"node_uuids": [new["body"]], "entry_node_uuid": new["body"]}
    assert (edges[0]["source_node_uuid"], edges[0]["target_node_uuid"]) == (new["source"], new["cond"])
    # 非节点引用（Handle、字面量）不动
    assert edges[0]["source_handle_uuid"] == handle and loop["param"]["expression"]["args"][1] == {"lit": 3}


def test_node_output_material_source_survives_export_and_import(api) -> None:
    """库存需求的来源是上游节点的结果：导入后指向副本里的那个节点（以前留着旧身份，导入被拒）。"""

    client, _service = api
    workflow_record = _ok(client.post("/api/v1/workflows", json={"name": "来源"}), 201)

    def node(name: str, requirements: list[dict[str, Any]], depends_on: list[str]) -> dict[str, Any]:
        return {
            "uuid": str(uuid.uuid4()), "name": name, "type": "device_action", "material_uuid": str(uuid.uuid4()),
            "action_name": "fill_well", "action_type": "UniLabJsonCommand", "param": {"slot": "T1"},
            "pose": {"x": 0, "y": 0}, "execution_policy": {"depends_on": depends_on},
            "meta_data": {"target_device_id": "material_bench", "inventory_requirements": requirements},
        }

    plate = node("取板", [], [])
    fill = node("加液", [{"key": "buffer", "kind": "content", "substance_name": "PBS", "quantity": 4, "unit": "ml",
                          "material_uuid": {"kind": "node_output", "workflow_node_uuid": plate["uuid"], "data_key": "plate"}}],
                [plate["uuid"]])
    _ok(client.put(f"/api/v1/workflows/{workflow_record['uuid']}/graph", json={
        "revision": workflow_record["revision"], "nodes": [plate, fill], "edges": []}))
    exported = _ok(client.get(f"/api/v1/workflows/{workflow_record['uuid']}/graph"))

    copy = _ok(client.get(f"/api/v1/workflows/{_import(client, exported, '来源副本')}/graph"))
    by_name = {item["name"]: item for item in copy["nodes"]}
    source = by_name["加液"]["meta_data"]["inventory_requirements"][0]["material_uuid"]["workflow_node_uuid"]
    assert source == by_name["取板"]["uuid"] != plate["uuid"]
    assert by_name["加液"]["execution_policy"]["depends_on"] == [by_name["取板"]["uuid"]]
    assert copy["inventory_requirements"][0]["meta_data"]["unilab"]["material_source_node_uuid"] == source


class Result(TypedDict):
    value: float


def test_copy_remaps_workflow_level_references() -> None:
    """复制工作流：输出绑定与资源作用域引用的是副本自己的节点，不再指向原工作流。"""

    @workflow(display_name="复制")
    def flow(ctx) -> Result:
        out = ctx.run("carry-device/add", {"value": 1}, name="加")
        with ctx.lock("carry-device", name="锁"):
            ctx.run("carry-device/note", {"text": "x"}, name="记")
        return {"value": out.value}

    service = WorkflowService(":memory:")
    try:
        source = service.get_graph(_store(service, "复制")["uuid"])
        copy = service.duplicate_workflow(source["workflow"]["uuid"], name="副本")
        unilab = copy["workflow"]["meta_data"]["unilab"]
        by_name = {node["name"]: node["uuid"] for node in copy["nodes"]}
        assert unilab["output_bindings"]["value"]["workflow_node_uuid"] == by_name["加"]
        assert unilab["resource_scopes"][0]["node_uuids"] == [by_name["记"]]
    finally:
        service.close()
