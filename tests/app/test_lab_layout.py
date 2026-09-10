"""实验室布局层（layout.py + domains.py）单元测试."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from unilabos.app.scheduler.inventory.domains import get_domain_pack, list_domain_packs
from unilabos.app.scheduler.inventory.layout import (
    create_lab_router,
    delete_placement,
    delete_zone,
    get_assembly,
    get_layout,
    get_profile,
    seed_demo,
    update_profile,
    upsert_placement,
    upsert_zone,
)
from unilabos.app.scheduler.inventory.service import InventoryService
from unilabos.app.scheduler.inventory.store import InventoryStore


@pytest.fixture()
def service() -> InventoryService:
    return InventoryService(InventoryStore(":memory:"), edge_id="edge-t", lab_id="lab-t")


def test_domain_packs_presets():
    packs = list_domain_packs()
    domains = {p["domain"] for p in packs}
    assert {"general", "organic", "inorganic", "bio", "materials"} <= domains
    assert get_domain_pack("nonexistent")["domain"] == "general"


def test_profile_roundtrip(service: InventoryService):
    profile = get_profile(service.store)
    assert profile["domain"] == "general"
    assert profile["pack"]["domain"] == "general"

    updated = update_profile(service.store, name="有机一号实验室", domain="organic")
    assert updated["name"] == "有机一号实验室"
    assert updated["domain"] == "organic"
    assert updated["pack"]["name"] == "有机化学实验室"

    # 持久化在 lab_meta，重新读取一致
    again = get_profile(service.store)
    assert again["domain"] == "organic"


def test_zone_placement_crud(service: InventoryService):
    upsert_zone(service.store, {"zone_id": "z1", "name": "台A", "kind": "bench",
                                "x": 10, "y": 20, "w": 300, "h": 150})
    upsert_zone(service.store, {"zone_id": "z1", "name": "台A改", "kind": "bench",
                                "x": 10, "y": 20, "w": 300, "h": 150})
    zones = service.store.list_zones()
    assert len(zones) == 1
    assert zones[0]["name"] == "台A改"
    assert zones[0]["version"] == 2

    service.register_instance(template_id="", edge_uuid="inst-1", barcode="B1")
    upsert_placement(service.store, {"subject_id": "inst-1", "zone_id": "z1",
                                     "x": 5, "y": 5, "w": 40, "h": 40, "label": "盒子"})
    layout = get_layout(service.store)
    assert len(layout["placements"]) == 1
    assert layout["placements"][0]["instance"]["edge_uuid"] == "inst-1"

    # 删除 zone 后 placement 保留但 zone_id 置空
    delete_zone(service.store, "z1")
    layout = get_layout(service.store)
    assert layout["zones"] == []
    assert layout["placements"][0]["zone_id"] == ""

    delete_placement(service.store, "inst-1")
    assert get_layout(service.store)["placements"] == []


def test_assembly_tree(service: InventoryService):
    import json as _json

    with service.store.transaction() as conn:
        conn.execute(
            "INSERT INTO inventory_resource_template("
            "template_id, name, category, spec_json, version) "
            "VALUES ('tpl-rack', '架', 'rack', ?, 1)",
            (_json.dumps({"grid": {"rows": 2, "cols": 3}}),),
        )
    service.register_instance(template_id="tpl-rack", edge_uuid="rack-1")
    service.register_instance(edge_uuid="tube-1", parent_uuid="rack-1", slot_id="A1")
    service.register_instance(edge_uuid="tube-2", parent_uuid="rack-1", slot_id="B3")
    service.update_content("tube-1", {"substance": "水", "volume_ml": 5})

    asm = get_assembly(service.store, "rack-1")
    root = asm["root"]
    assert root["edge_uuid"] == "rack-1"
    assert root["spec"]["grid"]["rows"] == 2
    slots = {c["slot_id"]: c for c in root["children"]}
    assert set(slots) == {"A1", "B3"}
    assert slots["A1"]["content"]["substance"] == "水"

    with pytest.raises(KeyError):
        get_assembly(service.store, "missing")


def test_seed_demo_idempotent(service: InventoryService):
    first = seed_demo(service)
    assert first["instances"] > 0
    second = seed_demo(service)
    assert second["instances"] == 0  # 幂等：重复种子不再新建实例
    assert second["lots"] == 0

    layout = get_layout(service.store)
    assert len(layout["zones"]) == 6
    deck = next(p for p in layout["placements"] if p["subject_id"] == "demo-deck-a")
    assert deck["children_count"] == 3  # rack×2 + plate

    # 存储区无手工摆放：仓储内容从批次库位派生
    assert all(p["zone_id"] not in ("zone-storage", "zone-cold")
               for p in layout["placements"])

    asm = get_assembly(service.store, "demo-deck-a")
    rack1 = next(c for c in asm["root"]["children"] if c["edge_uuid"] == "demo-rack-1")
    assert len(rack1["children"]) == 5


def test_storage_summary_derived_from_lots(service: InventoryService):
    """地图存储区渲染依据 = 批次库位（物料性质映射），不是手工摆放."""
    seed_demo(service)
    summary = get_layout(service.store)["storage_summary"]

    ambient = {i["template_id"]: i for i in summary["zone-storage"]}
    assert ambient["reagent-naoh"]["quantity_available"] == 500.0
    assert ambient["reagent-naoh"]["hazard_class"] == "corrosive"
    assert ambient["reagent-etoh"]["storage_class"] == "flammable_cabinet"

    cold = {i["template_id"]: i for i in summary["zone-cold"]}
    assert cold["reagent-enzyme"]["storage_class"] == "cold"

    # 消耗后派生视图跟随批次数量变化
    from unilabos.app.scheduler.inventory.domain import MaterialRequirement

    service.reserve_workflow(
        "wf-x", {"n1": [MaterialRequirement(template_id="reagent-naoh", quantity=100.0)]}
    )
    service.consume_reservation("wf-x", "n1")
    summary2 = get_layout(service.store)["storage_summary"]
    naoh = next(i for i in summary2["zone-storage"] if i["template_id"] == "reagent-naoh")
    assert naoh["quantity_available"] == 400.0


def test_warehouse_view_categories(service: InventoryService):
    """品类库存聚合对齐云端 MaterialWarehouse：模板性质 × 批次 × 实例."""
    from unilabos.app.scheduler.inventory.warehouse import build_warehouse_view

    seed_demo(service)
    view = build_warehouse_view(service.store)
    cats = {c["template_id"]: c for c in view["categories"]}

    naoh = cats["reagent-naoh"]
    assert naoh["storage_class"] == "ambient"
    assert naoh["hazard_class"] == "corrosive"
    assert naoh["quantity_available"] == 500.0
    assert naoh["batch_count"] == 1
    assert naoh["zones"][0]["zone_id"] == "zone-storage"

    # 一物一码实例计数：两只 500mL 瓶在库
    bottle = cats["tpl-bottle-500"]
    assert bottle["in_stock_instances"] == 2

    # 纯模板（无批次无实例）也在品类目录中
    assert "tpl-plate-96" in cats
    assert {s["id"] for s in view["storage_classes"]} >= {"ambient", "cold"}


def test_lab_router_http(service: InventoryService):
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(create_lab_router(service))
    client = TestClient(app)

    assert client.get("/api/v1/lab/profile").json()["domain"] == "general"
    resp = client.put("/api/v1/lab/profile", json={"domain": "bio"})
    assert resp.json()["pack"]["name"] == "生物实验室"

    assert client.post("/api/v1/lab/demo").status_code == 200
    layout = client.get("/api/v1/lab/layout").json()
    assert len(layout["zones"]) == 6
    assert "zone-storage" in layout["storage_summary"]

    wh = client.get("/api/v1/lab/warehouse").json()
    assert any(c["template_id"] == "reagent-naoh" for c in wh["categories"])

    asm = client.get("/api/v1/lab/assembly/demo-deck-a").json()
    assert asm["root"]["edge_uuid"] == "demo-deck-a"
    assert client.get("/api/v1/lab/assembly/nope").status_code == 404
