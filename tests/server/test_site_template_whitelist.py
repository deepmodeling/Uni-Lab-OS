"""库位准入：模板白名单（V1 ``site_template_not_allowed`` 6008）与库位成环（6009 / 6004）。

白名单写在库位定义的 ``allowed_resource_template_uuids``（模板 UUID 或模板名），实例化后在 Site
``extra`` 里；所有放入（V1 ``site_placement``、库存命令移动、transfer、工作流搬运预检、入站预留）共用
``MaterialsService._apply_material_move`` 的同一组检查。
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.api_envelope import command_body, error_of, ok_data
from unilabos.client.materials import bind_payload
from unilabos.protocol.materials import InventoryMutation, MaterialMove
from unilabos.server.api.materials import install_materials_api
from unilabos.server.services.materials import MaterialsService

_BOTTLE = {
    "id": "bottle_500ml",
    "display_name": "500mL 试剂瓶",
    "registry_type": "resource",
    "class": {"module": "pylabrobot.resources:Container", "type": "pylabrobot"},
    "category": ["container"],
}
_TUBE = {**_BOTTLE, "id": "tube_15ml", "display_name": "15mL 离心管"}
_RACK = {
    "id": "bottle_rack",
    "display_name": "试剂瓶架",
    "registry_type": "resource",
    "class": {"module": "pylabrobot.resources:Resource", "type": "pylabrobot"},
    "category": ["rack"],
    "available_sites": [
        {"label": "A1", "allowed_resource_template_uuids": ["bottle_500ml"]},
        {"label": "A2"},
    ],
}
_OPEN_RACK = {**_RACK, "id": "open_rack", "available_sites": [{"label": "B1"}]}


@pytest.fixture()
def stack(tmp_path):
    service = MaterialsService(tmp_path / "materials.db")
    app = FastAPI()
    install_materials_api(app, service)
    with TestClient(app) as client:
        yield service, client
    service.close()


def _templates(client) -> dict[str, str]:
    receipt = ok_data(client.post("/api/v1/resource-templates", json={"resources": [_BOTTLE, _TUBE, _RACK, _OPEN_RACK]}))
    return {item["name"]: item["uuid"] for item in receipt["templates"]}


def _create(client, template_uuid: str, name: str, site_uuid: str | None = None) -> dict:
    body = {"resource_template_uuid": template_uuid, "name": name}
    if site_uuid:
        body["site_placement"] = {"action": "place", "site_uuid": site_uuid}
    return ok_data(client.post("/api/v1/materials", json=body), status=201)


def _place(client, material_uuid: str, site_uuid: str):
    return client.put(f"/api/v1/materials/{material_uuid}", json={"site_placement": {"action": "place", "site_uuid": site_uuid}})


def _move_command(client, material_uuid: str, site_uuid: str) -> dict:
    mutation = InventoryMutation(command_uuid=str(uuid4()), effect_key="move_material", operation="move_material",
                                 actor_uuid="operator-1")
    body = bind_payload(mutation, MaterialMove(material_uuid=material_uuid, destination_site_uuid=site_uuid))
    return command_body(client.post("/api/v1/inventory/commands", json=body.model_dump(mode="json", exclude_none=False)))


def test_site_template_whitelist_rejects_other_templates_on_every_placement(stack) -> None:
    _service, client = stack
    identities = _templates(client)
    rack = _create(client, identities["bottle_rack"], "瓶架-1")
    a1, a2 = rack["site_uuids"]
    site = ok_data(client.get(f"/api/v1/sites/{a1}"))
    assert site["allowed_resource_template_uuids"] == ["bottle_500ml"]
    assert ok_data(client.get(f"/api/v1/sites/{a2}"))["allowed_resource_template_uuids"] == []

    # 新建并放入：在建物料之前就拒绝，不留半成品
    error_of(client.post("/api/v1/materials", json={
        "resource_template_uuid": identities["tube_15ml"], "name": "管-1",
        "site_placement": {"action": "place", "site_uuid": a1}}), 6008)
    assert ok_data(client.get("/api/v1/materials", params={"name": "管-1"}))["total"] == 0

    tube = _create(client, identities["tube_15ml"], "管-2")
    error_of(_place(client, tube["uuid"], a1), 6008)
    rejected = _move_command(client, tube["uuid"], a1)
    assert rejected["status"] == "rejected" and rejected["code"] == 6008, rejected
    assert rejected["error_code"] == "conflict:site_template" and "bottle_500ml" in rejected["error"]
    ok_data(_place(client, tube["uuid"], a2))

    # 白名单里的模板（按模板名或 UUID 匹配）照常放入
    bottle = _create(client, identities["bottle_500ml"], "瓶-1", site_uuid=a1)
    assert bottle["current_site"]["uuid"] == a1


def test_site_whitelist_by_template_uuid(stack) -> None:
    service, client = stack
    identities = _templates(client)
    rack = _create(client, identities["open_rack"], "开放架")
    (b1,) = rack["site_uuids"]
    record = service.get_site(b1)
    with service.write():
        service.connection.execute(
            "UPDATE site SET extra_json=? WHERE site_uuid=?",
            ('{"allowed_resource_template_uuids": ["%s"]}' % identities["bottle_500ml"].upper(), record.site_uuid),
        )
    tube = _create(client, identities["tube_15ml"], "管")
    error_of(_place(client, tube["uuid"], b1), 6008)
    bottle = _create(client, identities["bottle_500ml"], "瓶")
    ok_data(_place(client, bottle["uuid"], b1))


def test_site_cycles_report_v1_codes(stack) -> None:
    _service, client = stack
    identities = _templates(client)
    outer = _create(client, identities["open_rack"], "外架")
    inner = _create(client, identities["open_rack"], "内架", site_uuid=outer["site_uuids"][0])
    # 物料占用自己的库位：6009（原来经触发器报 4002）
    error_of(_place(client, outer["uuid"], outer["site_uuids"][0]), 6009)
    rejected = _move_command(client, inner["uuid"], inner["site_uuids"][0])
    assert rejected["status"] == "rejected" and rejected["code"] == 6009, rejected
    # 放进自己后代的库位：父子关系成环 6004
    error_of(_place(client, outer["uuid"], inner["site_uuids"][0]), 6004)
    rejected = _move_command(client, outer["uuid"], inner["site_uuids"][0])
    assert rejected["status"] == "rejected" and rejected["code"] == 6004, rejected


def test_ingress_reservation_only_picks_sites_that_accept_the_carrier(stack) -> None:
    _service, client = stack
    identities = _templates(client)
    rack = _create(client, identities["bottle_rack"], "入口架")
    a1, a2 = rack["site_uuids"]
    tube = _create(client, identities["tube_15ml"], "管载体")
    bottle = _create(client, identities["bottle_500ml"], "瓶载体")
    request = {"idempotency_key": "agv-tube", "carrier_material_uuid": tube["uuid"], "candidate_site_uuids": [a1],
               "ttl_seconds": 600}
    error_of(client.post("/api/v1/inventory/ingress-reservations", json=request), 6008)
    reserved = ok_data(client.post("/api/v1/inventory/ingress-reservations", json={
        **request, "idempotency_key": "agv-tube-2", "candidate_site_uuids": [a1, a2]}), status=201)
    assert reserved["site_uuid"] == a2
    accepted = ok_data(client.post("/api/v1/inventory/ingress-reservations", json={
        **request, "idempotency_key": "agv-bottle", "carrier_material_uuid": bottle["uuid"]}), status=201)
    assert accepted["site_uuid"] == a1
