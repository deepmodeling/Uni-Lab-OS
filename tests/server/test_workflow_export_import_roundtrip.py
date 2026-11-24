"""导出（读图）→ 导入 → 再导出：图结构稳定，节点引用随新身份改写，组合调用的私有子图重新展开。

导出只含本层（组合调用的私有子图隐藏），图根 ``inventory_requirements`` 是从节点派生的只读投影；
导入换掉全部节点 / 连线身份。覆盖：组合调用父图（子操作与本层同名需求、子操作里的循环与携带）、
分组、循环、条件分支、禁用节点、以上游结果为来源的库存需求、作者声明的 I/O 合同、节点输入绑定、
资源作用域与创作顺序，以及工作流复制。
"""

from __future__ import annotations

import copy
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
from tests.server.test_workflow_publication import (
    DEVICE_TEMPLATE,
    NUMBER,
    VOLUME_SOURCE,
    VOLUME_TARGET,
    _install_device_template,
)

WATER ="33333333-3333-4333-8333-333333333333"
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


def _canonical(client: TestClient, service: WorkflowService, workflow_uuid: str) -> dict[str, Any]:
    """读图结果里的节点身份换成名字路径（私有子图节点带上所属调用节点的名字），去掉随身份变的字段。"""

    exported = _ok(client.get(f"/api/v1/workflows/{workflow_uuid}/graph"))
    full = {node["uuid"]: node for node in service.get_graph(workflow_uuid)["nodes"]}

    def label(identity: str) -> str:
        names, node = [], full.get(identity)
        while node is not None:
            names.append(node["name"])
            node = full.get(node.get("parent_uuid"))
        return "<" + "/".join(reversed(names)) + ">"

    volatile = {"uuid", "create_time", "update_time", "workflow_uuid"}
    payload = {
        "workflow_unilab": [exported["workflow"]["meta_data"].get("unilab") or {}],
        "nodes":[{key: value for key, value in node.items() if key not in volatile - {"uuid"}} for node in exported["nodes"]],
        "edges": [{key: value for key, value in edge.items() if key not in volatile} for edge in exported["edges"]],
        "inventory_requirements": [
            {key: value for key, value in item.items() if key not in volatile} for item in exported["inventory_requirements"]
        ],
    }
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    # 按长度倒序替换，``<node>/<key>`` 形式的 requirement_key 里的身份一并换掉
    for identity in sorted(full, key=len, reverse=True):
        text = text.replace(identity, label(identity))
    canonical = json.loads(text)
    return {key: sorted(items, key=lambda item: json.dumps(item, sort_keys=True)) for key, items in canonical.items()}


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


def test_composite_parent_with_a_shared_requirement_key_round_trips(api) -> None:
    """本层与子操作都要 ``water``：导出的 key 是 ``<node>/water``，原样导入不再被拒；私有子图按发布重新展开。"""

    client, service = api
    requirement = [{"key": "water", "kind": "lot", "template_uuid": WATER, "quantity": 100.0, "unit": "ul"}]

    def node(name: str) -> dict[str, Any]:
        return {
            "uuid": str(uuid.uuid4()), "name": name, "type": "device_action", "material_uuid": str(uuid.uuid4()),
            "action_name": "fill_well", "action_type": "UniLabJsonCommand", "param": {"slot": "T1"},
            "pose": {"x": 0, "y": 0}, "execution_policy": {"depends_on": []},
            "meta_data": {"target_device_id": "material_bench", "inventory_requirements": requirement},
        }

    child = _ok(client.post("/api/v1/workflows", json={"name": "子操作", "workflow_type": "experiment_operation"}), 201)
    saved = _ok(client.put(f"/api/v1/workflows/{child['uuid']}/graph", json={
        "revision": child["revision"], "nodes": [node("子步骤")], "edges": []}))
    contract = _ok(client.post(f"/api/v1/workflows/{child['uuid']}/publications",
                               json={"revision": saved["workflow"]["revision"]}), 201)
    parent = _ok(client.post("/api/v1/workflows", json={"name": "父流程"}), 201)
    saved = _ok(client.put(f"/api/v1/workflows/{parent['uuid']}/graph", json={
        "revision": parent["revision"], "nodes": [node("本层步骤")], "edges": []}))
    _ok(client.post(f"/api/v1/workflows/{parent['uuid']}/composite-invocations", json={
        "revision": saved["workflow"]["revision"], "contract_uuid": contract["uuid"], "invocation_uuid": str(uuid.uuid4()),
        "device_bindings": {}, "pose": {"x": 1, "y": 1}, "param": {}}))

    exported = _ok(client.get(f"/api/v1/workflows/{parent['uuid']}/graph"))
    [listed] = exported["inventory_requirements"]
    assert listed["requirement_key"].endswith("/water")

    copy_uuid = _import(client, exported, "父流程副本")
    stored = service.get_graph(copy_uuid)["nodes"]
    call = next(item for item in stored if item["type"] == "workflow")
    assert call["meta_data"]["unilab"]["composite"]["child_workflow_uuid"] == child["uuid"]
    assert [item["name"] for item in stored if item.get("parent_uuid") == call["uuid"]] == ["子步骤"]
    assert _canonical(client, service, copy_uuid) == _canonical(client, service, parent["uuid"])


def test_rich_workflow_export_import_export_is_stable(api) -> None:
    """分组、循环、条件分支、禁用节点、上游结果来源与含循环携带的组合调用：再导出与原导出一致，且可再次导入。"""

    client, service = api

    @workflow(display_name="子操作")
    def child(ctx):
        n = 0
        while n < 2:
            out = ctx.run("carry-device/add", {"value": n}, name="累加",
                          inventory=[{"key": "water", "kind": "lot", "template_uuid": WATER, "quantity": 100.0, "unit": "ul"}])
            n = out.value
        ctx.run("carry-device/note", {"text": n}, name="记录")

    @workflow(display_name="父流程")
    def parent(ctx):
        first = ctx.run("carry-device/add", {"value": 1}, name="首步",
                        inventory=[{"key": "water", "kind": "lot", "template_uuid": WATER, "quantity": 5.0, "unit": "ul"}])
        with ctx.parallel("并行"):
            ctx.run("carry-device/note", {"text": first.value}, name="并行一")
            ctx.run("carry-device/note", {"text": "x"}, name="并行二",
                    inventory=[{"key": "tips", "kind": "material", "template_uuid": WATER}])
        for i in range(2):
            ctx.run("carry-device/mark", {"index": i}, name="循环体",
                    inventory=[{"key": "buffer", "kind": "content", "substance_name": "PBS", "quantity": 4, "unit": "ml",
                                "material_uuid": first.plate}])
        if first.value > 1:
            ctx.run("carry-device/note", {"text": "big"}, name="大")
        else:
            ctx.run("carry-device/note", {"text": "small"}, name="小")

    child_record = _store(service, "子操作")
    published = service.publish_workflow(child_record["uuid"], revision=service.get_workflow(child_record["uuid"])["revision"])
    parent_record = _store(service, "父流程")
    service.insert_composite_invocation(
        parent_record["uuid"], revision=service.get_workflow(parent_record["uuid"])["revision"],
        child_workflow_uuid=child_record["uuid"], child_revision=published["revision"], invocation_uuid=str(uuid.uuid4()),
    )
    disabled = next(item for item in service.get_graph(parent_record["uuid"])["nodes"] if item["name"] == "并行二")
    service.patch_workflow_node(disabled["uuid"], patch={"disabled": True})
    assert {item["type"] for item in service.get_graph(parent_record["uuid"])["nodes"]} >= {
        "group", "loop", "condition", "workflow", "device_action"}

    original = _canonical(client, service, parent_record["uuid"])
    # 子操作同名的 water 计入 key；禁用节点的 tips 不列出
    assert sorted(item["requirement_key"] for item in original["inventory_requirements"]) == ["<首步>/water", "buffer"]

    exported = _ok(client.get(f"/api/v1/workflows/{parent_record['uuid']}/graph"))
    first_copy = _import(client, exported, "副本一")
    assert _canonical(client, service, first_copy) == original
    second_copy = _import(client, _ok(client.get(f"/api/v1/workflows/{first_copy}/graph")), "副本二")
    assert _canonical(client, service, second_copy) == original
    # 私有子图（循环、携带）跟着新调用节点重新展开
    stored = service.get_graph(second_copy)["nodes"]
    assert len(stored) == len(service.get_graph(parent_record["uuid"])["nodes"])


def test_declared_io_and_resource_scopes_round_trip_and_are_validated(api) -> None:
    """入口参数、输出绑定、节点输入绑定、资源作用域与创作顺序随导入保留，节点引用换成新身份。

    以前导入丢掉工作流的保留元数据：引用入口参数的节点被拒（1000），其余声明悄悄消失。文件内容不原样
    信任：输出绑定、作用域指向不存在的节点，或节点引用了没声明的入口参数，整次导入失败并指明哪一项。
    """

    client, service = api
    _install_device_template(service)
    record = _ok(client.post("/api/v1/workflows", json={"name": "带接口"}), 201)
    path = f"/api/v1/workflows/{record['uuid']}"
    for name in ("加样一", "加样二"):
        _ok(client.post(f"{path}/nodes", json={
            "workflow_node_template_uuid": DEVICE_TEMPLATE, "name": name, "param": {"volume": 1},
            "material_uuid": DEVICE, "meta_data": {"target_device_id": "pump-1"},
        }), 201)
    graph = _ok(client.get(f"{path}/graph"))
    first, second = (next(node["uuid"] for node in graph["nodes"] if node["name"] == name) for name in ("加样一", "加样二"))
    declared = _ok(client.put(f"{path}/io", json={
        "revision": graph["workflow"]["revision"],
        "input_contract": {"version": 1, "parameters": [{"name": "volume", "schema": NUMBER, "required": True}]},
        "output_contract": {"version": 1, "outputs": [{"name": "dosed", "schema": NUMBER}]},
        "output_bindings": {"dosed": {"kind": "node_output", "workflow_node_uuid": second, "source_handle_uuid": VOLUME_SOURCE}},
        "node_input_bindings": {first: {VOLUME_TARGET: {"parameter": "volume"}}},
        "node_params": {second: {"volume": {"kind": "workflow_input", "parameter": "volume"}}},
    }))
    _ok(client.put(f"{path}/resource-scopes", json={"revision": declared["workflow"]["revision"], "scopes": [
        {"scope_id": "锁", "kind": "with", "resources": ["pump-1"], "node_uuids": [first, second]},
    ]}))

    original = _canonical(client, service, record["uuid"])
    [unilab] = original["workflow_unilab"]
    assert unilab["output_bindings"]["dosed"]["workflow_node_uuid"] == "<加样二>"
    assert unilab["resource_scopes"][0]["node_uuids"] == ["<加样一>", "<加样二>"]
    by_name = {node["name"]: node for node in original["nodes"]}
    assert by_name["加样一"]["meta_data"]["unilab"] == {
        "authoring_source_order": 0, "input_bindings": {VOLUME_TARGET: {"parameter": "volume"}}}
    assert by_name["加样二"]["param"]["volume"] == {"kind": "workflow_input", "parameter": "volume"}

    exported = _ok(client.get(f"{path}/graph"))
    first_copy = _import(client, exported, "带接口副本")
    assert _canonical(client, service, first_copy) == original
    second_copy = _import(client, _ok(client.get(f"/api/v1/workflows/{first_copy}/graph")), "带接口副本二")
    assert _canonical(client, service, second_copy) == original

    def tampered(change) -> dict[str, Any]:
        payload = copy.deepcopy(exported)
        change(payload["workflow"]["meta_data"]["unilab"])
        return payload

    # 合法但未规范化的合同按保存路径落库规范化后的形状
    loose = tampered(lambda meta: meta["output_contract"]["outputs"][0].pop("implicit"))
    normalized_copy = _import(client, loose, "未规范化")
    assert _canonical(client, service, normalized_copy) == original

    before = service.list_workflows()["total"]
    for payload, item in (
        (tampered(lambda meta: meta["output_bindings"]["dosed"].update(workflow_node_uuid=str(uuid.uuid4()))),
         "output_bindings"),
        (tampered(lambda meta: meta["resource_scopes"][0]["node_uuids"].append(str(uuid.uuid4()))), "resource_scopes"),
        (tampered(lambda meta: meta.pop("input_contract")), "input_binding"),
    ):
        refused = client.post("/api/v1/workflows/import", json={**payload, "name": "坏文件"}).json()
        assert refused["code"] == 1000 and item in refused["error"]["msg"], refused
    assert service.list_workflows()["total"] == before


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
