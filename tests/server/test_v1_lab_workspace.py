"""V1 实验室布局（/api/v1/lab/*）与工作区只读路由（/workspace/package-mounts、/file-browser）契约测试。"""

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
    from unilabos.server.services.runtime import RuntimeService
    from unilabos.server.services.runtime.lab import LabLayoutService

    service = MaterialsService(tmp_path / "materials.db")
    runtime = RuntimeService(tmp_path / "runtime.db")
    app = FastAPI()
    install_materials_api(app, service)
    install_lab_api(app, service, LabLayoutService(runtime))
    with TestClient(app) as client:
        yield service, client
    service.close()
    runtime.close()


def test_profile_carries_domain_pack_and_dev_grid(lab) -> None:
    _service, client = lab
    profile = ok_data(client.get("/api/v1/lab/profile"))
    assert profile["name"] == "Uni-Lab 实验室" and profile["domain"] == "general"
    assert profile["pack"]["domain"] == "general" and {item["kind"] for item in profile["zone_kinds"]} >= {"bench", "storage"}
    assert profile["grid"]["revision"] == 0
    updated = ok_data(client.put("/api/v1/lab/profile", json={
        "name": "有机合成室", "domain": "organic",
        "grid": {"revision": 0, "cell_size": 50, "zones": [{"id": "z1", "name": "台面", "cells": ["0,0", "1,0"]}], "walls": ["2,0"]},
    }))
    assert updated["name"] == "有机合成室" and updated["pack"]["name"] == "有机化学实验室"
    assert updated["grid"]["revision"] == 1 and updated["grid"]["walls"] == ["2,0"] and updated["grid"]["update_time"].endswith("Z")
    # 网格仍是 revision 乐观锁：过期写入为 3003
    error_of(client.put("/api/v1/lab/profile", json={"grid": {"revision": 0, "cell_size": 50}}), 3003)
    assert ok_data(client.put("/api/v1/lab/profile", json={"grid": None}))["grid"]["revision"] == 0
    # dev 的整份网格写入路径已并入档案
    assert client.put("/api/v1/lab/layout", json={}).status_code == 405


def test_zones_placements_layout_warehouse_and_assembly(lab) -> None:
    _service, client = lab
    identities = {item["name"]: item["uuid"] for item in ok_data(client.post("/api/v1/resource-templates", json={
        "resources": [{"id": "deck", "registry_type": "resource", "available_sites": [{"id": "A1", "label": "A1"}]},
                      {"id": "tube", "registry_type": "resource"}]}))["templates"]}
    deck = ok_data(client.post("/api/v1/materials", json={"resource_template_uuid": identities["deck"], "name": "台面-1"}), status=201)
    ok_data(client.post("/api/v1/materials", json={
        "resource_template_uuid": identities["tube"], "name": "管-1",
        "site_placement": {"action": "place", "site_uuid": deck["site_uuids"][0]}}), status=201)
    zone = ok_data(client.post("/api/v1/lab/zones", json={"zone_id": "zone-a", "name": "实验台 A", "kind": "bench", "w": 300}))
    assert zone["version"] == 1 and zone["w"] == 300
    assert ok_data(client.post("/api/v1/lab/zones", json={"zone_id": "zone-a", "name": "实验台 A'"}))["version"] == 2
    error_of(client.post("/api/v1/lab/zones", json={"name": "无 id"}), 1000)
    placement = ok_data(client.post("/api/v1/lab/placements", json={
        "subject_id": deck["uuid"], "subject_kind": "container", "zone_id": "zone-a", "x": 10, "label": "台面"}))
    assert placement["zone_id"] == "zone-a"
    layout = ok_data(client.get("/api/v1/lab/layout"))
    assert [item["zone_id"] for item in layout["zones"]] == ["zone-a"]
    assert layout["placements"][0]["instance"]["edge_uuid"] == deck["uuid"]
    assert layout["placements"][0]["children_count"] == 1 and "storage_summary" in layout
    assembly = ok_data(client.get(f"/api/v1/lab/assembly/{deck['uuid']}"))
    assert assembly["root"]["children"][0]["slot_id"] == "A1" and assembly["placement"]["subject_id"] == deck["uuid"]
    error_of(client.get("/api/v1/lab/assembly/missing"), 6000)
    assert ok_data(client.delete("/api/v1/lab/zones/zone-a")) == {"zone_id": "zone-a", "deleted": True}
    assert ok_data(client.get("/api/v1/lab/layout"))["placements"][0]["zone_id"] == ""
    assert ok_data(client.delete(f"/api/v1/lab/placements/{deck['uuid']}"))["deleted"] is True

    seeded = ok_data(client.post("/api/v1/lab/demo"))["seeded"]
    assert seeded["zones"] == 6 and seeded["lots"] == 3
    assert ok_data(client.post("/api/v1/lab/demo"))["seeded"]["lots"] == 0  # 幂等
    summary = ok_data(client.get("/api/v1/lab/layout"))["storage_summary"]
    assert {item["name"] for item in summary["zone-storage"]} == {"NaOH 1M 溶液", "乙醇 99.5%"}
    warehouse = ok_data(client.get("/api/v1/lab/warehouse"))
    naoh = next(item for item in warehouse["categories"] if item["name"] == "NaOH 1M 溶液")
    assert naoh["quantity_available"] == 500 and naoh["storage_class"] == "ambient"
    assert naoh["zones"] == [{"zone_id": "zone-storage", "quantity_available": 500.0, "batch_count": 1}]
    assert {item["id"] for item in warehouse["storage_classes"]} >= {"ambient", "cold"}


def test_package_mounts_and_file_browser(tmp_path) -> None:
    package_dir = tmp_path / "pkgs" / "acme_driver"
    package_dir.mkdir(parents=True)
    (tmp_path / "work" / "sub").mkdir(parents=True)
    (tmp_path / "work" / "run.py").write_text("print(1)\n")
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
