"""显式软件包来源到完整包目录（PackageCatalog）的深模块。"""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path
from typing import Any, Protocol

from ...model import (
    PackageAsset,
    PackageCatalog,
    PackageCompileError,
    PackageDefinitionCatalog,
    PackageDiagnostic,
    PackageDistributionIdentity,
)
from ...sources import WorkspaceSource
from .registry import compile_registry_definitions
from .workflow import compile_workflow_definitions

_IGNORED_PARTS = {"__pycache__", ".git", ".pytest_cache", ".mypy_cache"}
_IGNORED_AGENT_SKILL_PATHS = {
    (".claude", "skills"),
    (".codex", "skills"),
}
_IGNORED_SUFFIXES = {".pyc", ".pyo"}


class PythonPackageProject(Protocol):
    """Python 包目录（PackageCatalog）编译需要的发行项目只读合同。"""

    name: str
    normalized_name: str
    version: str
    description: str
    license: str
    homepage: str
    requires_python: str
    dependencies: tuple[str, ...]


class PythonPackageCompilationPlan(Protocol):
    """根编排层传入 Python 静态编译器的封闭输入合同。"""

    source: WorkspaceSource
    package_directory: Path
    community_namespace: str
    import_package: str
    workflow_manifest: Any
    project_metadata: PythonPackageProject
    project_file_bytes: bytes
    has_workflow_manifest: bool
    workflow_manifest_bytes: bytes | None


def _path_name(path: Path) -> str:
    """读取工作区目录成员的稳定文件名排序键。

    参数：``path`` 是当前目录的一项直接成员。
    返回：不含父目录的文件名。
    异常：无。
    """

    return path.name


def compile_package_source(
    source: WorkspaceSource,
    *,
    startup_plan: PythonPackageCompilationPlan,
) -> PackageCatalog:
    """完整静态编译一个显式工作区软件包来源。

    参数：``source`` 是唯一授权的工作区来源 Adapter；``startup_plan`` 是根编排层
    提供的同来源冻结输入，编译器不得再次读取或解释项目/工作流清单。
    返回：包含全部设备、资源、显式工作流源码（Workflow Source）和资产的不可变
    包目录（PackageCatalog）。
    异常：来源、语法、身份、动作合同（Action Contract）或资产不合法时抛出
    ``PackageCompileError``；函数不修改 ``sys.path``、不导入作者模块且不发布部分状态。
    """

    if not isinstance(source, WorkspaceSource):
        raise TypeError("source 必须是 WorkspaceSource")
    try:
        # ``resolved_startup_plan`` 是高层完成来源发现后交入的完整固定输入。
        resolved_startup_plan = startup_plan
        if resolved_startup_plan.source.root != source.root:
            raise ValueError("startup_plan 必须属于当前显式工作区来源")
        # ``logical_files`` 是本次输入代允许进入摘要和定义编译的完整相对路径集。
        logical_files = _collect_package_files(
            source,
            resolved_startup_plan.package_directory,
        )
    except (TypeError, ValueError) as error:
        raise PackageCompileError(
            (
                PackageDiagnostic(
                    code="package_source_invalid",
                    message="软件包来源或项目元数据无效",
                ),
            )
        ) from error

    # ``file_contents`` 是一次编译观察到的完整文件字节，不从磁盘二次猜测内容。
    file_contents = {
        logical_path: source.read_bytes(logical_path) for logical_path in logical_files
    }
    # ``python_files`` 只含通过 UTF-8 和语法门禁的规范绝对源码路径。
    python_files = _validate_python_sources(
        source=source,
        file_contents=file_contents,
    )
    # ``content_hashes`` 以工作区逻辑路径为键，绑定本次固定文件观察的内容身份。
    content_hashes = {
        logical_path: _sha256(content)
        for logical_path, content in file_contents.items()
    }
    # ``devices`` 与 ``resources`` 是同一输入代产生、尚未发布的完整静态定义。
    devices, resources = compile_registry_definitions(
        workspace_root=source.root,
        namespace=resolved_startup_plan.community_namespace,
        python_files=python_files,
        content_hashes=content_hashes,
    )
    # ``workflows`` 只包含清单显式授权且 UUID 已与源码核对的工作流定义。
    workflows = compile_workflow_definitions(
        source=source,
        import_package=resolved_startup_plan.import_package,
        namespace=resolved_startup_plan.community_namespace,
        manifest=resolved_startup_plan.workflow_manifest,
        content_hashes=content_hashes,
    )
    # ``all_fqids`` 汇总跨种类规范定义身份，任何重复都会使整包关闭式失败。
    all_fqids = [item.fqid for item in (*devices, *resources, *workflows)]
    if len(set(all_fqids)) != len(all_fqids):
        raise PackageCompileError(
            (
                PackageDiagnostic(
                    code="duplicate_definition",
                    message="不同定义种类共享同一全限定身份",
                ),
            )
        )
    # ``assets`` 是非 Python 文件的不可变目录证据，逻辑路径和摘要共同标识内容。
    assets = tuple(
        PackageAsset(
            logical_path=logical_path,
            digest=content_hashes[logical_path],
            size=len(content),
        )
        for logical_path, content in sorted(file_contents.items())
        if not logical_path.endswith(".py")
    )
    # ``project`` 是工作区启动和目录编译共用的项目元数据事实。
    project = resolved_startup_plan.project_metadata
    # ``distribution`` 冻结发行包身份，不包含工作区绝对路径或运行时安装状态。
    distribution = PackageDistributionIdentity(
        name=project.name,
        normalized_name=project.normalized_name,
        version=project.version,
        description=project.description,
        license=project.license,
        homepage=project.homepage,
        requires_python=project.requires_python,
        dependencies=project.dependencies,
    )
    # ``content_items`` 是完整来源内容摘要的输入闭包，清单原始字节只读取一次。
    content_items = [
        ("pyproject.toml", resolved_startup_plan.project_file_bytes),
        *(
            [
                (
                    "package.yaml",
                    _required_workflow_manifest_bytes(resolved_startup_plan),
                )
            ]
            if resolved_startup_plan.has_workflow_manifest
            else []
        ),
        *sorted(file_contents.items()),
    ]
    return PackageCatalog.create(
        distribution=distribution,
        import_package=resolved_startup_plan.import_package,
        namespace=resolved_startup_plan.community_namespace,
        definitions=PackageDefinitionCatalog(
            devices=devices,
            resources=resources,
            workflows=workflows,
        ),
        assets=assets,
        content_digest=_content_digest(content_items),
    )


def _required_workflow_manifest_bytes(
    startup_plan: PythonPackageCompilationPlan,
) -> bytes:
    """读取已冻结启动计划拥有的工作流源码清单原始字节。

    参数：``startup_plan`` 是完整目录编译正在复用的同来源启动计划。
    返回：与 ``workflow_manifest`` 同次读取的 ``package.yaml`` 原始字节。
    异常：计划声明存在清单却没有固定字节时抛出 ``ValueError``，禁止为摘要再次
    读取磁盘并形成混合输入代。
    """

    # ``manifest_bytes`` 与解析后的工作流清单属于同一固定输入代。
    manifest_bytes = startup_plan.workflow_manifest_bytes
    if manifest_bytes is None:
        raise ValueError("工作流源码清单缺少同代固定原始字节")
    return manifest_bytes


def _collect_package_files(
    source: WorkspaceSource,
    package_directory: Path,
) -> tuple[str, ...]:
    """稳定列出导入包内全部安全普通文件。

    参数：``source`` 是授权来源；``package_directory`` 是唯一导入包目录。
    返回：排除缓存和字节码后的 POSIX 逻辑路径集合。
    异常：遇到符号链接、不可读目录或越界对象时抛出 ``ValueError``。
    """

    # ``pending_directories`` 只遍历已验证导入包边界，不递归整个工作区。
    pending_directories = [package_directory]
    # ``logical_files`` 保存相对授权根的路径身份，禁止绝对路径进入目录摘要。
    logical_files: list[str] = []
    while pending_directories:
        # ``current_directory`` 始终来自导入包边界内的待访问集合。
        current_directory = pending_directories.pop()
        try:
            # ``entries`` 先按文件名稳定排序，消除文件系统遍历顺序差异。
            entries = sorted(current_directory.iterdir(), key=_path_name)
        except OSError as error:
            raise ValueError("软件包工作区不可读取") from error
        for entry in entries:
            if entry.name in _IGNORED_PARTS:
                continue
            relative_entry_parts = entry.relative_to(package_directory).parts
            if any(
                relative_entry_parts[: len(ignored_path)] == ignored_path
                for ignored_path in _IGNORED_AGENT_SKILL_PATHS
            ):
                continue
            if entry.is_symlink():
                raise ValueError("软件包工作区不得包含符号链接")
            if entry.is_dir():
                pending_directories.append(entry)
                continue
            if not entry.is_file() or entry.suffix in _IGNORED_SUFFIXES:
                continue
            # ``logical_path`` 是资产、诊断和源码摘要共同使用的包内稳定路径身份。
            logical_path = entry.relative_to(source.root).as_posix()
            if not source.has_file(logical_path):
                raise ValueError("软件包文件不在授权来源内")
            logical_files.append(logical_path)
    return tuple(sorted(logical_files))


def _validate_python_sources(
    *,
    source: WorkspaceSource,
    file_contents: dict[str, bytes],
) -> tuple[Path, ...]:
    """在注册表扫描前完整验证所有 Python 文件的编码和语法。

    参数：``source`` 是授权工作区；``file_contents`` 是本次固定文件观察。
    返回：可交给产品 AST 注册表解析器的规范绝对路径。
    异常：任一文件编码或语法无效时抛出单个 ``PackageCompileError``，不会继续投影。
    """

    # ``python_files`` 收集可交给 AST 编译器的规范源码路径，不代表已导入模块。
    python_files: list[Path] = []
    # ``diagnostics`` 聚合整包语法门禁结果，禁止先发布部分有效源码。
    diagnostics: list[PackageDiagnostic] = []
    for logical_path, content in sorted(file_contents.items()):
        if not logical_path.endswith(".py"):
            continue
        try:
            # ``source_text`` 仅供静态语法校验，不执行也不导入作者代码。
            source_text = content.decode("utf-8")
            ast.parse(source_text, filename=logical_path)
        except UnicodeError:
            diagnostics.append(
                PackageDiagnostic(
                    code="python_encoding_error",
                    message="Python 源码必须使用 UTF-8",
                    path=logical_path,
                )
            )
            continue
        except SyntaxError as error:
            diagnostics.append(
                PackageDiagnostic(
                    code="python_syntax_error",
                    message="Python 源码语法无效",
                    path=logical_path,
                    line=error.lineno,
                )
            )
            continue
        python_files.append(source.root / logical_path)
    if diagnostics:
        raise PackageCompileError(tuple(diagnostics))
    return tuple(python_files)


def _sha256(content: bytes) -> str:
    """计算一个文件内容的稳定 SHA-256 身份。

    参数：``content`` 是本次固定观察到的文件字节。
    返回：带 ``sha256:`` 前缀的小写十六进制摘要。
    异常：无。
    """

    return "sha256:" + hashlib.sha256(content).hexdigest()


def _content_digest(items: list[tuple[str, bytes]]) -> str:
    """计算带路径和长度分隔的软件包完整内容摘要。

    参数：``items`` 是逻辑路径与内容字节对；路径必须在调用前稳定排序或唯一。
    返回：不依赖绝对路径和修改时间的 SHA-256 摘要。
    异常：路径不是 UTF-8 可编码文本时传播编码异常。
    """

    # ``digest`` 按路径和内容长度分隔累计，避免不同边界组合产生同一输入序列。
    digest = hashlib.sha256()
    for logical_path, content in sorted(items):
        # ``path_bytes`` 编码稳定逻辑路径身份，绝对工作区路径从不进入摘要。
        path_bytes = logical_path.encode("utf-8")
        digest.update(len(path_bytes).to_bytes(8, "big"))
        digest.update(path_bytes)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return "sha256:" + digest.hexdigest()


__all__ = ["compile_package_source"]
