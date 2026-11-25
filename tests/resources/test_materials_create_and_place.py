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
from unilabos.server.services.materials.core import MaterialValidationError
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
    # assign 与 transfer 同一条路：权威提交后经物料服务下发 unload / load
    service.set_resource_sync_dispatcher(runtime.dispatch_material_sync)
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

    result = materials.assign(node, created, slot="slot_b")

    assert _site(service, device["device_uuid"], "slot_a").occupied_material_uuid is None
    assert (
        _site(service, device["device_uuid"], "slot_b").occupied_material_uuid
        == created.unilabos_uuid
    )
    # 同设备换位也是一次 unload + load：本地按权威重建，tracker 里只有新的那一份
    [moved] = result["resources"]
    assert node.resource_tracker.uuid_to_resources[created.unilabos_uuid] is moved
    assert [res.name for res in node.resource_tracker.resources] == ["mover"]


def test_assign_to_the_device_without_a_site_only_sets_the_parent(device) -> None:
    service, node = device["service"], device["node"]
    loose = materials.create(plate("loose"), gateway=device["gateway"])

    materials.assign(node, loose)

    assert service.get_material(loose.unilabos_uuid).material.parent_material_uuid == device["device_uuid"]
    assert all(
        site.occupied_material_uuid is None
        for site in service.get_material(device["device_uuid"]).sites
    )


def test_assign_loads_a_material_the_authority_already_put_on_this_device(device) -> None:
    """权威里它已在目标库位、本设备却没有它：assign 从权威重建挂上并成功，不把「没有变化」当错误。"""

    from unilabos.protocol.materials import MaterialMove

    service, node, gateway = device["service"], device["node"], device["gateway"]
    loose = materials.create(plate("committed"), gateway=gateway)
    slot_a = _site(service, device["device_uuid"], "slot_a")
    # 只改权威、不做设备侧投影（等同一次提交了、material.sync 却没做完的移动）
    service.move_material(
        _move_mutation(),
        MaterialMove(material_uuid=loose.unilabos_uuid, destination_site_uuid=slot_a.site_uuid),
    )
    assert loose.unilabos_uuid not in node.resource_tracker.uuid_to_resources

    result = materials.assign(node, loose.unilabos_uuid, slot="slot_a")

    [mounted] = result["resources"]
    assert node.resource_tracker.uuid_to_resources[loose.unilabos_uuid] is mounted
    assert result["moves"] == []
    assert _site(service, device["device_uuid"], "slot_a").occupied_material_uuid == loose.unilabos_uuid
    # 本设备已持有它：再来一次什么都不做，仍是同一份实例
    again = materials.assign(node, loose.unilabos_uuid, slot="slot_a")
    assert again["resources"] == [mounted]


def test_assign_converges_after_the_device_projection_failed_once(device) -> None:
    """权威已提交、设备侧 unload 失败（例如当时还没有 material.sync handler）：调用方收到错误，
    再 assign 一次就收敛——权威答「已在目标位置」，本设备从权威加载。来源与目标都是调用方自己。"""

    from tests.plr_fixtures import site_deck
    from unilabos.server.services.materials import MaterialTransferSyncError

    service, node, gateway = device["service"], device["node"], device["gateway"]
    deck_uuid = str(uuid4())
    materials.ensure(site_deck(deck_uuid, "Bench_Deck", ("T1", "T2")), gateway=gateway)
    dispatch = node.__dict__["_hostlink_runtime"].dispatch_material_sync
    failures = []

    def unload_fails_once(command):
        if command.action == "unload" and not failures:
            failures.append(command)
            raise RuntimeError("unknown action: material.sync")
        return dispatch(command)

    service.set_resource_sync_dispatcher(unload_fails_once)

    with pytest.raises(MaterialTransferSyncError, match="unload failed"):
        materials.assign(node, deck_uuid)
    assert failures[0].device_id == "placer"
    assert service.get_material(deck_uuid).material.parent_material_uuid == device["device_uuid"]
    assert deck_uuid not in node.resource_tracker.uuid_to_resources

    [deck] = materials.assign(node, deck_uuid)["resources"]

    assert node.resource_tracker.uuid_to_resources[deck_uuid] is deck
    # 收敛后与权威一致：后续挂到台面库位、严格快照照常进行
    loose = materials.create(plate("after_retry"), gateway=gateway)
    [placed] = materials.assign(node, loose, parent=deck_uuid, slot="T1")["resources"]
    assert placed.parent is deck
    materials.update(node, deck)


def test_assign_of_several_materials_moves_the_rest_when_one_is_already_there(device) -> None:
    """一条命令里有一个物料已在目标位置时，其余的照常移动，已在的那个不报错。"""

    service, node, gateway = device["service"], device["node"], device["gateway"]
    first = materials.create(plate("first_of_two"), gateway=gateway)
    second = materials.create(plate("second_of_two"), gateway=gateway)
    materials.assign(node, first)

    result = materials.assign(node, [first, second])

    assert {res.unilabos_uuid for res in result["resources"]} == {first.unilabos_uuid, second.unilabos_uuid}
    assert service.get_material(second.unilabos_uuid).material.parent_material_uuid == device["device_uuid"]
    assert second.unilabos_uuid in node.resource_tracker.uuid_to_resources


def _move_mutation():
    from unilabos.protocol.materials import InventoryMutation

    command_uuid = str(uuid4())
    return InventoryMutation(
        command_uuid=command_uuid,
        effect_key=f"move_material:{command_uuid}",
        operation="move_material",
        actor_type="test",
        actor_uuid="create-and-place-test",
    )


def test_assign_rejects_an_unknown_device_site_without_mounting(device) -> None:
    service, node = device["service"], device["node"]
    loose = materials.create(plate("stray"), gateway=device["gateway"])

    with pytest.raises(MaterialValidationError, match="slot_z"):
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

    [moved] = materials.assign(node, inner, slot="slot_b")["resources"]

    assert service.get_material(inner.unilabos_uuid).material.parent_material_uuid == device["device_uuid"]
    assert _site(service, device["device_uuid"], "slot_b").occupied_material_uuid == inner.unilabos_uuid
    assert inner.parent is None and created.children == []
    assert {res.name for res in node.resource_tracker.resources} == {"holder", "inner"}
    assert node.resource_tracker.parent_resource(moved) is None
    # 原父树与权威一致：严格快照不再因为「本地多一个孩子」被拒
    materials.update(node, created)
    materials.update(node, moved)


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

    with pytest.raises(MaterialValidationError, match="has no Site"):
        materials.assign(node, loose, site=foreign.site_uuid)

    assert loose.unilabos_uuid not in node.resource_tracker.uuid_to_resources
    assert service.get_material(loose.unilabos_uuid).material.parent_material_uuid is None
    assert _site(service, other_uuid, "o1").occupied_material_uuid is None


# ── 没给库位的落位：位置是原点，权威里也是，重启照样反序列化 ─────────────────


def _holder_on(device, name: str = "holder"):
    from pylabrobot.resources import Resource

    holder = Resource(name, size_x=200, size_y=200, size_z=10, category="container")
    return materials.create(holder, node=device["node"], site="slot_a", gateway=device["gateway"])


def _restart(device, root_uuid: str):
    """设备重启时持有的物料按权威整棵反序列化。"""

    return materials.get(root_uuid, gateway=device["gateway"]).to_plr_resources()[0]


def test_create_under_a_parent_without_a_site_gets_the_origin_and_restarts(device) -> None:
    from pylabrobot.resources import Coordinate

    holder = _holder_on(device)

    other = materials.create(plate("other"), node=device["node"], parent=holder, gateway=device["gateway"])

    assert other.location == Coordinate(0, 0, 0)
    stored = device["service"].get_material(other.unilabos_uuid).position
    assert (stored.position_x, stored.position_y, stored.position_z) == (0, 0, 0)
    restored = _restart(device, holder.unilabos_uuid)
    assert [child.name for child in restored.children] == ["other"]


def test_assign_under_a_parent_without_a_site_gets_the_origin_and_restarts(device) -> None:
    from pylabrobot.resources import Coordinate

    holder = _holder_on(device)
    loose = materials.create(plate("loose_child"), gateway=device["gateway"])

    [mounted] = materials.assign(device["node"], loose, parent=holder)["resources"]

    assert mounted.parent is device["node"].resource_tracker.uuid_to_resources[holder.unilabos_uuid]
    assert mounted.location == Coordinate(0, 0, 0)
    assert [child.name for child in _restart(device, holder.unilabos_uuid).children] == ["loose_child"]


# ── 本地加载失败时不留分叉：先查前提，提交后失败就在同一条命令下撤销 ─────────


def test_a_plr_key_the_tree_already_uses_is_rejected_before_anything_changes(device) -> None:
    """PLR 键在这棵树里已经有了（resource_id 不同）：权威拒绝，本地加载根本不会开始。"""

    from pylabrobot.resources import Coordinate, Resource

    service, node = device["service"], device["node"]
    holder = Resource("holder", size_x=200, size_y=200, size_z=10, category="container")
    holder.assign_child_resource(plate("other"), location=Coordinate(0, 0, 0))
    holder = materials.create(holder, node=node, site="slot_a", gateway=device["gateway"])
    before = len(service.list_materials())

    with pytest.raises(Exception, match="other"):
        materials.create(plate("other"), node=node, parent=holder, gateway=device["gateway"])

    assert len(service.list_materials()) == before
    local = node.resource_tracker.uuid_to_resources[holder.unilabos_uuid]
    assert [child.name for child in local.children] == ["other"]
    assert len(node.resource_tracker.uuid_to_resources) == 1 + len(local.get_all_children())
    materials.update(node, local)


def test_a_local_load_failure_after_the_commit_is_undone(device, monkeypatch) -> None:
    """权威已建好并落位、本地挂载失败（库位对不上）：撤销权威里的新树，tracker 里不留孤儿。"""

    from unilabos.client.host.runtime.node import DeviceNode

    service, node = device["service"], device["node"]
    holder = _holder_on(device)
    before = {item.material.material_uuid for item in service.list_materials()}
    indexed = set(node.resource_tracker.uuid_to_resources)

    def mismatch(*_args, **_kwargs):
        raise ValueError("本地库位对不上")

    monkeypatch.setattr(DeviceNode, "_carrier_placement", classmethod(mismatch))
    with pytest.raises(ValueError, match="本地库位对不上"):
        materials.create(plate("stray"), node=node, parent=holder, gateway=device["gateway"])

    assert {item.material.material_uuid for item in service.list_materials()} == before
    assert set(node.resource_tracker.uuid_to_resources) == indexed
    assert node.resource_tracker.uuid_to_resources[holder.unilabos_uuid].children == []


def test_a_parent_the_device_does_not_hold_is_refused_before_the_commit(device) -> None:
    service = device["service"]
    elsewhere = materials.create(plate("elsewhere"), gateway=device["gateway"])
    before = len(service.list_materials())

    with pytest.raises(ValueError, match="不在设备"):
        materials.create(plate("orphan_child"), node=device["node"], parent=elsewhere, gateway=device["gateway"])

    assert len(service.list_materials()) == before


def test_a_placed_create_from_the_node_executor_is_refused_before_the_commit(device) -> None:
    from unilabos.client.host.runtime.async_utils import run_node_coroutine

    service, node = device["service"], device["node"]
    before = len(service.list_materials())

    async def from_the_executor():
        materials.create(plate("blocked"), node=node, site="slot_b", gateway=device["gateway"], timeout=1.0)

    with pytest.raises(RuntimeError, match="执行器线程"):
        run_node_coroutine(node, from_the_executor(), 5.0)

    assert len(service.list_materials()) == before


# ── assign 到库位就是一次物料移动：权威提交 → 原设备卸下 → 新设备重建 ─────────


@pytest.fixture
def second_device(device):
    service, gateway = device["service"], device["gateway"]
    device_uuid = str(uuid4())
    config = ResourceDictInstance(ResourceDict.model_validate({
        **_device_config(device_uuid).res_content.model_dump(by_alias=True), "id": "receiver", "name": "receiver",
    }))
    apply_device_authority_state(config, {"available_sites": DEVICE_SITES}, DEVICE_TEMPLATE, gateway=gateway)
    runtime = HostLinkLocalRuntime()
    node = runtime.add_driver(HostLinkDriverSpec("receiver", _Device, {}, resource_uuid=device_uuid))
    runtime.set_resource_service(AuthorityResourceService(gateway))
    first_dispatch = device["node"].__dict__["_hostlink_runtime"].dispatch_material_sync

    def dispatch(command):
        target = runtime if command.device_id == "receiver" else None
        return target.dispatch_material_sync(command) if target is not None else first_dispatch(command)

    service.set_resource_sync_dispatcher(dispatch)
    runtime.start()
    try:
        yield {"node": node, "device_uuid": device_uuid}
    finally:
        runtime.stop()


def test_assign_onto_another_devices_site_unloads_it_from_the_holder(device, second_device) -> None:
    service, source = device["service"], device["node"]
    sample = materials.create(plate("traveller"), node=source, site="slot_a", gateway=device["gateway"])

    [arrived] = materials.assign(second_device["node"], sample.unilabos_uuid, slot="slot_b")["resources"]

    assert sample.unilabos_uuid not in source.resource_tracker.uuid_to_resources
    assert source.resource_tracker.resources == []
    assert second_device["node"].resource_tracker.uuid_to_resources[sample.unilabos_uuid] is arrived
    assert service.get_material(sample.unilabos_uuid).material.parent_material_uuid == second_device["device_uuid"]
    assert _site(service, device["device_uuid"], "slot_a").occupied_material_uuid is None
    # 两边本地树都与权威一致：严格快照照常提交
    materials.update(second_device["node"], arrived)


def test_a_rejected_assign_onto_a_material_changes_nothing_locally(device) -> None:
    """权威拒绝（库位只收别的模板）时本地不先挂：父物料下不会多出一个权威里没有的孩子。"""

    from tests.plr_fixtures import site_deck

    service, node, gateway = device["service"], device["node"], device["gateway"]
    deck_uuid = str(uuid4())
    materials.ensure(site_deck(deck_uuid, "Bench_Deck", ("T1", "T2")), gateway=gateway)
    materials.assign(node, deck_uuid)
    t1 = next(site for site in service.get_material(deck_uuid).sites if site.label == "T1")
    with service.write() as connection:
        connection.execute(
            "UPDATE site SET extra_json=? WHERE site_uuid=?",
            ('{"allowed_resource_template_uuids": ["only_tip_racks"]}', t1.site_uuid),
        )
    loose = materials.create(plate("refused"), gateway=gateway)

    with pytest.raises(Exception, match="只接受资源模板"):
        materials.assign(node, loose, parent=deck_uuid, slot="T1")

    deck = node.resource_tracker.uuid_to_resources[deck_uuid]
    assert deck.children == [] and loose.unilabos_uuid not in node.resource_tracker.uuid_to_resources
    assert service.get_material(loose.unilabos_uuid).material.parent_material_uuid is None
