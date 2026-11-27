"""资产行为基类与试跑命令：限位、时长、读回、安装位姿和拒绝路径。"""
import json
import math

import pytest

pytest.importorskip("pxr")
pytest.importorskip("scipy")

import sys  # noqa: E402
from pathlib import Path  # noqa: E402

from pxr import Gf, Usd, UsdGeom, UsdPhysics  # noqa: E402

from unilabos.simulation.mechanism import (  # noqa: E402
    Mechanism,
    MechanismError,
    Playback,
    evaluate_duration,
    mechanism_actions,
    plan_action,
    run_action,
)
from unilabos.simulation.mechanism_try import parse_action, try_actions  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent))
from mechanism_fixtures import author, make_package  # noqa: E402


class Drawer:
    """零导入的资产行为类：公开方法即机构动作，第一个参数是 OS 传入的关节视图。"""

    def open_door(self, joints):
        return {"move": {"drawer_joint": joints.upper("drawer_joint")}, "duration": 4.0, "state": {"door": "open"}}

    def open_and_swing(self, joints):
        return [{"move": {"drawer_joint": 0.3}, "duration": 1.0}, {"move": {"arm_joint": math.pi / 2}},
                {"duration": 0.5}]

    def swing(self, joints, angle_deg):
        return {"move": {"arm_joint": math.radians(angle_deg)}, "duration": abs(angle_deg) / 90 * 2}

    def spin(self, joints, turns):
        return {"move": {"rotor_joint": turns * 2 * math.pi}, "duration": 1.0}

    def push(self, joints):
        return {"move": {"free_joint": 0.1}, "duration": 1.0}

    def process(self, joints):
        return {"duration": 2.0}

    def bad_keys(self, joints):
        return {"goto": {"drawer_joint": 0.1}}

    def _helper(self, joints):  # 下划线开头：不是机构动作
        return None


@pytest.fixture
def drawer():
    stage = author(Usd.Stage.CreateInMemory())
    root = UsdGeom.Xformable(stage.GetPrimAtPath("/Device"))
    root.AddTranslateOp().Set((1.2, -0.4, 0.3))
    root.AddRotateZOp().Set(31.0)
    return Mechanism(stage, "/Device", Drawer())


def test_joints_report_si_limits_and_undeclared_ranges(drawer):
    joints = drawer.joints
    assert joints["MotionJoints/drawer_joint"].upper == pytest.approx(0.3)
    assert joints["MotionJoints/arm_joint"].lower == pytest.approx(-math.pi / 2)
    assert joints["MotionJoints/free_joint"].lower is None and joints["MotionJoints/rotor_joint"].upper is None
    assert drawer.joint("drawer_joint").name == "MotionJoints/drawer_joint"
    with pytest.raises(MechanismError) as error:
        drawer.upper("free_joint")
    assert error.value.code == "joint_range_undeclared"


def test_public_methods_are_actions_and_behavior_needs_no_os_import(drawer):
    assert sorted(mechanism_actions(drawer)) == ["bad_keys", "open_and_swing", "open_door", "process", "push",
                                                 "spin", "swing"]
    with pytest.raises(MechanismError) as error:
        plan_action(drawer, "bad_keys")
    assert error.value.code == "action_result_unsupported"


def test_action_runs_to_declared_limit_with_readback_and_state(drawer):
    installation = UsdGeom.Xformable(drawer.stage.GetPrimAtPath("/Device")).GetLocalTransformation()
    run = run_action(drawer, "open_door")
    assert run["plan"]["duration_s"] == 4.0 and len(run["frames"]) == 81
    assert run["frames"][40]["positions_si"]["MotionJoints/drawer_joint"] == pytest.approx(0.15)
    assert run["result"]["positions_si"]["MotionJoints/drawer_joint"] == pytest.approx(0.3)
    assert run["result"]["state"] == {"door": "open"} and run["result"]["evidence"] == "kinematic" and len(run["result"]["confirmed_steps"]) == 1
    # 机构运动不得改动实例的安装变换；刚体在世界里的位移等于安装旋转后的关节位移。
    assert UsdGeom.Xformable(drawer.stage.GetPrimAtPath("/Device")).GetLocalTransformation() == installation
    world = UsdGeom.XformCache().GetLocalToWorldTransform(drawer.stage.GetPrimAtPath("/Device/drawer"))
    offset = world.ExtractTranslation() - Gf.Vec3d(1.2, -0.4, 0.3)
    # USD 的关节限位是单精度，容差按 1e-6。
    assert offset[0] == pytest.approx(0.3 * math.cos(math.radians(31)), abs=1e-6)
    assert offset[1] == pytest.approx(0.3 * math.sin(math.radians(31)), abs=1e-6)


def test_out_of_range_target_is_rejected_not_clipped_and_stage_untouched(drawer):
    with pytest.raises(MechanismError) as error:
        run_action(drawer, "swing", {"angle_deg": 120})
    assert error.value.code == "target_out_of_range"
    assert drawer.positions["MotionJoints/arm_joint"] == 0.0 and drawer.state == {}


def test_prismatic_without_declared_stroke_is_rejected_but_continuous_revolute_moves(drawer):
    with pytest.raises(MechanismError) as error:
        plan_action(drawer, "push")
    assert error.value.code == "joint_range_undeclared"
    result = run_action(drawer, "spin", {"turns": 0.25})["result"]
    assert result["positions_si"]["MotionJoints/rotor_joint"] == pytest.approx(math.pi / 2)
    # 超过半圈时刚体位姿只能确定转角模 2π，读回按已下发位置取圈数。
    result = run_action(drawer, "spin", {"turns": 1.75})["result"]
    assert result["positions_si"]["MotionJoints/rotor_joint"] == pytest.approx(3.5 * math.pi)


def test_durations_come_from_explicit_segments_then_action_estimate(drawer):
    with pytest.raises(MechanismError) as error:
        plan_action(drawer, "open_and_swing")  # 有一步没写 duration，也没有外部给的预计时长
    assert error.value.code == "duration_undeclared"
    plan = plan_action(drawer, "open_and_swing", default_duration=3.0)
    assert [segment.duration for segment in plan.segments] == [1.0, 1.5, 0.5] and plan.duration == 3.0
    assert plan_action(drawer, "swing", {"angle_deg": 45}).duration == pytest.approx(1.0)
    process = plan_action(drawer, "process")
    assert process.duration == 2.0 and all(not segment.targets for segment in process.segments)


def test_child_joint_follows_parent_and_playback_is_repeatable(drawer):
    playback = Playback(drawer, plan_action(drawer, "open_and_swing", default_duration=3.0))
    middle = playback.at(1.75)
    assert middle["MotionJoints/drawer_joint"] == pytest.approx(0.3)
    assert middle["MotionJoints/arm_joint"] == pytest.approx(math.pi / 4)
    assert playback.at(0.5)["MotionJoints/drawer_joint"] == pytest.approx(0.15)  # 可以往回摆
    final = playback.finish()["positions_si"]
    assert final["MotionJoints/arm_joint"] == pytest.approx(math.pi / 2)
    arm = UsdGeom.XformCache().GetLocalToWorldTransform(drawer.stage.GetPrimAtPath("/Device/arm"))
    drawer_body = UsdGeom.XformCache().GetLocalToWorldTransform(drawer.stage.GetPrimAtPath("/Device/drawer"))
    # 摆臂的关节原点在抽屉上 0.1 m 处：抽屉走了，摆臂跟着走。
    assert (arm.ExtractTranslation() - drawer_body.ExtractTranslation()).GetLength() == pytest.approx(0.1, abs=1e-6)


@pytest.mark.parametrize("expression", ["__import__('os').system('x')", "angle.real", "unknown*2", "1/0", "-5"])
def test_duration_expression_only_allows_arithmetic_on_parameters(expression):
    with pytest.raises(MechanismError):
        evaluate_duration(expression, {"angle": 90})
    assert evaluate_duration("max(0.6, abs(angle)/90*1.2)", {"angle": -180}) == pytest.approx(2.4)


def test_try_lists_then_runs_package_behavior_and_stops_at_first_rejection(tmp_path):
    package = make_package(tmp_path)
    listed = try_actions(package, None, [])
    assert listed["status"] == "listed" and sorted(listed["actions"]) == ["open_door", "swing"]
    assert listed["actions"]["swing"]["signature"] == "(angle_deg)"
    report = try_actions(package, None, [parse_action("open_door"), parse_action('swing={"angle_deg": 120}'),
                                         parse_action("open_door")])
    assert report["status"] == "rejected" and report["installation_preserved"] is True
    assert [run["status"] for run in report["runs"]] == ["passed", "rejected"]
    assert report["runs"][0]["geometry_changed"] is True and report["runs"][0]["readback_max_deviation"] < 1e-9
    assert report["runs"][1]["code"] == "target_out_of_range"


def test_try_rejects_missing_behavior_class(tmp_path):
    package = make_package(tmp_path)
    other = tmp_path / "other.py"
    other.write_text("VALUE = 1\n", encoding="utf8")
    with pytest.raises(ValueError):
        try_actions(package, f"{other}:Missing", [])


class Handler:
    """带物料的动作：用库位视图算位置，用 carry 提示物料画面上的走向，用物料视图取物料几何。"""

    def present(self, joints, sites):
        travel = sites.along("drawer_joint", "handoff") - sites.along("drawer_joint", "tray")
        return [{"move": {"drawer_joint": joints.position("drawer_joint") + travel}, "duration": 1.0},
                {"duration": 0.5, "carry": {"from": "tray", "to": "handoff"}}]

    def point_at(self, joints, sites, target):
        delta = sites.angle_about("arm_joint", target) - sites.angle_about("arm_joint", "grip")
        return {"move": {"arm_joint": joints.position("arm_joint") + delta}, "duration": 1.0}

    def lower_onto(self, joints, sites, materials, plate):
        return {"move": {"drawer_joint": materials.get("plate", "size_z")}, "duration": 1.0}

    def bad_carry(self, joints, sites):
        return {"duration": 1.0, "carry": {"from": "tray", "to": "nowhere"}}


@pytest.fixture
def handler():
    stage = author(Usd.Stage.CreateInMemory())
    root = UsdGeom.Xformable(stage.GetPrimAtPath("/Device"))
    root.AddTranslateOp().Set((1.2, -0.4, 0.3))
    root.AddRotateZOp().Set(31.0)
    return Mechanism(stage, "/Device", Handler())


def test_sites_are_named_by_path_without_sites_level_and_given_in_instance_frame(handler):
    from unilabos.simulation.mechanism import SiteView, action_parameters, mechanism_actions

    view = SiteView(handler)
    assert view.names == ["arm/grip", "deck_origin", "drawer/tray", "handoff", "head/tool"]
    assert view.position("handoff") == pytest.approx((0.35, 0.0, 0.2), abs=1e-6)  # 与实例的安装位姿无关
    assert view.position("tray") == pytest.approx((0.05, 0.0, 0.1), abs=1e-6)   # 结尾唯一即可
    assert view.has("grip") and not view.has("missing")
    with pytest.raises(MechanismError) as error:
        view.position("missing")
    assert error.value.code == "site_unknown"
    # 注入的视图不算动作入参
    assert action_parameters(mechanism_actions(handler)["lower_onto"]) == ["plate"]


def test_site_helpers_let_behavior_align_prismatic_and_revolute_joints(handler):
    from unilabos.simulation.mechanism import SiteView

    run = run_action(handler, "present")
    assert run["result"]["positions_si"]["MotionJoints/drawer_joint"] == pytest.approx(0.3, abs=1e-6)
    # carry 在所在段的确认步核对两端库位并记下读回后的实际位置；时间是那一步开始的时刻，不改库存（库存由 OS 结算）
    assert run["result"]["carries"] == [{"t": 1.0, "from": "drawer/tray", "to": "handoff", "basis": "kinematic", "from_position": [0.35, 0.0, 0.1], "to_position": [0.35, 0.0, 0.2]}]
    view = SiteView(handler)
    assert view.along("drawer_joint", "tray") == pytest.approx(view.along("drawer_joint", "handoff"), abs=1e-6)
    before = view.angle_about("arm_joint", "grip")
    handler.set_positions({"arm_joint": 0.4})
    assert view.angle_about("arm_joint", "grip") - before == pytest.approx(0.4, abs=1e-6)


def test_carry_must_name_known_sites(handler):
    with pytest.raises(MechanismError) as error:
        plan_action(handler, "bad_carry")
    assert error.value.code == "site_unknown"


def test_material_geometry_comes_from_the_material_view_and_is_never_defaulted(handler):
    with pytest.raises(MechanismError) as error:
        plan_action(handler, "lower_onto", {"plate": "ignored"})
    assert error.value.code == "material_geometry_undeclared"
    plan = plan_action(handler, "lower_onto", {"plate": "ignored"}, materials={"plate": {"size_z": 0.0144}})
    assert plan.segments[0].targets == {"MotionJoints/drawer_joint": 0.0144}


GRAPH = {"nodes": [
    {"id": "station", "type": "device", "parent": None, "position": {"x": 999, "y": 999, "z": 999}, "config": {}},
    {"id": "deck", "type": "deck", "parent": "station", "position": {"x": 0, "y": 0, "z": 0}, "config": {}},
    {"id": "carrier", "type": "resource", "parent": "deck", "position": {"x": 100, "y": 0, "z": 50},
     "config": {"size_x": 135, "size_y": 497, "size_z": 130}},
    {"id": "plate", "type": "resource", "parent": "carrier", "position": {"x": 4, "y": 0, "z": 20},
     "config": {"size_x": 127.76, "size_y": 85.48, "size_z": 14.2}},
    {"id": "well_A1", "type": "resource", "parent": "plate", "position": {"x": 10, "y": -3, "z": 3},
     "config": {"type": "Well", "size_x": 6, "size_y": 6, "size_z": 10, "material_z_thickness": 0.5}},
    {"id": "tip_A1", "type": "resource", "parent": "carrier", "position": {"x": 0, "y": -3, "z": 100},
     "config": {"type": "TipSpot", "size_x": 9, "size_y": 6, "size_z": 0,
                "prototype_tip": {"total_tip_length": 59.9, "fitting_depth": 8}}},
    {"id": "tilted", "type": "resource", "parent": "deck", "position": {"x": 0, "y": 0, "z": 0},
     "config": {"size_x": 1, "size_y": 1, "size_z": 1, "rotation": {"x": 0, "y": 0, "z": 90}}},
]}


class Pipettor:
    def touch(self, joints, sites, materials, resource_name):
        target = sites.from_frame("deck_origin", materials.point(resource_name, "bottom_center"))
        return [{"move": sites.reach("tool", target, clearance=0.02), "duration": 1.0},
                {"move": sites.reach("tool", target), "duration": 0.5}]

    def swing_tool(self, joints, sites):
        return {"move": sites.reach("grip", (0.0, 0.0, 0.0)), "duration": 1.0}


def test_resource_points_chain_relative_positions_up_to_the_deck_in_metres():
    from unilabos.simulation.mechanism_resources import ResourcePoints

    points = ResourcePoints.from_graph(GRAPH)
    assert points.point("well_A1", "origin") == pytest.approx((0.114, -0.003, 0.073))  # 设备节点的位置不计入
    assert points.point("well_A1", "bottom_center") == pytest.approx((0.117, 0.0, 0.0735))
    assert points.point("well_A1", "top_center") == pytest.approx((0.117, 0.0, 0.083))
    assert points.attribute("tip_A1", "tip_length") == pytest.approx(0.0599)
    assert points.attribute("tip_A1", "fitting_depth") == pytest.approx(0.008)
    for name, code in (("missing", "resource_unknown"), ("tilted", "resource_rotation_unsupported")):
        with pytest.raises(MechanismError) as error:
            points.point(name, "origin")
        assert error.value.code == code
    with pytest.raises(MechanismError) as error:
        points.attribute("well_A1", "tip_length")
    assert error.value.code == "material_geometry_undeclared"


def test_behavior_reaches_a_resource_point_with_a_cartesian_chain():
    from unilabos.simulation.mechanism import SiteView
    from unilabos.simulation.mechanism_resources import ResourcePoints

    stage = author(Usd.Stage.CreateInMemory())
    root = UsdGeom.Xformable(stage.GetPrimAtPath("/Device"))
    root.AddTranslateOp().Set((1.2, -0.4, 0.3))
    root.AddRotateZOp().Set(31.0)
    mechanism = Mechanism(stage, "/Device", Pipettor(), ResourcePoints.from_graph(GRAPH))
    result = run_action(mechanism, "touch", {"resource_name": "well_A1"})["result"]
    # 台面点 (0.117, 0, 0.0735) + 台面基准 (0.1, 0, 0.05) = 实例坐标 (0.217, 0, 0.1235)
    assert SiteView(mechanism).position("tool") == pytest.approx((0.217, 0.0, 0.1235), abs=1e-6)
    assert result["positions_si"]["MotionJoints/gantry_x"] == pytest.approx(0.217, abs=1e-6)
    assert result["positions_si"]["MotionJoints/head_z"] == pytest.approx(0.1235, abs=1e-6)
    # 工具点和机座之间有转动关节的机构不是直角机构：拒绝，交给逆解
    with pytest.raises(MechanismError) as error:
        plan_action(mechanism, "swing_tool")
    assert error.value.code == "reach_needs_cartesian_chain"


# ---------------------------------------------------------------- 相对转动：spin / advance
class Spinner:
    """转子按角速度转（连续关节）；卷轴按增量转（声明 0–360°，按整圈折回）；摆臂范围不足整圈。"""

    def run(self, joints, rpm, seconds):
        omega = rpm * 2 * math.pi / 60
        return [{"spin": {"rotor_joint": [0.0, omega]}, "duration": 2.0},
                {"spin": {"rotor_joint": omega}, "duration": seconds},
                {"spin": {"rotor_joint": [omega, 0.0]}, "duration": 2.0}]

    def feed(self, joints, angle):
        return {"advance": {"reel_joint": angle}, "duration": 1.0}

    def swing_by(self, joints, angle):
        return {"advance": {"arm_joint": angle}, "duration": 1.0}

    def spin_limited(self, joints):
        return {"spin": {"reel_joint": 1.0}, "duration": 1.0}

    def spin_and_move(self, joints):
        return {"move": {"rotor_joint": 1.0}, "spin": {"rotor_joint": 1.0}, "duration": 1.0}

    def spin_prismatic(self, joints):
        return {"advance": {"drawer_joint": 0.1}, "duration": 1.0}


@pytest.fixture
def spinner():
    from mechanism_fixtures import _joint
    stage = author(Usd.Stage.CreateInMemory())
    UsdGeom.Xform.Define(stage, "/Device/reel")
    _joint(stage, "/Device/MotionJoints/reel_joint", "revolute", "/Device/base", "/Device/reel", "X", 0.0, 360.0)
    return Mechanism(stage, "/Device", Spinner())


def test_spin_integrates_angular_velocity(spinner):
    run = run_action(spinner, "run", {"rpm": 60, "seconds": 3.0}, rate_hz=10)
    omega = 2 * math.pi
    expected = omega * 1.0 + omega * 3.0 + omega * 1.0   # 两段线性加减速各走 ω·T/2
    assert run["result"]["positions_si"]["MotionJoints/rotor_joint"] == pytest.approx(expected)
    assert run["plan"]["segments"][1]["spin"]["MotionJoints/rotor_joint"] == pytest.approx([omega, omega])
    # 加速段中点：ω·t²/(2T)
    spinner.set_positions({"rotor_joint": 0.0})
    playback = Playback(spinner, plan_action(spinner, "run", {"rpm": 60, "seconds": 3.0}))
    assert playback.targets_at(1.0)["MotionJoints/rotor_joint"] == pytest.approx(omega * 1.0 / 4)


def test_advance_wraps_full_turn_joint_without_widening_limits(spinner):
    first = run_action(spinner, "feed", {"angle": 4.0})
    assert first["result"]["positions_si"]["MotionJoints/reel_joint"] == pytest.approx(4.0)
    second = run_action(spinner, "feed", {"angle": 4.0})
    assert second["result"]["positions_si"]["MotionJoints/reel_joint"] == pytest.approx(8.0 - 2 * math.pi)
    assert second["plan"]["segments"][0]["targets"]["MotionJoints/reel_joint"] == pytest.approx(8.0 - 2 * math.pi)
    upper = spinner.joints["MotionJoints/reel_joint"].upper
    assert all(0.0 - 1e-9 <= f["positions_si"]["MotionJoints/reel_joint"] <= upper + 1e-9 for f in second["frames"])


def test_advance_on_short_range_joint_rejected_out_of_range(spinner):
    assert run_action(spinner, "swing_by", {"angle": 1.0})["result"]["positions_si"]["MotionJoints/arm_joint"] \
        == pytest.approx(1.0)
    with pytest.raises(MechanismError) as error:
        plan_action(spinner, "swing_by", {"angle": 1.0})
    assert error.value.code == "target_out_of_range"


@pytest.mark.parametrize("action, code", [("spin_limited", "spin_needs_continuous_joint"),
                                          ("spin_and_move", "target_duplicate"),
                                          ("spin_prismatic", "advance_needs_revolute_joint")])
def test_relative_motion_rejections(spinner, action, code):
    with pytest.raises(MechanismError) as error:
        plan_action(spinner, action)
    assert error.value.code == code
