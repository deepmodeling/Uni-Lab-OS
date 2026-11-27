"""遗留软件包的注册表（Registry）发现与读取。"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from unilabos.utils import logger

from ..package_catalog.project_metadata import (
    parse_project_metadata,
    project_to_legacy_dict,
)
from .archive import ARCHIVE_EXCLUDE_DIRS
from .errors import PackageCLIError

COMMUNITY_PREFIX = "community."


def normalize_name(name: str) -> str:
    """把发行包名归一化为稳定的文件与身份片段。

    参数：``name`` 是项目声明的发行包名。
    返回：全小写、连续非字母数字字符折叠为单个下划线且首尾无下划线的字符串。
    异常：无；空白或只有分隔符的输入返回空字符串，由调用方决定是否拒绝。
    """

    return re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")


def resolve_class_namespace(project_name: str, namespace: str | None) -> str:
    """解析遗留上传投影使用的社区类命名空间。

    参数：``project_name`` 是发行包名；``namespace`` 是可选显式命名空间。
    返回：以 ``community.`` 开头的类命名空间；未显式提供时使用归一化发行包名。
    异常：无；本函数只处理遗留投影，规范包目录（PackageCatalog）的命名空间
    仍由统一编译器决定，调用方必须拒绝冲突覆盖。
    """

    if namespace:
        # ``resolved_namespace`` 是遗留上传投影最终使用的社区类命名空间身份。
        resolved_namespace = namespace.strip()
        if not resolved_namespace.startswith(COMMUNITY_PREFIX):
            resolved_namespace = COMMUNITY_PREFIX + resolved_namespace
        return resolved_namespace
    return COMMUNITY_PREFIX + normalize_name(project_name)


def discover_registry_paths_from_project(project_root: Path | str) -> list[Path]:
    """从包根推导目录化注册表路径。

    参数：``project_root`` 是包含项目元数据的软件包根。
    返回：按项目声明顺序排列且实际存在的注册表（Registry）绝对目录；未声明时
    仅在默认 ``unilabos_registry`` 存在时返回该目录，否则返回空列表。
    异常：路径解析错误直接传播；项目元数据读取或解析失败由内部解析器记录警告并
    关闭式返回空列表，不扫描其他目录。
    """

    # ``root`` 是当前软件包检查唯一允许解析注册表（Registry）路径的包根。
    root = Path(project_root).resolve()
    # ``pyproject_paths`` 是项目显式授权的注册表（Registry）目录集合。
    pyproject_paths = _read_pyproject_registry_paths(root)
    if pyproject_paths:
        return pyproject_paths

    # ``fallback`` 是未显式声明时唯一允许采用的包内注册表（Registry）目录。
    fallback = root / "unilabos_registry"
    if fallback.is_dir():
        return [fallback]
    return []


def _read_pyproject_registry_paths(project_root: Path) -> list[Path]:
    """通过统一项目解析器解析显式注册表目录。

    参数：``project_root`` 是包含 ``pyproject.toml`` 的软件包根。
    返回：项目显式声明且实际存在的规范绝对注册表（Registry）目录列表。
    异常：元数据或文件不可读时记录警告并返回空列表；不得回退扫描 ``sys.path``
    或环境中可导入的软件包。
    """

    # ``pyproject`` 是当前包身份与注册表（Registry）路径声明的唯一元数据文件。
    pyproject = project_root / "pyproject.toml"
    if not pyproject.is_file():
        return []
    try:
        # ``project`` 与工作区（Workspace）启动、目录编译共用一套解析。
        project = parse_project_metadata(pyproject.read_bytes())
    except (OSError, TypeError, ValueError) as error:
        logger.warning(f"[package] 解析注册表路径失败: {error}")
        return []

    # ``paths`` 聚合实际存在且由项目显式授权的注册表（Registry）绝对目录。
    paths: list[Path] = []
    for raw_path in project.registry_paths:
        # ``registry_path`` 是一个声明路径相对包根解析后的规范位置。
        registry_path = (project_root / raw_path).resolve()
        if registry_path.is_dir():
            paths.append(registry_path)
    return paths


def read_pyproject(pkg_dir: Path) -> dict[str, Any]:
    """通过统一项目解析器读取遗留上传投影。

    参数：``pkg_dir`` 是包含 ``pyproject.toml`` 的软件包根目录。
    返回：遗留 ``package_info`` 上传投影构造器需要的项目字段字典。
    异常：文件缺失或项目元数据无效时抛出 ``PackageCLIError``。
    """

    # ``pyproject_path`` 是遗留上传投影读取的项目身份文件。
    pyproject_path = pkg_dir / "pyproject.toml"
    if not pyproject_path.is_file():
        raise PackageCLIError(f"未找到 pyproject.toml：{pyproject_path}")
    try:
        # ``project`` 是工作区（Workspace）、注册表（Registry）和包工具共享的项目事实。
        project = parse_project_metadata(pyproject_path.read_bytes())
    except (OSError, TypeError, ValueError) as error:
        raise PackageCLIError("pyproject.toml [project] 元数据无效") from error
    return project_to_legacy_dict(project)


def scan_package_devices(pkg_dir: Path) -> dict[str, dict[str, Any]]:
    """通过 AST 扫描遗留包内声明的设备定义。

    参数：``pkg_dir`` 是已验证存在的软件包根。
    返回：以稳定设备定义身份为键、扫描元数据为值的字典；缓存、构建、虚拟环境
    和版本控制目录中的 Python 文件不会进入结果。
    异常：扫描器或文件系统异常直接传播；线程池无论成功失败都必须关闭。本函数只
    生成遗留注册表（Registry）投影，不导入作者模块。
    """

    from unilabos.registry.ast_registry_scanner import scan_directory

    # ``py_files`` 是当前包根内允许进入遗留 AST 注册表（Registry）扫描的文件集。
    py_files = [
        source_file
        for source_file in pkg_dir.rglob("*.py")
        if not source_file.name.startswith("__")
        and not (set(source_file.relative_to(pkg_dir).parts) & ARCHIVE_EXCLUDE_DIRS)
    ]
    # ``executor`` 只并行执行本次显式包根内的 AST 扫描，不承担常驻发现职责。
    executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="PackageInspect")
    try:
        # ``result`` 是扫描器对本次固定文件集合生成的完整遗留定义投影。
        result = scan_directory(pkg_dir, executor=executor, include_files=py_files)
    finally:
        executor.shutdown(wait=True)
    # ``devices`` 是按稳定设备定义身份索引的 AST 扫描候选集合。
    devices = result.get("devices", {})
    return {
        device_id: metadata
        for device_id, metadata in devices.items()
        if isinstance(metadata, dict)
    }


def read_registry_yaml_devices(pkg_dir: Path) -> dict[str, dict[str, Any]]:
    """读取包根 YAML 文件中的遗留设备注册表条目。

    参数：``pkg_dir`` 是已验证存在的软件包根。
    返回：以设备定义身份为键、原始 YAML 注册表（Registry）条目为值的字典；只
    接受显式设备类型或带动作映射的条目。
    异常：缺少 PyYAML、单个文件不可读或 YAML 无效时记录警告并跳过，不发布部分
    文件内部条目以外的推断；本函数不递归扫描目录。
    """

    try:
        import yaml
    except ModuleNotFoundError:
        logger.warning("[package] 未安装 pyyaml，跳过 registry.yaml 读取")
        return {}

    # ``entries`` 聚合以稳定设备定义身份索引的根目录 YAML 注册表（Registry）条目。
    entries: dict[str, dict[str, Any]] = {}
    for yaml_path in sorted(list(pkg_dir.glob("*.yaml")) + list(pkg_dir.glob("*.yml"))):
        try:
            # ``data`` 是单个根目录 YAML 文件的完整解析结果。
            data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError) as error:
            logger.warning(f"[package] 解析 {yaml_path} 失败: {error}")
            continue
        if not isinstance(data, dict):
            continue
        for device_id, entry in data.items():
            if not isinstance(entry, dict):
                continue
            # ``registry_class`` 是当前设备条目的驱动与动作（Action）注册定义。
            registry_class = (
                entry.get("class") if isinstance(entry.get("class"), dict) else {}
            )
            # ``is_device`` 关闭式判断当前条目是否属于设备定义。
            is_device = entry.get("resource_type") == "device" or bool(
                registry_class.get("action_value_mappings")
            )
            if is_device:
                entries[str(device_id)] = entry
    return entries


def read_external_registry_devices(pkg_dir: Path) -> dict[str, dict[str, Any]]:
    """读取包内目录式外部注册表（Registry）的设备条目。

    参数：``pkg_dir`` 是包含项目声明的显式软件包根。
    返回：以设备定义身份为键、已展开本地 ``$ref`` 的注册表条目为值的字典；只
    读取声明目录下 ``devices`` 的直接 YAML 文件。
    异常：缺少 PyYAML、单个文件不可读、引用无效或 YAML 无效时记录警告并跳过；
    不扫描环境或猜测外部来源。
    """

    try:
        import yaml
    except ModuleNotFoundError:
        logger.warning("[package] 未安装 pyyaml，跳过外部注册表读取")
        return {}

    from unilabos.registry.yaml_ref import resolve_yaml_refs

    # ``registry_roots`` 是项目显式声明或包内默认的注册表（Registry）根集合。
    registry_roots = discover_registry_paths_from_project(pkg_dir)
    if not registry_roots:
        return {}

    # ``entries`` 聚合以稳定设备定义身份索引的目录式注册表（Registry）条目。
    entries: dict[str, dict[str, Any]] = {}
    for registry_root in registry_roots:
        # ``devices_dir`` 是当前注册表根中唯一允许读取设备定义的子目录。
        devices_dir = registry_root / "devices"
        if not devices_dir.is_dir():
            continue
        for yaml_path in sorted(
            list(devices_dir.glob("*.yaml")) + list(devices_dir.glob("*.yml"))
        ):
            try:
                # ``raw`` 保留单文件解析形状；``data`` 是本地引用展开后的完整条目。
                raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
                data = resolve_yaml_refs(raw, base_file=yaml_path)
            except (
                IndexError,
                KeyError,
                OSError,
                TypeError,
                UnicodeError,
                ValueError,
                yaml.YAMLError,
            ) as error:
                logger.warning(f"[package] 解析外部注册表 {yaml_path} 失败: {error}")
                continue
            if not isinstance(data, dict):
                continue
            for device_id, entry in data.items():
                if not isinstance(entry, dict):
                    continue
                # ``registry_class`` 是当前候选设备定义的驱动注册信息。
                registry_class = (
                    entry.get("class") if isinstance(entry.get("class"), dict) else {}
                )
                # ``is_device`` 关闭式验证目录条目是否具备可加载设备定义。
                is_device = (
                    bool(registry_class.get("module"))
                    or entry.get("resource_type") == "device"
                )
                if is_device:
                    entries[str(device_id)] = entry
    return entries


__all__ = [
    "discover_registry_paths_from_project",
    "normalize_name",
    "read_external_registry_devices",
    "read_pyproject",
    "read_registry_yaml_devices",
    "resolve_class_namespace",
    "scan_package_devices",
]
