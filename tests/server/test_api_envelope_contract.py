"""统一信封机制（``unilabos/server/api/envelope.py``）的契约测试。

覆盖：成功包装、各类失败（HTTPException 按错误口径、ApiError、领域异常、请求校验、非法 JSON）、
不包信封的端点（探针、SSE、未知异常）、完整装配后的 ``/api/v1`` 路由审计、控制面 token
（只认 X-UniLab-Token，401 → 1002）、SSE 事件名映射与 Python 客户端解包。
"""

from __future__ import annotations

from typing import Optional

import pytest
from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from pydantic import BaseModel

from tests.api_envelope import error_of, ok_data
from unilabos.client.http import HTTPClient, HTTPClientConfig
from unilabos.client.utils.envelope import (
    APIHTTPError,
    EnvelopeError,
    decode_json_body,
    http_status_for_code,
)
from unilabos.config.config import BasicConfig
from unilabos.server.api import envelope
from unilabos.server.api.envelope import (
    RESOURCE,
    WORKFLOW,
    ApiError,
    EnvelopeRoute,
    envelope_router,
    iter_api_routes,
    raw_response,
)


class Item(BaseModel):
    name: str
    count: int = 0


def _probe_app(profile=envelope.GENERIC) -> FastAPI:
    router = envelope_router(prefix="/api/v1/probe", profile=profile)

    @router.get("/item", response_model=Item)
    def get_item() -> Item:
        return Item(name="a")

    @router.get("/items")
    async def list_items() -> list[Item]:
        return [Item(name="a"), Item(name="b", count=2)]

    @router.post("/items", status_code=201)
    def create_item(body: Item) -> Item:
        return body

    @router.delete("/items/{name}", status_code=204)
    def delete_item(name: str) -> None:
        return None

    @router.get("/status/{status}")
    def fail_with(status: int):
        raise HTTPException(status_code=status, detail=f"status {status}")

    @router.get("/api-error")
    def api_error():
        raise ApiError(
            envelope.MATERIAL_SITE_OCCUPIED, "库位已占用", symbol="site_occupied", details={"site_uuid": "s1"}
        )

    @router.get("/query")
    def query(limit: int = Query(ge=1)) -> int:
        return limit

    @router.get("/health")
    @raw_response
    def health() -> dict:
        return {"status": "ok"}

    @router.get("/stream")
    def stream() -> StreamingResponse:
        return StreamingResponse(iter(["data: 1\n\n"]), media_type="text/event-stream")

    @router.get("/boom")
    def boom():
        raise RuntimeError("未登记的异常")

    @router.get("/empty", status_code=204, response_class=Response)
    def empty() -> Response:
        return Response(status_code=204)

    app = FastAPI()
    app.include_router(router)
    return app


# ── 成功 ─────────────────────────────────────────────────────────


def test_success_wraps_plain_return_values_and_keeps_status_codes() -> None:
    client = TestClient(_probe_app())
    assert client.get("/api/v1/probe/item").json() == {"code": 0, "data": {"name": "a", "count": 0}}
    assert ok_data(client.get("/api/v1/probe/items")) == [{"name": "a", "count": 0}, {"name": "b", "count": 2}]
    created = client.post("/api/v1/probe/items", json={"name": "c"})
    assert created.status_code == 201 and created.json() == {"code": 0, "data": {"name": "c", "count": 0}}
    # 无数据的命令：HTTP 200 + {"code": 0}（不再是 204 空响应）
    for response in (client.delete("/api/v1/probe/items/c"), client.get("/api/v1/probe/empty")):
        assert response.status_code == 200 and response.json() == {"code": 0}


def test_openapi_documents_the_envelope_model() -> None:
    schema = TestClient(_probe_app()).get("/openapi.json").json()
    ref = schema["paths"]["/api/v1/probe/item"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    model = schema["components"]["schemas"][ref["$ref"].rsplit("/", 1)[-1]]
    assert set(model["properties"]) == {"code", "data"}


# ── 失败 ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("profile", "status", "code"),
    [
        (WORKFLOW, 404, envelope.WORKFLOW_NOT_FOUND),
        (envelope.GENERIC, 404, envelope.RESOURCE_NOT_FOUND),
        (RESOURCE, 404, envelope.RESOURCE_NOT_FOUND),
        (WORKFLOW, 409, envelope.STATE_CONFLICT),
        (envelope.GENERIC, 409, envelope.STATE_CONFLICT),
        (RESOURCE, 409, envelope.RESOURCE_DATA_CONFLICT),
        (envelope.GENERIC, 422, envelope.INVALID_PARAMETER),
        (envelope.GENERIC, 400, envelope.INVALID_PARAMETER),
        (envelope.GENERIC, 403, envelope.OPERATION_NOT_ALLOWED),
        (envelope.GENERIC, 503, envelope.SERVICE_UNAVAILABLE),
        (envelope.GENERIC, 410, envelope.INTERNAL_ERROR),
        (envelope.GENERIC, 500, envelope.INTERNAL_ERROR),
    ],
)
def test_http_exceptions_map_to_business_codes_at_http_200(profile, status, code) -> None:
    client = TestClient(_probe_app(profile))
    error = error_of(client.get(f"/api/v1/probe/status/{status}"), code)
    assert error == {"msg": f"status {status}"}


def test_401_keeps_its_http_status_with_code_1002() -> None:
    client = TestClient(_probe_app())
    assert error_of(client.get("/api/v1/probe/status/401"), envelope.UNAUTHORIZED, status=401)


def test_api_error_carries_symbol_and_details() -> None:
    client = TestClient(_probe_app())
    error = error_of(client.get("/api/v1/probe/api-error"), 6007)
    assert error == {"msg": "库位已占用", "code": "site_occupied", "details": {"site_uuid": "s1"}}


def test_validation_and_malformed_json_return_code_1000() -> None:
    client = TestClient(_probe_app())
    assert "limit" in error_of(client.get("/api/v1/probe/query", params={"limit": 0}), 1000)["msg"]
    assert "name" in error_of(client.post("/api/v1/probe/items", json={"count": 1}), 1000)["msg"]
    malformed = client.post(
        "/api/v1/probe/items", content=b"{not json", headers={"content-type": "application/json"}
    )
    error_of(malformed, 1000)


def test_raw_endpoints_streams_and_unknown_exceptions_are_not_enveloped() -> None:
    client = TestClient(_probe_app(), raise_server_exceptions=False)
    assert client.get("/api/v1/probe/health").json() == {"status": "ok"}
    stream = client.get("/api/v1/probe/stream")
    assert stream.headers["content-type"].startswith("text/event-stream") and stream.text == "data: 1\n\n"
    # 未登记的异常是服务端缺陷：照常 500，不伪装成业务失败
    assert client.get("/api/v1/probe/boom").status_code == 500


@pytest.mark.parametrize(
    ("exc_factory", "code", "symbol"),
    [
        (lambda: _workflow_error("not_found"), 3002, None),
        (lambda: _workflow_error("conflict"), 3003, None),
        (lambda: _workflow_error("invalid_input"), 1000, None),
        (lambda: _workflow_error("template_catalog_unavailable"), 5001, None),
        (lambda: _workflow_error("local_task_authority_forbidden"), 1001, None),
        (lambda: _workflow_error("internal_error"), 1, None),
        # 物料域：业务码由异常显式携带的 subject / conflict 决定，消息文字不再参与判断
        (lambda: _materials("MaterialNotFoundError", "material not found: m1", subject="material"), 6000, None),
        (lambda: _materials("MaterialNotFoundError", "material root not found: m1", subject="material"), 6000, None),
        (lambda: _materials("MaterialNotFoundError", "site not found: s1", subject="site"), 6006, None),
        (lambda: _materials("MaterialNotFoundError", "template not found: t1", subject="template"), 6001, None),
        (lambda: _materials("MaterialNotFoundError", "parent not found: p1", subject="parent"), 6003, None),
        (lambda: _materials("MaterialNotFoundError", "inventory lot not found: l1", subject="lot"), 4001, None),
        (lambda: _materials("MaterialNotFoundError", "material not found: m1"), 4001, None),
        (lambda: _materials("MaterialConflictError", "destination Site s1 is occupied", conflict="site_occupied"), 6007, None),
        (lambda: _materials("MaterialConflictError", "物料或库位仍由未结算动作 j1 占用", conflict="active_claim"), 6011, None),
        (lambda: _materials("MaterialConflictError", "material uuid already exists: m1", conflict="identity"), 6010, None),
        (lambda: _materials("MaterialConflictError", "destination Site s1 is occupied"), 4002, None),
        (lambda: _materials("MaterialConflictError", "material m1 state changed"), 4002, None),
        (lambda: _materials("MaterialValidationError", "bad"), 1000, None),
        (lambda: _materials("MaterialTransferSyncError", "device"), 5001, None),
        (lambda: _graph("not_found"), 4001, None),
        (lambda: _graph("identity_conflict"), 4002, None),
        (lambda: _chemical("conflict"), 4002, None),
        (lambda: _chemical("not_found"), 4001, None),
    ],
)
def test_domain_errors_map_through_the_central_table(exc_factory, code, symbol) -> None:
    api_error = envelope.to_api_error(exc_factory(), WORKFLOW)
    assert api_error is not None and api_error.code == code and api_error.symbol == symbol
    assert api_error.to_body()["error"]["msg"]


def test_unregistered_exceptions_are_left_to_the_framework() -> None:
    assert envelope.to_api_error(RuntimeError("x")) is None


def _workflow_error(code: str):
    from unilabos.server.services.runtime.workflow.service import WorkflowError

    return WorkflowError(code)


def _materials(name: str, message: str, **kwargs):
    from unilabos.server.services.materials import core

    return getattr(core, name)(message, **kwargs)


def _graph(code: str):
    from unilabos.server.services.materials.graph import GraphError

    return GraphError(code, "graph")


def _chemical(code: str):
    from unilabos.server.services.materials.chemicals import ChemicalError

    return ChemicalError(code, "chemical")


def test_code_table_matches_v1() -> None:
    assert {
        "INVALID_PARAMETER": 1000, "OPERATION_NOT_ALLOWED": 1001, "UNAUTHORIZED": 1002, "DATABASE_CONFLICT": 2005,
        "WORKFLOW_NOT_FOUND": 3002, "STATE_CONFLICT": 3003, "REAGENT_INFO_IN_USE": 4000,
        "RESOURCE_NOT_FOUND": 4001, "RESOURCE_DATA_CONFLICT": 4002, "RESOURCE_TEMPLATE_NOT_FOUND": 5000,
        "SERVICE_UNAVAILABLE": 5001, "TEMPLATE_DEFINITION_INVALID": 5003, "TEMPLATE_DATA_CONFLICT": 5004,
        "MATERIAL_NOT_FOUND": 6000, "MATERIAL_TEMPLATE_NOT_FOUND": 6001, "MATERIAL_TEMPLATE_IMMUTABLE": 6002,
        "MATERIAL_PARENT_NOT_FOUND": 6003, "MATERIAL_PARENT_CYCLE": 6004, "MATERIAL_SITE_NOT_FOUND": 6006,
        "MATERIAL_SITE_OCCUPIED": 6007, "MATERIAL_SITE_TEMPLATE_NOT_ALLOWED": 6008, "MATERIAL_SITE_CYCLE": 6009,
        "MATERIAL_IDENTITY_CONFLICT": 6010, "MATERIAL_ACTIVE_CLAIM_CONFLICT": 6011,
    } == {name: getattr(envelope, name) for name in (
        "INVALID_PARAMETER", "OPERATION_NOT_ALLOWED", "UNAUTHORIZED", "DATABASE_CONFLICT", "WORKFLOW_NOT_FOUND",
        "STATE_CONFLICT", "REAGENT_INFO_IN_USE", "RESOURCE_NOT_FOUND", "RESOURCE_DATA_CONFLICT",
        "RESOURCE_TEMPLATE_NOT_FOUND", "SERVICE_UNAVAILABLE", "TEMPLATE_DEFINITION_INVALID",
        "TEMPLATE_DATA_CONFLICT", "MATERIAL_NOT_FOUND", "MATERIAL_TEMPLATE_NOT_FOUND",
        "MATERIAL_TEMPLATE_IMMUTABLE", "MATERIAL_PARENT_NOT_FOUND", "MATERIAL_PARENT_CYCLE",
        "MATERIAL_SITE_NOT_FOUND", "MATERIAL_SITE_OCCUPIED", "MATERIAL_SITE_TEMPLATE_NOT_ALLOWED",
        "MATERIAL_SITE_CYCLE", "MATERIAL_IDENTITY_CONFLICT", "MATERIAL_ACTIVE_CLAIM_CONFLICT",
    )}
    assert set(envelope.CODE_MESSAGES) >= {0, 1, 1000, 1001, 1002, 3002, 3003, 4001, 5001, 6000}


# ── 完整装配后的路由审计 ─────────────────────────────────────────

#: 允许不走信封的 ``/api/v1`` 路由：探针、Host↔权威内部通道（见 docs/developer_guide/api_v1_migration.md）。
_RAW_ALLOWED = {"/api/v1/health", "/api/v1/readiness", "/api/v1/edge/http-responses/{request_uuid}"}


def _full_app(tmp_path) -> tuple[FastAPI, list]:
    from unilabos.server.api import install_server_apis
    from unilabos.server.api.device_processes import create_device_processes_router
    from unilabos.server.api.driver_package_graphs import create_driver_package_graphs_router
    from unilabos.server.api.driver_packages import create_driver_packages_router
    from unilabos.server.api.host_relay import create_host_relay_router
    from unilabos.server.api.runtime import create_backend_router, install_edge_control_api
    from unilabos.server.api.runtime.logs import create_runtime_logs_router
    from unilabos.server.api.runtime.registry import install_registry_api
    from unilabos.server.api.runtime.workflow import install_workflow_api
    from unilabos.server.composition import ServerServices
    from unilabos.server.database import ServerDatabasePaths
    from unilabos.server.services.runtime.workflow.service import WorkflowService

    services = ServerServices.open(ServerDatabasePaths.resolve(tmp_path))
    workflow = WorkflowService(":memory:")
    app = FastAPI()
    for router in (
        create_driver_packages_router(),
        create_driver_package_graphs_router(),
        create_device_processes_router(),
        create_runtime_logs_router(),
        create_host_relay_router(),
        create_backend_router(lambda: None, lambda: None),
    ):
        app.include_router(router)
    install_workflow_api(app, workflow)
    install_registry_api(app)
    install_server_apis(app, services)
    install_edge_control_api(app)
    return app, [services, workflow]


def test_every_api_v1_json_route_is_enveloped(tmp_path) -> None:
    from fastapi.routing import APIRoute

    app, closers = _full_app(tmp_path)
    try:
        routes = [(path, route) for path, route in iter_api_routes(app.routes) if isinstance(route, APIRoute)]
        api_routes = [(path, route) for path, route in routes if path.startswith("/api/v1")]
        assert len(api_routes) > 150
        unwrapped = sorted(
            path for path, route in api_routes
            if path not in _RAW_ALLOWED
            and not (isinstance(route, EnvelopeRoute) and not envelope.is_raw_endpoint(route.endpoint))
        )
        assert unwrapped == []
        # 探针确实是裸 JSON，内部通道保持普通路由
        raw = {path for path, route in api_routes if envelope.is_raw_endpoint(route.endpoint)}
        assert {"/api/v1/health", "/api/v1/readiness"} <= raw
    finally:
        for closer in closers:
            closer.close()


def test_full_app_answers_errors_with_the_envelope(tmp_path) -> None:
    app, closers = _full_app(tmp_path)
    try:
        with TestClient(app) as client:
            missing = "5b1c2f0e-8a8b-4c39-9a51-0d4f3c9a7e21"
            error_of(client.get(f"/api/v1/workflow-tasks/{missing}"), 3002)
            error_of(client.get("/api/v1/materials/instances/missing"), 6000)
            error_of(client.get("/api/v1/runtime/jobs/missing"), 4001)
            error_of(client.get("/api/v1/telemetry/events/missing"), 4001)
            error_of(client.get("/api/v1/history/events/missing"), 4001)
            error_of(client.get("/api/v1/graphs/missing"), 4001)
            error_of(client.get("/api/v1/scheduler/drain"), 5001)
            error_of(client.post("/api/v1/workflows", json={}), 1000)
            error_of(client.put("/api/v1/lab/layout", content=b"[", headers={"content-type": "application/json"}), 1000)
            assert ok_data(client.get("/api/v1/lab/layout"))["revision"] == 0
            assert ok_data(client.delete("/api/v1/lab/layout")) is None
            assert client.get("/api/v1/health").json()["status"] == "ok"
    finally:
        for closer in closers:
            closer.close()


# ── 控制面 token ─────────────────────────────────────────────────


def _token_app() -> FastAPI:
    from unilabos.server.api.app import ConfiguredCORSMiddleware, ControlTokenMiddleware

    router = envelope_router(prefix="/api/v1/things")

    @router.post("")
    def create() -> dict:
        return {"created": True}

    app = FastAPI()
    app.include_router(router)
    app.add_middleware(ControlTokenMiddleware)
    app.add_middleware(ConfiguredCORSMiddleware)
    return app


def test_control_token_only_accepts_x_unilab_token(monkeypatch) -> None:
    monkeypatch.setattr(BasicConfig, "control_token", "secret")
    monkeypatch.setattr(BasicConfig, "cors_allow_origins", "*")
    client = TestClient(_token_app())
    missing = client.post("/api/v1/things", json={})
    error = error_of(missing, envelope.UNAUTHORIZED, status=401)
    assert "X-UniLab-Token" in error["msg"]
    assert missing.headers["www-authenticate"].startswith("X-UniLab-Token")
    # Bearer 留给 V1 工站 api_key：控制面不再接受
    error_of(client.post("/api/v1/things", json={}, headers={"Authorization": "Bearer secret"}), 1002, status=401)
    assert ok_data(client.post("/api/v1/things", json={}, headers={"X-UniLab-Token": "secret"})) == {"created": True}
    preflight = client.options(
        "/api/v1/things",
        headers={
            "Origin": "https://fe.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "X-UniLab-Token, Idempotency-Key, X-Workflow-Filename, Authorization",
        },
    )
    assert preflight.status_code == 200
    allowed = preflight.headers["access-control-allow-headers"].lower()
    for header in ("x-unilab-token", "idempotency-key", "x-workflow-filename", "authorization"):
        assert header in allowed


# ── SSE 事件名 ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("dev_name", "v1_name", "kind"),
    [
        ("workflow.task.changed", "workflow.runtime.changed", "task"),
        ("workflow.node_run.changed", "workflow.runtime.changed", "node_run"),
        ("workflow.node_job.changed", "workflow.runtime.changed", "node_job"),
        ("workflow.manual_confirmation.changed", "workflow.runtime.changed", "manual_confirmation"),
        ("workflow.resource.changed", "workflow.runtime.changed", "resource"),
        ("workflow.authoring.changed", "workflow.authoring.changed", None),
        ("stream.reset", "stream.reset", None),
        ("runtime.logs.changed", "runtime.logs.changed", None),
    ],
)
def test_sse_events_use_v1_names_and_keep_the_payload(dev_name, v1_name, kind) -> None:
    from unilabos.server.api.runtime.events import to_v1_event

    event = {"id": 7, "event": dev_name, "data": {"workflow_task_uuid": "t1", "status": "running"}}
    mapped = to_v1_event(event)
    assert mapped["event"] == v1_name and mapped["id"] == 7
    assert mapped["data"]["workflow_task_uuid"] == "t1" and mapped["data"]["status"] == "running"
    assert mapped["data"].get("kind") == kind
    assert event["data"] == {"workflow_task_uuid": "t1", "status": "running"}


# ── Python 客户端 ────────────────────────────────────────────────


def _client_for(app: FastAPI, **config) -> HTTPClient:
    client = HTTPClient(HTTPClientConfig(base_url="http://testserver/api/v1", max_retries=0, **config))
    client._client = TestClient(app, base_url="http://testserver/api/v1")
    return client


def test_http_client_unwraps_once_and_raises_typed_errors(monkeypatch) -> None:
    monkeypatch.setattr(BasicConfig, "control_token", "")
    client = _client_for(_probe_app(WORKFLOW))
    assert client.get("/probe/item") == {"name": "a", "count": 0}
    assert client.delete("/probe/items/x") is None
    with pytest.raises(EnvelopeError) as caught:
        client.get("/probe/status/404")
    assert caught.value.code == 3002 and caught.value.error == "status 404"
    assert caught.value.http_status == 200 and caught.value.status_code == 404
    with pytest.raises(EnvelopeError) as caught:
        client.get("/probe/api-error")
    assert (caught.value.code, caught.value.symbol, caught.value.details) == (6007, "site_occupied", {"site_uuid": "s1"})
    assert client.get("/probe/health", envelope=False) == {"status": "ok"}


def test_http_client_reports_the_token_rejection_as_1002(monkeypatch) -> None:
    monkeypatch.setattr(BasicConfig, "control_token", "secret")
    monkeypatch.setattr(BasicConfig, "cors_allow_origins", "*")
    monkeypatch.setattr("unilabos.config.control_token.client_token", lambda: "wrong")
    with pytest.raises(EnvelopeError) as caught:
        _client_for(_token_app()).post("/things", json={})
    assert caught.value.code == 1002 and caught.value.http_status == 401


def test_body_decoder_and_domain_client_errors() -> None:
    assert decode_json_body(b'{"code":0,"data":[1]}') == [1]
    assert decode_json_body(b'{"code":0}') is None
    with pytest.raises(EnvelopeError) as caught:
        decode_json_body(b'{"code":6006,"error":{"msg":"site not found: s"}}')
    assert caught.value.status_code == 404
    with pytest.raises(EnvelopeError) as caught:
        decode_json_body(b'{"detail":"Not Found"}', http_status=404)
    assert (caught.value.code, caught.value.error, caught.value.status_code) == (4001, "Not Found", 404)
    with pytest.raises(ValueError):
        decode_json_body(b'{"status":"ok"}')
    assert decode_json_body(b'{"status":"ok"}', envelope=False) == {"status": "ok"}
    assert [http_status_for_code(code) for code in (1000, 1002, 3002, 3003, 4001, 4002, 5001, 6007, 1)] == [
        422, 401, 404, 409, 404, 409, 503, 409, 500,
    ]

    class DemoHTTPError(APIHTTPError):
        api_name = "demo"

    error = DemoHTTPError.from_envelope_error(EnvelopeError(3003, "冲突", symbol="x", http_status=200))
    assert (error.status_code, error.detail, error.business_code, error.symbol) == (409, "冲突", 3003, "x")
    assert str(error) == "demo API returned 409: 冲突"


def test_optional_annotation_is_supported() -> None:
    router = envelope_router(prefix="/api/v1/opt")

    @router.get("")
    def maybe(flag: bool = False) -> Optional[Item]:
        return Item(name="x") if flag else None

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    assert client.get("/api/v1/opt").json() == {"code": 0}
    assert ok_data(client.get("/api/v1/opt", params={"flag": True})) == {"name": "x", "count": 0}


# ── 权威经控制面读写 Host：信封失败不能当成功 ─────────────────────


@pytest.mark.parametrize(
    "body",
    [
        '{"code":1000,"error":{"msg":"scope 无效"}}'.encode(),
        '{"code":5001,"error":{"msg":"Host 正在重启"}}'.encode(),
        b"not json",
    ],
)
def test_edge_restart_notification_rejected_by_the_host_keeps_dispatch_paused(monkeypatch, body) -> None:
    from types import SimpleNamespace

    from unilabos.server.backend.restart import RestartCoordinator

    coordinator = RestartCoordinator(lambda: None, lambda: None)
    rejected = SimpleNamespace(status_code=200, body_bytes=lambda: body)
    monkeypatch.setattr("unilabos.server.api.edge_proxy.edge_http", lambda *a, **kw: rejected)
    monkeypatch.setattr(coordinator, "_wait_edge_reconnected", lambda **_: pytest.fail("被拒绝时不得等待 Host 重连"))
    coordinator._restarting = True
    coordinator._execute_edge_restart()
    assert coordinator._restarting is False and coordinator._edge_restart_requested is False


def test_edge_response_data_unwraps_host_envelopes() -> None:
    from unilabos.protocol.runtime.control import EdgeHttpResponse
    from unilabos.server.api.edge_proxy import edge_response_data

    def reply(status: int, body: bytes) -> EdgeHttpResponse:
        import base64

        return EdgeHttpResponse(request_uuid="r", status_code=status, body_base64=base64.b64encode(body).decode())

    assert edge_response_data(reply(200, b'{"code":0,"data":[{"endpoint_uuid":"e"}]}')) == [{"endpoint_uuid": "e"}]
    with pytest.raises(EnvelopeError) as caught:
        edge_response_data(reply(200, b'{"code":4001,"error":{"msg":"missing"}}'))
    assert caught.value.status_code == 404
    with pytest.raises(EnvelopeError) as caught:
        edge_response_data(reply(500, b"Internal Server Error"))
    assert (caught.value.code, caught.value.http_status) == (1, 500)
