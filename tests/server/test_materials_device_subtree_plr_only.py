"""设备子树只收 PLR 物料（权威在每一次落位上把关）。

设备启动时按 ``ResourceTreeSet`` 把设备子树整棵反序列化成 PLR 实例，靠每个节点的
``config.type``（PLR 自己的类型名）找类。只有身份的物料（网关手写、模板实例化不了）一旦挂进
设备子树，设备重启就起不来；所以 move / transfer / 结算 / 设备挂载 / 建树即落位都走
``_apply_material_move`` 的同一道检查，在落位时就拒绝。
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from unilabos.client.materials import LocalMaterialsClient
from unilabos.protocol.materials import (
    InventoryMutation,
    MaterialIdentityWrite,
    MaterialMove,
    MaterialNodeCreate,
    MaterialPlacement,
    MaterialTreeCreate,
    ResourceTemplateWrite,
)
from unilabos.resources.adapters.device_site import apply_device_authority_state
from unilabos.resources.resource_tracker import ResourceDict, ResourceDictInstance
from unilabos.server.services.materials import MaterialsService
from unilabos.server.services.materials.core import MaterialValidationError

DEVICE_TEMPLATE = "plr_only_test_device"


def _mutation(operation: str) -> InventoryMutation:
    command = str(uuid4())
    return InventoryMutation(
        command_uuid=command,
        effect_key=f"{operation}:{command}",
        operation=operation,
        actor_type="test",
        actor_uuid="device-subtree-plr-only",
    )


@pytest.fixture
def service(tmp_path):
    materials = MaterialsService(tmp_path / "materials.db")
    materials.put_template(
        _mutation("put_template"),
        ResourceTemplateWrite(name="sample_tpl", display_name="sample", class_name="Resource"),
    )
    try:
        yield materials
    finally:
        materials.close()


@pytest.fixture
def device(service):
    device_uuid = str(uuid4())
    config = ResourceDictInstance(
        ResourceDict.model_validate(
            {
                "id": "plr-only-device",
                "uuid": device_uuid,
                "name": "plr-only-device",
                "type": "device",
                "class": DEVICE_TEMPLATE,
                "template_name": DEVICE_TEMPLATE,
                "config": {},
                "data": {},
                "extra": {},
                "sites": None,
                "sites_initialized": False,
            }
        )
    )
    apply_device_authority_state(
        config,
        {"available_sites": [{"index": 1, "label": "s1"}, {"index": 2, "label": "s2"}]},
        DEVICE_TEMPLATE,
        gateway=LocalMaterialsClient(service),
    )
    return {
        "uuid": device_uuid,
        "sites": {site.label: site.site_uuid for site in service.get_material(device_uuid).sites},
    }


def _node(ref: str, *, plr_type: str | None, parent_ref: str | None = None) -> MaterialNodeCreate:
    return MaterialNodeCreate(
        client_ref=ref,
        parent_client_ref=parent_ref,
        identity=MaterialIdentityWrite(
            resource_id=f"{ref}-{uuid4().hex[:8]}",
            template_name="sample_tpl",
            config={"type": plr_type} if plr_type else {},
        ),
    )


def _create(service, *nodes: MaterialNodeCreate, placement: MaterialPlacement | None = None) -> str:
    created = service.create_tree(
        _mutation("create_material_tree"),
        MaterialTreeCreate(nodes=list(nodes), placement=placement),
    )
    return created.data.root_material_uuid


def _move(service, material_uuid: str, **target) -> None:
    service.move_material(_mutation("move_material"), MaterialMove(material_uuid=material_uuid, **target))


def test_identity_only_material_cannot_be_placed_on_a_device_site(service, device) -> None:
    bare = _create(service, _node("bare", plr_type=None))

    with pytest.raises(MaterialValidationError, match="config.type"):
        _move(service, bare, destination_site_uuid=device["sites"]["s1"])

    assert service.get_material(bare).material.parent_material_uuid is None
    assert all(site.occupied_material_uuid is None for site in service.get_material(device["uuid"]).sites)


def test_identity_only_material_cannot_be_mounted_on_the_device_itself(service, device) -> None:
    bare = _create(service, _node("bare", plr_type=None))

    with pytest.raises(MaterialValidationError, match="只收 PLR 物料"):
        _move(service, bare, parent_material_uuid=device["uuid"])


def test_plr_material_is_placed_on_a_device_site(service, device) -> None:
    plate = _create(service, _node("plate", plr_type="Plate"))

    _move(service, plate, destination_site_uuid=device["sites"]["s1"])

    assert service.get_material(plate).material.parent_material_uuid == device["uuid"]


def test_every_node_of_the_moved_tree_must_be_plr(service, device) -> None:
    rack = _create(
        service,
        _node("rack", plr_type="TubeRack"),
        _node("tube", plr_type=None, parent_ref="rack"),
    )

    with pytest.raises(MaterialValidationError, match="tube"):
        _move(service, rack, destination_site_uuid=device["sites"]["s1"])


def test_rule_applies_below_the_device_not_only_on_it(service, device) -> None:
    carrier = _create(service, _node("carrier", plr_type="Resource"))
    _move(service, carrier, destination_site_uuid=device["sites"]["s1"])
    bare = _create(service, _node("bare", plr_type=None))

    with pytest.raises(MaterialValidationError, match="plr-only-device"):
        _move(service, bare, parent_material_uuid=carrier)


def test_identity_only_material_still_goes_on_plain_shelves(service) -> None:
    shelf = _create(service, _node("shelf", plr_type=None))
    bare = _create(service, _node("bare", plr_type=None))

    _move(service, bare, parent_material_uuid=shelf)

    assert service.get_material(bare).material.parent_material_uuid == shelf


def test_device_created_with_identity_only_children_is_rejected(service) -> None:
    service.put_template(
        _mutation("put_template"),
        ResourceTemplateWrite(name="bench_tpl", display_name="bench", resource_type="device", class_name="bench_tpl"),
    )
    before = len(service.list_materials())
    device = MaterialNodeCreate(
        client_ref="bench",
        identity=MaterialIdentityWrite(
            resource_id=f"bench-{uuid4().hex[:8]}",
            resource_type="device",
            class_name="bench_tpl",
            template_name="bench_tpl",
        ),
    )

    with pytest.raises(MaterialValidationError, match="config.type"):
        _create(service, device, _node("bare", plr_type=None, parent_ref="bench"))

    assert len(service.list_materials()) == before


def test_device_created_with_plr_children_is_accepted(service) -> None:
    service.put_template(
        _mutation("put_template"),
        ResourceTemplateWrite(name="bench_tpl", display_name="bench", resource_type="device", class_name="bench_tpl"),
    )
    device = MaterialNodeCreate(
        client_ref="bench",
        identity=MaterialIdentityWrite(
            resource_id=f"bench-{uuid4().hex[:8]}",
            resource_type="device",
            class_name="bench_tpl",
            template_name="bench_tpl",
        ),
    )

    bench = _create(service, device, _node("plate", plr_type="Plate", parent_ref="bench"))

    assert service.get_material(bench).material.resource_type == "device"


def test_create_and_place_on_a_device_rolls_back_identity_only_trees(service, device) -> None:
    before = {item.material.material_uuid for item in service.list_materials()}

    with pytest.raises(MaterialValidationError, match="config.type"):
        _create(
            service,
            _node("bare", plr_type=None),
            placement=MaterialPlacement(parent_material_uuid=device["uuid"], site=device["sites"]["s2"]),
        )

    assert {item.material.material_uuid for item in service.list_materials()} == before


def test_create_and_place_on_a_device_site_commits_plr_trees(service, device) -> None:
    plate = _create(
        service,
        _node("plate", plr_type="Plate"),
        placement=MaterialPlacement(parent_material_uuid=device["uuid"], site=device["sites"]["s2"]),
    )

    sites = {site.label: site for site in service.get_material(device["uuid"]).sites}
    assert sites["s2"].occupied_material_uuid == plate
    assert service.get_material(plate).material.parent_material_uuid == device["uuid"]
