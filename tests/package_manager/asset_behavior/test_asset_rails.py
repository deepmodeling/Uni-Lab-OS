"""轨道：格号 ↔ 位姿、占格、重叠（与 PLR 的 TecanDeck/HamiltonDeck 同一套做法）。"""
import pytest

from unilabos.package_manager.package_distribution.asset_behavior import (
    carrier_at,
    carrier_pose,
    check_layout,
    covered,
    grid_at,
    mount_pose,
    rail_mount_from_asset,
    rails_from_asset,
)

# 轨道沿设备资产 −x 走，挂点坐标系相对资产绕 z 转 180°（Fluent 的实际情形）。
DEVICE = {"parameters": {"rails": {"schema": "unilab.rails/1", "rails": [{
    "id": "WorktableGrids", "count": 52, "first": 1, "pitch_mm": 25.0, "step_mm": [-25.0, 0.0, 0.0],
    "asset_from_first_mount": [[-1.0, 0.0, 0.0, 647.0], [0.0, -1.0, 0.0, 212.85], [0.0, 0.0, 1.0, 213.9], [0.0, 0.0, 0.0, 1.0]]}]}}}
CARRIER = {"parameters": {"rail_mount": {"schema": "unilab.rail-mount/1", "rail": "WorktableGrids", "pitch_mm": 25.0, "span": 6,
    "asset_from_mount": [[-1.0, 0.0, 0.0, 74.75], [0.0, -1.0, 0.0, 303.25], [0.0, 0.0, 1.0, -15.8], [0.0, 0.0, 0.0, 1.0]]}}}


def test_grid_gives_the_pose_and_the_pose_gives_the_grid_back():
    rail, mount = rails_from_asset(DEVICE)["WorktableGrids"], rail_mount_from_asset(CARRIER)
    assert [row[3] for row in mount_pose(rail, 27)[:3]] == [647.0 - 26 * 25.0, 212.85, 213.9]
    pose = carrier_pose(rail, 27, mount)
    # 挂点转了 180°，载架又相对挂点转 180°，合起来与设备同向；位置 = 挂点 + R·偏移。
    assert [round(pose[i][i], 9) for i in range(3)] == [1.0, 1.0, 1.0]
    assert [round(pose[i][3], 6) for i in range(3)] == [-3.0 - 74.75, 212.85 - 303.25, 213.9 - 15.8]
    grid, off = grid_at(rail, pose, mount)
    assert abs(grid - 27) < 1e-9 and off < 1e-9
    pose[0][3] -= 10.0      # 挪 10 mm：不在格上
    pose[1][3] += 3.0       # 再偏出这一排 3 mm
    grid, off = grid_at(rail, pose, mount)
    assert abs(grid - 27.4) < 1e-9 and abs(off - 3.0) < 1e-9


def test_a_carrier_answers_for_every_grid_it_covers_and_overlap_is_by_interval():
    layout = [{"name": "wash", "grid": 1, "span": 2}, {"name": "nest_a", "grid": 3, "span": 6}, {"name": "nest_b", "grid": 27, "span": 6}]
    assert list(covered(27, 6)) == [27, 28, 29, 30, 31, 32]
    assert [carrier_at(layout, grid)["name"] for grid in (27, 30, 32)] == ["nest_b"] * 3
    assert carrier_at(layout, 33) is None and carrier_at(layout, 9) is None
    rail = rails_from_asset(DEVICE)["WorktableGrids"]
    assert check_layout(rail, layout) == []
    assert check_layout(rail, layout + [{"name": "late", "grid": 31, "span": 2}]) == ["late at grid 31 overlaps nest_b"]
    assert check_layout(rail, [{"name": "edge", "grid": 48, "span": 6}]) == ["edge covers grids 48..53, the rail has 1..52"]


def test_what_does_not_fit_is_refused_not_placed():
    rail, mount = rails_from_asset(DEVICE)["WorktableGrids"], rail_mount_from_asset(CARRIER)
    with pytest.raises(ValueError, match="has grids 1..52, not 53"):
        mount_pose(rail, 53)
    with pytest.raises(ValueError, match="not 53"):
        carrier_pose(rail, 48, mount)                       # 第 48 格起占 6 格，伸出轨道
    with pytest.raises(ValueError, match="does not fit rail"):
        carrier_pose(rail, 3, {**mount, "pitch_mm": 22.5})   # Hamilton 的载架挂不上 Tecan 的轨道
    with pytest.raises(ValueError, match="its pitch says"):
        rails_from_asset({"parameters": {"rails": {"schema": "unilab.rails/1", "rails": [
            {**DEVICE["parameters"]["rails"]["rails"][0], "step_mm": [-22.5, 0.0, 0.0]}]}}})
    with pytest.raises(ValueError, match="schema"):
        rail_mount_from_asset({"parameters": {"rail_mount": {"schema": "other"}}})
    assert rails_from_asset({}) == {} and rail_mount_from_asset({"parameters": {}}) is None
