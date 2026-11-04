from __future__ import annotations

import json

from fastapi.testclient import TestClient

from unilabos.config.config import HTTPConfig
from unilabos.server.api.app import SITE_INDEX_REPO_URL, SITE_INDEX_URL, app, browser_landing_url


def test_web_root_is_a_backend_frontend_catalog() -> None:
    with TestClient(app) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "Uni-Lab-OS Microbackend" in response.text
        # 内置兜底卡片始终服务端渲染，索引不可达时页面仍有可点的前端入口。
        assert "https://xuwznln.github.io/OpenLabSite/" in response.text
        assert "/api/docs" in response.text
        assert "https://deepmodeling.github.io/Uni-Lab-OS/" in response.text


def test_web_root_reads_site_index_in_the_browser() -> None:
    """「推荐前端」由浏览器直接读 awesome-lab-sites 索引补卡，Edge 进程本身不出网。"""

    with TestClient(app) as client:
        response = client.get("/")
        assert SITE_INDEX_URL.startswith("https://raw.githubusercontent.com/Xuwznln/awesome-lab-sites/")
        assert f'href="{SITE_INDEX_REPO_URL}"' in response.text
        assert 'id="frontends"' in response.text
        assert 'id="frontends-note"' in response.text
        # 索引地址以 JSON 字面量注入脚本，避免拼接出坏 JS。
        assert f"var url = {json.dumps(SITE_INDEX_URL)};" in response.text


def test_web_root_signposts_backend_when_edge_is_backend_controlled(monkeypatch) -> None:
    monkeypatch.setattr(HTTPConfig, "remote_addr", "http://127.0.0.1:8081")
    with TestClient(app) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "Uni-Lab-OS Edge 进程" in response.text
        assert "http://127.0.0.1:8081/" in response.text
        # Edge 侧不再宣传社区前端（内置卡片与站点索引都不出现），前端应连接调度权威地址。
        assert "https://xuwznln.github.io/OpenLabSite/" not in response.text
        assert SITE_INDEX_URL not in response.text
        assert 'id="frontends"' not in response.text
        # 设备侧调试入口仍保留。
        assert "/api/docs" in response.text


def test_browser_opens_backend_page_when_backend_controlled(monkeypatch) -> None:
    monkeypatch.setattr(HTTPConfig, "remote_addr", "http://127.0.0.1:8081/")
    assert browser_landing_url("0.0.0.0", 8002) == "http://127.0.0.1:8081/"

    monkeypatch.setattr(HTTPConfig, "remote_addr", "")
    assert browser_landing_url("0.0.0.0", 8002) == "http://localhost:8002/"


def test_non_api_display_routes_are_not_exposed() -> None:
    paths = {getattr(route, "path", "") for route in app.routes}
    assert "/status" not in paths
    assert "/registry-editor" not in paths
    assert "/open-folder" not in paths
    assert "/api/v1/job/add" not in paths
    assert "/api/v1/online-devices" not in paths
