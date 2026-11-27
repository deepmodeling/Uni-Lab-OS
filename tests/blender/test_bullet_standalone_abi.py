"""独立 Bullet DLL 的最小闭环：盒体落地、几何射线、铰链约束。

只通过 C ABI（RB_* / lab_*）调用，不依赖 Blender 进程内符号。
"""

import ctypes as C
import math
import re
from pathlib import Path

import pytest

F = C.c_float
STEP = 1.0 / 240.0


def vec3(*values):
    return (F * 3)(*values)


def quat4(*values):
    return (F * 4)(*values)


def make_world(abi, gravity=(0.0, 0.0, -9.81)):
    b = abi.B
    world = b.RB_dworld_new(vec3(*gravity))
    assert world
    b.RB_dworld_set_solver_iterations(world, 1000)
    b.RB_dworld_set_split_impulse(world, 1)
    return world


def add_box(abi, world, half, position, mass=0.0, mask=1):
    b = abi.B
    shape = b.RB_shape_new_box(*half)
    assert shape
    b.RB_shape_set_margin(shape, 0.0001)
    body = b.RB_body_new(shape, vec3(*position), quat4(1.0, 0.0, 0.0, 0.0))
    assert body
    b.RB_body_set_mass(body, mass)
    b.RB_body_set_friction(body, 0.8)
    b.RB_body_set_damping(body, 0.0, 0.0)
    b.RB_body_set_sleep_thresh(body, 0.0, 0.0)
    b.RB_body_set_activation_state(body, 0)
    b.RB_dworld_add_body(world, body, mask)
    return body, shape


def step(abi, world, count=1):
    for _ in range(count):
        abi.B.RB_dworld_step_simulation(world, STEP, 0, STEP)


def body_position(abi, body):
    out = vec3(0.0, 0.0, 0.0)
    abi.B.RB_body_get_position(body, out)
    return tuple(out)


def body_velocity(abi, body, angular=False):
    out = vec3(0.0, 0.0, 0.0)
    getattr(abi.B, "RB_body_get_%s_velocity" % ("angular" if angular else "linear"))(body, out)
    return tuple(out)


def read_contacts(abi, world, capacity=256):
    buffer = (abi.Contact * capacity)()
    count = abi.E.lab_contacts(world, buffer, capacity)
    assert count >= 0, "contact buffer exhausted: %d" % count
    return [
        {
            "a": contact.a,
            "b": contact.b,
            "position": tuple(contact.position),
            "normal": tuple(contact.normal),
            "separation": contact.separation,
            "impulse": contact.impulse,
        }
        for contact in buffer[:count]
    ]


def test_scalar_precision_is_double(abi):
    assert abi.scalar_bytes() == 8


def test_box_falls_onto_ground_and_reports_contact(abi):
    world = make_world(abi)
    ground, _ = add_box(abi, world, (0.5, 0.5, 0.05), (0.0, 0.0, -0.05))
    box, _ = add_box(abi, world, (0.02, 0.02, 0.02), (0.0, 0.0, 0.30), mass=0.1)
    step(abi, world, 480)

    x, y, z = body_position(abi, box)
    assert abs(x) < 1e-3 and abs(y) < 1e-3
    assert abs(z - 0.02) < 2e-3, "resting height %r" % z
    assert abs(body_velocity(abi, box)[2]) < 1e-2

    pairs = [row for row in read_contacts(abi, world) if {row["a"], row["b"]} == {ground, box}]
    assert pairs, "no ground/box manifold"
    # 每个流形点各带一份冲量；0.1 kg 静置时的合力应接近 m*g，而不是单点值。
    total_force = sum(row["impulse"] for row in pairs) * 240.0
    assert 0.5 < total_force < 2.0, total_force
    strongest = max(pairs, key=lambda row: row["impulse"])
    assert strongest["impulse"] * 240.0 > 0.1
    assert abs(strongest["position"][2]) < 5e-3
    assert abs(strongest["separation"]) < 1e-3
    assert abs(math.sqrt(sum(v * v for v in strongest["normal"])) - 1.0) < 1e-3


def test_lab_ray_hits_ground_and_respects_filter_limits(abi):
    world = make_world(abi)
    ground, _ = add_box(abi, world, (0.5, 0.5, 0.05), (0.0, 0.0, -0.05))
    distance = C.c_double(0.0)

    origin = (C.c_double * 3)(0.0, 0.0, 0.08)
    end = (C.c_double * 3)(0.0, 0.0, -0.02)
    hit = abi.E.lab_ray(world, origin, end, None, C.byref(distance))
    assert hit == ground
    assert 0.075 < distance.value < 0.081, distance.value

    # 排除命中的刚体后同一射线不应再返回它。
    hit = abi.E.lab_ray(world, origin, end, ground, C.byref(distance))
    assert hit in (None, 0)

    # 兼容层把有效距离限制在 [0.005, 0.1] 米。
    far = (C.c_double * 3)(0.0, 0.0, 0.30)
    hit = abi.E.lab_ray(world, far, end, None, C.byref(distance))
    assert hit in (None, 0)
    near = (C.c_double * 3)(0.0, 0.0, 0.004)
    hit = abi.E.lab_ray(world, near, end, None, C.byref(distance))
    assert hit in (None, 0)


def test_hinge_keeps_pivot_and_transfers_rotation(abi):
    # 关闭重力，单独验证铰链的转轴约束与初始角速度。
    world = make_world(abi, gravity=(0.0, 0.0, 0.0))
    anchor, _ = add_box(abi, world, (0.01, 0.01, 0.01), (0.0, 0.0, 0.0))
    rod, _ = add_box(abi, world, (0.05, 0.005, 0.005), (0.05, 0.0, 0.0), mass=0.05)
    abi.E.lab_ignore(anchor, rod, 1)

    pivot = (F * 3)(0.0, 0.0, 0.0)
    orientation = (F * 4)(1.0, 0.0, 0.0, 0.0)
    hinge = abi.B.RB_constraint_new_hinge(pivot, orientation, anchor, rod)
    assert hinge
    abi.B.RB_dworld_add_constraint(world, hinge, 1)
    abi.B.RB_constraint_set_solver_iterations(hinge, 1000)
    abi.B.RB_constraint_set_limits_hinge(hinge, 1.0, -1.0)  # 解锁角度限制

    abi.B.RB_body_set_angular_velocity(rod, vec3(0.0, 0.0, -2.0))
    step(abi, world, 60)

    x, y, z = body_position(abi, rod)
    assert abs(z) < 5e-3, "rod left the hinge plane: %r" % z
    assert abs(math.hypot(x, y) - 0.05) < 2e-3, "pivot distance drifted: %r" % math.hypot(x, y)
    angle = math.atan2(y, x)
    # 初始 ω=-2 绕质心；铰链把运动变成绕 pivot 旋转，ω 降到约 -0.50 rad/s。
    assert -0.20 < angle < -0.05, "expected ~-0.126 rad, got %r" % angle
    angular_z = body_velocity(abi, rod, angular=True)[2]
    assert -0.70 < angular_z < -0.35, angular_z


def test_contact_buffer_overflow_is_reported(abi):
    world = make_world(abi)
    add_box(abi, world, (0.5, 0.5, 0.05), (0.0, 0.0, -0.05))
    add_box(abi, world, (0.02, 0.02, 0.02), (0.0, 0.0, 0.02), mass=0.1)
    step(abi, world, 240)

    buffer = (abi.Contact * 4)()
    count = abi.E.lab_contacts(world, buffer, 0)
    # 容量 0 时约定返回 -(已写入数 + 1)；任何接触点都会立即触发溢出。
    assert count == -1
    assert buffer[0].a in (None, 0)


def test_every_bullet_native_symbol_is_exported(abi):
    source_path = Path(__file__).resolve().parents[2] / "unilabos" / "simulation" / "bullet_native.py"
    source = source_path.read_text(encoding="utf-8")
    names = {name for name in re.findall(r"'(RB_[A-Za-z0-9_]+)'", source) if not name.endswith("_")}
    names.update("RB_constraint_new_" + kind for kind in ("fixed", "hinge", "slider", "motor"))
    lab_names = set(re.findall(r"E\.(lab_[a-z0-9_]+)", source))
    assert "RB_dworld_new" in names and "lab_contacts" in lab_names

    missing = [name for name in sorted(names | lab_names) if not hasattr(abi.cdll, name)]
    assert not missing, "standalone DLL is missing exports: %s" % missing


@pytest.mark.parametrize("kind", ["fixed", "slider", "motor"])
def test_other_constraint_factories_exist(abi, kind):
    world = make_world(abi)
    anchor, _ = add_box(abi, world, (0.01, 0.01, 0.01), (0.0, 0.0, 0.0))
    body, _ = add_box(abi, world, (0.02, 0.02, 0.02), (0.0, 0.0, 0.05), mass=0.1)
    factory = getattr(abi.B, "RB_constraint_new_" + kind)
    constraint = factory((F * 3)(0.0, 0.0, 0.0), (F * 4)(1.0, 0.0, 0.0, 0.0), anchor, body)
    assert constraint
    abi.B.RB_dworld_add_constraint(world, constraint, 1)
    abi.B.RB_constraint_set_enabled(constraint, 0)
    step(abi, world, 5)
