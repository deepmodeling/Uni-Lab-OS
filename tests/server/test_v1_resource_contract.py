"""V1.0 资源合同（/resource-templates、/materials、/sites、/material-states）与内容物合同
（/reagent-infos、/reagents、/samples、/current-substances、历史）的契约测试。

请求 / 响应示例取自 V1 实现（backend_api.py / reagent_api.py / content_api.py）与 V1 控制台调用。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.api_envelope import error_of, ok_data
from unilabos.server.api.materials import install_materials_api
from unilabos.server.services.materials import MaterialsService

_BOTTLE = {
    "id": "bottle_500ml",
    "display_name": "500mL 试剂瓶",
    "registry_type": "resource",
    "class": {"module": "pylabrobot.resources:Container", "type": "pylabrobot"},
    "category": ["container"],
    "description": "试剂瓶",
    "handles": [],
    "config_info": [],
}
_RACK = {
    "id": "tube_rack_2",
    "display_name": "两孔试管架",
    "registry_type": "resource",
    "class": {"module": "pylabrobot.resources:Resource", "type": "pylabrobot"},
    "category": ["rack"],
    "available_sites": [
        {"id": "A1", "label": "A1", "position": {"x": 0, "y": 0, "z": 0}},
        {"id": "A2", "label": "A2", "position": {"x": 10, "y": 0, "z": 0}},
    ],
}


@pytest.fixture()
def client(tmp_path):
    service = MaterialsService(tmp_path / "materials.db")
    app = FastAPI()
    install_materials_api(app, service)
    with TestClient(app) as test_client:
        yield test_client
    service.close()


def _templates(client) -> dict[str, str]:
    receipt = ok_data(client.post("/api/v1/resource-templates", json={"resources": [_BOTTLE, _RACK]}))
    assert [item["name"] for item in receipt["templates"]] == ["bottle_500ml", "tube_rack_2"]
    return {item["name"]: item["uuid"] for item in receipt["templates"]}


def test_resource_template_sync_receipt_list_detail_update_delete(client) -> None:
    identities = _templates(client)
    # 重复同步复用同一身份
    again = ok_data(client.post("/api/v1/resource-templates", json={"resources": [_BOTTLE]}))
    assert again["templates"] == [{"uuid": identities["bottle_500ml"], "name": "bottle_500ml"}]
    error_of(client.post("/api/v1/resource-templates", json={"resources": []}), 5003)
    error_of(client.post("/api/v1/resource-templates", json={"resources": [{"id": "x"}, {"id": "x"}]}), 5003)

    page = ok_data(client.get("/api/v1/resource-templates", params={"page": 1, "page_size": 100}))
    assert {item["uuid"] for item in page["items"]} == set(identities.values())
    assert page["has_more"] is False and page["page"] == 1 and page["page_size"] == 100
    summary = next(item for item in page["items"] if item["name"] == "bottle_500ml")
    assert summary["display_name"] == "500mL 试剂瓶" and summary["tags"] == ["container"]
    assert ok_data(client.get("/api/v1/resource-templates", params={"keyword": "rack"}))["items"][0]["name"] == "tube_rack_2"

    detail = ok_data(client.get(f"/api/v1/resource-templates/{identities['tube_rack_2']}"))
    assert detail["uuid"] == identities["tube_rack_2"] and len(detail["available_sites"]) == 2
    assert detail["create_time"].endswith("Z") and detail["handles"] == []
    error_of(client.get("/api/v1/resource-templates/00000000-0000-0000-0000-000000000001"), 5000)

    updated = ok_data(client.put(
        f"/api/v1/resource-templates/{identities['bottle_500ml']}",
        json={"display_name": "试剂瓶 500 mL", "description": "棕色"},
    ))
    assert updated["display_name"] == "试剂瓶 500 mL" and updated["description"] == "棕色"

    material = ok_data(client.post("/api/v1/materials", json={
        "resource_template_uuid": identities["bottle_500ml"], "name": "瓶-1", "barcode": "BTL-1",
    }), status=201)
    error_of(client.delete(f"/api/v1/resource-templates/{identities['bottle_500ml']}"), 5004)
    ok_data(client.delete(f"/api/v1/materials/{material['uuid']}"))
    assert ok_data(client.delete(f"/api/v1/resource-templates/{identities['bottle_500ml']}")) is None
    error_of(client.get(f"/api/v1/resource-templates/{identities['bottle_500ml']}"), 5000)


def test_material_create_place_update_ledger_and_delete(client) -> None:
    identities = _templates(client)
    rack = ok_data(client.post("/api/v1/materials", json={
        "resource_template_uuid": identities["tube_rack_2"], "name": "架-1",
    }), status=201)
    assert len(rack["site_uuids"]) == 2 and rack["current_site"] is None
    site_uuid = rack["site_uuids"][0]
    bottle = ok_data(client.post("/api/v1/materials", json={
        "resource_template_uuid": identities["bottle_500ml"], "name": "瓶-1", "barcode": "BTL-1",
        "site_placement": {"action": "PLACE", "site_uuid": site_uuid},
        "relative_position": {"position_x": 1, "width": 50},
    }), status=201)
    assert bottle["current_site"]["uuid"] == site_uuid and bottle["parent_uuid"] == rack["uuid"]
    assert bottle["relative_position"]["width"] == 50 and bottle["resource_template_uuid"] == identities["bottle_500ml"]

    # 条码唯一、库位占用、模板不存在
    error_of(client.post("/api/v1/materials", json={
        "resource_template_uuid": identities["bottle_500ml"], "name": "瓶-2", "barcode": "BTL-1"}), 6010)
    error_of(client.post("/api/v1/materials", json={
        "resource_template_uuid": identities["bottle_500ml"], "name": "瓶-3",
        "site_placement": {"action": "place", "site_uuid": site_uuid}}), 6007)
    error_of(client.post("/api/v1/materials", json={
        "resource_template_uuid": "00000000-0000-0000-0000-00000000aaaa", "name": "x"}), 6001)
    error_of(client.post("/api/v1/materials", json={"name": "缺模板"}), 1000)

    listed = ok_data(client.get("/api/v1/materials", params={"barcode": "BTL-1", "page": 1, "page_size": 20}))
    assert listed["total"] == 1 and listed["items"][0]["uuid"] == bottle["uuid"]
    assert ok_data(client.get("/api/v1/materials", params={"name": "瓶"}))["total"] == 1

    updated = ok_data(client.put(f"/api/v1/materials/{bottle['uuid']}", json={
        "name": "瓶-改", "expected_revision": bottle["revision"], "config": {"capacity": {"max_volume_ul": 500000}},
    }))
    assert updated["name"] == "瓶-改" and updated["config"]["capacity"] == {"max_volume_ul": 500000}
    error_of(client.put(f"/api/v1/materials/{bottle['uuid']}", json={"name": "x", "expected_revision": 1}), 4002)
    error_of(client.put(f"/api/v1/materials/{bottle['uuid']}", json={
        "resource_template_uuid": identities["tube_rack_2"]}), 6002)
    removed = ok_data(client.put(f"/api/v1/materials/{bottle['uuid']}", json={"site_placement": {"action": "remove"}}))
    assert removed["current_site"] is None

    ledger = ok_data(client.get(f"/api/v1/materials/{bottle['uuid']}/ledger"))
    assert ledger["total"] >= 2 and ledger["items"][0]["occurred_at"] >= ledger["items"][-1]["occurred_at"]
    sites = ok_data(client.get(f"/api/v1/materials/{rack['uuid']}/sites"))
    assert [site["name"] for site in sites] == ["A1", "A2"]
    assert ok_data(client.get(f"/api/v1/sites/{site_uuid}"))["material_uuid"] == rack["uuid"]
    error_of(client.get("/api/v1/sites/missing-site"), 6006)
    error_of(client.get("/api/v1/materials/missing-material"), 6000)

    graph = ok_data(client.get("/api/v1/materials/graph"))
    assert {node["material"]["uuid"] for node in graph["nodes"]} == {rack["uuid"], bottle["uuid"]}

    # 有子物料时只删本体冲突（2005）；连同内容删除成功
    ok_data(client.put(f"/api/v1/materials/{bottle['uuid']}", json={"site_placement": {"action": "place", "site_uuid": site_uuid}}))
    error_of(client.delete(f"/api/v1/materials/{rack['uuid']}"), 2005)
    assert ok_data(client.delete(f"/api/v1/materials/{rack['uuid']}", params={"scope": "material_with_contents"})) is None
    error_of(client.get(f"/api/v1/materials/{bottle['uuid']}"), 6000)


def test_literal_dev_routes_are_not_captured_by_material_uuid_route(client) -> None:
    # dev 独有的字面量路由先于 V1 的 /materials/{material_uuid} 注册
    assert isinstance(ok_data(client.get("/api/v1/materials/registry-classes")), list)
    assert ok_data(client.get("/api/v1/materials/executions")) == []
    error_of(client.get("/api/v1/materials/reservations/by-job/none"), 4001)


def test_material_states_append_list_latest(client) -> None:
    identities = _templates(client)
    bottle = ok_data(client.post("/api/v1/materials", json={
        "resource_template_uuid": identities["bottle_500ml"], "name": "瓶-1"}), status=201)
    first = ok_data(client.post(f"/api/v1/materials/{bottle['uuid']}/states", json={
        "status": "ok", "state_data": {"temp": 20}, "observed_at": "2026-11-12T08:00:00.000Z"}), status=201)
    second = ok_data(client.post(f"/api/v1/materials/{bottle['uuid']}/states", json={
        "status": "hot", "state_data": {"temp": 80}, "observed_at": "2026-11-12T09:00:00.000Z"}), status=201)
    assert ok_data(client.get(f"/api/v1/materials/{bottle['uuid']}/states/latest"))["uuid"] == second["uuid"]
    page = ok_data(client.get(f"/api/v1/materials/{bottle['uuid']}/states", params={"limit": 1}))
    assert [item["uuid"] for item in page["items"]] == [second["uuid"]] and page["has_more"] is True
    older = ok_data(client.get(f"/api/v1/materials/{bottle['uuid']}/states", params={
        "limit": 1, "before_time": page["next_before_time"], "before_uuid": page["next_before_uuid"]}))
    assert [item["uuid"] for item in older["items"]] == [first["uuid"]]
    assert ok_data(client.get(f"/api/v1/material-states/{first['uuid']}"))["state_data"] == {"temp": 20}
    error_of(client.get("/api/v1/material-states/missing"), 4001)


def test_reagent_info_reagent_and_history_flow(client) -> None:
    identities = _templates(client)
    bottle = ok_data(client.post("/api/v1/materials", json={
        "resource_template_uuid": identities["bottle_500ml"], "name": "乙醇瓶", "barcode": "BTL-E"}), status=201)
    # V1 控制台 createReagentInfo 的请求体
    info = ok_data(client.post("/api/v1/reagent-infos", json={
        "name": "乙醇", "name_en": "Ethanol", "aliases": ["EtOH"], "cas": "64-17-5", "molecular_formula": "C2H6O",
        "molecular_weight": 46.07, "density_g_per_ml": 0.789, "physical_state": "liquid", "meta_data": {},
    }), status=201)
    assert info["uuid"] and info["cas"] == "64-17-5" and info["create_time"].endswith("Z")
    error_of(client.post("/api/v1/reagent-infos", json={"name": "重复", "cas": "64-17-5"}), 4002)
    listed = ok_data(client.get("/api/v1/reagent-infos", params={"page": 1, "page_size": 100}))
    assert listed["total"] == 1 and listed["has_more"] is False
    renamed = ok_data(client.put(f"/api/v1/reagent-infos/{info['uuid']}", json={"description": "无水", "smiles": "CCO"}))
    assert renamed["description"] == "无水" and renamed["smiles"] == "CCO" and renamed["name"] == "乙醇"
    assert ok_data(client.get(f"/api/v1/reagent-infos/{info['uuid']}/structure-3d"))["status"] == "pending"

    # V1 控制台 createReagent 的请求体
    reagent = ok_data(client.post("/api/v1/reagents", json={
        "material_uuid": bottle["uuid"], "reagent_info_uuid": info["uuid"], "quantity": 400, "quantity_unit": "ml",
        "physical_state": "unknown", "source": "frontend:os-console", "meta_data": {},
    }), status=201)
    assert reagent["material_uuid"] == bottle["uuid"] and reagent["name"] == "乙醇"
    assert reagent["quantity"] == 400 and reagent["revision"] == 1 and reagent["container_barcode"] == "BTL-E"
    assert reagent["physical_state"] == "liquid" and reagent["active_workflow_reserved_quantity"] == 0
    error_of(client.post("/api/v1/reagents", json={
        "material_uuid": bottle["uuid"], "reagent_info_uuid": info["uuid"], "quantity": 1, "quantity_unit": "ml"}), 4002)
    # 试剂数量就是该容器内容物的数量（dev 物料聚合同一份数据）
    detail = client.get(f"/api/v1/materials/{bottle['uuid']}")
    assert ok_data(detail)["uuid"] == bottle["uuid"]
    assert ok_data(client.get("/api/v1/reagents", params={"barcode": "BTL-E"}))["items"][0]["uuid"] == reagent["uuid"]

    updated = ok_data(client.put(f"/api/v1/reagents/{reagent['uuid']}", json={
        "quantity": 350, "quantity_unit": "ml", "expected_revision": 1, "source": "frontend:os-console", "meta_data": {}}))
    assert updated["quantity"] == 350 and updated["revision"] == 2
    error_of(client.put(f"/api/v1/reagents/{reagent['uuid']}", json={
        "quantity": 1, "quantity_unit": "ml", "expected_revision": 1}), 4002)
    error_of(client.delete(f"/api/v1/reagent-infos/{info['uuid']}"), 4000)

    history = ok_data(client.get(f"/api/v1/materials/{bottle['uuid']}/reagent-history", params={"page": 1, "page_size": 100}))
    assert [item["event_type"] for item in history["items"]] == ["adjust", "create"]
    assert history["items"][0]["quantity_delta"] == -50 and history["items"][0]["subject_uuid"] == reagent["uuid"]
    assert history["items"][0]["changes"]["result"]["quantity"] == 350
    assert ok_data(client.get(f"/api/v1/reagent-history/{history['items'][0]['uuid']}"))["event_type"] == "adjust"

    assert ok_data(client.delete(f"/api/v1/reagents/{reagent['uuid']}")) is None
    error_of(client.get(f"/api/v1/reagents/{reagent['uuid']}"), 4001)
    assert ok_data(client.delete(f"/api/v1/reagent-infos/{info['uuid']}")) is None


def test_samples_current_substances_and_substance_history(client) -> None:
    identities = _templates(client)
    tube = ok_data(client.post("/api/v1/materials", json={
        "resource_template_uuid": identities["bottle_500ml"], "name": "样品管"}), status=201)
    info = ok_data(client.post("/api/v1/reagent-infos", json={"name": "水", "cas": "7732-18-5", "physical_state": "liquid"}), status=201)
    water_bottle = ok_data(client.post("/api/v1/materials", json={
        "resource_template_uuid": identities["bottle_500ml"], "name": "水瓶"}), status=201)
    water = ok_data(client.post("/api/v1/reagents", json={
        "material_uuid": water_bottle["uuid"], "reagent_info_uuid": info["uuid"], "quantity": 100, "quantity_unit": "ml"}), status=201)

    sample = ok_data(client.post("/api/v1/samples", json={
        "material_uuid": tube["uuid"], "code": "S-01", "name": "血清", "quantity": 2, "quantity_unit": "ml"}), status=201)
    assert sample["code"] == "S-01" and sample["material_uuid"] == tube["uuid"]
    error_of(client.post("/api/v1/samples", json={
        "material_uuid": water_bottle["uuid"], "code": "s-01", "name": "x", "quantity": 1, "quantity_unit": "ml"}), 6010)
    assert ok_data(client.get("/api/v1/samples", params={"keyword": "S-0"}))["total"] == 1
    changed = ok_data(client.put(f"/api/v1/samples/{sample['uuid']}", json={
        "material_uuid": tube["uuid"], "code": "S-01", "name": "血清", "quantity": 1.5, "quantity_unit": "ml"}))
    assert changed["quantity"] == 1.5 and changed["revision"] == 2

    current = ok_data(client.post("/api/v1/current-substances", json={
        "material_uuid": tube["uuid"], "name": "稀释液", "quantity": 3, "quantity_unit": "ml", "physical_state": "liquid",
        "components": [{"reagent_uuid": water["uuid"], "quantity": 3, "quantity_unit": "ml"}],
    }), status=201)
    assert current["components"][0]["reagent_info_uuid"] == info["uuid"] and current["components"][0]["sort_order"] == 0
    assert ok_data(client.get(f"/api/v1/materials/{tube['uuid']}/current-substance"))["uuid"] == current["uuid"]
    error_of(client.put(f"/api/v1/current-substances/{current['uuid']}", json={
        "material_uuid": tube["uuid"], "quantity": 2, "quantity_unit": "ml", "physical_state": "liquid", "expected_revision": 9}), 4002)
    ok_data(client.put(f"/api/v1/current-substances/{current['uuid']}", json={
        "material_uuid": tube["uuid"], "quantity": 2, "quantity_unit": "ml", "physical_state": "liquid", "expected_revision": 1}))
    history = ok_data(client.get(f"/api/v1/materials/{tube['uuid']}/substance-history"))
    assert {item["subject_type"] for item in history["items"]} == {"sample", "current_substance"}
    assert ok_data(client.get(f"/api/v1/substance-history/{history['items'][0]['uuid']}"))["uuid"] == history["items"][0]["uuid"]
    assert ok_data(client.delete(f"/api/v1/current-substances/{current['uuid']}")) is None
    assert ok_data(client.delete(f"/api/v1/samples/{sample['uuid']}")) is None
    assert ok_data(client.get("/api/v1/current-substances", params={"material_uuid": tube["uuid"]}))["total"] == 0


def test_compound_lookup_uses_local_registry_then_optional_source(tmp_path) -> None:
    from unilabos.server.api.materials.contents import create_v1_content_router

    class Source:
        def lookup_by_cas(self, cas):
            if cas == "50-00-0":
                raise LookupError(cas)
            return {"name": "Acetone", "molecular_formula": "C3H6O", "smiles": "CC(C)=O", "inchi_key": "K", "molecular_weight": 58.08}

    service = MaterialsService(tmp_path / "materials.db")
    app = FastAPI()
    app.include_router(create_v1_content_router(service, compound_source=Source()))
    try:
        with TestClient(app) as client:
            assert ok_data(client.get("/api/v1/compounds/67-64-1"))["compound"]["name"] == "Acetone"
            assert ok_data(client.get("/api/v1/compounds/50-00-0"))["status"] == "not_found"
            error_of(client.get("/api/v1/compounds/67-64-2"), 1000)
            ok_data(client.post("/api/v1/reagent-infos", json={"name": "丙酮", "cas": "67-64-1"}), status=201)
            assert ok_data(client.get("/api/v1/compounds/67-64-1"))["status"] == "registered"
    finally:
        service.close()


def test_reagent_info_batch_and_import(client) -> None:
    result = ok_data(client.post("/api/v1/reagent-infos/batch", json={"items": [
        {"name": "甲醇", "cas": "67-56-1"}, {"name": "丙酮", "cas": "67-64-1"}]}))
    assert result["created"] == 2 and result["failed"] == 0
    partial = ok_data(client.post("/api/v1/reagent-infos/batch", json={"atomic": False, "items": [
        {"name": "甲醇-重复", "cas": "67-56-1"}, {"name": "乙腈", "cas": "75-05-8"}]}))
    assert partial["created"] == 1 and partial["errors"][0]["row"] == 1
    error_of(client.post("/api/v1/reagent-infos/batch", json={"items": [{"name": "甲苯", "cas": "108-88-3"}, {"name": "x", "cas": "67-56-1"}]}), 4002)
    assert ok_data(client.get("/api/v1/reagent-infos", params={"name": "甲苯"}))["total"] == 0
    csv_body = "name,cas,aliases\n乙酸乙酯,141-78-6,EA;EtOAc\n"
    imported = ok_data(client.post("/api/v1/reagent-infos/import", files={"file": ("infos.csv", csv_body, "text/csv")}))
    assert imported["created"] == 1 and imported["items"][0]["aliases"] == ["EA", "EtOAc"]
