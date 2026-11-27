"""Blender 内运行：``bullet_usd`` 经回退后端驱动 USD 刚体的端到端冒烟。

用法（Blender 5.1.2 / Windows）::

    & 'D:\\blender\\blender.exe' --background --factory-startup --python tests/blender/bullet_usd_smoke.py

``bullet_usd`` 是 OS 侧真实消费者（``Body`` / ``shape`` / ``constraint``），只依赖
Blender 内置的 ``pxr`` 与 ``bullet_native``。本脚本用内存 USD 舞台构造“地面 + 落体”，
验证形状转换、质量/惯量回写、位姿读回、接触与几何射线都走 ``bullet_native``
（Windows 上即 ``bullet_bpy`` 回退路径）。成功打印 ``BULLET_USD_SMOKE_OK``。
"""
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pxr import Usd, UsdGeom, UsdPhysics  # noqa: E402

from unilabos.simulation import bullet_native  # noqa: E402
from unilabos.simulation.bullet_usd import Body, matrix  # noqa: E402


def cube(stage, path, size, position, mass=None):
    """建立“刚体根 prim + 几何子 prim”的 USD 立方体，返回根 prim。

    与真实资产一致：RigidBodyAPI/MassAPI 在根 prim，CollisionAPI 在几何子 prim；
    ``Body`` 从根 prim 读质量，从 ``Usd.PrimRange`` 里收集碰撞体。
    """
    xform = UsdGeom.Xform.Define(stage, path)
    xform.AddTranslateOp().Set(tuple(float(value) for value in position))
    root = xform.GetPrim()
    UsdPhysics.RigidBodyAPI.Apply(root)
    if mass is not None:
        mass_api = UsdPhysics.MassAPI.Apply(root)
        mass_api.GetMassAttr().Set(float(mass))
        inertia = float(mass) * (float(size) ** 2) / 6.0
        mass_api.GetDiagonalInertiaAttr().Set((inertia, inertia, inertia))
    geometry = UsdGeom.Cube.Define(stage, path + "/geometry")
    geometry.GetSizeAttr().Set(float(size))
    UsdPhysics.CollisionAPI.Apply(geometry.GetPrim())
    return root


def main():
    stage = Usd.Stage.CreateInMemory()
    ground_prim = cube(stage, "/World/ground", 2.0, (0.0, 0.0, -1.0))
    body_prim = cube(stage, "/World/body", 0.1, (0.0, 0.0, 0.5), mass=1.0)

    world = bullet_native.World()
    ground = Body(world, "ground", ground_prim, matrix(ground_prim), static=True)
    body = Body(world, "body", body_prim, matrix(body_prim))
    print(
        "BULLET_USD_SMOKE_SCENE "
        f"ground_colliders={ground.colliders} body_colliders={body.colliders} "
        f"mass={body.mass} backend={bullet_native.BACKEND}",
        flush=True,
    )

    heights = []
    for _ in range(480):
        world.step()
        heights.append(float(body.pose()[2, 3]))
    contacts = world.contacts()
    print(
        "BULLET_USD_SMOKE_SIM "
        f"start_z={heights[0]:.4f} final_z={heights[-1]:.4f} min_z={min(heights):.4f} "
        f"contacts={len(contacts)}",
        flush=True,
    )
    assert heights[0] > 0.45, f"起始位姿异常: {heights[0]}"
    assert abs(heights[-1] - 0.05) < 0.005, f"落体未停在接触高度: {heights[-1]}"
    assert contacts, "落地后应报告接触"
    total = sum(row["normal_force_n"] for row in contacts)
    assert 5.0 < total < 15.0, f"静止支撑力异常: {total}"

    from unilabos.simulation.bullet_native import C, E

    distance = C.c_double(-1.0)
    hit = E.lab_ray(
        world.ptr,
        (C.c_double * 3)(0.0, 0.0, 0.12),
        (C.c_double * 3)(0.0, 0.0, 0.0),
        0,
        C.byref(distance),
    )
    assert hit == body.ptr, f"射线未命中落体: {hit}"
    print(
        "BULLET_USD_SMOKE_OK "
        f"backend={bullet_native.BACKEND} final_z={heights[-1]:.4f} "
        f"contact_pair={sorted({(row['a'], row['b']) for row in contacts})} "
        f"normal_force_n={total:.3f} ray_distance={distance.value:.4f}",
        flush=True,
    )


main()
