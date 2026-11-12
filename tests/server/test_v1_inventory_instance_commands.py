"""V1 库存命令中的实例 / 内容物 / 批次调整类型：``inventory.inbound``（kind=instance）、``material.deploy``、
``material.content.set|clear``、``material.consume``、``material.mark_used``、``material.adjust``。

每类覆盖幂等重放（同 command_id）、版本冲突、预留冲突与数量边界；``material_substance`` 是唯一数量权威。
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.api_envelope import ok_data
from unilabos.client.materials import bind_payload
from unilabos.protocol.materials import (
    InventoryMutation,
    InventoryRequirement,
    InventoryReservationCreate,
    MaterialIdentityWrite,
    MaterialNodeCreate,
    MaterialTreeCreate,
    SiteCreate,
)
from unilabos.server.api.materials import install_materials_api
from unilabos.server.services.materials import MaterialsService

COMMANDS = "/api/v1/inventory/commands"


@pytest.fixture()
def stack(tmp_path):
    service = MaterialsService(tmp_path / "materials.db")
    app = FastAPI()
    install_materials_api(app, service)
    with TestClient(app) as client:
        yield service, client
    service.close()


def _dev(client, operation: str, value) -> dict:
    mutation = InventoryMutation(command_uuid=str(uuid4()), effect_key=operation, operation=operation)
    body = bind_payload(mutation, value).model_dump(mode="json", exclude_none=False)
    result = client.post(COMMANDS, json=body).json()
    assert result["status"] == "completed", result
    return result["result"]["data"]


def _run(client, command_type: str, payload: dict, *, command_id: str | None = None, **extra) -> dict:
    body = {"command_id": command_id or str(uuid4()), "type": command_type, "actor": "operator-1",
            "payload": payload, **extra}
    response = client.post(COMMANDS, json=body)
    assert response.status_code == 200
    return response.json()


def _templates(client) -> dict[str, str]:
    rows = ok_data(client.post("/api/v1/resource-templates", json={"resources": [
        {"id": "bottle", "registry_type": "resource", "category": ["container"]},
        {"id": "rack", "registry_type": "resource"},
    ]}))["templates"]
    return {row["name"]: row["uuid"] for row in rows}


def _rack(client, name: str = "架") -> str:
    tree = MaterialTreeCreate(nodes=[MaterialNodeCreate(
        client_ref="root",
        identity=MaterialIdentityWrite(resource_id=name, name=name, template_name="rack"),
        sites=[SiteCreate(template_name="bottle", site_index=index, label=label)
               for index, label in enumerate(("A1", "A2"))],
    )])
    return _dev(client, "create_material_tree", tree)["root_material_uuid"]


def _instance(client, **payload) -> dict:
    result = _run(client, "inventory.inbound", {"kind": "instance", "template_id": "bottle", **payload})
    assert result["status"] == "completed", result
    return result["result"]


def test_instance_inbound_is_idempotent_and_keeps_local_identity(stack) -> None:
    _service, client = stack
    _templates(client)
    rack = _rack(client)
    edge_uuid = str(uuid4())
    body = {"kind": "instance", "template_id": "bottle", "barcode": "BC-1", "cloud_uuid": "cloud-9",
            "edge_uuid": edge_uuid, "parent_uuid": rack, "slot_id": "A1"}
    first = _run(client, "inventory.inbound", body, command_id="in-1")
    assert first["status"] == "completed" and first["replayed"] is False, first
    result = first["result"]
    assert result["edge_uuid"] == edge_uuid and result["status"] == "active" and result["created"] is True
    assert result["legacy_cloud_id"] == "cloud-9" and result["barcode"] == "BC-1"
    assert result["parent_uuid"] == rack and result["slot_id"] == "A1"
    # 同 command_id 重放首次结果；同 edge_uuid 的新命令按身份幂等，不重复建
    assert _run(client, "inventory.inbound", body, command_id="in-1")["replayed"] is True
    again = _run(client, "inventory.inbound", body)
    assert again["status"] == "completed" and again["result"]["created"] is False
    assert len(ok_data(client.get("/api/v1/inventory/instances", params={"name": "BC-1"}))["instances"]) == 1
    # 条码已被活动实例占用、实例入库带批次字段、云端映射冲突：拒绝
    duplicate = _run(client, "inventory.inbound", {"kind": "instance", "template_id": "bottle", "barcode": "BC-1"})
    assert duplicate["status"] == "rejected" and duplicate["code"] == 6010
    mixed = _run(client, "inventory.inbound", {"kind": "instance", "template_id": "bottle", "quantity": 3})
    assert mixed["status"] == "rejected" and mixed["code"] == 1000
    clash = _run(client, "inventory.inbound", {"kind": "instance", "template_id": "bottle",
                                               "cloud_uuid": "a", "legacy_cloud_id": "b"})
    assert clash["status"] == "rejected" and clash["code"] == 1000
    occupied = _run(client, "inventory.inbound", {"kind": "instance", "template_id": "bottle",
                                                  "parent_uuid": rack, "slot_id": "A1"})
    assert occupied["status"] == "rejected" and occupied["code"] == 6007
    # 拒绝回滚整条命令：没有留下半成品物料
    assert len(ok_data(client.get("/api/v1/inventory/instances"))["instances"]) == 2
    lot_with_instance_field = _run(client, "inventory.inbound", {"template_id": "bottle", "quantity": 1, "barcode": "x"})
    assert lot_with_instance_field["status"] == "rejected" and lot_with_instance_field["code"] == 1000


def test_deploy_then_consume_moves_out_and_is_terminal(stack) -> None:
    _service, client = stack
    _templates(client)
    rack = _rack(client)
    bottle = _instance(client, barcode="BC-2")
    other = _instance(client, barcode="BC-3")
    stale = _run(client, "material.deploy", {"edge_uuid": bottle["edge_uuid"], "parent_uuid": rack, "slot_id": "A1"},
                 expected_version=bottle["version"] + 5)
    assert stale["status"] == "rejected" and stale["code"] == 3003
    missing = _run(client, "material.deploy", {"edge_uuid": bottle["edge_uuid"], "parent_uuid": str(uuid4())})
    assert missing["status"] == "rejected" and missing["code"] == 6003
    deployed = _run(client, "material.deploy", {"edge_uuid": bottle["edge_uuid"], "parent_uuid": rack, "slot_id": "A1"},
                    command_id="deploy-1", expected_version=bottle["version"])
    assert deployed["status"] == "completed", deployed
    assert deployed["result"]["parent_uuid"] == rack and deployed["result"]["slot_id"] == "A1"
    assert deployed["result"]["version"] > bottle["version"]
    blocked = _run(client, "material.deploy", {"edge_uuid": other["edge_uuid"], "parent_uuid": rack, "slot_id": "A1"})
    assert blocked["status"] == "rejected" and blocked["code"] == 6007

    consumed = _run(client, "material.consume", {"edge_uuid": bottle["edge_uuid"]}, command_id="consume-1")
    assert consumed["status"] == "completed", consumed
    result = consumed["result"]
    assert result["status"] == "consumed" and result["parent_uuid"] == "" and result["site_uuid"] is None
    assert result["previous_status"] == "active"
    assert _run(client, "material.consume", {"edge_uuid": bottle["edge_uuid"]}, command_id="consume-1")["replayed"]
    again = _run(client, "material.consume", {"edge_uuid": bottle["edge_uuid"]})
    assert again["status"] == "rejected" and again["code"] == 3003
    redeploy = _run(client, "material.deploy", {"edge_uuid": bottle["edge_uuid"], "parent_uuid": rack, "slot_id": "A2"})
    assert redeploy["status"] == "rejected" and redeploy["code"] == 3003
    # 库位已释放，另一件可以上台
    assert _run(client, "material.deploy", {"edge_uuid": other["edge_uuid"], "parent_uuid": rack,
                                            "slot_id": "A1"})["status"] == "completed"


def test_consume_and_mark_used_respect_reservations(stack) -> None:
    service, client = stack
    _templates(client)
    rack = _rack(client)
    bottle = _instance(client, barcode="BC-4", parent_uuid=rack, slot_id="A1")
    reserved = _instance(client, barcode="BC-5")
    # 作业库存预留（物料型）占用：消耗与标为已使用都拒绝（6011）
    service.reserve_inventory(
        InventoryMutation(command_uuid=str(uuid4()), effect_key="reserve", operation="reserve_inventory",
                          job_uuid="job-1"),
        InventoryReservationCreate(task_uuid="task-1", node_uuid="node-1", job_uuid="job-1", scheduler_revision=1,
                                   requirements=[InventoryRequirement(key="k", kind="material",
                                                                      material_uuid=reserved["edge_uuid"])]),
    )
    for command_type in ("material.consume", "material.mark_used"):
        refused = _run(client, command_type, {"edge_uuid": reserved["edge_uuid"]})
        assert refused["status"] == "rejected" and refused["code"] == 6011, refused

    # 内容物数量预留占用同样拒绝
    content = _run(client, "material.content.set", {"edge_uuid": bottle["edge_uuid"], "state": {
        "substances": [{"name": "水", "quantity": 100, "quantity_unit": "ml"}]}})
    assert content["status"] == "completed", content
    substance_uuid = service.get_material(bottle["edge_uuid"]).data.substances[0].substance_uuid
    reservation = _run(client, "content.reserve", {"substance_uuid": substance_uuid, "task_uuid": "task-2",
                                                   "quantity": 10, "quantity_unit": "ml"})
    assert reservation["status"] == "completed"
    assert _run(client, "material.mark_used", {"edge_uuid": bottle["edge_uuid"]})["code"] == 6011
    assert _run(client, "content.release", {"reservation_uuid": reservation["result"]["reservation_uuid"]})["status"] == "completed"

    used = _run(client, "material.mark_used", {"edge_uuid": bottle["edge_uuid"], "reason": "已加样"})
    assert used["status"] == "completed", used
    assert used["result"]["status"] == "used" and used["result"]["previous_status"] == "active"
    # 标为已使用保留库位与内容物
    assert used["result"]["parent_uuid"] == rack and used["result"]["slot_id"] == "A1"
    assert service.get_material(bottle["edge_uuid"]).data.substances[0].quantity == 100
    repeat = _run(client, "material.mark_used", {"edge_uuid": bottle["edge_uuid"]})
    assert repeat["status"] == "completed" and repeat["result"]["previous_status"] == "used"
    # 已使用的实例仍可最终消耗
    assert _run(client, "material.consume", {"edge_uuid": bottle["edge_uuid"]})["result"]["previous_status"] == "used"


def test_content_set_and_clear_are_versioned_and_keep_reserved_quantity(stack) -> None:
    service, client = stack
    _templates(client)
    bottle = _instance(client, barcode="BC-6")
    first = _run(client, "material.content.set", {"edge_uuid": bottle["edge_uuid"],
                                                  "state": {"substance": "water", "volume_ml": 5}},
                 command_id="content-1", expected_version=0)
    assert first["status"] == "completed", first
    version = first["result"]["version"]
    assert first["result"]["state"] == {"substance": "water", "volume_ml": 5}
    assert service.get_material(bottle["edge_uuid"]).data.data == {"substance": "water", "volume_ml": 5}
    assert _run(client, "material.content.set", {"edge_uuid": bottle["edge_uuid"],
                                                 "state": {"substance": "water", "volume_ml": 5}},
                command_id="content-1", expected_version=0)["replayed"] is True
    stale = _run(client, "material.content.set", {"edge_uuid": bottle["edge_uuid"], "state": {}}, expected_version=0)
    assert stale["status"] == "rejected" and stale["code"] == 3003
    filled = _run(client, "material.content.set", {"edge_uuid": bottle["edge_uuid"], "state": {
        "note": "加液", "substances": [{"name": "乙醇", "quantity": 100, "quantity_unit": "ml"}]}},
        expected_version=version)
    assert filled["status"] == "completed" and filled["result"]["version"] == version + 1
    data = service.get_material(bottle["edge_uuid"]).data
    assert data.data == {"note": "加液"} and [item.quantity for item in data.substances] == [100]
    substance = data.substances[0]
    reserve = _run(client, "content.reserve", {"substance_uuid": substance.substance_uuid, "task_uuid": "task-9",
                                               "quantity": 60, "quantity_unit": "ml"})
    assert reserve["status"] == "completed"
    below = _run(client, "material.content.set", {"edge_uuid": bottle["edge_uuid"], "state": {"substances": [
        {"substance_uuid": substance.substance_uuid, "name": "乙醇", "quantity": 50, "quantity_unit": "ml"}]}})
    assert below["status"] == "rejected" and below["code"] == 3003
    removed = _run(client, "material.content.set", {"edge_uuid": bottle["edge_uuid"], "state": {"substances": []}})
    assert removed["status"] == "rejected" and removed["code"] == 6011
    assert _run(client, "material.content.clear", {"edge_uuid": bottle["edge_uuid"]})["code"] == 6011
    kept = _run(client, "material.content.set", {"edge_uuid": bottle["edge_uuid"], "state": {"substances": [
        {"substance_uuid": substance.substance_uuid, "name": "乙醇", "quantity": 0.06, "quantity_unit": "l"}]}})
    assert kept["status"] == "completed", kept
    _run(client, "content.release", {"reservation_uuid": reserve["result"]["reservation_uuid"]})
    current = service.get_material(bottle["edge_uuid"]).data.content_version
    cleared = _run(client, "material.content.clear", {"edge_uuid": bottle["edge_uuid"]}, expected_version=current)
    assert cleared["status"] == "completed" and cleared["result"] == {
        "instance_uuid": bottle["edge_uuid"], "edge_uuid": bottle["edge_uuid"], "version": current + 1, "state": {}}
    data = service.get_material(bottle["edge_uuid"]).data
    assert data.data == {} and data.substances == []
    bad = _run(client, "material.content.set", {"edge_uuid": bottle["edge_uuid"], "state": {"substances": "水"}})
    assert bad["status"] == "rejected" and bad["code"] == 1000


def test_adjust_lot_requires_audit_and_keeps_reserved_quantity(stack) -> None:
    service, client = stack
    _templates(client)
    assert _run(client, "inventory.inbound", {"template_id": "bottle", "lot_id": "lot-1", "quantity": 500,
                                              "unit": "ml"})["status"] == "completed"
    reserved = _run(client, "inventory.reserve", {"workflow_id": "wf-1", "node_requirements": {
        "node-1": [{"lot_id": "lot-1", "quantity": 100, "unit": "ml"}]}})
    assert reserved["status"] == "completed", reserved
    for payload, actor in (({"lot_id": "lot-1", "new_total": 400}, "operator-1"),
                           ({"lot_id": "lot-1", "new_total": 400, "reason": "盘点"}, "")):
        refused = client.post(COMMANDS, json={"command_id": str(uuid4()), "type": "material.adjust", "actor": actor,
                                              "payload": payload}).json()
        assert refused["status"] == "rejected" and refused["code"] == 1000
    below = _run(client, "material.adjust", {"lot_id": "lot-1", "new_total": 50, "reason": "盘点"})
    assert below["status"] == "rejected" and below["code"] in {3003, 4002}
    negative = _run(client, "material.adjust", {"lot_id": "lot-1", "new_total": -1, "reason": "盘点"})
    assert negative["status"] == "rejected" and negative["code"] == 1000
    lot = service.get_inventory_lot("lot-1")
    stale = _run(client, "material.adjust", {"lot_id": "lot-1", "new_total": 400, "reason": "盘点"},
                 expected_version=lot.version + 1)
    assert stale["status"] == "rejected" and stale["code"] == 3003
    adjusted = _run(client, "material.adjust", {"lot_id": "lot-1", "new_total": 400, "reason": "盘点"},
                    command_id="adjust-1", expected_version=lot.version)
    assert adjusted["status"] == "completed", adjusted
    assert adjusted["result"]["quantity_total"] == 400 and adjusted["result"]["delta"] == -100
    after = service.get_inventory_lot("lot-1")
    assert (after.quantity_total, after.quantity_available, after.quantity_reserved) == (400, 300, 100)
    replay = _run(client, "material.adjust", {"lot_id": "lot-1", "new_total": 400, "reason": "盘点"},
                  command_id="adjust-1", expected_version=lot.version)
    assert replay["replayed"] is True and service.get_inventory_lot("lot-1").version == after.version
