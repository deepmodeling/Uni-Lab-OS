"""控制面 token：权威生成与沿用、Host 侧携带、控制 WebSocket 与 Host 专属路由的鉴权、CORS 配置。"""

from __future__ import annotations

import os
import stat
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from unilabos.config.config import BasicConfig
from unilabos.config.control_token import (
    TOKEN_HEADER,
    client_headers,
    client_token,
    ensure_authority_token,
    presented_token,
    token_accepted,
)
from unilabos.protocol.runtime.control import EdgeHttpResponse
from unilabos.server.api.runtime.control import install_edge_control_api
from unilabos.server.backend.edge_control import EdgeControlService, set_edge_control_service


@pytest.fixture(autouse=True)
def _isolated_config(monkeypatch, tmp_path):
    monkeypatch.setattr(BasicConfig, "control_token", "")
    monkeypatch.setattr(BasicConfig, "working_dir", str(tmp_path))
    monkeypatch.setattr(BasicConfig, "cors_allow_origins", "*")
    yield


def test_authority_generates_a_private_token_once_and_reuses_it(tmp_path, monkeypatch) -> None:
    token, path = ensure_authority_token(tmp_path)
    assert path == tmp_path / "control_token"
    assert len(token) >= 32 and path.read_text(encoding="utf-8").strip() == token
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert BasicConfig.control_token == token

    monkeypatch.setattr(BasicConfig, "control_token", "")
    again, _ = ensure_authority_token(tmp_path)
    assert again == token  # 重启沿用同一个，远端 Host 的配置不会失效


def test_configured_token_wins_and_is_not_written(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(BasicConfig, "control_token", "  from-config  ")
    token, path = ensure_authority_token(tmp_path)
    assert (token, path) == ("from-config", None)
    assert not (tmp_path / "control_token").exists()


def test_client_reads_config_then_the_working_dir_file(tmp_path, monkeypatch) -> None:
    assert client_token() == "" and client_headers() == {}
    (tmp_path / "control_token").write_text("from-file\n", encoding="utf-8")
    assert client_headers() == {TOKEN_HEADER: "from-file"}
    monkeypatch.setattr(BasicConfig, "control_token", "from-config")
    assert client_token() == "from-config"


def test_presented_token_header_forms(monkeypatch) -> None:
    assert presented_token({TOKEN_HEADER: " t1 "}) == "t1"
    assert presented_token({"x-unilab-token": "t2"}) == "t2"
    assert presented_token({"authorization": "Bearer t3"}) == "t3"
    assert presented_token({"authorization": "Lab abc"}) == ""  # 旧云端 ak/sk 不是控制面 token
    monkeypatch.setattr(BasicConfig, "control_token", "secret")
    assert token_accepted({TOKEN_HEADER: "secret"})
    assert not token_accepted({TOKEN_HEADER: "secrex"})
    assert not token_accepted({})


@pytest.fixture
def control_app(monkeypatch):
    monkeypatch.setattr(BasicConfig, "control_token", "secret")
    service = EdgeControlService()
    set_edge_control_service(service)
    app = FastAPI()
    install_edge_control_api(app)
    try:
        yield app, service
    finally:
        set_edge_control_service(None)


def test_edge_routes_require_the_token(control_app) -> None:
    app, _service = control_app
    with TestClient(app) as client:
        missing = client.get("/edge/commands/00000000-0000-0000-0000-000000000000")
        assert missing.status_code == 401
        wrong = client.get(
            "/edge/commands/00000000-0000-0000-0000-000000000000", headers={TOKEN_HEADER: "nope"}
        )
        assert wrong.status_code == 401
        ok = client.get(
            "/edge/commands/00000000-0000-0000-0000-000000000000", headers={TOKEN_HEADER: "secret"}
        )
        assert ok.status_code == 404  # 鉴权通过，命令不存在

        body = EdgeHttpResponse(request_uuid="r-1", status_code=200).model_dump(mode="json")
        assert client.post("/api/v1/edge/http-responses/r-1", json=body).status_code == 401
        accepted = client.post(
            "/api/v1/edge/http-responses/r-1", json=body, headers={"Authorization": "Bearer secret"}
        )
        assert accepted.status_code == 200


def test_control_websocket_rejects_connections_without_the_token(control_app) -> None:
    app, service = control_app
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/api/v1/ws/schedule", headers={"EdgeSession": "intruder"}):
                pass
        assert not service.connected  # 没挤掉任何连接

        with client.websocket_connect(
            "/api/v1/ws/schedule", headers={"EdgeSession": "host-1", TOKEN_HEADER: "secret"}
        ) as websocket:
            session = websocket.receive_json()
            assert session["action"] == "backend_session"
            assert service.connected


def test_cors_allows_every_origin_by_default_and_can_be_restricted(monkeypatch) -> None:
    from unilabos.server.api.app import ConfiguredCORSMiddleware, cors_allow_origins

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
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": TOKEN_HEADER,
            },
        )

    with build() as client:
        allowed = preflight(client, "https://a.example")
        assert allowed.status_code == 200
        assert allowed.headers["access-control-allow-origin"] == "https://a.example"
        assert TOKEN_HEADER.lower() in allowed.headers["access-control-allow-headers"].lower()
        assert preflight(client, "https://evil.example").status_code == 400

    monkeypatch.setattr(BasicConfig, "cors_allow_origins", "*")
    with build() as client:
        anyone = preflight(client, "https://anything.example")
        assert anyone.status_code == 200
        assert anyone.headers["access-control-allow-origin"] == "https://anything.example"


def test_env_override_does_not_log_the_token(monkeypatch, caplog) -> None:
    import logging

    from unilabos.config import config as config_module

    monkeypatch.setenv("UNILABOS_BASICCONFIG_CONTROL_TOKEN", "very-secret-value")
    with caplog.at_level(logging.INFO):
        config_module._update_config_from_env()
    assert BasicConfig.control_token == "very-secret-value"
    assert "very-secret-value" not in caplog.text
    assert "BasicConfig.control_token = <已设置>" in caplog.text  # 确实记录了，只是不带值
