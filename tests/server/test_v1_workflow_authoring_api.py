"""V1.0 工作流作者合同（统一分支）：类型 / 类别、增量节点连线、引用、发布合同、节点模板。

每个路由断言路径、方法、V1 请求体与 ``{code, data|error}`` 信封业务码；请求示例取自 V1
``tests/app/test_experiment_operation_category_api.py``、``workflow_api.py`` 与 V1 控制台
``frontend/src/lib/edgeClient.ts``。
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from unilabos.server.api.runtime.workflow import create_workflow_app
from unilabos.server.services.runtime.workflow.operation_category import DEVICE_OPERATION_CATEGORY_UUID
from unilabos.server.services.runtime.workflow.service import WorkflowService


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


def _code(response) -> int:
    assert response.status_code == 200, response.text
    return response.json()["code"]


def _canvas_node(name: str = "记录", *, depends_on: list[str] | None = None) -> dict:
    return {
        "uuid": str(uuid4()), "name": name, "type": "device_action", "material_uuid": str(uuid4()),
        "action_name": "record", "action_type": "UniLabJsonCommand", "param": {"value": 1},
        "pose": {"x": 0, "y": 0}, "execution_policy": {"depends_on": depends_on or []},
        "meta_data": {"target_device_id": "dev-a"},
    }


# ── 实验操作类别 + workflow_type ─────────────────────────────────────


def test_categories_crud_and_workflow_type_filters(api) -> None:
    client, _service = api
    categories = _ok(client.get("/api/v1/experiment-operation-categories"))["items"]
    assert [item["name"] for item in categories] == ["设备操作", "集成操作", "人工操作"]
    device = next(item for item in categories if item["uuid"] == DEVICE_OPERATION_CATEGORY_UUID)
    assert device == {"uuid": DEVICE_OPERATION_CATEGORY_UUID, "name": "设备操作", "sort_order": 10}

    explicit = _ok(client.post("/api/v1/workflows", json={
        "name": "显式设备操作", "workflow_type": "experiment_operation",
        "operation_category_uuid": device["uuid"], "tags": [], "meta_data": {},
    }), 201)
    legacy = _ok(client.post("/api/v1/workflows", json={
        "name": "旧标签设备操作", "workflow_type": "experiment_operation", "tags": ["device_operation"], "meta_data": {},
    }), 201)
    normal = _ok(client.post("/api/v1/workflows", json={"name": "普通工作流", "tags": [], "meta_data": {}}), 201)
    assert explicit["workflow_type"] == "experiment_operation" and explicit["status"] == "source"
    assert explicit["operation_category_uuid"] == legacy["operation_category_uuid"] == device["uuid"]
    assert normal["workflow_type"] == "normal" and normal["operation_category_uuid"] is None

    renamed = _ok(client.put(f"/api/v1/experiment-operation-categories/{device['uuid']}", json={"name": "仪器操作"}))
    assert renamed == {**device, "name": "仪器操作"}
    assert _ok(client.get(f"/api/v1/experiment-operation-categories/{device['uuid']}"))["name"] == "仪器操作"

    filtered = _ok(client.get("/api/v1/workflows", params={
        "workflow_type": "experiment_operation", "operation_category_uuid": device["uuid"],
    }))
    assert set(filtered) == {"items", "has_more", "page", "page_size"}
    assert {item["uuid"] for item in filtered["items"]} == {explicit["uuid"], legacy["uuid"]}
    combined = _ok(client.get("/api/v1/workflows", params={
        "workflow_type": "experiment_operation", "status": "source", "name": "显式设备",
        "operation_category_uuid": device["uuid"],
    }))["items"]
    assert [item["uuid"] for item in combined] == [explicit["uuid"]]
    # 兼容旧前端的 keyword；name 优先
    assert [item["uuid"] for item in _ok(client.get("/api/v1/workflows", params={"keyword": "普通"}))["items"]] == [normal["uuid"]]
    assert _ok(client.get("/api/v1/workflows", params={"workflow_type": "experiment_operation", "status": "published"}))["items"] == []

    # 显式 null 清空类别；之后省略类别保持清空
    cleared = _ok(client.put(f"/api/v1/workflows/{legacy['uuid']}", json={
        "name": "旧标签设备操作", "workflow_type": "experiment_operation", "operation_category_uuid": None,
        "tags": ["device_operation"], "meta_data": {},
    }))
    assert cleared["operation_category_uuid"] is None
    renamed_workflow = _ok(client.put(f"/api/v1/workflows/{legacy['uuid']}", json={
        "name": "改名", "tags": ["device_operation"], "meta_data": {},
    }))
    assert renamed_workflow["operation_category_uuid"] is None
    # 不能在普通工作流与实验操作之间转换
    assert _code(client.put(f"/api/v1/workflows/{normal['uuid']}", json={
        "name": "x", "workflow_type": "experiment_operation",
    })) == 1000
    assert _code(client.get("/api/v1/workflows", params={"workflow_type": "workflow"})) == 1000


def test_category_rejects_normal_workflow_and_delete_while_referenced(api) -> None:
    client, _service = api
    created = _ok(client.post("/api/v1/experiment-operation-categories", json={"name": "自定义操作", "sort_order": 40}), 201)
    assert created["sort_order"] == 40
    assert _code(client.post("/api/v1/experiment-operation-categories", json={"name": "自定义操作"})) == 3003
    assert _code(client.post("/api/v1/experiment-operation-categories", json={"name": "x", "extra": 1})) == 1000
    assert _code(client.put(f"/api/v1/experiment-operation-categories/{created['uuid']}", json={})) == 1000
    assert _code(client.get(f"/api/v1/experiment-operation-categories/{uuid4()}")) == 3002
    assert _code(client.get("/api/v1/experiment-operation-categories/not-a-uuid")) == 1000
    assert _code(client.post("/api/v1/workflows", json={
        "name": "错误分类", "workflow_type": "normal", "operation_category_uuid": created["uuid"],
    })) == 1000
    child = _ok(client.post("/api/v1/workflows", json={
        "name": "引用类别", "workflow_type": "experiment_operation", "operation_category_uuid": created["uuid"],
    }), 201)
    assert _code(client.delete(f"/api/v1/experiment-operation-categories/{created['uuid']}")) == 3003
    _ok(client.put(f"/api/v1/workflows/{child['uuid']}", json={
        "name": child["name"], "workflow_type": "experiment_operation", "operation_category_uuid": None,
    }))
    assert _ok(client.delete(f"/api/v1/experiment-operation-categories/{created['uuid']}")) is None
    assert created["uuid"] not in {item["uuid"] for item in _ok(client.get("/api/v1/experiment-operation-categories"))["items"]}
    # 删除的默认类别不会被重新补种
    _ok(client.delete(f"/api/v1/experiment-operation-categories/{DEVICE_OPERATION_CATEGORY_UUID}"))
    assert DEVICE_OPERATION_CATEGORY_UUID not in {
        item["uuid"] for item in _ok(client.get("/api/v1/experiment-operation-categories"))["items"]
    }


# ── 增量节点 / 连线 ─────────────────────────────────────────────────


def test_node_and_edge_routes_follow_v1_contract(api) -> None:
    client, service = api
    workflow = _ok(client.post("/api/v1/workflows", json={"name": "增量编辑"}), 201)
    graph = _ok(client.post(f"/api/v1/workflows/{workflow['uuid']}/nodes", json={
        "type": "script", "name": "脚本", "param": {"code": "x"}, "pose": {"x": 1, "y": 2},
        "manual_confirmation": {"timeout_seconds": 60},
    }), 201)
    assert graph["workflow"]["revision"] == 2 and graph["workflow"]["workflow_type"] == "normal"
    node = graph["nodes"][0]
    assert node["type"] == "script" and node["manual_confirmation"] == {"timeout_seconds": 60}
    assert node["meta_data"]["unilab"]["authoring_source_order"] == 0
    # 无模板节点类型受限；未知字段拒绝
    assert _code(client.post(f"/api/v1/workflows/{workflow['uuid']}/nodes", json={"type": "device_action", "name": "x"})) == 1000
    assert _code(client.post(f"/api/v1/workflows/{workflow['uuid']}/nodes", json={"type": "script", "name": "x", "bogus": 1})) == 1000
    assert _code(client.post(f"/api/v1/workflows/{workflow['uuid']}/nodes", json={
        "type": "script", "name": "x", "manual_confirmation": {"timeout_seconds": 0},
    })) == 1000

    fetched = _ok(client.get(f"/api/v1/workflow-nodes/{node['uuid']}"))
    assert fetched["uuid"] == node["uuid"]
    patched = _ok(client.patch(f"/api/v1/workflow-nodes/{node['uuid']}", json={"pose": {"x": 9, "y": 9}, "name": "改名"}))
    assert patched["pose"] == {"x": 9, "y": 9} and patched["name"] == "改名"
    assert _code(client.patch(f"/api/v1/workflow-nodes/{node['uuid']}", json={})) == 1000
    copy = _ok(client.post(f"/api/v1/workflow-nodes/{node['uuid']}/duplicate", json={"name": "副本"}), 201)
    assert copy["uuid"] != node["uuid"] and copy["name"] == "副本"
    listed = _ok(client.get(f"/api/v1/workflows/{workflow['uuid']}/nodes", params={"page_size": 1}))
    assert listed["total"] == 2 and len(listed["items"]) == 1

    # 无模板节点没有 Handle：连线引用未知 Handle 被拒绝，图不变
    revision = service.get_workflow(workflow["uuid"])["revision"]
    assert _code(client.post(f"/api/v1/workflows/{workflow['uuid']}/edges", json={
        "source_node_uuid": node["uuid"], "target_node_uuid": copy["uuid"],
        "source_handle_uuid": str(uuid4()), "target_handle_uuid": str(uuid4()),
    })) != 0
    assert service.get_workflow(workflow["uuid"])["revision"] == revision

    assert _ok(client.delete(f"/api/v1/workflow-nodes/{copy['uuid']}")) is None
    assert _code(client.get(f"/api/v1/workflow-nodes/{copy['uuid']}")) == 3002
    assert _code(client.delete(f"/api/v1/workflow-edges/{uuid4()}")) == 3002

    second = _ok(client.post(f"/api/v1/workflows/{workflow['uuid']}/nodes", json={"type": "script", "name": "二"}), 201)
    second_uuid = next(item["uuid"] for item in second["nodes"] if item["name"] == "二")
    assert second["nodes"][-1]["meta_data"]["unilab"]["authoring_source_order"] == 1
    duplicated = _ok(client.post(f"/api/v1/workflows/{workflow['uuid']}/duplicate", json={"name": "复制品"}), 201)
    assert duplicated["workflow"]["uuid"] != workflow["uuid"] and duplicated["workflow"]["name"] == "复制品"
    assert len(duplicated["nodes"]) == 2
    remaining = _ok(client.post(f"/api/v1/workflows/{workflow['uuid']}/batch-delete", json={"node_uuids": [second_uuid]}))
    assert [item["uuid"] for item in remaining["nodes"]] == [node["uuid"]]
    assert _code(client.post(f"/api/v1/workflows/{workflow['uuid']}/batch-delete", json={})) == 1000


def test_graph_get_and_put_follow_v1_defaults(api) -> None:
    client, _service = api
    workflow = _ok(client.post("/api/v1/workflows", json={"name": "图", "workflow_type": "experiment_operation"}), 201)
    node = {**_canvas_node(), "manual_confirmation": {"timeout_seconds": 30}}
    # V1 客户端不传 site_binding_mode：按 preserve 原样保存
    saved = _ok(client.put(f"/api/v1/workflows/{workflow['uuid']}/graph", json={
        "revision": workflow["revision"], "nodes": [node], "edges": [],
    }))
    assert saved["workflow"]["status"] == "source" and saved["workflow"]["workflow_type"] == "experiment_operation"
    assert saved["nodes"][0]["manual_confirmation"] == {"timeout_seconds": 30}
    assert saved["inventory_requirements"] == []
    graph = _ok(client.get(f"/api/v1/workflows/{workflow['uuid']}/graph"))
    assert set(graph) >= {"workflow", "nodes", "edges", "node_templates", "handle_templates", "inventory_requirements"}
    assert graph["workflow"]["operation_category_uuid"] is None
    assert graph["nodes"][0]["manual_confirmation"] == {"timeout_seconds": 30}


def test_referenced_by_publication_contract_and_composite_invocation(api) -> None:
    client, service = api
    child = _ok(client.post("/api/v1/workflows", json={"name": "子操作", "workflow_type": "experiment_operation"}), 201)
    graph = _ok(client.put(f"/api/v1/workflows/{child['uuid']}/graph", json={
        "revision": child["revision"], "nodes": [_canvas_node()], "edges": [],
    }))
    contract = _ok(client.post(f"/api/v1/workflows/{child['uuid']}/publications",
                               json={"revision": graph["workflow"]["revision"]}), 201)
    assert contract["workflow_uuid"] == child["uuid"] and contract["workflow_revision"] == graph["workflow"]["revision"]
    assert contract["version"] == 1 and contract["node_count"] == 1 and contract["edge_count"] == 0
    assert contract["executor_requirements"] == [] and contract["node_template_uuid"]
    assert _ok(client.get(f"/api/v1/workflows/{child['uuid']}"))["status"] == "published"
    published = _ok(client.get("/api/v1/workflows", params={"workflow_type": "experiment_operation", "status": "published"}))
    assert [item["uuid"] for item in published["items"]] == [child["uuid"]]
    legacy = _ok(client.get("/api/v1/published-workflow-contracts"))
    assert [item["uuid"] for item in legacy["items"]] == [contract["uuid"]]

    parent = _ok(client.post("/api/v1/workflows", json={"name": "父流程"}), 201)
    invocation = str(uuid4())
    inserted = _ok(client.post(f"/api/v1/workflows/{parent['uuid']}/composite-invocations", json={
        "revision": parent["revision"], "contract_uuid": contract["uuid"], "invocation_uuid": invocation,
        "device_bindings": {}, "pose": {"x": 120, "y": 180}, "param": {},
    }))
    # 当前层：只有调用节点；私有展开节点由 restore 在整图保存时保留
    assert [item["uuid"] for item in inserted["nodes"]] == [invocation]
    assert len(service.get_graph(parent["uuid"])["nodes"]) == 2
    referenced = _ok(client.get(f"/api/v1/workflows/{child['uuid']}/referenced-by"))
    assert [item["uuid"] for item in referenced["items"]] == [parent["uuid"]]
    assert referenced["has_more"] is False
    resaved = _ok(client.put(f"/api/v1/workflows/{parent['uuid']}/graph", json={
        "revision": inserted["workflow"]["revision"], "nodes": inserted["nodes"], "edges": inserted["edges"],
    }))
    assert [item["uuid"] for item in resaved["nodes"]] == [invocation]
    assert len(service.get_graph(parent["uuid"])["nodes"]) == 2

    # V1 规则：普通工作流不能作为子工作流；子工作流当前修订须已发布；绑定必须与执行器需求一致
    plain = _ok(client.post("/api/v1/workflows", json={"name": "普通"}), 201)
    plain_graph = _ok(client.put(f"/api/v1/workflows/{plain['uuid']}/graph", json={
        "revision": plain["revision"], "nodes": [_canvas_node()], "edges": [],
    }))
    plain_contract = _ok(client.post(f"/api/v1/workflows/{plain['uuid']}/publications",
                                     json={"revision": plain_graph["workflow"]["revision"]}), 201)
    parent_revision = service.get_workflow(parent["uuid"])["revision"]
    assert _code(client.post(f"/api/v1/workflows/{parent['uuid']}/composite-invocations", json={
        "revision": parent_revision, "contract_uuid": plain_contract["uuid"],
    })) == 1000
    assert _code(client.post(f"/api/v1/workflows/{parent['uuid']}/composite-invocations", json={
        "revision": parent_revision, "contract_uuid": contract["uuid"], "device_bindings": {"x": str(uuid4())},
    })) == 1000
    assert _code(client.post(f"/api/v1/workflows/{parent['uuid']}/composite-invocations", json={
        "revision": parent_revision, "contract_uuid": str(uuid4()),
    })) == 3002
    # dev 旧 DTO 已移除
    assert _code(client.post(f"/api/v1/workflows/{parent['uuid']}/composite-invocations", json={
        "revision": parent_revision, "child_workflow_uuid": child["uuid"],
    })) == 1000
    _ok(client.put(f"/api/v1/workflows/{child['uuid']}", json={"name": "子操作（改）"}))
    _ok(client.put(f"/api/v1/workflows/{child['uuid']}/graph", json={
        "revision": service.get_workflow(child["uuid"])["revision"], "nodes": [_canvas_node("新")], "edges": [],
    }))
    stale = client.post(f"/api/v1/workflows/{parent['uuid']}/composite-invocations", json={
        "revision": parent_revision, "contract_uuid": contract["uuid"],
    }).json()
    assert stale["code"] == 3003


def test_run_preflight_get_and_v1_post_body(api) -> None:
    client, _service = api
    workflow = _ok(client.post("/api/v1/workflows", json={"name": "预检"}), 201)
    response = client.get(f"/api/v1/workflows/{workflow['uuid']}/run-preflight", params={"run_mode": "normal"})
    report = _ok(response)
    assert response.headers["cache-control"] == "no-store"
    assert report["workflow_uuid"] == workflow["uuid"] and report["run_mode"] == "normal"
    posted = _ok(client.post(f"/api/v1/workflows/{workflow['uuid']}/run-preflight", json={
        "run_mode": "batch", "input": {}, "inventory_bindings": [{
            "requirement_key": "water", "inventory_type": "reagent", "inventory_uuid": str(uuid4()),
            "reserved_quantity": 2.5, "quantity_unit": "mL",
        }],
    }))
    # V1 run_mode 为字符串：未知模式由报告里的检查项拒绝，而不是 1000
    assert posted["run_mode"] == "batch" and posted["status"] != "passed"
    assert posted["inventory_bindings"][0]["requirement_key"] == "water"


def test_authoring_apply_takes_only_candidate_hash(api) -> None:
    client, _service = api
    workflow = _ok(client.post("/api/v1/workflows", json={"name": "源码"}), 201)
    legacy_body = {"expected_draft_hash": "sha256:" + "0" * 64, "expected_workflow_revision": 1,
                   "expected_candidate_hash": "sha256:" + "1" * 64}
    assert _code(client.post(f"/api/v1/workflows/{workflow['uuid']}/authoring/apply", json=legacy_body)) == 1000
    assert _code(client.post(f"/api/v1/workflows/{workflow['uuid']}/authoring/apply",
                             json={"candidate_hash": "not-a-hash"})) == 1000
