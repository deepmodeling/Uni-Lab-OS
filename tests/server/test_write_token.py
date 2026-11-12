"""管理 API 写操作的控制面 token：中间件、401 正文与跨域头、豁免路径、导航页交接、内部客户端与 CLI。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from unilabos.config.config import BasicConfig
from unilabos.server.api.app import (
    ConfiguredCORSMiddleware,
    ControlTokenMiddleware,
    _render_catalog_page,
    RECOMMENDED_FRONTENDS,
    browser_landing_url,
)


@pytest.fixture(autouse=True)
def _isolated_config(monkeypatch, tmp_path):
    monkeypatch.setattr(BasicConfig, "control_token", "secret")
    monkeypatch.setattr(BasicConfig, "working_dir", str(tmp_path))
    monkeypatch.setattr(BasicConfig, "cors_allow_origins", "*")
    yield


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(ControlTokenMiddleware)
    app.add_middleware(ConfiguredCORSMiddleware)

    @app.get("/api/v1/things")
    def list_things() -> dict:
        return {"items": []}

    @app.post("/api/v1/things")
    def create_thing() -> dict:
        return {"created": True}

    @app.delete("/api/v1/things/{thing_id}")
    def delete_thing(thing_id: str) -> dict:
        return {"deleted": thing_id}

    @app.post("/mcp")
    def mcp() -> dict:
        return {"mcp": True}

    return app


def test_writes_need_the_token_reads_do_not() -> None:
    with TestClient(_app()) as client:
        assert client.get("/api/v1/things").status_code == 200

        rejected = client.post("/api/v1/things", json={}, headers={"Origin": "https://evil.example"})
        assert rejected.status_code == 401
        body = rejected.json()
        assert body["code"] == 1002 and "unilab token" in body["error"]["msg"]
        assert set(body) == {"code", "error"}
        assert rejected.headers["www-authenticate"].startswith("X-UniLab-Token")
        # 外层 CORS 补了跨域头：浏览器能读到 401 的原因
        assert rejected.headers["access-control-allow-origin"] == "https://evil.example"

        assert client.delete("/api/v1/things/1").status_code == 401
        assert client.post("/api/v1/things", json={}, headers={"X-UniLab-Token": "wrong"}).status_code == 401
        assert client.post("/api/v1/things", json={}, headers={"X-UniLab-Token": "secret"}).json() == {"created": True}
        # Authorization: Bearer 留给 V1 工站 api_key，控制面不再接受
        assert client.delete("/api/v1/things/1", headers={"Authorization": "Bearer secret"}).status_code == 401
        assert client.delete("/api/v1/things/1", headers={"X-UniLab-Token": "secret"}).status_code == 200


def test_preflight_and_mcp_are_not_blocked() -> None:
    with TestClient(_app()) as client:
        preflight = client.options(
            "/api/v1/things",
            headers={
                "Origin": "https://xuwznln.github.io",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,content-type",
            },
        )
        assert preflight.status_code == 200
        assert "authorization" in preflight.headers["access-control-allow-headers"].lower()
        # MCP 端点自带本机 Host 白名单并拒绝浏览器 Origin，不再叠加 token
        assert client.post("/mcp", json={}).json() == {"mcp": True}


def test_without_an_authority_token_nothing_is_checked(monkeypatch) -> None:
    monkeypatch.setattr(BasicConfig, "control_token", "")
    with TestClient(_app()) as client:
        assert client.post("/api/v1/things", json={}).status_code == 200


def test_browser_opens_the_landing_page_with_the_token_in_the_fragment(monkeypatch) -> None:
    from unilabos.config.config import HTTPConfig

    monkeypatch.setattr(HTTPConfig, "remote_addr", "")
    assert browser_landing_url("0.0.0.0", 8002) == "http://localhost:8002/#token=secret"
    monkeypatch.setattr(BasicConfig, "control_token", "")
    assert browser_landing_url("127.0.0.1", 8002) == "http://127.0.0.1:8002/"


def test_landing_page_hands_the_token_only_to_built_in_frontends() -> None:
    page = _render_catalog_page(
        "t", "i", (("frontends", "推荐前端", RECOMMENDED_FRONTENDS),), site_index_url="https://index.example/i.json"
    )
    assert 'href="https://xuwznln.github.io/OpenLabSite/" target="_blank" rel="noreferrer" data-token-handoff="1"' in page
    assert "a.card[data-token-handoff]" in page  # 站点索引补进来的第三方卡片没有这个属性
    assert "history.replaceState" in page
    assert "secret" not in page  # token 只在浏览器的地址片段里，从不进页面


def test_internal_http_clients_send_the_token(monkeypatch) -> None:
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
    assert captured[-1]["x-unilab-token"] == "secret"


def test_cli_token_prints_only_the_token_on_stdout(tmp_path, monkeypatch, capsys) -> None:
    from unilabos.app.cli.router import _cmd_token

    monkeypatch.setattr(BasicConfig, "control_token", "")
    session = SimpleNamespace(working_dir=tmp_path)
    with pytest.raises(SystemExit):
        _cmd_token({}, session)
    assert "还没有控制面 token" in capsys.readouterr().err

    (tmp_path / "control_token").write_text("from-file\n", encoding="utf-8")
    _cmd_token({}, session)
    output = capsys.readouterr()
    assert output.out == "from-file\n" and "X-UniLab-Token" in output.err
    _cmd_token({"json": True}, session)
    assert json.loads(capsys.readouterr().out)["token"] == "from-file"


def test_mcp_in_process_writes_pass_the_token_check(tmp_path) -> None:
    """MCP 工具经 ASGI 在进程内调用管理 API：写操作同样过中间件，必须自带 token。"""

    import asyncio

    from unilabos.server.api.materials import install_materials_api
    from unilabos.server.mcp.server import ProtocolMCP
    from unilabos.server.openapi_export import export_openapi
    from unilabos.server.services.materials import MaterialsService

    service = MaterialsService(tmp_path / "materials.db")
    app = FastAPI()
    app.add_middleware(ControlTokenMiddleware)
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
