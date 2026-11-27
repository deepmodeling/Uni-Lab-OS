"""``bullet_usd`` 的独立 DLL 版本（不依赖 mathutils）。

与 ``unilabos.simulation.bullet_usd`` 的 Body/shape/constraint 语义一致：
原始 USD 碰撞体 → compound 形状，质量/质心/主惯量来自 UsdPhysics.MassAPI，
约束 pivot/axis 转到 Bullet 约束帧。差别只是底层库换成独立 Bullet。
"""

import json  # noqa: F401 - bullet_usd 的星号导入面包含 json

import numpy as np
from pxr import Usd, UsdGeom, UsdPhysics

from .compat import V3, V4, B, C, E, World, axis_quaternion, pose, quat, velocity

# 与 bullet_usd 的星号导入面保持兼容：消费者直接使用 B/E/C、np、Usd 与 constraint。
__all__ = [
    "json",
    "np",
    "Usd",
    "UsdGeom",
    "UsdPhysics",
    "B",
    "C",
    "E",
    "V3",
    "V4",
    "World",
    "axis_quaternion",
    "pose",
    "quat",
    "velocity",
    "matrix",
    "points",
    "shape",
    "Body",
    "constraint",
]


def matrix(prim):
    return np.asarray(UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(Usd.TimeCode.Default())).T


def points(prim):
    if prim.IsA(UsdGeom.Mesh):
        return np.asarray(UsdGeom.Mesh(prim).GetPointsAttr().Get())
    if prim.IsA(UsdGeom.Cube):
        size = UsdGeom.Cube(prim).GetSizeAttr().Get() / 2
        return np.array([(x, y, z) for x in (-size, size) for y in (-size, size) for z in (-size, size)])
    raise ValueError("Unhandled original collider " + str(prim.GetPath()))


def shape(prim, transform):
    verts = np.ascontiguousarray((points(prim) @ transform[:3, :3].T + transform[:3, 3]), dtype=np.float32)
    approximation = (
        UsdPhysics.MeshCollisionAPI(prim).GetApproximationAttr().Get()
        if prim.IsA(UsdGeom.Mesh)
        else "convexHull"
    )
    if approximation in ("sdf", "none"):
        geo = UsdGeom.Mesh(prim)
        indices = np.asarray(geo.GetFaceVertexIndicesAttr().Get())
        counts = np.asarray(geo.GetFaceVertexCountsAttr().Get())
        faces = []
        start = 0
        for count in counts:
            face = indices[start:start + count]
            faces.extend((int(face[0]), int(face[i]), int(face[i + 1])) for i in range(1, count - 1))
            start += count
        mesh = B.RB_trimesh_data_new(len(faces), len(verts))
        B.RB_trimesh_add_vertices(mesh, verts.ctypes.data, len(verts), 12)
        for index, face in enumerate(faces):
            B.RB_trimesh_add_triangle_indices(mesh, index, *face)
        B.RB_trimesh_finish(mesh)
        collision_shape = B.RB_shape_new_gimpact_mesh(mesh)
    else:
        embedded = C.c_bool(True)
        collision_shape = B.RB_shape_new_convex_hull(
            verts.ctypes.data, 12, len(verts), 0, C.byref(embedded)
        )
    B.RB_shape_set_margin(collision_shape, 0.00005)
    return collision_shape


class Body:
    """USD 刚体 → 独立 Bullet 刚体；接口与 ``bullet_usd.Body`` 相同。"""

    def __init__(self, world, name, prim, world_from_root, static=False, mask=1):
        mass = UsdPhysics.MassAPI(prim)
        self.mass = float(mass.GetMassAttr().Get() or 0)
        self.com = np.eye(4)
        center = mass.GetCenterOfMassAttr().Get()
        if center is not None and np.isfinite(np.asarray(center)).all():
            self.com[:3, 3] = np.asarray(center)
        axes = mass.GetPrincipalAxesAttr().Get()
        if axes is not None:
            from .compat import quaternion_matrix

            self.com[:3, :3] = quaternion_matrix(
                (axes.GetReal(), *np.asarray(axes.GetImaginary()))
            )

        colliders = [
            item
            for item in Usd.PrimRange(prim)
            if item.HasAPI(UsdPhysics.CollisionAPI)
            and UsdPhysics.CollisionAPI(item).GetCollisionEnabledAttr().Get() is not False
        ]
        compound = B.RB_shape_new_compound()
        inverse = np.linalg.inv(matrix(prim))
        inverse_com = np.linalg.inv(self.com)
        for item in colliders:
            child = shape(item, inverse_com @ inverse @ matrix(item))
            B.RB_compound_add_child_shape(compound, child, V3(0, 0, 0), V4(1, 0, 0, 0))
        if not colliders:
            child = B.RB_shape_new_box(0.0001, 0.0001, 0.0001)
            B.RB_compound_add_child_shape(compound, child, V3(0, 0, 0), V4(1, 0, 0, 0))
            mask = 0
        self.ptr = world.body(name, compound, world_from_root @ self.com, 0 if static else self.mass, mask)
        inertia = mass.GetDiagonalInertiaAttr().Get()
        if not static and inertia is not None and min(inertia) > 0:
            E.lab_inertia(self.ptr, self.mass, *inertia)
        self.name = name
        self.colliders = len(colliders)

    def pose(self):
        return pose(self.ptr) @ np.linalg.inv(self.com)

    def velocity(self):
        return velocity(self.ptr)


def constraint(world, kind, parent, child, pivot, axis=None):
    """``bullet_usd.constraint`` 的 mathutils 无关等价实现。"""

    orientation = V4(1, 0, 0, 0) if axis is None else axis_quaternion(kind, axis)
    pointer = getattr(B, "RB_constraint_new_" + kind)(V3(*pivot), orientation, parent, child)
    B.RB_dworld_add_constraint(world.ptr, pointer, 1)
    B.RB_constraint_set_solver_iterations(pointer, 1000)
    if kind == "hinge":
        B.RB_constraint_set_limits_hinge(pointer, 1, -1)
    if kind == "slider":
        B.RB_constraint_set_limits_slider(pointer, 1, -1)
    return pointer
