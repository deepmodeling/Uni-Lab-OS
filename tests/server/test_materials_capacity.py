"""容量兼容边界：旧体积规格允许记录未知体积，显式容量合同仍严格校验。"""

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from unilabos.protocol.materials import (
    InventoryMutation,
    MaterialDataDelta,
    MaterialDataWrite,
    MaterialDelta,
    MaterialIdentityWrite,
    MaterialNodeCreate,
    MaterialNodeDelta,
    MaterialPatch,
    MaterialSnapshot,
    MaterialSubstance,
    MaterialTreeCreate,
    ResourceTemplateWrite,
)
from unilabos.server.services.materials import MaterialsService, MaterialValidationError
from unilabos.server.services.materials.capacity import validate_contents


def _mutation(operation: str) -> InventoryMutation:
    return InventoryMutation(
        command_uuid=str(uuid4()), effect_key=operation, operation=operation,
    )


@pytest.fixture
def service(tmp_path):
    with MaterialsService(tmp_path / "materials.db") as service:
        service.put_template(_mutation("put_template"), _template({}))
        yield service


def _template(definition: dict[str, Any]) -> ResourceTemplateWrite:
    return ResourceTemplateWrite(
        template_uuid="vial", name="vial", resource_type="container",
        class_name="Container", definition=definition,
    )


def _tree(config: dict[str, Any], substances: list[MaterialSubstance]) -> MaterialTreeCreate:
    return MaterialTreeCreate(nodes=[MaterialNodeCreate(
        client_ref="vial",
        identity=MaterialIdentityWrite(
            resource_id="vial", name="vial", resource_type="container",
            class_name="Container", template_name="vial", config=config,
        ),
        data=MaterialDataWrite(substances=substances),
    )])


def _write_request(
    service: MaterialsService,
    route: str,
    config: dict[str, Any],
    substances: list[MaterialSubstance],
) -> tuple[InventoryMutation, Callable[[], Any]]:
    """构造固定请求以验证同一命令重放，写入路径仍使用真实服务事务。"""
    if route == "create":
        command = _mutation("create_material_tree")
        body = _tree(config, substances)
        return command, lambda: service.create_tree(command, body)

    node = service.create_tree(
        _mutation("create_material_tree"), _tree(config, []),
    ).data.nodes[0]
    identity = node.material.material_uuid
    if route == "put":
        command = _mutation("put_data")
        body = MaterialDataWrite(substances=substances)
        return command, lambda: service.put_data(command, identity, body)
    if route == "snapshot":
        command = _mutation("apply_material_snapshot")
        observed = node.model_copy(update={
            "data": node.data.model_copy(update={"substances": substances}),
        })
        body = MaterialSnapshot(root_material_uuid=identity, nodes=[observed])
        return command, lambda: service.apply_snapshot(command, body)
    assert route == "delta"
    command = _mutation("apply_material_delta")
    body = MaterialDelta(root_material_uuid=identity, nodes=[MaterialNodeDelta(
        material_uuid=identity, expected_version=node.material.version,
        data=MaterialDataDelta(substances=substances),
    )])
    return command, lambda: service.apply_delta(command, body)


@pytest.mark.parametrize("route", ["create", "put", "snapshot", "delta"])
@pytest.mark.parametrize("capacity_source", ["legacy", "instance", "template"])
def test_mass_facts_are_compatible_only_with_legacy_volume(service, route, capacity_source):
    config = {"max_volume": 100}
    if capacity_source == "instance":
        config["capacity"] = {"max_volume_ul": 100}
    elif capacity_source == "template":
        service.put_template(_mutation("put_template"), _template({
            "capacity": {"max_volume_ul": 100},
        }))
    substances = [
        MaterialSubstance(name="NaCl", quantity=250, quantity_unit="ug", physical_state="solid"),
        MaterialSubstance(name="solvent", quantity=.06, quantity_unit="mL"),
        MaterialSubstance(name="water", quantity=40, quantity_unit="ul"),
    ]
    command, write = _write_request(service, route, config, substances)
    if capacity_source != "legacy":
        before = service.list_materials()
        with pytest.raises(MaterialValidationError, match="无法.*验证容量"):
            write()
        assert service.list_materials() == before
        assert service.get_effect(command.command_uuid, command.effect_key).status == "rejected"
        return

    write()
    assert write().replayed
    data = service.list_materials()[0].data
    assert [(item.name, item.quantity, item.quantity_unit) for item in data.substances] == [
        ("NaCl", 250, "ug"), ("solvent", .06, "mL"), ("water", 40, "ul"),
    ]


@pytest.mark.parametrize("route", ["create", "put", "snapshot", "delta"])
def test_unknown_volume_cannot_hide_known_overflow_and_rejection_replays(service, route):
    substances = [
        MaterialSubstance(name="NaCl", quantity=250, quantity_unit="ug", physical_state="solid"),
        MaterialSubstance(name="solvent", quantity=.06, quantity_unit="mL"),
        MaterialSubstance(name="water", quantity=41, quantity_unit="ul"),
    ]
    command, write = _write_request(service, route, {"max_volume": 100}, substances)
    before = service.list_materials()
    ledger = service.changes()
    with pytest.raises(MaterialValidationError, match="超过上限 100"):
        write()
    effect = service.get_effect(command.command_uuid, command.effect_key)
    assert effect.status == "rejected"
    with pytest.raises(MaterialValidationError, match="超过上限 100"):
        write()
    assert service.get_effect(command.command_uuid, command.effect_key) == effect
    assert service.list_materials() == before
    assert service.changes() == ledger


@pytest.mark.parametrize("capacity_source", ["instance", "template"])
def test_enabling_explicit_volume_validates_existing_mass_facts(service, capacity_source):
    node = service.create_tree(_mutation("create_material_tree"), _tree(
        {"max_volume": 100},
        [MaterialSubstance(name="salt", quantity=1, quantity_unit="g", physical_state="solid")],
    )).data.nodes[0]
    with pytest.raises(MaterialValidationError, match="无法.*验证容量"):
        if capacity_source == "instance":
            service.patch_material(
                _mutation("patch_material"), node.material.material_uuid,
                MaterialPatch(config={"max_volume": 100, "capacity": {"max_volume_ul": 100}}),
            )
        else:
            service.put_template(_mutation("put_template"), _template({
                "capacity": {"max_volume_ul": 100},
            }))
    assert service.get_material(node.material.material_uuid) == node
    assert service.get_template("vial").definition == {}


@pytest.mark.parametrize("quantity,unit,error", [
    (500, "mg", None), (1001, "mg", "超过上限"), (250, "ug", "无法.*验证容量"),
])
def test_legacy_volume_does_not_relax_explicit_mass_capacity(service, quantity, unit, error):
    request = _tree(
        {"max_volume": 100, "capacity": {"max_mass_g": 1}},
        [MaterialSubstance(name="salt", quantity=quantity, quantity_unit=unit, physical_state="solid")],
    )
    if error:
        with pytest.raises(MaterialValidationError, match=error):
            service.create_tree(_mutation("create_material_tree"), request)
        assert service.list_materials() == []
    else:
        result = service.create_tree(_mutation("create_material_tree"), request)
        assert result.data.nodes[0].data.substances[0].quantity == quantity


@pytest.mark.parametrize("quantity,unit,density", [(1e308, "L", None), (1e308, "g", 1e-308)])
def test_legacy_volume_does_not_treat_numeric_overflow_as_unknown(quantity, unit, density):
    substance = SimpleNamespace(
        name="liquid", quantity=quantity, quantity_unit=unit, physical_state="liquid",
        chemical_uuid="liquid", meta_data_json={}, composition=[],
    )
    with pytest.raises(ValueError, match="无法.*验证容量"):
        validate_contents(
            {"max_volume_ul": 100}, [substance], {"liquid": {"density_g_per_ml": density}},
            allow_unquantified_volume=True,
        )


def test_legacy_volume_counts_mass_only_with_trusted_liquid_properties():
    substance = SimpleNamespace(
        name="water", quantity=.101, quantity_unit="g", physical_state="liquid",
        chemical_uuid="water", meta_data_json={}, composition=[],
    )
    chemicals = {"water": {"density_g_per_ml": 1}}
    with pytest.raises(ValueError, match="超过上限"):
        validate_contents({"max_volume_ul": 100}, [substance], chemicals, allow_unquantified_volume=True)
    # 有浓度声明时纯液体密度不再适用，旧规格仅允许保存原始记录。
    substance.meta_data_json["concentration_value"] = .5
    validate_contents({"max_volume_ul": 100}, [substance], chemicals, allow_unquantified_volume=True)
    with pytest.raises(ValueError, match="无法.*验证容量"):
        validate_contents({"max_volume_ul": 100}, [substance], chemicals)
