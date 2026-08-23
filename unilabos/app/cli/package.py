"""社区设备包命令：本地 inspect/install 实现。"""

import hashlib
import json
import re
import subprocess
import tarfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional

from unilabos.registry.init_enforce import validate_init_param_enforce
from unilabos.utils import logger
from unilabos.utils.banner_print import print_status

COMMUNITY_PREFIX = "community."
DEFAULT_SOURCE_TYPE = "community"
ARCHIVE_EXCLUDE_DIRS = {
    "__pycache__",
    ".git",
    ".idea",
    ".vscode",
    "dist",
    "build",
    ".pytest_cache",
    "unilabos_data",
    ".venv",
    "venv",
    "node_modules",
}
ARCHIVE_EXCLUDE_SUFFIXES = {".pyc", ".pyo"}


class PackageCLIError(RuntimeError):
    """package 子命令执行过程中的可预期错误。"""


def register_package_commands(subparsers: Any) -> None:
    """把 package 命令组注册到统一 CLI。"""

    package_parser = subparsers.add_parser(
        "package",
        aliases=["pkg"],
        help="Community device package tools: inspect / install",
    )
    package_actions = package_parser.add_subparsers(
        title="package actions", dest="package_action"
    )
    for action in ("inspect",):
        action_parser = package_actions.add_parser(
            action,
            help="Scan package dir and generate package_info/archive locally",
        )
        action_parser.add_argument(
            "--path",
            dest="package_path",
            type=str,
            required=True,
            help="Path to the community device package directory (contains pyproject.toml)",
        )
        action_parser.add_argument(
            "--namespace",
            type=str,
            default=None,
            help="Class namespace, e.g. community.acme; defaults from pyproject name",
        )
        action_parser.add_argument(
            "--out",
            type=str,
            default=None,
            help="Output dir for archive/package_info.json",
        )
    install_parser = package_actions.add_parser(
        "install",
        help="Install a pip spec / git URL locally, then scan @device IDs",
    )
    install_parser.add_argument(
        "install_spec",
        type=str,
        help="pip spec (name==version / name) or git URL (git+https://...)",
    )
    install_parser.add_argument(
        "--no-inspect",
        "--no_inspect",
        dest="no_inspect",
        action="store_true",
        help="Skip post-install @device scan / device listing",
    )


def normalize_name(name: str) -> str:
    """归一化包名：小写、连字符转下划线（与 Edge _normalize_package_dir_name 取向一致）。"""
    return name.strip().lower().replace("-", "_")


def resolve_class_namespace(project_name: str, namespace: Optional[str]) -> str:
    """确定 class_namespace：显式 --namespace 优先，否则 community.<归一化包名>。"""
    if namespace:
        ns = namespace.strip()
        if not ns.startswith(COMMUNITY_PREFIX):
            ns = COMMUNITY_PREFIX + ns
        return ns
    return COMMUNITY_PREFIX + normalize_name(project_name)


def discover_registry_paths_from_project(project_root: Path | str) -> List[Path]:
    """从包根推导目录化注册表路径。

    ``[tool.unilabos.registry].paths`` 相对包含 ``pyproject.toml`` 的包根解析；
    未声明时回退到包根下的 ``unilabos_registry/``。
    """
    root = Path(project_root).resolve()
    pyproject_paths = _read_pyproject_registry_paths(root)
    if pyproject_paths:
        return pyproject_paths

    fallback = root / "unilabos_registry"
    if fallback.is_dir():
        return [fallback]
    return []


def _read_pyproject_registry_paths(project_root: Path) -> List[Path]:
    pyproject = project_root / "pyproject.toml"
    if not pyproject.is_file():
        return []

    data = _load_toml(pyproject)
    registry_config = data.get("tool", {}).get("unilabos", {}).get("registry", {})
    raw_paths = registry_config.get("paths", [])
    if not isinstance(raw_paths, list):
        return []

    paths: List[Path] = []
    for raw_path in raw_paths:
        if not isinstance(raw_path, str):
            continue
        registry_path = (project_root / raw_path).resolve()
        if registry_path.is_dir():
            paths.append(registry_path)
    return paths


def read_pyproject(pkg_dir: Path) -> Dict[str, Any]:
    """读取 pyproject.toml 的 [project] 表，返回 name/version/summary/license/homepage/dependencies 等。"""
    pyproject_path = pkg_dir / "pyproject.toml"
    if not pyproject_path.is_file():
        raise PackageCLIError(f"未找到 pyproject.toml：{pyproject_path}")

    data = _load_toml(pyproject_path)
    project = data.get("project", {}) if isinstance(data, dict) else {}
    if not isinstance(project, dict):
        project = {}

    name = str(project.get("name") or "").strip()
    if not name:
        raise PackageCLIError("pyproject.toml [project].name 为空，无法生成 package_info")
    version = str(project.get("version") or "0.0.0").strip()

    license_value = project.get("license")
    if isinstance(license_value, dict):
        license_str = str(license_value.get("text") or license_value.get("file") or "")
    else:
        license_str = str(license_value or "")

    urls = project.get("urls") if isinstance(project.get("urls"), dict) else {}
    homepage = ""
    for key in ("Homepage", "homepage", "Repository", "repository", "Source", "source"):
        if urls.get(key):
            homepage = str(urls[key])
            break

    dependencies = project.get("dependencies")
    deps: List[str] = [str(d) for d in dependencies] if isinstance(dependencies, list) else []

    return {
        "name": name,
        "version": version,
        "summary": str(project.get("description") or ""),
        "license": license_str,
        "homepage": homepage,
        "dependencies": deps,
    }


def scan_package_devices(pkg_dir: Path) -> Dict[str, Dict[str, Any]]:
    """纯 AST 扫描包目录下的 @device 注册表，返回 {device_id: meta}。
    """
    from unilabos.registry.ast_registry_scanner import scan_directory

    py_files = [
        f
        for f in pkg_dir.rglob("*.py")
        if not f.name.startswith("__")
        and not (set(f.relative_to(pkg_dir).parts) & ARCHIVE_EXCLUDE_DIRS)
    ]
    executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="PackageInspect")
    try:
        result = scan_directory(pkg_dir, executor=executor, include_files=py_files)
    finally:
        executor.shutdown(wait=True)
    devices = result.get("devices", {})
    return {did: meta for did, meta in devices.items() if isinstance(meta, dict)}


def read_registry_yaml_devices(pkg_dir: Path) -> Dict[str, Dict[str, Any]]:
    """读取包目录下 registry.yaml/*.yaml 里的设备注册表条目，返回 {device_id: entry}。

    community_drivers 标准布局（driver.py + registry.yaml + startup.json）使用 YAML 注册表，
    其条目天然含 class.action_value_mappings/schema，是最完整的 source_registry。
    仅采纳 resource_type=device 或带 class.action_value_mappings 的条目。
    """
    try:
        import yaml
    except ModuleNotFoundError:
        logger.warning("[package] 未安装 pyyaml，跳过 registry.yaml 读取")
        return {}

    entries: Dict[str, Dict[str, Any]] = {}
    for yaml_path in sorted(list(pkg_dir.glob("*.yaml")) + list(pkg_dir.glob("*.yml"))):
        try:
            data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(f"[package] 解析 {yaml_path} 失败: {exc}")
            continue
        if not isinstance(data, dict):
            continue
        for device_id, entry in data.items():
            if not isinstance(entry, dict):
                continue
            cls = entry.get("class") if isinstance(entry.get("class"), dict) else {}
            is_device = entry.get("resource_type") == "device" or bool(cls.get("action_value_mappings"))
            if is_device:
                entries[str(device_id)] = entry
    return entries


def read_external_registry_devices(pkg_dir: Path) -> Dict[str, Dict[str, Any]]:
    """读取包内"文件夹式"外部注册表的设备条目，返回 {device_id: entry}。

    遵循 Plan 09 外部包注册表约定（与运行时 Registry.load_device_types 同构）：
    - 注册表根来自 pyproject ``[tool.unilabos.registry] paths``，否则回退 ``unilabos_registry/``；
    - 每个根下的 ``devices/*.yaml`` 即设备文件；
    - 逐文件用 ``resolve_yaml_refs`` 展开跨文件 ``$ref``（共享 contracts），与运行时一致。

    与根目录 ``registry.yaml`` 互补：不要求把条目摊平到包根，目录化注册表即可被纳管。
    """
    try:
        import yaml
    except ModuleNotFoundError:
        logger.warning("[package] 未安装 pyyaml，跳过外部注册表读取")
        return {}

    from unilabos.registry.yaml_ref import resolve_yaml_refs

    registry_roots = discover_registry_paths_from_project(pkg_dir)
    if not registry_roots:
        return {}

    entries: Dict[str, Dict[str, Any]] = {}
    for root in registry_roots:
        devices_dir = root / "devices"
        if not devices_dir.is_dir():
            continue
        for yaml_path in sorted(list(devices_dir.glob("*.yaml")) + list(devices_dir.glob("*.yml"))):
            try:
                raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
                data = resolve_yaml_refs(raw, base_file=yaml_path)
            except Exception as exc:
                logger.warning(f"[package] 解析外部注册表 {yaml_path} 失败: {exc}")
                continue
            if not isinstance(data, dict):
                continue
            for device_id, entry in data.items():
                if not isinstance(entry, dict):
                    continue
                cls = entry.get("class") if isinstance(entry.get("class"), dict) else {}
                # devices/ 目录下条目天然是设备；接受带 class.module 或显式 resource_type=device 的条目
                is_device = bool(cls.get("module")) or entry.get("resource_type") == "device"
                if is_device:
                    entries[str(device_id)] = entry
    return entries


def build_archive(pkg_dir: Path, archive_path: Path) -> str:
    """把包目录打包为 tar.gz，跳过缓存/版本控制目录，返回 "sha256:<hex>"。"""
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    arc_root = pkg_dir.name

    def _filter(tarinfo: tarfile.TarInfo) -> Optional[tarfile.TarInfo]:
        parts = set(Path(tarinfo.name).parts)
        if parts & ARCHIVE_EXCLUDE_DIRS:
            return None
        if Path(tarinfo.name).suffix in ARCHIVE_EXCLUDE_SUFFIXES:
            return None
        return tarinfo

    with tarfile.open(archive_path, "w:gz") as tar:
        tar.add(str(pkg_dir), arcname=arc_root, filter=_filter)

    return "sha256:" + _sha256_file(archive_path)


_PY_TO_JSON_SCHEMA_TYPE = {
    "float": "number",
    "int": "integer",
    "str": "string",
    "bool": "boolean",
    "dict": "object",
    "list": "array",
    "Dict": "object",
    "List": "array",
    "Any": "string",
}


def _json_schema_type(py_type: str) -> str:
    """把 Python 类型注解字符串归一化为 JSON Schema type（取裸类型名，未知回退 string）。"""
    base = (py_type or "").strip().split("[")[0].split(".")[-1]
    return _PY_TO_JSON_SCHEMA_TYPE.get(base, "string")


def build_action_value_mappings(actions: Dict[str, Any]) -> Dict[str, Any]:
    """把 AST 扫描的原始 action（params/return_type）转换成前后端期望的
    """
    result: Dict[str, Any] = {}
    for name, meta in actions.items():
        if not isinstance(meta, dict):
            continue
        params = meta.get("params") if isinstance(meta.get("params"), list) else []
        goal_props: Dict[str, Any] = {}
        required: List[str] = []
        goal_default: Dict[str, Any] = {}
        for param in params:
            if not isinstance(param, dict):
                continue
            pname = str(param.get("name") or "").strip()
            if not pname:
                continue
            goal_props[pname] = {"type": _json_schema_type(str(param.get("type", ""))), "title": pname}
            if param.get("required"):
                required.append(pname)
            if param.get("default") is not None:
                goal_default[pname] = param.get("default")
        goal_schema: Dict[str, Any] = {"type": "object", "properties": goal_props}
        if required:
            goal_schema["required"] = required
        action_args = meta.get("action_args") if isinstance(meta.get("action_args"), dict) else {}
        action_type_raw = action_args.get("action_type")
        action_type = "UniLabJsonCommand"
        if isinstance(action_type_raw, str) and action_type_raw.strip():
            action_type = action_type_raw.strip().split(":")[-1].split(".")[-1]
        entry: Dict[str, Any] = {
            "type": action_type,
            "goal": goal_schema,
            "result": {"type": "object", "properties": {}},
            "feedback": {"type": "object", "properties": {}},
            "description": str(meta.get("docstring") or action_args.get("description") or ""),
        }
        if goal_default:
            entry["goal_default"] = goal_default
        materials_need_lock = action_args.get("materials_need_lock")
        if isinstance(materials_need_lock, list):
            entry["materials_need_lock"] = list(materials_need_lock)
        result[name] = entry
    return result


def build_resources(devices: Dict[str, Dict[str, Any]], package_info: Dict[str, Any]) -> List[Dict[str, Any]]:
    """把扫描出的设备 meta 映射为本地包清单 resources。"""
    resources: List[Dict[str, Any]] = []
    for device_id, meta in devices.items():
        actions = meta.get("actions") if isinstance(meta.get("actions"), dict) else {}
        action_value_mappings = build_action_value_mappings(actions)
        status_props = meta.get("status_properties") if isinstance(meta.get("status_properties"), dict) else {}
        handles = meta.get("handles") if isinstance(meta.get("handles"), list) else []

        reg_class = {
            "module": meta.get("module", ""),
            "type": meta.get("device_type", "python"),
            "action_value_mappings": action_value_mappings,
            "status_types": status_props,
        }
        # source_registry 保存原始定义，供 Registry/materials adapter 读取动作能力。
        source_registry = {
            "class": reg_class,
            "handles": handles,
            "device_id": device_id,
            "version": meta.get("version", package_info.get("version", "")),
            "description": meta.get("description", ""),
            "displayname": meta.get("displayname") or device_id,
            "icon": meta.get("icon", ""),
        }
        category = meta.get("category") if isinstance(meta.get("category"), list) else []
        resources.append(
            {
                "id": device_id,
                "registry_type": "device",
                "version": meta.get("version", package_info.get("version", "0.0.1")),
                "description": meta.get("description", ""),
                "displayname": meta.get("displayname") or device_id,
                "icon": meta.get("icon", ""),
                "class": reg_class,
                "category": category,
                "handles": _map_handles(handles),
                "package_info": package_info,
                "source_registry": source_registry,
            }
        )
    return resources


def build_resources_from_registry(
    entries: Dict[str, Dict[str, Any]],
    package_info: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """把 registry.yaml 设备条目映射为本地包清单 resources。

    条目本身已含 class.action_value_mappings/schema，直接保留为 source_registry。
    """
    resources: List[Dict[str, Any]] = []
    for device_id, entry in entries.items():
        cls = entry.get("class") if isinstance(entry.get("class"), dict) else {}
        init_schema = entry.get("init_param_schema") if isinstance(entry.get("init_param_schema"), dict) else None
        init_enforce = validate_init_param_enforce(
            device_id,
            init_schema,
            entry.get("init_param_enforce"),
            error_factory=PackageCLIError,
        )
        category = entry.get("category") or entry.get("tags") or []
        if isinstance(category, str):
            category = [category]
        resource: Dict[str, Any] = {
            "id": device_id,
            "registry_type": str(entry.get("resource_type", "device")),
            "version": str(entry.get("version", package_info.get("version", "0.0.1"))),
            "description": entry.get("description", ""),
            "icon": entry.get("icon", ""),
            "class": {
                "module": cls.get("module", ""),
                "type": cls.get("type", "python"),
                "action_value_mappings": cls.get("action_value_mappings", {}),
                "status_types": cls.get("status_types", {}),
            },
            "handles": [],
            "category": category if isinstance(category, list) else [],
            "manufacturer": str(entry.get("manufacturer", "")),
            "model": entry.get("model"),
            "scene": entry.get("scene"),
            "device_params": entry.get("device_params"),
            "package_info": package_info,
            # source_registry：直接保存 YAML 原始条目（含 class.action_value_mappings）
            "source_registry": entry,
        }
        if init_schema is not None:
            resource["init_param_schema"] = init_schema
        resource["init_param_enforce"] = init_enforce
        resources.append(resource)
    return resources


def build_package_info(
    project: Dict[str, Any],
    class_namespace: str,
    sha256: str,
    download_url: str = "",
    oss_object_key: str = "",
) -> Dict[str, Any]:
    """根据 pyproject 元信息 + 命名空间 + 归档指纹构造 package_info（后端/Edge 共同消费的字段）。"""
    name = project["name"]
    info: Dict[str, Any] = {
        "name": name,
        "version": project["version"],
        "class_namespace": class_namespace,
        "module_prefix": class_namespace.split(".")[0] if class_namespace else "community",
        "normalized_name": normalize_name(name),
        "source_type": DEFAULT_SOURCE_TYPE,
        "install_spec": f"{name}=={project['version']}" if project.get("version") else name,
        "summary": project.get("summary", ""),
        "license": project.get("license", ""),
        "homepage": project.get("homepage", ""),
        # pyproject [project].dependencies：Edge 消费侧据此安装运行依赖（不安装包体本身）
        "dependencies": list(project.get("dependencies") or []),
        "sha256": sha256,
        "download_url": download_url,
    }
    if oss_object_key:
        info["oss_object_key"] = oss_object_key
    return info


def inspect_package(
    path: str,
    namespace: Optional[str] = None,
    out_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """扫描并打包一个社区设备包，产出 package_info / resources / 归档。返回结果汇总。"""
    pkg_dir = Path(path).resolve()
    if not pkg_dir.is_dir():
        raise PackageCLIError(f"包目录不存在：{pkg_dir}")

    project = read_pyproject(pkg_dir)
    class_namespace = resolve_class_namespace(project["name"], namespace)

    out_path = Path(out_dir).resolve() if out_dir else (pkg_dir.parent / "dist")
    out_path.mkdir(parents=True, exist_ok=True)
    archive_name = f"{normalize_name(project['name'])}-{project['version']}.tar.gz"
    archive_path = out_path / archive_name
    sha256 = build_archive(pkg_dir, archive_path)

    package_info = build_package_info(project, class_namespace, sha256)

    # 设备来源优先级：根目录 registry.yaml > 文件夹式外部注册表(unilabos_registry/) > @device AST 扫描
    # 前两者条目均自带完整 class.action_value_mappings，可直接作为 source_registry。
    yaml_entries = read_registry_yaml_devices(pkg_dir)
    if not yaml_entries:
        yaml_entries = read_external_registry_devices(pkg_dir)
        registry_source = "unilabos_registry/"
    else:
        registry_source = "registry.yaml"
    if yaml_entries:
        device_source = registry_source
        device_ids = sorted(yaml_entries)
        resources = build_resources_from_registry(yaml_entries, package_info)
    else:
        device_source = "@device AST"
        ast_devices = scan_package_devices(pkg_dir)
        device_ids = sorted(ast_devices)
        resources = build_resources(ast_devices, package_info)
    devices = {rid: None for rid in device_ids}
    if not resources:
        print_status(f"警告：{pkg_dir} 未发现 registry.yaml / unilabos_registry/ 或 @device 设备，仅生成 package_info", "warning")

    package_info_path = out_path / "package_info.json"
    resources_path = out_path / "resources.json"
    package_info_path.write_text(json.dumps(package_info, ensure_ascii=False, indent=2), encoding="utf-8")
    resources_path.write_text(json.dumps(resources, ensure_ascii=False, indent=2), encoding="utf-8")

    print_status(f"package inspect 完成：{project['name']}@{project['version']}", "info")
    print_status(f"  class_namespace : {class_namespace}", "info")
    print_status(f"  设备来源        : {device_source}", "info")
    print_status(f"  设备数          : {len(resources)} ({', '.join(device_ids) or '无'})", "info")
    print_status(f"  归档            : {archive_path} ({sha256})", "info")
    print_status(f"  package_info    : {package_info_path}", "info")
    print_status(f"  resources       : {resources_path}", "info")

    return {
        "project": project,
        "class_namespace": class_namespace,
        "devices": devices,
        "archive_path": str(archive_path),
        "sha256": sha256,
        "package_info": package_info,
        "resources": resources,
        "package_info_path": str(package_info_path),
        "resources_path": str(resources_path),
    }


def install_package(spec: str, run_inspect: bool = True) -> Dict[str, Any]:
    """本地安装一个设备包：uv pip install 优先、回退 pip install，
    """
    spec = (spec or "").strip()
    if not spec:
        raise PackageCLIError("缺少安装目标，用法：unilab package install <pip-spec 或 git-url>")

    installer = _run_pip_install(spec)
    print_status(f"package install 完成：{spec}（{installer}）", "info")

    # PyPI 规格直接取名；本地目录/文件路径装完后从其 pyproject.toml 读分发名（git/URL 仍取不到）。
    dist_name = _spec_dist_name(spec) or _local_dist_name(spec)
    device_ids: List[str] = []
    if run_inspect and dist_name:
        device_ids = _installed_device_ids(dist_name)

    if device_ids:
        print_status(f"  包内可用设备    : {', '.join(device_ids)}", "info")
    elif dist_name:
        print_status(f"  已安装分发      : {dist_name}（未扫描到 @device，可能非 Uni-Lab 设备包）", "info")
    else:
        print_status("  已安装（git/URL 来源，无法确定分发名，跳过设备扫描）", "info")

    return {"spec": spec, "installer": installer, "dist_name": dist_name, "device_ids": device_ids}


def cmd_package(args_dict: Dict[str, Any]) -> None:
    """package 子命令分发入口，由 main() 在配置/鉴权就绪后调用。"""
    action = args_dict.get("package_action")
    path = args_dict.get("package_path")
    namespace = args_dict.get("namespace")
    out_dir = args_dict.get("out")

    if not action:
        raise PackageCLIError(
            "缺少 package 子动作，请使用 `unilab package inspect|install`"
        )

    if action == "install":
        install_package(
            args_dict.get("install_spec", "") or "",
            run_inspect=not args_dict.get("no_inspect", False),
        )
        return

    if not path:
        raise PackageCLIError("缺少 --path（社区设备包目录）")

    if action == "inspect":
        inspect_package(path, namespace=namespace, out_dir=out_dir)
    else:
        raise PackageCLIError(f"未知 package 子动作：{action}")


def run_package_command(
    args: Dict[str, Any],
    *,
    args_namespace: Any = None,
    session_manager: Any = None,
) -> bool:
    """执行 package 命令；非 package 命令返回 ``False``。"""

    if args.get("command") not in {"package", "pkg"}:
        return False
    del args_namespace, session_manager
    try:
        cmd_package(args)
    except PackageCLIError as exc:
        print_status(str(exc), "error")
        raise SystemExit(1) from exc
    return True


# --- 内部工具 ---


def _run_pip_install(spec: str) -> str:
    """优先 `uv pip install`、回退 `python -m pip install` 安装 spec，返回实际使用的安装器名。

    与 environment_check 共用安装器选择(_installer_candidates，含 uv 可用性校验+缓存)与命令构造
    (_install_command)：中文 locale 自动走清华源、uv 显式 --python 兼容 conda。
    失败（含找不到 uv）时切下一个；全部失败抛 PackageCLIError 并带最后一次 stderr。
    """
    from unilabos.utils.environment_check import _install_command, _installer_candidates, _is_chinese_locale

    is_chinese = _is_chinese_locale()
    last_err = ""
    for installer in _installer_candidates():
        name = "uv pip install" if installer == "uv" else "pip install"
        cmd = _install_command(installer, spec, False, is_chinese)
        print_status(f"尝试安装：{name} {spec}", "info")
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        except FileNotFoundError:
            continue  # 安装器不可用，换下一个
        except subprocess.TimeoutExpired:
            last_err = "timeout after 600s"
            continue
        if proc.returncode == 0:
            return name
        last_err = (proc.stderr or proc.stdout or "").strip()
    raise PackageCLIError(f"安装失败：{spec}\n{last_err}")


def _spec_dist_name(spec: str) -> str:
    """从 pip spec 取分发名；git/URL/本地路径无法在安装前可靠确定分发名，返回空串跳过设备扫描。"""
    s = spec.strip()
    if s.startswith(("git+", "http://", "https://", "file:", ".", "/")):
        return ""
    match = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)", s)
    return match.group(1) if match else ""


def _local_dist_name(spec: str) -> str:
    """本地目录/文件路径 spec：装完后从其 pyproject.toml 读分发名，用于补扫 @device。

    git/URL 来源在安装前后都无法可靠读到本地 pyproject，返回空串跳过。
    """
    s = spec.strip()
    if s.startswith(("git+", "http://", "https://")):
        return ""
    if s.startswith("file:"):
        s = s[5:]
    p = Path(s).expanduser()
    if not p.exists():
        return ""
    pkg_dir = p if p.is_dir() else p.parent
    try:
        return str(read_pyproject(pkg_dir).get("name") or "").strip()
    except PackageCLIError:
        return ""


def _installed_device_ids(dist_name: str) -> List[str]:
    """对已安装分发的 top-level 模块做 @device AST 扫描，返回设备 ID 列表（失败返回空）。    """
    try:
        from importlib.metadata import PackageNotFoundError, distribution
    except Exception:
        return []
    try:
        dist = distribution(dist_name)
    except PackageNotFoundError:
        return []
    except Exception as exc:
        logger.warning(f"[package] 读取已安装分发失败: {dist_name}, {exc}")
        return []

    top_modules: List[str] = []
    try:
        top_text = dist.read_text("top_level.txt") or ""
        top_modules = [line.strip() for line in top_text.splitlines() if line.strip()]
    except Exception:
        top_modules = []
    if not top_modules:
        # 现代 wheel 可能不写 top_level.txt：从 RECORD/files 兜底推断顶层模块名。
        inferred: set[str] = set()
        for entry in dist.files or []:
            parts = entry.parts
            if not parts:
                continue
            head = parts[0]
            if head in {"..", "__pycache__"} or head.endswith((".dist-info", ".data")):
                continue
            if len(parts) == 1 and head.endswith(".py"):
                inferred.add(head[:-3])  # 单文件模块
            elif len(parts) > 1 and "." not in head:
                inferred.add(head)  # 包目录
        top_modules = sorted(inferred) or [dist_name.replace("-", "_")]

    import importlib.util

    from unilabos.registry.ast_registry_scanner import scan_directory

    scan_files: List[Path] = []
    for module_name in top_modules:
        try:
            module_spec = importlib.util.find_spec(module_name)
        except (ImportError, ValueError):
            continue
        if module_spec is None:
            continue
        if module_spec.submodule_search_locations:
            for location in module_spec.submodule_search_locations:
                loc_path = Path(location)
                if not loc_path.is_dir():
                    continue
                scan_files.extend(
                    f
                    for f in loc_path.rglob("*.py")
                    if not f.name.startswith("__")
                    and not (set(f.relative_to(loc_path).parts) & ARCHIVE_EXCLUDE_DIRS)
                )
        elif module_spec.origin and module_spec.origin.endswith(".py"):
            scan_files.append(Path(module_spec.origin))

    if not scan_files:
        return []

    executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="PackageInstallScan")
    try:
        result = scan_directory(scan_files[0].parent, executor=executor, include_files=scan_files)
    finally:
        executor.shutdown(wait=True)
    devices = result.get("devices", {})
    return sorted(did for did, meta in devices.items() if isinstance(meta, dict))


def _map_handles(handles: List[Any]) -> List[Dict[str, Any]]:
    """把扫描出的 handles 列表映射为后端 RegHandle 友好结构（缺字段留空，不阻断上传）。"""
    mapped: List[Dict[str, Any]] = []
    for handle in handles:
        if isinstance(handle, dict):
            mapped.append(
                {
                    "data_key": str(handle.get("data_key", "")),
                    "data_source": str(handle.get("data_source", "")),
                    "data_type": str(handle.get("data_type", "")),
                    "description": str(handle.get("description", "")),
                    "handler_key": str(handle.get("handler_key", "")),
                    "io_type": str(handle.get("io_type", "")),
                    "label": str(handle.get("label", "")),
                    "side": str(handle.get("side", "")),
                }
            )
    return mapped


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_toml(path: Path) -> Dict[str, Any]:
    """加载 toml：优先当前运行时的标准库 tomllib，再回退 tomli 和极简解析。"""
    raw = path.read_bytes()
    try:
        import tomllib  # type: ignore

        return tomllib.loads(raw.decode("utf-8"))
    except ModuleNotFoundError:
        pass
    try:
        import tomli  # type: ignore

        return tomli.loads(raw.decode("utf-8"))
    except ModuleNotFoundError:
        logger.warning("[package] 未找到 tomllib/tomli，使用极简解析仅提取 [project] 标量字段")
        return {"project": _minimal_project_parse(raw.decode("utf-8"))}


def _minimal_project_parse(text: str) -> Dict[str, str]:
    """极简 fallback：仅解析 [project] 段内的 name/version/description 标量。"""
    result: Dict[str, str] = {}
    in_project = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            in_project = line == "[project]"
            continue
        if not in_project or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key in {"name", "version", "description"}:
            result[key] = value
    return result
