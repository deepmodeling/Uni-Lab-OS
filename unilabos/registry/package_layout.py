"""驱动包目录布局：导入根、包根与 AST 扫描入口（运行时挂载、package inspect、package install 共用）。

同一个驱动包无论经 ``--devices``、``unilab package inspect`` 还是 ``unilab package install``
进入系统，都必须推导出同一套模块路径与设备 id；这里集中这些规则，避免各入口各写一份。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Union

PathLike = Union[str, Path]


def find_import_root(directory: PathLike) -> Path:
    """返回 *directory* 所在顶层包的父目录，即导入它时应在 ``sys.path`` 上的目录。

    - 常规包（含 ``__init__.py``）沿父目录上溯，直到第一个不含 ``__init__.py`` 的目录：
      挂载 ``<repo>/pkg/devices/family`` 时导入根是 ``<repo>``，模块路径为
      ``pkg.devices.family.<模块>``，与 ``import pkg`` 一致；
    - 本身不是常规包的目录（命名空间目录）按顶层包处理，返回其父目录（``--devices`` 的既有约定）。
    """

    path = Path(directory).resolve()
    if not (path / "__init__.py").is_file():
        return path.parent
    current = path
    while (current / "__init__.py").is_file() and current.parent != current:
        current = current.parent
    return current


def find_project_root(start: PathLike, max_up: int = 6) -> Optional[Path]:
    """从 *start* 起（含自身）向上最多 *max_up* 层，定位含 ``pyproject.toml`` 的包根。"""

    current = Path(start).resolve()
    for _ in range(max_up + 1):
        if (current / "pyproject.toml").is_file():
            return current
        if current.parent == current:
            break
        current = current.parent
    return None


def declared_registry_paths(project_root: PathLike) -> List[Path]:
    """pyproject ``[tool.unilabos.registry].paths``：相对包根解析，只返回存在的目录。"""

    import tomllib

    pyproject = Path(project_root) / "pyproject.toml"
    if not pyproject.is_file():
        return []
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    tool = data.get("tool") if isinstance(data, dict) else None
    registry_config = (((tool or {}).get("unilabos") or {}).get("registry") or {}) if isinstance(tool, dict) else {}
    raw_paths = registry_config.get("paths") if isinstance(registry_config, dict) else None
    if not isinstance(raw_paths, list):
        return []
    paths: List[Path] = []
    for raw_path in raw_paths:
        if isinstance(raw_path, str):
            candidate = (Path(project_root) / raw_path).resolve()
            if candidate.is_dir():
                paths.append(candidate)
    return paths


def is_registry_root(path: PathLike) -> bool:
    """目录是否为注册表根：含 ``devices/`` ``device_comms/`` ``resources/`` 任一子目录且其中有 YAML。"""

    root = Path(path)
    if not root.is_dir():
        return False
    for sub in ("devices", "device_comms", "resources"):
        sub_path = root / sub
        if sub_path.is_dir() and next(sub_path.rglob("*.yaml"), None) is not None:
            return True
    return False


def mount_module_prefix(mount: PathLike) -> str:
    """挂载目录对应的模块前缀（相对导入根），如 ``<repo>/pkg/devices/family`` → ``pkg.devices.family``。"""

    path = Path(mount).resolve()
    return ".".join(path.relative_to(find_import_root(path)).parts)


def module_in_prefix(module: Any, prefix: Optional[str]) -> bool:
    """注册表条目的 ``class.module``（``a.b.c:Class``）是否落在模块前缀 *prefix* 之内。"""

    if not prefix:
        return True
    if not isinstance(module, str) or not module:
        return False
    module_name = module.split(":", 1)[0]
    return module_name == prefix or module_name.startswith(prefix + ".")


@dataclass(frozen=True)
class RegistryRoot:
    """一个驱动包挂载目录对应的 YAML 注册表根。

    ``recursive``：``devices/`` 与 ``device_comms/`` 是否包含子目录（挂载目录本身兜底当注册表根时
    只读顶层，不进驱动代码目录）。
    ``module_prefix``：挂载的是包里的子目录（如一个设备家族）时，挂载目录之外的包级注册表只采纳
    ``class.module`` 落在该子包内的条目；``None`` 表示整份加载。
    """

    path: Path
    recursive: bool = True
    module_prefix: Optional[str] = None


def registry_roots_for_mount(mount: PathLike) -> List[RegistryRoot]:
    """一个 ``--devices`` 目录（或 ``package install`` 登记的包目录）内嵌的 YAML 注册表根。

    注册表根与 ``unilabos/registry`` 同构（``ROOT/{devices,device_comms,resources}/**/*.yaml``）。
    候选按顺序、去重，只返回确实含 YAML 的根：

    1. ``<挂载目录>/registry``、``<挂载目录>/unilabos_registry``；
    2. 包根（向上找 ``pyproject.toml``）声明的 ``[tool.unilabos.registry].paths``；
    3. ``<包根>/registry``、``<包根>/unilabos_registry``；
    4. ``<挂载目录>`` 本身（直接挂载注册表根的兜底，只读顶层）。

    运行时、``package inspect`` 与 ``package install`` 台账都用这一份规则。
    """

    base = Path(mount).resolve()
    import_root = find_import_root(base)
    # 挂载的是子包（导入根不是挂载目录的父目录）：挂载目录之外的注册表按模块前缀过滤
    prefix = mount_module_prefix(base) if import_root != base.parent else None
    candidates: List[RegistryRoot] = [RegistryRoot(base / "registry"), RegistryRoot(base / "unilabos_registry")]
    # 子包挂载：上溯到导入根之前的各级包目录（如 <repo>/pkg/registry）
    ancestor = base.parent
    while prefix and ancestor != import_root and import_root in ancestor.parents:
        candidates.append(RegistryRoot(ancestor / "registry", module_prefix=prefix))
        candidates.append(RegistryRoot(ancestor / "unilabos_registry", module_prefix=prefix))
        ancestor = ancestor.parent
    project_root = find_project_root(base)
    if project_root is not None:
        candidates.extend(RegistryRoot(path, module_prefix=prefix) for path in declared_registry_paths(project_root))
        candidates.append(RegistryRoot(project_root / "registry", module_prefix=prefix))
        candidates.append(RegistryRoot(project_root / "unilabos_registry", module_prefix=prefix))
    candidates.append(RegistryRoot(base, recursive=False))

    roots: List[RegistryRoot] = []
    seen: set = set()
    for candidate in candidates:
        resolved = candidate.path.resolve()
        if resolved in seen or not is_registry_root(resolved):
            continue
        seen.add(resolved)
        # 注册表就在挂载目录内：整份加载
        inside = resolved == base or base in resolved.parents
        roots.append(RegistryRoot(resolved, candidate.recursive, None if inside else candidate.module_prefix))
    return roots


def device_yaml_files(root: RegistryRoot) -> List[Path]:
    """注册表根下的设备 YAML：``devices/`` 与 ``device_comms/``（与 ``Registry.load_device_types`` 一致）。"""

    pattern = "**/*.yaml" if root.recursive else "*.yaml"
    files: List[Path] = []
    for sub in ("devices", "device_comms"):
        files.extend(sorted((root.path / sub).glob(pattern)))
    return files


def community_registry_file(mount: PathLike) -> Optional[Path]:
    """挂载目录根的 ``registry.yaml`` / ``registry.yml``（社区包单文件注册表），没有返回 ``None``。"""

    for name in ("registry.yaml", "registry.yml"):
        candidate = Path(mount) / name
        if candidate.is_file():
            return candidate
    return None


def _read_device_entries_from_files(files: Iterable[Path]) -> Dict[str, Dict[str, Any]]:
    import yaml

    from unilabos.registry.utils.yaml_ref import resolve_yaml_refs
    from unilabos.utils import logger

    entries: Dict[str, Dict[str, Any]] = {}
    for yaml_path in files:
        try:
            data = resolve_yaml_refs(yaml.safe_load(yaml_path.read_text(encoding="utf-8")), base_file=yaml_path)
        except Exception as exc:  # noqa: BLE001 - 单个文件损坏不影响其它条目
            logger.warning(f"[package] 解析注册表 {yaml_path} 失败: {exc}")
            continue
        if not isinstance(data, dict):
            continue
        for device_id, entry in data.items():
            if not isinstance(entry, dict) or str(device_id) in entries:
                continue
            cls = entry.get("class") if isinstance(entry.get("class"), dict) else {}
            if cls.get("module") or entry.get("resource_type") == "device":
                entries[str(device_id)] = entry
    return entries


def read_yaml_device_entries(roots: Iterable[RegistryRoot]) -> Dict[str, Dict[str, Any]]:
    """读取注册表根里的设备条目（展开跨文件 ``$ref``），返回 {device_id: 条目}；先出现的 id 优先。

    只采纳带 ``class.module`` 或显式 ``resource_type: device`` 的条目，与运行时加载口径一致。
    """

    entries: Dict[str, Dict[str, Any]] = {}
    for root in roots:
        for device_id, entry in _read_device_entries_from_files(device_yaml_files(root)).items():
            module = (entry.get("class") or {}).get("module") if isinstance(entry.get("class"), dict) else None
            if device_id not in entries and module_in_prefix(module, root.module_prefix):
                entries[device_id] = entry
    return entries


def package_registry_roots(mounts: Iterable[PathLike]) -> List[RegistryRoot]:
    """一组挂载目录的注册表根（去重，保持顺序）。"""

    roots: List[RegistryRoot] = []
    seen: set = set()
    for mount in mounts:
        for root in registry_roots_for_mount(mount):
            key = (root.path, root.module_prefix)
            if key not in seen:
                seen.add(key)
                roots.append(root)
    return roots


def package_device_ids(mounts: Iterable[PathLike]) -> List[str]:
    """一组挂载目录在运行时会注册的设备 id：``@device`` AST 扫描 ∪ 根目录 ``registry.yaml`` ∪
    注册表根里的 YAML（与 ``Registry.setup`` 对 ``--devices`` 的加载范围一致）。"""

    mounts = [Path(item) for item in mounts]
    ids = set(scan_package_dirs(mounts).get("devices", {}))
    community_files = [path for path in (community_registry_file(mount) for mount in mounts) if path is not None]
    ids.update(_read_device_entries_from_files(community_files))
    ids.update(read_yaml_device_entries(package_registry_roots(mounts)))
    return sorted(ids)


def scan_package_dirs(
    package_dirs: Iterable[PathLike],
    *,
    executor: Optional[ThreadPoolExecutor] = None,
) -> Dict[str, Any]:
    """AST 扫描一组驱动包目录（不 import），模块路径按各自的导入根推导。

    返回与 ``scan_directory`` 相同形状的合并结果：``devices`` / ``resources`` / ``workflows`` /
    ``_scan_errors``；同一 id 出现在两个目录时抛 ``ValueError``。
    """

    from unilabos.registry.ast_registry_scanner import scan_directory

    merged: Dict[str, Any] = {"devices": {}, "resources": {}, "workflows": {}, "_scan_errors": []}
    own_executor = executor is None
    pool = executor or ThreadPoolExecutor(max_workers=4, thread_name_prefix="PackageScan")
    try:
        for item in package_dirs:
            directory = Path(item).resolve()
            if not directory.is_dir():
                continue
            result = scan_directory(directory, python_path=find_import_root(directory), executor=pool)
            for kind in ("devices", "resources"):
                for key, meta in result.get(kind, {}).items():
                    if key in merged[kind]:
                        existing = merged[kind][key].get("file_path", "?")
                        raise ValueError(
                            f"@{kind[:-1]} id 重复: '{key}' 同时出现在 {existing} 和 {meta.get('file_path', '?')}"
                        )
                    merged[kind][key] = meta
            merged["workflows"].update(result.get("workflows", {}))
            merged["_scan_errors"].extend(result.get("_scan_errors", []))
    finally:
        if own_executor:
            pool.shutdown(wait=True)
    return merged


def format_scan_errors(errors: List[Dict[str, str]]) -> str:
    return "；".join(f"{item.get('file')}: {item.get('error')}" for item in errors)


__all__ = [
    "RegistryRoot",
    "community_registry_file",
    "declared_registry_paths",
    "device_yaml_files",
    "find_import_root",
    "find_project_root",
    "format_scan_errors",
    "is_registry_root",
    "module_in_prefix",
    "mount_module_prefix",
    "package_device_ids",
    "package_registry_roots",
    "read_yaml_device_entries",
    "registry_roots_for_mount",
    "scan_package_dirs",
]
