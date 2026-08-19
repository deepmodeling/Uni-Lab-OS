"""四库 API 公共安装入口测试。"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from unilabos.server.api import install_server_apis
from unilabos.server.composition import ServerServices
from unilabos.server.database import ServerDatabasePaths


def test_install_server_apis_mounts_all_database_namespaces(tmp_path) -> None:
    services = ServerServices.open(ServerDatabasePaths.resolve(tmp_path))
    app = FastAPI()
    install_server_apis(app, services)
    try:
        paths = set(app.openapi()["paths"])
        assert any(path.startswith("/api/v1/runtime/") for path in paths)
        assert any(path.startswith("/api/v1/materials/") for path in paths)
        assert any(path.startswith("/api/v1/telemetry/") for path in paths)
        assert any(path.startswith("/api/v1/history/") for path in paths)

        with TestClient(app) as client:
            assert client.get("/api/v1/runtime/jobs/missing").status_code == 404
            assert client.get("/api/v1/materials/instances/missing").status_code == 404
            assert client.get("/api/v1/telemetry/events/missing").status_code == 404
            assert client.get("/api/v1/history/events/missing").status_code == 404
    finally:
        services.close()
