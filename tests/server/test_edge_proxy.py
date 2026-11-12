"""调度权威把 Host 专有路由经控制面 WS 交给 Host 在进程内执行；Host 不在线时 503；权威自己的域不转发。"""

from __future__ import annotations

import base64
import json
import logging
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from tests.api_envelope import error_of, ok_data
from unilabos.protocol.runtime.control import EdgeHttpResponse
from unilabos.server.api import edge_proxy
from unilabos.server.api.runtime.diagnostics import create_backend_app
import unilabos.server.backend.edge_control as edge_control


class _FakeEdgeControl:
    """假的控制面服务：把 backend_http 请求直接在一个假 Host 应用上执行。"""

    def __init__(self, host_app: FastAPI, *, connected: bool = True) -> None:
        self.connected = connected
        self._client = TestClient(host_app)
        self.requests: list[tuple[str, str]] = []

    def http_request(self, method, path, *, headers=None, body=b"", timeout=60.0):
        self.requests.append((method, path))
        upstream = self._client.request(method, path, headers=headers or {}, content=body or None)
        return EdgeHttpResponse(
            request_uuid="r",
            status_code=upstream.status_code,
            headers=dict(upstream.headers),
            body_base64=base64.b64encode(upstream.content).decode("ascii"),
        )


@pytest.fixture()
def host_app() -> FastAPI:
    app = FastAPI()

    @app.api_route("/api/v1/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
    async def echo(request: Request, path: str):
        return {
            "method": request.method,
            "path": request.url.path,
            "query": dict(request.query_params),
            "body": (await request.body()).decode() or None,
            "header": request.headers.get("x-probe"),
        }

    return app


@pytest.fixture()
def authority_app():
    app = FastAPI()
    app.include_router(edge_proxy.create_edge_proxy_router())

    @app.get("/api/v1/health")
    def health():
        return {"local": True}

    edge_proxy.configure_edge_proxy(True)
    yield app
    edge_proxy.configure_edge_proxy(False)
    edge_control.set_edge_control_service(None)


def test_proxy_forwards_host_routes_over_control_plane(host_app: FastAPI, authority_app: FastAPI) -> None:
    fake = _FakeEdgeControl(host_app)
    edge_control.set_edge_control_service(fake)
    client = TestClient(authority_app)

    response = client.get("/api/v1/driver-packages", params={"a": "1"}, headers={"x-probe": "yes"})
    assert response.status_code == 200
    assert response.json() == {
        "method": "GET",
        "path": "/api/v1/driver-packages",
        "query": {"a": "1"},
        "body": None,
        "header": "yes",
    }
    assert fake.requests[-1] == ("GET", "/api/v1/driver-packages?a=1")

    # 日志复用 hostlink 路由代理，不能在调度权威进程误读成自己的日志。
    cursor = "a" * 24 + ":42"
    response = client.get("/api/v1/hostlink/logs", params={"source_id": "slave:机器 A", "cursor": cursor})
    assert response.json()["query"] == {"source_id": "slave:机器 A", "cursor": cursor}
    assert client.get("/api/v1/hostlink/log-sources").json()["path"] == "/api/v1/hostlink/log-sources"

    response = client.post("/api/v1/device-processes/abc/start", json={"x": 1})
    assert response.status_code == 200
    assert response.json()["path"] == "/api/v1/device-processes/abc/start"
    assert json.loads(response.json()["body"]) == {"x": 1}

    for prefix in edge_proxy.EDGE_ROUTE_PREFIXES:
        assert client.get(f"/api/v1/{prefix}").json()["path"] == f"/api/v1/{prefix}"

    # 权威自己的域不经过 Host
    assert client.get("/api/v1/health").json() == {"local": True}
    assert client.get("/api/v1/workflows").status_code == 404


def test_proxy_returns_503_while_host_is_offline(host_app: FastAPI, authority_app: FastAPI) -> None:
    edge_control.set_edge_control_service(_FakeEdgeControl(host_app, connected=False))
    response = TestClient(authority_app).get("/api/v1/telemetry/states")
    # Host 不在线：HTTP 200 + 业务码 5001（503 → 5001），仍带 Retry-After
    assert "Host 执行进程" in error_of(response, 5001)["msg"]
    assert response.headers["retry-after"] == "3"

    edge_control.set_edge_control_service(None)
    error_of(TestClient(authority_app).get("/api/v1/runtime/endpoints"), 5001)
    assert edge_proxy.edge_http("GET", "/api/v1/health") is None


def test_proxy_logs_host_offline_once_per_episode(
    host_app: FastAPI, authority_app: FastAPI, caplog: pytest.LogCaptureFixture
) -> None:
    """Host 启动期间前端每秒轮询多条路由：只在开始拒绝 / 恢复各记一条，不逐请求刷屏。"""

    fake = _FakeEdgeControl(host_app, connected=False)
    edge_control.set_edge_control_service(fake)
    client = TestClient(authority_app)
    with caplog.at_level(logging.INFO, logger=edge_proxy.__name__):
        for path in ("/api/v1/telemetry/states", "/api/v1/hostlink/peers", "/api/v1/runtime/endpoints"):
            error_of(client.get(path), 5001)
        offline = [r for r in caplog.records if "尚未接入" in r.getMessage()]
        assert len(offline) == 1
        assert "GET /api/v1/telemetry/states" in offline[0].getMessage()

        fake.connected = True
        assert client.get("/api/v1/telemetry/states").status_code == 200
        assert client.get("/api/v1/hostlink/peers").status_code == 200
        online = [r for r in caplog.records if "恢复转发" in r.getMessage()]
        assert len(online) == 1

        # 再次掉线：新一轮只再记一条
        fake.connected = False
        error_of(client.get("/api/v1/telemetry/states"), 5001)
        error_of(client.get("/api/v1/hostlink/peers"), 5001)
        assert len([r for r in caplog.records if "尚未接入" in r.getMessage()]) == 2


def test_proxy_disabled_when_not_configured(host_app: FastAPI, authority_app: FastAPI) -> None:
    edge_control.set_edge_control_service(_FakeEdgeControl(host_app))
    edge_proxy.configure_edge_proxy(False)
    assert edge_proxy.edge_proxy_enabled() is False
    error_of(TestClient(authority_app).get("/api/v1/runtime/endpoints"), 5001)


def test_health_reports_host_child_state(monkeypatch: pytest.MonkeyPatch) -> None:
    client = TestClient(create_backend_app(get_scheduler=lambda: object()))
    edge_proxy.configure_edge_proxy(False)
    assert client.get("/api/v1/health").json()["execution"] == "disabled"

    edge_proxy.configure_edge_proxy(True)
    try:
        monkeypatch.setattr(edge_control, "get_edge_control_service", lambda: None)
        assert client.get("/api/v1/health").json()["execution"] == "restarting"
        monkeypatch.setattr(
            edge_control, "get_edge_control_service", lambda: SimpleNamespace(connected=True)
        )
        assert client.get("/api/v1/health").json()["execution"] == "ready"
    finally:
        edge_proxy.configure_edge_proxy(False)


def test_install_server_apis_can_skip_host_data_routes(tmp_path) -> None:
    from unilabos.server.api import install_server_apis
    from unilabos.server.composition import ServerServices
    from unilabos.server.database import ServerDatabasePaths

    services = ServerServices.open(ServerDatabasePaths.resolve(tmp_path))
    try:
        app = FastAPI()
        install_server_apis(app, services, include_host_data=False)
        # FastAPI 新版 app.routes 里含 _IncludedRouter，没有 path；用 OpenAPI 的路径集合
        paths = set(app.openapi()["paths"])
        assert not any(path.startswith("/api/v1/runtime") for path in paths)
        assert not any(path.startswith("/api/v1/telemetry") for path in paths)
        assert not any(path.startswith("/api/v1/edge/history") for path in paths)
        # Graph Authority 与布局留在权威：老数据都在权威的库里
        assert any(path.startswith("/api/v1/graphs") for path in paths)
        assert any(path.startswith("/api/v1/lab") for path in paths)
        assert any(path.startswith("/api/v1/materials") for path in paths)
        assert any(path.startswith("/api/v1/debug") for path in paths)
    finally:
        services.close()
