"""软件包命令行（Package CLI）的公共分派入口。"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from unilabos.utils.banner_print import print_status

from .package_catalog import PackageCompileError
from .package_distribution import (
    PackageBuildArtifact,
    PackageBuildError,
    PackageDependencyManager,
    build_workspace_package,
)
from .package_distribution.adapters.cloud import upload_package as _upload_package
from .package_distribution.errors import PackageCLIError
from .package_distribution.inspection import inspect_package as _inspect_package
from .workspace_runtime.discovery import compile_package_source


def inspect_package(
    path: str,
    namespace: str | None = None,
    out_dir: str | None = None,
) -> dict[str, Any]:
    """用产品统一编译器检查并归档一个软件包。

    参数：``path`` 是软件包根；``namespace`` 是仅供遗留包使用的类命名空间；
    ``out_dir`` 是可选产物目录。
    返回：包含规范包目录（PackageCatalog）摘要、遗留 DTO 和归档路径的结果。
    异常：路径、静态编译、归档或遗留投影失败时保持 ``PackageCLIError`` 和既有
    文件系统异常语义；不执行作者驱动代码。
    """

    return _inspect_package(
        path,
        namespace=namespace,
        out_dir=out_dir,
        compile_catalog=compile_package_source,
    )


def build_package(
    path: str,
    out_dir: str | None = None,
) -> PackageBuildArtifact:
    """构建并自审计一个规范软件包工作区（Package Workspace）。

    参数：``path`` 是软件包根；``out_dir`` 是可选发布产物目录，默认使用工作区
    同级 ``dist``。
    返回：已通过 wheel 来源重编译和闭包校验的软件包构建产物。
    异常：工作区、标准构建或自审计失败时统一抛出 ``PackageCLIError``；作者源码
    不会被写入生成目录。
    """

    # ``workspace_root`` 是公共命令显式选择的规范源码边界。
    workspace_root = Path(path).expanduser().resolve()
    # ``output_root`` 是审计通过后才接收 wheel 和投影的发布目录。
    output_root = (
        Path(out_dir).expanduser().resolve()
        if out_dir
        else workspace_root.parent / "dist"
    )
    try:
        # ``artifact`` 是包分发深模块完成全部物理构建和内容证明后的结果。
        artifact = build_workspace_package(
            workspace_root,
            output_root,
            compile_catalog=compile_package_source,
        )
    except PackageCompileError as error:
        # ``diagnostic_codes`` 提供稳定、无源码正文的目录编译失败摘要。
        diagnostic_codes = ", ".join(item.code for item in error.diagnostics)
        raise PackageCLIError(
            f"包目录（PackageCatalog）编译失败：{diagnostic_codes}"
        ) from error
    except (PackageBuildError, TypeError, ValueError) as error:
        raise PackageCLIError(str(error)) from error
    print_status(
        "package build 完成："
        f"{artifact.catalog.distribution.name}@"
        f"{artifact.catalog.distribution.version}",
        "info",
    )
    print_status(f"  wheel           : {artifact.wheel}", "info")
    print_status(f"  catalog_digest  : {artifact.catalog.catalog_digest}", "info")
    print_status(f"  artifact_digest : {artifact.artifact_digest}", "info")
    return artifact


def upload_package(
    path: str,
    http_client: Any,
    out_dir: str | None = None,
) -> dict[str, Any]:
    """检查软件包并通过云端 Adapter 发布既有兼容投影。

    参数：``path`` 是软件包根；``http_client`` 是鉴权 HTTP Adapter；``out_dir``
    是产物目录。
    返回：云端发布结果。
    异常：检查、鉴权、上传或云端拒绝时保留既有 ``PackageCLIError``/传输异常语义。
    """

    return _upload_package(
        path,
        http_client,
        out_dir=out_dir,
        package_builder=build_package,
    )


def register_package_subcommands(subparsers: Any) -> None:
    """把软件包命令行（Package CLI）完整注册到公共 ``unilab`` 解析器。

    参数：``subparsers`` 是应用组合根（Composition Root）创建的顶层 argparse
    子解析器集合。
    返回：无；注册 ``package``/``pkg`` 及 inspect、upload、add、update、remove。
    异常：重复注册或传入对象不兼容 argparse 时传播原始异常。依赖管理动作都
    支持末尾 ``--workspace [PATH]``，省略 PATH 时使用当前目录。
    """

    package_parser = subparsers.add_parser(
        "package",
        aliases=["pkg"],
        help="Community package inspect, build, upload, and dependency tools",
    )
    actions = package_parser.add_subparsers(
        title="package actions",
        dest="package_action",
    )
    for action_name in ("inspect", "build", "upload"):
        action_parser = actions.add_parser(
            action_name,
            help=(
                "Compile one package and write package catalog artifacts"
                if action_name == "inspect"
                else (
                    "Build and audit one Catalog-embedded wheel"
                    if action_name == "build"
                    else "Build, audit, and upload one package wheel"
                )
            ),
        )
        action_parser.add_argument(
            "--path",
            dest="package_path",
            type=str,
            required=True,
            help="Package workspace containing pyproject.toml",
        )
        if action_name == "inspect":
            action_parser.add_argument(
                "--namespace",
                default=None,
                help="Legacy class namespace override",
            )
        action_parser.add_argument(
            "--out",
            default=None,
            help="Artifact output directory",
        )

    add_parser = actions.add_parser(
        "add",
        help="Add and lock an explicit external package workspace",
    )
    add_parser.add_argument(
        "dependency_source",
        help="External Uni-Lab package workspace path",
    )
    _add_dependency_workspace_flag(add_parser)

    update_parser = actions.add_parser(
        "update",
        help="Recompile and relock one explicit package dependency",
    )
    update_parser.add_argument(
        "dependency_identity",
        help="Distribution name, normalized name, or community namespace",
    )
    update_parser.add_argument(
        "dependency_source",
        nargs="?",
        default="",
        help="Optional replacement package workspace path",
    )
    _add_dependency_workspace_flag(update_parser)

    remove_parser = actions.add_parser(
        "remove",
        help="Remove one explicit package dependency",
    )
    remove_parser.add_argument(
        "dependency_identity",
        help="Distribution name, normalized name, or community namespace",
    )
    _add_dependency_workspace_flag(remove_parser)


def _add_dependency_workspace_flag(parser: argparse.ArgumentParser) -> None:
    """为一个依赖管理子命令增加当前路径缺省的工作区选择。

    参数：``parser`` 是 add、update 或 remove 子解析器。
    返回：无；增加与顶层 ``--workspace`` 相同目标字段的可选参数。
    异常：解析器字段冲突时传播 argparse 原始异常。
    """

    parser.add_argument(
        "--workspace",
        nargs="?",
        const=".",
        default=argparse.SUPPRESS,
        help="Main workspace root (default: current directory)",
    )


def cmd_package(args_dict: dict[str, Any], http_client: Any = None) -> None:
    """分派一次软件包子命令。

    参数：``args_dict`` 是公共命令行解析结果；``http_client`` 是仅发布
    动作需要的可选鉴权 HTTP 适配器。
    返回：无；成功结果由具体子命令输出。
    异常：动作、路径、显式依赖或具体操作无效时抛出 ``PackageCLIError``；
    依赖管理绝不扫描或安装 ambient site-packages。
    """

    action = args_dict.get("package_action")
    package_path = args_dict.get("package_path")
    namespace = args_dict.get("namespace")
    output_directory = args_dict.get("out")

    if not action:
        raise PackageCLIError(
            "缺少 package 子动作，请使用 "
            "`unilab package inspect|build|upload|add|update|remove`"
        )
    if action in {"add", "update", "remove"}:
        try:
            manager = PackageDependencyManager(
                args_dict.get("workspace") or ".",
                compile_catalog=compile_package_source,
            )
            if action == "add":
                result = manager.add(args_dict.get("dependency_source") or "")
            elif action == "update":
                replacement_source = args_dict.get("dependency_source") or None
                result = manager.update(
                    args_dict.get("dependency_identity") or "",
                    replacement_source,
                )
            else:
                result = manager.remove(args_dict.get("dependency_identity") or "")
        except (RuntimeError, TypeError, ValueError) as error:
            raise PackageCLIError(str(error)) from error
        # ``result`` 是命令成功切换后的完整软件包依赖锁代际。
        print_status(
            f"package {action} 完成：锁定 {len(result.packages)} 个显式外部包",
            "info",
        )
        return
    if not package_path:
        raise PackageCLIError("缺少 --path（社区软件包工作区路径）")
    if action == "inspect":
        inspect_package(
            package_path,
            namespace=namespace,
            out_dir=output_directory,
        )
        return
    if action == "build":
        build_package(package_path, out_dir=output_directory)
        return
    if action == "upload":
        upload_package(
            package_path,
            http_client=http_client,
            out_dir=output_directory,
        )
        return
    raise PackageCLIError(f"未知 package 子动作：{action}")


__all__ = [
    "PackageCLIError",
    "build_package",
    "cmd_package",
    "inspect_package",
    "register_package_subcommands",
    "upload_package",
]
