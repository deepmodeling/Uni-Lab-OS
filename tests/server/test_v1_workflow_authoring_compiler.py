"""工作流作者编译器（dev ``@workflow`` 语法，复用 ``registry/workflow_ast``）与 V1 作者路由。

覆盖：``/local/workflows/import-python``、``/workflows/{id}/authoring`` 草稿 → 候选 →
``authoring/apply {candidate_hash}``、``/authoring/compile|generate-python|validate``、
``/workflows/import``（旧版 JSON），以及 CLI ``workflow authoring`` 所用的客户端轮询。
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from unilabos.registry.workflows import DeviceCatalog
from unilabos.server.api.runtime.workflow import create_workflow_app
from unilabos.server.services.runtime.workflow.authoring_compiler import WorkflowAuthoringCompiler
from unilabos.server.services.runtime.workflow.service import WorkflowService

PUMP_UUID = "11111111-0000-4000-8000-000000000001"
SOURCE = '''from unilabos.registry.workflows import workflow


@workflow("加样流程", tags=["demo"])
def flow(ctx, *, amount: float = 2.0):
    first = ctx.run("pump-1/dose", {"volume": amount}, name="加样")
    ctx.run("pump-1/dose", {"volume": first.volume}, name="复加")
'''


@pytest.fixture
def api(tmp_path):
    catalog = DeviceCatalog()
    catalog.add("pump-1", "pump", PUMP_UUID)
    service = WorkflowService(tmp_path / "runtime.db")
    service.configure_authoring(
        compiler=WorkflowAuthoringCompiler(catalog_provider=lambda: catalog, registry_fingerprint=lambda: "r1"),
        source_root=tmp_path / "workflow_sources",
    )
    try:
        yield TestClient(create_workflow_app(service)), service, tmp_path
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


def _import(client, source: str = SOURCE, name: str = "dose_flow.py"):
    return client.post("/api/v1/local/workflows/import-python", content=source.encode("utf-8"),
                       headers={"X-Workflow-Filename": name, "Content-Type": "text/x-python"})


def test_import_python_creates_applied_workflow_with_source(api) -> None:
    client, service, tmp_path = api
    graph = _ok(_import(client), 201)
    workflow = graph["workflow"]
    assert workflow["name"] == "加样流程" and workflow["tags"] == ["demo"] and workflow["revision"] == 2
    assert [node["name"] for node in graph["nodes"]] == ["加样", "复加"]
    first, second = graph["nodes"]
    assert first["param"] == {"volume": {"kind": "workflow_input", "parameter": "amount"}}
    assert second["param"] == {"volume": {"kind": "node_output", "workflow_node_uuid": first["uuid"], "data_key": "volume"}}
    assert second["execution_policy"]["depends_on"] == [first["uuid"]]
    assert first["material_uuid"] == PUMP_UUID and first["meta_data"]["target_device_id"] == "pump-1"
    assert workflow["meta_data"]["unilab"]["input_contract"]["parameters"][0]["name"] == "amount"
    authoring = _ok(client.get(f"/api/v1/workflows/{workflow['uuid']}/authoring"))
    assert authoring["state"] == "applied" and authoring["draft"]["python_source"] == SOURCE
    assert authoring["draft"]["source_uri"].startswith("package://local/workflows/dose_flow_")
    assert list((tmp_path / "workflow_sources" / "workflows").glob("dose_flow_*.py"))

    # 不合法的源码 / 文件名：1000 / 3003，不留下工作流
    before = service.list_workflows()["total"]
    assert _code(_import(client, "import os\n")) == 1000
    assert _code(_import(client, name="../evil.py")) == 1000
    broken = SOURCE.replace('"pump-1/dose"', '"pump-1"')
    assert _code(_import(client, broken)) == 1000
    assert service.list_workflows()["total"] == before


def test_draft_candidate_apply_with_candidate_hash(api) -> None:
    client, service, _tmp = api
    workflow = _ok(client.post("/api/v1/workflows", json={"name": "从零编写"}), 201)
    authoring = _ok(client.get(f"/api/v1/workflows/{workflow['uuid']}/authoring"))
    assert authoring["state"] == "draft_missing" and authoring["workflow_revision"] == 1
    saved = _ok(client.put(f"/api/v1/workflows/{workflow['uuid']}/authoring/draft", json={
        "python_source": SOURCE, "expected_draft_hash": None, "expected_workflow_revision": 1,
    }))
    assert saved["state"] == "unapplied_graph"
    candidate = saved["candidate"]
    assert candidate["changeset"]["kind"] == "graph" and len(candidate["changeset"]["created_node_uuids"]) == 2
    assert _code(client.post(f"/api/v1/workflows/{workflow['uuid']}/authoring/apply",
                             json={"candidate_hash": "sha256:" + "0" * 64})) == 3003
    applied = _ok(client.post(f"/api/v1/workflows/{workflow['uuid']}/authoring/apply",
                              json={"candidate_hash": candidate["candidate_hash"]}))
    assert applied["apply_result"]["workflow_revision"] == 2 and applied["authoring"]["state"] == "applied"
    assert len(service.get_graph(workflow["uuid"])["nodes"]) == 2
    # 草稿编译失败：带源码范围的诊断，不签发候选
    broken = _ok(client.put(f"/api/v1/workflows/{workflow['uuid']}/authoring/draft", json={
        "python_source": SOURCE + "    print(1)\n", "expected_draft_hash": applied["authoring"]["draft"]["draft_hash"],
        "expected_workflow_revision": 2,
    }))
    assert broken["state"] == "draft_invalid" and broken["candidate"] is None
    diagnostic = broken["draft"]["diagnostics"][0]
    assert diagnostic["code"] == "authoring_syntax" and diagnostic["source_range"]["start_line"] == 8
    assert _code(client.post(f"/api/v1/workflows/{workflow['uuid']}/authoring/apply",
                             json={"candidate_hash": candidate["candidate_hash"]})) == 3003


def test_authoring_transforms_round_trip_canvas_graph(api) -> None:
    client, service, _tmp = api
    workflow = _ok(client.post("/api/v1/workflows", json={"name": "画布"}), 201)
    first, second = str(uuid4()), str(uuid4())
    node = {"type": "device_action", "material_uuid": PUMP_UUID, "action_name": "dose", "action_type": "UniLabJsonCommand",
            "meta_data": {"target_device_id": "pump-1"}}
    graph = _ok(client.put(f"/api/v1/workflows/{workflow['uuid']}/graph", json={"revision": 1, "edges": [], "nodes": [
        {**node, "uuid": first, "name": "一", "param": {"volume": 1}, "pose": {"x": 10, "y": 20}},
        {**node, "uuid": second, "name": "二", "param": {"volume": 2}, "execution_policy": {"depends_on": [first]}},
    ]}))
    identity = {"workflow_uuid": workflow["uuid"], "revision": graph["workflow"]["revision"], "source_uri": "package://local/workflows/x.py"}
    generated = _ok(client.post("/api/v1/authoring/generate-python", json={**identity, "graph": graph}))
    assert generated["diagnostics"] == [] and generated["changeset"]["kind"] == "source_only"
    source = generated["normalized_python_source"]
    assert f"# unilab:node_uuid {first}" in source and 'ctx.run("pump-1/dose", {"volume": 1}, name="一")' in source
    assert {entry["workflow_node_uuid"] for entry in generated["source_map"]} == {first, second}
    validated = _ok(client.post("/api/v1/authoring/validate", json={**identity, "graph": graph, "python_source": source}))
    assert validated["changeset"]["kind"] == "source_only"
    compiled = _ok(client.post("/api/v1/authoring/compile", json={
        **identity, "applied_graph": graph, "python_source": source.replace('{"volume": 2}', '{"volume": 5}'),
    }))
    assert compiled["changeset"]["kind"] == "graph" and compiled["changeset"]["updated_node_uuids"] == [second]
    by_uuid = {item["uuid"]: item for item in compiled["graph"]["nodes"]}
    assert by_uuid[second]["param"] == {"volume": 5} and by_uuid[first]["pose"] == {"x": 10, "y": 20}
    mismatch = _ok(client.post("/api/v1/authoring/validate", json={
        **identity, "graph": graph, "python_source": source.replace('{"volume": 2}', '{"volume": 5}'),
    }))
    assert mismatch["graph"] is None and mismatch["diagnostics"][0]["code"] == "graph_not_representable"
    failed = _ok(client.post("/api/v1/authoring/compile", json={**identity, "applied_graph": graph, "python_source": "x = (\n"}))
    assert failed["graph"] is None and failed["diagnostics"][0]["code"] == "authoring_syntax"
    # 身份不一致 / 未知字段：1000
    assert _code(client.post("/api/v1/authoring/compile", json={
        **identity, "workflow_uuid": str(uuid4()), "applied_graph": graph, "python_source": source})) == 1000
    assert _code(client.post("/api/v1/authoring/validate", json={
        **identity, "graph": graph, "python_source": source, "extra": 1})) == 1000


def test_transforms_without_compiler_report_unavailable() -> None:
    service = WorkflowService(":memory:")
    try:
        client = TestClient(create_workflow_app(service))
        workflow = _ok(client.post("/api/v1/workflows", json={"name": "x"}), 201)
        graph = _ok(client.get(f"/api/v1/workflows/{workflow['uuid']}/graph"))
        assert _code(client.post("/api/v1/authoring/compile", json={
            "workflow_uuid": workflow["uuid"], "revision": 1, "source_uri": "x", "applied_graph": graph,
            "python_source": SOURCE})) == 5001
        assert _code(client.get(f"/api/v1/workflows/{workflow['uuid']}/authoring")) == 3002
        assert _code(_import(client)) == 5001
    finally:
        service.close()


def test_legacy_json_import_rebuilds_identities(api) -> None:
    client, service, _tmp = api
    first, second = str(uuid4()), str(uuid4())
    node = {"type": "device_action", "material_uuid": PUMP_UUID, "action_name": "dose",
            "meta_data": {"target_device_id": "pump-1"}, "pose": {"x": 0, "y": 0}}
    imported = _ok(client.post("/api/v1/workflows/import", json={"data": {
        "workflow_name": "旧版导入", "workflow_type": "experiment_operation",
        "nodes": [{**node, "uuid": first, "name": "一", "param": {"volume": 1}},
                  {**node, "uuid": second, "name": "二", "param": {"volume": 2}, "parent_uuid": None}],
        "edges": [],
    }}), 201)
    assert imported["workflow"]["name"] == "旧版导入" and imported["workflow"]["workflow_type"] == "experiment_operation"
    assert {node["name"] for node in imported["nodes"]} == {"一", "二"}
    assert not {node["uuid"] for node in imported["nodes"]} & {first, second}
    before = service.list_workflows()["total"]
    assert _code(client.post("/api/v1/workflows/import", json={"name": "空", "nodes": []})) == 1000
    assert _code(client.post("/api/v1/workflows/import", json={"nodes": [{**node, "uuid": first, "name": "x"}]})) == 1000
    assert _code(client.post("/api/v1/workflows/import", json={
        "name": "坏节点", "nodes": [{**node, "uuid": first, "name": "x", "type": "nonsense"}]})) != 0
    assert service.list_workflows()["total"] == before
