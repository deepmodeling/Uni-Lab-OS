"""Blender bpy 刚体回退后端（``bullet_bpy``）与 ``bullet_native`` 回退路径的 TDD。

本文件只在 Blender 内置 Python 内可执行（需要 ``bpy``）：:

    & 'D:\\blender\\blender.exe' --background --factory-startup \
        --python tests/app/test_simulation_bullet_bpy.py

被 pytest 收集时若缺少 ``bpy`` 则整文件跳过，避免在普通 CI 解释器里导入 Blender。

覆盖：
1. 盒子下落并形成静止接触（接触位置/分离距离/近似法向力）；
2. ``E.lab_ray`` 命中、超距未命中、过近过滤与被排除刚体；
3. fixed 与 hinge（含 motor / 限位）约束；
4. 形状（box / convex hull / trimesh / compound）、版本与回退路径不被硬 gate。

断言只依赖 ``bullet_native`` 的公开接口（``World`` / ``B`` / ``E`` / ``pose``），
不导入 ``bullet_usd``（其依赖 pxr）。
"""
import importlib
import json
import math
import os
import sys
import traceback
from pathlib import Path

import numpy as np

try:  # pragma: no cover - 仅在 Blender 内为真
    import pytest
except ImportError:  # Blender 自带 Python 通常没有 pytest
    pytest = None

if pytest is not None:
    bpy = pytest.importorskip("bpy", reason="需要 Blender 内置 Python 才能执行刚体回退后端")
else:  # pragma: no cover - Blender 内执行
    import bpy

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ENV_KEYS = (
    "UNILABOS_BULLET_BACKEND",
    "UNILABOS_BULLET_HEADERS",
    "UNILABOS_BULLET_CACHE",
    "UNILABOS_BULLET_STANDALONE",
)


def build_hash():
    """返回当前 Blender 构建哈希文本（不同版本可能是 bytes 或 str）。"""
    value = bpy.app.build_hash
    return value.decode() if isinstance(value, bytes) else str(value)


def world():
    """导入回退世界并新建一个干净的 Bullet 场景。"""
    from unilabos.simulation import bullet_native

    return bullet_native, bullet_native.World()


def angle_about_z(body):
    """返回刚体绕世界 Z 轴的角度（弧度，用于铰链/电机断言）。"""
    from unilabos.simulation.bullet_native import pose

    position = pose(body)[:3, 3]
    return math.atan2(position[1], position[0])


def test_box_drop_contact():
    """盒子自由下落并停在地面，接触带位置、分离距离与近似法向力。"""
    bn, scene = world()
    scene.box("ground", (1.0, 1.0, 0.05), (0.0, 0.0, -0.05), mask=1)
    cube = scene.box("cube", (0.05, 0.05, 0.05), (0.0, 0.0, 0.8), mass=1.0)
    heights = []
    vertical_speeds = []
    for _ in range(480):
        scene.step()
        heights.append(float(bn.pose(cube)[2, 3]))
        vertical_speeds.append(float(bn.velocity(cube)[2]))

    assert heights[0] > 0.7, f"起始高度异常: {heights[0]}"
    assert min(heights) > 0.0, f"盒子穿透地面: min_z={min(heights)}"
    assert abs(heights[-1] - 0.05) < 0.005, f"未停在接触高度: z={heights[-1]}"
    assert abs(heights[-1] - heights[-2]) < 1e-4, "接触后仍在移动"
    # 自由落体第 48 步（0.2s）的垂向速度应接近 -g*t ≈ -1.96 m/s（位姿差分估计）。
    assert abs(vertical_speeds[47] + 9.81 * (48 / 240)) < 0.3, f"速度估计异常: {vertical_speeds[47]}"
    assert abs(float(bn.velocity(cube, angular=True)[2])) < 0.5, "自由落体不应自转"

    rows = scene.contacts()
    assert rows, "落地后应报告接触"
    pairs = {(row["a"], row["b"]) for row in rows}
    assert ("cube", "ground") in pairs or ("ground", "cube") in pairs, f"接触对异常: {pairs}"
    for row in rows:
        assert len(row["position"]) == 3 and len(row["normal"]) == 3
        assert row["separation"] <= 0.002, f"接触分离距离应接近 0: {row['separation']}"
    total = sum(row["normal_force_n"] for row in rows)
    assert 5.0 < total < 15.0, f"静止支撑力应接近 m*g≈9.81N: {total}"
    assert all(row["normal_force_n"] > 0 for row in rows)
    vertical = max(abs(row["normal"][2]) for row in rows)
    assert vertical > 0.9, f"地面接触法向应近似竖直: {vertical}"


def test_ray_hit_and_miss():
    """``E.lab_ray`` 命中最近刚体；超距、过近与排除刚体均返回 0。"""
    bn, scene = world()
    from unilabos.simulation.bullet_native import C, E

    scene.box("ground", (1.0, 1.0, 0.05), (0.0, 0.0, -0.05), mask=1)
    target = scene.box("target", (0.05, 0.05, 0.05), (0.0, 0.0, 0.5), mass=1.0)
    scene.step()

    def cast(start_z, end_z, exclude=0):
        distance = C.c_double(-1.0)
        handle = E.lab_ray(
            scene.ptr,
            (C.c_double * 3)(0.0, 0.0, start_z),
            (C.c_double * 3)(0.0, 0.0, end_z),
            exclude,
            C.byref(distance),
        )
        return handle, distance.value

    handle, distance = cast(0.6, 0.0)
    assert handle == target, f"应命中 target，实际 {handle}"
    assert 0.04 < distance < 0.06, f"命中距离应约为 0.05m: {distance}"

    missed, _ = cast(1.5, 0.0)
    # C 桥的 ``c_void_p`` 空返回是 None（native/standalone），bpy 回退用 0。
    assert missed in (0, None), "超过 0.1m 的命中必须被过滤"

    excluded, _ = cast(0.6, 0.0, target)
    assert excluded in (0, None), "被排除的刚体不得作为命中返回"

    too_close, _ = cast(0.552, 0.0)
    assert too_close in (0, None), "小于 5mm 的命中必须被过滤"

    assert E.lab_scalar_bytes() == 8
    del bn


def test_constraints_fixed_and_hinge():
    """fixed 约束保持相对位姿；hinge + motor 绕轴旋转且限位/反向区间语义正确。

    三个场景分开建：Blender 的刚体实现里同一个 PASSIVE 刚体同时参与 fixed 与
    motor 两条约束链时，后建链的电机可能静默失效；限位也只在首次推进前生效
    （见 ``bullet_bpy.LIMITATIONS``）。因此按 SZLab 的实际用法分别验收每种约束。
    """
    bn, scene = world()
    from unilabos.simulation.bullet_native import V3, V4, B

    identity = V4(1, 0, 0, 0)
    anchor = scene.box("anchor", (0.03, 0.03, 0.03), (0.0, 0.0, 1.0))
    child = scene.box("child", (0.05, 0.05, 0.05), (0.4, 0.0, 1.0), mass=1.0)
    fixed = B.RB_constraint_new_fixed(V3(0.2, 0.0, 1.0), identity, anchor, child)
    B.RB_dworld_add_constraint(scene.ptr, fixed, 1)
    for _ in range(240):
        scene.step()
    child_pose = bn.pose(child)
    assert abs(float(child_pose[2, 3]) - 1.0) < 0.01, f"fixed 约束未阻止下落: z={child_pose[2, 3]}"
    assert abs(float(child_pose[0, 3]) - 0.4) < 0.01, f"fixed 约束未保持相对位姿: x={child_pose[0, 3]}"

    def rotor_with_motor(limits):
        """新建场景：静态 anchor + 铰链 + 电机，返回 (模块, 世界, 转子, 起始角)。"""
        module, world_object = world()
        anchor_body = world_object.box("anchor", (0.03, 0.03, 0.03), (0.0, 0.0, 1.0))
        rotor = world_object.box("rotor", (0.05, 0.05, 0.05), (0.3, 0.0, 1.0), mass=1.0)
        hinge = B.RB_constraint_new_hinge(V3(0.0, 0.0, 1.0), identity, anchor_body, rotor)
        B.RB_dworld_add_constraint(world_object.ptr, hinge, 1)
        B.RB_constraint_set_limits_hinge(hinge, *limits)
        motor_axis = V4(0.0, 0.7071067811865476, 0.0, 0.7071067811865476)  # +X 转到 +Z
        motor = B.RB_constraint_new_motor(V3(0.0, 0.0, 1.0), motor_axis, anchor_body, rotor)
        B.RB_dworld_add_constraint(world_object.ptr, motor, 1)
        B.RB_constraint_set_enable_motor(motor, 0, 1)
        B.RB_constraint_set_max_impulse_motor(motor, 10.0, 10.0)
        B.RB_constraint_set_target_velocity_motor(motor, 0.0, 2.0)
        return module, world_object, rotor, angle_about_z(rotor)

    # 铰链 + 电机：反向区间 (1,-1) 表示不启用限位，转子应自由转动约 1rad。
    module, world_object, rotor, start = rotor_with_motor((1.0, -1.0))
    for _ in range(120):
        world_object.step()
    turned = angle_about_z(rotor) - start
    turned = (turned + math.pi) % (2 * math.pi) - math.pi
    # 电机驱动方向取决于约束坐标系朝向（native 同样如此），只断言转速量级。
    assert 0.3 < abs(turned) < 1.6, f"电机的铰链转角应约为 1rad: {turned}"
    assert abs(float(module.pose(rotor)[2, 3]) - 1.0) < 0.01, "铰链应锁住平移自由度"

    # 有效限位（推进前设置）：电机应被挡在 ±0.25rad 内。
    module, world_object, rotor, start = rotor_with_motor((-0.25, 0.25))
    for _ in range(240):
        world_object.step()
    limited = angle_about_z(rotor) - start
    limited = (limited + math.pi) % (2 * math.pi) - math.pi
    assert 0.05 < abs(limited) < 0.35, f"铰链限位未生效: {limited}"


def test_geometry_and_version_not_gated():
    """形状齐全，原生桥仅按环境/符号回退，不按 Blender 版本或平台拒绝。"""
    for key in ENV_KEYS:
        os.environ.pop(key, None)
    automatic = reload_native()
    assert automatic.BACKEND in ("standalone", "bpy"), (
        f"auto 只应选独立库或 bpy: {automatic.BACKEND}"
    )
    assert json.loads(automatic.MANIFEST.read_text(encoding="utf-8"))["backend"] == automatic.BACKEND
    if automatic.BACKEND == "bpy":
        assert automatic.NATIVE_ERROR is not None, "回退路径必须记录原生桥的原始失败原因"

    module = reload_native({"UNILABOS_BULLET_BACKEND": "bpy"})
    assert module.BACKEND == "bpy", f"显式 bpy 后端不可用: {module.BACKEND}"
    assert module.NATIVE_ERROR is None, "显式 bpy 后端不应尝试原生桥"
    manifest = json.loads(module.MANIFEST.read_text(encoding="utf-8"))
    assert manifest["backend"] == "bpy"
    assert manifest["pinned"] is False
    assert manifest["blender_version"] == bpy.app.version_string
    assert manifest["blender_build_hash"] == build_hash()
    assert module.PIN == build_hash()
    assert "d13f752e3b9c4f8c261cda552b1021f8bcc0382c" in json.dumps(manifest), "需记录 native PIN 供对照"

    try:
        reload_native({"UNILABOS_BULLET_BACKEND": "native"})
    except Exception as error:  # noqa: BLE001 - 这里就是要记录被强制要求的原生失败
        forced_native_error = repr(error)
    else:
        forced_native_error = None
    assert forced_native_error is not None, "强制 native 且不可用时必须报错而不是静默回退"

    module = reload_native()

    # 形状：box / convex hull / trimesh / compound 均可构建并推进。
    from unilabos.simulation.bullet_native import V3, V4, B, C
    from unilabos.simulation.bullet_native import np as bullet_np

    identity = bullet_np.eye(4)
    box_shape = B.RB_shape_new_box(0.05, 0.05, 0.05)
    B.RB_shape_set_margin(box_shape, 0.0001)

    hull_points = bullet_np.array(
        [[-0.05, -0.05, -0.05], [0.05, -0.05, -0.05], [0.05, 0.05, -0.05],
         [-0.05, 0.05, -0.05], [0.0, 0.0, 0.05]], dtype=bullet_np.float32)
    embedded = C.c_bool(True)
    hull_shape = B.RB_shape_new_convex_hull(
        hull_points.ctypes.data, 12, len(hull_points), 0.0, C.byref(embedded))
    B.RB_shape_set_margin(hull_shape, 0.0001)

    vertices = bullet_np.array(
        [[-0.05, -0.05, -0.05], [0.05, -0.05, -0.05], [0.05, 0.05, -0.05], [-0.05, 0.05, -0.05],
         [-0.05, -0.05, 0.05], [0.05, -0.05, 0.05], [0.05, 0.05, 0.05], [-0.05, 0.05, 0.05]],
        dtype=bullet_np.float32)
    faces = [(0, 1, 3), (1, 2, 3), (4, 6, 7), (4, 5, 6), (0, 4, 7), (0, 7, 3),
             (1, 5, 4), (1, 4, 0), (2, 6, 5), (2, 5, 1), (3, 7, 6), (3, 6, 2)]
    data = B.RB_trimesh_data_new(len(faces), len(vertices))
    B.RB_trimesh_add_vertices(data, vertices.ctypes.data, len(vertices), 12)
    for index, face in enumerate(faces):
        B.RB_trimesh_add_triangle_indices(data, index, *face)
    B.RB_trimesh_finish(data)
    mesh_shape = B.RB_shape_new_gimpact_mesh(data)
    B.RB_shape_set_margin(mesh_shape, 0.0001)

    compound = B.RB_shape_new_compound()
    B.RB_compound_add_child_shape(compound, box_shape, V3(0.0, 0.0, 0.0), V4(1, 0, 0, 0))
    B.RB_compound_add_child_shape(compound, hull_shape, V3(0.15, 0.0, 0.0), V4(1, 0, 0, 0))

    scene = module.World()
    scene.box("ground", (1.0, 1.0, 0.05), (0.0, 0.0, -0.05), mask=1)
    bodies = {}
    for name, shape, position in (
        ("shape_box", box_shape, (-0.3, 0.0, 0.5)),
        ("shape_hull", hull_shape, (-0.1, 0.0, 0.5)),
        ("shape_mesh", mesh_shape, (0.1, 0.0, 0.5)),
        ("shape_compound", compound, (0.3, 0.0, 0.5)),
    ):
        matrix = identity.copy()
        matrix[:3, 3] = position
        bodies[name] = scene.body(name, shape, matrix, 0.5)

    for _ in range(360):
        scene.step()
    for name, handle in bodies.items():
        position = module.pose(handle)[:3, 3]
        assert np.isfinite(position).all(), f"{name} 位姿非有限值: {position}"
        assert 0.0 < float(position[2]) < 0.4, f"{name} 未落到地面附近: z={position[2]}"
    assert scene.contacts(), "复合/网格刚体落地后应报告接触"

    # native 同形的读写指针接口：位置/朝向回读。
    from unilabos.simulation.bullet_native import V3, V4

    queried_position = V3()
    B.RB_body_get_position(bodies["shape_box"], queried_position)
    assert abs(float(queried_position[2]) - float(module.pose(bodies["shape_box"])[2, 3])) < 1e-4
    queried_rotation = V4()
    B.RB_body_get_orientation(bodies["shape_box"], queried_rotation)
    assert abs(sum(float(component) ** 2 for component in queried_rotation) - 1.0) < 1e-3


def reload_native(env=None):
    """按给定环境变量重新导入 ``bullet_native``，返回新模块。"""
    saved = {key: os.environ.get(key) for key in ENV_KEYS}
    try:
        for key in ENV_KEYS:
            os.environ.pop(key, None)
        for key, value in (env or {}).items():
            os.environ[key] = value
        importlib.invalidate_caches()
        sys.modules.pop("unilabos.simulation.bullet_native", None)
        return importlib.import_module("unilabos.simulation.bullet_native")
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def main():
    """在 Blender 内顺序执行全部用例并汇总；失败返回非零退出码。"""
    print(
        "BULLET_BPY_TEST_ENV "
        f"blender={bpy.app.version_string} hash={build_hash()} "
        f"python={sys.version.split()[0]} platform={sys.platform}",
        flush=True,
    )
    tests = [
        test_box_drop_contact,
        test_ray_hit_and_miss,
        test_constraints_fixed_and_hinge,
        test_geometry_and_version_not_gated,
    ]
    failures = []
    for test in tests:
        name = test.__name__
        try:
            test()
        except Exception as error:  # noqa: BLE001 - 冒烟必须打印原始失败
            failures.append((name, error))
            traceback.print_exc()
            print(f"BULLET_BPY_TEST {name} FAIL {type(error).__name__}: {error}", flush=True)
        else:
            print(f"BULLET_BPY_TEST {name} PASS", flush=True)
    print(
        f"BULLET_BPY_TEST_SUMMARY total={len(tests)} passed={len(tests) - len(failures)} "
        f"failed={len(failures)}",
        flush=True,
    )
    if failures:
        print("BULLET_BPY_TEST_RESULT FAIL", flush=True)
        return 1
    print("BULLET_BPY_TEST_RESULT OK", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
