"""设备初始化拿到的子树先拆一遍：旧数据里的非 PLR 物料拆下来点名报错，其余照常初始化。

权威已在落位时拒绝新的非 PLR 物料进入设备子树；这里只处理升级前留下的旧数据（如旧版虚拟加热
平台经网关手写、没有 ``config.type`` 的样品）——以前它让设备在重启时直接起不来。拆的粒度是节点：
台面上放着一个旧物料时，台面（连同其它 PLR 物料）仍和设备一起初始化，比如液体工作站构造时
要带上的 deck。
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

from pylabrobot.resources import Resource

from unilabos.client.host.runtime.driver_creator import DeviceClassCreator, PyLabRobotCreator
from unilabos.resources.resource_tracker import (
    DeviceNodeResourceTracker,
    ResourceDict,
    ResourceDictInstance,
    ResourceTreeInstance,
    ResourceTreeSet,
    split_plr_material_tree,
)
from tests.plr_fixtures import SiteDeck, site_deck


class _Driver:
    def __init__(self, **_kwargs) -> None:
        pass


class _Handler:
    """构造时要带上 deck 的驱动（液体工作站的形状）。"""

    def __init__(self, deck: Any = None, **_kwargs) -> None:
        self.deck = deck


def _legacy_node(device_uuid: str, *, parent_uuid: str | None = None, uuid: str | None = None) -> dict:
    return {
        "id": "legacy_bottle",
        "uuid": uuid or str(uuid4()),
        "name": "legacy_bottle",
        "type": "container",
        "class": "Resource",
        "template_name": "legacy_bottle",
        "parent_uuid": parent_uuid or device_uuid,
        "config": {},
        "data": {"volume_ml": 5},
        "extra": {},
    }


def _deck_with_legacy_occupant(device_uuid: str) -> tuple[ResourceDictInstance, str, str]:
    """T1 上一个 PLR 盒子、T2 上一个旧版非 PLR 物料的台面（挂在设备下）。"""

    deck_uuid = str(uuid4())
    deck = site_deck(deck_uuid, "bench_deck", ("T1", "T2"))
    box = Resource("keep_box", size_x=20, size_y=20, size_z=20)
    box.unilabos_uuid = str(uuid4())
    deck.assign_child_resource(box, spot=0)
    dumped = ResourceTreeSet.from_plr_resources([deck], known_random_uuid=True).dump()[0]
    dumped[0]["parent_uuid"] = device_uuid
    legacy_uuid = str(uuid4())
    dumped.append(_legacy_node(device_uuid, parent_uuid=deck_uuid, uuid=legacy_uuid))
    for site in dumped[0]["sites"]:
        if site["label"] == "T2":
            site["occupied_material_uuid"] = legacy_uuid
    tree = ResourceTreeSet.load([dumped]).trees[0]
    return tree.root_node, box.unilabos_uuid, legacy_uuid


def test_split_keeps_the_plr_part_and_frees_the_legacy_site() -> None:
    device_uuid = str(uuid4())
    root, box_uuid, legacy_uuid = _deck_with_legacy_occupant(device_uuid)

    kept, pruned = split_plr_material_tree(root)

    assert [content.uuid for content in pruned] == [legacy_uuid]
    assert kept is not None
    assert [node.res_content.uuid for node in kept.children] == [box_uuid]
    sites = {site.label: site.occupied_material_uuid for site in kept.res_content.sites}
    assert sites == {"T1": box_uuid, "T2": None}
    # 输入不改：拆的是副本
    assert len(root.children) == 2
    # 剩下的部分能按 PLR 反序列化
    (deck,) = ResourceTreeSet([ResourceTreeInstance(kept)]).to_plr_resources()
    assert [child.name for child in deck.children] == ["keep_box"]


def test_split_returns_plr_trees_unchanged() -> None:
    root, _box, _legacy = _deck_with_legacy_occupant(str(uuid4()))
    kept, _pruned = split_plr_material_tree(root)

    again, pruned = split_plr_material_tree(kept)

    assert again is kept and pruned == []


def test_attach_skips_only_the_legacy_nodes(caplog) -> None:
    device_uuid = str(uuid4())
    deck, box_uuid, legacy_uuid = _deck_with_legacy_occupant(device_uuid)
    loose = ResourceDictInstance(ResourceDict.model_validate(_legacy_node(device_uuid)))
    tracker = DeviceNodeResourceTracker()
    creator = DeviceClassCreator(_Driver, [deck, loose], tracker)

    with caplog.at_level(logging.ERROR):
        creator.create_instance({})

    assert deck.res_content.uuid in tracker.uuid_to_resources
    assert box_uuid in tracker.uuid_to_resources
    assert legacy_uuid not in tracker.uuid_to_resources
    assert loose.res_content.uuid not in tracker.uuid_to_resources
    messages = [record.getMessage() for record in caplog.records]
    assert sum("legacy_bottle" in message for message in messages) == 2


def test_device_built_with_its_deck_still_initializes_when_a_legacy_material_sits_on_it(caplog) -> None:
    """液体工作站的形状：deck 是构造参数，台面上的旧物料拆下来，设备与台面照常初始化。"""

    device_uuid = str(uuid4())
    deck, box_uuid, legacy_uuid = _deck_with_legacy_occupant(device_uuid)
    tracker = DeviceNodeResourceTracker()
    creator = PyLabRobotCreator(_Handler, [deck], tracker)

    with caplog.at_level(logging.ERROR):
        handler = creator.create_instance(
            {"deck": {"_resource_child_name": "bench_deck", "_resource_type": "tests.plr_fixtures:SiteDeck"}}
        )

    assert handler is not None and isinstance(handler.deck, SiteDeck)
    assert [child.name for child in handler.deck.children] == ["keep_box"]
    assert handler.deck.unilabos_uuid == deck.res_content.uuid
    assert box_uuid in tracker.uuid_to_resources and legacy_uuid not in tracker.uuid_to_resources
    assert any("legacy_bottle" in record.getMessage() for record in caplog.records)
