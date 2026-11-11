"""枪头从 state 转移到结构孩子时，权威身份和内容物必须保持。"""

from uuid import uuid4
import asyncio

import pytest
from pylabrobot.resources import Coordinate, Resource, Tip, TipSpot

from unilabos.resources import materials
from unilabos.resources.adapters.plr_materials import resource_tree_to_snapshot
from unilabos.resources.resource_tracker import ResourceTreeSet
from unilabos.protocol.materials import InventoryMutation
from unilabos.server.services.materials import MaterialsService
from unilabos.client.materials import LocalMaterialsClient

def make_root():
    from pylabrobot.resources.n_channel_pipettes import TipMountingShaft

    root = Resource("bench", 300, 300, 300, category="resource")
    spot = TipSpot(
        "bench_spot",
        9,
        9,
        lambda name: Tip(
            name=name,
            diameter=5,
            size_z=50,
            has_filter=False,
            maximal_volume=200,
            fitting_depth=8,
        ),
    )
    spot.assign_tip(spot.make_tip())
    root.assign_child_resource(spot, location=Coordinate(10, 20, 30))
    root.assign_child_resource(
        TipMountingShaft("bench_channel", "friction"), location=Coordinate(50, 60, 70)
    )
    return root


def test_authority_tip_pickup_snapshot_restore_and_return(tmp_path, monkeypatch):
    monkeypatch.setattr("unilabos.resources.resource_tracker.register", lambda: None)
    service = MaterialsService(tmp_path / "materials.db")
    draft = make_root()
    root = materials.create(draft, gateway=LocalMaterialsClient(service))
    spot, shaft = root.children
    tip = spot.tip
    uid = tip.unilabos_uuid
    assert not getattr(draft.children[0].tip, "unilabos_uuid", None)
    assert service.get_material(uid).material.parent_material_uuid == spot.unilabos_uuid
    baseline = service.get_tree(root.unilabos_uuid)
    assert not service.compare_snapshot(
        resource_tree_to_snapshot(
            ResourceTreeSet.from_plr_resources([root]),
            baseline,
        )
    ).changed

    shaft.mount_tip(tip)
    tip.tracker.set_volume(12)
    tree = ResourceTreeSet.from_plr_resources([root])
    snapshot = resource_tree_to_snapshot(tree, baseline)
    diff = service.compare_snapshot(snapshot)
    assert any(change.aggregate_uuid == uid for change in diff.changes)
    service.apply_snapshot(
        InventoryMutation(
            command_uuid=str(uuid4()),
            effect_key="pickup",
            operation="apply_material_snapshot",
        ),
        snapshot,
    )
    assert (
        service.get_material(uid).material.parent_material_uuid == shaft.unilabos_uuid
    )
    from unilabos.resources.adapters.plr_materials import material_tree_to_plr_resources

    restored = material_tree_to_plr_resources(service.get_tree(root.unilabos_uuid))[0]
    new_spot, new_shaft = restored.children
    assert new_spot.tip is None
    assert new_shaft.tip.unilabos_uuid == uid
    assert new_shaft.tip.tracker.get_used_volume() == 12
    new_spot.assign_tip(new_shaft.tip)
    again = ResourceTreeSet.from_plr_resources([restored]).to_plr_resources(
        skip_devices=False
    )[0]
    assert again.children[0].tip.unilabos_uuid == uid
    assert again.children[0].tip.tracker.get_used_volume() == 12
    assert again.children[1].tip is None
    assert again.children[0].make_tip().name != tip.name


def test_snapshot_does_not_change_tip_counter_or_prototype():
    from unilabos.resources.plr_identity import material_serialization

    root = make_root()
    counter = root.children[0]._tip_counter
    first = material_serialization(root)
    assert material_serialization(root) == first
    assert root.children[0]._tip_counter == counter


@pytest.mark.parametrize("head_field", ["head", "head96"])
def test_legacy_head_tip_identity_roundtrip(head_field, monkeypatch, tmp_path):
    from pylabrobot.resources import Deck
    from pylabrobot.legacy.tip_tracker import TipTracker
    from pylabrobot.legacy.liquid_handling.liquid_handler import LiquidHandler
    from pylabrobot.legacy.liquid_handling.backends.chatterbox import (
        LiquidHandlerChatterboxBackend,
    )

    monkeypatch.setattr("unilabos.resources.resource_tracker.register", lambda: None)
    lh = LiquidHandler(
        LiquidHandlerChatterboxBackend(),
        Deck(name="deck", size_x=300, size_y=300, size_z=300),
    )
    spot = make_root().children[0]
    tip = spot.unassign_tip()
    tip.tracker.set_volume(17)
    channel = TipTracker("channel")
    channel.add_tip(tip)
    getattr(lh, head_field)[0] = channel
    service = MaterialsService(tmp_path / "legacy.db")
    lh = materials.create(lh, gateway=LocalMaterialsClient(service))
    tip_uuid = getattr(lh, head_field)[0].get_tip().unilabos_uuid
    assert (
        service.get_material(tip_uuid).material.parent_material_uuid == lh.unilabos_uuid
    )
    tree = ResourceTreeSet.from_plr_resources([lh])
    restored = tree.to_plr_resources(skip_devices=False)[0]
    restored_tip = getattr(restored, head_field)[0].get_tip()
    assert restored_tip.unilabos_uuid == tip_uuid
    assert restored_tip.tracker.get_used_volume() == 17
    ResourceTreeSet.from_plr_resources([restored])


def test_graphio_tip_identity_roundtrip(monkeypatch):
    from unilabos.resources.graphio import resource_plr_to_ulab, resource_ulab_to_plr

    root = make_root()
    ResourceTreeSet.from_plr_resources([root], known_random_uuid=True)
    tip_uuid = root.children[0].tip.unilabos_uuid
    restored = resource_ulab_to_plr(resource_plr_to_ulab(root), plr_model=True)
    assert restored.children[0].tip.unilabos_uuid == tip_uuid


def test_uncommitted_tip_transfer_is_explicitly_rejected():
    root = make_root()
    root.children[0].tracker.remove_tip(commit=False)
    with pytest.raises(ValueError, match="未提交"):
        ResourceTreeSet.from_plr_resources([root], known_random_uuid=True)


def test_observer_reports_tip_moves_and_contents(tmp_path, monkeypatch):
    from unilabos.backend.runtime.resource import (
        AuthorityResourceService,
        MaterialSnapshotObserver,
    )

    monkeypatch.setattr("unilabos.resources.resource_tracker.register", lambda: None)
    authority = MaterialsService(tmp_path / "observer.db")
    gateway = LocalMaterialsClient(authority)
    root = materials.create(make_root(), gateway=gateway)
    service = AuthorityResourceService(gateway)

    async def run():
        observer = MaterialSnapshotObserver(
            service,
            device_id=lambda: "device-1",
            device_uuid=lambda: "device-uuid",
            schedule=asyncio.create_task,
        )
        observer.observe(root)
        spot, shaft = root.children
        tip = spot.tip
        uid = tip.unilabos_uuid
        try:
            shaft.mount_tip(tip)
            await observer.wait_idle()
            assert observer.errors == ()
            assert (
                authority.get_material(uid).material.parent_material_uuid
                == shaft.unilabos_uuid
            )
            tip.tracker.set_volume(19)
            await observer.wait_idle()
            assert observer.errors == ()
            assert (
                sum(s.quantity for s in authority.get_material(uid).data.substances)
                == 19
            )
            spot.assign_tip(tip)
            await observer.wait_idle()
            assert observer.errors == ()
            assert (
                authority.get_material(uid).material.parent_material_uuid
                == spot.unilabos_uuid
            )
        finally:
            observer.unobserve(root)

    try:
        asyncio.run(run())
    finally:
        authority.close()
