"""把一个显式工作区（Workspace）投影为产品基线启动配置。"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from unilabos.workflow.source_manifest import (
    EditablePackageManifest,
    SourceManifestError,
    parse_editable_package_manifest,
)
from unilabos.workflow.source_workspace import (
    PackageRootSnapshot,
    SourceWorkspaceError,
    read_package_root,
    validate_declared_sources,
)

from ..package_catalog.compilers.python import (
    compile_package_source as compile_python_package_source,
)
from ..package_catalog.model import (
    PackageCatalog,
    PackageCompileError,
    PackageDiagnostic,
)
from ..package_catalog.project_metadata import (
    PackageProject,
    normalize_distribution_name,
    parse_project_metadata,
)
from ..package_catalog.sources import WorkspaceSource


@dataclass(frozen=True)
class WorkspaceStartupPlan:
    """完成静态校验、尚未激活设备或工作流的启动计划。"""

    # ``source`` 是本轮启动唯一被授权的工作区文件来源。
    source: WorkspaceSource
    # ``project_metadata`` 是工作区、注册表（Registry）和包工具共用的项目声明。
    project_metadata: PackageProject
    # ``distribution_name`` 是 pyproject.toml 声明的原始发行包身份。
    distribution_name: str
    # ``import_package`` 是从发行元数据规范化得到的 Python 导入包身份。
    import_package: str
    # ``package_directory`` 是注册表（Registry）唯一允许扫描的本地包目录。
    package_directory: Path
    # ``community_namespace`` 是本地包对物理图公开的社区命名空间。
    community_namespace: str
    # ``has_workflow_manifest`` 表示工作区是否声明了工作流源码清单。
    has_workflow_manifest: bool
    # ``workflow_source_count`` 是清单静态校验通过的工作流源码数量。
    workflow_source_count: int
    # ``workflow_manifest`` 是本次完整解析后的封闭来源清单；缺失文件时为 None。
    workflow_manifest: EditablePackageManifest | None
    # ``default_graph`` 是工作区为普通启动选择的物理图相对路径。
    default_graph: str | None
    # ``default_config`` 是工作区为普通启动选择的配置文件相对路径。
    default_config: str | None
    # ``default_app_bridges`` 是工作区覆盖产品通用桥接默认值的封闭集合。
    default_app_bridges: tuple[str, ...] | None
    # ``ensure_dependencies`` 决定启动时是否检查并自动补齐 Python 依赖。
    ensure_dependencies: bool
    # ``project_file_bytes`` 是本次计划解析过的唯一项目清单原始字节。
    project_file_bytes: bytes = field(repr=False, compare=False)
    # ``workflow_manifest_bytes`` 是可选工作流源码清单的同次固定原始字节。
    workflow_manifest_bytes: bytes | None = field(repr=False, compare=False)

    def apply(self, arguments: dict[str, Any]) -> None:
        """把启动计划一次应用到现有产品参数字典。

        参数：``arguments`` 是公共命令行（CLI）解析后的可变参数字典。
        返回：无；应用后设备扫描、社区命名空间和工作流源码授权使用同一工作区。
        异常：调用者同时提供遗留 ``--devices`` 时抛出 ``ValueError``，防止出现
        第二套包发现权威。
        """

        if arguments.get("devices"):
            raise ValueError("--workspace 不可与 legacy --devices 同时使用")
        # ``workspace_root`` 是本进程唯一显式工作区身份，供图与源码共同寻址。
        workspace_root = str(self.source.root)
        # ``device_directory`` 是注册表（Registry）唯一额外 AST 扫描根。
        device_directory = str(self.package_directory)
        arguments["devices"] = [device_directory]
        arguments["_workspace_root"] = workspace_root
        arguments["_community_namespaces"] = {
            device_directory: self.community_namespace
        }
        arguments["workflow_editable_package_root"] = (
            [workspace_root] if self.has_workflow_manifest else None
        )
        self.apply_product_defaults(arguments)
        if workspace_root in sys.path:
            sys.path.remove(workspace_root)
        sys.path.insert(0, workspace_root)

    def apply_product_defaults(self, arguments: dict[str, Any]) -> None:
        """只应用不授权作者模块导入的产品启动默认值。

        参数：``arguments`` 是公共命令行（CLI）的可变参数投影。
        返回：无；只补全运行目录、物理图、配置、桥接器与依赖保障策略。
        异常：默认文件逃逸、缺失或经过符号链接时传播 ``ValueError``；本方法不设置
        ``devices``、工作流源码授权或 ``sys.path``，有限激活仍由注册表快照
        （Registry Snapshot）负责。
        """

        # ``working_dir`` 是可写运行状态根，不与只读工作区语义合并；普通启动
        # 仅省略其 CLI 输入，并从工作区确定性派生隐藏的 ``.unilabos`` 子目录。
        if not arguments.get("working_dir"):
            arguments["working_dir"] = str(self.source.root / ".unilabos")
        graph_argument = arguments.get("graph") or self.default_graph
        if isinstance(graph_argument, str) and graph_argument:
            arguments["graph"] = str(self.resolve_graph(graph_argument))
        config_argument = arguments.get("config") or self.default_config
        if isinstance(config_argument, str) and config_argument:
            arguments["config"] = str(
                self.resolve_workspace_file(config_argument, label="配置文件")
            )
        # argparse 的通用默认值包含遗留云 WebSocket；工作区只有在调用者没有选择
        # 其他桥接组合时才用自己的本地部署默认值替换它。
        if self.default_app_bridges is not None and arguments.get("app_bridges") == [
            "websocket",
            "fastapi",
        ]:
            arguments["app_bridges"] = list(self.default_app_bridges)
        # 依赖策略是已校验工作区计划的内部投影，不再形成第二个公共 CLI 入口。
        arguments["_ensure_dependencies"] = self.ensure_dependencies

    def resolve_graph(self, graph_argument: str) -> Path:
        """解析公共命令行（CLI）的物理图文件参数。

        参数：``graph_argument`` 是绝对路径或相对工作区根的图文件路径。
        返回：存在的规范绝对文件路径。
        异常：相对路径逃逸、包含符号链接、缺失或不是普通文件时抛出
        ``ValueError``。
        """

        return self.resolve_workspace_file(graph_argument, label="物理图文件")

    def resolve_workspace_file(self, file_argument: str, *, label: str) -> Path:
        """在唯一工作区授权边界内解析一个启动文件。

        参数：``file_argument`` 是绝对路径或相对工作区根路径；``label`` 是稳定中文
        文件类型。返回：存在且不经过符号链接的规范绝对文件路径。
        异常：路径越界、缺失或不是普通文件时抛出 ``ValueError``。
        """

        # ``requested_path`` 保留调用者选择的路径身份，供绝对路径边界检查使用。
        requested_path = Path(file_argument).expanduser()
        if requested_path.is_absolute():
            try:
                # ``logical_file`` 把绝对路径重新约束为工作区来源的安全相对路径。
                logical_file = requested_path.relative_to(self.source.root).as_posix()
            except ValueError as error:
                raise ValueError(
                    f"绝对{label}必须位于工作区内: {file_argument}"
                ) from error
            if not self.source.has_file(logical_file):
                raise ValueError(f"工作区内{label}不存在: {file_argument}")
            return self.source.root.joinpath(*Path(logical_file).parts).resolve()
        logical_file = requested_path.as_posix()
        if not self.source.has_file(logical_file):
            raise ValueError(f"工作区内{label}不存在: {file_argument}")
        return self.source.root.joinpath(*Path(logical_file).parts).resolve()


def compile_workspace_startup(source: WorkspaceSource) -> WorkspaceStartupPlan:
    """静态编译启动门禁所需的最小工作区计划。

    参数：``source`` 是公共命令行（CLI）显式授权的工作区来源。
    返回：不导入设备模块、不应用工作流图的不可变启动计划。
    异常：项目元数据、导入包目录或可选 ``package.yaml`` 无效时抛出
    ``ValueError``，不返回部分计划。
    """

    if not isinstance(source, WorkspaceSource):
        raise TypeError("source 必须是 WorkspaceSource")
    # ``project_metadata`` 是所有包调用者共用的唯一 TOML 解析结果。
    project_file_bytes = source.read_bytes("pyproject.toml")
    project_metadata = parse_project_metadata(project_file_bytes)
    # ``import_package`` 是发行身份规范化后的唯一 Python 包身份。
    import_package = project_metadata.normalized_name
    # 下列默认值来自同一项目元数据解析，不建立第二套 TOML 解释权威。
    default_graph = project_metadata.startup_graph
    default_config = project_metadata.startup_config
    default_app_bridges = project_metadata.startup_app_bridges
    ensure_dependencies = project_metadata.startup_ensure_dependencies
    # ``package_directory`` 是注册表（Registry）唯一允许扫描的包目录。
    package_directory = source.root / import_package
    if (
        package_directory.is_symlink()
        or not package_directory.is_dir()
        or package_directory.resolve() != package_directory
        or not package_directory.joinpath("__init__.py").is_file()
    ):
        raise ValueError(
            f"工作区缺少规范 Python 包目录或 __init__.py: {import_package}"
        )
    _validate_registry_python_tree(source, package_directory)

    # ``has_workflow_manifest`` 决定是否授权工作流源码（Workflow Source）发现。
    has_workflow_manifest = source.has_file("package.yaml")
    # ``workflow_source_count`` 证明本次计划确实加载了封闭来源声明。
    workflow_source_count = 0
    # ``workflow_manifest`` 保留本次已验证模型，后续目录编译不得再次解释 YAML。
    workflow_manifest: EditablePackageManifest | None = None
    # ``workflow_manifest_bytes`` 与已解析模型绑定，供完整目录摘要复用同一观察。
    workflow_manifest_bytes: bytes | None = None
    if has_workflow_manifest:
        manifest_snapshot = read_package_root(source.root)
        workflow_manifest_bytes = manifest_snapshot.manifest_bytes
        workflow_manifest = _validate_workflow_manifest(
            manifest_snapshot,
            import_package=import_package,
        )
        workflow_source_count = len(workflow_manifest.workflows)
    startup_plan = WorkspaceStartupPlan(
        source=source,
        project_metadata=project_metadata,
        distribution_name=project_metadata.name,
        import_package=import_package,
        package_directory=package_directory,
        community_namespace=f"community.{import_package}",
        has_workflow_manifest=has_workflow_manifest,
        workflow_source_count=workflow_source_count,
        workflow_manifest=workflow_manifest,
        default_graph=default_graph,
        default_config=default_config,
        default_app_bridges=default_app_bridges,
        ensure_dependencies=ensure_dependencies,
        project_file_bytes=project_file_bytes,
        workflow_manifest_bytes=workflow_manifest_bytes,
    )
    # 启动计划在返回前验证其默认文件；显式 CLI 覆盖不能掩盖损坏的工作区声明。
    if default_graph is not None:
        startup_plan.resolve_graph(default_graph)
    if default_config is not None:
        startup_plan.resolve_workspace_file(default_config, label="配置文件")
    return startup_plan


def prepare_workspace_startup(
    arguments: dict[str, Any],
) -> WorkspaceStartupPlan | None:
    """从公共命令行（CLI）参数准备并应用可选工作区。

    参数：``arguments`` 是 argparse 生成的可变参数字典。
    返回：配置了 ``--workspace`` 时返回已应用计划，否则返回 ``None``。
    异常：参数形状或工作区内容无效时抛出 ``TypeError``/``ValueError``；不会导入
    设备实现、应用工作流（Workflow）候选或创建工作流任务（WorkflowTask）。
    """

    if not isinstance(arguments, dict):
        raise TypeError("启动参数必须是 dict")
    workspace_argument = arguments.get("workspace")
    if workspace_argument is None:
        return None
    if not isinstance(workspace_argument, str) or not workspace_argument.strip():
        raise ValueError("--workspace 必须是非空目录路径")
    # ``workspace_source`` 是本轮启动唯一被授权的本地包来源。
    workspace_source = WorkspaceSource(workspace_argument)
    # ``startup_plan`` 是静态校验后的唯一启动投影，不包含工作流任务（WorkflowTask）。
    startup_plan = compile_workspace_startup(workspace_source)
    startup_plan.apply(arguments)
    return startup_plan


def project_catalog_startup_plan(
    source: WorkspaceSource,
    catalog: Any,
) -> WorkspaceStartupPlan:
    """从已编译包目录（PackageCatalog）投影只读产品启动计划。

    参数：``source`` 是目录编译使用的显式来源；``catalog`` 是同一次完整静态编译
    已冻结的包目录（PackageCatalog）。
    返回：复用目录身份和工作流定义计数、但不重新读取 ``package.yaml`` 的启动计划。
    异常：来源、发行身份、导入包、命名空间或定义集合与当前项目元数据不一致时
    抛出 ``TypeError``/``ValueError``；不会应用工作流图或创建工作流任务
    （WorkflowTask）。
    """

    if not isinstance(source, WorkspaceSource):
        raise TypeError("source 必须是 WorkspaceSource")
    # ``project_metadata`` 只读取产品启动默认值；工作流清单已经由包目录冻结。
    project_file_bytes = source.read_bytes("pyproject.toml")
    project_metadata = parse_project_metadata(project_file_bytes)
    catalog_distribution = getattr(catalog, "distribution", None)
    catalog_import_package = getattr(catalog, "import_package", None)
    catalog_namespace = getattr(catalog, "namespace", None)
    catalog_definitions = getattr(catalog, "definitions", None)
    catalog_workflows = getattr(catalog_definitions, "workflows", None)
    if (
        getattr(catalog_distribution, "name", None) != project_metadata.name
        or catalog_import_package != project_metadata.normalized_name
        or catalog_namespace != f"community.{project_metadata.normalized_name}"
        or not isinstance(catalog_workflows, tuple)
    ):
        raise ValueError("包目录与当前工作区项目身份不一致")

    # ``package_directory`` 只供同代模型资产和产品默认值解析，不重新扫描动作合同。
    package_directory = source.root / project_metadata.normalized_name
    if (
        package_directory.is_symlink()
        or not package_directory.is_dir()
        or package_directory.resolve() != package_directory
        or not package_directory.joinpath("__init__.py").is_file()
    ):
        raise ValueError("包目录对应的规范 Python 包目录不存在")
    startup_plan = WorkspaceStartupPlan(
        source=source,
        project_metadata=project_metadata,
        distribution_name=project_metadata.name,
        import_package=project_metadata.normalized_name,
        package_directory=package_directory,
        community_namespace=f"community.{project_metadata.normalized_name}",
        has_workflow_manifest=source.has_file("package.yaml"),
        workflow_source_count=len(catalog_workflows),
        workflow_manifest=None,
        default_graph=project_metadata.startup_graph,
        default_config=project_metadata.startup_config,
        default_app_bridges=project_metadata.startup_app_bridges,
        ensure_dependencies=project_metadata.startup_ensure_dependencies,
        project_file_bytes=project_file_bytes,
        workflow_manifest_bytes=None,
    )
    if startup_plan.default_graph is not None:
        startup_plan.resolve_graph(startup_plan.default_graph)
    if startup_plan.default_config is not None:
        startup_plan.resolve_workspace_file(
            startup_plan.default_config,
            label="配置文件",
        )
    return startup_plan


def _validate_workflow_manifest(
    root_snapshot: PackageRootSnapshot,
    *,
    import_package: str,
) -> EditablePackageManifest:
    """验证工作流源码（Workflow Source）声明与项目包身份一致。

    参数：``root_snapshot`` 是已经一次读取清单字节并固定目录身份的工作区快照；
    ``import_package`` 是项目规范导入身份。
    返回：完成身份与源码路径校验的封闭可编辑包（Editable Package）清单。
    异常：YAML、身份或源码路径无效时转换为不泄漏内容的 ``ValueError``。
    """

    try:
        # ``manifest`` 拥有包身份与工作流源码（Workflow Source）声明顺序。
        manifest = parse_editable_package_manifest(root_snapshot.manifest_bytes)
        if manifest.package_id != import_package:
            raise ValueError("package.yaml 包身份与 pyproject.toml 不一致")
        # ``source_snapshot`` 用同一目录快照验证全部声明源码，不再次读取清单。
        source_snapshot = validate_declared_sources(
            root_snapshot,
            package_id=manifest.package_id,
            relative_paths=(workflow.relative_path for workflow in manifest.workflows),
        )
    except (SourceManifestError, SourceWorkspaceError) as error:
        raise ValueError("工作区 package.yaml 或工作流源码声明无效") from error
    if source_snapshot.package_root.name != manifest.package_id:
        raise ValueError("工作区工作流源码包身份不一致")
    return manifest


def _validate_registry_python_tree(
    source: WorkspaceSource,
    package_directory: Path,
) -> None:
    """验证注册表（Registry）扫描树中的 Python 文件没有符号链接路径。

    参数：``source`` 是工作区授权来源；``package_directory`` 是将交给注册表扫描
    的本地包目录。
    返回：无；所有 Python 文件和可递归目录均位于同一工作区授权边界时正常结束。
    异常：目录不可读，或 Python 文件/递归目录经过符号链接时抛出 ``ValueError``。
    """

    # ``pending_directories`` 保存尚未检查的真实包目录，拒绝跟随任何符号链接目录。
    pending_directories = [package_directory]
    while pending_directories:
        # ``current_directory`` 是本轮读取目录项的真实注册表扫描目录。
        current_directory = pending_directories.pop()
        try:
            # ``entries`` 使用稳定排序，保证错误定位和测试结果可重复。
            entries = sorted(current_directory.iterdir())
        except OSError as error:
            raise ValueError(
                f"注册表 Python 扫描目录不可访问: {current_directory}"
            ) from error
        for entry in entries:
            # ``entry`` 是包目录中可能被注册表递归访问的一个文件系统对象。
            if entry.is_symlink():
                if entry.suffix == ".py" or (
                    entry.is_dir() and not entry.name.startswith(("__", "."))
                ):
                    raise ValueError(f"注册表 Python 扫描路径不得经过符号链接: {entry}")
                continue
            if entry.is_dir():
                if not entry.name.startswith(("__", ".")):
                    pending_directories.append(entry)
                continue
            if entry.suffix != ".py":
                continue
            # ``logical_python`` 是 Python 文件相对工作区根的安全逻辑身份。
            logical_python = entry.relative_to(source.root).as_posix()
            if not source.has_file(logical_python):
                raise ValueError(f"注册表 Python 文件不存在: {entry}")


def compile_package_source(
    source: WorkspaceSource,
    *,
    startup_plan: WorkspaceStartupPlan | None = None,
) -> PackageCatalog:
    """解析工作区启动输入后调用 Python 包目录（PackageCatalog）纯编译器。

    参数：``source`` 是显式授权的工作区来源；``startup_plan`` 是可选的同来源
    已冻结启动输入，产品启动传入后不会再次读取清单。
    返回：完整校验且不可变的包目录（PackageCatalog）。
    异常：来源发现失败或静态定义无效时保持既有 ``PackageCompileError`` 行为。
    """

    if not isinstance(source, WorkspaceSource):
        raise TypeError("source 必须是 WorkspaceSource")
    # ``resolved_startup_plan`` 是工作区运行时（Workspace Runtime）唯一允许解析的
    # 启动输入；失败保持结构化诊断，不向调用者泄漏文件系统实现细节。
    try:
        resolved_startup_plan = startup_plan or compile_workspace_startup(source)
    except (TypeError, ValueError) as error:
        raise PackageCompileError(
            (
                PackageDiagnostic(
                    code="package_source_invalid",
                    message="软件包来源或项目元数据无效",
                ),
            )
        ) from error
    return compile_python_package_source(
        source,
        startup_plan=resolved_startup_plan,
    )


__all__ = [
    "WorkspaceStartupPlan",
    "compile_package_source",
    "compile_workspace_startup",
    "normalize_distribution_name",
    "prepare_workspace_startup",
]
