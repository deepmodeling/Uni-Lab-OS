"""管理 API 与控制面不再有控制面 token：写请求、控制 WebSocket 与 Host 专属路由都不鉴权，旧客户端带的
``X-UniLab-Token`` 被忽略；CORS 可配置；导航页只把后端地址交给内置前端；旧配置项静默忽略。

唯一保留的鉴权是 V1 工站密钥（见 ``tests/server/test_station_invocations_v1.py``）。
"""

from __future__ import annotations

import importlib
import logging
from types import ModuleType

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from unilabos.config.config import BasicConfig, HTTPConfig
from unilabos.protocol.runtime.control import EdgeHttpResponse
from unilabos.server.api.app import (
    RECOMMENDED_FRONTENDS,
    ConfiguredCORSMiddleware,
    StationKeyMiddleware,
    _render_catalog_page,
    browser_landing_url,
)
from unilabos.server.api.runtime.control import install_edge_control_api
from unilabos.server.backend.edge_control import EdgeControlService, set_edge_control_service


@pytest.fixture(autouse=True)
def _isolated_config(monkeypatch, tmp_path):
    monkeypatch.setattr(BasicConfig, "working_dir", str(tmp_path))
    monkeypatch.setattr(BasicConfig, "cors_allow_origins", "*")
    yield


def test_control_token_module_and_config_field_are_gone() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("unilabos.config.control_token")
    assert not hasattr(BasicConfig, "control_token")


def test_old_control_token_config_and_env_are_ignored_quietly(monkeypatch, caplog) -> None:
    from unilabos.config import config as config_module

    module = ModuleType("lab_old_config")
    module.BasicConfig = type("BasicConfig", (), {"control_token": "left-over", "cors_allow_origins": "*"})
    config_module._update_config_from_module(module)
    assert not hasattr(BasicConfig, "control_token")

    monkeypatch.setenv("UNILABOS_BASICCONFIG_CONTROL_TOKEN", "left-over")
    with caplog.at_level(logging.INFO):
        config_module._update_config_from_env()
    assert not hasattr(BasicConfig, "control_token")
    assert "CONTROL_TOKEN" not in caplog.text and "left-over" not in caplog.text


@pytest.fixture
def control_app():
    service = EdgeControlService()
    set_edge_control_service(service)
    app = FastAPI()
    install_edge_control_api(app)
    try:
        yield app, service
    finally:
        set_edge_control_service(None)


def test_control_websocket_and_host_routes_need_no_token(control_app) -> None:
    app, service = control_app
    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/ws/schedule", headers={"EdgeSession": "host-1"}) as websocket:
            assert websocket.receive_json()["action"] == "backend_session"
            assert service.connected
        # 旧 Host 仍带 X-UniLab-Token：照样接入
        with client.websocket_connect(
            "/api/v1/ws/schedule", headers={"EdgeSession": "host-2", "X-UniLab-Token": "stale"}
        ) as websocket:
            assert websocket.receive_json()["action"] == "backend_session"

        missing = client.get("/edge/commands/00000000-0000-0000-0000-000000000000")
        assert missing.status_code != 401
        body = EdgeHttpResponse(request_uuid="r-1", status_code=200).model_dump(mode="json")
        assert client.post("/api/v1/edge/http-responses/r-1", json=body).status_code == 200


def _write_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(StationKeyMiddleware)
    app.add_middleware(ConfiguredCORSMiddleware)

    @app.post("/api/v1/things")
    def create_thing() -> dict:
        return {"created": True}

    @app.delete("/api/v1/things/{thing_id}")
    def delete_thing(thing_id: str) -> dict:
        return {"deleted": thing_id}

    return app


def test_writes_need_no_token_and_a_stale_token_header_is_ignored() -> None:
    with TestClient(_write_app()) as client:
        assert client.post("/api/v1/things", json={}, headers={"Origin": "https://fe.example"}).json() == {"created": True}
        assert client.delete("/api/v1/things/1", headers={"X-UniLab-Token": "whatever"}).json() == {"deleted": "1"}
        # 旧前端带 X-UniLab-Token 的跨域写请求：预检仍放行这个头，否则浏览器会直接拦下
        preflight = client.options(
            "/api/v1/things",
            headers={
                "Origin": "https://fe.example",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type,x-unilab-token",
            },
        )
        assert preflight.status_code == 200
        assert "x-unilab-token" in preflight.headers["access-control-allow-headers"].lower()


def test_cors_allows_every_origin_by_default_and_can_be_restricted(monkeypatch) -> None:
    from unilabos.server.api.app import cors_allow_origins

    assert cors_allow_origins() == ["*"]
    monkeypatch.setattr(BasicConfig, "cors_allow_origins", "https://a.example, https://b.example/")
    assert cors_allow_origins() == ["https://a.example", "https://b.example"]

    def build() -> TestClient:
        app = FastAPI()
        app.add_middleware(ConfiguredCORSMiddleware)

        @app.get("/ping")
        def ping() -> dict:
            return {"ok": True}

        return TestClient(app)

    def preflight(client: TestClient, origin: str):
        return client.options(
            "/ping",
            headers={"Origin": origin, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type"},
        )

    with build() as client:
        allowed = preflight(client, "https://a.example")
        assert allowed.status_code == 200
        assert allowed.headers["access-control-allow-origin"] == "https://a.example"
        assert preflight(client, "https://evil.example").status_code == 400

    monkeypatch.setattr(BasicConfig, "cors_allow_origins", "*")
    with build() as client:
        anyone = preflight(client, "https://anything.example")
        assert anyone.status_code == 200
        assert anyone.headers["access-control-allow-origin"] == "https://anything.example"


def test_browser_opens_the_plain_landing_page(monkeypatch) -> None:
    monkeypatch.setattr(HTTPConfig, "remote_addr", "")
    assert browser_landing_url("0.0.0.0", 8002) == "http://localhost:8002/"
    assert browser_landing_url("127.0.0.1", 8002) == "http://127.0.0.1:8002/"


def test_landing_page_hands_only_the_backend_address_to_built_in_frontends() -> None:
    page = _render_catalog_page(
        "t", "i", (("frontends", "推荐前端", RECOMMENDED_FRONTENDS),), site_index_url="https://index.example/i.json"
    )
    assert 'href="https://xuwznln.github.io/OpenLabSite/" target="_blank" rel="noreferrer" data-backend-handoff="1"' in page
    assert "a.card[data-backend-handoff]" in page  # 站点索引补进来的第三方卡片没有这个属性
    assert "#/?backend=" in page
    assert "token" not in page.lower()


def test_internal_http_clients_send_no_token_header(monkeypatch) -> None:
    from unilabos.client import history as history_module

    captured: list[dict[str, str]] = []

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self) -> bytes:
            return b'{"code":0,"data":{}}'

    def fake_urlopen(request, timeout=None):
        captured.append({key.lower(): value for key, value in request.header_items()})
        return _Response()

    monkeypatch.setattr(history_module, "urlopen", fake_urlopen)
    client = history_module.HTTPHistoryClient("http://127.0.0.1:8002")
    client._request("POST", "/events", {"x": 1})
    assert "x-unilab-token" not in captured[-1]


def test_mcp_in_process_writes_reach_the_routes(tmp_path) -> None:
    """MCP 工具经 ASGI 在进程内调用管理 API：写操作照样经过中间件，不再需要注入 token。"""

    import asyncio

    from unilabos.server.api.materials import install_materials_api
    from unilabos.server.mcp.server import ProtocolMCP
    from unilabos.server.openapi_export import export_openapi
    from unilabos.server.services.materials import MaterialsService

    service = MaterialsService(tmp_path / "materials.db")
    app = FastAPI()
    app.add_middleware(StationKeyMiddleware)
    install_materials_api(app, service)
    protocol = ProtocolMCP(app, export_openapi())
    try:
        created = asyncio.run(protocol.call_tool("materials_v1_templates_create", {"body": {
            "command_uuid": "template-command", "effect_key": "template-effect", "operation": "create_template",
            "payload": {"name": "mcp-water"},
        }}))
        assert not created.isError, created
        assert created.structuredContent["http_status"] in (200, 201)
    finally:
        service.close()
