"""按规范结构匹配实例，同时保留已提升的内嵌枪头身份。"""

from uuid import uuid4

import pytest
from pylabrobot.resources import Coordinate, Resource

from unilabos.resources.resource_tracker import (
    ResourceTreeSet,
    iter_serialized_plr_children,
)


def test_structural_children_match_by_name_not_runtime_order(monkeypatch):
    root = Resource("root", 10, 10, 10)
    children = [Resource(name, 1, 1, 1) for name in ("state_only", "a", "b")]
    for child in children:
        root.assign_child_resource(child, location=Coordinate(0, 0, 0))
    for node in (root, *children[1:]):
        node.unilabos_uuid = str(uuid4())
    payload = root.serialize()
    payload["children"] = list(reversed(payload["children"][1:]))
    monkeypatch.setattr(root, "serialize", lambda: payload)
    tree = ResourceTreeSet.from_plr_resources([root])
    restored_children = tree.root_nodes[0].children
    assert [node.res_content.name for node in restored_children] == ["b", "a"]
    assert [node.res_content.uuid for node in restored_children] == [
        children[2].unilabos_uuid,
        children[1].unilabos_uuid,
    ]
    assert not hasattr(children[0], "unilabos_uuid")
    from unilabos.resources.graphio import resource_plr_to_ulab

    graph = resource_plr_to_ulab(root)
    assert [node["uuid"] for node in graph["children"]] == [
        children[2].unilabos_uuid,
        children[1].unilabos_uuid,
    ]


def test_missing_serialized_child_fails_instead_of_silent_truncation():
    root = Resource("root", 10, 10, 10)
    with pytest.raises(ValueError, match="无法唯一匹配"):
        list(iter_serialized_plr_children(root, {"children": [{"name": "missing"}]}))


def test_tip_identity_and_contents_roundtrip(monkeypatch):
    from unilabos.devices.liquid_handling.prcxi.prcxi_labware import PRCXI_300ul_Tips

    rack = PRCXI_300ul_Tips("tips")
    tree = ResourceTreeSet.from_plr_resources([rack], known_random_uuid=True)
    for spot, node in zip(rack.children, tree.root_nodes[0].children):
        assert node.res_content.uuid == spot.unilabos_uuid
        assert len(node.children) == 1
        assert node.children[0].res_content.uuid == spot.children[0].unilabos_uuid
        assert "pending_tip" not in node.res_content.data
    ResourceTreeSet.from_plr_resources([rack])
    first_spot = rack.children[0]
    before = [node.res_content.model_dump() for node in tree.trees[0].get_all_nodes()]
    first_spot.get_tip().tracker.set_volume(12)
    updated = ResourceTreeSet.from_plr_resources([rack])
    assert [
        node.res_content.model_dump() for node in updated.trees[0].get_all_nodes()
    ] != before
    monkeypatch.setattr("unilabos.resources.resource_tracker.register", lambda: None)
    restored = updated.to_plr_resources(skip_devices=False)[0]
    assert restored.children[0].get_tip().tracker.get_used_volume() == 12
    assert (
        restored.children[0].get_tip().unilabos_uuid
        == first_spot.get_tip().unilabos_uuid
    )
    ResourceTreeSet.from_plr_resources([restored])
