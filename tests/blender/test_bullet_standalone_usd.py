"""USD → 独立 Bullet 的建体路径测试（需要 pxr + numpy）。

覆盖 ``usd_compat.Body`` 的 compound/convexHull 与 SDF(GImpact) 两条路径；
资产放置由 ``world_from_root`` 提供，和 SZLab 消费者的用法一致。

pxr 只在专用 USD 环境里可用，默认跳过；显式设置
``UNILABOS_BULLET_STANDALONE_USD=1`` 后才导入 pxr（避免普通解释器加载不完整的 pxr）。
"""

import os

import numpy as np
import pytest

if os.environ.get("UNILABOS_BULLET_STANDALONE_USD") != "1":
    pytest.skip("设置 UNILABOS_BULLET_STANDALONE_USD=1 后运行 USD 集成测试", allow_module_level=True)

pytest.importorskip("pxr")

from pxr import Gf, Usd, UsdGeom, UsdPhysics

from unilabos.simulation.bullet_standalone import compat, usd_compat


def placement(position):
    matrix = np.eye(4)
    matrix[:3, 3] = np.asarray(position, dtype=float)
    return matrix


def make_cube(stage, path, size, mass=0.0):
    geom = UsdGeom.Cube.Define(stage, path)
    geom.GetSizeAttr().Set(size)
    prim = geom.GetPrim()
    UsdPhysics.CollisionAPI.Apply(prim)
    UsdPhysics.RigidBodyAPI.Apply(prim)
    UsdPhysics.MassAPI.Apply(prim).CreateMassAttr(float(mass))
    return prim


def test_usd_compat_star_import_surface(abi):
    """SZLab 的 `from ...bullet_usd import *` 消费者不需要额外 import。"""

    exported = set(usd_compat.__all__)
    expected = {
        "np", "Usd", "UsdGeom", "UsdPhysics", "B", "C", "E", "V3", "V4",
        "World", "Body", "constraint", "matrix", "points", "shape", "pose", "velocity",
    }
    assert expected.issubset(exported)
    assert all(hasattr(usd_compat, name) for name in expected)


def test_usd_cube_body_falls_and_reports_named_contacts(abi):
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Xform.Define(stage, "/World")
    ground_prim = make_cube(stage, "/World/ground", 0.1)
    box_prim = make_cube(stage, "/World/box", 0.04, mass=0.1)

    world = compat.World()
    ground = usd_compat.Body(world, "ground", ground_prim, placement((0, 0, -0.05)), static=True)
    box = usd_compat.Body(world, "box", box_prim, placement((0, 0, 0.30)))
    assert ground.colliders == 1 and box.colliders == 1

    for _ in range(480):
        world.step()

    pose = box.pose()
    assert abs(pose[2, 3] - 0.02) < 2e-3, pose
    rows = [row for row in world.contacts() if {row["a"], row["b"]} == {"ground", "box"}]
    assert 0.5 < sum(row["normal_force_n"] for row in rows) < 2.0
    assert abs(box.velocity()[2]) < 1e-2


def test_usd_compat_hinge_constraint_rotates_about_pivot(abi):
    """usd_compat.constraint 的 hinge 轴映射：Z 轴铰链，0.25 s 后绕 pivot 旋转。"""

    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Xform.Define(stage, "/World")
    anchor_prim = make_cube(stage, "/World/anchor", 0.02)
    rod_prim = make_cube(stage, "/World/rod", 0.1, mass=0.05)

    world = compat.World(gravity=(0.0, 0.0, 0.0))
    anchor = usd_compat.Body(world, "anchor", anchor_prim, placement((0, 0, 0)), static=True)
    rod = usd_compat.Body(world, "rod", rod_prim, placement((0.05, 0, 0)))
    compat.E.lab_ignore(anchor.ptr, rod.ptr, 1)

    usd_compat.constraint(world, "hinge", anchor.ptr, rod.ptr, (0.0, 0.0, 0.0), (0.0, 0.0, 1.0))
    compat.B.RB_body_set_angular_velocity(rod.ptr, compat.V3(0.0, 0.0, -2.0))
    for _ in range(60):
        world.step()

    pose = rod.pose()
    radius = float(np.hypot(pose[0, 3], pose[1, 3]))
    angle = float(np.arctan2(pose[1, 3], pose[0, 3]))
    assert abs(radius - 0.05) < 2e-3, radius
    assert abs(pose[2, 3]) < 5e-3
    assert -0.45 < angle < -0.15, angle


def test_usd_sdf_trimesh_body_collides_via_gimpact(abi):
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Xform.Define(stage, "/World")
    plate = UsdGeom.Mesh.Define(stage, "/World/plate")
    corners = [(-0.2, -0.2), (0.2, -0.2), (0.2, 0.2), (-0.2, 0.2)]
    points = [Gf.Vec3f(x, y, z) for z in (-0.01, 0.0) for (x, y) in corners]
    plate.GetPointsAttr().Set(points)
    plate.GetFaceVertexCountsAttr().Set([4] * 6)
    plate.GetFaceVertexIndicesAttr().Set(
        [0, 3, 2, 1, 4, 5, 6, 7, 0, 1, 5, 4, 1, 2, 6, 5, 2, 3, 7, 6, 3, 0, 4, 7]
    )
    plate_prim = plate.GetPrim()
    UsdPhysics.CollisionAPI.Apply(plate_prim)
    UsdPhysics.MeshCollisionAPI.Apply(plate_prim).CreateApproximationAttr("sdf")
    UsdPhysics.MassAPI.Apply(plate_prim).CreateMassAttr(0.0)

    box_prim = make_cube(stage, "/World/box", 0.04, mass=0.1)
    world = compat.World()
    usd_compat.Body(world, "plate", plate_prim, placement((0, 0, 0)), static=True)
    box = usd_compat.Body(world, "box", box_prim, placement((0, 0, 0.20)))

    for _ in range(480):
        world.step()

    z = box.pose()[2, 3]
    assert abs(z - 0.02) < 4e-3, z
    names = {row["a"] for row in world.contacts()} | {row["b"] for row in world.contacts()}
    assert "plate" in names and "box" in names
