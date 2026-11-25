"""设备持有的物料只收格式合规的 PLR 物料，一棵 PLR 树里 PLR 键不重复（权威在每一条写路径上把关）。

设备启动时按 ``ResourceTreeSet`` 把设备子树整棵反序列化成 PLR 实例，靠每个节点的
``config.type``（PLR 自己的类型名）找类，PLR 还要求一棵树里名字唯一。权威只核对格式（Python 标识符
形式的类名），不判断类是否存在（类不存在由 Host 发现，那台设备进入维护模式）。建树即落位、move /
transfer、patch config（含 V1 PUT）与设备快照都按写入后的最终状态走同一道检查。
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
    MaterialPatch,
    MaterialPlacement,
    MaterialSnapshot,
    MaterialTreeCreate,
    ResourceTemplateWrite,
)
from unilabos.resources.adapters.device_site import apply_device_authority_state
from unilabos.resources.resource_tracker import ResourceDict, ResourceDictInstance
from unilabos.server.services.materials import MaterialsService
from unilabos.server.services.materials.core import MaterialConflictError, MaterialValidationError

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


def _node(
    ref: str, *, plr_type: str | None, parent_ref: str | None = None, plr_name: str | None = None
) -> MaterialNodeCreate:
    config: dict = {"type": plr_type} if plr_type else {}
    if plr_name:
        config["name"] = plr_name
    return MaterialNodeCreate(
        client_ref=ref,
        parent_client_ref=parent_ref,
        identity=MaterialIdentityWrite(
            resource_id=f"{ref}-{uuid4().hex[:8]}",
            template_name="sample_tpl",
            config=config,
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

    with pytest.raises(MaterialValidationError, match="config.type"):
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


def _on_device(service, device, site: str = "s1", **node) -> str:
    return _create(
        service,
        _node("plate", **{"plr_type": "Plate", **node}),
        placement=MaterialPlacement(parent_material_uuid=device["uuid"], site=device["sites"][site]),
    )


@pytest.mark.parametrize("config", [{"note": "edited"}, {"type": ""}, {"type": "   "}, {"type": "Not a class"}])
def test_patch_cannot_drop_or_break_config_type_on_a_device(service, device, config) -> None:
    plate = _on_device(service, device)
    before = service.get_material(plate).material

    with pytest.raises(MaterialValidationError, match="config.type"):
        service.patch_material(_mutation("patch_material"), plate, MaterialPatch(config=config))

    after = service.get_material(plate).material
    assert after.config == before.config and after.version == before.version


def test_patch_with_a_well_formed_but_unknown_type_is_left_to_the_host(service, device) -> None:
    """权威只核对格式：类是否存在由 Host 在设备启动时判断（不存在时那台设备进入维护模式）。"""

    plate = _on_device(service, device)

    service.patch_material(_mutation("patch_material"), plate, MaterialPatch(config={"type": "NotAPlrClass"}))

    assert service.get_material(plate).material.config["type"] == "NotAPlrClass"


def test_patch_outside_device_subtrees_is_not_restricted(service) -> None:
    loose = _create(service, _node("loose", plr_type="Plate"))

    service.patch_material(_mutation("patch_material"), loose, MaterialPatch(config={"note": "edited"}))

    assert "type" not in service.get_material(loose).material.config


def test_v1_put_config_cannot_drop_config_type_inside_a_device(service, device) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from unilabos.server.api.materials import install_materials_api

    plate = _on_device(service, device)
    app = FastAPI()
    install_materials_api(app, service)
    with TestClient(app) as client:
        response = client.put(f"/api/v1/materials/{plate}", json={"config": {"note": "edited"}})

    assert response.json()["code"] == 1000
    assert "config.type" in str(response.json())
    assert service.get_material(plate).material.config["type"] == "Plate"


def test_create_on_a_device_site_rejects_a_malformed_type(service, device) -> None:
    before = len(service.list_materials())

    with pytest.raises(MaterialValidationError, match="config.type"):
        _on_device(service, device, plr_type="plate type")

    assert len(service.list_materials()) == before


def test_snapshot_cannot_drop_config_type_inside_a_device(service, device) -> None:
    plate = _on_device(service, device)
    node = service.get_tree(plate).nodes[0]
    observed = node.model_copy(update={"material": node.material.model_copy(update={"config": {"note": "x"}})})

    with pytest.raises(MaterialValidationError, match="config.type"):
        service.apply_snapshot(
            _mutation("apply_material_snapshot"),
            MaterialSnapshot(root_material_uuid=plate, nodes=[observed]),
        )

    assert service.get_material(plate).material.config["type"] == "Plate"


# ── 一棵 PLR 树里 PLR 键唯一 ─────────────────────────────────────


def test_create_onto_a_device_rejects_a_plr_key_already_held_by_that_device(service, device) -> None:
    _on_device(service, device, site="s1", plr_name="other")
    before = len(service.list_materials())

    with pytest.raises(MaterialConflictError, match="other"):
        _on_device(service, device, site="s2", plr_name="other")

    assert len(service.list_materials()) == before
    sites = {site.label: site for site in service.get_material(device["uuid"]).sites}
    assert sites["s2"].occupied_material_uuid is None


def test_the_same_plr_key_on_different_devices_is_fine(service, device) -> None:
    other_uuid = str(uuid4())
    config = ResourceDictInstance(ResourceDict.model_validate({
        "id": "plr-only-other", "uuid": other_uuid, "name": "plr-only-other", "type": "device",
        "class": DEVICE_TEMPLATE, "template_name": DEVICE_TEMPLATE, "config": {}, "data": {}, "extra": {},
        "sites": None, "sites_initialized": False,
    }))
    apply_device_authority_state(config, {"available_sites": [{"index": 1, "label": "o1"}]}, DEVICE_TEMPLATE,
                                 gateway=LocalMaterialsClient(service))
    _on_device(service, device, plr_name="shared")

    _create(
        service,
        _node("plate", plr_type="Plate", plr_name="shared"),
        placement=MaterialPlacement(parent_material_uuid=other_uuid),
    )


def test_move_into_a_tree_that_already_uses_the_plr_key_is_rejected(service) -> None:
    rack = _create(
        service,
        _node("rack", plr_type="TubeRack", plr_name="rack"),
        _node("tube", plr_type="Tube", parent_ref="rack", plr_name="tube_1"),
    )
    loose = _create(service, _node("loose", plr_type="Tube", plr_name="tube_1"))

    with pytest.raises(MaterialConflictError, match="tube_1"):
        _move(service, loose, parent_material_uuid=rack)

    assert service.get_material(loose).material.parent_material_uuid is None


def test_a_tree_created_with_duplicate_plr_keys_is_rejected(service) -> None:
    before = len(service.list_materials())

    with pytest.raises(MaterialConflictError, match="A1"):
        _create(
            service,
            _node("rack", plr_type="TubeRack", plr_name="rack-dup"),
            _node("t1", plr_type="Tube", parent_ref="rack", plr_name="A1"),
            _node("t2", plr_type="Tube", parent_ref="rack", plr_name="A1"),
        )

    assert len(service.list_materials()) == before
