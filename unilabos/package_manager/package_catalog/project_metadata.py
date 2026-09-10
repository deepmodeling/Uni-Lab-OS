"""包目录（PackageCatalog）使用的软件包项目声明唯一解析器。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import tomllib


@dataclass(frozen=True, slots=True)
class PackageProject:
    """规范化前后身份一致的软件包项目声明。"""

    # 以下字段共同描述发行身份、依赖和注册表（Registry）扫描声明。
    name: str
    normalized_name: str
    version: str
    description: str
    license: str
    homepage: str
    requires_python: str
    dependencies: tuple[str, ...]
    registry_paths: tuple[str, ...]
    # 以下字段是普通产品启动可采用、但不会授予设备导入资格的工作区默认值。
    startup_graph: str | None = None
    startup_config: str | None = None
    startup_app_bridges: tuple[str, ...] | None = None
    startup_ensure_dependencies: bool = True


def normalize_distribution_name(distribution_name: str) -> str:
    """把发行包名称规范化为唯一 Python 导入包身份。

    参数：``distribution_name`` 是 ``pyproject.toml`` 的 ``project.name``。
    返回：小写并把连字符、点和下划线段统一为下划线的 Python 标识符。
    异常：名称不是非空字符串或不能形成 Python 标识符时抛出 ``ValueError``。
    """

    if not isinstance(distribution_name, str) or not distribution_name.strip():
        raise ValueError("pyproject.toml project.name 必须是非空字符串")
    # ``normalized_name`` 同时决定导入包和社区命名空间，不接受运行时猜测。
    normalized_name = re.sub(r"[-_.]+", "_", distribution_name.strip().lower())
    if not normalized_name.isidentifier():
        raise ValueError("工作区发行包名称不能规范化为 Python 包身份")
    return normalized_name


def parse_project_metadata(raw: bytes) -> PackageProject:
    """静态解析 ``pyproject.toml`` 的封闭项目元数据子集。

    参数：``raw`` 是显式软件包来源读取的原始 TOML 字节。
    返回：供工作区、注册表（Registry）和 ``package inspect`` 共同使用的不可变声明。
    异常：UTF-8、TOML 或项目字段形状无效时抛出 ``ValueError``/``TypeError``。
    """

    try:
        document = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError) as error:
        raise ValueError("工作区 pyproject.toml 无效") from error
    project = document.get("project")
    if not isinstance(project, dict):
        raise TypeError("pyproject.toml 缺少合法 [project]")
    name = project.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("pyproject.toml project.name 必须是非空字符串")
    version_value = project.get("version", "0.0.0")
    if not isinstance(version_value, str) or not version_value.strip():
        raise ValueError("pyproject.toml project.version 必须是非空字符串")
    dependencies_value = project.get("dependencies", [])
    if not isinstance(dependencies_value, list) or any(
        not isinstance(item, str) or not item.strip() for item in dependencies_value
    ):
        raise TypeError("pyproject.toml project.dependencies 必须是非空字符串列表")
    # ``license_text`` 是兼容 PEP 621 字符串和旧表形状的只读展示元数据。
    license_value = project.get("license", "")
    if isinstance(license_value, dict):
        license_text = str(license_value.get("text") or license_value.get("file") or "")
    elif isinstance(license_value, str):
        license_text = license_value
    else:
        raise TypeError("pyproject.toml project.license 必须是字符串或表")
    urls = project.get("urls", {})
    if not isinstance(urls, dict):
        raise TypeError("pyproject.toml project.urls 必须是表")
    homepage = next(
        (
            str(urls[key])
            for key in (
                "Homepage",
                "homepage",
                "Repository",
                "repository",
                "Source",
                "source",
            )
            if isinstance(urls.get(key), str) and urls[key]
        ),
        "",
    )
    description = project.get("description", "")
    requires_python = project.get("requires-python", "")
    if not isinstance(description, str) or not isinstance(requires_python, str):
        raise TypeError("pyproject.toml 项目描述和 Python 版本要求必须是字符串")
    # ``registry_paths`` 是注册表（Registry）与包工具共用的显式目录声明。
    tool = document.get("tool", {})
    if not isinstance(tool, dict):
        raise TypeError("pyproject.toml tool 必须是表")
    unilabos_tool = tool.get("unilabos", {})
    if not isinstance(unilabos_tool, dict):
        raise TypeError("pyproject.toml tool.unilabos 必须是表")
    registry_tool = unilabos_tool.get("registry", {})
    if not isinstance(registry_tool, dict):
        raise TypeError("pyproject.toml tool.unilabos.registry 必须是表")
    registry_paths_value = registry_tool.get("paths", [])
    if not isinstance(registry_paths_value, list) or any(
        not isinstance(item, str) or not item.strip() for item in registry_paths_value
    ):
        raise TypeError("pyproject.toml 注册表路径必须是非空字符串列表")
    (
        startup_graph,
        startup_config,
        startup_app_bridges,
        startup_ensure_dependencies,
    ) = _parse_startup_defaults(unilabos_tool)
    return PackageProject(
        name=name.strip(),
        normalized_name=normalize_distribution_name(name),
        version=version_value.strip(),
        description=description,
        license=license_text,
        homepage=homepage,
        requires_python=requires_python,
        dependencies=tuple(sorted(item.strip() for item in dependencies_value)),
        registry_paths=tuple(item.strip() for item in registry_paths_value),
        startup_graph=startup_graph,
        startup_config=startup_config,
        startup_app_bridges=startup_app_bridges,
        startup_ensure_dependencies=startup_ensure_dependencies,
    )


def _parse_startup_defaults(
    unilabos_tool: dict[str, Any],
) -> tuple[str | None, str | None, tuple[str, ...] | None, bool]:
    """解析产品工作区的封闭启动默认值。

    参数：``unilabos_tool`` 是已验证的 ``tool.unilabos`` TOML 表。
    返回：依次返回物理图、配置、应用桥接器和依赖保障策略；未声明时返回安全默认值。
    异常：未知字段、空路径、未知或重复桥接器、非布尔依赖策略时抛出
    ``ValueError``/``TypeError``，禁止调用者另行宽松解释同一项目文件。
    """

    startup_table = unilabos_tool.get("startup")
    if startup_table is None:
        return None, None, None, True
    if not isinstance(startup_table, dict):
        raise TypeError("pyproject.toml [tool.unilabos.startup] 必须是表")
    if not set(startup_table).issubset(
        {"graph", "config", "app_bridges", "ensure_dependencies"}
    ):
        raise ValueError("pyproject.toml [tool.unilabos.startup] 字段无效")

    # ``startup_graph`` 与 ``startup_config`` 仍须由工作区来源验证边界和存在性。
    startup_graph = startup_table.get("graph")
    startup_config = startup_table.get("config")
    for field_name, field_value in (
        ("graph", startup_graph),
        ("config", startup_config),
    ):
        if field_value is not None and (
            not isinstance(field_value, str) or not field_value.strip()
        ):
            raise ValueError(f"工作区启动 {field_name} 必须是非空字符串")

    # ``startup_app_bridges`` 是当前产品明确支持的本地桥接器封闭集合。
    startup_app_bridges_value = startup_table.get("app_bridges")
    startup_app_bridges: tuple[str, ...] | None = None
    if startup_app_bridges_value is not None:
        if (
            not isinstance(startup_app_bridges_value, list)
            or not startup_app_bridges_value
            or any(
                not isinstance(bridge, str) or bridge not in {"websocket", "fastapi"}
                for bridge in startup_app_bridges_value
            )
            or len(set(startup_app_bridges_value)) != len(startup_app_bridges_value)
        ):
            raise ValueError("工作区启动 app_bridges 必须是非空且不重复的已知集合")
        startup_app_bridges = tuple(startup_app_bridges_value)

    # ``startup_ensure_dependencies`` 控制启动前环境保障，不改变包目录编译语义。
    startup_ensure_dependencies = startup_table.get("ensure_dependencies", True)
    if not isinstance(startup_ensure_dependencies, bool):
        # 对外启动合同历史上使用 ValueError；保持调用方错误分类稳定。
        raise ValueError(  # noqa: TRY004
            "工作区启动 ensure_dependencies 必须是布尔值"
        )
    return (
        startup_graph,
        startup_config,
        startup_app_bridges,
        startup_ensure_dependencies,
    )


def project_to_legacy_dict(project: PackageProject) -> dict[str, Any]:
    """把统一项目模型投影为遗留包上传代码需要的字段字典。

    参数：``project`` 是已验证的软件包项目声明。
    返回：不含额外权威、仅供现有上传适配器消费的兼容字典。
    异常：参数类型错误时抛出 ``TypeError``。
    """

    if not isinstance(project, PackageProject):
        raise TypeError("project 必须是 PackageProject")
    return {
        "name": project.name,
        "version": project.version,
        "summary": project.description,
        "license": project.license,
        "homepage": project.homepage,
        "dependencies": list(project.dependencies),
    }


__all__ = [
    "PackageProject",
    "normalize_distribution_name",
    "parse_project_metadata",
    "project_to_legacy_dict",
]
