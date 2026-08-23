from __future__ import annotations

import copy

import pytest

from unilabos.resources.presets.container import RegularContainer
from unilabos.resources.resource_tracker import ResourceTreeSet


def _draft(name: str) -> RegularContainer:
    resource = RegularContainer(
        name=name,
        size_x=10,
        size_y=10,
        size_z=20,
        max_volume=100,
    )
    resource.unilabos_extra = {"unilabos_resource_class": "group-container"}
    return resource


def _two_groups() -> list[list[dict]]:
    return ResourceTreeSet.from_plr_resources(
        [_draft("first"), _draft("second")],
        known_random_uuid=True,
    ).dump()


def test_nested_groups_roundtrip_one_root_per_group() -> None:
    payload = _two_groups()

    restored = ResourceTreeSet.load(payload)

    assert len(restored.trees) == 2
    assert [tree.root_node.res_content.name for tree in restored.trees] == [
        "first",
        "second",
    ]


def test_nested_group_rejects_empty_group() -> None:
    with pytest.raises(ValueError, match="非空列表"):
        ResourceTreeSet.load([[]])


def test_nested_group_rejects_multiple_roots_in_one_group() -> None:
    first, second = _two_groups()

    with pytest.raises(ValueError, match="恰好包含一个根节点"):
        ResourceTreeSet.load([first + second])


def test_nested_groups_reject_cross_group_duplicate_uuid() -> None:
    first, _second = _two_groups()
    duplicate = copy.deepcopy(first)

    with pytest.raises(ValueError, match="不同物料组之间存在重复 UUID"):
        ResourceTreeSet.load([first, duplicate])
