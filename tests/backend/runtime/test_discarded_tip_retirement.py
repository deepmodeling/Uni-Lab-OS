"""被丢弃的枪头随设备快照退役（I04）：运行时证据、局部上报与非枪头缺席。

规则见 ``unilabos.resources.adapters.plr_materials.discarded_tip_uuids``：权威里的
枪头，其父节点在本次上报里、而它既不在上报里、也不再被设备运行时持有时，随同一
快照退役（retired + 软删除）。
"""

from __future__ import annotations

import asyncio

import pytest
from pylabrobot.resources import Coordinate, Deck, Resource, Tip, TipSpot, Trash
from pylabrobot.resources.opentrons.tip_racks import opentrons_96_tiprack_300ul

from unilabos.backend.runtime.resource import AuthorityResourceService
from unilabos.client.materials import LocalMaterialsClient
from unilabos.resources import materials
from unilabos.resources.adapters.plr_materials import (
    SnapshotUuidMismatchError,
    resource_tree_to_snapshot,
)
from unilabos.resources.resource_tracker import ResourceTreeSet
from unilabos.server.services.materials import MaterialNotFoundError, MaterialsService
from tests.plr_fixtures import plate


@pytest.fixture
def authority(tmp_path, monkeypatch):
    monkeypatch.setattr("unilabos.resources.resource_tracker.register", lambda: None)
    service = MaterialsService(tmp_path / "materials.db")
    try:
        yield service, LocalMaterialsClient(service)
    finally:
        service.close()


def _state(service: MaterialsService, material_uuid: str):
    """权威里的 (lifecycle_status, parent_material_uuid)；已退役（软删除）返回 None。"""

    try:
        material = service.get_material(material_uuid).material
    except MaterialNotFoundError:
        return None
    return material.lifecycle_status, material.parent_material_uuid


def _operations(service: MaterialsService, material_uuid: str) -> list[str]:
    return [
        change.operation
        for change in service.changes(limit=10_000)
        if change.aggregate_uuid == material_uuid
    ]


def _bench_with_channel() -> Resource:
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


def test_tip_discarded_from_channel_is_retired_by_strict_snapshot(authority) -> None:
    """现代 PLR 通道（TipMountingShaft）：拿起 → 快照搬到通道；丢弃 → 同一类快照退役。"""

    service, gateway = authority
    root = materials.create(_bench_with_channel(), gateway=gateway)
    spot, shaft = root.children
    tip = spot.tip
    resources = AuthorityResourceService(gateway)

    shaft.mount_tip(tip)
    tip.tracker.set_volume(12)
    asyncio.run(resources.snapshot_resource_tree("bench", "bench-uuid", root))
    assert _state(service, tip.unilabos_uuid) == ("active", shaft.unilabos_uuid)

    discarded = shaft.release_tip()
    assert discarded is tip and tip.parent is None
    asyncio.run(resources.snapshot_resource_tree("bench", "bench-uuid", root))

    assert _state(service, tip.unilabos_uuid) is None
    assert tip.unilabos_uuid not in {
        node.material.material_uuid for node in service.get_tree(root.unilabos_uuid).nodes
    }
    assert _operations(service, tip.unilabos_uuid)[-1] == "retire"

    # 恢复不会把枪头变回来；孔位发号继续，不与已退役枪头同名
    restored = materials.get(root.unilabos_uuid, gateway=gateway).to_plr_resources()[0]
    new_spot, new_shaft = restored.children
    assert new_spot.tip is None and new_shaft.tip is None
    assert new_spot.make_tip().name != tip.name


def _liquid_handler(gateway):
    from pylabrobot.legacy.liquid_handling.backends.chatterbox import (
        LiquidHandlerChatterboxBackend,
    )

    from unilabos.devices.liquid_handling.liquid_handler_abstract import (
        LiquidHandlerAbstract,
    )

    deck = Deck(name="lh_deck", size_x=600, size_y=400, size_z=100)
    deck.assign_child_resource(opentrons_96_tiprack_300ul("rack"), location=Coordinate(10, 10, 0))
    deck.assign_child_resource(Trash("trash", 50, 50, 50), location=Coordinate(400, 10, 0))
    # 运行时形态：台面是权威物料根，液体处理器本身不是物料（台面的 PLR 父节点）
    deck = materials.create(deck, gateway=gateway)
    handler = LiquidHandlerAbstract(LiquidHandlerChatterboxBackend(8), deck, channel_num=8)
    asyncio.run(handler.setup())
    assert deck.parent is handler and not getattr(handler, "unilabos_uuid", None)
    return handler, deck


def test_liquid_handler_abstract_discard_retires_only_the_dropped_tip(authority, monkeypatch) -> None:
    """LiquidHandlerAbstract 开启枪头追踪：拿到通道上的枪头不在台面上报里，但仍被设备持有。

    只上报台面时，通道上的枪头不能退役；丢进废料槽的退役；放回孔位的保持原身份。
    """

    from pylabrobot.resources import tip_tracking

    monkeypatch.setattr(tip_tracking, "tip_tracking_enabled", True)
    service, gateway = authority
    handler, deck = _liquid_handler(gateway)
    resources = AuthorityResourceService(gateway)
    rack = deck.get_resource("rack")
    spot_a1, spot_a2 = rack.get_item("A1"), rack.get_item("A2")
    dropped, returned = spot_a1.tip, spot_a2.tip

    asyncio.run(handler.pick_up_tips([spot_a1, spot_a2], use_channels=[0, 1]))
    assert spot_a1.tip is None and spot_a2.tip is None
    resources.update_resources_sync("lh", "lh-uuid", [deck])
    # 在通道上被持有：权威保持原状，不当成丢弃
    assert _state(service, dropped.unilabos_uuid) == ("active", spot_a1.unilabos_uuid)
    assert _state(service, returned.unilabos_uuid) == ("active", spot_a2.unilabos_uuid)

    asyncio.run(handler.discard_tips(use_channels=[0]))
    resources.update_resources_sync("lh", "lh-uuid", [deck])
    assert _state(service, dropped.unilabos_uuid) is None
    assert _operations(service, dropped.unilabos_uuid)[-1] == "retire"
    assert _state(service, returned.unilabos_uuid) == ("active", spot_a2.unilabos_uuid)

    asyncio.run(handler.return_tips(use_channels=[1]))
    assert spot_a2.tip is returned
    resources.update_resources_sync("lh", "lh-uuid", [deck])
    asyncio.run(resources.snapshot_resource_tree("lh", "lh-uuid", deck))
    assert _state(service, returned.unilabos_uuid) == ("active", spot_a2.unilabos_uuid)
    assert "retire" not in _operations(service, returned.unilabos_uuid)

    restored = materials.get(deck.unilabos_uuid, gateway=gateway).to_plr_resources()[0]
    restored_rack = restored.get_resource("rack")
    assert restored_rack.get_item("A1").tip is None
    assert restored_rack.get_item("A2").tip.unilabos_uuid == returned.unilabos_uuid


def test_liquid_handler_abstract_without_tip_tracking_keeps_tips_in_place(authority) -> None:
    """PLR 默认关闭枪头追踪：孔位不交出枪头，台面结构不变，权威也不退役任何枪头。"""

    from pylabrobot.resources.tip_tracking import does_tip_tracking

    assert not does_tip_tracking()
    service, gateway = authority
    handler, deck = _liquid_handler(gateway)
    spot = deck.get_resource("rack").get_item("A1")
    tip = spot.tip

    asyncio.run(handler.pick_up_tips([spot], use_channels=[0]))
    asyncio.run(handler.discard_tips(use_channels=[0]))
    assert spot.tip is tip
    AuthorityResourceService(gateway).update_resources_sync("lh", "lh-uuid", [deck])
    assert _state(service, tip.unilabos_uuid) == ("active", spot.unilabos_uuid)


def _deck_with_plate_and_tips(gateway):
    deck = Deck(name="deck", size_x=600, size_y=400, size_z=100)
    deck.assign_child_resource(opentrons_96_tiprack_300ul("tips"), location=Coordinate(10, 10, 0))
    deck.assign_child_resource(plate("plate"), location=Coordinate(200, 10, 0))
    return materials.create(deck, gateway=gateway)


def test_absent_non_tip_child_is_rejected_or_left_alone(authority) -> None:
    """非枪头的缺席子节点保持原有语义：严格快照报结构漂移，局部上报保持权威不变。"""

    service, gateway = authority
    deck = _deck_with_plate_and_tips(gateway)
    resources = AuthorityResourceService(gateway)
    labware = deck.get_resource("plate")
    deck.unassign_child_resource(labware)

    with pytest.raises(SnapshotUuidMismatchError):
        asyncio.run(resources.snapshot_resource_tree("deck", "deck-uuid", deck))
    resources.update_resources_sync("deck", "deck-uuid", [deck])
    assert _state(service, labware.unilabos_uuid) == ("active", deck.unilabos_uuid)
    assert "retire" not in _operations(service, labware.unilabos_uuid)


def test_unreported_parent_and_reports_without_runtime_evidence_do_not_retire(authority) -> None:
    """父节点不在上报里、或上报不是运行时 PLR 序列化（原始字典 / 权威读回）时不推断丢弃。"""

    service, gateway = authority
    deck = _deck_with_plate_and_tips(gateway)
    resources = AuthorityResourceService(gateway)
    spot = deck.get_resource("tips").get_item("A1")
    tip = spot.tip
    spot.unassign_tip()

    # 只上报一块板：枪头的孔位不在上报里
    resources.update_resources_sync("deck", "deck-uuid", [deck.get_resource("plate")])
    assert _state(service, tip.unilabos_uuid) == ("active", spot.unilabos_uuid)

    # 同一棵台面树，但经 dump/load 成为原始字典：没有运行时证据
    raw = ResourceTreeSet.load(ResourceTreeSet.from_plr_resources([deck]).dump())
    assert raw.runtime_material_uuids is None
    resources.update_resources_sync("deck", "deck-uuid", raw)
    assert _state(service, tip.unilabos_uuid) == ("active", spot.unilabos_uuid)
    baseline = service.get_tree(deck.unilabos_uuid)
    nodes = [item.res_content for item in raw.all_nodes]
    snapshot = resource_tree_to_snapshot(nodes, baseline, allow_partial=True)
    assert all(node.material.lifecycle_status == "active" for node in snapshot.nodes)

    # 运行时树集带着证据：同一次缺席就是丢弃
    resources.update_resources_sync("deck", "deck-uuid", [deck])
    assert _state(service, tip.unilabos_uuid) is None


def test_stale_baseline_after_concurrent_retirement_converges(authority) -> None:
    """观察者与 update_resource 并发：一方已退役枪头，另一方的旧基线按冲突重读收敛，不报找不到。"""

    service, gateway = authority
    deck = _deck_with_plate_and_tips(gateway)
    observer_side = AuthorityResourceService(gateway)
    explicit_side = AuthorityResourceService(gateway)
    explicit_side.update_resources_sync("deck", "deck-uuid", [deck])  # 缓存含枪头的基线

    spot = deck.get_resource("tips").get_item("A1")
    tip = spot.tip
    spot.unassign_tip()
    observer_side.update_resources_sync("deck", "deck-uuid", [deck])
    assert _state(service, tip.unilabos_uuid) is None

    authoritative = explicit_side.update_resources_sync("deck", "deck-uuid", [deck])
    assert tip.unilabos_uuid not in set(authoritative.all_nodes_uuid)
    assert _operations(service, tip.unilabos_uuid).count("retire") == 1
