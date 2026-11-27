"""``bullet_native`` 的独立 DLL 版本（无 Blender / mathutils 依赖）。

导出面与 ``unilabos.simulation.bullet_native`` 对齐：``B`` / ``E`` 句柄、
``V3`` / ``V4``、``Contact``、``World``、``pose`` / ``velocity`` / ``quat``。
另提供不依赖 mathutils 的 ``axis_quaternion`` 与 ``constraint``，供
``bullet_usd`` 的 USD 建体路径替换使用。
"""

import ctypes as C
import math

import numpy as np

from . import loader

_abi = loader.load()
B = _abi.B
E = _abi.E
V3 = C.c_float * 3
V4 = C.c_float * 4
Contact = _abi.Contact
LIBRARY_PATH = _abi.path
ABI_VERSION = _abi.abi_version()

__all__ = [
    "B",
    "E",
    "C",
    "V3",
    "V4",
    "Contact",
    "World",
    "pose",
    "velocity",
    "quat",
    "axis_quaternion",
    "constraint",
    "LIBRARY_PATH",
    "ABI_VERSION",
]


def quaternion_matrix(quaternion) -> np.ndarray:
    """(w, x, y, z) → 3×3 旋转矩阵，等价 mathutils Quaternion.to_matrix()。"""

    w, x, y, z = (float(value) for value in quaternion)
    norm = math.sqrt(w * w + x * x + y * y + z * z)
    if norm == 0.0:
        # mathutils 把全零四元数当作单位旋转；USD 未设置 principal axes 时就是 (0,0,0,0)。
        return np.eye(3)
    w, x, y, z = w / norm, x / norm, y / norm, z / norm
    return np.array(
        [
            [1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
            [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
            [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)],
        ]
    )


def matrix_quaternion(matrix) -> tuple[float, float, float, float]:
    """3×3/4×4 旋转矩阵 → (w, x, y, z)；w 保持非负，便于结果比较。"""

    value = np.asarray(matrix, dtype=float)
    if value.shape == (4, 4):
        value = value[:3, :3]
    if value.shape != (3, 3):
        raise ValueError("expected a 3x3 or 4x4 matrix, got %r" % (value.shape,))
    m = value
    trace = m[0, 0] + m[1, 1] + m[2, 2]
    if trace > 0.0:
        scale = math.sqrt(trace + 1.0) * 2.0
        w = 0.25 * scale
        x = (m[2, 1] - m[1, 2]) / scale
        y = (m[0, 2] - m[2, 0]) / scale
        z = (m[1, 0] - m[0, 1]) / scale
    elif m[0, 0] > m[1, 1] and m[0, 0] > m[2, 2]:
        scale = math.sqrt(1.0 + m[0, 0] - m[1, 1] - m[2, 2]) * 2.0
        w = (m[2, 1] - m[1, 2]) / scale
        x = 0.25 * scale
        y = (m[0, 1] + m[1, 0]) / scale
        z = (m[0, 2] + m[2, 0]) / scale
    elif m[1, 1] > m[2, 2]:
        scale = math.sqrt(1.0 + m[1, 1] - m[0, 0] - m[2, 2]) * 2.0
        w = (m[0, 2] - m[2, 0]) / scale
        x = (m[0, 1] + m[1, 0]) / scale
        y = 0.25 * scale
        z = (m[1, 2] + m[2, 1]) / scale
    else:
        scale = math.sqrt(1.0 + m[2, 2] - m[0, 0] - m[1, 1]) * 2.0
        w = (m[1, 0] - m[0, 1]) / scale
        x = (m[0, 2] + m[2, 0]) / scale
        y = (m[1, 2] + m[2, 1]) / scale
        z = 0.25 * scale
    if w < 0.0:
        w, x, y, z = -w, -x, -y, -z
    return (w, x, y, z)


def pose(pointer) -> np.ndarray:
    """刚体位姿的 4×4 矩阵，布局与 ``bullet_native.pose`` 相同。"""

    location = V3()
    orientation = V4()
    B.RB_body_get_position(pointer, location)
    B.RB_body_get_orientation(pointer, orientation)
    matrix = np.eye(4)
    matrix[:3, :3] = quaternion_matrix(orientation)
    matrix[:3, 3] = np.asarray(location)
    return matrix


def velocity(pointer, angular: bool = False) -> np.ndarray:
    """线速度或角速度（m/s、rad/s）。"""

    value = V3()
    getattr(B, "RB_body_get_%s_velocity" % ("angular" if angular else "linear"))(pointer, value)
    return np.asarray(value)


def quat(matrix) -> V4:
    """4×4/3×3 矩阵 → Blender 顺序的 (w, x, y, z) ctypes 数组。"""

    return V4(*matrix_quaternion(matrix))


class World:
    """与 ``bullet_native.World`` 同接口的独立 Bullet 世界。"""

    def __init__(self, gravity=(0.0, 0.0, -9.81)):
        self.ptr = B.RB_dworld_new(V3(*gravity))
        B.RB_dworld_set_solver_iterations(self.ptr, 1000)
        B.RB_dworld_set_split_impulse(self.ptr, 1)
        self.names = {}
        self.buffer = (Contact * 100000)()
        self.tick = 0

    def body(self, name, shape, mat, mass, mask=1):
        mat = np.asarray(mat, dtype=float)
        pointer = B.RB_body_new(shape, V3(*mat[:3, 3]), quat(mat))
        B.RB_body_set_mass(pointer, mass)
        B.RB_body_set_friction(pointer, 0.8)
        B.RB_body_set_damping(pointer, 0, 0)
        B.RB_body_set_sleep_thresh(pointer, 0, 0)
        B.RB_body_set_activation_state(pointer, 0)
        B.RB_dworld_add_body(self.ptr, pointer, mask)
        self.names[pointer] = name
        return pointer

    def box(self, name, half, position, mass=0, mask=1):
        shape = B.RB_shape_new_box(*half)
        B.RB_shape_set_margin(shape, 0.0001)
        matrix = np.eye(4)
        matrix[:3, 3] = np.asarray(position, dtype=float)
        return self.body(name, shape, matrix, mass, mask)

    def step(self):
        B.RB_dworld_step_simulation(self.ptr, 1 / 240, 0, 1 / 240)
        self.tick += 1

    def contacts(self):
        count = E.lab_contacts(self.ptr, self.buffer, len(self.buffer))
        if count < 0:
            raise RuntimeError("Contact buffer exhausted")
        return [
            {
                "a": self.names.get(contact.a),
                "b": self.names.get(contact.b),
                "position": list(contact.position),
                "normal": list(contact.normal),
                "separation": contact.separation,
                "normal_force_n": contact.impulse * 240,
            }
            for contact in self.buffer[:count]
        ]

    def close(self):
        if self.ptr:
            B.RB_dworld_delete(self.ptr)
            self.ptr = None
            self.names.clear()


def axis_quaternion(kind: str, axis) -> V4:
    """把约束局部轴对齐到给定世界轴的旋转，等价 mathutils ``rotation_difference``。

    hinge 的基准轴是局部 Z，slider/motor 是局部 X（与 ``bullet_usd.constraint`` 一致）。
    """

    base = np.array([0.0, 0.0, 1.0]) if kind == "hinge" else np.array([1.0, 0.0, 0.0])
    target = np.asarray(axis, dtype=float)
    norm = np.linalg.norm(target)
    if norm == 0.0:
        raise ValueError("zero constraint axis")
    target = target / norm
    dot = float(np.clip(np.dot(base, target), -1.0, 1.0))
    cross = np.cross(base, target)
    if dot < -1.0 + 1e-9:
        helper = np.array([1.0, 0.0, 0.0]) if abs(base[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
        cross = np.cross(base, helper)
        cross /= np.linalg.norm(cross)
        return V4(0.0, *cross)
    quaternion = np.array([1.0 + dot, *cross])
    quaternion /= np.linalg.norm(quaternion)
    return V4(*quaternion)


def constraint(world, kind, parent, child, pivot, axis=None):
    """``bullet_usd.constraint`` 的 mathutils 无关版本。"""

    orientation = V4(1.0, 0.0, 0.0, 0.0) if axis is None else axis_quaternion(kind, axis)
    pointer = getattr(B, "RB_constraint_new_" + kind)(V3(*pivot), orientation, parent, child)
    B.RB_dworld_add_constraint(world.ptr, pointer, 1)
    B.RB_constraint_set_solver_iterations(pointer, 1000)
    if kind == "hinge":
        B.RB_constraint_set_limits_hinge(pointer, 1, -1)
    if kind == "slider":
        B.RB_constraint_set_limits_slider(pointer, 1, -1)
    return pointer
