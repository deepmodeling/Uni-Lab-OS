"""物料创建即落位（原子）与挂到设备自身库位：``materials.create(site=)`` / ``materials.assign(site=)``。

驱动创建物料只走一条路：client 物料服务（``ResourceTreeSet`` 序列化）发给权威，权威在建树的
同一事务里把根物料挂到父物料、占用库位；任一步不满足整体回滚，不留没落位的根物料。
本地按 transfer 的 load 投影挂上，返回 tracker 里那一份实例（uuid → 实例唯一）。
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from unilabos.client.host.hostlink.local_runtime import HostLinkDriverSpec, HostLinkLocalRuntime
from unilabos.client.host.runtime.resource import AuthorityResourceService
from unilabos.client.materials import LocalMaterialsClient
from unilabos.resources import materials
from unilabos.resources.adapters.device_site import apply_device_authority_state
from unilabos.resources.resource_tracker import ResourceDict, ResourceDictInstance
from unilabos.server.services.materials import MaterialsService
from tests.plr_fixtures import plate

DEVICE_TEMPLATE = "create_and_place_test_device"
DEVICE_SITES = [
    {"index": 1, "label": "slot_a"},
    {"index": 2, "label": "slot_b"},
]


class _Device:
    def __init__(self, **_kwargs) -> None:
        pass


def _device_config(device_uuid: str) -> ResourceDictInstance:
    return ResourceDictInstance(
        ResourceDict.model_validate(
            {
                "id": "placer",
                "uuid": device_uuid,
                "name": "placer",
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


@pytest.fixture
def device(tmp_path):
    service = MaterialsService(tmp_path / "materials.db")
    gateway = LocalMaterialsClient(service)
    device_uuid = str(uuid4())
    config = _device_config(device_uuid)
    apply_device_authority_state(
        config, {"available_sites": DEVICE_SITES}, DEVICE_TEMPLATE, gateway=gateway
    )
    runtime = HostLinkLocalRuntime()
    node = runtime.add_driver(
        HostLinkDriverSpec("placer", _Device, {}, resource_uuid=device_uuid)
    )
    runtime.set_resource_service(AuthorityResourceService(gateway))
    runtime.start()
    try:
        yield {
            "service": service,
            "gateway": gateway,
            "node": node,
            "device_uuid": device_uuid,
            "sites": {site.label: site for site in config.res_content.sites},
        }
    finally:
        runtime.stop()
        service.close()


def _site(service, device_uuid: str, label: str):
    return next(
        site for site in service.get_material(device_uuid).sites if site.label == label
    )


def test_create_places_the_root_on_a_device_site_in_one_transaction(device) -> None:
    service, node = device["service"], device["node"]

    created = materials.create(
        plate("plate_a"), node=node, site="slot_a", gateway=device["gateway"]
    )

    material_uuid = created.unilabos_uuid
    assert _site(service, device["device_uuid"], "slot_a").occupied_material_uuid == material_uuid
    stored = service.get_material(material_uuid).material
    assert stored.parent_material_uuid == device["device_uuid"]
    # 返回的就是 tracker 登记的那一份实例
    assert node.resource_tracker.uuid_to_resources[material_uuid] is created
    # 建树与落位同一条变更命令：账本里没有一个「建好了还没落位」的中间版本
    site_uuid = _site(service, device["device_uuid"], "slot_a").site_uuid
    ledger = service.list_ledger(limit=100_000)
    created_by = next(entry.command_uuid for entry in ledger if entry.aggregate_uuid == material_uuid)
    occupied_by = [
        entry.command_uuid
        for entry in ledger
        if entry.aggregate_uuid == site_uuid
        and entry.delta_json.get("occupied_material_uuid") == material_uuid
    ]
    assert occupied_by == [created_by]


def test_create_accepts_the_site_instance_from_post_init(device) -> None:
    service, node = device["service"], device["node"]

    created = materials.create(
        plate("plate_b"), node=node, site=device["sites"]["slot_b"], gateway=device["gateway"]
    )

    assert (
        _site(service, device["device_uuid"], "slot_b").occupied_material_uuid
        == created.unilabos_uuid
    )


def test_create_rolls_back_when_the_site_is_occupied(device) -> None:
    service, node, gateway = device["service"], device["node"], device["gateway"]
    first = materials.create(plate("first"), node=node, site="slot_a", gateway=gateway)
    before = {item.material.material_uuid for item in service.list_materials()}

    with pytest.raises(Exception, match="occupied"):
        materials.create(plate("second"), node=node, site="slot_a", gateway=gateway)

    # 整体回滚：没有留下没落位的新根物料，库位仍是原占用者，本地也没多出实例
    assert {item.material.material_uuid for item in service.list_materials()} == before
    assert _site(service, device["device_uuid"], "slot_a").occupied_material_uuid == first.unilabos_uuid
    assert [res.name for res in node.resource_tracker.resources] == ["first"]


def test_create_rejects_an_unknown_site_before_anything_is_written(device) -> None:
    service, node = device["service"], device["node"]
    before = len(service.list_materials())

    with pytest.raises(ValueError, match="slot_z"):
        materials.create(plate("lost"), node=node, site="slot_z", gateway=device["gateway"])

    assert len(service.list_materials()) == before
    assert node.resource_tracker.resources == []


def test_create_with_placement_requires_a_node(device) -> None:
    with pytest.raises(ValueError, match="node"):
        materials.create(plate("orphan"), site="slot_a", gateway=device["gateway"])


def test_assign_occupies_a_device_site_by_instance_and_by_label(device) -> None:
    service, node, gateway = device["service"], device["node"], device["gateway"]
    by_instance = materials.create(plate("by_instance"), gateway=gateway)
    by_label = materials.create(plate("by_label"), gateway=gateway)

    materials.assign(node, by_instance, site=device["sites"]["slot_a"])
    materials.assign(node, by_label.unilabos_uuid, slot="slot_b")

    assert (
        _site(service, device["device_uuid"], "slot_a").occupied_material_uuid
        == by_instance.unilabos_uuid
    )
    assert (
        _site(service, device["device_uuid"], "slot_b").occupied_material_uuid
        == by_label.unilabos_uuid
    )
    for material in (by_instance, by_label):
        stored = service.get_material(material.unilabos_uuid).material
        assert stored.parent_material_uuid == device["device_uuid"]
        assert material.unilabos_uuid in node.resource_tracker.uuid_to_resources


def test_assign_moves_between_device_sites(device) -> None:
    service, node = device["service"], device["node"]
    created = materials.create(plate("mover"), node=node, site="slot_a", gateway=device["gateway"])

    materials.assign(node, created, slot="slot_b")

    assert _site(service, device["device_uuid"], "slot_a").occupied_material_uuid is None
    assert (
        _site(service, device["device_uuid"], "slot_b").occupied_material_uuid
        == created.unilabos_uuid
    )
    assert node.resource_tracker.uuid_to_resources[created.unilabos_uuid] is created


def test_assign_to_the_device_without_a_site_only_sets_the_parent(device) -> None:
    service, node = device["service"], device["node"]
    loose = materials.create(plate("loose"), gateway=device["gateway"])

    materials.assign(node, loose)

    assert service.get_material(loose.unilabos_uuid).material.parent_material_uuid == device["device_uuid"]
    assert all(
        site.occupied_material_uuid is None
        for site in service.get_material(device["device_uuid"]).sites
    )


def test_assign_rejects_an_unknown_device_site_without_mounting(device) -> None:
    service, node = device["service"], device["node"]
    loose = materials.create(plate("stray"), gateway=device["gateway"])

    with pytest.raises(ValueError, match="slot_z"):
        materials.assign(node, loose, slot="slot_z")

    assert loose.unilabos_uuid not in node.resource_tracker.uuid_to_resources
    assert service.get_material(loose.unilabos_uuid).material.parent_material_uuid is None


def test_assign_puts_at_most_one_material_on_a_site(device) -> None:
    gateway = device["gateway"]
    first = materials.create(plate("one"), gateway=gateway)
    second = materials.create(plate("two"), gateway=gateway)

    with pytest.raises(ValueError, match="一个库位只能放一个物料"):
        materials.assign(device["node"], [first, second], slot="slot_a")


def test_assign_onto_an_occupied_device_site_leaves_nothing_mounted(device) -> None:
    """权威拒绝（库位被占）时本地还没动：物料不进设备 tracker，权威父物料不变。"""

    service, node, gateway = device["service"], device["node"], device["gateway"]
    first = materials.create(plate("first"), node=node, site="slot_a", gateway=gateway)
    loose = materials.create(plate("loose"), gateway=gateway)

    with pytest.raises(Exception, match="occupied"):
        materials.assign(node, loose, slot="slot_a")

    assert loose.unilabos_uuid not in node.resource_tracker.uuid_to_resources
    assert [res.name for res in node.resource_tracker.resources] == ["first"]
    assert service.get_material(loose.unilabos_uuid).material.parent_material_uuid is None
    assert _site(service, device["device_uuid"], "slot_a").occupied_material_uuid == first.unilabos_uuid


def test_assign_moves_a_nested_material_off_its_plr_parent_onto_a_device_site(device) -> None:
    """本地 PLR 父物料下的孩子放到设备库位：本地也从原父物料卸下，原父树之后照常快照。"""

    from pylabrobot.resources import Coordinate, Resource

    service, node, gateway = device["service"], device["node"], device["gateway"]
    holder = Resource("holder", size_x=200, size_y=200, size_z=10, category="container")
    holder.assign_child_resource(plate("inner"), location=Coordinate(0, 0, 0))
    created = materials.create(holder, node=node, site="slot_a", gateway=gateway)
    inner = next(child for child in created.children if child.name == "inner")

    materials.assign(node, inner, slot="slot_b")

    assert service.get_material(inner.unilabos_uuid).material.parent_material_uuid == device["device_uuid"]
    assert _site(service, device["device_uuid"], "slot_b").occupied_material_uuid == inner.unilabos_uuid
    assert inner.parent is None and created.children == []
    assert {res.name for res in node.resource_tracker.resources} == {"holder", "inner"}
    assert node.resource_tracker.parent_resource(inner) is None
    # 原父树与权威一致：严格快照不再因为「本地多一个孩子」被拒
    materials.update(node, created)
    materials.update(node, inner)


def test_assign_rejects_another_devices_site_uuid(device) -> None:
    """``site`` 给的 uuid 必须是本设备的库位；别的设备的 Site 不会被当成本设备的。"""

    service, node, gateway = device["service"], device["node"], device["gateway"]
    other_uuid = str(uuid4())
    other = ResourceDictInstance(ResourceDict.model_validate({
        **_device_config(other_uuid).res_content.model_dump(by_alias=True), "id": "other", "name": "other",
    }))
    apply_device_authority_state(other, {"available_sites": [{"index": 1, "label": "o1"}]}, DEVICE_TEMPLATE,
                                 gateway=gateway)
    foreign = _site(service, other_uuid, "o1")
    loose = materials.create(plate("wanderer"), gateway=gateway)

    with pytest.raises(ValueError, match="不存在"):
        materials.assign(node, loose, site=foreign.site_uuid)

    assert loose.unilabos_uuid not in node.resource_tracker.uuid_to_resources
    assert service.get_material(loose.unilabos_uuid).material.parent_material_uuid is None
    assert _site(service, other_uuid, "o1").occupied_material_uuid is None
