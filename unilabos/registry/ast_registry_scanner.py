"""
AST-based Registry Scanner

Statically parse Python files to extract @device, @action, @topic_config, @resource
decorator metadata without importing any modules. This is ~100x faster than importlib
since it only reads and parses text files.

Includes a file-level cache: each file's MD5 hash, size and mtime are tracked so
unchanged files skip AST parsing entirely. The cache is persisted as JSON in the
working directory (``unilabos_data/ast_scan_cache.json``).

Usage:
    from unilabos.registry.ast_registry_scanner import scan_directory

    # Scan all device and resource files under a package directory
    result = scan_directory("unilabos", python_path="/project")
    # => {"devices": {device_id: {...}, ...}, "resources": {resource_id: {...}, ...}}
"""

import ast
import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from unilabos.registry.backend_metadata import normalize_supported_backends
from unilabos.registry.material_locks import normalize_material_parameter_names
from unilabos.registry.utils import resolve_registry_displayname
from unilabos.resources.objects.site import normalize_available_sites


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_SCAN_DEPTH = 10      # 最大目录递归深度
MAX_SCAN_FILES = 1000    # 最大扫描文件数量
_CACHE_VERSION = 13      # 缓存/entry 构建格式版本号，格式变更时递增
_DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9_]+$")

# 合法的装饰器来源模块
_REGISTRY_DECORATOR_MODULE = "unilabos.registry.decorators"
# @subscribe 订阅装饰器来源模块（区分于注册表，这是运行时，订阅回调不应被当作 action）
_SUBSCRIBE_DECORATOR_MODULE = "unilabos.utils.decorator"
# placeholder_keys 常量来源模块（如 PLACEHOLDER_DEDUCT_RESOURCE），值需解析成字符串字面量
_PLACEHOLDER_MODULE = "unilabos.registry.placeholder_type"


@lru_cache(maxsize=1)
def _placeholder_constants() -> Dict[str, str]:
    """静态解析同目录 ``placeholder_type.py``，提取顶层 ``NAME = "str"`` 常量映射。

    让 @action 装饰器里能用 placeholder 常量替代字面量：扫描器把对应 ``ast.Name``
    解析成常量的字符串值。值由静态 AST 解析得到（不 import 该模块，保持纯文本扫描），
    与 ``placeholder_type.py`` 单一数据源（DRY）。
    """
    consts: Dict[str, str] = {}
    try:
        path = Path(__file__).with_name("placeholder_type.py")
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if (
                isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        consts[target.id] = node.value.value
    except Exception:
        pass
    return consts


def _validate_device_ids(device_ids: List[str]) -> None:
    invalid_ids = [device_id for device_id in device_ids if not _DEVICE_ID_RE.fullmatch(device_id)]
    if invalid_ids:
        raise ValueError(
            "@device id 只能包含英文、数字、下划线: "
            + ", ".join(repr(device_id) for device_id in invalid_ids)
        )


# ---------------------------------------------------------------------------
# File-level cache helpers
# ---------------------------------------------------------------------------


def _file_fingerprint(filepath: Path) -> Dict[str, Any]:
    """Return size, mtime and MD5 hash for *filepath*."""
    stat = filepath.stat()
    md5 = hashlib.md5(filepath.read_bytes()).hexdigest()
    return {"size": stat.st_size, "mtime": stat.st_mtime, "md5": md5}


def load_scan_cache(cache_path: Optional[Path]) -> Dict[str, Any]:
    """Load the AST scan cache from *cache_path*. Returns empty structure on any error."""
    if cache_path is None or not cache_path.is_file():
        return {"version": _CACHE_VERSION, "files": {}}
    try:
        raw = cache_path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if data.get("version") != _CACHE_VERSION:
            return {"version": _CACHE_VERSION, "files": {}}
        return data
    except Exception:
        return {"version": _CACHE_VERSION, "files": {}}


def save_scan_cache(cache_path: Optional[Path], cache: Dict[str, Any]) -> None:
    """Persist *cache* to *cache_path* (atomic-ish via temp file)."""
    if cache_path is None:
        return
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(cache_path)
    except Exception:
        pass


def _is_cache_hit(entry: Dict[str, Any], fp: Dict[str, Any]) -> bool:
    """Check if a cache entry matches the current file fingerprint."""
    return (
        entry.get("md5") == fp["md5"]
        and entry.get("size") == fp["size"]
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _collect_py_files(
    root_dir: Path,
    max_depth: int = MAX_SCAN_DEPTH,
    max_files: int = MAX_SCAN_FILES,
    exclude_files: Optional[set] = None,
) -> List[Path]:
    """
    收集 root_dir 下的 .py 文件，限制最大递归深度和文件数量。

    Args:
        root_dir: 扫描根目录
        max_depth: 最大递归深度 (默认 10 层)
        max_files: 最大文件数量 (默认 1000 个)
        exclude_files: 要排除的文件名集合 (如 {"lab_resources.py"})

    Returns:
        排序后的 .py 文件路径列表
    """
    result: List[Path] = []
    _exclude = exclude_files or set()

    def _walk(dir_path: Path, depth: int):
        if depth > max_depth or len(result) >= max_files:
            return
        try:
            entries = sorted(dir_path.iterdir())
        except (PermissionError, OSError):
            return
        for entry in entries:
            if len(result) >= max_files:
                return
            if entry.is_file() and entry.suffix == ".py" and not entry.name.startswith("__"):
                if entry.name not in _exclude:
                    result.append(entry)
            elif entry.is_dir() and not entry.name.startswith(("__", ".")):
                _walk(entry, depth + 1)

    _walk(root_dir, 0)
    return result


def scan_directory(
    root_dir: Union[str, Path],
    python_path: Union[str, Path] = "",
    max_depth: int = MAX_SCAN_DEPTH,
    max_files: int = MAX_SCAN_FILES,
    executor: ThreadPoolExecutor = None,
    exclude_files: Optional[set] = None,
    cache: Optional[Dict[str, Any]] = None,
    include_files: Optional[List[Union[str, Path]]] = None,
) -> Dict[str, Any]:
    """
    Recursively scan .py files under *root_dir* for @device and @resource
    decorated classes/functions.

    Uses a thread pool to parse files in parallel for faster I/O.
    When *cache* is provided, files whose fingerprint (MD5 + size) hasn't
    changed since the last scan are served from cache without re-parsing.

    Returns:
        {"devices": {device_id: meta, ...}, "resources": {resource_id: meta, ...}}

    Args:
        root_dir: Directory to scan (e.g. "unilabos/devices").
        python_path: The directory that should be on sys.path, i.e. the parent
                     of the top-level package. Module paths are derived as
                     filepath relative to this directory. If empty, defaults to
                     root_dir's parent.
        max_depth: Maximum directory recursion depth (default 10).
        max_files: Maximum number of .py files to scan (default 1000).
        executor: Shared ThreadPoolExecutor (required). The caller manages its
                  lifecycle.
        exclude_files: 要排除的文件名集合 (如 {"lab_resources.py"})
        cache: Mutable cache dict (``load_scan_cache()`` result). Hits are read
               from here; misses are written back so the caller can persist later.
        include_files: 指定扫描的文件列表，提供时跳过目录递归收集，直接扫描这些文件。
    """
    if executor is None:
        raise ValueError("executor is required and must not be None")

    root_dir = Path(root_dir).resolve()
    if not python_path:
        python_path = root_dir.parent
    else:
        python_path = Path(python_path).resolve()

    # --- Collect files (depth/count limited) ---
    if include_files is not None:
        py_files = [Path(f).resolve() for f in include_files if Path(f).resolve().exists()]
    else:
        py_files = _collect_py_files(root_dir, max_depth=max_depth, max_files=max_files, exclude_files=exclude_files)

    cache_files: Dict[str, Any] = cache.get("files", {}) if cache else {}

    # --- Parallel scan (with cache fast-path) ---
    devices: Dict[str, dict] = {}
    resources: Dict[str, dict] = {}
    cache_hits = 0
    cache_misses = 0

    def _parse_one_cached(py_file: Path) -> Tuple[List[dict], List[dict], bool]:
        """Returns (devices, resources, was_cache_hit)."""
        key = str(py_file)
        try:
            fp = _file_fingerprint(py_file)
        except OSError:
            return [], [], False

        cached_entry = cache_files.get(key)
        if cached_entry and _is_cache_hit(cached_entry, fp):
            return cached_entry.get("devices", []), cached_entry.get("resources", []), True

        try:
            devs, ress = _parse_file(py_file, python_path)
        except (SyntaxError, Exception):
            devs, ress = [], []

        cache_files[key] = {
            "md5": fp["md5"],
            "size": fp["size"],
            "mtime": fp["mtime"],
            "devices": devs,
            "resources": ress,
        }
        return devs, ress, False

    def _collect_results(futures_dict: Dict):
        nonlocal cache_hits, cache_misses
        for future in as_completed(futures_dict):
            devs, ress, hit = future.result()
            if hit:
                cache_hits += 1
            else:
                cache_misses += 1
            for dev in devs:
                device_id = dev.get("device_id")
                if device_id:
                    if device_id in devices:
                        existing = devices[device_id].get("file_path", "?")
                        new_file = dev.get("file_path", "?")
                        raise ValueError(
                            f"@device id 重复: '{device_id}' 同时出现在 {existing} 和 {new_file}"
                        )
                    devices[device_id] = dev
            for res in ress:
                resource_id = res.get("resource_id")
                if resource_id:
                    if resource_id in resources:
                        existing = resources[resource_id].get("file_path", "?")
                        new_file = res.get("file_path", "?")
                        raise ValueError(
                            f"@resource id 重复: '{resource_id}' 同时出现在 {existing} 和 {new_file}"
                        )
                    resources[resource_id] = res

    futures = {executor.submit(_parse_one_cached, f): f for f in py_files}
    _collect_results(futures)

    if cache is not None:
        cache["files"] = cache_files

    return {
        "devices": devices,
        "resources": resources,
        "_cache_stats": {"hits": cache_hits, "misses": cache_misses, "total": len(py_files)},
    }


# ---------------------------------------------------------------------------
# File-level parsing
# ---------------------------------------------------------------------------

# 已知继承自 rclpy.node.Node 的基类名 (用于 AST 静态检测)
_KNOWN_ROS2_BASE_CLASSES = {"Node", "BaseROS2DeviceNode"}
_KNOWN_ROS2_MODULES = {"rclpy", "rclpy.node"}


def _detect_class_type(cls_node: ast.ClassDef, import_map: Dict[str, str]) -> str:
    """
    检测类是否继承自 rclpy Node，返回 'ros2' 或 'python'。

    通过检查类的基类名称和 import_map 中的模块路径来判断：
    1. 基类名在已知 ROS2 基类集合中
    2. 基类在 import_map 中解析到 rclpy 相关模块
    3. 基类在 import_map 中解析到 BaseROS2DeviceNode
    """
    for base in cls_node.bases:
        base_name = ""
        if isinstance(base, ast.Name):
            base_name = base.id
        elif isinstance(base, ast.Attribute):
            base_name = base.attr
        elif isinstance(base, ast.Subscript) and isinstance(base.value, ast.Name):
            # Generic[T] 形式，如 BaseROS2DeviceNode[SomeType]
            base_name = base.value.id

        if not base_name:
            continue

        # 直接匹配已知 ROS2 基类名
        if base_name in _KNOWN_ROS2_BASE_CLASSES:
            return "ros2"

        # 通过 import_map 检查模块路径
        module_path = import_map.get(base_name, "")
        if any(mod in module_path for mod in _KNOWN_ROS2_MODULES):
            return "ros2"
        if "BaseROS2DeviceNode" in module_path:
            return "ros2"

    return "python"


def _parse_file(
    filepath: Path,
    python_path: Path,
) -> Tuple[List[dict], List[dict]]:
    """
    Parse a single .py file using ast and extract all @device-decorated classes
    and @resource-decorated functions/classes.

    Returns:
        (devices, resources) -- two lists of metadata dicts.
    """
    source = filepath.read_text(encoding="utf-8", errors="replace")
    tree = ast.parse(source, filename=str(filepath))

    # Derive module path from file path
    module_path = _filepath_to_module(filepath, python_path)

    # Build import map from the file (includes same-file class defs)
    import_map = _collect_imports(tree, module_path)
    literal_constants = _collect_literal_constants(tree, import_map)

    devices: List[dict] = []
    resources: List[dict] = []

    for node in ast.iter_child_nodes(tree):
        # --- @device on classes ---
        if isinstance(node, ast.ClassDef):
            device_decorator = _find_decorator(node, "device")
            if device_decorator is not None and _is_registry_decorator("device", import_map):
                device_args = _extract_decorator_args(device_decorator, import_map)
                # Site 数组通常较长，允许装饰器引用本文件中的静态字面量常量。
                # AST 扫描仍不执行用户代码，也不解析导入模块里的动态对象。
                for key in ("available_sites", "id_meta"):
                    value = device_args.get(key)
                    if isinstance(value, str):
                        constant_name = value.rsplit(":", 1)[-1]
                        if constant_name in literal_constants:
                            device_args[key] = literal_constants[constant_name]
                class_body = _extract_class_body(node, import_map)

                # Support ids + id_meta (multi-device) or id (single device)
                device_ids: List[str] = []
                if device_args.get("ids") is not None:
                    device_ids = list(device_args["ids"])
                else:
                    did = device_args.get("id") or device_args.get("device_id")
                    device_ids = [did] if did else [f"{module_path}:{node.name}"]

                _validate_device_ids(device_ids)
                id_meta = device_args.get("id_meta") or {}
                displayname = device_args.get("displayname", "")
                device_type = device_args.get("device_type") or _detect_class_type(
                    node, import_map
                )
                base_meta = {
                    "class_name": node.name,
                    "module": f"{module_path}:{node.name}",
                    "file_path": str(filepath).replace("\\", "/"),
                    "category": device_args.get("category", []),
                    "description": device_args.get("description", ""),
                    "displayname": displayname,
                    "icon": device_args.get("icon", ""),
                    "version": device_args.get("version", "1.0.0"),
                    "device_type": device_type,
                    "supported_backends": normalize_supported_backends(
                        device_args.get("supported_backends"),
                        device_type=device_type,
                    ),
                    "handles": device_args.get("handles", []),
                    "available_sites": normalize_available_sites(
                        device_args.get("available_sites")
                    ),
                    "model": device_args.get("model"),
                    "hardware_interface": device_args.get("hardware_interface"),
                    "actions": class_body.get("actions", {}),
                    "status_properties": class_body.get("status_properties", {}),
                    "init_params": class_body.get("init_params", []),
                    "init_docstring": class_body.get("init_docstring"),
                    "auto_methods": class_body.get("auto_methods", {}),
                    "import_map": import_map,
                }
                for did in device_ids:
                    meta = dict(base_meta)
                    meta["device_id"] = did
                    overrides = id_meta.get(did, {})
                    for key in (
                        "handles",
                        "available_sites",
                        "description",
                        "displayname",
                        "icon",
                        "model",
                        "hardware_interface",
                        "supported_backends",
                    ):
                        if key in overrides:
                            meta[key] = (
                                normalize_available_sites(overrides[key])
                                if key == "available_sites"
                                else normalize_supported_backends(
                                    overrides[key],
                                    device_type=device_type,
                                )
                                if key == "supported_backends"
                                else overrides[key]
                            )
                    meta["displayname"] = resolve_registry_displayname(meta.get("displayname"), did)
                    devices.append(meta)

            # --- @resource on classes ---
            resource_decorator = _find_decorator(node, "resource")
            if resource_decorator is not None and _is_registry_decorator("resource", import_map):
                res_meta = _extract_resource_meta(
                    resource_decorator, node.name, module_path, filepath, import_map,
                    is_function=False,
                    init_node=_find_init_in_class(node),
                )
                resources.append(res_meta)

        # --- @resource on module-level functions ---
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            resource_decorator = _find_method_decorator(node, "resource")
            if resource_decorator is not None and _is_registry_decorator("resource", import_map):
                res_meta = _extract_resource_meta(
                    resource_decorator, node.name, module_path, filepath, import_map,
                    is_function=True,
                    func_node=node,
                )
                resources.append(res_meta)

    return devices, resources


_STATIC_MODEL_CALLS = frozenset(
    {
        "unilabos.resources.objects.site:SiteDefinition",
        "unilabos.resources.objects.pose:ResourceDictPosition",
        "unilabos.resources.objects.pose:ResourceDictPositionObject",
        "unilabos.resources.objects.pose:ResourceDictPositionSize",
    }
)


def _decode_static_model_calls(value: Any) -> Any:
    """将受支持的 Pydantic 构造表达式还原成纯字典，不执行用户代码。"""

    if isinstance(value, list):
        return [_decode_static_model_calls(item) for item in value]
    if not isinstance(value, dict):
        return value

    call_name = value.get("_call")
    if call_name is not None and call_name not in _STATIC_MODEL_CALLS:
        raise ValueError(f"不支持的静态模型构造: {call_name}")
    return {
        key: _decode_static_model_calls(item)
        for key, item in value.items()
        if key != "_call"
    }


def _collect_literal_constants(
    tree: ast.Module,
    import_map: Dict[str, str],
) -> Dict[str, Any]:
    """收集模块根级的安全字面量或受支持模型常量，不执行用户代码。"""

    result: Dict[str, Any] = {}
    for statement in tree.body:
        target: Optional[ast.Name] = None
        value: Optional[ast.expr] = None
        if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
            if isinstance(statement.targets[0], ast.Name):
                target = statement.targets[0]
                value = statement.value
        elif isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
            target = statement.target
            value = statement.value
        if target is None or value is None:
            continue
        try:
            result[target.id] = ast.literal_eval(value)
        except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
            try:
                result[target.id] = _decode_static_model_calls(
                    _ast_node_to_value(value, import_map)
                )
            except (TypeError, ValueError):
                continue
    return result


def _find_init_in_class(cls_node: ast.ClassDef) -> Optional[ast.FunctionDef]:
    """Find __init__ method in a class."""
    for item in cls_node.body:
        if isinstance(item, ast.FunctionDef) and item.name == "__init__":
            return item
    return None


def _extract_resource_meta(
    decorator_node: Union[ast.Call, ast.Name],
    name: str,
    module_path: str,
    filepath: Path,
    import_map: Dict[str, str],
    is_function: bool = False,
    func_node: Optional[Union[ast.FunctionDef, ast.AsyncFunctionDef]] = None,
    init_node: Optional[ast.FunctionDef] = None,
) -> dict:
    """
    Extract resource metadata from a @resource decorator on a function or class.
    """
    res_args = _extract_decorator_args(decorator_node, import_map)

    resource_id = res_args.get("id") or res_args.get("resource_id")
    if resource_id is None:
        resource_id = f"{module_path}:{name}"

    # Extract init/function params
    init_params: List[dict] = []
    if is_function and func_node is not None:
        init_params = _extract_method_params(func_node, import_map)
    elif not is_function and init_node is not None:
        init_params = _extract_method_params(init_node, import_map)

    return {
        "resource_id": resource_id,
        "name": name,
        "module": f"{module_path}:{name}",
        "file_path": str(filepath).replace("\\", "/"),
        "is_function": is_function,
        "category": res_args.get("category", []),
        "description": res_args.get("description", ""),
        "displayname": resolve_registry_displayname(res_args.get("displayname"), resource_id),
        "icon": res_args.get("icon", ""),
        "version": res_args.get("version", "1.0.0"),
        "class_type": res_args.get("class_type", "pylabrobot"),
        "handles": res_args.get("handles", []),
        "model": res_args.get("model"),
        "init_params": init_params,
    }


# ---------------------------------------------------------------------------
# Import map collection
# ---------------------------------------------------------------------------


def _collect_imports(tree: ast.Module, module_path: str = "") -> Dict[str, str]:
    """
    Walk all Import/ImportFrom nodes in the AST tree, build a mapping from
    local name to fully-qualified import path.

    Also includes top-level class/function definitions from the same file,
    so that same-file TypedDict / Enum / dataclass references can be resolved.

    Returns:
        {"SendCmd": "unilabos_msgs.action:SendCmd",
         "StrSingleInput": "unilabos_msgs.action:StrSingleInput",
         "InputHandle": "unilabos.registry.decorators:InputHandle",
         "SetLiquidReturn": "unilabos.devices.liquid_handling.liquid_handler_abstract:SetLiquidReturn",
         ...}
    """
    import_map: Dict[str, str] = {}

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                local_name = alias.asname if alias.asname else alias.name
                import_map[local_name] = f"{module}:{alias.name}"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                local_name = alias.asname if alias.asname else alias.name
                import_map[local_name] = alias.name

    # 同文件顶层 class / function 定义
    if module_path:
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                import_map.setdefault(node.name, f"{module_path}:{node.name}")
            elif isinstance(node, ast.FunctionDef) or isinstance(node, ast.AsyncFunctionDef):
                import_map.setdefault(node.name, f"{module_path}:{node.name}")
            elif isinstance(node, ast.Assign):
                # 顶层赋值 (如 MotorAxis = Enum(...))
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        import_map.setdefault(target.id, f"{module_path}:{target.id}")

    return import_map


# ---------------------------------------------------------------------------
# Decorator finding & argument extraction
# ---------------------------------------------------------------------------


def _find_decorator(
    node: Union[ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef],
    decorator_name: str,
) -> Optional[ast.Call]:
    """
    Find a specific decorator call on a class or function definition.

    Handles both:
      - @device(...)  -> ast.Call with func=ast.Name(id="device")
      - @module.device(...) -> ast.Call with func=ast.Attribute(attr="device")
    """
    for dec in node.decorator_list:
        if isinstance(dec, ast.Call):
            if isinstance(dec.func, ast.Name) and dec.func.id == decorator_name:
                return dec
            if isinstance(dec.func, ast.Attribute) and dec.func.attr == decorator_name:
                return dec
        elif isinstance(dec, ast.Name) and dec.id == decorator_name:
            # @device without parens (unlikely but handle it)
            return None  # Can't extract args from bare decorator
    return None


def _find_method_decorator(func_node: ast.FunctionDef, decorator_name: str) -> Optional[Union[ast.Call, ast.Name]]:
    """Find a decorator on a method."""
    for dec in func_node.decorator_list:
        if isinstance(dec, ast.Call):
            if isinstance(dec.func, ast.Name) and dec.func.id == decorator_name:
                return dec
            if isinstance(dec.func, ast.Attribute) and dec.func.attr == decorator_name:
                return dec
        elif isinstance(dec, ast.Name) and dec.id == decorator_name:
            # @action without parens, or @topic_config without parens
            return dec
    return None


def _has_decorator(func_node: ast.FunctionDef, decorator_name: str) -> bool:
    """Check if a method has a specific decorator (with or without call)."""
    for dec in func_node.decorator_list:
        if isinstance(dec, ast.Call):
            if isinstance(dec.func, ast.Name) and dec.func.id == decorator_name:
                return True
            if isinstance(dec.func, ast.Attribute) and dec.func.attr == decorator_name:
                return True
        elif isinstance(dec, ast.Name) and dec.id == decorator_name:
            return True
    return False


def _is_registry_decorator(name: str, import_map: Dict[str, str]) -> bool:
    """Check that *name* was imported from ``unilabos.registry.decorators``."""
    source = import_map.get(name, "")
    return _REGISTRY_DECORATOR_MODULE in source


def _is_subscribe_decorator(name: str, import_map: Dict[str, str]) -> bool:
    """Check that *name* was imported from ``unilabos.utils.decorator`` (the @subscribe source)."""
    source = import_map.get(name, "")
    return _SUBSCRIBE_DECORATOR_MODULE in source


def _extract_decorator_args(
    node: Union[ast.Call, ast.Name],
    import_map: Dict[str, str],
) -> dict:
    """
    Extract keyword arguments from a decorator call AST node.

    Resolves Name references (e.g. SendCmd, Side.NORTH) via import_map.
    Handles literal values (strings, ints, bools, lists, dicts, None).
    """
    if isinstance(node, ast.Name):
        return {}  # Bare decorator, no args
    if not isinstance(node, ast.Call):
        return {}

    result: dict = {}

    for kw in node.keywords:
        if kw.arg is None:
            continue  # **kwargs, skip
        result[kw.arg] = _ast_node_to_value(kw.value, import_map)

    return result


# ---------------------------------------------------------------------------
# AST node value conversion
# ---------------------------------------------------------------------------


def _ast_node_to_value(node: ast.expr, import_map: Dict[str, str]) -> Any:
    """
    Convert an AST expression node to a Python value.

    Handles:
      - Literals (str, int, float, bool, None)
      - Lists, Tuples, Dicts, Sets
      - Name references (e.g. SendCmd -> resolved via import_map)
      - Attribute access (e.g. Side.NORTH -> resolved)
      - Function/class calls (e.g. InputHandle(...) -> structured dict)
      - Unary operators (e.g. -1)
    """
    # --- Constant (str, int, float, bool, None) ---
    if isinstance(node, ast.Constant):
        return node.value

    # --- Name (e.g. SendCmd, True, False, None) ---
    if isinstance(node, ast.Name):
        return _resolve_name(node.id, import_map)

    # --- Attribute (e.g. Side.NORTH, DataSource.HANDLE) ---
    if isinstance(node, ast.Attribute):
        return _resolve_attribute(node, import_map)

    # --- List ---
    if isinstance(node, ast.List):
        return [_ast_node_to_value(elt, import_map) for elt in node.elts]

    # --- Tuple ---
    if isinstance(node, ast.Tuple):
        return [_ast_node_to_value(elt, import_map) for elt in node.elts]

    # --- Dict ---
    if isinstance(node, ast.Dict):
        result = {}
        for k, v in zip(node.keys, node.values):
            if k is None:
                continue  # **kwargs spread
            key = _ast_node_to_value(k, import_map)
            val = _ast_node_to_value(v, import_map)
            result[key] = val
        return result

    # --- Set ---
    if isinstance(node, ast.Set):
        return [_ast_node_to_value(elt, import_map) for elt in node.elts]

    # --- Call (e.g. InputHandle(...), OutputHandle(...)) ---
    if isinstance(node, ast.Call):
        return _ast_call_to_value(node, import_map)

    # --- UnaryOp (e.g. -1, -0.5) ---
    if isinstance(node, ast.UnaryOp):
        if isinstance(node.op, ast.USub):
            operand = _ast_node_to_value(node.operand, import_map)
            if isinstance(operand, (int, float)):
                return -operand
        elif isinstance(node.op, ast.Not):
            operand = _ast_node_to_value(node.operand, import_map)
            return not operand

    # --- BinOp (e.g. "a" + "b") ---
    if isinstance(node, ast.BinOp):
        if isinstance(node.op, ast.Add):
            left = _ast_node_to_value(node.left, import_map)
            right = _ast_node_to_value(node.right, import_map)
            if isinstance(left, str) and isinstance(right, str):
                return left + right

    # --- JoinedStr (f-string) ---
    if isinstance(node, ast.JoinedStr):
        return "<f-string>"

    # Fallback: return the AST dump as a string marker
    return f"<ast:{type(node).__name__}>"


def _resolve_name(name: str, import_map: Dict[str, str]) -> str:
    """
    Resolve a bare Name reference via import_map.

    E.g. "SendCmd" -> "unilabos_msgs.action:SendCmd"
         "True" -> True (handled by ast.Constant in Python 3.8+)

    placeholder_type 常量（如 PLACEHOLDER_DEDUCT_RESOURCE）特殊处理：解析成其字符串值，
    使装饰器 placeholder_keys 可用常量替代字面量；按导入的原始属性名取值以兼容 as 别名。
    """
    source = import_map.get(name)
    if source and source.startswith(_PLACEHOLDER_MODULE + ":"):
        attr = source.split(":", 1)[1]
        value = _placeholder_constants().get(attr)
        if value is not None:
            return value
    if source is not None:
        return source
    # Fallback: return the name as-is
    return name


_DECORATOR_ENUM_CLASSES = frozenset({"Side", "DataSource", "NodeType"})


def _resolve_attribute(node: ast.Attribute, import_map: Dict[str, str]) -> str:
    """
    Resolve an attribute access like Side.NORTH or DataSource.HANDLE.

    对于来自 ``unilabos.registry.decorators`` 的枚举类 (Side / DataSource / NodeType)，
    直接返回枚举成员名 (如 ``"NORTH"`` / ``"HANDLE"`` / ``"MANUAL_CONFIRM"``)，
    省去消费端二次 rsplit 解析。其它 import 仍返回完整模块路径。
    """
    parts = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)

    parts.reverse()
    # parts = ["Side", "NORTH"] or ["DataSource", "HANDLE"] or ["NodeType", "MANUAL_CONFIRM"]

    if len(parts) >= 2:
        base = parts[0]
        attr = ".".join(parts[1:])

        if base in _DECORATOR_ENUM_CLASSES:
            source = import_map.get(base, "")
            if not source or _REGISTRY_DECORATOR_MODULE in source:
                return parts[-1]

        if base in import_map:
            return f"{import_map[base]}.{attr}"

    return ".".join(parts)


def _ast_call_to_value(node: ast.Call, import_map: Dict[str, str]) -> dict:
    """
    Convert a function/class call like InputHandle(key="in", ...) to a structured dict.

    Returns:
        {"_call": "unilabos.registry.decorators:InputHandle",
         "key": "in", "data_type": "fluid", ...}
    """
    # Resolve the call target
    if isinstance(node.func, ast.Name):
        call_name = _resolve_name(node.func.id, import_map)
    elif isinstance(node.func, ast.Attribute):
        call_name = _resolve_attribute(node.func, import_map)
    else:
        call_name = "<unknown>"

    result: dict = {"_call": call_name}

    # Positional args
    for i, arg in enumerate(node.args):
        result[f"_pos_{i}"] = _ast_node_to_value(arg, import_map)

    # Keyword args
    for kw in node.keywords:
        if kw.arg is None:
            continue
        result[kw.arg] = _ast_node_to_value(kw.value, import_map)

    return result


# ---------------------------------------------------------------------------
# Class body extraction
# ---------------------------------------------------------------------------


def _extract_class_body(
    cls_node: ast.ClassDef,
    import_map: Dict[str, str],
) -> dict:
    """
    Walk the class body to extract:
      - @action-decorated methods
      - @property with @topic_config (status properties)
      - get_* methods with @topic_config
      - __init__ parameters
      - Public methods without @action (auto-actions)
    """
    result: dict = {
        "actions": {},          # method_name -> action_info
        "status_properties": {},  # prop_name -> status_info
        "init_params": [],      # [{"name": ..., "type": ..., "default": ...}, ...]
        "init_docstring": None,
        "auto_methods": {},     # method_name -> method_info (no @action decorator)
    }

    for item in cls_node.body:
        if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        method_name = item.name

        # --- __init__ ---
        if method_name == "__init__":
            result["init_params"] = _extract_method_params(item, import_map)
            result["init_docstring"] = ast.get_docstring(item)
            continue

        # --- Skip private/dunder ---
        if method_name.startswith("_"):
            continue

        # --- Skip @subscribe 订阅回调（不是 action）---
        if _has_decorator(item, "subscribe") and _is_subscribe_decorator("subscribe", import_map):
            continue

        # --- Check for @property or @topic_config → status property ---
        is_property = _has_decorator(item, "property")
        has_topic = (
            _has_decorator(item, "topic_config")
            and _is_registry_decorator("topic_config", import_map)
        )

        if is_property or has_topic:
            topic_args = {}
            topic_dec = _find_method_decorator(item, "topic_config")
            if topic_dec is not None:
                topic_args = _extract_decorator_args(topic_dec, import_map)
                topic_args.setdefault("status_policy", {})
                if topic_args["status_policy"]:
                    from unilabos.registry.status_policy import (
                        normalize_status_policy,
                    )

                    topic_args["status_policy"] = (
                        normalize_status_policy(topic_args["status_policy"]) or {}
                    )

            return_type = _get_annotation_str(item.returns, import_map)
            # 非 @property 的 @topic_config 方法，用去掉 get_ 前缀的名称
            prop_name = method_name[4:] if method_name.startswith("get_") and not is_property else method_name

            result["status_properties"][prop_name] = {
                "name": prop_name,
                "return_type": return_type,
                "is_property": is_property,
                "topic_config": topic_args if topic_args else None,
            }
            continue

        # --- Check for @action ---
        action_dec = _find_method_decorator(item, "action")
        if action_dec is not None and _is_registry_decorator("action", import_map):
            action_args = _extract_decorator_args(action_dec, import_map)
            # 补全 @action 装饰器的默认值（与 decorators.py 中 action() 签名一致）
            action_args.setdefault("action_type", None)
            action_args.setdefault("goal", {})
            action_args.setdefault("feedback", {})
            action_args.setdefault("result", {})
            action_args.setdefault("handles", {})
            action_args.setdefault("goal_default", {})
            action_args.setdefault("placeholder_keys", {})
            action_args.setdefault("always_free", False)
            action_args.setdefault("is_protocol", False)
            action_args.setdefault("feedback_interval", 1.0)
            action_args.setdefault("description", "")
            action_args.setdefault("auto_prefix", False)
            action_args.setdefault("parent", False)
            action_args.setdefault("error_policy", {})
            if action_args["error_policy"]:
                from unilabos.registry.action_policy import normalize_error_policy

                action_args["error_policy"] = (
                    normalize_error_policy(action_args["error_policy"]) or {}
                )
            method_params = _extract_method_params(item, import_map)
            action_args["materials_need_lock"] = normalize_material_parameter_names(
                action_args.get("materials_need_lock"),
                action_parameter_names=(
                    param["name"] for param in method_params
                ),
                action_name=method_name,
            )
            return_type = _get_annotation_str(item.returns, import_map)
            is_async = isinstance(item, ast.AsyncFunctionDef)
            method_doc = ast.get_docstring(item)

            result["actions"][method_name] = {
                "action_args": action_args,
                "params": method_params,
                "return_type": return_type,
                "is_async": is_async,
                "docstring": method_doc,
            }
            continue

        # --- Check for @not_action ---
        if _has_decorator(item, "not_action") and _is_registry_decorator("not_action", import_map):
            continue

        # --- get_ 前缀且无额外参数（仅 self）→ status property ---
        if method_name.startswith("get_"):
            real_args = [a for a in item.args.args if a.arg != "self"]
            if len(real_args) == 0:
                prop_name = method_name[4:]
                return_type = _get_annotation_str(item.returns, import_map)
                if prop_name not in result["status_properties"]:
                    result["status_properties"][prop_name] = {
                        "name": prop_name,
                        "return_type": return_type,
                        "is_property": False,
                        "topic_config": None,
                    }
                continue

        # --- Public method without @action => auto-action ---
        if method_name in ("post_init", "__str__", "__repr__"):
            continue

        method_params = _extract_method_params(item, import_map)
        return_type = _get_annotation_str(item.returns, import_map)
        is_async = isinstance(item, ast.AsyncFunctionDef)
        method_doc = ast.get_docstring(item)

        auto_entry: dict = {
            "params": method_params,
            "return_type": return_type,
            "is_async": is_async,
            "docstring": method_doc,
        }
        if _has_decorator(item, "always_free") and _is_registry_decorator("always_free", import_map):
            auto_entry["always_free"] = True
        result["auto_methods"][method_name] = auto_entry

    return result


# ---------------------------------------------------------------------------
# Method parameter extraction
# ---------------------------------------------------------------------------


_PARAM_SKIP_NAMES = frozenset({"sample_uuids"})


def _extract_method_params(
    func_node: Union[ast.FunctionDef, ast.AsyncFunctionDef],
    import_map: Optional[Dict[str, str]] = None,
) -> List[dict]:
    """
    Extract parameters from a class method definition.

    Automatically skips the first positional argument (self / cls) and any
    domain-specific names listed in ``_PARAM_SKIP_NAMES``.

    Returns:
        [{"name": "position", "type": "str", "default": None, "required": True}, ...]
    """
    if import_map is None:
        import_map = {}
    params: List[dict] = []

    args = func_node.args

    # Skip the first positional arg (self/cls) -- always present for class methods
    # noinspection PyUnresolvedReferences
    positional_args = args.args[1:] if args.args else []

    # defaults align to the *end* of the args list; offset must account for the skipped arg
    num_args = len(args.args)
    num_defaults = len(args.defaults)
    first_default_idx = num_args - num_defaults

    for i, arg in enumerate(positional_args, start=1):
        name = arg.arg
        if name in _PARAM_SKIP_NAMES:
            continue

        param_info: dict = {"name": name}

        # Type annotation
        if arg.annotation:
            param_info["type"] = _get_annotation_str(arg.annotation, import_map)
        else:
            param_info["type"] = ""

        # Default value
        default_idx = i - first_default_idx
        if 0 <= default_idx < len(args.defaults):
            default_val = _ast_node_to_value(args.defaults[default_idx], import_map)
            param_info["default"] = default_val
            param_info["required"] = False
        else:
            param_info["default"] = None
            param_info["required"] = True

        params.append(param_info)

    # Keyword-only arguments (self/cls never appear here)
    for i, arg in enumerate(args.kwonlyargs):
        name = arg.arg
        if name in _PARAM_SKIP_NAMES:
            continue

        param_info: dict = {"name": name}

        if arg.annotation:
            param_info["type"] = _get_annotation_str(arg.annotation, import_map)
        else:
            param_info["type"] = ""

        if i < len(args.kw_defaults) and args.kw_defaults[i] is not None:
            param_info["default"] = _ast_node_to_value(args.kw_defaults[i], import_map)
            param_info["required"] = False
        else:
            param_info["default"] = None
            param_info["required"] = True

        params.append(param_info)

    return params


def _get_annotation_str(node: Optional[ast.expr], import_map: Dict[str, str]) -> str:
    """Convert a type annotation AST node to a string representation.

    保持类型字符串为合法 Python 表达式 (可被 ast.parse 解析)。
    不在此处做 import_map 替换 — 由上层在需要时通过 import_map 解析。
    """
    if node is None:
        return ""

    if isinstance(node, ast.Constant):
        return str(node.value)

    if isinstance(node, ast.Name):
        return node.id

    if isinstance(node, ast.Attribute):
        parts = []
        current = node
        while isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        if isinstance(current, ast.Name):
            parts.append(current.id)
        parts.reverse()
        return ".".join(parts)

    # Handle subscript types like List[str], Dict[str, int], Optional[str]
    if isinstance(node, ast.Subscript):
        base = _get_annotation_str(node.value, import_map)
        if isinstance(node.slice, ast.Tuple):
            args = ", ".join(_get_annotation_str(elt, import_map) for elt in node.slice.elts)
        else:
            args = _get_annotation_str(node.slice, import_map)
        return f"{base}[{args}]"

    # Handle Union types (X | Y in Python 3.10+)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        left = _get_annotation_str(node.left, import_map)
        right = _get_annotation_str(node.right, import_map)
        return f"Union[{left}, {right}]"

    return ast.dump(node)


# ---------------------------------------------------------------------------
# Module path derivation
# ---------------------------------------------------------------------------


def _filepath_to_module(filepath: Path, python_path: Path) -> str:
    """
    通过 *python_path*（sys.path 中的根目录）推导 Python 模块路径。

    做法：取 filepath 相对于 python_path 的路径，将目录分隔符替换为 '.'。

    E.g. filepath    = "/project/unilabos/devices/pump/valve.py"
         python_path = "/project"
         => "unilabos.devices.pump.valve"
    """
    try:
        relative = filepath.relative_to(python_path)
    except ValueError:
        return str(filepath)

    parts = list(relative.parts)
    # 去掉 .py 后缀
    if parts and parts[-1].endswith(".py"):
        parts[-1] = parts[-1][:-3]
    # 去掉 __init__
    if parts and parts[-1] == "__init__":
        parts.pop()

    return ".".join(parts)
