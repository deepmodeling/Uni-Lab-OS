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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from unilabos.registry.utils.backend_metadata import normalize_supported_backends
from unilabos.registry.material_locks import (
    REMOVED_MATERIAL_LOCK_OPTIONS,
    warn_ignored_material_lock_override,
)
from unilabos.registry.action_options import check_action_options
from unilabos.registry.action_schema import (
    ModuleBindings,
    build_action_schema_draft,
    framework_parameter_names,
    reject_removed_annotations,
    slot_kind,
)
from unilabos.registry.material_roles import (
    check_material_parameter_types,
    material_contract,
    material_roles_from_ast,
    material_site_selectors,
    warn_experimental,
)
from unilabos.registry.placeholder_keys import (
    check_placeholder_keys,
    placeholder_constants,
    placeholder_parameter_names,
)
from unilabos.registry.module_scope import resolve_relative_module
from unilabos.registry.utils.tools import resolve_registry_display_name
from unilabos.registry.workflow_options import ignored_workflow_options, warn_ignored_workflow_options
from unilabos.resources.objects.site import normalize_available_sites


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_SCAN_DEPTH = 10      # 最大目录递归深度
MAX_SCAN_FILES = 1000    # 最大扫描文件数量
_CACHE_VERSION = 31      # 31：资源语义声明类改名为 MaterialRole（Material.xxx(...) 报错）
# 允许点号分段（如 liquid_handler.prcxi），与内置 YAML 注册表的命名先例一致；
# 点号只用于注册表模板名，设备实例 id（图节点）另有约束。
_DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9_]+(\.[A-Za-z0-9_]+)*$")

# 合法的装饰器来源模块
_REGISTRY_DECORATOR_MODULE = "unilabos.registry.decorators"
# @workflow 装饰器来源模块（模块级函数声明默认子工作流）
_REGISTRY_WORKFLOW_MODULE = "unilabos.registry.workflows"
# @subscribe 订阅装饰器来源模块（区分于注册表，这是运行时，订阅回调不应被当作 action）
_SUBSCRIBE_DECORATOR_MODULE = "unilabos.utils.decorator"
# placeholder_type 常量来源模块（如 PLACEHOLDER_NODES），装饰器参数里的常量解析成字符串字面量
_PLACEHOLDER_MODULE = "unilabos.registry.placeholder_type"
#: 静态读取的 placeholder_type 常量（与 ``placeholder_keys`` 校验同一数据源）
_placeholder_constants = placeholder_constants


def _validate_device_ids(device_ids: List[str]) -> None:
    invalid_ids = [device_id for device_id in device_ids if not _DEVICE_ID_RE.fullmatch(device_id)]
    if invalid_ids:
        raise ValueError(
            "@device id 只能包含英文、数字、下划线（可用点号分段）: "
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
    exclude_dirs: Optional[set] = None,
) -> List[Path]:
    """
    收集 root_dir 下的 .py 文件，限制最大递归深度和文件数量。

    Args:
        root_dir: 扫描根目录
        max_depth: 最大递归深度 (默认 10 层)
        max_files: 最大文件数量 (默认 1000 个)
        exclude_files: 要排除的文件名集合 (如 {"lab_resources.py"})
        exclude_dirs: 整个跳过的目录（含子目录）

    Returns:
        排序后的 .py 文件路径列表
    """
    result: List[Path] = []
    _exclude = exclude_files or set()
    _exclude_dirs = {Path(item).resolve() for item in exclude_dirs or ()}

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
                if _exclude_dirs and entry.resolve() in _exclude_dirs:
                    continue
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
    exclude_dirs: Optional[set] = None,
) -> Dict[str, Any]:
    """
    Recursively scan .py files under *root_dir* for @device and @resource
    decorated classes/functions.

    Uses a thread pool to parse files in parallel for faster I/O.
    When *cache* is provided, files whose fingerprint (MD5 + size) hasn't
    changed since the last scan are served from cache without re-parsing.

    Returns:
        {"devices": {device_id: meta, ...}, "resources": {resource_id: meta, ...},
         "workflows": {"module:function": meta, ...},
         "_scan_errors": [{"file": path, "error": message}, ...],
         "_cache_stats": {...}}

        ``_scan_errors`` 列出解析失败、被跳过的文件（缓存命中时同样列出，失败不会被缓存
        “吞掉”），供 ``--check_mode`` 判定失败。

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
        exclude_dirs: 递归收集时整个跳过的目录（含子目录）。

    结果按文件路径排序、文件内按声明顺序合并，与线程完成先后无关。
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
        py_files = sorted(Path(f).resolve() for f in include_files if Path(f).resolve().exists())
    else:
        py_files = _collect_py_files(
            root_dir, max_depth=max_depth, max_files=max_files,
            exclude_files=exclude_files, exclude_dirs=exclude_dirs,
        )

    cache_files: Dict[str, Any] = cache.get("files", {}) if cache else {}

    # --- Parallel scan (with cache fast-path) ---
    devices: Dict[str, dict] = {}
    resources: Dict[str, dict] = {}
    workflows: Dict[str, dict] = {}
    cache_hits = 0
    cache_misses = 0

    scan_errors: List[Dict[str, str]] = []

    def _report_scan_error(py_file: Path, message: str) -> None:
        # 解析失败的文件不能静默丢弃：装饰器写错时用户需要看到原因，check_mode 据此失败。
        import logging

        logging.getLogger("unilabos.registry.ast_scan").warning(
            "AST 扫描文件失败，已跳过 %s: %s", py_file, message
        )

    def _parse_one_cached(py_file: Path) -> Tuple[List[dict], List[dict], List[dict], bool, Optional[str]]:
        """Returns (devices, resources, workflows, was_cache_hit, scan_error)."""
        key = str(py_file)
        try:
            fp = _file_fingerprint(py_file)
        except OSError:
            return [], [], [], False, None

        cached_entry = cache_files.get(key)
        # 模块路径由导入根推导：同一文件换了导入根（如改为挂载子目录）必须重新解析
        if cached_entry and _is_cache_hit(cached_entry, fp) and cached_entry.get("python_path") == str(python_path):
            error = cached_entry.get("error")
            if error:
                _report_scan_error(py_file, error)
            return (
                cached_entry.get("devices", []),
                cached_entry.get("resources", []),
                cached_entry.get("workflows", []),
                True,
                error,
            )

        error: Optional[str] = None
        try:
            devs, ress, flows = _parse_file(py_file, python_path)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            _report_scan_error(py_file, error)
            devs, ress, flows = [], [], []

        cache_entry = {
            "md5": fp["md5"],
            "size": fp["size"],
            "mtime": fp["mtime"],
            "python_path": str(python_path),
            "devices": devs,
            "resources": ress,
            "workflows": flows,
        }
        if error:
            cache_entry["error"] = error
        cache_files[key] = cache_entry
        return devs, ress, flows, False, error

    def _collect_results(futures_dict: Dict):
        nonlocal cache_hits, cache_misses
        # 按提交顺序（排好序的文件列表）合并、不按完成先后：条目顺序与重复 id 报错里谁算先出现
        # 都不取决于线程调度
        for future, py_file in futures_dict.items():
            devs, ress, flows, hit, error = future.result()
            if hit:
                cache_hits += 1
            else:
                cache_misses += 1
            if error:
                scan_errors.append({"file": str(py_file).replace("\\", "/"), "error": error})
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
            for flow in flows:
                flow_key = f"{flow.get('module')}:{flow.get('function')}"
                workflows[flow_key] = flow

    futures = {executor.submit(_parse_one_cached, f): f for f in py_files}
    _collect_results(futures)

    if cache is not None:
        cache["files"] = cache_files

    return {
        "devices": devices,
        "resources": resources,
        "workflows": workflows,
        "_scan_errors": sorted(scan_errors, key=lambda item: item["file"]),
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
) -> Tuple[List[dict], List[dict], List[dict]]:
    """
    Parse a single .py file using ast and extract all @device-decorated classes,
    @resource-decorated functions/classes and module-level @workflow functions.

    Returns:
        (devices, resources, workflows) -- three lists of metadata dicts.
    """
    # utf-8-sig：Windows 工具链写入的 BOM 不应让文件被跳过。
    source = filepath.read_text(encoding="utf-8-sig", errors="replace")
    tree = ast.parse(source, filename=str(filepath))

    # Derive module path from file path
    module_path = _filepath_to_module(filepath, python_path)
    # 包的 __init__.py：相对导入以包自身为基准
    is_package = filepath.name == "__init__.py"

    # Build import map from the file (includes same-file class defs)
    import_map = _collect_imports(tree, module_path, is_package=is_package)
    literal_constants = _collect_literal_constants(tree, import_map)

    devices: List[dict] = []
    resources: List[dict] = []
    workflows: List[dict] = []

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
                class_body = _extract_class_body(
                    node, import_map, module=tree, module_name=module_path, is_package=is_package,
                )

                # Support ids + id_meta (multi-device) or id (single device)
                device_ids: List[str] = []
                if device_args.get("ids") is not None:
                    device_ids = list(device_args["ids"])
                else:
                    did = device_args.get("id") or device_args.get("device_id")
                    device_ids = [did] if did else [f"{module_path}:{node.name}"]

                _validate_device_ids(device_ids)
                id_meta = device_args.get("id_meta") or {}
                display_name = device_args.get("display_name", "")
                device_type = device_args.get("device_type") or _detect_class_type(
                    node, import_map
                )
                base_meta = {
                    "class_name": node.name,
                    "module": f"{module_path}:{node.name}",
                    "file_path": str(filepath).replace("\\", "/"),
                    "category": device_args.get("category", []),
                    "description": device_args.get("description", ""),
                    "display_name": display_name,
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
                        "display_name",
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
                    meta["display_name"] = resolve_registry_display_name(meta.get("display_name"), did)
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

        # --- @resource / @workflow on module-level functions ---
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            resource_decorator = _find_method_decorator(node, "resource")
            if resource_decorator is not None and _is_registry_decorator("resource", import_map):
                res_meta = _extract_resource_meta(
                    resource_decorator, node.name, module_path, filepath, import_map,
                    is_function=True,
                    func_node=node,
                )
                resources.append(res_meta)
                continue

            workflow_decorator = _find_method_decorator(node, "workflow")
            if workflow_decorator is not None and _is_workflow_decorator("workflow", import_map):
                workflow_args = _extract_decorator_args(workflow_decorator, import_map)
                # 已移除的 compiler：只接受 "ast"，值忽略、不进缓存与注册表，按定义告警一次
                # （与装饰器同一身份 模块:函数）；其他取值与装饰器一样报错。
                ignored_options = ignored_workflow_options(workflow_args)
                if ignored_options:
                    warn_ignored_workflow_options(f"{module_path}:{node.name}", ignored_options)
                workflows.append(
                    {
                        "module": module_path,
                        "function": node.name,
                        "display_name": str(workflow_args.get("display_name") or ""),
                        "description": str(workflow_args.get("description") or ""),
                        "tags": workflow_args.get("tags") or [],
                        "file_path": str(filepath).replace("\\", "/"),
                    }
                )

    return devices, resources, workflows


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
        "display_name": resolve_registry_display_name(res_args.get("display_name"), resource_id),
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


def _collect_imports(tree: ast.Module, module_path: str = "", is_package: bool = False) -> Dict[str, str]:
    """
    Walk all Import/ImportFrom nodes in the AST tree, build a mapping from
    local name to fully-qualified import path.

    相对导入（``from .labware import Deck``）按 *module_path* 解析成绝对路径
    （*is_package* 表示该模块是包的 ``__init__.py``）；无法解析（缺少模块身份或越过
    顶层包）的相对导入不登记，避免把 ``labware`` 误当成顶层模块。

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
            if node.level:
                if not module_path:
                    continue
                try:
                    module = resolve_relative_module(module_path, node.level, node.module, is_package=is_package)
                except ValueError:
                    continue
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


def _is_workflow_decorator(name: str, import_map: Dict[str, str]) -> bool:
    """Check that *name* was imported from ``unilabos.registry.workflows``."""
    source = import_map.get(name, "")
    return _REGISTRY_WORKFLOW_MODULE in source


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

    placeholder_type 常量（如 PLACEHOLDER_NODES）特殊处理：解析成其字符串值，
    使装饰器参数可用常量替代字面量；按导入的原始属性名取值以兼容 as 别名。
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
    *,
    module: Optional[ast.Module] = None,
    module_name: str = "",
    is_package: bool = False,
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

    # 同一模块内的动作共用一份注解绑定（严格作用域解析失败时退回导入表）
    bindings = ModuleBindings(module, module_name=module_name, is_package=is_package, import_map=import_map)

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
            # 已移除的按动作改写物料锁：值忽略、不进缓存与注册表，按定义告警一次
            # （与装饰器同一身份 模块.类.方法）。物料入参一律由注册表按签名默认加锁。
            definition = f"{module_name}.{cls_node.name}.{method_name}"
            ignored_lock_options = [f"{name}=" for name in REMOVED_MATERIAL_LOCK_OPTIONS if name in action_args]
            for name in REMOVED_MATERIAL_LOCK_OPTIONS:
                action_args.pop(name, None)
            json_action = not action_args.get("action_type")
            # 已删除的写法（contract= / resource_contract= / 改名前的 material= 与 materials= / JSON 动作的 goal= 与 result=，
            # 以及注解里的 Placeholder / SiteSelector）与装饰器同一套报错：整个文件记为扫描错误，错误信息指出新写法。
            check_action_options(action_args, json_action=json_action, definition=definition)
            # 框架注入的参数（sample_uuids、按类型识别的 ActionContext）不属于动作接口；
            # 叫 action_context 却没有 ActionContext 注解的旧写法在这里报错
            framework = framework_parameter_names(item, bindings, definition=definition)
            # 手写的前端选择器：键必须是参数名，未知种类告警一次；注册表与类型推出的取并集（手写优先）
            action_args["placeholder_keys"] = check_placeholder_keys(
                action_args.get("placeholder_keys"),
                parameter_names=_placeholder_parameter_names(item, action_args, framework),
                definition=definition,
            )
            canonical_schema = None
            draft: Dict[str, Any] = {}
            resource_contract: Dict[str, Any] = {}
            site_selectors: Dict[str, Any] = {}
            material_value = action_args.pop("material_contract", None)
            if material_value is not None:
                if not json_action:
                    raise ValueError(f"{definition}：material_contract= 只用于 JSON 动作（不写 action_type）")
                roles = material_roles_from_ast(material_value)
                resource_contract = material_contract(roles)
                site_selectors = material_site_selectors(roles)
            if json_action:
                draft = build_action_schema_draft(
                    item,
                    bindings,
                    action_name=method_name,
                    description=str(action_args.get("description") or ""),
                    definition=definition,
                    site_selectors=site_selectors,
                )
                canonical_schema = draft["schema"]
                if material_value is not None:
                    # 字符串参数对照签名：物料 ResourceSlot、库位 SiteSlot、设备 DeviceSlot；material_contract= 是实验性功能，告警一次
                    goal_fields = canonical_schema["properties"]["goal"]["properties"]
                    check_material_parameter_types(
                        roles,
                        lambda name: slot_kind(goal_fields[name]) if name in goal_fields else None,
                        definition=definition,
                    )
                    warn_experimental(roles, definition)
                if resource_contract:
                    from unilabos.registry.action_resource_contract import validate_action_resource_contract_schema

                    validate_action_resource_contract_schema(resource_contract, canonical_schema)
                    canonical_schema["x-unilabos-action-contract"]["resource_contract"] = resource_contract
                ignored_lock_options.extend(_material_lock_free_parameters(item, bindings))
            else:
                reject_removed_annotations(item, bindings, definition=definition)
            # 补全 @action 装饰器的默认值（与 decorators.py 中 action() 签名一致）
            action_args.setdefault("action_type", None)
            action_args.setdefault("goal", {})
            action_args.setdefault("feedback", {})
            action_args.setdefault("result", {})
            action_args.setdefault("handles", {})
            action_args.setdefault("goal_default", {})
            action_args.setdefault("always_free", False)
            action_args.setdefault("is_protocol", False)
            action_args.setdefault("feedback_interval", 1.0)
            action_args.setdefault("description", "")
            action_args.setdefault("display_name", "")
            action_args.setdefault("auto_prefix", False)
            action_args.setdefault("parent", False)
            action_args.setdefault("error_policy", {})
            if action_args["error_policy"]:
                from unilabos.registry.action_policy import normalize_error_policy

                action_args["error_policy"] = (
                    normalize_error_policy(action_args["error_policy"]) or {}
                )
            if ignored_lock_options:
                warn_ignored_material_lock_override(definition, ignored_lock_options)
            method_params = _extract_method_params(item, import_map, skip=framework)
            # 超时字段与装饰器同一套校验：硬超时必须是正数秒；软超时可以是秒或只引用
            # 动作入参的四则运算表达式（归一化为规范字符串写入注册表）。
            from unilabos.registry.action_timeout import (
                normalize_action_timeout,
                normalize_execution_timeout,
            )

            action_args["timeout"] = normalize_action_timeout(
                action_args.get("timeout"), action_name=method_name
            )
            action_args["execution_timeout"] = normalize_execution_timeout(
                action_args.get("execution_timeout"),
                action_parameter_names=(param["name"] for param in method_params),
                action_name=method_name,
            )
            # 预计时长：兜底写了才保留（含 None），表达式按入参名校验并归一化
            from unilabos.registry.action_duration import (
                EXPRESS_FIELD,
                FIXED_FIELD,
                normalize_estimate_duration_express,
                normalize_estimate_duration_fixed,
            )

            if FIXED_FIELD in action_args:
                action_args[FIXED_FIELD] = normalize_estimate_duration_fixed(
                    action_args[FIXED_FIELD], action_name=method_name
                )
            if EXPRESS_FIELD in action_args:
                express = normalize_estimate_duration_express(
                    action_args[EXPRESS_FIELD],
                    action_parameter_names=(param["name"] for param in method_params),
                    action_name=method_name,
                )
                if express:
                    action_args[EXPRESS_FIELD] = express
                else:
                    action_args.pop(EXPRESS_FIELD)
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
            _store_schema_draft(result["actions"][method_name], canonical_schema, draft)
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

        definition = f"{module_name}.{cls_node.name}.{method_name}"
        framework = framework_parameter_names(item, bindings, definition=definition)
        method_params = _extract_method_params(item, import_map, skip=framework)
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
        draft = build_action_schema_draft(
            item, bindings, action_name=f"auto-{method_name}", definition=definition
        )
        _store_schema_draft(auto_entry, draft["schema"], draft)
        result["auto_methods"][method_name] = auto_entry

    return result


# ---------------------------------------------------------------------------
# Method parameter extraction
# ---------------------------------------------------------------------------


def _store_schema_draft(entry: dict, schema: Optional[dict], draft: dict) -> None:
    """把统一 schema 草稿与扫描附带的信息写进缓存条目（注册表补全推断字段后使用）。"""

    if schema is not None:
        entry["canonical_schema"] = schema
        if draft.get("inferred"):
            entry["schema_inferred"] = list(draft["inferred"])
        if draft.get("result_inferred"):
            entry["result_inferred"] = list(draft["result_inferred"])
        if draft.get("skipped"):
            entry["schema_skipped"] = list(draft["skipped"])


def _placeholder_parameter_names(
    item: Union[ast.FunctionDef, ast.AsyncFunctionDef], action_args: Dict[str, Any], framework: Any = (),
) -> Optional[List[str]]:
    """``placeholder_keys`` 可以写的键（规则见 ``placeholder_keys.placeholder_parameter_names``）；
    ``framework`` 是框架注入、不属于动作接口的参数名。"""

    arguments = item.args
    positional = [*arguments.posonlyargs, *arguments.args][1:]
    return placeholder_parameter_names(
        [argument.arg for argument in [*positional, *arguments.kwonlyargs] if argument.arg not in framework],
        goal=action_args.get("goal"),
        ros_action=bool(action_args.get("action_type")),
        parent=bool(action_args.get("parent")),
        is_protocol=bool(action_args.get("is_protocol")),
    )


def _material_lock_free_parameters(item: Union[ast.FunctionDef, ast.AsyncFunctionDef], bindings: Any) -> List[str]:
    """写了已废弃 ``MaterialLock(free=True)`` 的参数（只用于告警，不再免锁）。"""

    names: List[str] = []
    for argument in [*item.args.posonlyargs, *item.args.args, *item.args.kwonlyargs]:
        annotation = argument.annotation
        if not isinstance(annotation, ast.Subscript) or bindings.identity(annotation.value) not in {
            "typing:Annotated",
            "typing_extensions:Annotated",
        }:
            continue
        members = annotation.slice.elts if isinstance(annotation.slice, ast.Tuple) else [annotation.slice]
        for member in members[1:]:
            if isinstance(member, ast.Call) and bindings.identity(member.func) == "unilabos.registry.annotations:MaterialLock":
                names.append(f"{argument.arg}: MaterialLock(free=True)")
    return names


#: 按名字识别、由运行时注入的参数：``sample_uuids``（样品映射）。反馈/取消上下文按类型
#: （``ActionContext``）识别，由调用方经 ``skip`` 传入（见 ``action_schema.framework_parameter_names``）。
_PARAM_SKIP_NAMES = frozenset({"sample_uuids"})


def _extract_method_params(
    func_node: Union[ast.FunctionDef, ast.AsyncFunctionDef],
    import_map: Optional[Dict[str, str]] = None,
    *,
    skip: Any = (),
) -> List[dict]:
    """
    Extract parameters from a class method definition.

    Automatically skips the first positional argument (self / cls), any
    domain-specific names listed in ``_PARAM_SKIP_NAMES`` and the names in ``skip``
    (framework-injected ``ActionContext`` parameters, recognised by type).

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

    skipped = _PARAM_SKIP_NAMES | set(skip)
    for i, arg in enumerate(positional_args, start=1):
        name = arg.arg
        if name in skipped:
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
        if name in skipped:
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
