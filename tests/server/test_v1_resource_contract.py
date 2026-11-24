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


def test_v1_material_name_is_the_display_name(client) -> None:
    """V1 POST 的 name 落到 display_name；PUT 的 name / display_name 只改 display_name。"""

    identities = _templates(client)
    created = ok_data(client.post("/api/v1/materials", json={
        "resource_template_uuid": identities["bottle_500ml"], "name": "瓶-1",
    }), status=201)
    assert created["display_name"] == "瓶-1"
    labelled = ok_data(client.post("/api/v1/materials", json={
        "resource_template_uuid": identities["bottle_500ml"], "name": "瓶-2", "display_name": "二号瓶",
    }), status=201)
    assert labelled["display_name"] == "二号瓶"

    renamed = ok_data(client.put(f"/api/v1/materials/{created['uuid']}", json={"name": "瓶-改"}))
    assert renamed["display_name"] == "瓶-改" and renamed["name"] == created["name"]
    relabelled = ok_data(client.put(f"/api/v1/materials/{created['uuid']}", json={
        "name": "忽略", "display_name": "一号瓶",
    }))
    assert relabelled["display_name"] == "一号瓶" and relabelled["name"] == created["name"]

    # 列表的 name 筛选按显示名匹配
    listed = ok_data(client.get("/api/v1/materials", params={"name": "二号"}))
    assert [item["uuid"] for item in listed["items"]] == [labelled["uuid"]]


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
    # V1 的 name 是显示名：PUT 只改 display_name，不连带改物料 name
    assert updated["display_name"] == "瓶-改" and updated["config"]["capacity"] == {"max_volume_ul": 500000}
    assert updated["name"] == bottle["name"]
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


def test_v1_create_places_and_fills_in_one_transaction(client) -> None:
    """建物料、放入库位与装液是同一个事务：装液失败时物料与落位一起回滚，库位仍空、不留半成品。"""

    identities = _templates(client)
    rack = ok_data(client.post("/api/v1/materials", json={
        "resource_template_uuid": identities["tube_rack_2"], "name": "架-T"}), status=201)
    site_uuid = rack["site_uuids"][0]

    def total() -> int:
        return ok_data(client.get("/api/v1/materials", params={"page": 1, "page_size": 100}))["total"]

    before = total()
    failed = client.post("/api/v1/materials", json={
        "resource_template_uuid": identities["bottle_500ml"], "name": "瓶-坏",
        "site_placement": {"action": "place", "site_uuid": site_uuid},
        "reagent": {"quantity": 5},  # 缺 quantity_unit：装液被拒
    })
    assert failed.json()["code"] == 1000, failed.text
    assert total() == before
    assert ok_data(client.get(f"/api/v1/sites/{site_uuid}"))["occupied_material_uuid"] is None

    placed = ok_data(client.post("/api/v1/materials", json={
        "resource_template_uuid": identities["bottle_500ml"], "name": "瓶-好",
        "site_placement": {"action": "place", "site_uuid": site_uuid},
    }), status=201)
    assert placed["current_site"]["uuid"] == site_uuid and placed["parent_uuid"] == rack["uuid"]
    assert ok_data(client.get(f"/api/v1/sites/{site_uuid}"))["occupied_material_uuid"] == placed["uuid"]


def test_v1_create_initializes_a_plr_material_that_a_device_site_accepts(tmp_path) -> None:
    """V1 POST /materials 先初始化出 PLR 实例（config.type 或模板的 PLR 类），能放上设备库位、能反序列化。"""

    from uuid import uuid4

    from unilabos.client.materials import LocalMaterialsClient
    from unilabos.resources import materials
    from unilabos.resources.adapters.device_site import apply_device_authority_state
    from unilabos.resources.resource_tracker import ResourceDict, ResourceDictInstance

    service = MaterialsService(tmp_path / "materials.db")
    device_uuid = str(uuid4())
    device = ResourceDictInstance(ResourceDict.model_validate({
        "id": "v1-device", "uuid": device_uuid, "name": "v1-device", "type": "device", "class": "v1_device",
        "template_name": "v1_device", "config": {}, "data": {}, "extra": {}, "sites": None, "sites_initialized": False,
    }))
    apply_device_authority_state(device, {"available_sites": [{"index": 1, "label": "s1"}, {"index": 2, "label": "s2"}]},
                                 "v1_device", gateway=LocalMaterialsClient(service))
    sites = {site.label: site.site_uuid for site in service.get_material(device_uuid).sites}
    app = FastAPI()
    install_materials_api(app, service)
    try:
        with TestClient(app) as client:
            identities = _templates(client)
            placed = ok_data(client.post("/api/v1/materials", json={
                "resource_template_uuid": identities["bottle_500ml"], "name": "设备上的瓶",
                "config": {"max_volume": 500},
                "site_placement": {"action": "place", "site_uuid": sites["s1"]},
            }), status=201)
            assert placed["parent_uuid"] == device_uuid and placed["current_site"]["uuid"] == sites["s1"]
            assert placed["config"]["type"] == "Container" and placed["config"]["max_volume"] == 500
            restored = materials.get(placed["uuid"], gateway=LocalMaterialsClient(service)).to_plr_resources()[0]
            assert type(restored).__name__ == "Container" and restored.max_volume == 500

            # 显式 config.type 优先于模板的实现类
            typed = ok_data(client.post("/api/v1/materials", json={
                "resource_template_uuid": identities["tube_rack_2"], "name": "显式类型",
                "config": {"type": "Container"},
                "site_placement": {"action": "place", "site_uuid": sites["s2"]},
            }), status=201)
            assert typed["config"]["type"] == "Container"

            # 实例化不出来：说明原因（1000），不建任何东西
            odd = ok_data(client.post("/api/v1/resource-templates", json={"resources": [{
                "id": "odd_thing", "registry_type": "resource", "class": {"module": "lab.custom:Thing", "type": "python"},
                "category": ["thing"],
            }]}))["templates"][0]["uuid"]
            before = ok_data(client.get("/api/v1/materials", params={"page_size": 100}))["total"]
            failed = client.post("/api/v1/materials", json={"resource_template_uuid": odd, "name": "怪东西"}).json()
            assert failed["code"] == 1000 and "config.type" in str(failed)
            assert ok_data(client.get("/api/v1/materials", params={"page_size": 100}))["total"] == before
    finally:
        service.close()


def test_v1_material_records_carry_inventory_facts(client) -> None:
    """V1 物料 DTO 带 inventory_status / inventory_presence / disposition，由生命周期派生。"""

    from unilabos.server.services.materials.resource_contract import inventory_facts

    identities = _templates(client)
    bottle = ok_data(client.post("/api/v1/materials", json={
        "resource_template_uuid": identities["bottle_500ml"], "name": "瓶-1"}), status=201)
    in_stock = {"inventory_status": "warehouse", "inventory_presence": "current", "disposition": "active"}
    assert {key: bottle[key] for key in in_stock} == in_stock
    listed = ok_data(client.get("/api/v1/materials"))["items"][0]
    assert {key: listed[key] for key in in_stock} == in_stock
    node = ok_data(client.get("/api/v1/materials/graph"))["nodes"][0]["material"]
    assert {key: node[key] for key in in_stock} == in_stock

    # 只有在库（active）能被新任务预留；设备不是库存物料；软删记为废弃
    assert inventory_facts("plate", "reserved", None)["inventory_status"] == "reserved"
    assert inventory_facts("plate", "in_use", None)["inventory_status"] == "in_use"
    assert inventory_facts("plate", "quarantined", None)["inventory_status"] == "quarantined"
    assert inventory_facts("plate", "consumed", None)["inventory_status"] == "consumed"
    assert inventory_facts("plate", "retired", None)["inventory_status"] == "used"
    assert inventory_facts("device", "active", None) == {
        "inventory_status": None, "inventory_presence": None, "disposition": None,
    }
    assert inventory_facts("plate", "retired", 123)["disposition"] == "discarded"


def test_material_list_filters_by_category_scope_and_empty_containers(client) -> None:
    identities = _templates(client)
    empty = ok_data(client.post("/api/v1/materials", json={
        "resource_template_uuid": identities["bottle_500ml"], "name": "空瓶"}), status=201)
    filled = ok_data(client.post("/api/v1/materials", json={
        "resource_template_uuid": identities["bottle_500ml"], "name": "样品瓶"}), status=201)
    ok_data(client.post("/api/v1/samples", json={
        "material_uuid": filled["uuid"], "code": "S-01", "name": "血清", "quantity": 0, "quantity_unit": "ml"}), status=201)

    def uuids(**params) -> set[str]:
        return {item["uuid"] for item in ok_data(client.get("/api/v1/materials", params=params))["items"]}

    assert uuids(category="material") == {empty["uuid"], filled["uuid"]}
    assert uuids(category="device") == set()
    # 数量为 0 的有效登记仍算非空
    assert uuids(empty_containers_only="true") == {empty["uuid"]}
    history = ok_data(client.get("/api/v1/materials", params={"inventory_scope": "history"}))
    assert history["items"] == [] and history["total"] == 0
    assert uuids(inventory_scope="current") == {empty["uuid"], filled["uuid"]}
    error_of(client.get("/api/v1/materials", params={"inventory_scope": "bogus"}), 1000)
    error_of(client.get("/api/v1/materials", params={"category": "bogus"}), 1000)


def test_empty_containers_are_containers_without_contents_in_any_form(tmp_path) -> None:
    """内容物行（content.set / put_data / 设备快照写的）也算内容；设备、台面、载架不是容器。"""

    from uuid import uuid4

    from unilabos.protocol.materials import (
        InventoryMutation,
        MaterialDataWrite,
        MaterialIdentityWrite,
        MaterialNodeCreate,
        MaterialSubstance,
        MaterialTreeCreate,
    )

    service = MaterialsService(tmp_path / "materials.db")
    app = FastAPI()
    install_materials_api(app, service)

    def mutation(operation: str) -> InventoryMutation:
        command = str(uuid4())
        return InventoryMutation(command_uuid=command, effect_key=f"{operation}:{command}", operation=operation,
                                 actor_type="test", actor_uuid="empty-containers")

    def create(resource_type: str) -> str:
        return service.create_tree(mutation("create_material_tree"), MaterialTreeCreate(nodes=[MaterialNodeCreate(
            client_ref="root", identity=MaterialIdentityWrite(
                resource_id=f"{resource_type}-{uuid4().hex[:6]}", template_name=f"{resource_type}_tpl",
                resource_type=resource_type, config={"type": "Resource"}),
        )])).data.root_material_uuid

    try:
        with TestClient(app) as client:
            identities = _templates(client)

            def bottle(name: str) -> str:
                return ok_data(client.post("/api/v1/materials", json={
                    "resource_template_uuid": identities["bottle_500ml"], "name": name}), status=201)["uuid"]

            empty, set_by_command, put_by_data, drained = bottle("空瓶"), bottle("命令装液"), bottle("数据装液"), bottle("用空")
            ok_data(client.post("/api/v1/inventory/commands", json={
                "command_id": "fill-1", "type": "material.content.set",
                "payload": {"edge_uuid": set_by_command, "state": {"substances": [
                    {"name": "水", "quantity": 10, "quantity_unit": "ml"}]}}}))
            for material_uuid, quantity in ((put_by_data, 5.0), (drained, 0.0)):
                service.put_data(mutation("put_data"), material_uuid, MaterialDataWrite(substances=[
                    MaterialSubstance(name="水", quantity=quantity, quantity_unit="ml")]))
            not_containers = {create("device"), create("deck"), create("plate_carrier"), create("warehouse")}

            listed = {item["uuid"] for item in ok_data(client.get(
                "/api/v1/materials", params={"empty_containers_only": "true", "page_size": 100}))["items"]}

            assert listed == {empty, drained}
            assert not listed & not_containers
    finally:
        service.close()


def test_material_graph_rereads_only_the_named_materials_and_their_site_owners(client) -> None:
    identities = _templates(client)
    rack = ok_data(client.post("/api/v1/materials", json={
        "resource_template_uuid": identities["tube_rack_2"], "name": "架-1"}), status=201)
    bottle = ok_data(client.post("/api/v1/materials", json={
        "resource_template_uuid": identities["bottle_500ml"], "name": "瓶-1",
        "site_placement": {"action": "place", "site_uuid": rack["site_uuids"][0]},
    }), status=201)
    ok_data(client.post("/api/v1/materials", json={
        "resource_template_uuid": identities["bottle_500ml"], "name": "旁边的瓶"}), status=201)

    def graph(**params) -> dict:
        return ok_data(client.get("/api/v1/materials/graph", params=params))

    assert len(graph()["nodes"]) == 3
    # 瓶在架的库位上：客户端要在同一响应里解析 current_site_uuid，架也一起回来
    partial = graph(material_uuid=[bottle["uuid"]])
    assert {node["material"]["uuid"] for node in partial["nodes"]} == {bottle["uuid"], rack["uuid"]}
    assert all(link["source_uuid"] in {bottle["uuid"], rack["uuid"]} for link in partial["links"])
    # 不存在 / 已删除的物料不返回：客户端据此从图里移除
    assert graph(material_uuid=["00000000-0000-4000-8000-000000000000"])["nodes"] == []
    # 空值不算点名（``?material_uuid=``）：仍是全量
    assert len(graph(material_uuid=[""])["nodes"]) == 3
    assert {node["material"]["uuid"] for node in graph(material_uuid=["", bottle["uuid"]])["nodes"]} == {
        bottle["uuid"], rack["uuid"],
    }
    assert graph(inventory_scope="history") == {"nodes": [], "links": []}
    error_of(client.get("/api/v1/materials/graph", params={"inventory_scope": "bogus"}), 1000)


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
