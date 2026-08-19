from __future__ import annotations

from fastapi.testclient import TestClient

from unilabos.app.web.server import app


def test_web_root_is_a_backend_frontend_catalog() -> None:
    with TestClient(app) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "UniLab Microbackend" in response.text
        assert "/api/docs" in response.text
        assert "https://deepmodeling.github.io/Uni-Lab-OS/" in response.text


def test_legacy_display_routes_are_not_exposed() -> None:
    paths = {getattr(route, "path", "") for route in app.routes}
    assert "/status" not in paths
    assert "/registry-editor" not in paths
    assert "/open-folder" not in paths
    assert "/api/v1/job/add" not in paths
    assert "/api/v1/online-devices" not in paths
