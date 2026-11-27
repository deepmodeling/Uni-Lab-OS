"""任意机构（龙门、门、导轨上的臂、携带物）的关节轨迹碰撞查询。"""
import json
import math

import numpy as np
import pytest

from unilabos.simulation.collision import (
    Attachment,
    Body,
    CollisionQuery,
    Geometry,
    KinematicChain,
    Mechanism,
    densify,
    receipt,
    states_digest,
    states_from_motion_feed,
)
from unilabos.simulation.collision.mechanism import transform


def pose(x=0, y=0, z=0):
    p = np.eye(4)
    p[:3, 3] = [x, y, z]
    return p


def box(name, at=None, size=(.1, .1, .1)):
    return Body(name, at if at is not None else pose(), (Geometry("box", size_m=size),))


GANTRY_URDF = """<robot name="gantry">
  <link name="base"/><link name="x"/><link name="y"/><link name="head"/><link name="finger_l"/><link name="finger_r"/>
  <joint name="gantry_x" type="prismatic"><parent link="base"/><child link="x"/><axis xyz="1 0 0"/>
    <limit lower="0" upper="1.0" effort="1" velocity="1"/></joint>
  <joint name="gantry_y" type="prismatic"><parent link="x"/><child link="y"/><axis xyz="0 1 0"/>
    <limit lower="0" upper="1.0" effort="1" velocity="1"/></joint>
  <joint name="gantry_z" type="prismatic"><parent link="y"/><child link="head"/><origin xyz="0 0 0.5"/><axis xyz="0 0 -1"/>
    <limit lower="0" upper="0.4" effort="1" velocity="1"/></joint>
  <joint name="finger_l_joint" type="prismatic"><parent link="head"/><child link="finger_l"/><axis xyz="0 1 0"/>
    <limit lower="0" upper="0.03" effort="1" velocity="1"/></joint>
  <joint name="finger_r_joint" type="prismatic"><parent link="head"/><child link="finger_r"/><axis xyz="0 -1 0"/>
    <limit lower="0" upper="0.03" effort="1" velocity="1"/><mimic joint="finger_l_joint" multiplier="1" offset="0"/></joint>
</robot>"""


def gantry():
    chain = KinematicChain.from_urdf(GANTRY_URDF)
    return Mechanism("gantry", chain, pose(), {"gantry::head": ("head", np.eye(4)),
                                               "gantry::finger_l": ("finger_l", pose(0, .02, -.05)),
                                               "gantry::finger_r": ("finger_r", pose(0, -.02, -.05))})


def test_urdf_chain_mimic_and_limits():
    chain = KinematicChain.from_urdf(GANTRY_URDF)
    assert chain.actuated == ("gantry_x", "gantry_y", "gantry_z", "finger_l_joint")
    links = chain.link_poses({"gantry_x": .3, "gantry_y": .2, "gantry_z": .1, "finger_l_joint": .01})
    assert np.allclose(links["head"][:3, 3], [.3, .2, .4])
    assert np.allclose(links["finger_r"][:3, 3], [.3, .19, .4])  # 跟随关节反向张开
    with pytest.raises(ValueError, match="joint_out_of_limit"):
        chain.link_poses({"gantry_z": .5})


def test_spec_chain_revolute_door_and_initial_pose_binding():
    chain = KinematicChain.from_spec([dict(name="lid_joint", type="revolute", parent="body", child="lid",
                                           origin=dict(xyz=(0, 0, .2)), axis=(1, 0, 0), lower=0, upper=1.6)])
    # 门板导出时（零位）中心在铰链前方 0.15 m
    lid = Mechanism.from_initial_poses("centrifuge", chain, pose(2, 0, 0), {"lid": "lid"}, {"lid": pose(2, .15, .2)})
    open_ = lid.poses({"lid_joint": math.pi / 2})["lid"]
    assert np.allclose(open_[:3, 3], [2, 0, .35], atol=1e-9)


# 夹指装在头上：机械安装面是显式许可的接触，不是自动豁免。
MOUNT = [("gantry::head", "gantry::finger_l"), ("gantry::head", "gantry::finger_r")]


def scene_with(*extra):
    return [box("gantry::head", size=(.08, .08, .08)), box("gantry::finger_l", size=(.01, .02, .06)),
            box("gantry::finger_r", size=(.01, .02, .06)), *extra]


def test_gantry_sweep_hits_a_fixed_obstacle_only_where_it_should():
    from unilabos.simulation.collision.kernel import SceneChecker
    pytest.importorskip("fcl")
    obstacle = box("rack", pose(.5, 0, .3), size=(.1, .4, .2))
    query = CollisionQuery(SceneChecker(scene_with(obstacle)), [gantry()])
    path = [{"gantry": {"gantry_x": x, "gantry_y": 0., "gantry_z": .2, "finger_l_joint": 0.}} for x in np.linspace(0, 1, 11)]
    report = query.check_states(path, allowed_pairs=MOUNT, clearance_margin_m=.3)
    assert report["status"] == "collision"
    assert report["collision_samples"] == [5]                # 只有 x=0.5 时头与机架重叠
    assert report["first_hits"][0]["pairs"][0][1] == "rack" or "rack" in report["first_hits"][0]["pairs"][0]
    assert report["min_clearance"]["min_m"] == pytest.approx(.01, abs=1e-6)  # x=0.4/0.6 时头与机架相距 1 cm
    # 抬高 z 越过机架
    high = [{"gantry": dict(s["gantry"], gantry_z=0.)} for s in path]
    assert query.check_states(high, allowed_pairs=MOUNT)["status"] == "clear"


def test_two_mechanisms_moving_toward_each_other_collide():
    from unilabos.simulation.collision.kernel import SceneChecker
    pytest.importorskip("fcl")
    rail = KinematicChain.from_spec([dict(name="slide", type="prismatic", parent="rail", child="carriage", axis=(1, 0, 0),
                                          lower=-1, upper=1)])
    west = Mechanism("west", rail, pose(-1, 0, 0), {"west::carriage": ("carriage", np.eye(4))})
    east = Mechanism("east", rail, pose(1, 0, 0) @ transform(rpy=(0, 0, math.pi)), {"east::carriage": ("carriage", np.eye(4))})
    query = CollisionQuery(SceneChecker([box("west::carriage", size=(.2, .2, .2)), box("east::carriage", size=(.2, .2, .2))]),
                           [west, east])
    path = [{"west": {"slide": s}, "east": {"slide": s}} for s in (0., .5, .85, .95)]
    report = query.check_states(path)
    assert report["collision_samples"] == [3] and report["devices"] == ["east", "west"]


def test_carried_plate_follows_the_head_and_contact_is_explicit():
    from unilabos.simulation.collision.kernel import SceneChecker
    pytest.importorskip("fcl")
    mechanism = gantry()
    plate = box("plate", pose(0, 0, .44), size=(.12, .08, .02))
    wall = box("wall", pose(.6, 0, .3), size=(.02, 1., 1.))
    query = CollisionQuery(SceneChecker(scene_with(plate, wall)), [mechanism])
    start = {"gantry_x": 0., "gantry_y": 0., "gantry_z": 0., "finger_l_joint": 0.}
    carry = Attachment.grasp("plate", mechanism, "head", start, pose(0, 0, .44))
    grip = MOUNT + [("gantry::finger_l", "plate"), ("gantry::finger_r", "plate"), ("gantry::head", "plate")]
    path = [{"gantry": dict(start, gantry_x=x)} for x in (0., .3, .54)]
    # x=0.54 时头离墙还有 1 cm，但携带的板已经插进墙里
    report = query.check_states(path, attachments=[carry], allowed_pairs=grip)
    assert report["collision_samples"] == [2]
    assert {"plate", "wall"} == set(report["first_hits"][0]["pairs"][0])
    with pytest.raises(ValueError):
        query.check_states(path, attachments=[carry, carry])
    # 不声明抓取接触时，夹爪与板的接触本身就是碰撞：许可必须显式
    assert query.check_states(path[:1], attachments=[carry], allowed_pairs=MOUNT)["status"] == "collision"


def test_each_body_has_one_owner_and_unknown_devices_fail():
    from unilabos.simulation.collision.kernel import SceneChecker
    pytest.importorskip("fcl")
    checker = SceneChecker(scene_with())
    with pytest.raises(ValueError, match="碰撞所有者"):
        CollisionQuery(checker, [gantry(), Mechanism("copy", gantry().chain, pose(), {"gantry::head": ("head", np.eye(4))})])
    query = CollisionQuery(checker, [gantry()])
    with pytest.raises(ValueError, match="运动链"):
        query.check_states([{"stranger": {"q": 0.}}])
    with pytest.raises(ValueError, match="为空"):
        query.check_states([])


def test_motion_feed_states_densify_digest_and_receipt(tmp_path):
    feed = tmp_path / "motion.jsonl"
    rows = [{"schema": "bioyond-sim-motion/1", "devices": {"gantry": ["gantry_x", "pose"]}},
            {"t": 0.0, "event": "initial", "values": {"gantry": {"gantry_x": 0.0, "pose": [1, 0]}, "other": {"q": 1}}},
            {"t": 0.5, "event": "move", "values": {"gantry": {"gantry_x": 0.3}}},
            {"t": 1.0, "event": "move", "values": {"other": {"q": 2}}}]
    feed.write_text("\n".join(json.dumps(r) for r in rows))
    states = list(states_from_motion_feed(feed, ["gantry"], joints={"gantry": ["gantry_x"]},
                                          initial={"gantry": {"gantry_y": 0.}}))
    assert [t for t, _ in states] == [0.0, 0.5, 1.0]
    assert states[1][1] == {"gantry": {"gantry_y": 0.0, "gantry_x": 0.3}} and states[2][1] == states[1][1]
    dense = list(densify([s for _, s in states], max_step=.1))
    assert len(dense) == 5 and dense[1]["gantry"]["gantry_x"] == pytest.approx(.1)
    with pytest.raises(ValueError, match="步长"):
        list(densify([s for _, s in states], max_step={"gantry_x": .1}))
    digest = states_digest(dense)
    value = receipt({"status": "clear"}, action_id="move-1", trajectory_digest=digest, scene_digest="sha256:" + "0" * 64,
                    topology="gantry", planned="sim:motion.jsonl")
    assert value.evidence_level.value == "collision_checked" and value.collision_status.value == "clear"
