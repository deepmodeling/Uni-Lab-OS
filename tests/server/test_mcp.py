"""MCP 的协议契约、真实 JSON-RPC 传输与业务边界。"""

from __future__ import annotations

import asyncio
import json
import re
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi import FastAPI
from jsonschema import Draft202012Validator
from starlette.testclient import TestClient

from unilabos.server.mcp import install_mcp
from unilabos.server.mcp.catalog import OPERATIONS
from unilabos.server.mcp.server import ProtocolMCP
from unilabos.server.openapi_export import export_openapi


@pytest.fixture(scope="module")
def contract():
    return export_openapi()


@pytest.fixture
def application(contract):
    app = FastAPI()

    @app.get("/api/v1/health")
    def health():
        return {"code": 0, "data": {"status": "ok"}}

    @app.post("/api/v1/workflows")
    def create(body: dict):
        return {"code": 0, "data": {"name": body["name"], "uuid": "workflow-test"}}

    @app.get("/api/v1/workflow-tasks/{task_uuid}")
    def task(task_uuid: str):
        return {"code": 0, "data": {"uuid": task_uuid, "status": "failed"}}

    install_mcp(app, contract)
    return app


def rpc(client, method, params=None, request_id=1):
    response = client.post("/mcp", json={"jsonrpc": "2.0", "id": request_id, "method": method,
                                         "params": params or {}},
                           headers={"Accept": "application/json, text/event-stream"})
    assert response.status_code == 200, response.text
    value = response.json()
    assert "error" not in value, value
    return value["result"]


def test_all_public_operations_use_current_protocol(contract, application):
    protocol = application.state.protocol_mcp
    assert len(OPERATIONS) == 132
    assert len(protocol.tools) == 138
    for tool in protocol.tools:
        assert len(tool.name) <= 64
        Draft202012Validator.check_schema(tool.inputSchema)
    assert len({operation.tool_name for operation in OPERATIONS}) == len(OPERATIONS)
    for operation in OPERATIONS:
        assert operation.method.lower() in contract["paths"][operation.path]
    operations = {(op.method, op.path) for op in OPERATIONS}
    for method, path in operations:
        if path.startswith(("/api/v1/runtime/", "/api/v1/telemetry/", "/api/v1/history/")):
            assert method == "GET", (method, path)
        assert "/snapshots/" not in path
        assert "/reservations" not in path or method == "GET"
        assert path not in {"/api/v1/events", "/api/v1/materials/events"}


def test_documented_tool_names_and_examples_match_protocol(application):
    guide = (Path(__file__).resolve().parents[2] / "docs/developer_guide/mcp_guide.md").read_text(encoding="utf-8")
    tools = {tool.name: tool for tool in application.state.protocol_mcp.tools}
    prefixes = (
        "protocol_", "system_", "runtime_v1_", "registry_", "materials_v1_", "decisions_",
        "workflow_workflow_", "workflow_graph_", "workflow_task_", "workflow_job_",
    )
    # registry_class 是写请求的字段名，不是 registry 域的工具。
    references = {name for name in re.findall(r"`([a-z][a-z0-9_]+)`", guide)
                  if name.startswith(prefixes) and name != "registry_class"}
    assert references
    assert references <= tools.keys(), f"指南引用了不存在的工具：{references - tools.keys()}"
    examples = re.findall(r"```json\n(.*?)\n```", guide, re.DOTALL)
    assert examples, "指南应包含可校验的工具调用示例"
    for example in examples:
        call = json.loads(example)
        Draft202012Validator(tools[call["name"]].inputSchema).validate(call["arguments"])
        if call["name"] == "protocol_batch":
            for entry in call["arguments"]["requests"]:
                Draft202012Validator(tools[entry["tool_name"]].inputSchema).validate(entry["arguments"])


def test_real_initialize_tools_resources_and_write(application):
    with TestClient(application, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        initialized = rpc(client, "initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                                  "clientInfo": {"name": "contract-test", "version": "1"}})
        assert initialized["serverInfo"]["name"] == "unilabos"
        assert len(rpc(client, "tools/list")["tools"]) == 138
        resources = rpc(client, "resources/list")["resources"]
        assert len(resources) == 2
        guide = rpc(client, "resources/read", {"uri": "unilab://protocol/guide"})
        assert "config.type" in guide["contents"][0]["text"]
        result = rpc(client, "tools/call", {"name": "system_health", "arguments": {}})
        assert not result["isError"]
        assert result["structuredContent"] == {"http_status": 200, "body": {"code": 0, "data": {"status": "ok"}}}
        result = rpc(client, "tools/call", {"name": "workflow_workflow_create", "arguments": {"body": {"name": "MCP 流程"}}})
        assert not result["isError"]
        assert result["structuredContent"]["body"]["data"]["name"] == "MCP 流程"
    assert application.state.mcp_manager is None
    # 重复 TestClient 生命周期不能重用 SDK 的单次 manager.run。
    with TestClient(application, base_url="http://localhost", client=("127.0.0.1", 50001)) as client:
        assert rpc(client, "tools/list")["tools"]


def test_invalid_input_never_reaches_http(application):
    with TestClient(application, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        for name, arguments in [
            ("workflow_workflow_create", {"body": {}}),
            ("system_health", {"url": "http://outside.invalid/"}),
            ("workflow_task_get", {"path": {"task_uuid": "../reset"}}),
            ("workflow_task_get", {"path": {"task_uuid": "%2e%2e"}}),
            ("workflow_task_get", {}),
            ("runtime_v1_jobs_create", {"body": {}}),
        ]:
            result = rpc(client, "tools/call", {"name": name, "arguments": arguments})
            assert result["isError"], (name, result)


@pytest.mark.parametrize("peer,headers,expected", [
    ("192.0.2.10", {}, 403),
    ("127.0.0.1", {"Origin": "https://evil.invalid"}, 403),
    ("127.0.0.1", {"Origin": "null"}, 403),
    ("127.0.0.1", {"Host": "evil.invalid"}, 421),
])
def test_local_only_and_rebinding_guard(application, peer, headers, expected):
    with TestClient(application, base_url="http://127.0.0.1", client=(peer, 50000)) as client:
        response = client.post("/mcp", headers={"Accept": "application/json, text/event-stream", **headers},
                               json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert response.status_code == expected, response.text


def test_business_failure_is_error_but_failed_task_is_data(application, contract):
    app = FastAPI()

    @app.get("/api/v1/health")
    def business_error():
        return {"code": 409, "message": "版本冲突", "data": None}

    protocol = ProtocolMCP(app, contract)
    result = asyncio.run(protocol.call_tool("system_health", {}))
    assert result.isError
    assert result.structuredContent["http_status"] == 200
    assert result.structuredContent["body"]["code"] == 409
    result = asyncio.run(application.state.protocol_mcp.call_tool("protocol_wait_task", {"task_uuid": "task-1"}))
    assert not result.isError
    assert result.structuredContent["reason"] == "terminal"
    assert result.structuredContent["body"]["data"]["status"] == "failed"
    result = asyncio.run(protocol.call_tool("workflow_task_get", {"path": {"task_uuid": "missing"}}))
    assert result.isError and result.structuredContent["http_status"] == 404


def test_large_results_are_bounded_and_pageable(application):
    protocol = application.state.protocol_mcp
    value = {"http_status": 200, "body": {"data": [{"name": str(i), "text": "x" * 1000} for i in range(100)]}}
    packed = protocol.pack(value).structuredContent
    assert packed["truncated"]
    page = protocol.read_result({"result_id": packed["result_id"], "pointer": "/body/data", "offset": 50, "limit": 2})
    assert [item["name"] for item in page["items"]] == ["50", "51"]
    assert page["next_offset"] == 52
    protocol.CACHE_TTL = -1
    with pytest.raises(ValueError, match="过期"):
        protocol.read_result({"result_id": packed["result_id"]})


def test_oversized_single_item_does_not_create_cache_loop(application):
    protocol = application.state.protocol_mcp
    packed = protocol.pack({"http_status": 200, "body": [{"devices": [{"uuid": "d"}], "large": "x" * 100_000}]}).structuredContent
    result_id = packed["result_id"]
    assert packed["outline"]["body"]["length"] == 1
    result = asyncio.run(protocol.call_tool("protocol_result_read", {"result_id": result_id, "pointer": "/body"}))
    assert result.structuredContent["result_id"] == result_id
    assert result.structuredContent["items"][0]["pointer"] == "/body/0"
    assert len(protocol.cache) == 1
    result = asyncio.run(protocol.call_tool("protocol_result_read", {"result_id": result_id, "pointer": "/body/0/devices"}))
    assert result.structuredContent["items"] == [{"uuid": "d"}]


def test_material_payload_comes_from_protocol(application):
    tool = next(tool for tool in application.state.protocol_mcp.tools if tool.name == "materials_v1_instances_instantiate")
    validator = Draft202012Validator(tool.inputSchema)
    valid = {"body": {"command_uuid": "c", "effect_key": "effect-1", "operation": "create_material_tree",
                       "payload": {"registry_class": "demo_plate_24", "name": "plate-1"}}}
    validator.validate(valid)
    valid["body"]["payload"] = {"klass": "Plate", "type": "Plate"}
    assert list(validator.iter_errors(valid))


def test_material_defaults_and_idempotency_use_real_http_authority(tmp_path, contract):
    from unilabos.server.api.materials import install_materials_api
    from unilabos.server.services.materials import MaterialsService

    service = MaterialsService(tmp_path / "materials.db")
    app = FastAPI()
    install_materials_api(app, service)
    protocol = ProtocolMCP(app, contract)
    try:
        created = asyncio.run(protocol.call_tool("materials_v1_templates_create", {"body": {
            "command_uuid": "template-command", "effect_key": "template-effect", "operation": "create_template",
            "payload": {"name": "mcp-water"},
        }}))
        assert not created.isError, created
        # body 是统一信封：data 为 MutationResult，其 data 才是模板
        template_uuid = created.structuredContent["body"]["data"]["data"]["template_uuid"]
        arguments = {"body": {"command_uuid": "lot-command", "effect_key": "lot-effect", "operation": "inbound_inventory_lot",
                               "payload": {"template_uuid": template_uuid, "unit": "ul", "quantity": 500}}}
        result = asyncio.run(protocol.call_tool("materials_v1_lots_inbound", arguments))
        assert not result.isError, result
        assert result.structuredContent["body"]["data"]["data"]["quantity_total"] == 500
        replay = asyncio.run(protocol.call_tool("materials_v1_lots_inbound", arguments))
        assert not replay.isError and replay.structuredContent["body"]["data"]["replayed"]
        assert replay.structuredContent["body"]["data"]["data"]["quantity_total"] == 500
        arguments["body"]["payload"]["quantity"] = 600
        conflict = asyncio.run(protocol.call_tool("materials_v1_lots_inbound", arguments))
        # 同幂等键不同请求：HTTP 200 + 物料冲突 4002，MCP 按 code != 0 标 isError
        assert conflict.isError and conflict.structuredContent["http_status"] == 200
        assert conflict.structuredContent["body"]["code"] == 4002
    finally:
        service.close()


def test_graph_validation_error_explains_how_to_fix(tmp_path, contract):
    from unilabos.server.api.runtime.workflow import create_workflow_app
    from unilabos.server.services.runtime.workflow.service import WorkflowService

    service = WorkflowService(tmp_path / "runtime.db")
    app = create_workflow_app(service)
    protocol = ProtocolMCP(app, contract)
    try:
        created = asyncio.run(protocol.call_tool("workflow_workflow_create", {"body": {"name": "graph check"}}))
        workflow = created.structuredContent["body"]["data"]
        result = asyncio.run(protocol.call_tool("workflow_graph_save", {
            "path": {"workflow_uuid": workflow["uuid"]},
            "body": {"revision": workflow["revision"], "nodes": [{"uuid": "b9724394-79cf-4c97-a51a-5970f45f589b",
                                                                 "name": "没有绑定设备", "type": "device_action"}], "edges": []},
        }))
        assert result.isError
        assert "material_uuid" in result.structuredContent["body"]["error"]["msg"]
    finally:
        service.close()


def test_batch_prevalidates_before_any_write(application):
    protocol = application.state.protocol_mcp
    results = asyncio.run(protocol.call_tool("protocol_batch", {"requests": [
        {"tool_name": "system_health", "arguments": {}},
        {"tool_name": "workflow_workflow_create", "arguments": {"body": {"name": "批量"}}},
    ]}))
    assert not results.isError
    assert results.structuredContent["atomic"] is False
    assert len(results.structuredContent["results"]) == 2
    results = asyncio.run(protocol.call_tool("protocol_batch", {"requests": [
        {"tool_name": "system_health", "arguments": {}},
        {"tool_name": "runtime_v1_jobs_create", "arguments": {"body": {}}},
    ]}))
    assert results.isError and "results" not in results.structuredContent


def test_task_wait_has_wall_clock_deadline(contract):
    app = FastAPI()

    @app.get("/api/v1/workflow-tasks/{task_uuid}")
    async def slow_task(task_uuid: str):
        await asyncio.sleep(30)

    protocol = ProtocolMCP(app, contract)
    async def execute():
        async with asyncio.timeout(1):
            return await protocol.call_tool("protocol_wait_task", {"task_uuid": "task-1", "timeout_seconds": 0.1})
    result = asyncio.run(execute())
    assert result.structuredContent["reason"] == "timeout"


def test_batch_validates_typed_payload_before_dispatch(application, monkeypatch):
    protocol = application.state.protocol_mcp
    dispatched = []

    async def request(operation, arguments):
        dispatched.append(operation.id)
        return {"http_status": 200, "body": {}}, False

    monkeypatch.setattr(protocol, "request", request)
    result = asyncio.run(protocol.call_tool("protocol_batch", {"requests": [
        {"tool_name": "workflow_workflow_create", "arguments": {"body": {"name": "不得写入"}}},
        {"tool_name": "materials_v1_trees_create", "arguments": {"body": {
            "command_uuid": "c", "effect_key": "e", "operation": "create_material_tree",
            "payload": {"nodes": []},
        }}},
    ]}))
    assert result.isError
    assert "at least one node" in result.structuredContent["error"]
    assert dispatched == []


def test_mount_preserves_lifespan_and_is_idempotent(contract):
    entered = []

    @asynccontextmanager
    async def lifecycle(app):
        entered.append("start")
        yield {"test": True}
        entered.append("stop")

    app = FastAPI(lifespan=lifecycle)
    first = install_mcp(app, contract)
    assert install_mcp(app, contract) is first
    assert len([route for route in app.routes if getattr(route, "path", "") == "/mcp"]) == 1
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)):
        assert entered == ["start"]
    assert entered == ["start", "stop"]
