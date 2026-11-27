"""用临时静态产物验证 OS 下发控制台，不依赖前端仓库构建。"""

import re
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pytest import MonkeyPatch, mark

from unilabos.app.web import console
from unilabos.app.web.console import (
    install_console_security,
    install_console_ui,
)
from unilabos.config.config import BasicConfig


def test_console_serves_bundle_from_default_directory(
    tmp_path: Path, monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(console, "DEFAULT_CONSOLE_DIRECTORY", _console_dist(tmp_path))

    app = FastAPI()
    assert install_console_ui(app)
    client = TestClient(app)
    index = client.get("/?page=devices", follow_redirects=False)
    assert index.status_code == 200
    assert "location" not in index.headers
    assert "/console/" not in index.text
    asset_urls = re.findall(r'(?:src|href)="(/assets/[^"]+)"', index.text)
    assert asset_urls
    for url in [*asset_urls, "/og.png"]:
        assert client.get(url, follow_redirects=False).status_code == 200


def _console_dist(tmp_path: Path) -> Path:
    dist = tmp_path / "console"
    assets = dist / "assets"
    assets.mkdir(parents=True)
    (dist / "index.html").write_text(
        '<!doctype html><div id="root">Uni-Lab OS Console</div>'
        '<script src="/assets/app-abc123.js"></script>'
        '<link rel="stylesheet" href="/assets/app-abc123.css">',
        encoding="utf-8",
    )
    (dist / "og.png").write_bytes(b"console-preview")
    (assets / "app-abc123.js").write_text(
        "window.__UNILAB_CONSOLE__ = true",
        encoding="utf-8",
    )
    (assets / "app-abc123.css").write_text("body { margin: 0; }", encoding="utf-8")
    return dist


def test_console_serves_spa_without_shadowing_api(tmp_path: Path) -> None:
    app = FastAPI(
        docs_url="/api/docs", redoc_url="/api/redoc", openapi_url="/api/openapi.json"
    )

    @app.get("/api/v1/readiness")
    def readiness() -> dict[str, str]:
        return {"status": "ready"}

    assert install_console_ui(app, _console_dist(tmp_path))

    # 模拟组合根随后注册遗留页面，确保根页面优先且诊断路由仍然可达。
    @app.get("/")
    def legacy_home() -> dict[str, str]:
        return {"page": "legacy"}

    @app.get("/status")
    @app.get("/registry-editor")
    @app.get("/open-folder")
    def diagnostics() -> dict[str, str]:
        return {"status": "diagnostics"}

    client = TestClient(app, follow_redirects=False)
    for path in [
        "/", "/?page=tasks", "/?page=devices", "/?page=workflows&workflow=test"
    ]:
        index = client.get(path)
        assert index.status_code == 200
        assert "location" not in index.headers
        assert "Uni-Lab OS Console" in index.text
        assert index.headers["cache-control"] == "no-cache"
        assert str(index.url).endswith(path)

    head = client.head("/?page=tasks")
    assert head.status_code == 200
    assert head.content == b""
    assert "location" not in head.headers

    asset = client.get("/assets/app-abc123.js")
    assert asset.status_code == 200
    assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert client.get("/og.png").content == b"console-preview"
    for path in [
        "/assets/missing.js", "/assets/missing", "/api/v1/missing", "/missing",
        "/assets/%2e%2e/index.html", "/tasks/missing.js", "/workflowz",
    ]:
        assert client.get(path).status_code == 404
    assert client.post("/").status_code == 405
    assert client.post("/tasks").status_code == 405
    assert client.get("/api/v1/readiness").json() == {"status": "ready"}
    for path in ["/status", "/registry-editor", "/open-folder"]:
        assert client.get(path).json() == {"status": "diagnostics"}
    for path in ["/api/docs", "/api/redoc", "/api/openapi.json"]:
        assert client.get(path).status_code == 200


@mark.parametrize(
    "path",
    [
        "/devices",
        "/reagents",
        "/materials",
        "/workflows?workflow=wf-1",
        "/tasks?task=task-1",
        "/schedule",
        "/embed",
        "/embed/",
        "/embed/tasks",
        "/embed/tasks?task=task-1",
        "/embed/workflows?workflow=wf-1",
    ],
)
def test_console_serves_standalone_and_embed_routes(tmp_path: Path, path: str) -> None:
    """从地址栏打开或刷新工站页面时，保留原 URL 并返回前端入口。"""

    app = FastAPI()
    assert install_console_ui(app, _console_dist(tmp_path))
    client = TestClient(app, follow_redirects=False)

    response = client.get(path)
    assert response.status_code == 200
    assert "location" not in response.headers
    assert "Uni-Lab OS Console" in response.text
    assert response.headers["cache-control"] == "no-cache"

    head = client.head(path)
    assert head.status_code == 200
    assert head.content == b""


@mark.parametrize("method", ["GET", "HEAD", "POST"])
@mark.parametrize("path", [
    "/console", "/console/", "/console/?page=tasks", "/console/tasks",
    "/console/assets/app-abc123.js",
])
def test_old_console_routes_are_removed(tmp_path: Path, method: str, path: str) -> None:
    app = FastAPI()
    assert install_console_ui(app, _console_dist(tmp_path))
    response = TestClient(app).request(method, path, follow_redirects=False)
    assert response.status_code == 404
    assert "location" not in response.headers


def test_console_install_is_idempotent_and_missing_bundle_is_nonfatal(
    tmp_path: Path, monkeypatch: MonkeyPatch,
) -> None:
    app = FastAPI()
    dist = _console_dist(tmp_path)

    assert install_console_ui(app, dist)
    assert not install_console_ui(app, dist)
    assert sum(route.name == "unilab-console" for route in app.routes) == 1

    api_only = FastAPI()

    @api_only.get("/api/v1/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    monkeypatch.setattr(console, "DEFAULT_CONSOLE_DIRECTORY", tmp_path / "missing")
    assert not install_console_ui(api_only)
    assert TestClient(api_only).get("/api/v1/health").json() == {
        "status": "ok"
    }


def test_console_install_ignores_composition_routes_without_names(
    tmp_path: Path,
) -> None:
    app = FastAPI()
    composition_route = object()
    app.router.routes.append(composition_route)  # type: ignore[arg-type]

    assert install_console_ui(app, _console_dist(tmp_path))
    assert any(
        getattr(route, "name", None) == "unilab-console"
        for route in app.routes
    )


def test_console_security_rejects_cross_site_and_dns_rebinding(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        BasicConfig,
        "frontend_allowed_hosts",
        "localhost,127.0.0.1,::1,edge-http",
    )
    monkeypatch.setattr(BasicConfig, "frontend_same_origin", True)
    app = FastAPI()

    @app.api_route("/api/v1/workflow-tasks", methods=["GET", "POST"])
    def workflow_tasks() -> dict[str, str]:
        return {"status": "accepted"}

    assert install_console_security(app)
    assert not install_console_security(app)
    client = TestClient(app, base_url="http://127.0.0.1:4173")

    same_origin = {
        "origin": "http://127.0.0.1:4173",
        "sec-fetch-site": "same-origin",
    }
    assert client.post(
        "/api/v1/workflow-tasks",
        headers=same_origin,
    ).status_code == 200
    assert client.post("/api/v1/workflow-tasks").status_code == 200

    assert client.get(
        "/api/v1/workflow-tasks",
        headers={
            "origin": "https://evil.example",
            "sec-fetch-site": "cross-site",
        },
    ).status_code == 403
    assert client.get(
        "/api/v1/workflow-tasks",
        headers={
            "host": "evil.example",
            "origin": "http://evil.example",
            "sec-fetch-site": "same-origin",
        },
    ).status_code == 403
    assert client.get(
        "/api/v1/workflow-tasks",
        headers={"origin": "http://127.0.0.1:not-a-port"},
    ).status_code == 403


def test_desktop_backend_serves_selected_bundle_and_checks_origins(tmp_path, monkeypatch):
    import pytest
    from fastapi import WebSocket
    from starlette.websockets import WebSocketDisconnect

    monkeypatch.setenv("UNILABOS_DESKTOP_WEB_HOST", "0.0.0.0")
    monkeypatch.setenv("UNILABOS_DESKTOP_CONSOLE_DIR", str(_console_dist(tmp_path)))
    monkeypatch.setenv("UNILABOS_LAUNCHER_CENTER_HTTP_BASE_URL", "http://station.test:80")
    app = FastAPI()

    @app.websocket("/socket")
    async def socket_endpoint(socket: WebSocket):
        await socket.accept()
        await socket.send_text("ready")
        await socket.close()

    assert install_console_security(app)
    assert install_console_ui(app)
    client = TestClient(app, base_url="http://127.0.0.1:8012")
    assert client.get("/workflows").status_code == 200
    advertised = {"Host": "station.test", "Origin": "http://station.test:80"}
    assert client.get("/tasks", headers=advertised).status_code == 200
    for headers in (
        {"Host": "unknown.test"}, {"Host": "station.test:8012"},
        {"Origin": "http://127.0.0.1:8013"}, {"Sec-Fetch-Site": "cross-site"},
    ):
        assert client.get("/tasks", headers=headers).status_code == 403
    assert client.post("/tasks", headers={"Content-Length": str(65 * 1024 * 1024)}).status_code == 413
    with client.websocket_connect("ws://127.0.0.1:8012/socket") as ws:
        assert ws.receive_text() == "ready"
    with pytest.raises(WebSocketDisconnect), client.websocket_connect(
        "ws://127.0.0.1:8012/socket", headers={"Origin": "http://other.test"},
    ):
        pass
