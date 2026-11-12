"""四库 API 公共安装入口测试。"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.api_envelope import error_of, ok_data
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
            error_of(client.get("/api/v1/runtime/jobs/missing"), 4001)
            error_of(client.get("/api/v1/materials/missing"), 6000)
            error_of(client.get("/api/v1/inventory/instances/missing"), 6000)
            error_of(client.get("/api/v1/telemetry/events/missing"), 4001)
            error_of(client.get("/api/v1/history/events/missing"), 4001)
    finally:
        services.close()


def test_install_server_apis_can_exclude_local_materials_authority(tmp_path) -> None:
    services = ServerServices.open(ServerDatabasePaths.resolve(tmp_path))
    app = FastAPI()
    install_server_apis(app, services, include_materials=False)
    try:
        paths = set(app.openapi()["paths"])
        assert any(path.startswith("/api/v1/runtime/") for path in paths)
        assert not any(path.startswith("/api/v1/materials/") for path in paths)
        assert any(path.startswith("/api/v1/telemetry/") for path in paths)
        assert any(path.startswith("/api/v1/history/") for path in paths)
    finally:
        services.close()
