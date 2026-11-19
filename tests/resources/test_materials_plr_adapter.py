"""PLR 创建必须往返 materials authority，并保留全部 substances。"""

from uuid import uuid4

import pytest

from unilabos.resources.presets.container import RegularContainer
from unilabos.resources.presets.itemized_carrier import ItemizedCarrier
from unilabos.resources import materials
from unilabos.resources.adapters.plr_materials import (
    create_plr_materials,
    plr_resources_to_create,
)
from unilabos.client.materials import LocalMaterialsClient
from unilabos.protocol.materials import InventoryMutation
from unilabos.protocol.materials import ResourceTemplateWrite
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
        assert created.result.data.nodes[0].sites == []
        assert created.result.data.nodes[0].data.sites_initialized is True
        assert created.tree.root_nodes[0].res_content.sites_initialized is True
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


def test_materials_create_unwraps_the_single_authoritative_root(tmp_path) -> None:
    service = MaterialsService(tmp_path / "materials.db")
    client = LocalMaterialsClient(service)
    draft = RegularContainer(
        name="single-root",
        size_x=10,
        size_y=10,
        size_z=20,
        max_volume=100,
    )
    draft.unilabos_extra = {
        "unilabos_resource_class": "single-root-template"
    }
    try:
        authoritative = materials.create(draft, gateway=client)

        assert isinstance(authoritative, RegularContainer)
        assert authoritative.name == "single-root"
        assert authoritative.unilabos_uuid
        assert not getattr(draft, "unilabos_uuid", "")
    finally:
        service.close()


def test_materials_create_can_return_the_complete_receipt(tmp_path) -> None:
    service = MaterialsService(tmp_path / "materials.db")
    client = LocalMaterialsClient(service)
    draft = RegularContainer(
        name="receipt-root",
        size_x=10,
        size_y=10,
        size_z=20,
        max_volume=100,
    )
    draft.unilabos_extra = {
        "unilabos_resource_class": "receipt-root-template"
    }
    try:
        created = materials.create(
            draft,
            gateway=client,
            unwrap_single=False,
        )

        assert len(created.resources) == 1
        assert len(created.tree.trees) == 1
        assert created.result.data.root_material_uuid == (
            created.resources[0].unilabos_uuid
        )
    finally:
        service.close()


def test_materials_create_rejects_multiple_root_inputs() -> None:
    roots = [
        RegularContainer(
            name=f"root-{index}",
            size_x=10,
            size_y=10,
            size_z=20,
            max_volume=100,
        )
        for index in range(2)
    ]

    with pytest.raises(ValueError, match="一次只能创建一个 PLR 根物料"):
        materials.create(roots)


def test_one_plr_class_may_carry_several_categories(tmp_path) -> None:
    """FRAMEWORK_GAPS #28：PLR ``category`` 是实例属性，同一个类可以有多种。

    子节点按类名共用组件模板（``Well``），模板的 resource_type 只是首个实例的默认值；
    物料自己的 resource_type 才是它的类型，不能因为与模板不同就拒绝建树。
    """

    from pylabrobot.resources import Coordinate, Resource, Well

    service = MaterialsService(tmp_path / "materials.db")
    client = LocalMaterialsClient(service)

    def rack(name: str, category: str) -> Resource:
        root = Resource(name, size_x=50, size_y=50, size_z=10, category="tube_rack")
        root.unilabos_extra = {"unilabos_resource_class": "category-rack"}
        root.assign_child_resource(
            Well(f"{name}_A1", size_x=10, size_y=10, size_z=40, category=category),
            Coordinate(1, 1, 0),
        )
        return root

    try:
        wells = materials.create(rack("wells", "well"), gateway=client)
        tubes = materials.create(rack("tubes", "tube"), gateway=client)

        def child_identity(root_uuid: str):
            node = service.get_tree(root_uuid).nodes[1]
            return node.material.template_name, node.material.resource_type

        assert child_identity(wells.unilabos_uuid) == ("Well", "well")
        assert child_identity(tubes.unilabos_uuid) == ("Well", "tube")
        restored = materials.get(tubes.unilabos_uuid, gateway=client).to_plr_resources()[0]
        assert restored.children[0].category == "tube"
    finally:
        service.close()
