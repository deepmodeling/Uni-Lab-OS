"""PLR 创建必须往返 materials authority，并保留全部 substances。"""

from uuid import uuid4

import pytest

from unilabos.resources.container import RegularContainer
from unilabos.resources.itemized_carrier import ItemizedCarrier
from unilabos.server.adapters.plr_materials import (
    create_plr_materials,
    plr_resources_to_create,
)
from unilabos.client.materials import LocalMaterialsClient
from unilabos.server.protocol.common import InventoryMutation
from unilabos.server.protocol.materials import ResourceTemplateWrite
from unilabos.server.services.materials import MaterialsService


def _mutation(operation: str) -> InventoryMutation:
    return InventoryMutation(
        command_uuid=str(uuid4()), effect_key=operation, operation=operation
    )


def test_plr_create_returns_server_uuid_and_all_substances(tmp_path) -> None:
    service = MaterialsService(tmp_path / "materials.db")
    client = LocalMaterialsClient(service)
    client.put_template(
        _mutation("put_template"),
        ResourceTemplateWrite(
            template_uuid="beaker-template",
            name="beaker",
            display_name="Beaker",
            resource_type="container",
            class_name="RegularContainer",
        ),
    )
    beaker = RegularContainer(
        name="beaker-1",
        size_x=10,
        size_y=10,
        size_z=20,
        max_volume=100,
    )
    beaker.unilabos_extra = {"unilabos_resource_class": "beaker"}
    beaker.tracker.add_liquid("water", 20, unit="ul")
    beaker.tracker.add_liquid("NaCl", 5, unit="ug")
    try:
        created = create_plr_materials(
            client,
            _mutation("create_material_tree"),
            [beaker],
        )
        authoritative_uuid = created.result.data.root_material_uuid

        assert not getattr(beaker, "unilabos_uuid", "")
        assert created.result.data.client_ref_map == {
            "node-0": authoritative_uuid
        }
        assert created.tree.root_nodes[0].res_content.uuid == authoritative_uuid
        assert (
            created.result.data.nodes[0].material.template_uuid
            == "beaker-template"
        )
        assert created.result.data.nodes[0].data.substances[1].physical_state == "solid"
        assert created.resources[0].tracker.substances == [
            ("water", 20.0, "ul"),
            ("NaCl", 5.0, "ug"),
        ]
        assert created.resources[0].unilabos_uuid == authoritative_uuid
    finally:
        service.close()


def test_plr_create_request_contains_refs_but_no_instance_uuids() -> None:
    carrier = ItemizedCarrier(
        name="carrier-1",
        size_x=100,
        size_y=80,
        size_z=20,
        sites={0: None},
        model="carrier",
    )

    request = plr_resources_to_create([carrier])
    payload = request.model_dump(mode="json")

    assert payload["nodes"][0]["client_ref"] == "node-0"
    assert "known_random_uuid" not in payload
    assert "material_uuid" not in payload["nodes"][0]["identity"]
    assert "template_uuid" not in payload["nodes"][0]["identity"]
    assert "site_uuid" not in payload["nodes"][0]["sites"][0]
    assert "occupied_material_uuid" not in payload["nodes"][0]["sites"][0]
    assert not getattr(carrier, "unilabos_uuid", "")
    assert carrier.resource_sites is None


def test_plr_create_rejects_an_existing_authoritative_resource() -> None:
    beaker = RegularContainer(
        name="beaker-1",
        size_x=10,
        size_y=10,
        size_z=20,
        max_volume=100,
    )
    beaker.unilabos_uuid = str(uuid4())
    beaker.unilabos_extra = {"unilabos_resource_class": "beaker"}

    with pytest.raises(ValueError, match="已有 UUID"):
        plr_resources_to_create([beaker])
