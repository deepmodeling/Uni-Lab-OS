"""库存增量接线：真实事务、只读预检、混合单位、容量和盘点。"""
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.api_envelope import error_of, ok_data
from unilabos.protocol.materials import (
    InventoryMutation, InventoryLotInbound, InventoryRequirement, InventoryReservationCreate,
    InventoryReservationTransition, InventoryStocktake, MaterialIdentityWrite, MaterialNodeCreate,
    MaterialTreeCreate, MaterialDataWrite, MaterialSubstance, MaterialPatch, ResourceTemplateWrite,
)
from unilabos.server.services.materials import MaterialsService, MaterialConflictError, MaterialValidationError
from unilabos.server.api.materials import install_materials_api


def mutation(operation, **kwargs):
    return InventoryMutation(command_uuid=str(uuid4()), effect_key=operation, operation=operation, **kwargs)


@pytest.fixture
def service(tmp_path):
    with MaterialsService(tmp_path / "materials.db") as service:
        service.put_template(mutation("put_template"), ResourceTemplateWrite(
            template_uuid="stock", name="stock", resource_type="container", class_name="Container",
        ))
        yield service


def inbound(service, identity, quantity, unit):
    return service.inbound_inventory_lot(mutation("inbound_inventory_lot"), InventoryLotInbound(
        lot_uuid=identity, template_uuid="stock", batch_no=identity, quantity=quantity, unit=unit,
    )).data


def reserve(service, requirements):
    return service.reserve_inventory(mutation("reserve_inventory"), InventoryReservationCreate(
        task_uuid="task", node_uuid="node", job_uuid=str(uuid4()), scheduler_revision=1, requirements=requirements,
    )).data


def test_preview_uses_shared_virtual_balance_and_does_not_write(service):
    inbound(service, "a", 1, "mL")
    inbound(service, "b", 500, "ul")
    requirements = [InventoryRequirement(key=key, kind="lot", template_uuid="stock", quantity=1, unit="ml") for key in ("first", "second")]
    before = service.connection.execute("SELECT total_changes()").fetchone()[0]
    result = service.preview_inventory(requirements)
    assert not result["available"]
    assert result["shortages"][0]["key"] == "second"
    assert result["shortages"][0]["shortage"] == pytest.approx(.5)
    assert service.connection.execute("SELECT total_changes()").fetchone()[0] == before
    assert service.get_inventory_lot("a").quantity_available == 1
    assert service.list_inventory_reservations() == []


def test_mixed_unit_allocation_consumes_and_releases_in_original_units(service):
    inbound(service, "a", 1, "mL")
    inbound(service, "b", 500, "ul")
    reservation = reserve(service, [InventoryRequirement(key="dose", kind="lot", template_uuid="stock", quantity=1250, unit="µL")])
    assert [(item.quantity, item.unit) for item in reservation.items] == [(1, "mL"), (250, "ul")]
    service.release_inventory_reservation(mutation("release_inventory_reservation"), InventoryReservationTransition(reservation_uuid=reservation.reservation_uuid))
    assert service.get_inventory_lot("b").quantity_available == 500
    reservation = reserve(service, [InventoryRequirement(key="dose", kind="lot", template_uuid="stock", quantity=.001, unit="l")])
    service.consume_inventory_reservation(mutation("consume_inventory_reservation"), InventoryReservationTransition(reservation_uuid=reservation.reservation_uuid))
    assert service.get_inventory_lot("a").quantity_total == 0


def test_micro_quantity_is_not_rounded_to_zero(service):
    inbound(service, "a", 1e-12, "kg")
    reservation = reserve(service, [InventoryRequirement(key="dose", kind="lot", lot_uuid="a", quantity=5e-10, unit="g")])
    assert service.get_inventory_lot("a").quantity_available == pytest.approx(5e-13, abs=1e-25)
    service.consume_inventory_reservation(mutation("consume_inventory_reservation"), InventoryReservationTransition(reservation_uuid=reservation.reservation_uuid))
    assert service.get_inventory_lot("a").quantity_total == pytest.approx(5e-13, abs=1e-25)


def test_stocktake_is_versioned_audited_idempotent_and_refuses_reserved_difference(service):
    lot = inbound(service, "a", 10, "count")
    request = InventoryStocktake(lot_uuid="a", expected_version=lot.version, measured_count=8, reason="人工核对")
    command = mutation("stocktake_inventory_lot", actor_type="operator", actor_uuid="operator-1")
    first = service.stocktake_inventory_lot(command, request)
    assert first.data.quantity_total == 8
    assert service.stocktake_inventory_lot(command, request).replayed
    with pytest.raises(MaterialConflictError, match="版本"):
        service.stocktake_inventory_lot(mutation("stocktake_inventory_lot", actor_uuid="operator-1"), request)
    reserve(service, [InventoryRequirement(key="tips", kind="lot", lot_uuid="a", quantity=2, unit="count")])
    request = request.model_copy(update={"expected_version": service.get_inventory_lot("a").version, "measured_count": 7})
    with pytest.raises(MaterialConflictError, match="stocktake_review_required"):
        service.stocktake_inventory_lot(mutation("stocktake_inventory_lot", actor_uuid="operator-1"), request)
    assert service.get_inventory_lot("a").quantity_total == 8


def test_capacity_sums_substances_and_rolls_back_all_write_paths(service):
    result = service.create_tree(mutation("create_material_tree"), MaterialTreeCreate(nodes=[MaterialNodeCreate(
        client_ref="vial", identity=MaterialIdentityWrite(
            resource_id="vial", name="vial", resource_type="container", class_name="Container", template_name="stock",
            config={"capacity": {"max_volume_ul": 1000}},
        ),
    )]))
    identity = result.data.nodes[0].material.material_uuid
    def contents(a, b):
        return MaterialDataWrite(substances=[MaterialSubstance(name="a", quantity=a, quantity_unit="mL"), MaterialSubstance(name="b", quantity=b, quantity_unit="ul")])
    service.put_data(mutation("put_data"), identity, contents(.5, 500))
    with pytest.raises(MaterialValidationError, match="超过"):
        service.put_data(mutation("put_data"), identity, contents(.6, 500))
    assert service.get_material(identity).data.substances[0].quantity == .5
    with pytest.raises(MaterialValidationError, match="超过"):
        service.patch_material(mutation("patch_material"), identity, MaterialPatch(config={"capacity": {"max_volume_ul": 900}}))
    assert service.get_material(identity).material.config["capacity"]["max_volume_ul"] == 1000


def test_inventory_preview_http_reports_all_shortages_without_mutation(service):
    app = FastAPI()
    install_materials_api(app, service)
    before = service.connection.execute("SELECT total_changes()").fetchone()[0]
    response = TestClient(app).post("/api/v1/materials/inventory/preview", json={"requirements": [
        {"key": "one", "kind": "lot", "template_uuid": "stock", "quantity": 1, "unit": "g"},
        {"key": "two", "kind": "lot", "template_uuid": "stock", "quantity": 2, "unit": "g"},
    ]})
    assert response.status_code == 200, response.text
    assert len(ok_data(response)["shortages"]) == 2
    assert service.connection.execute("SELECT total_changes()").fetchone()[0] == before


def test_template_capacity_edit_cannot_bypass_existing_content(service):
    result = service.create_tree(mutation("create_material_tree"), MaterialTreeCreate(nodes=[MaterialNodeCreate(
        client_ref="vial", identity=MaterialIdentityWrite(
            resource_id="rated-vial", name="rated-vial", resource_type="container", class_name="Container", template_name="stock",
        ), data=MaterialDataWrite(substances=[MaterialSubstance(name="water", quantity=1, quantity_unit="ml")]),
    )]))
    identity = result.data.nodes[0].material.material_uuid
    with pytest.raises(MaterialValidationError, match="超过"):
        service.put_template(mutation("put_template"), ResourceTemplateWrite(
            template_uuid="stock", name="stock", resource_type="container", class_name="Container",
            definition={"capacity": {"max_volume_ul": 500}},
        ))
    assert service.get_template("stock").definition == {}
    assert service.get_material(identity).data.substances[0].quantity == 1


@pytest.mark.parametrize("concentration", [0, 0.5])
def test_capacity_never_uses_pure_density_for_declared_concentration(concentration):
    from types import SimpleNamespace
    from unilabos.server.services.materials.capacity import validate_contents

    substance = SimpleNamespace(name="液体", quantity=1, quantity_unit="g", physical_state="liquid",
                                chemical_uuid="water", meta_data_json={}, composition=[])
    validate_contents({"max_volume_ul": 1000}, [substance], {"water": {"density_g_per_ml": 1}})
    substance.meta_data_json["concentration_value"] = concentration
    with pytest.raises(ValueError, match="无法.*验证容量"):
        validate_contents({"max_volume_ul": 1000}, [substance], {"water": {"density_g_per_ml": 1}})
