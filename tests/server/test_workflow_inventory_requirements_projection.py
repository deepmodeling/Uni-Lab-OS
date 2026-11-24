"""工作流库存需求：节点 ``meta_data.inventory_requirements`` 是唯一来源，V1 图根列表每次读取从节点派生。

前端只读 V1 图根 ``inventory_requirements``（节点「资源需求」页签、运行准备）；以前这里恒为空，
节点上真实声明的需求（调度据此预留）在前端看不到。
"""

from __future__ import annotations

from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest
from fastapi.testclient import TestClient

from unilabos.server.api.runtime.workflow import create_workflow_app
from unilabos.server.services.runtime.workflow.definition_edit import (
    definition_graph,
    project_inventory_requirements,
)
from unilabos.server.services.runtime.workflow.service import WorkflowService

WORKFLOW = "11111111-1111-4111-8111-111111111111"
PLATE_TEMPLATE = "22222222-2222-4222-8222-222222222222"
WATER_TEMPLATE = "33333333-3333-4333-8333-333333333333"


def _node(uuid: str, requirements: list[dict[str, Any]], **meta: Any) -> dict[str, Any]:
    return {
        "uuid": uuid,
        "type": "device_action",
        "execution_policy": {},
        "meta_data": {"target_device_id": "dev-a", "inventory_requirements": requirements, **meta},
    }


def test_node_requirements_project_to_the_v1_list() -> None:
    graph = {
        "workflow": {"uuid": WORKFLOW},
        "nodes": [
            _node("node-a", [{"key": "resource", "kind": "material", "template_uuid": PLATE_TEMPLATE}]),
            _node("node-b", [{"key": "water", "kind": "lot", "template_uuid": WATER_TEMPLATE, "quantity": 1200.0, "unit": "ul"}]),
            _node(
                "node-c",
                [{"key": "water", "kind": "lot", "template_uuid": WATER_TEMPLATE,
                  "quantity": {"kind": "workflow_input", "parameter": "volume"}, "unit": "ul"}],
            ),
            _node(
                "node-d",
                [{"key": "buffer", "kind": "content", "substance_name": "PBS", "quantity": 4, "unit": "ml",
                  "material_uuid": {"kind": "node_output", "workflow_node_uuid": "node-a", "data_key": "plate"}}],
            ),
            {"uuid": "node-e", "type": "device_action", "meta_data": {}},
        ],
        "edges": [],
    }

    items = project_inventory_requirements(graph)
    by_node = {item["consume_node_uuid"]: item for item in items}
    assert set(by_node) == {"node-a", "node-b", "node-c", "node-d"}
    for item in items:
        assert item["workflow_uuid"] == WORKFLOW
        assert item["uuid"] == str(
            uuid5(NAMESPACE_URL, f"unilabos://workflow/{WORKFLOW}/{item['consume_node_uuid']}/{item['meta_data']['unilab']['inventory']['key']}")
        )

    plate = by_node["node-a"]
    assert (plate["target_type"], plate["required_quantity"], plate["quantity_unit"], plate["allow_split"]) == (
        "material", 1.0, "件", False,
    )
    assert plate["requirement_key"] == "resource"
    assert plate["meta_data"]["unilab"]["inventory"]["template_uuid"] == PLATE_TEMPLATE
    assert plate["meta_data"]["unilab"]["deferred"] is False

    # 同一 key 出现在两个节点：按任务绑定的写法区分
    water = by_node["node-b"]
    assert water["requirement_key"] == "node-b/water"
    assert (water["target_type"], water["required_quantity"], water["quantity_unit"], water["allow_split"]) == (
        "lot", 1200.0, "ul", True,
    )
    assert water["meta_data"]["unilab"]["quantity_dynamic"] is False

    # 数量是入口引用：V1 只能给有限数，记 0 并标出来；含引用的需求推迟到节点就绪
    dynamic = by_node["node-c"]
    assert dynamic["requirement_key"] == "node-c/water"
    assert dynamic["required_quantity"] == 0.0 and dynamic["quantity_unit"] == "ul"
    assert dynamic["meta_data"]["unilab"]["quantity_dynamic"] is True
    assert dynamic["meta_data"]["unilab"]["deferred"] is True

    content = by_node["node-d"]
    assert (content["target_type"], content["required_quantity"], content["quantity_unit"]) == ("current_substance", 4.0, "ml")
    assert content["meta_data"]["unilab"]["material_source_node_uuid"] == "node-a"


def test_graph_without_node_requirements_keeps_an_empty_list() -> None:
    graph = {"workflow": {"uuid": WORKFLOW}, "nodes": [{"uuid": "n", "meta_data": {}}], "edges": []}
    assert project_inventory_requirements(graph) == []


def _water(quantity: Any = 100.0) -> list[dict[str, Any]]:
    return [{"key": "water", "kind": "lot", "template_uuid": WATER_TEMPLATE, "quantity": quantity, "unit": "ul"}]


def test_private_subgraph_requirements_are_hidden_but_still_make_the_key_unique() -> None:
    """组合调用的私有子图只在子操作定义里查看；它仍在执行计划里，同名 key 要写成 ``<node>/<key>`` 才能绑定。"""

    invocation = {"uuid": "call", "type": "workflow", "meta_data": {"unilab": {"composite": {"version": 1}}}}
    graph = {
        "workflow": {"uuid": WORKFLOW},
        "nodes": [
            _node("visible", _water()),
            invocation,
            {**_node("private", _water()), "parent_uuid": "call"},
        ],
        "edges": [],
    }

    [item] = project_inventory_requirements(graph)
    assert item["consume_node_uuid"] == "visible"
    assert item["requirement_key"] == "visible/water"
    # 读图投影同一规则：私有节点本身也不在本层
    assert [node["uuid"] for node in definition_graph(graph)["nodes"]] == ["visible", "call"]


def test_disabled_nodes_and_their_descendants_are_not_listed() -> None:
    """禁用节点（含禁用分组里的步骤）不进执行计划：需求不列出，也不让同名 key 变成 ``<node>/<key>``。"""

    graph = {
        "workflow": {"uuid": WORKFLOW},
        "nodes": [
            _node("enabled", _water()),
            {**_node("off", _water()), "disabled": True},
            {"uuid": "group", "type": "group", "disabled": True, "meta_data": {}},
            {**_node("in-group", _water()), "parent_uuid": "group"},
        ],
        "edges": [],
    }

    [item] = project_inventory_requirements(graph)
    assert (item["consume_node_uuid"], item["requirement_key"]) == ("enabled", "water")


def test_numeric_string_quantity_is_projected_like_the_scheduler_reads_it() -> None:
    """调度按 ``normalize_inventory`` 读 ``"1200"`` 为 1200；列表不能把它当成运行时决定的 0。"""

    [item] = project_inventory_requirements({"workflow": {"uuid": WORKFLOW}, "nodes": [_node("n", _water("1200"))]})
    assert item["required_quantity"] == 1200.0
    assert item["meta_data"]["unilab"]["quantity_dynamic"] is False
    assert item["meta_data"]["unilab"]["deferred"] is False
    # 原条目原样保留（导入时按它与节点比对）
    assert item["meta_data"]["unilab"]["inventory"]["quantity"] == "1200"


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


def test_saved_node_requirements_come_back_in_the_v1_graph(api) -> None:
    client, _service = api
    workflow = _ok(client.post("/api/v1/workflows", json={"name": "加液", "workflow_type": "experiment_operation"}), 201)
    node = {
        "uuid": str(uuid4()), "name": "加液", "type": "device_action", "material_uuid": str(uuid4()),
        "action_name": "fill_well", "action_type": "UniLabJsonCommand", "param": {"slot": "T1"},
        "pose": {"x": 0, "y": 0}, "execution_policy": {"depends_on": []},
        "meta_data": {
            "target_device_id": "material_bench",
            "inventory_requirements": [
                {"key": "water", "kind": "lot", "template_uuid": WATER_TEMPLATE, "quantity": 1200.0, "unit": "ul"}
            ],
        },
    }
    saved = _ok(client.put(f"/api/v1/workflows/{workflow['uuid']}/graph", json={
        "revision": workflow["revision"], "nodes": [node], "edges": [],
    }))
    graph = _ok(client.get(f"/api/v1/workflows/{workflow['uuid']}/graph"))
    for payload in (saved, graph):
        [item] = payload["inventory_requirements"]
        assert item["consume_node_uuid"] == node["uuid"]
        assert item["workflow_uuid"] == workflow["uuid"]
        assert (item["requirement_key"], item["target_type"], item["required_quantity"], item["quantity_unit"]) == (
            "water", "lot", 1200.0, "ul",
        )
    # 节点上的原条目保持不变（调度据此预留）
    assert graph["nodes"][0]["meta_data"]["inventory_requirements"][0]["key"] == "water"


def test_an_exported_graph_imports_again_and_a_handwritten_root_list_is_still_refused(api) -> None:
    """读图带回派生的图根列表；原样导入时它只是回声，导入后从新节点重新派生。

    回声按 (消耗节点, 节点上的需求原条目) 认：投影字段只读、改了也不生效；条目对不上节点（改了需求本身、
    手写一条）就不是回声，照旧拒绝。
    """

    client, _service = api
    workflow = _ok(client.post("/api/v1/workflows", json={"name": "导出", "workflow_type": "experiment_operation"}), 201)
    node = {
        "uuid": str(uuid4()), "name": "加液", "type": "device_action", "material_uuid": str(uuid4()),
        "action_name": "fill_well", "action_type": "UniLabJsonCommand", "param": {"slot": "T1"},
        "pose": {"x": 0, "y": 0}, "execution_policy": {"depends_on": []},
        "meta_data": {
            "target_device_id": "material_bench",
            "inventory_requirements": [
                {"key": "water", "kind": "lot", "template_uuid": WATER_TEMPLATE, "quantity": 1200.0, "unit": "ul"}
            ],
        },
    }
    _ok(client.put(f"/api/v1/workflows/{workflow['uuid']}/graph", json={
        "revision": workflow["revision"], "nodes": [node], "edges": [],
    }))
    exported = _ok(client.get(f"/api/v1/workflows/{workflow['uuid']}/graph"))
    assert len(exported["inventory_requirements"]) == 1

    response = client.post("/api/v1/workflows/import", json={**exported, "name": "导出副本"})
    assert response.status_code in (200, 201) and response.json()["code"] == 0, response.text
    imported = response.json()["data"]
    copy_uuid = (imported.get("workflow") or imported)["uuid"]
    copy = _ok(client.get(f"/api/v1/workflows/{copy_uuid}/graph"))
    [item] = copy["inventory_requirements"]
    assert item["workflow_uuid"] == copy_uuid and item["consume_node_uuid"] == copy["nodes"][0]["uuid"]
    assert (item["requirement_key"], item["required_quantity"]) == ("water", 1200.0)

    echoed = exported["inventory_requirements"][0]
    edited_inventory = {**echoed["meta_data"]["unilab"]["inventory"], "quantity": 5.0}
    handwritten = {key: value for key, value in echoed.items() if key != "meta_data"}
    for listed in (
        [{**echoed, "meta_data": {"unilab": {**echoed["meta_data"]["unilab"], "inventory": edited_inventory}}}],
        [echoed, handwritten],
    ):
        refused = client.post("/api/v1/workflows/import", json={**exported, "name": "手写", "inventory_requirements": listed})
        assert refused.status_code == 200 and refused.json()["code"] == 1000
        assert "inventory_requirements" in refused.json()["error"]["msg"]


def test_every_listed_requirement_key_binds_in_preflight(api) -> None:
    """列表给出的 requirement_key 拿去预检绑定，不会因为禁用节点报 invalid_inventory_binding。"""

    client, service = api
    workflow = _ok(client.post("/api/v1/workflows", json={"name": "绑定", "workflow_type": "experiment_operation"}), 201)
    container = str(uuid4())

    def node(name: str, **extra: Any) -> dict[str, Any]:
        return {
            "uuid": str(uuid4()), "name": name, "type": "device_action", "material_uuid": str(uuid4()),
            "action_name": "fill_well", "action_type": "UniLabJsonCommand", "param": {"slot": "T1"},
            "pose": {"x": 0, "y": 0}, "execution_policy": {"depends_on": []},
            "meta_data": {"target_device_id": "material_bench", "inventory_requirements": [
                {"key": "buffer", "kind": "content", "material_uuid": container, "substance_name": "PBS",
                 "quantity": 4.0, "unit": "ml"},
            ]},
            **extra,
        }

    _ok(client.put(f"/api/v1/workflows/{workflow['uuid']}/graph", json={
        "revision": workflow["revision"], "nodes": [node("启用"), node("禁用", disabled=True)], "edges": [],
    }))
    listed = _ok(client.get(f"/api/v1/workflows/{workflow['uuid']}/graph"))["inventory_requirements"]
    assert [item["requirement_key"] for item in listed] == ["buffer"]
    for item in listed:
        report = service.preflight_workflow(
            workflow_uuid=workflow["uuid"], run_mode="normal", target_node_uuid=None, input_value={},
            expected_revision=None, inventory_bindings=[{
                "requirement_key": item["requirement_key"], "inventory_type": "current_substance",
                "inventory_uuid": str(uuid4()), "reserved_quantity": item["required_quantity"],
                "quantity_unit": item["quantity_unit"],
            }],
        )
        assert "invalid_inventory_binding" not in {check["code"] for check in report["checks"]}, report["checks"]
