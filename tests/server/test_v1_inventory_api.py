"""V1 Edge 库存面 ``/api/v1/inventory/*``：统一命令入口（dev materials.v1 变更 + V1 类型化命令）、
读路由（具名数组 + limit）、工站入站预留，以及 HTTP 物料网关走这些路径的往返。
"""

from __future__ import annotations

import contextlib
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.api_envelope import error_of, ok_data
from unilabos.client.materials import HTTPMaterialsClient, LocalMaterialsClient, bind_payload
from unilabos.protocol.materials import (
    InventoryLotInbound,
    InventoryMutation,
    MaterialDataWrite,
    MaterialIdentityWrite,
    MaterialMove,
    MaterialNodeCreate,
    MaterialPatch,
    MaterialSubstance,
    MaterialTreeCreate,
    ResourceTemplateWrite,
)
from unilabos.server.api.materials import install_materials_api
from unilabos.server.services.materials import MaterialsService


def _mutation(operation: str, **kwargs) -> InventoryMutation:
    return InventoryMutation(command_uuid=str(uuid4()), effect_key=operation, operation=operation, **kwargs)


def _command(operation: str, value, **extra) -> dict:
    body = bind_payload(_mutation(operation, actor_uuid="operator-1"), value).model_dump(mode="json", exclude_none=False)
    return {**body, **extra}


@pytest.fixture()
def stack(tmp_path):
    service = MaterialsService(tmp_path / "materials.db")
    app = FastAPI()
    install_materials_api(app, service)
    with TestClient(app) as client:
        yield service, client
    service.close()


def _tree(name: str, template: str = "bottle", *, sites=None) -> MaterialTreeCreate:
    return MaterialTreeCreate(nodes=[MaterialNodeCreate(
        client_ref="root",
        identity=MaterialIdentityWrite(resource_id=name, name=name, resource_type="container",
                                       class_name="Container", template_name=template),
        sites=sites or [],
    )])


def _seed(client) -> tuple[str, str]:
    template = ResourceTemplateWrite(template_uuid="bottle-template", name="bottle", resource_type="container",
                                     class_name="Container")
    result = client.post("/api/v1/inventory/commands", json=_command("put_template", template))
    assert result.status_code == 200 and result.json()["status"] == "completed", result.text
    created = client.post("/api/v1/inventory/commands", json=_command("create_material_tree", _tree("瓶-A"))).json()
    assert created["status"] == "completed", created
    return "bottle-template", created["result"]["data"]["root_material_uuid"]


def test_command_entry_runs_materials_v1_mutations_idempotently(stack) -> None:
    _service, client = stack
    template_uuid, material_uuid = _seed(client)
    patch = _command("patch_material", MaterialPatch(barcode="BC-9"), target_uuid=material_uuid)
    first = client.post("/api/v1/inventory/commands", json=patch).json()
    replay = client.post("/api/v1/inventory/commands", json=patch).json()
    # V1 裸结果：不包信封，command_id 是 dev 的 command_uuid
    assert set(first) == {"command_id", "status", "result", "replayed"}
    assert first["status"] == "completed" and first["replayed"] is False
    assert replay["replayed"] is True and replay["result"]["data"]["material"]["barcode"] == "BC-9"
    missing = client.post("/api/v1/inventory/commands", json=_command(
        "patch_material", MaterialPatch(name="x"), target_uuid="no-such-material")).json()
    assert missing["status"] == "rejected" and missing["code"] == 6000 and missing["error_code"] == "not_found:material"
    no_target = client.post("/api/v1/inventory/commands", json=_command("put_data", MaterialDataWrite())).json()
    assert no_target["status"] == "rejected" and no_target["code"] == 1000
    unknown = client.post("/api/v1/inventory/commands", json=_command("explode", MaterialPatch())).json()
    assert unknown["status"] == "rejected" and unknown["error_code"] == "unsupported_command"

    # 读路由：具名数组 + V1 标识别名
    instance = ok_data(client.get(f"/api/v1/inventory/instances/{material_uuid}"))
    assert instance["edge_uuid"] == material_uuid and instance["material"]["barcode"] == "BC-9"
    assert instance["status"] == "active" and instance["relation"] is None
    instances = ok_data(client.get("/api/v1/inventory/instances", params={"limit": 10}))["instances"]
    assert [item["edge_uuid"] for item in instances] == [material_uuid]
    assert ok_data(client.get("/api/v1/inventory/instances", params={"status": "retired"}))["instances"] == []
    templates = ok_data(client.get("/api/v1/inventory/templates"))["templates"]
    assert templates[0]["template_id"] == template_uuid
    assert ok_data(client.get(f"/api/v1/inventory/templates/{template_uuid}"))["name"] == "bottle"
    entries = ok_data(client.get("/api/v1/inventory/ledger", params={"after_id": 0, "limit": 500}))["entries"]
    assert entries and entries[0]["sequence"] == 1
    processed = ok_data(client.get("/api/v1/inventory/commands/processed"))["commands"]
    assert {item["family"] for item in processed} == {"materials.v1"}
    error_of(client.get("/api/v1/inventory/instances/missing"), 6000)


def test_typed_lot_commands_inbound_correct_reconcile_and_reads(stack) -> None:
    service, client = stack
    _seed(client)
    reagent = ResourceTemplateWrite(template_uuid="naoh-template", name="naoh", resource_type="reagent", class_name="Reagent")
    client.post("/api/v1/inventory/commands", json=_command("put_template", reagent))
    inbound = client.post("/api/v1/inventory/commands", json={
        "command_id": "cmd-inbound-1", "type": "inventory.inbound", "actor": "operator-1",
        "payload": {"template_id": "naoh", "lot_id": "lot-naoh", "quantity": 500, "unit": "ml",
                    "batch_no": "B1", "warehouse_zone_id": "zone-storage"},
    }).json()
    assert inbound["status"] == "completed" and inbound["result"]["lot_id"] == "lot-naoh"
    # 同一 command_id 重放返回首次结果，不再执行
    again = client.post("/api/v1/inventory/commands", json={
        "command_id": "cmd-inbound-1", "type": "inventory.inbound", "actor": "operator-1",
        "payload": {"template_id": "naoh", "lot_id": "lot-naoh", "quantity": 500, "unit": "ml",
                    "batch_no": "B1", "warehouse_zone_id": "zone-storage"},
    }).json()
    assert again["replayed"] is True and service.get_inventory_lot("lot-naoh").quantity_total == 500
    conflict = client.post("/api/v1/inventory/commands", json={
        "command_id": "cmd-inbound-1", "type": "inventory.inbound", "payload": {"template_id": "naoh", "quantity": 1}}).json()
    assert conflict["status"] == "rejected" and conflict["code"] == 3003

    lot = ok_data(client.get("/api/v1/inventory/lots/lot-naoh"))
    assert lot["warehouse_zone_id"] == "zone-storage" and lot["lot_id"] == "lot-naoh"
    corrected = client.post("/api/v1/inventory/commands", json={
        "command_id": "cmd-correct-1", "type": "inventory.lot.correct", "actor": "operator-1",
        "payload": {"lot_id": "lot-naoh", "expected_version": lot["version"], "measured_quantity": 480.5,
                    "quarantined": True, "reason": "称重复核"},
    }).json()
    assert corrected["status"] == "completed" and corrected["result"]["quantity_total"] == 480.5
    assert ok_data(client.get("/api/v1/inventory/lots"))["lots"][0]["quarantined"] is True
    rejected = client.post("/api/v1/inventory/commands", json={
        "command_id": "cmd-reconcile-1", "type": "inventory.reconcile_count", "actor": "operator-1",
        "payload": {"lot_id": "lot-naoh", "measured_count": 3, "reason": "盘点"}}).json()
    assert rejected["status"] == "rejected" and rejected["code"] == 1000
    unsupported = client.post("/api/v1/inventory/commands", json={
        "command_id": "cmd-x", "type": "material.deploy", "payload": {}}).json()
    assert unsupported["status"] == "rejected" and unsupported["error_code"] == "unsupported_command"
    processed = ok_data(client.get("/api/v1/inventory/commands/processed"))["commands"]
    assert {"cmd-inbound-1", "cmd-correct-1", "cmd-reconcile-1"} <= {item["command_id"] for item in processed}
    assert ok_data(client.get("/api/v1/inventory/health"))["status"] == "ok"
    assert "max_sequence" in ok_data(client.get("/api/v1/inventory/outbox/backlog"))
    assert ok_data(client.get("/api/v1/inventory/sync/cursors"))["cursors"][0]["name"] == "materials.ledger"
    snapshot = ok_data(client.get("/api/v1/inventory/snapshot"))
    assert {"templates", "lots", "instances", "reservations", "contents"} <= set(snapshot)


def test_reagent_dispense_command_matches_v1_console_contract(stack) -> None:
    _service, client = stack
    identities = {item["name"]: item["uuid"] for item in ok_data(client.post("/api/v1/resource-templates", json={
        "resources": [{"id": "bottle", "registry_type": "resource", "category": ["container"]}]}))["templates"]}
    source, *targets = (
        ok_data(client.post("/api/v1/materials", json={"resource_template_uuid": identities["bottle"], "name": name}), status=201)
        for name in ("源瓶", "分装-1", "分装-2")
    )
    info = ok_data(client.post("/api/v1/reagent-infos", json={"name": "乙醇", "cas": "64-17-5", "physical_state": "liquid"}), status=201)
    reagent = ok_data(client.post("/api/v1/reagents", json={
        "material_uuid": source["uuid"], "reagent_info_uuid": info["uuid"], "quantity": 100, "quantity_unit": "ml"}), status=201)
    command = {
        "command_id": "dispense-1", "type": "reagent.dispense", "actor": "frontend:os-console",
        "payload": {"source_reagent_uuid": reagent["uuid"], "expected_revision": 1, "quantity_unit": "ml",
                    "targets": [{"material_uuid": item["uuid"], "quantity": 10} for item in targets], "reason": "分装"},
    }
    response = client.post("/api/v1/inventory/commands", json=command)
    body = response.json()
    assert response.status_code == 200 and body["command_id"] == "dispense-1" and body["status"] == "completed", body
    assert body["result"]["source"]["quantity"] == 80 and body["result"]["source"]["quantity_unit"] == "ml"
    assert [item["quantity"] for item in body["result"]["targets"]] == [10, 10]
    assert all(item["reagent_uuid"] and isinstance(item["revision"], int) for item in body["result"]["targets"])
    assert client.post("/api/v1/inventory/commands", json=command).json()["replayed"] is True
    assert ok_data(client.get(f"/api/v1/reagents/{reagent['uuid']}"))["quantity"] == 80
    too_much = client.post("/api/v1/inventory/commands", json={**command, "command_id": "dispense-2", "payload": {
        **command["payload"], "expected_revision": None, "targets": [{"material_uuid": source["uuid"], "quantity": 500}]}}).json()
    assert too_much["status"] == "rejected"


def test_content_reservations_prevent_overdrawing_a_bottle(stack) -> None:
    _service, client = stack
    identities = {item["name"]: item["uuid"] for item in ok_data(client.post("/api/v1/resource-templates", json={
        "resources": [{"id": "bottle", "registry_type": "resource"}]}))["templates"]}
    bottle = ok_data(client.post("/api/v1/materials", json={"resource_template_uuid": identities["bottle"], "name": "瓶"}), status=201)
    info = ok_data(client.post("/api/v1/reagent-infos", json={"name": "水", "cas": "7732-18-5"}), status=201)
    reagent = ok_data(client.post("/api/v1/reagents", json={
        "material_uuid": bottle["uuid"], "reagent_info_uuid": info["uuid"], "quantity": 100, "quantity_unit": "ml"}), status=201)

    def reserve(command_id, task, quantity, unit="ml"):
        return client.post("/api/v1/inventory/commands", json={"command_id": command_id, "type": "content.reserve", "payload": {
            "substance_uuid": reagent["uuid"], "task_uuid": task, "node_uuid": "n1", "quantity": quantity, "quantity_unit": unit}}).json()

    first = reserve("r-1", "task-a", 60)
    assert first["status"] == "completed" and first["result"]["status"] == "active"
    second = reserve("r-2", "task-b", 0.05, unit="l")
    assert second["status"] == "rejected" and second["code"] == 3003  # 60 + 50 > 100
    assert reserve("r-3", "task-b", 40)["status"] == "completed"
    assert ok_data(client.get(f"/api/v1/reagents/{reagent['uuid']}"))["active_workflow_reserved_quantity"] == 100
    # 人工改量不能低于活动预留
    error_of(client.put(f"/api/v1/reagents/{reagent['uuid']}", json={"quantity": 50, "quantity_unit": "ml"}), 3003)
    consumed = client.post("/api/v1/inventory/commands", json={"command_id": "c-1", "type": "content.consume", "payload": {
        "reservation_uuid": first["result"]["reservation_uuid"], "quantity": 55}}).json()
    assert consumed["status"] == "completed" and consumed["result"]["status"] == "consumed"
    assert ok_data(client.get(f"/api/v1/reagents/{reagent['uuid']}"))["quantity"] == 45
    history = ok_data(client.get(f"/api/v1/materials/{bottle['uuid']}/reagent-history"))["items"]
    assert history[0]["event_type"] == "consume" and history[0]["workflow_task_uuid"] == "task-a"
    listed = ok_data(client.get("/api/v1/inventory/reservations", params={"task_uuid": "task-b"}))
    assert [item["status"] for item in listed["content_reservations"]] == ["active"]


def test_material_source_bindings_share_or_exclude_and_block_delete(stack) -> None:
    _service, client = stack
    identities = {item["name"]: item["uuid"] for item in ok_data(client.post("/api/v1/resource-templates", json={
        "resources": [{"id": "bottle", "registry_type": "resource"}]}))["templates"]}
    bottle = ok_data(client.post("/api/v1/materials", json={"resource_template_uuid": identities["bottle"], "name": "瓶"}), status=201)

    def bind(command_id, task, policy):
        return client.post("/api/v1/inventory/commands", json={"command_id": command_id, "type": "source.bind", "payload": {
            "task_uuid": task, "node_uuid": "n1", "material_uuid": bottle["uuid"], "custody_policy": policy}}).json()

    assert bind("b-1", "task-a", "shared_source")["status"] == "completed"
    assert bind("b-2", "task-b", "shared_source")["status"] == "completed"
    exclusive = bind("b-3", "task-c", "task_exclusive")
    assert exclusive["status"] == "rejected" and exclusive["code"] == 6011
    error_of(client.delete(f"/api/v1/materials/{bottle['uuid']}"), 6011)
    for task in ("task-a", "task-b"):
        assert client.post("/api/v1/inventory/commands", json={
            "command_id": f"rel-{task}", "type": "source.release", "payload": {"task_uuid": task}}).json()["status"] == "completed"
    assert bind("b-4", "task-c", "task_exclusive")["status"] == "completed"


def test_station_ingress_reservation_lifecycle(stack) -> None:
    from unilabos.protocol.materials import SiteCreate

    _service, client = stack
    _seed(client)
    rack = client.post("/api/v1/inventory/commands", json=_command("create_material_tree", _tree(
        "入口架", sites=[SiteCreate(template_name="bottle", site_index=index, label=label)
                      for index, label in enumerate(("IN-1", "IN-2"))]))).json()["result"]["data"]
    rack_aggregate = ok_data(client.get(f"/api/v1/inventory/instances/{rack['root_material_uuid']}"))
    sites = [site["site_uuid"] for site in rack_aggregate["sites"]]
    carrier = client.post("/api/v1/inventory/commands", json=_command("create_material_tree", _tree("载体"))).json()
    carrier_uuid = carrier["result"]["data"]["root_material_uuid"]
    request = {"idempotency_key": "agv-1", "carrier_material_uuid": carrier_uuid, "candidate_site_uuids": sites,
               "ttl_seconds": 600, "backend_task_uuid": "task-1"}
    reserved = ok_data(client.post("/api/v1/inventory/ingress-reservations", json=request), status=201)
    assert reserved["state"] == "reserved" and reserved["site_uuid"] == sites[0] and reserved["revision"] == 1
    assert ok_data(client.post("/api/v1/inventory/ingress-reservations", json=request), status=201)["uuid"] == reserved["uuid"]
    error_of(client.post("/api/v1/inventory/ingress-reservations", json={**request, "ttl_seconds": 5}), 3003)
    second = ok_data(client.post("/api/v1/inventory/ingress-reservations", json={**request, "idempotency_key": "agv-2"}), status=201)
    assert second["site_uuid"] == sites[1]
    error_of(client.post("/api/v1/inventory/ingress-reservations", json={**request, "idempotency_key": "agv-3"}), 6007)
    error_of(client.post(f"/api/v1/inventory/ingress-reservations/{reserved['uuid']}/receive"), 3003)
    moving = ok_data(client.post(f"/api/v1/inventory/ingress-reservations/{reserved['uuid']}/in-transit"))
    assert moving["state"] == "in_transit" and moving["transport_started_at"]
    received = ok_data(client.post(f"/api/v1/inventory/ingress-reservations/{reserved['uuid']}/receive"))
    assert received["state"] == "received"
    placed = ok_data(client.get(f"/api/v1/inventory/instances/{carrier_uuid}"))
    assert placed["relation"]["site_uuid"] == sites[0]
    canceled = ok_data(client.post(f"/api/v1/inventory/ingress-reservations/{second['uuid']}/cancel", json={"reason": "改派"}))
    assert canceled["state"] == "canceled" and canceled["cancel_reason"] == "改派"
    error_of(client.post(f"/api/v1/inventory/ingress-reservations/{second['uuid']}/cancel", json={"reason": "别的"}), 3003)
    error_of(client.get("/api/v1/inventory/ingress-reservations/missing"), 4001)


class _TestClientOpener:
    """把 urllib 请求转给 FastAPI TestClient（HTTP 网关的端到端往返）。"""

    def __init__(self, client: TestClient) -> None:
        self.client = client

    def __call__(self, request, timeout=None):
        response = self.client.request(request.get_method(), request.full_url, content=request.data,
                                       headers=dict(request.header_items()))

        class _Response:
            status = response.status_code

            def read(self):
                return response.content

        return contextlib.nullcontext(_Response())


def test_http_materials_gateway_round_trips_through_v1_paths(stack, monkeypatch) -> None:
    service, client = stack
    monkeypatch.setattr("unilabos.client.materials.core.urlopen", _TestClientOpener(client))
    gateway = HTTPMaterialsClient("http://testserver")
    local = LocalMaterialsClient(service)
    template = ResourceTemplateWrite(name="tube", resource_type="container", class_name="Container")
    created = gateway.create_template(_mutation("create_template"), template).data
    assert gateway.get_template(created.template_uuid).name == "tube"
    assert [item.name for item in gateway.list_templates(name="tube")] == ["tube"]
    tree = gateway.create_tree(_mutation("create_material_tree"), _tree("管-1", "tube")).data
    material_uuid = tree.root_material_uuid
    assert gateway.get_material(material_uuid) == local.get_material(material_uuid)
    assert [item.material.name for item in gateway.search_materials("管-1")] == ["管-1"]
    assert len(gateway.list_materials(roots_only=True)) == 1
    data = MaterialDataWrite(substances=[MaterialSubstance(name="水", quantity=1, quantity_unit="ml")])
    gateway.put_data(_mutation("put_data"), material_uuid, data)
    assert gateway.get_material(material_uuid).data.substances[0].name == "水"
    gateway.patch_material(_mutation("patch_material"), material_uuid, MaterialPatch(barcode="T-1"))
    lot = gateway.inbound_inventory_lot(
        _mutation("inbound_inventory_lot"),
        InventoryLotInbound(lot_uuid="lot-1", template_uuid=created.template_uuid, unit="count", quantity=5),
    ).data
    assert gateway.get_inventory_lot("lot-1") == lot and gateway.list_inventory_lots()[0].lot_uuid == "lot-1"
    assert gateway.changes(after_sequence=0, limit=500)[0].sequence == 1
    from unilabos.client.materials.core import MaterialsHTTPError

    with pytest.raises(MaterialsHTTPError) as missing:
        gateway.move_material(_mutation("move_material"), MaterialMove(material_uuid="missing"))
    assert missing.value.status_code == 404 and missing.value.business_code == 6000
    with pytest.raises(MaterialsHTTPError):
        gateway.get_material("missing")
