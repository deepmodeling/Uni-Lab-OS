"""把完整工作区包代适配到遗留社区包启动链的单一组合接缝。"""

from __future__ import annotations

import json
import os
from typing import Any

from unilabos.app.community_packages import (
    CommunityPackageError,
    prepare_community_packages,
)
from unilabos.config.config import BasicConfig
from unilabos.package_manager.workspace_runtime import WorkspaceRegistryRuntime


class WorkspaceCommunityBootstrapError(RuntimeError):
    """表示工作区本地包来源无法安全接入遗留社区包启动链。"""


def resolve_graph_file_path(file_path: str | None) -> str | None:
    """按产品既有规则解析物理图（Graph）文件路径。

    参数：``file_path`` 是绝对路径、当前目录相对路径或包内相对路径。
    返回：命中的普通文件绝对路径；没有命中时原样返回，空值保持为空。
    异常：无；文件系统访问错误按未命中处理，由后续图加载合同给出诊断。
    """

    if file_path is None or os.path.isabs(file_path):
        return file_path
    # ``current_directory_candidate`` 优先保持公共命令行相对当前目录的既有语义。
    current_directory_candidate = os.path.abspath(file_path)
    if os.path.isfile(current_directory_candidate):
        return current_directory_candidate
    # ``package_candidate`` 兼容相对 UniLabOS 包根的历史图路径。
    package_candidate = os.path.abspath(
        os.path.join(__file__, "..", "..", file_path)
    )
    if os.path.isfile(package_candidate):
        return package_candidate
    return file_path


def load_graph_json_preview(file_path: str | None) -> dict[str, Any] | None:
    """只读预览一个本地 JSON 物理图（Graph）。

    参数：``file_path`` 是可选图文件路径。
    返回：成功读取对象时返回字典；非 JSON、缺失或内容无效时返回 ``None``。
    异常：无；预览错误留给正式图加载阶段处理，不在社区包准备前泄漏文件内容。
    """

    if (
        not file_path
        or not file_path.endswith(".json")
        or not os.path.isfile(file_path)
    ):
        return None
    try:
        with open(file_path, encoding="utf-8") as graph_file:
            # ``graph_preview`` 是只供社区命名空间识别的临时只读对象。
            graph_preview = json.load(graph_file)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return graph_preview if isinstance(graph_preview, dict) else None


def local_package_namespaces(
    runtime: WorkspaceRegistryRuntime,
) -> dict[str, str]:
    """投影完整候选代中所有显式本地包的社区命名空间。

    参数：``runtime`` 是已经完成主包和锁定外部包静态编译的工作区运行时。
    返回：以每个实际 Python 包目录为键、规范社区命名空间为值的新字典。
    异常：运行时或来源目录形状无效时抛出 ``TypeError``/``ValueError``。
    不变量：该映射只证明包来源已在本地满足；真正的作者模块导入仍只服从有限
    激活计划，不会因登记命名空间而开放未选外部包。
    """

    if not isinstance(runtime, WorkspaceRegistryRuntime):
        raise TypeError("runtime 必须是 WorkspaceRegistryRuntime")
    # ``namespace_by_package_directory`` 是遗留社区包解析器唯一可见的本地来源表。
    namespace_by_package_directory: dict[str, str] = {}
    for package_source in runtime.package_catalog_sources:
        # ``package_directory`` 是目录声明的规范 Python 导入包，不是整个来源根。
        package_directory = (
            package_source.source.root / package_source.catalog.import_package
        )
        if not package_directory.is_dir() or package_directory.is_symlink():
            raise ValueError("完整候选代包含无效的本地 Python 包目录")
        package_directory_text = str(package_directory)
        prior_namespace = namespace_by_package_directory.setdefault(
            package_directory_text,
            package_source.catalog.namespace,
        )
        if prior_namespace != package_source.catalog.namespace:
            raise ValueError("同一本地 Python 包目录映射到多个社区命名空间")
    return namespace_by_package_directory


def prepare_startup_community_packages(
    arguments: dict[str, Any],
    *,
    runtime: WorkspaceRegistryRuntime | None,
    check_mode: bool,
    workflow_upload: bool,
    ensure_dependencies: bool,
) -> None:
    """准备物理图引用的本地或远端社区包并回写启动参数。

    参数：``arguments`` 是产品启动参数；``runtime`` 是可选完整工作区候选代；
    ``check_mode`` 与 ``workflow_upload`` 决定是否跳过设备启动；``ensure_dependencies``
    控制依赖保障。
    返回：无；成功后回写固定图路径、启动 JSON、设备目录和本地命名空间。
    异常：社区包解析或依赖保障失败时抛出
    ``WorkspaceCommunityBootstrapError``，不静默回退到另一套本地包来源。
    不变量：工作区锁定外部包先登记为本地来源，社区包解析器不得把它们重新解释
    为远端缺失包。
    """

    if check_mode or workflow_upload:
        return
    # ``startup_json_preview`` 只在没有本地物理图时保存远端启动快照。
    startup_json_preview: dict[str, Any] | None = None
    if runtime is not None:
        # ``graph_file_path`` 和 ``graph_preview`` 来自同一冻结物理图（Graph）观察。
        graph_file_path = str(runtime.graph_path)
        graph_preview = runtime.graph_copy()
        arguments["_community_namespaces"] = local_package_namespaces(runtime)
    else:
        graph_file_path = resolve_graph_file_path(
            arguments.get("graph") or BasicConfig.startup_json_path
        )
        graph_preview = load_graph_json_preview(graph_file_path)
    arguments["_graph_file_path"] = graph_file_path

    # ``community_http_client`` 只在已有产品鉴权时允许解析远端社区包。
    community_http_client = None
    if BasicConfig.ak and BasicConfig.sk:
        from unilabos.app.web import http_client

        community_http_client = http_client
        if graph_preview is None and graph_file_path is None:
            startup_json_preview = community_http_client.request_startup_json()
            arguments["_startup_json"] = startup_json_preview
            graph_preview = startup_json_preview
    if not graph_preview:
        return

    try:
        # ``community_result`` 只补充尚未被完整本地候选代满足的远端社区包。
        community_result = prepare_community_packages(
            graph_preview,
            working_dir=BasicConfig.working_dir,
            http_client=community_http_client,
            available_namespaces=arguments.get("_community_namespaces"),
        )
    except CommunityPackageError as error:
        raise WorkspaceCommunityBootstrapError(str(error)) from error

    if community_result.devices_dirs:
        # ``combined_device_directories`` 仅用于遗留远端社区包 AST 扫描；工作区包
        # 本身仍由注册表快照（Registry Snapshot）发布。
        combined_device_directories = [
            *(arguments.get("devices") or []),
            *community_result.devices_dirs,
        ]
        arguments["devices"] = combined_device_directories
        if ensure_dependencies:
            from unilabos.utils.environment_check import (
                check_device_package_requirements,
                install_requirements_list,
            )

            if community_result.dependencies and not install_requirements_list(
                community_result.dependencies,
                label="community",
            ):
                raise WorkspaceCommunityBootstrapError(
                    "社区包 Python 依赖安装失败"
                )
            if not check_device_package_requirements(combined_device_directories):
                raise WorkspaceCommunityBootstrapError(
                    "社区包设备依赖检查失败"
                )
    arguments["_community_namespaces"] = community_result.namespaces


__all__ = [
    "WorkspaceCommunityBootstrapError",
    "load_graph_json_preview",
    "local_package_namespaces",
    "prepare_startup_community_packages",
    "resolve_graph_file_path",
]
