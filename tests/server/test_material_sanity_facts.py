"""派发前物料健全性检查的物料权威只读事实（``materials/sanity.py``）：本地与 HTTP 网关读出同一份。"""
from __future__ import annotations

from uuid import uuid4

from tests.server.test_material_execution import fill
from tests.server.test_scheduler_inventory_authority import _inbound, _material, _mutation, _template
from tests.server.test_workflow_inventory_recovery import Stack
from unilabos.protocol.materials import (
    InventoryRequirement,
    InventoryReservationCreate,
    MaterialDelete,
)


def _facts_fixture(stack):
    _template(stack.materials, "container-template", "container")
    _template(stack.materials, "reagent-template", "reagent")
    plate, gone, bottle = (_material(stack.materials, "container") for _ in range(3))
    stack.materials.delete_material(_mutation("delete_material"), MaterialDelete(material_uuid=gone))
    fill(stack.materials, bottle, [("water", 100)])
    _inbound(stack.materials, lot_uuid="lot-a", quantity=20)
    job = str(uuid4())
    reservation = stack.materials.reserve_inventory(_mutation("reserve_inventory", job_uuid=job), InventoryReservationCreate(
        task_uuid="task-1", node_uuid="node-1", job_uuid=job, scheduler_revision=1, requirements=[
            InventoryRequirement(key="plate", kind="material", material_uuid=plate),
            InventoryRequirement(key="solvent", kind="lot", lot_uuid="lot-a", quantity=0.01, unit="ml"),
            InventoryRequirement(key="buffer", kind="content", material_uuid=bottle, substance_name="water",
                                 quantity=30, unit="ul"),
        ])).data
    return plate, gone, bottle, job, reservation


def test_facts_cover_lifecycle_claims_and_the_jobs_reservation_in_one_read(tmp_path):
    stack = Stack(tmp_path)
    try:
        plate, gone, bottle, job, reservation = _facts_fixture(stack)
        missing = str(uuid4())
        facts = stack.client.material_sanity_facts({"material_uuids": [plate, gone, bottle, missing],
                                                    "reservation_uuid": reservation.reservation_uuid})
        materials = facts["materials"]
        assert (materials[plate]["lifecycle_status"], materials[plate]["deleted"]) == ("reserved", False)
        assert materials[plate]["reservations"] == [{"reservation_uuid": reservation.reservation_uuid, "job_uuid": job,
                                                      "task_uuid": "task-1", "status": "active"}]
        assert materials[gone]["deleted"] is True and materials[gone]["exists"] is True
        assert materials[bottle]["reservations"] == []  # 内容物预留不改变容器的生命周期
        assert materials[missing] == {"material_uuid": missing, "exists": False}
        current = {item["key"]: item["current"] for item in facts["reservation"]["items"]}
        assert facts["reservation"]["status"] == "active" and facts["reservation"]["job_uuid"] == job
        assert current["plate"] == {}
        assert (current["solvent"]["exists"], current["solvent"]["quantity"], current["solvent"]["unit_convertible"]) == (
            True, 20.0, True)  # ml → 批次单位 ul 可换算
        assert (current["buffer"]["material_uuid"], current["buffer"]["reservation_status"],
                current["buffer"]["unit_convertible"]) == (bottle, "active", True)
        assert stack.client.material_sanity_facts({"material_uuids": [], "reservation_uuid": "nope"})["reservation"] == {
            "reservation_uuid": "nope", "exists": False}
    finally:
        stack.close()


def test_http_gateway_reads_the_same_facts_as_the_local_one(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from tests.server.test_v1_inventory_api import _TestClientOpener
    from unilabos.client.materials import HTTPMaterialsClient
    from unilabos.server.api.materials import install_materials_api

    stack = Stack(tmp_path)
    plate, gone, _bottle, _job, reservation = _facts_fixture(stack)
    missing = str(uuid4())
    app = FastAPI()
    install_materials_api(app, stack.materials)
    try:
        with TestClient(app) as client:
            monkeypatch.setattr("unilabos.client.materials.core.urlopen", _TestClientOpener(client))
            payload = {"material_uuids": [plate, gone, missing], "reservation_uuid": reservation.reservation_uuid}
            facts = HTTPMaterialsClient("http://testserver").material_sanity_facts(payload)
            assert facts == stack.client.material_sanity_facts(payload)
            materials = facts["materials"]
            assert materials[plate]["lifecycle_status"] == "reserved" and materials[gone]["deleted"] is True
            assert materials[missing] == {"material_uuid": missing, "exists": False}
            assert facts["reservation"]["reservation_uuid"] == reservation.reservation_uuid
    finally:
        stack.close()
