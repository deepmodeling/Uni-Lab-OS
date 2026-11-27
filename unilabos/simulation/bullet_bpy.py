"""Blender 公共 Python API 实现的 Bullet 刚体回退后端（不依赖内部 ``RB_*`` C 符号）。

原生桥 :mod:`unilabos.simulation.bullet_native` 直接 ``ctypes`` 载入 Blender 进程内的
``RB_*`` 符号；Windows 版 Blender 不导出这些符号，且不同构建的 ABI/PIN 也不同。
本模块只使用 ``bpy`` / ``mathutils`` 公共 API，提供与 ``bullet_native`` 同形的
最小接口，让上层（``bullet_usd``、SZLab ``blender_bullet``）在任意平台/版本上可运行：

形状
    ``RB_shape_new_box`` / ``RB_shape_new_convex_hull`` / ``RB_shape_new_gimpact_mesh``
    / ``RB_shape_new_trimesh`` / ``RB_shape_new_compound`` + ``RB_compound_add_child_shape``；
    统一展平为刚体对象的局部三角网格。单盒形用 Blender ``BOX``，单凸包用
    ``CONVEX_HULL``，三者以上的复合与三角网用 ``MESH``（GImpact）。
刚体
    ``RB_body_new``、``RB_body_set_mass`` / ``friction`` / ``damping`` / ``sleep_thresh``
    / ``activation_state`` / ``linear_velocity`` / ``angular_velocity`` / ``loc_rot``、
    ``RB_body_get_position`` / ``orientation`` / ``linear_velocity`` / ``angular_velocity``。
世界
    ``RB_dworld_new`` / ``add_body`` / ``remove_body`` / ``step_simulation`` /
    ``set_solver_iterations`` / ``set_split_impulse`` / ``add_constraint``。
约束
    ``RB_constraint_new_fixed|hinge|slider|motor``、limits / motor / solver iterations /
    enabled / ``lab_ignore`` / ``lab_inertia`` / ``lab_force_at``。
查询
    ``E.lab_contacts``（BVH 重叠 + 最近点 + 近似法向冲量）、``E.lab_ray``
    （``scene.ray_cast``，保持 native 的 5mm–100mm 过滤与排除刚体语义）、
    ``E.lab_scalar_bytes``（返回 8，与 double 精度 native 桥一致）。

推进方式
    Blender 刚体世界按帧推进；本模块把 ``render.fps`` 固定为 240、
    ``substeps_per_frame`` 固定为 1，使一次 ``scene.frame_set`` 恰好等于 native 的
    ``stepSimulation(1/240, 0, 1/240)``（同为零插值定步长）。不按 Blender 版本或
    平台拒绝运行，只把 ``bpy.app.version`` / 构建哈希记入 ``MANIFEST``。

与 native 的语义差异（均为公共 API 的硬限制，见 ``LIMITATIONS``）
    1. 接触不是 Bullet manifold：用刚体局部几何的 BVH 重叠/最近点得到接触点与法向，
       冲量按“支撑重力份额 + 步前法向闭合速度”估算，因此 ``normal_force_n`` 是
       工程近似值，不是 Bullet 求解器的实际冲量。
    2. 线速度/角速度没有写入接口：``RB_body_set_linear_velocity`` 等记录为不支持调用
       并告警；读取速度由相邻帧位姿差分估计。
    3. 惯量张量不可指定（``lab_inertia`` 只记录），Blender 依据碰撞形状计算。
    4. 运行中移动刚体（``RB_body_set_loc_rot``）会被 Blender 的点缓存覆盖；刚体
       增删/质量修改在帧边界生效（增删会重置该刚体速度）。
    5. ``lab_ignore`` 只能对“已有约束连接”的刚体对真正关闭碰撞（``disable_collisions``），
       其余刚体对仅从接触报告里过滤；物理求解仍可能产生自碰撞响应。
    6. ``RB_dworld_add_body`` 的 ``mask`` 只区分“是否参与碰撞”（0 = 不碰撞），
       不实现 Bullet 的 group/mask 位掩码。
"""

from __future__ import annotations

import ctypes as C
import json
import math
import os
import platform
import struct
import sys
import tempfile
import warnings
from itertools import combinations, count
from pathlib import Path

import bmesh
import bpy
import numpy as np
from mathutils import Matrix, Quaternion, Vector
from mathutils.bvhtree import BVHTree

BACKEND = "bpy"
STEP_SECONDS = 1 / 240
STEP_HZ = 240
GRAVITY = 9.81
CONTACT_DISTANCE = 2e-3  # m，邻近接触判定阈值
CONTACT_CLUSTER = 5e-3  # m，同一接触点的聚类半径
RAY_MINIMUM = 5e-3  # m，与 native lab_ray 一致
RAY_MAXIMUM = 0.1  # m，与 native lab_ray 一致
DEFAULT_MARGIN = 1e-4
NATIVE_PIN = "d13f752e3b9c4f8c261cda552b1021f8bcc0382c"

P = C.c_void_p
F = C.c_float
I = C.c_int
V3 = F * 3
V4 = F * 4


class Contact(C.Structure):
    """与 native ``rbContact`` 相同的布局；``a``/``b`` 在回退后端是不透明句柄。"""

    _fields_ = [
        ("a", P),
        ("b", P),
        ("position", C.c_double * 3),
        ("normal", C.c_double * 3),
        ("separation", C.c_double),
        ("impulse", C.c_double),
    ]


def _build_hash():
    """返回当前 Blender 构建哈希文本。"""
    value = bpy.app.build_hash
    return value.decode() if isinstance(value, bytes) else str(value)


PIN = _build_hash()

LIMITATIONS = [
    "contacts are BVH geometric overlaps/nearest points with approximate normal impulses, not Bullet manifolds",
    "rigid body velocity cannot be written through the public Python API; velocities are pose differences",
    "inertia tensor overrides (lab_inertia) are recorded but not applied",
    "moving a body during simulation (RB_body_set_loc_rot) is overridden by Blender's point cache",
    "lab_ignore only disables physics collisions for constrained pairs; other pairs are contact-filtered",
    "collision groups are reduced to a collide/ignore boolean (mask != 0)",
    "constraint limits must be set before the first step; only motor target velocity updates live",
    "a PASSIVE body shared by a fixed constraint and a motor chain can silently disable that motor",
]


def _manifest_path():
    """返回回退后端清单文件路径（缓存目录或系统临时目录）。"""
    base = os.environ.get("UNILABOS_BULLET_CACHE")
    path = Path(base) if base else Path(tempfile.gettempdir()) / "unilabos-bullet-bpy"
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        path = Path(tempfile.gettempdir())
    return path / "bpy-manifest.json"


MANIFEST = _manifest_path()


def _write_manifest(native_error=None, standalone_error=None):
    """写入并返回回退后端清单；记录版本/哈希/限制，不伪装成 native 桥。"""
    data = {
        "backend": "bpy",
        "pinned": False,
        "native_pin": NATIVE_PIN,
        "blender_version": bpy.app.version_string,
        "blender_build_hash": PIN,
        "python": platform.python_version(),
        "platform": sys.platform,
        "step_seconds": STEP_SECONDS,
        "step_hz": STEP_HZ,
        "contacts": "bvh overlap/nearest + approximate normal impulse (weight share + pre-step closing speed)",
        "source": "unilabos/simulation/bullet_bpy.py",
        "limitations": list(LIMITATIONS),
    }
    if native_error is not None:
        data["native_error"] = native_error
    if standalone_error is not None:
        data["standalone_error"] = standalone_error
    try:
        MANIFEST.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass
    return data


_write_manifest()


def record_native_error(error, standalone_error=None):
    """由 ``bullet_native`` 回退路径调用，把原生桥/独立库的原始失败写入清单。"""
    return _write_manifest(
        None if error is None else repr(error),
        None if standalone_error is None else repr(standalone_error),
    )


# --------------------------------------------------------------------------------------
# 句柄：所有 B/E 接口都用不透明整数句柄（native 用指针），0 表示“无”。
# --------------------------------------------------------------------------------------
_handles = count(1)
_objects = {}
_WARNED = set()


def _new_handle(obj):
    handle = next(_handles)
    _objects[handle] = obj
    return handle


def _lookup(handle, kind):
    if not isinstance(handle, int):
        if isinstance(handle, kind):
            return handle
        raise TypeError(f"需要 {kind.__name__} 句柄，得到 {type(handle)!r}")
    try:
        obj = _objects[handle]
    except KeyError:
        raise ValueError(f"未知的 {kind.__name__} 句柄: {handle}") from None
    if not isinstance(obj, kind):
        raise TypeError(f"句柄 {handle} 不是 {kind.__name__}")
    return obj


def _warn(feature):
    """对公共 API 无法实现的操作只告警一次，避免每个子步刷屏。"""
    if feature in _WARNED:
        return
    _WARNED.add(feature)
    warnings.warn(
        f"bullet_bpy: {feature} is not supported by Blender's public Python API and is ignored",
        RuntimeWarning,
        stacklevel=3,
    )


def _scene():
    scene = bpy.context.scene
    return scene if scene is not None else bpy.data.scenes[0]


def _activate(obj):
    """选中并激活对象，供 ``bpy.ops.rigidbody.*`` 使用。"""
    for other in tuple(bpy.context.selected_objects):
        other.select_set(False)
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)


def _reset_scene(scene):
    """清空当前场景的刚体世界与对象：回退后端独占该 Blender 场景。"""
    if scene.rigidbody_world is not None:
        bpy.ops.rigidbody.world_remove()
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    try:
        bpy.data.orphans_purge(do_local_ids=True, do_linked_ids=True, do_recursive=True)
    except (AttributeError, TypeError):  # 旧版本无 orphans_purge
        for mesh in list(bpy.data.meshes):
            if mesh.users == 0:
                bpy.data.meshes.remove(mesh)


def _vec3(value):
    """读取 ctypes 数组 / numpy / 序列形式的三维向量。"""
    if isinstance(value, Vector):
        return value.copy()
    try:
        if len(value) == 3:
            return Vector((float(value[0]), float(value[1]), float(value[2])))
    except TypeError:
        pass
    raise TypeError(f"需要三维向量，得到 {value!r}")


def _quat(value):
    """读取 (w, x, y, z) 形式的四元数（native 的 ``V4`` / mathutils / 序列）。"""
    if isinstance(value, Quaternion):
        return value.copy()
    try:
        if len(value) == 4:
            quaternion = Quaternion((float(value[0]), float(value[1]), float(value[2]), float(value[3])))
            if not all(math.isfinite(component) for component in quaternion):
                raise ValueError("四元数非有限值")
            return quaternion.normalized()
    except TypeError:
        pass
    raise TypeError(f"需要四元数 (w, x, y, z)，得到 {value!r}")


def _store_double(pointer, value):
    """把 float 写入 ``C.byref(C.c_double())`` 或 ``C.c_double`` 指针。"""
    if isinstance(pointer, C.c_double):
        pointer.value = float(value)
        return
    wrapped = getattr(pointer, "_obj", None)
    if isinstance(wrapped, C.c_double):
        wrapped.value = float(value)
        return
    C.cast(pointer, C.POINTER(C.c_double))[0] = float(value)


def _read_points(pointer, stride, amount):
    """从 ``(address, stride, count)`` 读取 float32 顶点（stride 为字节）。"""
    if amount <= 0:
        return []
    address = int(pointer) if not isinstance(pointer, int) else pointer
    if stride <= 0:
        raise ValueError("stride 必须为正")
    raw = C.string_at(address, stride * amount)
    points = []
    for index in range(amount):
        x, y, z = struct.unpack_from("<3f", raw, index * stride)
        points.append(Vector((x, y, z)))
    return points


# --------------------------------------------------------------------------------------
# 形状描述：局部顶点 + 三角形；compound 递归合并为单一网格。
# --------------------------------------------------------------------------------------
class _Shape:
    """刚体碰撞形状描述（局部坐标三角网格 + 可选的单形状类型）。"""

    def __init__(self, kind, vertices, triangles, margin=DEFAULT_MARGIN):
        self.kind = kind
        self.vertices = [Vector(vertex) for vertex in vertices]
        self.triangles = [tuple(int(index) for index in triangle) for triangle in triangles]
        self.margin = float(margin)
        self.children = []  # compound: [(shape, Matrix local)]
        self.handle = _new_handle(self)


class _MeshData:
    """``RB_trimesh_data_*`` 的容器（顶点 + 三角形索引）。"""

    def __init__(self, triangle_count, vertex_count):
        self.vertices = []
        self.triangles = [None] * max(0, int(triangle_count))
        self.vertex_capacity = int(vertex_count)
        self.handle = _new_handle(self)


def _box_geometry(half_x, half_y, half_z):
    """返回盒体的 8 顶点 / 12 三角形（以原点为中心）。"""
    hx, hy, hz = abs(float(half_x)), abs(float(half_y)), abs(float(half_z))
    vertices = [
        (-hx, -hy, -hz), (hx, -hy, -hz), (hx, hy, -hz), (-hx, hy, -hz),
        (-hx, -hy, hz), (hx, -hy, hz), (hx, hy, hz), (-hx, hy, hz),
    ]
    triangles = [
        (0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7),
        (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5),
        (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7),
    ]
    return vertices, triangles


def _hull_geometry(points):
    """用 bmesh 求凸包三角网格（至少需要 4 个非共面点）。"""
    bm = bmesh.new()
    try:
        for point in points:
            bm.verts.new(point)
        bm.verts.ensure_lookup_table()
        if len(bm.verts) < 4:
            raise ValueError("RB_shape_new_convex_hull 需要至少 4 个顶点")
        bmesh.ops.remove_doubles(bm, verts=list(bm.verts), dist=1e-9)
        result = bmesh.ops.convex_hull(bm, input=list(bm.verts), use_existing_faces=False)
        geom = result.get("geom", [])
        faces = [face for face in geom if isinstance(face, bmesh.types.BMFace)]
        bmesh.ops.triangulate(bm, faces=faces or list(bm.faces))
        bm.verts.ensure_lookup_table()
        bm.faces.ensure_lookup_table()
        if not bm.faces:
            raise ValueError("RB_shape_new_convex_hull 顶点共面，无法生成凸包")
        vertices = [tuple(vertex.co) for vertex in bm.verts]
        triangles = [tuple(vertex.index for vertex in face.verts) for face in bm.faces]
    finally:
        bm.free()
    return vertices, triangles


def _geometry(shape, matrix=None):
    """把形状（含 compound）展平为局部顶点与三角形。"""
    matrix = Matrix.Identity(4) if matrix is None else matrix
    if shape.kind == "compound":
        vertices = []
        triangles = []
        for child, local in shape.children:
            child_vertices, child_triangles = _geometry(child, matrix @ local)
            offset = len(vertices)
            vertices.extend(child_vertices)
            triangles.extend(tuple(index + offset for index in triangle) for triangle in child_triangles)
        if not vertices:
            vertices, triangles = _box_geometry(DEFAULT_MARGIN, DEFAULT_MARGIN, DEFAULT_MARGIN)
        return vertices, triangles
    return [matrix @ vertex for vertex in shape.vertices], [tuple(triangle) for triangle in shape.triangles]


def _collision_shape(shape):
    """选择 Blender 碰撞形状：单盒 BOX、单凸包 CONVEX_HULL，其余 MESH。"""
    if shape.kind == "box":
        return "BOX"
    if shape.kind == "hull":
        return "CONVEX_HULL"
    return "MESH"


# --------------------------------------------------------------------------------------
# 刚体 / 约束包装
# --------------------------------------------------------------------------------------
class _Body:
    """一个 Blender 刚体对象；``object.matrix_world`` 即 Bullet 刚体位姿。"""

    def __init__(self, shape, location, rotation):
        self.shape = shape
        self.location = location
        self.rotation = rotation
        self.mass = 0.0
        self.friction = 0.8
        self.linear_damping = 0.0
        self.angular_damping = 0.0
        self.sleep_thresholds = (0.0, 0.0)
        self.activation_state = 0
        self.mask = 1
        self.enabled = False
        self.ignored = set()
        self.world = None
        self.linear_velocity = np.zeros(3)
        self.angular_velocity = np.zeros(3)
        self.pre_velocity = np.zeros(3)
        self.inertia = None
        self.handle = _new_handle(self)
        self.object = self._create_object()

    def _create_object(self):
        vertices, triangles = _geometry(self.shape)
        mesh = bpy.data.meshes.new(f"unilab-bpy-shape-{self.handle}")
        mesh.from_pydata([tuple(vertex) for vertex in vertices], [], [tuple(triangle) for triangle in triangles])
        mesh.update()
        obj = bpy.data.objects.new(f"unilab-bpy/body-{self.handle}", mesh)
        _scene().collection.objects.link(obj)
        obj.location = self.location
        obj.rotation_mode = "QUATERNION"
        obj.rotation_quaternion = self.rotation
        bpy.context.view_layer.update()
        return obj

    def rigid_body(self):
        return self.object.rigid_body

    def ensure_rigid_body(self):
        """首次加入世界时创建 Blender 刚体；已存在则复用。"""
        if self.object.rigid_body is None:
            _activate(self.object)
            bpy.ops.rigidbody.object_add()
            self.object.select_set(False)

    def apply(self, include_shape=True):
        """把描述字段写入 Blender 刚体；``include_shape`` 为假时只同步质量等标量。"""
        body = self.object.rigid_body
        if body is None:
            return
        body.type = "ACTIVE" if self.mass > 0 else "PASSIVE"
        if self.mass > 0:
            body.mass = max(float(self.mass), 1e-6)
        body.friction = float(self.friction)
        body.linear_damping = float(self.linear_damping)
        body.angular_damping = float(self.angular_damping)
        body.use_deactivation = not (self.sleep_thresholds == (0.0, 0.0)) and self.activation_state not in (0, 4)
        body.enabled = bool(self.enabled)
        if include_shape:
            body.collision_shape = _collision_shape(self.shape)
            body.mesh_source = "DEFORM"
            body.use_margin = True
            body.collision_margin = max(float(self.shape.margin), 0.0)
            for index in range(len(body.collision_collections)):
                body.collision_collections[index] = bool(self.mask)


class _Constraint:
    """Blender 刚体约束对象（空物体 + RigidBodyConstraint）。"""

    def __init__(self, kind, pivot, rotation, body_a, body_b):
        self.kind = kind
        self.pivot = pivot
        self.rotation = rotation
        self.body_a = body_a
        self.body_b = body_b
        self.handle = _new_handle(self)
        self.object = self._create_object()
        self.data = self.object.rigid_body_constraint

    def _create_object(self):
        obj = bpy.data.objects.new(f"unilab-bpy/constraint-{self.kind}-{self.handle}", None)
        _scene().collection.objects.link(obj)
        obj.location = self.pivot
        obj.rotation_mode = "QUATERNION"
        obj.rotation_quaternion = self.rotation
        bpy.context.view_layer.update()
        _activate(obj)
        bpy.ops.rigidbody.constraint_add()
        obj.select_set(False)
        data = obj.rigid_body_constraint
        data.type = self.kind.upper()
        data.object1 = self.body_a.object
        data.object2 = self.body_b.object
        data.disable_collisions = True
        return obj

    def apply_ignore(self):
        """已有约束连接的刚体对可以真正关闭物理碰撞（native ``lab_ignore``）。"""
        if self.body_b.handle in self.body_a.ignored or self.body_a.handle in self.body_b.ignored:
            self.data.disable_collisions = True


# --------------------------------------------------------------------------------------
# 世界
# --------------------------------------------------------------------------------------
class World:
    """``bullet_native.World`` 的 bpy 实现：一个 Blender 场景 = 一个 Bullet 世界。"""

    def __init__(self, gravity=(0.0, 0.0, -GRAVITY), fresh=True, solver_iterations=1000, split_impulse=True):
        self.scene = _scene()
        if fresh:
            _reset_scene(self.scene)
        self.scene.render.fps = STEP_HZ
        self.scene.render.fps_base = 1.0
        self.scene.gravity = tuple(float(value) for value in gravity)
        if self.scene.rigidbody_world is None:
            bpy.ops.rigidbody.world_add()
        world = self.scene.rigidbody_world
        world.enabled = True
        world.substeps_per_frame = 1
        world.solver_iterations = max(1, min(int(solver_iterations), 1000))
        world.use_split_impulse = bool(split_impulse)
        world.time_scale = 1.0
        world.point_cache.frame_start = 0
        world.point_cache.frame_end = 100000
        self.ptr = self
        self.names = {}
        self.bodies = {}
        self.constraints = {}
        self.tick = 0
        self.frame = 0
        self._geometry_cache = None
        self.scene.frame_set(0)

    # -- 与 bullet_native.World 相同的构造 API -----------------------------------------
    def body(self, name, shape, matrix, mass, mask=1):
        matrix = np.asarray(matrix, dtype=float)
        handle = RB_body_new(shape, V3(*matrix[:3, 3]), V4(*quat(matrix)))
        RB_body_set_mass(handle, mass)
        RB_body_set_friction(handle, 0.8)
        RB_body_set_damping(handle, 0.0, 0.0)
        RB_body_set_sleep_thresh(handle, 0.0, 0.0)
        RB_body_set_activation_state(handle, 0)
        RB_dworld_add_body(self.ptr, handle, mask)
        self.names[handle] = name
        return handle

    def box(self, name, half, position, mass=0, mask=1):
        shape = RB_shape_new_box(*half)
        RB_shape_set_margin(shape, 1e-4)
        matrix = np.eye(4)
        matrix[:3, 3] = position
        return self.body(name, shape, matrix, mass, mask)

    def step(self):
        for body in self.bodies.values():
            body.pre_velocity = body.linear_velocity.copy()
        self._geometry_cache = None
        self.frame += 1
        self.scene.frame_set(self.frame)
        self._measure()
        self.tick += 1

    def contacts(self):
        rows = _contact_rows(self)
        return [
            {
                "a": self.names.get(row["a"]),
                "b": self.names.get(row["b"]),
                "position": list(row["position"]),
                "normal": list(row["normal"]),
                "separation": float(row["separation"]),
                "normal_force_n": float(row["normal_force_n"]),
            }
            for row in rows
        ]

    def body_for_object(self, obj):
        for body in self.bodies.values():
            if body.object == obj:
                return body
        return None

    def depsgraph(self):
        return bpy.context.evaluated_depsgraph_get()

    # -- 内部 -------------------------------------------------------------------------
    def _measure(self):
        """由相邻帧位姿差估计线速度/角速度（bpy 不暴露刚体速度）。"""
        for body in self.bodies.values():
            matrix = body.object.matrix_world
            position = matrix.translation.copy()
            rotation = matrix.to_quaternion()
            previous = getattr(body, "_previous", None)
            if previous is not None:
                body.linear_velocity = (np.array(position) - np.array(previous[0])) / STEP_SECONDS
                delta = rotation @ previous[1].inverted()
                angle = float(delta.angle)
                axis = delta.axis if angle > 1e-9 else Vector((0.0, 0.0, 0.0))
                body.angular_velocity = np.array(axis) * angle / STEP_SECONDS
            body._previous = (position, rotation)


# --------------------------------------------------------------------------------------
# 世界几何 / 接触
# --------------------------------------------------------------------------------------
def _world_geometry(body):
    """返回刚体在当前位姿下的世界空间 BVH、三角形重心/法向与包围盒。"""
    local_vertices, local_triangles = _geometry(body.shape)
    matrix = body.object.matrix_world
    vertices = [matrix @ vertex for vertex in local_vertices]
    centroids = []
    normals = []
    for triangle in local_triangles:
        points = [vertices[index] for index in triangle]
        centroids.append((points[0] + points[1] + points[2]) / 3.0)
        normal = (points[1] - points[0]).cross(points[2] - points[0])
        normals.append(normal.normalized() if normal.length > 1e-12 else Vector((0.0, 0.0, 1.0)))
    bvh = BVHTree.FromPolygons(
        [tuple(vertex) for vertex in vertices], local_triangles, all_triangles=True
    )
    low = Vector((min(vertex[i] for vertex in vertices) for i in range(3)))
    high = Vector((max(vertex[i] for vertex in vertices) for i in range(3)))
    sample = range(len(local_triangles))
    if len(local_triangles) > 64:
        stride = int(math.ceil(len(local_triangles) / 64.0))
        sample = range(0, len(local_triangles), stride)
    return {
        "body": body,
        "vertices": vertices,
        "triangles": local_triangles,
        "centroids": centroids,
        "normals": normals,
        "bvh": bvh,
        "low": low,
        "high": high,
        "sample": list(sample),
    }


def _aabb_near(geometry_a, geometry_b, threshold):
    """包围盒（各轴外扩 threshold）是否相交，用于快速排除远距离刚体对。"""
    for axis in range(3):
        if geometry_a["high"][axis] + threshold < geometry_b["low"][axis]:
            return False
        if geometry_b["high"][axis] + threshold < geometry_a["low"][axis]:
            return False
    return True


def _may_contact(body_a, body_b):
    """按 mask 与 lab_ignore 记录判断刚体对是否参与接触。"""
    if body_a.mask == 0 or body_b.mask == 0:
        return False
    return body_b.handle not in body_a.ignored and body_a.handle not in body_b.ignored


def _dedupe(rows):
    """按位置聚类去掉同一接触区域的重复三角形对。"""
    kept = []
    for row in sorted(rows, key=lambda item: item["separation"]):
        if any((row["position"] - other["position"]).length < CONTACT_CLUSTER for other in kept):
            continue
        kept.append(row)
    return kept


def _pair_contacts(geometry_a, geometry_b):
    """计算一对刚体的接触行（位置/法向/分离距离，法向由 B 指向 A）。"""
    handle_a = geometry_a["body"].handle
    handle_b = geometry_b["body"].handle
    rows = []
    overlap = geometry_a["bvh"].overlap(geometry_b["bvh"])
    if overlap:
        for index_a, index_b in overlap:
            point_a = geometry_a["centroids"][index_a]
            point_b = geometry_b["centroids"][index_b]
            normal = geometry_b["normals"][index_b]
            # 与 native 一致：法向由 B 指向 A。
            if (point_a - point_b).dot(normal) < 0.0:
                normal = -normal
            rows.append(
                {
                    "a": handle_a,
                    "b": handle_b,
                    "position": (point_a + point_b) / 2.0,
                    "normal": normal.copy(),
                    "separation": -1e-6,
                }
            )
    else:
        for source, target, flip in ((geometry_a, geometry_b, False), (geometry_b, geometry_a, True)):
            for index in source["sample"]:
                point = source["centroids"][index]
                hit = target["bvh"].find_nearest(point, CONTACT_DISTANCE)
                if hit is None or hit[0] is None:
                    continue
                location, normal, _triangle, _distance = hit
                signed = (point - location).dot(normal)
                rows.append(
                    {
                        "a": handle_a,
                        "b": handle_b,
                        "position": Vector(location),
                        "normal": (-Vector(normal)) if flip else Vector(normal),
                        "separation": float(signed),
                    }
                )
    return _dedupe(rows)


def _contact_rows(world):
    """收集当前世界的接触并估算法向冲量（native ``lab_contacts`` 的等价物）。"""
    bodies = [body for body in world.bodies.values() if body.enabled and body.mask != 0]
    if len(bodies) < 2:
        return []
    if world._geometry_cache is None:
        world._geometry_cache = {body.handle: _world_geometry(body) for body in bodies}
    cache = world._geometry_cache
    pairs = {}
    for body_a, body_b in combinations(bodies, 2):
        if not _may_contact(body_a, body_b):
            continue
        geometry_a = cache[body_a.handle]
        geometry_b = cache[body_b.handle]
        if not _aabb_near(geometry_a, geometry_b, CONTACT_DISTANCE):
            continue
        rows = _pair_contacts(geometry_a, geometry_b)
        if rows:
            pairs[(body_a.handle, body_b.handle)] = rows
    if not pairs:
        return []

    pair_count = {}
    for handle_a, handle_b in pairs:
        pair_count[handle_a] = pair_count.get(handle_a, 0) + 1
        pair_count[handle_b] = pair_count.get(handle_b, 0) + 1

    rows = []
    for (handle_a, handle_b), contacts in pairs.items():
        body_a = _lookup(handle_a, _Body)
        body_b = _lookup(handle_b, _Body)
        mass_a = body_a.mass if body_a.mass > 0 else 0.0
        mass_b = body_b.mass if body_b.mass > 0 else 0.0
        if mass_a <= 0 and mass_b <= 0:
            continue  # 两个静态刚体之间不产生接触（与 Bullet 活动集一致）
        if mass_a > 0 and mass_b > 0:
            effective_mass = mass_a * mass_b / (mass_a + mass_b)
        else:
            effective_mass = max(mass_a, mass_b)
        dynamic_counts = [pair_count[handle] for handle, mass in ((handle_a, mass_a), (handle_b, mass_b)) if mass > 0]
        share = 1.0 / max(dynamic_counts)
        normal = contacts[0]["normal"]
        weight = effective_mass * GRAVITY * share
        closing = max(0.0, -float((body_a.pre_velocity - body_b.pre_velocity).dot(normal)))
        impact = effective_mass * closing / STEP_SECONDS * share
        force = weight + impact
        for contact in contacts:
            contact["normal_force_n"] = force / len(contacts)
            contact["impulse"] = contact["normal_force_n"] * STEP_SECONDS
            rows.append(contact)
    return rows


# --------------------------------------------------------------------------------------
# B：与 bullet_native 相同名字的刚体/形状/世界/约束函数
# --------------------------------------------------------------------------------------
def RB_shape_new_box(half_x, half_y, half_z):
    vertices, triangles = _box_geometry(half_x, half_y, half_z)
    return _Shape("box", vertices, triangles).handle


def RB_shape_new_convex_hull(vertices, stride, count, scale=0.0, embedded=None):
    points = _read_points(vertices, int(stride), int(count))
    if float(scale) not in (0.0, 1.0):
        points = [point * float(scale) for point in points]
    hull_vertices, hull_triangles = _hull_geometry(points)
    return _Shape("hull", hull_vertices, hull_triangles).handle


def RB_shape_new_compound():
    return _Shape("compound", [], []).handle


def RB_compound_add_child_shape(compound, child, location, rotation):
    shape = _lookup(compound, _Shape)
    if shape.kind != "compound":
        raise TypeError("RB_compound_add_child_shape 的第一个参数必须是 compound 形状")
    local = Matrix.Translation(_vec3(location)) @ _quat(rotation).to_matrix().to_4x4()
    shape.children.append((_lookup(child, _Shape), local))


def RB_trimesh_data_new(triangle_count, vertex_count):
    return _MeshData(triangle_count, vertex_count).handle


def RB_trimesh_add_vertices(data, vertices, count, stride):
    mesh = _lookup(data, _MeshData)
    mesh.vertices.extend(_read_points(vertices, int(stride), int(count)))


def RB_trimesh_add_triangle_indices(data, index, first, second, third):
    mesh = _lookup(data, _MeshData)
    if index >= len(mesh.triangles):
        mesh.triangles.extend([None] * (index + 1 - len(mesh.triangles)))
    mesh.triangles[int(index)] = (int(first), int(second), int(third))


def RB_trimesh_finish(data):
    _lookup(data, _MeshData)


def RB_shape_new_gimpact_mesh(data):
    mesh = _lookup(data, _MeshData)
    triangles = [triangle for triangle in mesh.triangles if triangle is not None]
    if not triangles:
        raise ValueError("RB_shape_new_gimpact_mesh 未收到任何三角形")
    return _Shape("trimesh", mesh.vertices, triangles).handle


def RB_shape_new_trimesh(data):
    return RB_shape_new_gimpact_mesh(data)


def RB_shape_set_margin(shape, margin):
    _lookup(shape, _Shape).margin = float(margin)


def RB_body_new(shape, location, rotation):
    body = _Body(_lookup(shape, _Shape), _vec3(location), _quat(rotation))
    return body.handle


def RB_body_set_mass(body, mass):
    target = _lookup(body, _Body)
    target.mass = float(mass)
    if target.rigid_body() is not None:
        target.apply(include_shape=False)


def RB_body_set_friction(body, friction):
    target = _lookup(body, _Body)
    target.friction = float(friction)
    if target.rigid_body() is not None:
        target.rigid_body().friction = float(friction)


def RB_body_set_damping(body, linear, angular):
    target = _lookup(body, _Body)
    target.linear_damping = float(linear)
    target.angular_damping = float(angular)
    if target.rigid_body() is not None:
        target.rigid_body().linear_damping = float(linear)
        target.rigid_body().angular_damping = float(angular)


def RB_body_set_sleep_thresh(body, linear, angular):
    target = _lookup(body, _Body)
    target.sleep_thresholds = (float(linear), float(angular))
    if target.rigid_body() is not None:
        target.apply(include_shape=False)


def RB_body_set_activation_state(body, state):
    target = _lookup(body, _Body)
    target.activation_state = int(state)
    if target.rigid_body() is not None:
        target.apply(include_shape=False)


def RB_body_set_linear_velocity(body, velocity):
    _lookup(body, _Body)
    _warn("RB_body_set_linear_velocity (line velocity cannot be written through bpy)")


def RB_body_set_angular_velocity(body, velocity):
    _lookup(body, _Body)
    _warn("RB_body_set_angular_velocity (angular velocity cannot be written through bpy)")


def RB_body_set_loc_rot(body, location, rotation):
    target = _lookup(body, _Body)
    target.location = _vec3(location)
    target.rotation = _quat(rotation)
    target.object.location = target.location
    target.object.rotation_mode = "QUATERNION"
    target.object.rotation_quaternion = target.rotation
    bpy.context.view_layer.update()
    if target.enabled:
        _warn("RB_body_set_loc_rot during simulation (Blender point cache overrides body transforms)")


def RB_body_apply_central_force(body, force):
    _lookup(body, _Body)
    _warn("RB_body_apply_central_force (forces cannot be applied through bpy)")


def RB_body_get_position(body, out):
    position = _lookup(body, _Body).object.matrix_world.translation
    out[0], out[1], out[2] = position.x, position.y, position.z


def RB_body_get_orientation(body, out):
    rotation = _lookup(body, _Body).object.matrix_world.to_quaternion()
    out[0], out[1], out[2], out[3] = rotation.w, rotation.x, rotation.y, rotation.z


def RB_body_get_linear_velocity(body, out):
    velocity = _lookup(body, _Body).linear_velocity
    out[0], out[1], out[2] = float(velocity[0]), float(velocity[1]), float(velocity[2])


def RB_body_get_angular_velocity(body, out):
    velocity = _lookup(body, _Body).angular_velocity
    out[0], out[1], out[2] = float(velocity[0]), float(velocity[1]), float(velocity[2])


def RB_dworld_new(gravity=None):
    return World(gravity=_vec3(gravity) if gravity is not None else (0.0, 0.0, -GRAVITY))


def RB_dworld_add_body(world, body, mask=1):
    target = _lookup(world, World)
    rigid = _lookup(body, _Body)
    rigid.world = target
    rigid.mask = int(mask)
    rigid.ensure_rigid_body()
    rigid.enabled = True
    rigid.apply()
    target.bodies[rigid.handle] = rigid
    target._geometry_cache = None
    bpy.context.view_layer.update()


def RB_dworld_remove_body(world, body):
    target = _lookup(world, World)
    rigid = _lookup(body, _Body)
    rigid.enabled = False
    if rigid.rigid_body() is not None:
        rigid.rigid_body().enabled = False
    target.bodies.pop(rigid.handle, None)
    target._geometry_cache = None


def RB_dworld_step_simulation(world, delta, max_substeps=0, fixed_time_step=None):
    target = _lookup(world, World)
    if abs(float(delta) - STEP_SECONDS) > 1e-9:
        raise ValueError(
            f"bullet_bpy 固定步长为 {STEP_SECONDS} 秒（fps={STEP_HZ}, substeps=1），收到 {delta}"
        )
    target.step()


def RB_dworld_set_solver_iterations(world, iterations):
    _lookup(world, World).scene.rigidbody_world.solver_iterations = max(1, min(int(iterations), 1000))


def RB_dworld_set_split_impulse(world, enabled):
    _lookup(world, World).scene.rigidbody_world.use_split_impulse = bool(enabled)


def RB_dworld_add_constraint(world, constraint, disable_collisions=1):
    target = _lookup(world, World)
    wrapper = _lookup(constraint, _Constraint)
    wrapper.data.disable_collisions = bool(disable_collisions)
    wrapper.apply_ignore()
    target.constraints[wrapper.handle] = wrapper


def RB_constraint_set_enabled(constraint, enabled):
    _lookup(constraint, _Constraint).data.enabled = bool(enabled)


def RB_constraint_set_solver_iterations(constraint, iterations):
    data = _lookup(constraint, _Constraint).data
    data.use_override_solver_iterations = True
    data.solver_iterations = max(1, min(int(iterations), 1000))


def RB_constraint_set_enable_motor(constraint, linear, angular):
    data = _lookup(constraint, _Constraint).data
    data.use_motor_lin = bool(linear)
    data.use_motor_ang = bool(angular)


def RB_constraint_set_max_impulse_motor(constraint, linear, angular):
    data = _lookup(constraint, _Constraint).data
    data.motor_lin_max_impulse = float(linear)
    data.motor_ang_max_impulse = float(angular)


def RB_constraint_set_target_velocity_motor(constraint, linear, angular):
    data = _lookup(constraint, _Constraint).data
    data.motor_lin_target_velocity = float(linear)
    data.motor_ang_target_velocity = float(angular)


def RB_constraint_set_limits_hinge(constraint, lower, upper):
    data = _lookup(constraint, _Constraint).data
    # Bullet 的 btHingeConstraint 在 lower > upper 时视为未启用限位（SZLab 用 (1,-1) 表达该意图）。
    if float(lower) > float(upper):
        data.use_limit_ang_z = False
        return
    data.use_limit_ang_z = True
    data.limit_ang_z_lower = float(lower)
    data.limit_ang_z_upper = float(upper)


def RB_constraint_set_limits_slider(constraint, lower, upper):
    data = _lookup(constraint, _Constraint).data
    if float(lower) > float(upper):
        data.use_limit_lin_x = False
        return
    data.use_limit_lin_x = True
    data.limit_lin_x_lower = float(lower)
    data.limit_lin_x_upper = float(upper)


def _constraint_new(kind, pivot, rotation, body_a, body_b):
    return _Constraint(
        kind, _vec3(pivot), _quat(rotation), _lookup(body_a, _Body), _lookup(body_b, _Body)
    ).handle


def RB_constraint_new_fixed(pivot, rotation, body_a, body_b):
    return _constraint_new("fixed", pivot, rotation, body_a, body_b)


def RB_constraint_new_hinge(pivot, rotation, body_a, body_b):
    return _constraint_new("hinge", pivot, rotation, body_a, body_b)


def RB_constraint_new_slider(pivot, rotation, body_a, body_b):
    return _constraint_new("slider", pivot, rotation, body_a, body_b)


def RB_constraint_new_motor(pivot, rotation, body_a, body_b):
    return _constraint_new("motor", pivot, rotation, body_a, body_b)


# --------------------------------------------------------------------------------------
# E：native 侧扩展（接触/射线/惯量/忽略/外力）
# --------------------------------------------------------------------------------------
def lab_contacts(world, out, capacity):
    target = _lookup(world, World)
    rows = _contact_rows(target)
    if len(rows) > int(capacity):
        return -len(rows) - 1
    for index, row in enumerate(rows):
        entry = out[index]
        entry.a = int(row["a"])
        entry.b = int(row["b"])
        for axis in range(3):
            entry.position[axis] = float(row["position"][axis])
            entry.normal[axis] = float(row["normal"][axis])
        entry.separation = float(row["separation"])
        entry.impulse = float(row["impulse"])
    return len(rows)


def lab_scalar_bytes():
    return 8


def lab_friction_2d(world):
    _lookup(world, World)


def lab_inertia(body, mass, x, y, z):
    target = _lookup(body, _Body)
    target.inertia = (float(mass), float(x), float(y), float(z))
    _warn("lab_inertia (Blender derives the inertia tensor from the collision shape)")


def lab_ignore(body_a, body_b, ignore=1):
    first = _lookup(body_a, _Body)
    second = _lookup(body_b, _Body)
    if ignore:
        first.ignored.add(second.handle)
        second.ignored.add(first.handle)
    else:
        first.ignored.discard(second.handle)
        second.ignored.discard(first.handle)
    for world in {first.world, second.world}:
        for constraint in (world.constraints.values() if world is not None else ()):
            if {constraint.body_a.handle, constraint.body_b.handle} == {first.handle, second.handle}:
                constraint.data.disable_collisions = bool(ignore)
    for world in {first.world, second.world}:
        if world is not None:
            world._geometry_cache = None


def lab_force_at(body, force, point):
    _lookup(body, _Body)
    _warn("lab_force_at (forces cannot be applied through bpy)")


def lab_ray(world, origin, target, exclude=0, distance=None):
    """沿线段做几何射线检测，保持 native 的最近命中 + 5mm/100mm 过滤语义。"""
    scene_world = _lookup(world, World)
    start = _vec3(origin)
    end = _vec3(target)
    delta = end - start
    length = delta.length
    if length <= 0.0:
        return 0
    direction = delta.normalized()
    excluded = 0
    if isinstance(exclude, int):
        excluded = exclude
    elif exclude is not None:
        excluded = _lookup(exclude, _Body).handle
    depsgraph = scene_world.depsgraph()
    limit = min(length, RAY_MAXIMUM)
    cursor = 0.0
    for _ in range(8):
        remaining = limit - cursor
        if remaining <= RAY_MINIMUM * 0.5:
            return 0
        hit, location, _normal, _index, obj, _matrix = scene_world.scene.ray_cast(
            depsgraph, start + direction * cursor, direction, distance=remaining
        )
        if not hit:
            return 0
        travelled = (location - start).length
        body = scene_world.body_for_object(obj)
        if body is not None and body.handle != excluded and travelled >= RAY_MINIMUM:
            if distance is not None:
                _store_double(distance, travelled)
            return body.handle
        cursor = travelled + 1e-5
    return 0


class _Namespace:
    """``B`` / ``E`` 命名空间：与 native ``CDLL`` 一样按名字取函数。"""

    def __init__(self, name):
        self._name = name

    def __getattr__(self, attribute):
        try:
            return globals()[attribute]
        except KeyError:
            raise AttributeError(f"{self._name} 没有 {attribute}") from None


B = _Namespace("B")
E = _Namespace("E")


# --------------------------------------------------------------------------------------
# 与 native 模块同形的辅助函数
# --------------------------------------------------------------------------------------
def pose(body):
    """返回刚体位姿的 4x4 numpy 矩阵。"""
    return np.array(_lookup(body, _Body).object.matrix_world)


def velocity(body, angular=False):
    """返回刚体线速度或角速度（由位姿差分估计）。"""
    target = _lookup(body, _Body)
    return np.array(target.angular_velocity if angular else target.linear_velocity)


def quat(matrix):
    """4x4 矩阵 -> (w, x, y, z) 的 ``V4``，与 native ``quat`` 相同。"""
    return V4(*Matrix(matrix.tolist()).to_quaternion())


__all__ = [
    "BACKEND",
    "PIN",
    "MANIFEST",
    "NATIVE_PIN",
    "LIMITATIONS",
    "STEP_SECONDS",
    "STEP_HZ",
    "Contact",
    "B",
    "E",
    "P",
    "F",
    "I",
    "V3",
    "V4",
    "C",
    "np",
    "bpy",
    "Matrix",
    "Quaternion",
    "Vector",
    "World",
    "pose",
    "velocity",
    "quat",
    "lab_contacts",
    "lab_ray",
    "lab_scalar_bytes",
    "lab_friction_2d",
    "lab_inertia",
    "lab_ignore",
    "lab_force_at",
    "record_native_error",
]
