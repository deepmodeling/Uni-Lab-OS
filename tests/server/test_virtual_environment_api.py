"""虚拟实验环境只在 test_mode 重建 materials.v1 物料树。"""

from __future__ import annotations

from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from unilabos.config.config import BasicConfig
from unilabos.server.api.materials import install_materials_api
from unilabos.server.services.materials import MaterialsService


def _app(service: MaterialsService) -> FastAPI:
    app = FastAPI()
    install_materials_api(app, service)
    return app


def _reset_body(request_uuid: str) -> dict[str, str]:
    return {
        "request_uuid": request_uuid,
        "confirmation": "reset-virtual-materials",
    }


def test_virtual_environment_catalog_is_readable_but_reset_is_protected(
    tmp_path,
) -> None:
    service = MaterialsService(tmp_path / "materials.db")
    previous = BasicConfig.test_mode
    BasicConfig.test_mode = False
    try:
        with TestClient(_app(service)) as client:
            catalog = client.get("/api/v1/materials/virtual-environments")
            assert catalog.status_code == 200
            assert catalog.json()["reset_allowed"] is False
            assert [item["preset_id"] for item in catalog.json()["presets"]] == [
                "organic",
                "biology",
                "materials",
            ]

            reset = client.post(
                "/api/v1/materials/virtual-environments/organic/reset",
                json=_reset_body(str(uuid4())),
            )
            assert reset.status_code == 403
            assert service.list_materials() == []
    finally:
        BasicConfig.test_mode = previous
        service.close()


def test_reset_replaces_material_library_and_is_idempotent(tmp_path) -> None:
    service = MaterialsService(tmp_path / "materials.db")
    previous = BasicConfig.test_mode
    BasicConfig.test_mode = True
    try:
        with TestClient(_app(service)) as client:
            organic_request = str(uuid4())
            organic = client.post(
                "/api/v1/materials/virtual-environments/organic/reset",
                json=_reset_body(organic_request),
            )
            assert organic.status_code == 200, organic.text
            organic_body = organic.json()
            assert organic_body["state"]["preset_id"] == "organic"
            assert organic_body["state"]["active_material_count"] == 6
            organic_root = organic_body["state"]["root_material_uuid"]

            materials = client.get("/api/v1/materials/instances").json()
            assert {item["material"]["barcode"] for item in materials} >= {
                "OPENLAB-ORG-THF",
                "OPENLAB-ORG-R1",
            }

            replay = client.post(
                "/api/v1/materials/virtual-environments/organic/reset",
                json=_reset_body(organic_request),
            )
            assert replay.status_code == 200
            assert replay.json()["replayed"] is True
            assert replay.json()["state"]["root_material_uuid"] == organic_root
            assert len(client.get("/api/v1/materials/instances").json()) == 6

            biology = client.post(
                "/api/v1/materials/virtual-environments/biology/reset",
                json=_reset_body(str(uuid4())),
            )
            assert biology.status_code == 200, biology.text
            assert biology.json()["deleted_root_count"] == 1
            assert biology.json()["state"]["preset_id"] == "biology"
            assert biology.json()["state"]["active_material_count"] == 7
            assert (
                client.get(f"/api/v1/materials/instances/{organic_root}").status_code
                == 404
            )

            catalog = client.get("/api/v1/materials/virtual-environments").json()
            assert catalog["reset_allowed"] is True
            assert catalog["current"]["preset_id"] == "biology"
    finally:
        BasicConfig.test_mode = previous
        service.close()
