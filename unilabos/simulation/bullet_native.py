"""Blender Bullet 后端选择：原生 ``RB_*`` 桥 → 独立 Bullet DLL → bpy 公共 API。

三条路径共用同一 ``World`` / ``B`` / ``E`` / ``pose`` / ``velocity`` / ``quat`` 模块面，
上层（``bullet_usd``、SZLab ``blender_bullet``）无需按后端分支：

* **native**：``ctypes`` 直接使用 Blender 进程内的 ``RB_*`` 符号，要求头部源码 PIN、
  缓存目录、macOS 动态库编译等条件；Windows 版 Blender 不导出这些符号。
* **standalone**：加载 :mod:`unilabos.simulation.bullet_standalone` 独立编译的 Bullet
  动态库（Windows ``.dll`` / Linux ``.so`` / macOS ``.dylib``），不依赖 Blender 进程内
  符号；失败原因见 ``STANDALONE_ERROR``。
* **bpy**：只用 ``bpy`` / ``mathutils`` 公共刚体 API（:mod:`unilabos.simulation.bullet_bpy`）。

``UNILABOS_BULLET_BACKEND``：

* 未设置或 ``auto``：原生桥可用就用原生；否则独立库 ``load()`` 成功就用独立库；
  都不可用时回退 bpy（需要 ``bpy``，否则抛出汇总了三条路径原始错误的 ``RuntimeError``）；
* ``native``：必须用原生桥，失败直接抛出原始错误（用于原生验收）；
* ``standalone``：必须用独立库，失败直接抛出原始错误；
* ``bpy``：跳过前两者，直接使用 bpy 公共 API。

``BACKEND`` 取 ``native`` / ``standalone`` / ``bpy``；``NATIVE_ERROR`` 记录原生桥的原始
失败、``STANDALONE_ERROR`` 记录独立库的原始失败，失败不会被静默冒充成别的后端。
``PIN`` 是所选后端的身份摘要：native 为固定的 Blender 提交，bpy 为当前 Blender 构建
哈希，standalone 为独立库 sha256 的前缀。``MANIFEST`` 是记录 ``backend``、
``blender_version`` 与（独立库时）``library_path`` / ``abi`` 的 JSON 清单路径。
"""
import ctypes as C
import hashlib
import json
import os
import platform
import subprocess
import sys
import tempfile
import warnings
from pathlib import Path

import numpy as np

REQUESTED_BACKEND = os.environ.get('UNILABOS_BULLET_BACKEND', '').strip().lower() or 'auto'
if REQUESTED_BACKEND not in ('auto', 'native', 'standalone', 'bpy'):
    raise RuntimeError("UNILABOS_BULLET_BACKEND 只能是 auto/native/standalone/bpy")
BACKEND = None
NATIVE_ERROR = None
STANDALONE_ERROR = None
NATIVE_PIN = 'd13f752e3b9c4f8c261cda552b1021f8bcc0382c'
PIN = None
MANIFEST = None
LIMITATIONS = []

STANDALONE_LIMITATIONS = [
    "independent Bullet library build; not the Blender-embedded Bullet build",
    "GImpact/convex collision; not PhysX SDF",
    "solver and constraint numerics can differ from Blender's internal Bullet",
]


def _native_bridge():
    """构建原生 ``RB_*`` 桥，返回模块面字典；任何前置条件不满足都抛原始错误。"""
    import bpy
    from mathutils import Matrix, Quaternion

    def build_hash():
        """返回当前 Blender 构建哈希文本（不同版本可能是 bytes 或 str）。"""
        value = bpy.app.build_hash
        return value.decode() if isinstance(value, bytes) else str(value)

    HERE = Path(__file__).resolve().parent
    SOURCE = Path(os.environ['UNILABOS_BULLET_HEADERS']).resolve(strict=True)
    pin = NATIVE_PIN
    if not pin.startswith(build_hash()): raise RuntimeError('bullet_build_mismatch')
    if subprocess.check_output(['git', '-C', str(SOURCE), 'rev-parse', 'HEAD'], text=True).strip() != pin: raise RuntimeError('bullet_headers_mismatch')
    if subprocess.check_output(['git', '-C', str(SOURCE), 'status', '--porcelain', '--', 'extern/bullet2/src'], text=True).strip(): raise RuntimeError('bullet_headers_dirty')
    CACHE = Path(os.environ['UNILABOS_BULLET_CACHE']).resolve()
    CACHE.mkdir(parents=True, exist_ok=True)
    if platform.system() != 'Darwin': raise RuntimeError('bullet_platform_unqualified')
    SOURCE_CPP = HERE / 'bullet_contacts.cpp'
    DIGEST = hashlib.sha256(SOURCE_CPP.read_bytes() + pin.encode() + platform.machine().encode()).hexdigest()
    LIB = CACHE / ('contacts-' + DIGEST + '.dylib')
    manifest = LIB.with_suffix('.json')

    def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()

    if not LIB.exists() or not manifest.exists() or json.loads(manifest.read_text()).get('library_sha256') != sha(LIB):
        with tempfile.TemporaryDirectory(dir=CACHE) as temp:
            target = Path(temp) / 'contacts.dylib'
            subprocess.run(['clang++', '-std=c++17', '-O2', '-DBT_USE_DOUBLE_PRECISION', '-dynamiclib', '-undefined', 'dynamic_lookup', '-I' + str(SOURCE / 'extern/bullet2/src'), str(SOURCE_CPP), '-o', str(target)], check=True)
            target.replace(LIB)
        manifest.write_text(json.dumps({'blender_commit': pin, 'source_sha256': sha(SOURCE_CPP), 'library_sha256': sha(LIB), 'architecture': platform.machine()}, indent=2))
    B = C.CDLL(None); E = C.CDLL(str(LIB)); P = C.c_void_p; F = C.c_float; I = C.c_int
    V3 = F * 3; V4 = F * 4

    def bind(name, ret, args):
        f = getattr(B, name); f.restype = ret; f.argtypes = args; return f
    for name, ret, args in [
        ('RB_dworld_remove_body', None, [P, P]), ('RB_dworld_new', P, [P]), ('RB_dworld_add_body', None, [P, P, I]), ('RB_dworld_step_simulation', None, [P, F, I, F]), ('RB_dworld_set_solver_iterations', None, [P, I]), ('RB_dworld_set_split_impulse', None, [P, I]),
        ('RB_shape_new_box', P, [F, F, F]), ('RB_body_new', P, [P, P, P]), ('RB_body_set_mass', None, [P, F]), ('RB_shape_set_margin', None, [P, F]),
        ('RB_body_set_friction', None, [P, F]), ('RB_body_set_damping', None, [P, F, F]), ('RB_body_set_sleep_thresh', None, [P, F, F]), ('RB_body_set_activation_state', None, [P, I]),
        ('RB_body_get_position', None, [P, P]), ('RB_body_get_orientation', None, [P, P]), ('RB_body_get_linear_velocity', None, [P, P]), ('RB_body_get_angular_velocity', None, [P, P]),
        ('RB_body_set_linear_velocity', None, [P, P]), ('RB_body_set_angular_velocity', None, [P, P]), ('RB_body_set_loc_rot', None, [P, P, P]), ('RB_body_apply_central_force', None, [P, P]),
        ('RB_shape_new_compound', P, []), ('RB_compound_add_child_shape', None, [P, P, P, P]), ('RB_shape_new_convex_hull', P, [P, I, I, F, P]),
        ('RB_trimesh_data_new', P, [I, I]), ('RB_trimesh_add_vertices', None, [P, P, I, I]), ('RB_trimesh_add_triangle_indices', None, [P, I, I, I, I]), ('RB_trimesh_finish', None, [P]), ('RB_shape_new_gimpact_mesh', P, [P]), ('RB_shape_new_trimesh', P, [P]),
        ('RB_constraint_set_solver_iterations', None, [P, I]), ('RB_dworld_add_constraint', None, [P, P, I]), ('RB_constraint_set_enabled', None, [P, I]), ('RB_constraint_set_enable_motor', None, [P, I, I]), ('RB_constraint_set_max_impulse_motor', None, [P, F, F]), ('RB_constraint_set_target_velocity_motor', None, [P, F, F]), ('RB_constraint_set_limits_hinge', None, [P, F, F]), ('RB_constraint_set_limits_slider', None, [P, F, F])]:
        bind(name, ret, args)
    for k in ('fixed', 'hinge', 'slider', 'motor'): bind('RB_constraint_new_' + k, P, [P, P, P, P])

    class Contact(C.Structure):
        _fields_ = [('a', P), ('b', P), ('position', C.c_double * 3), ('normal', C.c_double * 3), ('separation', C.c_double), ('impulse', C.c_double)]
    E.lab_contacts.restype = I; E.lab_contacts.argtypes = [P, C.POINTER(Contact), I]
    E.lab_friction_2d.argtypes = [P]; E.lab_friction_2d.restype = None
    assert E.lab_scalar_bytes() == 8

    def pose(p):
        loc = V3(); q = V4(); B.RB_body_get_position(p, loc); B.RB_body_get_orientation(p, q)
        mat = Quaternion(tuple(q)).to_matrix().to_4x4(); mat.translation = loc; return np.array(mat)

    def velocity(p, angular=False):
        v = V3(); getattr(B, 'RB_body_get_' + ('angular' if angular else 'linear') + '_velocity')(p, v); return np.array(v)

    def quat(matrix): return V4(*Matrix(matrix.tolist()).to_quaternion())

    class World:
        def __init__(self):
            self.ptr = B.RB_dworld_new(V3(0, 0, -9.81)); B.RB_dworld_set_solver_iterations(self.ptr, 1000); B.RB_dworld_set_split_impulse(self.ptr, 1)
            self.names = {}; self.buffer = (Contact * 100000)(); self.tick = 0

        def body(self, name, shape, mat, mass, mask=1):
            p = B.RB_body_new(shape, V3(*mat[:3, 3]), quat(mat)); B.RB_body_set_mass(p, mass); B.RB_body_set_friction(p, .8); B.RB_body_set_damping(p, 0, 0); B.RB_body_set_sleep_thresh(p, 0, 0); B.RB_body_set_activation_state(p, 0); B.RB_dworld_add_body(self.ptr, p, mask); self.names[p] = name; return p

        def box(self, name, half, position, mass=0, mask=1):
            shape = B.RB_shape_new_box(*half); B.RB_shape_set_margin(shape, .0001); m = np.eye(4); m[:3, 3] = position; return self.body(name, shape, m, mass, mask)

        def step(self): B.RB_dworld_step_simulation(self.ptr, 1 / 240, 0, 1 / 240); self.tick += 1

        def contacts(self):
            n = E.lab_contacts(self.ptr, self.buffer, len(self.buffer))
            if n < 0: raise RuntimeError('Contact buffer exhausted')
            return [dict(a=self.names.get(c.a), b=self.names.get(c.b), position=list(c.position), normal=list(c.normal), separation=c.separation, normal_force_n=c.impulse * 240) for c in self.buffer[:n]]
    E.lab_inertia.argtypes = [P, C.c_double, C.c_double, C.c_double, C.c_double]; E.lab_inertia.restype = None
    E.lab_ignore.argtypes = [P, P, I]; E.lab_ignore.restype = None

    E.lab_ray.argtypes = [P, C.POINTER(C.c_double), C.POINTER(C.c_double), P, C.POINTER(C.c_double)]; E.lab_ray.restype = P
    E.lab_force_at.argtypes = [P, C.POINTER(C.c_double), C.POINTER(C.c_double)]; E.lab_force_at.restype = None
    return {
        'B': B, 'E': E, 'P': P, 'F': F, 'I': I, 'V3': V3, 'V4': V4,
        'Contact': Contact, 'World': World, 'pose': pose, 'velocity': velocity,
        'quat': quat, 'PIN': pin, 'MANIFEST': manifest,
    }


def _blender_version():
    """返回当前进程可导入的 Blender 版本字符串；非 Blender 进程返回 None。"""
    try:
        import bpy
    except ImportError:
        return None
    return bpy.app.version_string


def _standalone_manifest(abi, digest):
    """写入独立库清单（记录 library_path/abi/blender_version），返回清单路径。"""
    data = {
        "backend": "standalone",
        "pinned": False,
        "native_pin": NATIVE_PIN,
        "blender_version": _blender_version(),
        "library_path": str(abi.path),
        "library_sha256": digest,
        "abi": abi.abi_version(),
        "scalar_bytes": abi.scalar_bytes(),
        "python": platform.python_version(),
        "platform": sys.platform,
        "step_seconds": 1 / 240,
        "contacts": "Bullet manifold normal impulses from the standalone library",
        "source": "unilabos/simulation/bullet_standalone/",
        "limitations": list(STANDALONE_LIMITATIONS),
        "native_error": None if NATIVE_ERROR is None else repr(NATIVE_ERROR),
    }
    candidates = [abi.path.parent / "backend-manifest.json",
                  Path(tempfile.gettempdir()) / "unilabos-bullet-standalone" / "backend-manifest.json"]
    for candidate in candidates:
        try:
            candidate.parent.mkdir(parents=True, exist_ok=True)
            candidate.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError:
            continue
        return candidate
    return candidates[-1]


def _standalone_bridge():
    """加载独立 Bullet DLL 并返回与 native 同形的模块面字典。"""
    from .bullet_standalone import compat, loader

    abi = loader.load()
    digest = hashlib.sha256(abi.path.read_bytes()).hexdigest()
    manifest = _standalone_manifest(abi, digest)
    return {
        'B': compat.B, 'E': compat.E, 'C': compat.C, 'P': C.c_void_p,
        'F': C.c_float, 'I': C.c_int, 'V3': compat.V3, 'V4': compat.V4,
        'Contact': compat.Contact, 'World': compat.World, 'pose': compat.pose,
        'velocity': compat.velocity, 'quat': compat.quat,
        'PIN': 'standalone:' + digest[:16], 'MANIFEST': manifest,
        'LIBRARY_PATH': abi.path, 'ABI_VERSION': abi.abi_version(),
    }


if REQUESTED_BACKEND in ('auto', 'native'):
    try:
        _state = _native_bridge()
    except Exception as error:  # noqa: BLE001 - 原生桥不可用的原因要原样记录并按需回退
        if REQUESTED_BACKEND == 'native':
            raise
        NATIVE_ERROR = error
    else:
        globals().update(_state)
        BACKEND = 'native'
        del _state

if BACKEND is None and REQUESTED_BACKEND in ('auto', 'standalone'):
    try:
        _state = _standalone_bridge()
    except Exception as error:  # noqa: BLE001 - 独立库不可用的原因要原样记录并按需回退
        if REQUESTED_BACKEND == 'standalone':
            raise
        STANDALONE_ERROR = error
    else:
        globals().update(_state)
        BACKEND = 'standalone'
        LIMITATIONS = list(STANDALONE_LIMITATIONS)
        del _state

if BACKEND is None:
    try:
        from .bullet_bpy import *  # noqa: F401,F403 - 复用同一 World/B/E 接口
    except ImportError as error:
        raise RuntimeError(
            "UNILABOS_BULLET_BACKEND=%s 无可用 Bullet 后端: native=%r standalone=%r bpy=%r"
            % (REQUESTED_BACKEND, NATIVE_ERROR, STANDALONE_ERROR, error)
        ) from error
    from . import bullet_bpy as _bullet_bpy
    _bullet_bpy.record_native_error(NATIVE_ERROR, STANDALONE_ERROR)
    PIN = _bullet_bpy.PIN
    MANIFEST = _bullet_bpy.MANIFEST
    BACKEND = _bullet_bpy.BACKEND
    LIMITATIONS = list(_bullet_bpy.LIMITATIONS)
    if REQUESTED_BACKEND == 'auto':
        warnings.warn(
            f'bullet_native: 原生 RB_* 桥与独立 Bullet 库都不可用，回退 bpy 公共 API 后端: '
            f'native={NATIVE_ERROR!r} standalone={STANDALONE_ERROR!r}',
            RuntimeWarning, stacklevel=2,
        )
