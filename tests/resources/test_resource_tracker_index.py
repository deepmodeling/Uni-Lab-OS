"""DeviceNodeResourceTracker 是设备持有物料的索引；树工具函数独立于任何设备状态。"""

from __future__ import annotations

from uuid import uuid4

import pytest
from pylabrobot.resources import Coordinate, Plate, Well

from unilabos.resources.resource_tracker import (
    DeviceNodeResourceTracker,
    find_resource_by_uuid,
    replace_resource_uuids_in_tree,
    resource_uuid,
    set_uuids_by_name,
    walk_resources,
)


def _plate(name: str, well_prefix: str | None = None) -> Plate:
    plate = Plate(name, size_x=127.0, size_y=85.0, size_z=14.0, ordered_items={})
    prefix = f"{name}_" if well_prefix is None else well_prefix
    for label in ("A1", "A2"):
        well = Well(f"{prefix}{label}", size_x=9.0, size_y=9.0, size_z=10.0)
        plate.assign_child_resource(well, location=Coordinate(0, 0, 0))
    for node in walk_resources(plate):
        node.unilabos_uuid = str(uuid4())
    return plate


def test_add_indexes_whole_tree_and_lookups_hit_the_index() -> None:
    tracker = DeviceNodeResourceTracker()
    plate = _plate("plate")
    tracker.add_resource(plate)

    well = plate.children[0]
    assert tracker.resources == [plate]
    assert tracker.uuid_to_resources[well.unilabos_uuid] is well
    assert tracker.figure_resource({"uuid": well.unilabos_uuid}) is well
    assert tracker.figure_resource({"name": "plate_A1"}) is well
    assert tracker.figure_resource(well) is well  # 实例按 uuid
    assert tracker.parent_resource(well) is plate
    assert tracker.parent_resource(plate) is None

    # 实例查询只匹配同类：用 Plate 实例查 well 的 uuid 不算命中
    probe = Plate("probe", size_x=1, size_y=1, size_z=1, ordered_items={})
    probe.unilabos_uuid = well.unilabos_uuid
    assert tracker.figure_resource(probe, try_mode=True) == []
    with pytest.raises(AssertionError, match="没有找到资源"):
        tracker.figure_resource({"name": "nope"})


def test_same_name_across_plates_is_allowed_and_unique_by_uuid() -> None:
    tracker = DeviceNodeResourceTracker()
    # 两块板的孔都叫 A1 / A2：名称索引允许同名，靠 uuid 区分
    left, right = _plate("left", well_prefix=""), _plate("right", well_prefix="")
    tracker.add_resource(left)
    tracker.add_resource(right)

    both = tracker.figure_resource({"name": "A1"}, try_mode=True)
    assert {id(item) for item in both} == {id(left.children[0]), id(right.children[0])}
    with pytest.raises(AssertionError, match="找到多个资源"):
        tracker.figure_resource({"name": "A1"})
    assert tracker.figure_resource({"uuid": right.children[0].unilabos_uuid}) is right.children[0]


def test_children_assigned_without_registration_are_still_found_by_walking() -> None:
    """驱动直接 assign 的子物料没进索引：查找退回遍历持有树，行为与旧实现一致。"""
    tracker = DeviceNodeResourceTracker()
    plate = _plate("plate")
    tracker.add_resource(plate)
    late = Well("late_well", size_x=9.0, size_y=9.0, size_z=10.0)
    late.unilabos_uuid = str(uuid4())
    plate.assign_child_resource(late, location=Coordinate(9, 0, 0))

    assert late.unilabos_uuid not in tracker.uuid_to_resources
    assert tracker.figure_resource({"uuid": late.unilabos_uuid}) is late
    assert tracker.figure_resource({"name": "late_well"}) is late
    assert tracker.parent_resource(late) is plate  # PLR 自带 parent 兜底


def test_remove_clears_every_index_and_reports_unknown() -> None:
    tracker = DeviceNodeResourceTracker()
    plate = _plate("plate")
    tracker.add_resource(plate)
    well_uuid = plate.children[0].unilabos_uuid

    assert tracker.remove_resource(plate) is True
    assert tracker.resources == []
    assert well_uuid not in tracker.uuid_to_resources
    assert tracker.figure_resource({"name": "plate_A1"}, try_mode=True) == []
    assert tracker.remove_resource(plate) is False


def test_loop_set_uuid_reindexes() -> None:
    tracker = DeviceNodeResourceTracker()
    plate = Plate("fresh", size_x=1, size_y=1, size_z=1, ordered_items={})
    tracker.add_resource(plate)
    assert tracker.uuid_to_resources == {}

    new_uuid = str(uuid4())
    assert tracker.loop_set_uuid(plate, {"fresh": new_uuid}) == 1
    assert tracker.uuid_to_resources[new_uuid] is plate
    assert tracker.figure_resource({"uuid": new_uuid}) is plate


def test_tree_utilities_work_on_dicts_and_instances() -> None:
    plate = _plate("plate")
    target = plate.children[1]
    assert find_resource_by_uuid(plate, target.unilabos_uuid) is target
    assert find_resource_by_uuid([plate], "missing") is None

    payload = {
        "name": "root",
        "data": {"unilabos_uuid": "old-root"},
        "children": [{"name": "kid", "uuid": "old-kid", "parent_uuid": "old-root", "children": []}],
    }
    assert resource_uuid(payload) == "old-root"
    assert set_uuids_by_name(payload, {"kid": "named-kid"}) == 1
    assert payload["children"][0]["uuid"] == "named-kid"
    assert replace_resource_uuids_in_tree(payload, {"old-root": "new-root"}) == 1
    assert payload["uuid"] == "new-root" and payload["data"]["unilabos_uuid"] == "new-root"
    assert payload["children"][0]["parent_uuid"] == "new-root"
