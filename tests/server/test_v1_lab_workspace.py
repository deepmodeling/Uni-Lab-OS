"""V1 实验室视图（/api/v1/lab/warehouse、/assembly、/demo）与工作区只读路由（/workspace/package-mounts、/file-browser）契约测试。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.api_envelope import error_of, ok_data
from unilabos.server.api.materials import install_materials_api
from unilabos.server.api.materials.lab import install_lab_api
from unilabos.server.api.workspace import create_workspace_router
from unilabos.server.services.materials import MaterialsService


@pytest.fixture()
def lab(tmp_path):
    service = MaterialsService(tmp_path / "materials.db")
    app = FastAPI()
    install_materials_api(app, service)
    install_lab_api(app, service)
    with TestClient(app) as client:
        yield service, client
    service.close()


def test_profile_layout_zones_and_placements_are_gone(lab) -> None:
    """档案 / 布局 / 分区 / 摆放及其表按负责人 11-17 决定删除；不依赖这些表的视图保留。"""

    _service, client = lab
    assert client.get("/api/v1/lab/profile").status_code == 404
    assert client.get("/api/v1/lab/layout").status_code == 404
    assert client.post("/api/v1/lab/zones", json={"zone_id": "z"}).status_code == 404
    assert client.post("/api/v1/lab/placements", json={"subject_id": "s"}).status_code == 404


def test_warehouse_assembly_and_demo_seed(lab) -> None:
    _service, client = lab
    identities = {item["name"]: item["uuid"] for item in ok_data(client.post("/api/v1/resource-templates", json={
        "resources": [{"id": "deck", "registry_type": "resource", "available_sites": [{"id": "A1", "label": "A1"}]},
                      {"id": "tube", "registry_type": "resource"}]}))["templates"]}
    deck = ok_data(client.post("/api/v1/materials", json={"resource_template_uuid": identities["deck"], "name": "台面-1"}), status=201)
    ok_data(client.post("/api/v1/materials", json={
        "resource_template_uuid": identities["tube"], "name": "管-1",
        "site_placement": {"action": "place", "site_uuid": deck["site_uuids"][0]}}), status=201)
    assembly = ok_data(client.get(f"/api/v1/lab/assembly/{deck['uuid']}"))
    assert assembly["root"]["edge_uuid"] == deck["uuid"] and assembly["root"]["children"][0]["slot_id"] == "A1"
    assert assembly["placement"] is None  # 摆放表已删除，V1 的键保留为 null
    error_of(client.get("/api/v1/lab/assembly/missing"), 6000)

    seeded = ok_data(client.post("/api/v1/lab/demo"))["seeded"]
    assert seeded["templates"] == 3 and seeded["lots"] == 3 and seeded["zones"] == seeded["placements"] == 0
    assert ok_data(client.post("/api/v1/lab/demo"))["seeded"]["lots"] == 0  # 幂等
    warehouse = ok_data(client.get("/api/v1/lab/warehouse"))
    naoh = next(item for item in warehouse["categories"] if item["name"] == "NaOH 1M 溶液")
    assert naoh["quantity_available"] == 500 and naoh["storage_class"] == "ambient"
    assert naoh["zones"] == [{"zone_id": "zone-storage", "quantity_available": 500.0, "batch_count": 1}]
    assert {item["id"] for item in warehouse["storage_classes"]} >= {"ambient", "cold"}


def test_package_mounts_and_file_browser(tmp_path) -> None:
    package_dir = tmp_path / "pkgs" / "acme_driver"
    package_dir.mkdir(parents=True)
    (tmp_path / "work" / "sub").mkdir(parents=True)
    (tmp_path / "work" / "run.py").write_text("print(1)\n", encoding="utf-8")
    packages = SimpleNamespace(inventory=lambda: {"packages": [
        {"name": "acme", "version": "1.2.0", "enabled": True, "dirs_exist": True, "source_kind": "local",
         "package_dirs": [str(package_dir)], "device_ids": ["acme.pump"], "sha256": "abc"},
        {"name": "off", "version": "0.1", "enabled": False, "dirs_exist": True, "package_dirs": [str(package_dir)]},
    ]})
    app = FastAPI()
    app.include_router(create_workspace_router(working_dir=tmp_path / "work", driver_packages=packages))
    client = TestClient(app)
    mounts = ok_data(client.get("/api/v1/workspace/package-mounts"))
    assert mounts["schemaVersion"] == "workspace-package-mounts/v1" and len(mounts["items"]) == 1
    item = mounts["items"][0]
    assert item["distributionName"] == "acme" and item["packageRootUri"] == package_dir.resolve().as_uri()
    assert item["readOnly"] is True and mounts["mountRevision"]
    listing = ok_data(client.get("/api/v1/file-browser"))
    assert [entry["name"] for entry in listing["items"]] == ["sub", "run.py"]
    assert listing["items"][1]["is_python"] is True
    sub = ok_data(client.get("/api/v1/file-browser", params={"path": "sub"}))
    assert sub["items"][0]["is_parent"] is True
    error_of(client.get("/api/v1/file-browser", params={"path": "../"}), 1000)
    error_of(client.get("/api/v1/file-browser", params={"path": "missing"}), 4001)
    error_of(client.get("/api/v1/file-browser", params={"path": "run.py"}), 1000)
