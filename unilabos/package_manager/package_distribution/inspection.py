"""软件包检查（Package Inspect）的目录、归档和投影编排。"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from unilabos.utils.banner_print import print_status

from ..package_catalog import PackageCatalog, PackageCompileError, WorkspaceSource
from .archive import build_archive
from .errors import PackageCLIError
from .legacy_projection import (
    build_package_info,
    build_resources,
    build_resources_from_registry,
)
from .registry_discovery import (
    normalize_name,
    read_external_registry_devices,
    read_pyproject,
    read_registry_yaml_devices,
    resolve_class_namespace,
    scan_package_devices,
)

CatalogCompiler = Callable[[WorkspaceSource], PackageCatalog]


def inspect_package(
    path: str,
    namespace: str | None = None,
    out_dir: str | None = None,
    *,
    compile_catalog: CatalogCompiler,
) -> dict[str, Any]:
    """编译并打包一个本地软件包。

    参数：``path`` 是软件包根；``namespace`` 是仅供遗留包使用的可选社区命名空间；
    ``out_dir`` 是可选产物目录；``compile_catalog`` 是组合根显式注入且不执行作者
    代码的包目录（PackageCatalog）编译器。
    返回：包含包目录摘要、兼容上传投影和归档路径的字典。
    异常：路径、项目、静态定义或显式命名空间与规范目录冲突时抛出
    ``PackageCLIError``；文件系统归档/写入异常直接传播。本函数不读取产品配置、
    不启动 ROS 或设备，并且不发布部分规范编译结果。
    """

    if not callable(compile_catalog):
        raise TypeError("compile_catalog 必须可调用")
    # ``package_directory`` 是本次检查唯一授权的软件包根。
    package_directory = Path(path).resolve()
    if not package_directory.is_dir():
        raise PackageCLIError(f"包目录不存在：{package_directory}")

    # ``project`` 是软件包身份、版本和依赖的统一项目事实。
    project = read_pyproject(package_directory)
    # ``canonical_package`` 用来区分规范工作区与仅含 YAML 的遗留软件包。
    canonical_package = package_directory / normalize_name(project["name"])
    catalog: PackageCatalog | None = None
    if canonical_package.joinpath("__init__.py").is_file():
        try:
            # ``catalog`` 是这次检查唯一的规范静态编译结果。
            catalog = compile_catalog(WorkspaceSource(package_directory))
        except PackageCompileError as error:
            # ``diagnostic_codes`` 是完整失败诊断的稳定代码摘要，不含部分目录。
            diagnostic_codes = ", ".join(item.code for item in error.diagnostics)
            raise PackageCLIError(
                f"包目录（PackageCatalog）编译失败：{diagnostic_codes}"
            ) from error
        except (TypeError, ValueError) as error:
            raise PackageCLIError("包目录（PackageCatalog）编译失败") from error

    # ``class_namespace`` 是规范目录或遗留投影最终采用的类命名空间身份。
    class_namespace = (
        catalog.namespace
        if catalog is not None
        else resolve_class_namespace(project["name"], namespace)
    )
    if catalog is not None and namespace is not None:
        # ``requested_namespace`` 是用户请求的遗留覆盖，用于与规范身份比对。
        requested_namespace = resolve_class_namespace(project["name"], namespace)
        if requested_namespace != catalog.namespace:
            raise PackageCLIError(
                "规范工作区命名空间由项目身份决定，不能用 --namespace 覆盖"
            )

    # 以下路径共同标识本次检查的产物代与不可变归档内容。
    output_path = (
        Path(out_dir).resolve() if out_dir else (package_directory.parent / "dist")
    )
    output_path.mkdir(parents=True, exist_ok=True)
    archive_name = f"{normalize_name(project['name'])}-{project['version']}.tar.gz"
    archive_path = output_path / archive_name
    # ``archive_digest`` 是后续上传投影绑定的归档内容指纹。
    archive_digest = build_archive(package_directory, archive_path)
    # ``package_info`` 是与当前归档指纹绑定的遗留上传元信息。
    package_info = build_package_info(
        project,
        class_namespace,
        archive_digest,
    )

    if catalog is not None:
        # ``catalog_document`` 是兼容投影与落盘共用的解冻规范目录。
        catalog_document = catalog.to_dict()
        # ``registry_entries`` 只是遗留上传 DTO，不成为第二个定义权威。
        registry_entries = {
            item["id"]: item["details"]["registry_entry"]
            for definition_kind in ("devices", "resources")
            for item in catalog_document["definitions"][definition_kind]
        }
        # ``device_source`` 和 ``device_ids`` 描述本次投影采用的规范定义来源。
        device_source = "包目录（PackageCatalog）"
        device_ids = [item["id"] for item in catalog_document["definitions"]["devices"]]
        resources = build_resources_from_registry(registry_entries, package_info)
        catalog_path: Path | None = output_path / "package.catalog.json"
        catalog_path.write_bytes(catalog.to_canonical_bytes())
    else:
        # ``yaml_entries`` 优先读取根 YAML，再回退目录式外部注册表（Registry）。
        yaml_entries = read_registry_yaml_devices(package_directory)
        if not yaml_entries:
            yaml_entries = read_external_registry_devices(package_directory)
            registry_source = "unilabos_registry/"
        else:
            registry_source = "registry.yaml"
        if yaml_entries:
            device_source = registry_source
            device_ids = sorted(yaml_entries)
            resources = build_resources_from_registry(yaml_entries, package_info)
        else:
            device_source = "@device AST"
            # ``ast_devices`` 是两种 YAML 来源均为空时的最终遗留扫描候选。
            ast_devices = scan_package_devices(package_directory)
            device_ids = sorted(ast_devices)
            resources = build_resources(ast_devices, package_info)
        catalog_path = None

    # ``devices`` 保留旧调用方只查询设备身份集合的返回形状。
    devices = {resource_id: None for resource_id in device_ids}
    if not resources:
        print_status(
            f"警告：{package_directory} 未发现 registry.yaml / "
            "unilabos_registry/ 或 @device 设备，仅生成 package_info",
            "warning",
        )

    # 两个 JSON 路径是后端遗留上传接口消费的确定性产物。
    package_info_path = output_path / "package_info.json"
    resources_path = output_path / "resources.json"
    package_info_path.write_text(
        json.dumps(package_info, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    resources_path.write_text(
        json.dumps(resources, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _print_inspection_summary(
        project=project,
        class_namespace=class_namespace,
        device_source=device_source,
        device_ids=device_ids,
        resources=resources,
        archive_path=archive_path,
        archive_digest=archive_digest,
        package_info_path=package_info_path,
        resources_path=resources_path,
    )
    return {
        "project": project,
        "class_namespace": class_namespace,
        "devices": devices,
        "archive_path": str(archive_path),
        "sha256": archive_digest,
        "package_info": package_info,
        "resources": resources,
        "package_info_path": str(package_info_path),
        "resources_path": str(resources_path),
        "catalog_digest": catalog.catalog_digest if catalog is not None else None,
        "catalog_path": str(catalog_path) if catalog_path is not None else None,
    }


def _print_inspection_summary(
    *,
    project: dict[str, Any],
    class_namespace: str,
    device_source: str,
    device_ids: list[str],
    resources: list[dict[str, Any]],
    archive_path: Path,
    archive_digest: str,
    package_info_path: Path,
    resources_path: Path,
) -> None:
    """输出一次软件包检查的稳定人类可读摘要。

    参数：``project`` 是项目元数据；``class_namespace`` 是类命名空间；
    ``device_source`` 是定义来源标签；``device_ids`` 与 ``resources`` 是设备身份和
    投影；``archive_path`` 与 ``archive_digest`` 是归档事实；末两个路径是遗留 DTO。
    返回：无。
    异常：终端输出 Adapter 的异常原样传播，不改变已经产生的检查结果。
    """

    print_status(
        f"package inspect 完成：{project['name']}@{project['version']}",
        "info",
    )
    print_status(f"  class_namespace : {class_namespace}", "info")
    print_status(f"  设备来源        : {device_source}", "info")
    print_status(
        f"  设备数          : {len(device_ids)} ({', '.join(device_ids) or '无'})",
        "info",
    )
    print_status(f"  资源投影数      : {len(resources)}", "info")
    print_status(f"  归档            : {archive_path} ({archive_digest})", "info")
    print_status(f"  package_info    : {package_info_path}", "info")
    print_status(f"  resources       : {resources_path}", "info")


__all__ = ["CatalogCompiler", "inspect_package"]
