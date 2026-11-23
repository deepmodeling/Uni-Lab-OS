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

    # 与 definition_graph 的可见性过滤配合：私有子图节点的需求不出现在本层
    assert definition_graph({**graph, "inventory_requirements": items})["inventory_requirements"] == items


def test_graph_without_node_requirements_keeps_an_empty_list() -> None:
    graph = {"workflow": {"uuid": WORKFLOW}, "nodes": [{"uuid": "n", "meta_data": {}}], "edges": []}
    assert project_inventory_requirements(graph) == []


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
