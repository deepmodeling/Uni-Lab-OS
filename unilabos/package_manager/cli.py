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
    for action_name in ("inspect", "build", "upload"):  # assets 单独注册
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

    assets_parser = actions.add_parser(
        "assets",
        help="Fetch or verify pinned asset-library packages from OSS by asset lock",
    )
    assets_parser.add_argument("assets_action", choices=("fetch", "verify", "share", "build", "check-bindings"))
    assets_parser.add_argument("--lock", help="Domain package assets lock (unilab.asset-dependencies/1); required except for share")
    assets_parser.add_argument("--cache", help="Local asset cache directory; required except for share")
    assets_parser.add_argument("--offline", action="store_true",
                               help="Never contact the asset store: use lock rows with local path and packages already in --cache")
    assets_parser.add_argument("--root", action="append", default=[],
                               help="share: project-relative model directory holding local USD models (repeatable)")
    assets_parser.add_argument("--store", default=None, help="Optional OSS store config JSON (endpoint/bucket/region/prefix)")
    assets_parser.add_argument("--mirror", default=None, help="Use a local asset-library mirror instead of OSS")
    assets_parser.add_argument("--only", nargs="*", default=None, help="Lock keys (key or owner_registry_id) to fetch")
    assets_parser.add_argument("--output", help="New build directory or binding report JSON")
    assets_parser.add_argument("--python-package", help="Import package name for explicit behavior build")
    assets_parser.add_argument(
        "--project",
        help="Domain package project root (fetch: copy model deliveries into lock model_dir; "
        "verify: also check those copies; check-bindings: read-only)",
    )
    assets_parser.add_argument("--profile", choices=("web", "spatial"), default="web",
                               help="Model delivery for --project: web lightweight profile (default) or full spatial entry")
    assets_parser.add_argument("--no-share-textures", action="store_true",
                               help="Do not share identical PNGs across model directories under --project")
    assets_parser.add_argument("--preserve", action="append", default=[], help="Project-relative installation/graph file to hash")
    assets_parser.add_argument("--baseline", help="Previous binding report to compare contracts and transforms")

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
            "`unilab package inspect|build|upload|add|update|remove|assets`"
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
    if action == "assets":
        _cmd_assets(args_dict)
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


def _cmd_assets(args_dict: dict[str, Any]) -> None:
    """按资产锁从资产库（OSS 或本地镜像）拉取或复核固定工程包。

    参数：``args_dict`` 含 ``assets_action``、``lock``、``cache``、``store``、``mirror``、``only``；
    给 ``project`` 时 fetch 还把模型交付落到项目模型目录并共享相同贴图，verify 还核对这些副本。
    返回：无；逐包输出回执。
    异常：锁、索引、下载或校验失败时抛 ``PackageCLIError``，不留下部分写入的缓存包。
    """

    import json as _json

    from .package_distribution.adapters.asset_store import AssetStoreError, open_asset_store
    from .package_distribution.asset_fetch import (
        AssetFetchError,
        AssetIntegrityError,
        fetch_lock,
        read_asset_lock,
        verify_package,
    )

    operation = args_dict.get("assets_action")
    if operation == "share":
        _cmd_assets_share(args_dict)
        return
    if not args_dict.get("lock") or not args_dict.get("cache"):
        raise PackageCLIError(f"package assets {operation} 需要 --lock 与 --cache")
    lock = Path(args_dict["lock"]).expanduser()
    cache = Path(args_dict["cache"]).expanduser()
    try:
        if operation in ("build", "check-bindings"):
            from .package_distribution.asset_behavior import build_locked, check_bindings
            if args_dict.get("only"):
                raise ValueError("构建/绑定检查必须覆盖完整显式锁；不接受 --only")
            if not args_dict.get("output"):
                raise ValueError("构建/绑定检查需要 --output")
            output = Path(args_dict["output"]).resolve()
            if output == lock.resolve() or output.is_relative_to(cache.resolve()):
                raise ValueError("输出不能覆盖资产锁或固定缓存")
            if operation == "build":
                if not args_dict.get("python_package"):
                    raise ValueError("构建需要 --python-package")
                result = build_locked(lock, cache, output, args_dict["python_package"])
            else:
                from unilabos.registry.ast_registry_scanner import _parse_file
                if not args_dict.get("project"):
                    raise ValueError("绑定检查需要 --project")
                if output.exists():
                    raise ValueError("绑定报告必须写到新文件，以保留基线")
                baseline = _json.loads(Path(args_dict["baseline"]).read_text("utf-8")) if args_dict.get("baseline") else None
                result = check_bindings(lock, cache, Path(args_dict["project"]), scan_file=_parse_file,
                                        preserve=tuple(args_dict.get("preserve") or []), baseline=baseline)
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(_json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(_json.dumps(result, ensure_ascii=False))
            if result["status"] != "passed":
                raise PackageCLIError("资产绑定检查未通过：" + result["status"])
            return
        if args_dict.get("assets_action") == "verify":
            from .package_distribution.asset_fetch import cached_packages, local_package

            rows = read_asset_lock(lock)
            only = set(args_dict.get("only") or [])
            cached = cached_packages(cache)
            results = []
            for row in rows:
                key = row.get("key") or row.get("owner_registry_id") or row.get("asset_id") or row.get("path")
                if only and key not in only:
                    continue
                if row.get("path"):
                    folder = local_package(lock, row)
                else:
                    folder = cached.get(row["package_digest"])
                    if folder is None:
                        raise AssetFetchError(f"缓存里没有 {key} 的固定版本")
                if row.get("package_digest"):
                    verify_package(folder, row["package_digest"])
                results.append({"key": key, "path": str(folder), "status": "passed"})
            result = {"status": "passed", "packages": results}
            if args_dict.get("project"):
                from .package_distribution.asset_delivery import check_materialized

                problems = check_materialized(rows, results, Path(args_dict["project"]),
                                              profile=args_dict.get("profile") or "web")
                if problems:
                    raise AssetFetchError("落盘模型副本与锁不符：" + "；".join(problems[:20]))
                result["materialized"] = "passed"
        else:
            store = None if args_dict.get("offline") else (
                lambda: open_asset_store(args_dict.get("store"), args_dict.get("mirror")))
            result = fetch_lock(store, lock, cache, only=args_dict.get("only"))
            if args_dict.get("project"):
                from .package_distribution.asset_delivery import materialize_lock

                result["materialized"] = materialize_lock(
                    read_asset_lock(lock), result["packages"], Path(args_dict["project"]),
                    profile=args_dict.get("profile") or "web",
                    share=not args_dict.get("no_share_textures"),
                )
    except (ValueError, AssetStoreError, OSError, KeyError, TypeError) as error:
        raise PackageCLIError(f"package assets 失败：{error}") from error
    for row in result["packages"]:
        print_status(f"  {row['key']}: {row.get('action', 'verified')} -> {row['path']}", "info")
    print(_json.dumps(result, ensure_ascii=False))


def _cmd_assets_share(args_dict: dict[str, Any]) -> None:
    """在领域包已有的模型目录之间共享相同贴图（本地模型或已落盘的资产库模型都适用）。

    参数：``project`` 领域包项目根；``root`` 项目相对模型目录（可多个），另含共享清单里已登记的目录；
    给 ``lock`` 时再加锁行 ``model_dir/usd``。返回：无；输出统计。异常：参数或共享失败时抛 ``PackageCLIError``。
    """

    import json as _json
    from pathlib import PurePosixPath

    from .package_distribution.asset_delivery import share_textures
    from .package_distribution.asset_fetch import read_asset_lock

    if not args_dict.get("project"):
        raise PackageCLIError("package assets share 需要 --project")
    project = Path(args_dict["project"]).expanduser()
    roots = [PurePosixPath(root) for root in args_dict.get("root") or []]
    try:
        if args_dict.get("lock"):
            roots += [PurePosixPath(row["model_dir"]) / "usd" for row in read_asset_lock(Path(args_dict["lock"]))
                      if row.get("model_dir")]
        if not roots:
            raise ValueError("没有模型目录：给 --root 或 --lock")
        packages = {root.parts[0] for root in roots}
        if len(packages) != 1:
            raise ValueError("模型目录必须同属一个导入包：" + ", ".join(sorted(packages)))
        result = share_textures(project, packages.pop(), roots)
    except (ValueError, OSError, KeyError) as error:
        raise PackageCLIError(f"package assets share 失败：{error}") from error
    print(_json.dumps(result, ensure_ascii=False))


__all__ = [
    "PackageCLIError",
    "build_package",
    "cmd_package",
    "inspect_package",
    "register_package_subcommands",
    "upload_package",
]
