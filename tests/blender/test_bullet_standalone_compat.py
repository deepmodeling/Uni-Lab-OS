"""bullet_native 兼容层（独立 DLL）的模块面与 World 语义测试。"""

import importlib
import math

import numpy as np
import pytest


@pytest.fixture(scope="module")
def compat(abi):
    return importlib.import_module("unilabos.simulation.bullet_standalone.compat")


def test_compat_exports_bullet_native_surface(compat):
    expected = {"B", "E", "C", "V3", "V4", "Contact", "World", "pose", "velocity", "quat"}
    assert expected.issubset(set(dir(compat)))
    assert compat.B is compat.E


def test_world_box_step_and_named_contacts(compat):
    world = compat.World()
    ground = world.box("ground", (0.5, 0.5, 0.05), (0.0, 0.0, -0.05))
    box = world.box("box", (0.02, 0.02, 0.02), (0.0, 0.0, 0.30), mass=0.1)
    for _ in range(480):
        world.step()

    pose = compat.pose(box)
    assert pose.shape == (4, 4)
    assert np.allclose(pose[3], [0.0, 0.0, 0.0, 1.0])
    assert abs(pose[2, 3] - 0.02) < 2e-3
    assert world.tick == 480
    assert world.names[ground] == "ground" and world.names[box] == "box"

    rows = world.contacts()
    assert rows and set(rows[0]) == {
        "a", "b", "position", "normal", "separation", "normal_force_n"
    }
    pair = [row for row in rows if {row["a"], row["b"]} == {"ground", "box"}]
    assert pair
    # 流形每点一份冲量，合力（×240 Hz）应接近 0.1 kg 的重量。
    assert 0.5 < sum(row["normal_force_n"] for row in pair) < 2.0
    assert abs(compat.velocity(box)[2]) < 1e-2


def test_pose_quat_round_trip_for_yaw(compat):
    angle = math.radians(30.0)
    orientation = (compat.V4)(math.cos(angle / 2), 0.0, 0.0, math.sin(angle / 2))
    shape = compat.B.RB_shape_new_box(0.01, 0.01, 0.01)
    body = compat.B.RB_body_new(shape, (compat.V3)(0.0, 0.0, 0.0), orientation)
    try:
        matrix = compat.pose(body)
        expected = np.array([
            [math.cos(angle), -math.sin(angle), 0.0, 0.0],
            [math.sin(angle), math.cos(angle), 0.0, 0.0],
            [0.0, 0.0, 1.0, 0.0],
            [0.0, 0.0, 0.0, 1.0],
        ])
        assert np.allclose(matrix, expected, atol=1e-6)

        w, x, y, z = compat.quat(matrix)
        assert abs(abs(w) - math.cos(angle / 2)) < 1e-6
        assert abs(abs(z) - math.sin(angle / 2)) < 1e-6
        assert abs(x) < 1e-9 and abs(y) < 1e-9
    finally:
        compat.B.RB_body_delete(body)
        compat.B.RB_shape_delete(shape)


def test_axis_quaternion_helper_used_for_constraints(compat):
    qz = compat.axis_quaternion("hinge", (0.0, 0.0, 1.0))
    assert qz == pytest.approx((1.0, 0.0, 0.0, 0.0), abs=1e-7)

    # hinge 以局部 Z 轴为基准：(0,0,1) -> (0,1,0) 是绕 X 轴 -90°。
    w, x, y, z = compat.axis_quaternion("hinge", (0.0, 1.0, 0.0))
    assert abs(w - math.cos(-math.pi / 4)) < 1e-6
    assert abs(x - math.sin(-math.pi / 4)) < 1e-6
    assert abs(y) < 1e-9 and abs(z) < 1e-9

    # slider 以局部 X 轴为基准：(1,0,0) -> (0,1,0) 是绕 Z 轴 +90°。
    w, x, y, z = compat.axis_quaternion("slider", (0.0, 1.0, 0.0))
    assert abs(w - math.cos(math.pi / 4)) < 1e-6
    assert abs(z - math.sin(math.pi / 4)) < 1e-6
    assert abs(x) < 1e-9 and abs(y) < 1e-9
