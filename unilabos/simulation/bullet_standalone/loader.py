"""独立 Bullet 库的 ctypes 加载器（Windows / Linux / macOS 同源）。

构建产物是单个动态库（``.dll`` / ``.so`` / ``.dylib``），内部静态链接
conda-forge ``bullet-cpp`` 的 float64 Bullet；不再依赖 Blender 进程内符号。

环境变量：

===============================  ==============================================
``UNILABOS_BULLET_ROOT``         含 Bullet 的 conda 前缀（缺省尝试 CONDA_PREFIX）
``UNILABOS_BULLET_INCLUDE_DIR``  直接指定含 btBulletDynamicsCommon.h 的目录
``UNILABOS_BULLET_LIBRARY_DIR``  直接指定含 Bullet*-float64 库的目录
``UNILABOS_BULLET_TOOLS_PREFIX`` cmake/ninja/clang++ 所在的 conda 前缀
``UNILABOS_BULLET_TOOLS_DIR``    只含构建工具的 bin 目录
``UNILABOS_BULLET_CMAKE``        指定 cmake 可执行文件
``UNILABOS_BULLET_NINJA``        指定 ninja 可执行文件
``UNILABOS_BULLET_CXX``          指定 C++ 编译器
``UNILABOS_BULLET_THREADSAFE``   ``1``/``0``，默认按 conda float64 构建视为 1
``UNILABOS_BULLET_STANDALONE``   直接加载已有的动态库，跳过构建
``UNILABOS_BULLET_STANDALONE_CACHE`` 构建缓存目录
``UNILABOS_BULLET_STANDALONE_REBUILD`` ``1`` 强制重建
``UNILABOS_BULLET_STANDALONE_REQUIRE`` ``1`` 缺少工具链时失败而不是跳过测试
===============================  ==============================================
"""

from __future__ import annotations

import ctypes as C
import hashlib
import json
import os
import platform as _platform
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
CPP_DIR = HERE / "cpp"
SOURCES = sorted(CPP_DIR.glob("src/*.h")) + sorted(CPP_DIR.glob("src/*.cpp")) + [CPP_DIR / "CMakeLists.txt"]
PLATFORM = _platform.system()
MACHINE = _platform.machine()
LIB_NAMES = {
    "Windows": "unilab_bullet_standalone.dll",
    "Linux": "unilab_bullet_standalone.so",
    "Darwin": "unilab_bullet_standalone.dylib",
}
LIB_NAME = LIB_NAMES.get(PLATFORM)
EXPECTED_ABI_VERSION = 1
BUILD_ERROR_TAIL = 40


class LoaderError(RuntimeError):
    """工具链或 Bullet 布局不满足构建条件。"""


class BulletBuildError(RuntimeError):
    """CMake 配置或编译失败。"""


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return None
    return value.strip()


def require_environment() -> bool:
    return (_env("UNILABOS_BULLET_STANDALONE_REQUIRE") or "").lower() in {"1", "true", "yes"}


def _exe_suffix() -> str:
    return ".exe" if PLATFORM == "Windows" else ""


def _tool_dirs(prefix: Path | None) -> list[Path]:
    dirs: list[Path] = []
    tools_dir = _env("UNILABOS_BULLET_TOOLS_DIR")
    if tools_dir:
        dirs.append(Path(tools_dir))
    if prefix:
        dirs.extend([prefix / "Library" / "bin", prefix / "bin", prefix / "Scripts"])
    return dirs


def find_tool(name: str, prefix: Path | None = None) -> Path | None:
    override = _env("UNILABOS_BULLET_" + name.replace("++", "CXX").replace("-", "_").upper())
    if override:
        path = Path(override)
        if not path.is_file():
            raise LoaderError("环境变量指定的 %s 不存在: %s" % (name, path))
        return path
    candidates = [name + ".exe", name] if PLATFORM == "Windows" else [name]
    for directory in _tool_dirs(prefix):
        for candidate in candidates:
            path = directory / candidate
            if path.is_file():
                return path
    found = shutil.which(name)
    return Path(found) if found else None


def bullet_layout(prefix: Path | None = None) -> tuple[Path, Path]:
    """返回（头文件目录，库目录）。要求找到 float64 变体的 BulletDynamics。"""

    include_override = _env("UNILABOS_BULLET_INCLUDE_DIR")
    library_override = _env("UNILABOS_BULLET_LIBRARY_DIR")
    root_value = _env("UNILABOS_BULLET_ROOT") or os.environ.get("CONDA_PREFIX")
    root = Path(prefix) if prefix else (Path(root_value) if root_value else None)

    include_dir = Path(include_override) if include_override else None
    library_dir = Path(library_override) if library_override else None

    if include_dir and not (include_dir / "btBulletDynamicsCommon.h").is_file():
        raise LoaderError(
            "UNILABOS_BULLET_INCLUDE_DIR 下缺少 btBulletDynamicsCommon.h: %s" % include_dir
        )

    if root is not None:
        bases = [root, root / "Library"] if PLATFORM == "Windows" else [root]
        for base in bases:
            candidate_include = base / "include" / "bullet"
            candidate_library = base / "lib"
            has_headers = (candidate_include / "btBulletDynamicsCommon.h").is_file()
            has_float64 = any(candidate_library.glob("BulletDynamics-float64.*")) or any(
                candidate_library.glob("libBulletDynamics-float64.*")
            )
            if include_dir is None and has_headers:
                include_dir = candidate_include
            if library_dir is None and has_float64:
                library_dir = candidate_library
            if include_dir and library_dir:
                break

    if include_dir is None or library_dir is None:
        raise LoaderError(
            "找不到 Bullet float64 头文件/库；请安装 conda-forge bullet-cpp 并设置 "
            "UNILABOS_BULLET_ROOT（当前: %r）" % (str(root) if root else None)
        )

    has_dynamics = any(library_dir.glob("BulletDynamics-float64.*")) or any(
        library_dir.glob("libBulletDynamics-float64.*")
    )
    if not has_dynamics:
        raise LoaderError(
            "%s 下没有 BulletDynamics-float64 库；独立桥要求 double 精度 Bullet" % library_dir
        )
    return include_dir, library_dir


def _tool_prefix() -> Path | None:
    explicit = _env("UNILABOS_BULLET_TOOLS_PREFIX") or _env("UNILABOS_BULLET_ROOT")
    if explicit:
        return Path(explicit)
    try:
        include_dir, _ = bullet_layout()
    except LoaderError:
        return None
    # <prefix>/Library/include/bullet 或 <prefix>/include/bullet
    if include_dir.name == "bullet":
        include_root = include_dir.parent
        if include_root.name == "include":
            base = include_root.parent
            return base.parent if base.name == "Library" else base
    return None


def _compiler(prefix: Path | None) -> Path | None:
    explicit = _env("UNILABOS_BULLET_CXX") or _env("CXX")
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise LoaderError("C++ 编译器不存在: %s" % path)
        return path
    for name in ("clang++", "g++", "c++", "clang-cl", "cl"):
        found = find_tool(name, prefix)
        if found:
            return found
    return None


def _resource_compiler(prefix: Path | None) -> Path | None:
    """CMake 的 Windows-Clang 平台模块要求 RC 编译器；这里找到 llvm-rc 或 SDK rc。"""

    explicit = _env("UNILABOS_BULLET_RC")
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise LoaderError("UNILABOS_BULLET_RC 不存在: %s" % path)
        return path
    for directory in _tool_dirs(prefix):
        for name in ("llvm-rc.exe", "rc.exe"):
            path = directory / name
            if path.is_file():
                return path
    if PLATFORM != "Windows":
        return None
    program_files = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
    roots = [
        program_files / "Windows Kits" / "10" / "bin",
        Path(os.environ.get("WindowsSdkDir", "")) / "bin" if os.environ.get("WindowsSdkDir") else None,
    ]
    candidates: list[tuple[tuple[int, ...], Path]] = []
    for root in roots:
        if not root or not root.is_dir():
            continue
        for version in root.iterdir():
            if not version.is_dir():
                continue
            for arch in ("x64", "x86"):
                path = version / arch / "rc.exe"
                if path.is_file():
                    numbers = tuple(int(part) for part in version.name.split(".") if part.isdigit())
                    candidates.append((numbers, path))
    if candidates:
        return max(candidates, key=lambda item: item[0])[1]
    return None


def _threadsafe() -> bool:
    override = _env("UNILABOS_BULLET_THREADSAFE")
    if override is not None:
        return override.lower() in {"1", "true", "yes", "on"}
    # conda-forge 的 bullet-cpp float64 变体用 -DBULLET2_MULTITHREADING=ON 构建，
    # 但安装的 BulletConfig 不导出 BT_THREADSAFE。默认按 1 处理，未知构建可显式关闭。
    return True


def build_available() -> tuple[bool, str]:
    explicit = _env("UNILABOS_BULLET_STANDALONE")
    if explicit:
        path = Path(explicit)
        return (path.is_file(), "UNILABOS_BULLET_STANDALONE 不存在: %s" % path)
    prebuilt = HERE / "lib" / PLATFORM.lower() / LIB_NAME if LIB_NAME else None
    if prebuilt and prebuilt.is_file():
        return True, ""

    problems: list[str] = []
    prefix = _tool_prefix()
    try:
        bullet_layout()
    except LoaderError as error:
        problems.append(str(error))
    try:
        if not find_tool("cmake", prefix):
            problems.append("找不到 cmake")
        if not find_tool("ninja", prefix):
            problems.append("找不到 ninja")
        if not _compiler(prefix):
            problems.append("找不到 C++ 编译器（clang++/g++/cl）")
        if PLATFORM == "Windows" and not _resource_compiler(prefix):
            problems.append("找不到 Windows 资源编译器（llvm-rc 或 SDK rc.exe）")
    except LoaderError as error:
        problems.append(str(error))
    if problems:
        return False, "独立 Bullet 不可用: " + "; ".join(problems)
    return True, ""


def availability() -> tuple[bool, str]:
    """测试夹具入口：显式库路径、预编译产物或可构建工具链任一满足即可。"""

    return build_available()


def _run(command: list[str], *, cwd: Path, extra_path: list[Path], log_path: Path) -> subprocess.CompletedProcess:
    environment = os.environ.copy()
    path_parts = [str(path) for path in extra_path if path]
    if environment.get("PATH"):
        path_parts.append(environment["PATH"])
    environment["PATH"] = os.pathsep.join(path_parts)
    result = subprocess.run(
        command,
        cwd=str(cwd),
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    with log_path.open("a", encoding="utf-8") as stream:
        stream.write("$ " + " ".join(command) + "\n")
        stream.write(result.stdout or "")
        stream.write("\n[exit %d]\n\n" % result.returncode)
    return result


def _tool_identity(path: Path | None, extra_path: list[Path]) -> str:
    if not path:
        return "missing"
    environment = os.environ.copy()
    parts = [str(item) for item in extra_path if item]
    if environment.get("PATH"):
        parts.append(environment["PATH"])
    environment["PATH"] = os.pathsep.join(parts)
    try:
        output = subprocess.run(
            [str(path), "--version"],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        ).stdout
    except Exception:  # noqa: BLE001 - 版本探测失败不应阻塞构建
        return str(path)
    return str(path) + "|" + (output or "").splitlines()[0][:120]


def _digest(include_dir: Path, library_dir: Path, tools: dict[str, Path | None]) -> str:
    digest = hashlib.sha256()
    for source in SOURCES:
        digest.update(source.name.encode())
        digest.update(source.read_bytes())
    digest.update(PLATFORM.encode())
    digest.update(MACHINE.encode())
    digest.update(str(sys.maxsize).encode())
    digest.update(str(include_dir).encode())
    digest.update(str(library_dir).encode())
    digest.update(str(_threadsafe()).encode())
    for name in ("cmake", "ninja", "cxx"):
        digest.update(name.encode())
        digest.update(str(tools.get(name)).encode())
    digest.update(str(tools.get("rc")).encode())
    return digest.hexdigest()


def cache_root() -> Path:
    override = _env("UNILABOS_BULLET_STANDALONE_CACHE")
    if override:
        return Path(override)
    if PLATFORM == "Windows":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "unilabos" / "bullet-standalone"


def _artifact(build_dir: Path) -> Path:
    direct = build_dir / LIB_NAME
    if direct.is_file():
        return direct
    matches = [path for path in build_dir.rglob(LIB_NAME) if path.is_file()]
    if not matches:
        raise BulletBuildError("构建完成但找不到 %s（目录 %s）" % (LIB_NAME, build_dir))
    return max(matches, key=lambda path: path.stat().st_mtime)


def _cmake_path(path: Path | str) -> str:
    """CMake 会把 -D 值原样写进 .cmake 文件；Windows 反斜杠会变成非法转义。"""

    return str(path).replace("\\", "/")


def build_library(target_dir: Path) -> Path:
    """在 target_dir 中配置/编译独立库，返回产物路径。"""

    include_dir, library_dir = bullet_layout()
    prefix = _tool_prefix()
    cmake = find_tool("cmake", prefix)
    ninja = find_tool("ninja", prefix)
    compiler = _compiler(prefix)
    resource = _resource_compiler(prefix)
    if not cmake:
        raise BulletBuildError("找不到 cmake")
    if not ninja:
        raise BulletBuildError("找不到 ninja（独立 Bullet 构建固定使用 Ninja 生成器）")
    if not compiler:
        raise BulletBuildError("找不到 C++ 编译器")
    if PLATFORM == "Windows" and not resource:
        raise BulletBuildError("找不到 Windows 资源编译器（llvm-rc 或 SDK rc.exe）")

    target_dir.mkdir(parents=True, exist_ok=True)
    build_dir = target_dir / "build"
    build_dir.mkdir(parents=True, exist_ok=True)
    log_path = target_dir / "build.log"
    if log_path.exists():
        log_path.unlink()

    tools = {"cmake": cmake, "ninja": ninja, "cxx": compiler, "rc": resource}
    configure = [
        str(cmake),
        "-S",
        _cmake_path(CPP_DIR),
        "-B",
        _cmake_path(build_dir),
        "-G",
        "Ninja",
        "-DCMAKE_BUILD_TYPE=Release",
        "-DCMAKE_CXX_COMPILER=" + _cmake_path(compiler),
        "-DCMAKE_MAKE_PROGRAM=" + _cmake_path(ninja),
        "-DUNILAB_BULLET_INCLUDE_DIR=" + _cmake_path(include_dir),
        "-DUNILAB_BULLET_LIBRARY_DIR=" + _cmake_path(library_dir),
        "-DUNILAB_BULLET_THREADSAFE=" + ("ON" if _threadsafe() else "OFF"),
        "-DUNILAB_BULLET_BUILD_SMOKE_TEST="
        + ("ON" if (_env("UNILABOS_BULLET_STANDALONE_SMOKE") or "") == "1" else "OFF"),
    ]
    if resource:
        configure.append("-DCMAKE_RC_COMPILER=" + _cmake_path(resource))
    extra_path = _tool_dirs(prefix) + [compiler.parent, cmake.parent, ninja.parent]
    if resource:
        extra_path.append(resource.parent)

    result = _run(configure, cwd=target_dir, extra_path=extra_path, log_path=log_path)
    if result.returncode != 0:
        raise BulletBuildError(
            "CMake 配置失败（%s）\n%s" % (result.returncode, _tail(log_path))
        )
    build = [str(cmake), "--build", str(build_dir), "--config", "Release", "--parallel"]
    result = _run(build, cwd=target_dir, extra_path=extra_path, log_path=log_path)
    if result.returncode != 0:
        raise BulletBuildError(
            "编译失败（%s）\n%s" % (result.returncode, _tail(log_path))
        )
    artifact = _artifact(build_dir)
    target = target_dir / LIB_NAME
    shutil.copy2(artifact, target)
    (target_dir / "manifest.json").write_text(
        json.dumps(
            {
                "library": LIB_NAME,
                "library_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                "bullet_include_dir": str(include_dir),
                "bullet_library_dir": str(library_dir),
                "bullet_threadsafe": _threadsafe(),
                "cmake": _tool_identity(cmake, extra_path),
                "ninja": _tool_identity(ninja, extra_path),
                "cxx": _tool_identity(compiler, extra_path),
                "platform": PLATFORM,
                "machine": MACHINE,
                "built_utc": datetime.now(timezone.utc).isoformat(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return target


def _tail(log_path: Path, lines: int = BUILD_ERROR_TAIL) -> str:
    if not log_path.is_file():
        return ""
    content = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(content[-lines:])


def library_path(*, rebuild: bool | None = None) -> Path:
    explicit = _env("UNILABOS_BULLET_STANDALONE")
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise LoaderError("UNILABOS_BULLET_STANDALONE 不存在: %s" % path)
        return path
    prebuilt = HERE / "lib" / PLATFORM.lower() / LIB_NAME if LIB_NAME else None
    if prebuilt and prebuilt.is_file():
        return prebuilt
    if LIB_NAME is None:
        raise LoaderError("不支持的平台: %s" % PLATFORM)

    include_dir, library_dir = bullet_layout()
    prefix = _tool_prefix()
    tools = {
        "cmake": find_tool("cmake", prefix),
        "ninja": find_tool("ninja", prefix),
        "cxx": _compiler(prefix),
        "rc": _resource_compiler(prefix),
    }
    digest = _digest(include_dir, library_dir, tools)
    target_dir = cache_root() / digest
    target = target_dir / LIB_NAME
    manifest = target_dir / "manifest.json"
    force = rebuild if rebuild is not None else (_env("UNILABOS_BULLET_STANDALONE_REBUILD") or "") == "1"
    if not force and target.is_file() and manifest.is_file():
        try:
            recorded = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            recorded = {}
        if recorded.get("library_sha256") == hashlib.sha256(target.read_bytes()).hexdigest():
            return target
    if not tools["cmake"] or not tools["ninja"] or not tools["cxx"]:
        raise BulletBuildError(
            "需要 cmake、ninja 与 C++ 编译器；当前 cmake=%r ninja=%r cxx=%r"
            % (tools["cmake"], tools["ninja"], tools["cxx"])
        )
    if PLATFORM == "Windows" and not tools["rc"]:
        raise BulletBuildError("需要 Windows 资源编译器（llvm-rc 或 SDK rc.exe）")
    return build_library(target_dir)


class BulletABI:
    """已绑定签名的独立 Bullet 库句柄。"""

    Contact = type(
        "Contact",
        (C.Structure,),
        {
            "_fields_": [
                ("a", C.c_void_p),
                ("b", C.c_void_p),
                ("position", C.c_double * 3),
                ("normal", C.c_double * 3),
                ("separation", C.c_double),
                ("impulse", C.c_double),
            ]
        },
    )

    def __init__(self, path: Path):
        self.path = Path(path)
        self.cdll = C.CDLL(str(self.path))
        self.B = self.cdll
        self.E = self.cdll
        self._bind()
        scalar_bytes = self.cdll.lab_scalar_bytes()
        if scalar_bytes != 8:
            raise LoaderError(
                "独立 Bullet 的 btScalar 为 %d 字节，兼容层要求 double（8 字节）" % scalar_bytes
            )
        version = self.cdll.lab_abi_version()
        if version != EXPECTED_ABI_VERSION:
            raise LoaderError("独立 Bullet ABI 版本 %d != 期望 %d" % (version, EXPECTED_ABI_VERSION))

    def _bind(self) -> None:
        pointer = C.c_void_p
        float_type = C.c_float
        int_type = C.c_int
        for name, restype, argtypes in RB_BINDINGS:
            function = getattr(self.cdll, name)
            function.restype = restype
            function.argtypes = argtypes

        self.cdll.lab_contacts.restype = int_type
        self.cdll.lab_contacts.argtypes = [pointer, C.POINTER(self.Contact), int_type]
        self.cdll.lab_scalar_bytes.restype = int_type
        self.cdll.lab_abi_version.restype = int_type
        self.cdll.lab_friction_2d.argtypes = [pointer]
        self.cdll.lab_friction_2d.restype = None
        self.cdll.lab_inertia.argtypes = [
            pointer, C.c_double, C.c_double, C.c_double, C.c_double
        ]
        self.cdll.lab_inertia.restype = None
        self.cdll.lab_ignore.argtypes = [pointer, pointer, int_type]
        self.cdll.lab_ignore.restype = None
        self.cdll.lab_force_at.argtypes = [pointer, C.POINTER(C.c_double), C.POINTER(C.c_double)]
        self.cdll.lab_force_at.restype = None
        self.cdll.lab_shape_aabb.argtypes = [
            pointer, C.POINTER(C.c_double), C.POINTER(C.c_double)
        ]
        self.cdll.lab_shape_aabb.restype = None
        self.cdll.lab_ray.argtypes = [
            pointer, C.POINTER(C.c_double), C.POINTER(C.c_double), pointer,
            C.POINTER(C.c_double),
        ]
        self.cdll.lab_ray.restype = pointer

    def scalar_bytes(self) -> int:
        return self.cdll.lab_scalar_bytes()

    def abi_version(self) -> int:
        return self.cdll.lab_abi_version()


P = C.c_void_p
F = C.c_float
I = C.c_int

# 与 unilabos.simulation.bullet_native 的绑定表逐项一致，另外补充清理函数。
RB_BINDINGS = [
    ("RB_dworld_remove_body", None, [P, P]),
    ("RB_dworld_new", P, [P]),
    ("RB_dworld_delete", None, [P]),
    ("RB_dworld_add_body", None, [P, P, I]),
    ("RB_dworld_step_simulation", None, [P, F, I, F]),
    ("RB_dworld_set_solver_iterations", None, [P, I]),
    ("RB_dworld_set_split_impulse", None, [P, I]),
    ("RB_shape_new_box", P, [F, F, F]),
    ("RB_body_new", P, [P, P, P]),
    ("RB_body_delete", None, [P]),
    ("RB_body_set_mass", None, [P, F]),
    ("RB_shape_set_margin", None, [P, F]),
    ("RB_body_set_friction", None, [P, F]),
    ("RB_body_set_damping", None, [P, F, F]),
    ("RB_body_set_sleep_thresh", None, [P, F, F]),
    ("RB_body_set_activation_state", None, [P, I]),
    ("RB_body_get_position", None, [P, P]),
    ("RB_body_get_orientation", None, [P, P]),
    ("RB_body_get_linear_velocity", None, [P, P]),
    ("RB_body_get_angular_velocity", None, [P, P]),
    ("RB_body_set_linear_velocity", None, [P, P]),
    ("RB_body_set_angular_velocity", None, [P, P]),
    ("RB_body_set_loc_rot", None, [P, P, P]),
    ("RB_body_apply_central_force", None, [P, P]),
    ("RB_shape_new_compound", P, []),
    ("RB_compound_add_child_shape", None, [P, P, P, P]),
    ("RB_shape_new_convex_hull", P, [P, I, I, F, P]),
    ("RB_trimesh_data_new", P, [I, I]),
    ("RB_trimesh_add_vertices", None, [P, P, I, I]),
    ("RB_trimesh_add_triangle_indices", None, [P, I, I, I, I]),
    ("RB_trimesh_finish", None, [P]),
    ("RB_shape_new_gimpact_mesh", P, [P]),
    ("RB_shape_new_trimesh", P, [P]),
    ("RB_shape_delete", None, [P]),
    ("RB_constraint_set_solver_iterations", None, [P, I]),
    ("RB_dworld_add_constraint", None, [P, P, I]),
    ("RB_constraint_set_enabled", None, [P, I]),
    ("RB_constraint_set_enable_motor", None, [P, I, I]),
    ("RB_constraint_set_max_impulse_motor", None, [P, F, F]),
    ("RB_constraint_set_target_velocity_motor", None, [P, F, F]),
    ("RB_constraint_set_limits_hinge", None, [P, F, F]),
    ("RB_constraint_set_limits_slider", None, [P, F, F]),
    ("RB_constraint_delete", None, [P]),
] + [
    ("RB_constraint_new_" + kind, P, [P, P, P, P])
    for kind in ("fixed", "hinge", "slider", "motor")
]

_LOADED: BulletABI | None = None


def load(*, rebuild: bool | None = None, path: Path | None = None) -> BulletABI:
    """加载（必要时构建）独立 Bullet 库。"""

    global _LOADED
    if path is None and _LOADED is not None and not rebuild:
        return _LOADED
    target = Path(path) if path else library_path(rebuild=rebuild)
    abi = BulletABI(target)
    if path is None:
        _LOADED = abi
    return abi
