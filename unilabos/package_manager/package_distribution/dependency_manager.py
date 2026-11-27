"""显式软件包依赖来源解析、完整校验与代际变更编排。"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from ..package_catalog import (
    PackageCatalog,
    WorkspaceSource,
    compile_registry_snapshot,
)
from .lock_codec import load_dependency_state
from .models import (
    LockedPackage,
    PackageDependencyError,
    PackageDependencyLock,
    ResolvedPackageSource,
)
from .transaction import (
    publish_dependency_state,
    serialized_dependency_mutation,
)

CatalogCompiler = Callable[[WorkspaceSource], PackageCatalog]


class PackageDependencyManager:
    """在一个小 Interface 后完成依赖解析、全局校验和双文件事务发布。"""

    def __init__(
        self,
        workspace: str | Path,
        *,
        compile_catalog: CatalogCompiler,
    ) -> None:
        """固定主工作区并恢复其当前显式依赖代际。

        参数：``workspace`` 是当前可编辑主工作区根；``compile_catalog`` 是组合根
        显式注入、供全部软件包来源共用的目录编译器。
        返回：无；构造函数只验证根目录，不写文件或发现环境软件包。
        异常：工作区缺失、包含符号链接或不可访问时传播 ``WorkspaceSource`` 的
        ``ValueError``。
        """

        # ``_workspace`` 是全部依赖声明、锁和相对来源解析的唯一主工作区权威。
        self._workspace = WorkspaceSource(workspace)
        # ``_compile_catalog`` 是本管理器全部来源必须共用的目录编译 Interface。
        self._compile_catalog = compile_catalog

    @serialized_dependency_mutation
    def add(self, source: str | Path) -> PackageDependencyLock:
        """新增一个显式外部软件包并发布新的依赖锁。

        参数：``source`` 是相对主工作区或绝对的外部软件包工作区路径。
        返回：成功发布后的完整软件包依赖锁（Package Dependency Lock）。
        异常：来源无效、身份已存在、完整静态编译或聚合注册表校验失败时抛出；
        所有验证在写文件前完成，失败保持原声明和锁不变。
        """

        # 当前声明和锁共同定义变更前不可分割的依赖代际。
        declarations, current_lock = load_dependency_state(self._workspace.root)
        # ``source_path`` 是安全规范绝对来源；``portable_source`` 是待写入锁的相对来源。
        source_path, portable_source = resolve_dependency_source(
            self._workspace.root,
            source,
        )
        # ``catalog`` 是新来源在任何文件写入前完整编译的包目录（PackageCatalog）。
        catalog = compile_dependency_catalog(
            WorkspaceSource(source_path),
            compile_catalog=self._compile_catalog,
        )
        if any(
            item.normalized_name == catalog.distribution.normalized_name
            for item in current_lock.packages
        ):
            raise PackageDependencyError(
                f"软件包依赖已存在，请使用 update: {catalog.distribution.name}"
            )
        # ``new_entry`` 是候选来源对应的稳定发行身份和目录摘要事实。
        new_entry = LockedPackage.from_catalog(
            catalog=catalog,
            source=portable_source,
        )
        # ``next_lock`` 是加入候选后、尚未发布的完整依赖代际。
        next_lock = PackageDependencyLock(packages=(*current_lock.packages, new_entry))
        # ``next_declarations`` 与候选锁保持一一对应的显式来源集合。
        next_declarations = {
            **declarations,
            new_entry.normalized_name: (
                new_entry.distribution_name,
                new_entry.source,
            ),
        }
        validate_complete_generation(
            workspace=self._workspace,
            dependency_catalogs=tuple(
                catalog_for_entry(
                    workspace_root=self._workspace.root,
                    entry=item,
                    verify_lock=item.normalized_name != new_entry.normalized_name,
                    compile_catalog=self._compile_catalog,
                )
                if item.normalized_name != new_entry.normalized_name
                else catalog
                for item in next_lock.packages
            ),
            compile_catalog=self._compile_catalog,
        )
        publish_dependency_state(
            workspace_root=self._workspace.root,
            declarations=next_declarations,
            dependency_lock=next_lock,
        )
        return next_lock

    @serialized_dependency_mutation
    def update(
        self,
        identity: str,
        source: str | Path | None = None,
    ) -> PackageDependencyLock:
        """重新解析并锁定一个既有显式外部软件包。

        参数：``identity`` 是发行名、规范化发行名或社区命名空间；``source`` 是
        可选新来源，省略时继续使用既有显式路径。
        返回：内容摘要已推进、依赖身份不变的完整软件包依赖锁。
        异常：目标不存在、新来源发行身份变化、任一其他依赖漂移或聚合校验失败
        时关闭式抛出；验证失败不修改声明和锁。
        """

        # ``declarations`` 与 ``current_lock`` 共同固定更新前完整依赖代际。
        declarations, current_lock = load_dependency_state(self._workspace.root)
        # ``current_entry`` 是用户身份在当前完整依赖代际中的唯一匹配条目。
        current_entry = find_locked_package(current_lock, identity)
        # ``selected_source`` 保留显式新来源或回用锁中的可移植既有来源。
        selected_source = source if source is not None else current_entry.source
        # ``source_path`` 是重新编译根；``portable_source`` 是候选锁保存的来源身份。
        source_path, portable_source = resolve_dependency_source(
            self._workspace.root,
            selected_source,
        )
        # ``catalog`` 是候选更新来源重新完整静态编译得到的包目录。
        catalog = compile_dependency_catalog(
            WorkspaceSource(source_path),
            compile_catalog=self._compile_catalog,
        )
        # ``next_entry`` 是更新后候选摘要，不得改变发行规范身份。
        next_entry = LockedPackage.from_catalog(
            catalog=catalog,
            source=portable_source,
        )
        if next_entry.normalized_name != current_entry.normalized_name:
            raise PackageDependencyError(
                "软件包 update 不得改变发行身份: "
                f"{current_entry.distribution_name} -> {next_entry.distribution_name}"
            )
        # ``next_lock`` 只替换目标条目，其他依赖身份和摘要必须重放验证。
        next_lock = PackageDependencyLock(
            packages=tuple(
                next_entry
                if item.normalized_name == current_entry.normalized_name
                else item
                for item in current_lock.packages
            )
        )
        # ``next_declarations`` 只推进目标来源，并与候选锁保持完整一一对应。
        next_declarations = {
            **declarations,
            next_entry.normalized_name: (
                next_entry.distribution_name,
                next_entry.source,
            ),
        }
        # ``dependency_catalogs`` 是更新候选与全部未漂移依赖组成的完整集合。
        dependency_catalogs = tuple(
            catalog
            if item.normalized_name == next_entry.normalized_name
            else catalog_for_entry(
                workspace_root=self._workspace.root,
                entry=item,
                verify_lock=True,
                compile_catalog=self._compile_catalog,
            )
            for item in next_lock.packages
        )
        validate_complete_generation(
            workspace=self._workspace,
            dependency_catalogs=dependency_catalogs,
            compile_catalog=self._compile_catalog,
        )
        publish_dependency_state(
            workspace_root=self._workspace.root,
            declarations=next_declarations,
            dependency_lock=next_lock,
        )
        return next_lock

    @serialized_dependency_mutation
    def remove(self, identity: str) -> PackageDependencyLock:
        """删除一个显式外部软件包并重新验证剩余完整代际。

        参数：``identity`` 是发行名、规范化发行名或社区命名空间。
        返回：不再包含目标、但仍显式保存为空也合法的完整依赖锁。
        异常：目标不存在、其他依赖已经漂移或剩余聚合校验失败时关闭式抛出；
        失败不会切换到 ambient site-packages。
        """

        # ``declarations`` 与 ``current_lock`` 共同固定删除前完整依赖代际。
        declarations, current_lock = load_dependency_state(self._workspace.root)
        # ``current_entry`` 是删除命令唯一允许移除的既有锁条目。
        current_entry = find_locked_package(current_lock, identity)
        # ``next_lock`` 是排除目标、保留其余条目原身份摘要的完整候选锁。
        next_lock = PackageDependencyLock(
            packages=tuple(
                item
                for item in current_lock.packages
                if item.normalized_name != current_entry.normalized_name
            )
        )
        # ``next_declarations`` 同步删除对应显式来源，但即使为空仍会持久化。
        next_declarations = {
            key: value
            for key, value in declarations.items()
            if key != current_entry.normalized_name
        }
        # ``remaining_catalogs`` 证明删除目标后其余依赖仍与锁完全一致。
        remaining_catalogs = tuple(
            catalog_for_entry(
                workspace_root=self._workspace.root,
                entry=item,
                verify_lock=True,
                compile_catalog=self._compile_catalog,
            )
            for item in next_lock.packages
        )
        validate_complete_generation(
            workspace=self._workspace,
            dependency_catalogs=remaining_catalogs,
            compile_catalog=self._compile_catalog,
        )
        publish_dependency_state(
            workspace_root=self._workspace.root,
            declarations=next_declarations,
            dependency_lock=next_lock,
        )
        return next_lock


def load_locked_package_catalogs(
    workspace: str | Path,
    *,
    compile_catalog: CatalogCompiler,
) -> tuple[PackageCatalog, ...]:
    """只从显式声明和锁文件加载外部包目录（PackageCatalog）。

    参数：``workspace`` 是主工作区根；``compile_catalog`` 是显式目录编译器；
    函数从不扫描 ``sys.path`` 或 ambient site-packages。
    返回：按命名空间排序、重新完整编译且摘要与锁一致的包目录元组。
    异常：声明/锁缺一、身份或摘要漂移、来源无效、跨包冲突时抛出
    ``PackageDependencyError``，不返回部分集合。
    """

    # ``workspace_source`` 固定依赖来源相对路径与主包聚合校验的共同根。
    workspace_source = WorkspaceSource(workspace)
    # ``package_sources`` 保留每个依赖目录对应的显式来源，正式查询接口只返回目录。
    package_sources = load_locked_package_sources(
        workspace_source.root,
        compile_catalog=compile_catalog,
    )
    # ``catalogs`` 是正式查询接口不暴露物理来源的包目录（PackageCatalog）结果。
    catalogs = tuple(item.catalog for item in package_sources)
    validate_complete_generation(
        workspace=workspace_source,
        dependency_catalogs=catalogs,
        compile_catalog=compile_catalog,
    )
    return tuple(sorted(catalogs, key=catalog_namespace))


def load_locked_package_sources(
    workspace: str | Path,
    *,
    compile_catalog: CatalogCompiler,
) -> tuple[ResolvedPackageSource, ...]:
    """一次编译并返回显式锁定外部包的来源/目录配对。

    参数：``workspace`` 是主工作区根；``compile_catalog`` 是显式目录编译器；
    只读取成对依赖声明与锁定的 workspace 来源，绝不发现 ambient site-packages。
    返回：按包命名空间稳定排序、摘要与锁完全一致的 ``ResolvedPackageSource``
    元组；主包不在结果中，由完整候选代组合者复用其已有编译结果。
    异常：声明、锁、来源、摘要或静态编译无效时抛出
    ``PackageDependencyError``，不返回部分集合；本函数不重复编译主包。
    """

    # ``workspace_source`` 固定全部相对外部来源解析使用的主工作区根。
    workspace_source = WorkspaceSource(workspace)
    # ``dependency_lock`` 是本次加载唯一授权的完整外部包代际；声明仅用于成对校验。
    _declarations, dependency_lock = load_dependency_state(workspace_source.root)
    # ``package_sources`` 让后续有限运行激活不必从目录字段反推物理路径。
    package_sources: list[ResolvedPackageSource] = []
    for entry in dependency_lock.packages:
        # ``source_path`` 是条目的安全绝对来源；相对身份已由锁校验，不再另行使用。
        source_path, _portable_source = resolve_dependency_source(
            workspace_source.root,
            entry.source,
        )
        # ``catalog`` 是从显式来源重编译并与锁完整核对的包目录。
        catalog = catalog_for_entry(
            workspace_root=workspace_source.root,
            entry=entry,
            verify_lock=True,
            compile_catalog=compile_catalog,
        )
        package_sources.append(
            ResolvedPackageSource(
                source=WorkspaceSource(source_path),
                catalog=catalog,
            )
        )
    return tuple(sorted(package_sources, key=package_source_namespace))


def resolve_dependency_source(
    workspace_root: Path,
    source: str | Path,
) -> tuple[Path, str]:
    """解析外部软件包来源并生成可移植声明路径。

    参数：``workspace_root`` 是主工作区；``source`` 是绝对路径或相对主工作区
    的路径。
    返回：经过 ``WorkspaceSource`` 校验的规范绝对路径及 POSIX 相对声明。
    异常：来源为空、指回主工作区或不安全时抛出 ``PackageDependencyError``。
    """

    if not str(source).strip():
        raise PackageDependencyError("软件包依赖来源不能为空")
    # ``selected`` 是调用者输入按主工作区解析前的路径候选。
    selected = Path(source).expanduser()
    if not selected.is_absolute():
        selected = workspace_root / selected
    try:
        # ``source_root`` 是经过来源安全校验的规范外部工作区根。
        source_root = WorkspaceSource(selected).root
    except ValueError as error:
        raise PackageDependencyError("软件包依赖来源不是安全工作区") from error
    if source_root == workspace_root:
        raise PackageDependencyError("主工作区不能依赖自身")
    # ``portable_source`` 保证锁文件跨主工作区绝对位置移动后仍可解析。
    portable_source = Path(os.path.relpath(source_root, workspace_root)).as_posix()
    return source_root, portable_source


def find_locked_package(
    dependency_lock: PackageDependencyLock,
    identity: str,
) -> LockedPackage:
    """按三种稳定外部身份定位唯一锁条目。

    参数：``dependency_lock`` 是当前完整锁；``identity`` 是原发行名、规范化发行
    名或社区命名空间。
    返回：唯一匹配的既有锁条目。
    异常：身份为空、不存在或因损坏数据出现多重匹配时抛出
    ``PackageDependencyError``。
    """

    # ``normalized_identity`` 保留 CLI 允许的三类稳定身份之一并拒绝空白。
    normalized_identity = identity.strip() if isinstance(identity, str) else ""
    if not normalized_identity:
        raise PackageDependencyError("软件包依赖身份不能为空")
    # ``matches`` 是锁中与用户输入任一受支持身份完全相同的条目集合。
    matches = tuple(
        item
        for item in dependency_lock.packages
        if normalized_identity
        in {
            item.distribution_name,
            item.normalized_name,
            item.namespace,
        }
    )
    if len(matches) != 1:
        raise PackageDependencyError(
            f"软件包依赖不存在或身份不唯一: {normalized_identity}"
        )
    return matches[0]


def catalog_for_entry(
    *,
    workspace_root: Path,
    entry: LockedPackage,
    verify_lock: bool,
    compile_catalog: CatalogCompiler,
) -> PackageCatalog:
    """重新编译一项锁定来源并按需核对全部身份摘要。

    参数：``workspace_root`` 是主工作区；``entry`` 是锁条目；``verify_lock``
    决定是否要求重编译目录与现有锁完全一致；``compile_catalog`` 是显式编译器。
    返回：只从显式路径观察得到的包目录（PackageCatalog）。
    异常：来源越界、编译失败或任一锁字段漂移时抛出
    ``PackageDependencyError``。
    """

    # ``source_path`` 用于重编译；``portable_source`` 用于逐字段重建锁身份。
    source_path, portable_source = resolve_dependency_source(
        workspace_root,
        entry.source,
    )
    # ``catalog`` 是锁定来源当前磁盘内容重新完整编译的包目录。
    catalog = compile_dependency_catalog(
        WorkspaceSource(source_path),
        compile_catalog=compile_catalog,
    )
    # ``rebuilt`` 把当前目录投影回锁模型，供逐字段确定性比较。
    rebuilt = LockedPackage.from_catalog(catalog=catalog, source=portable_source)
    if verify_lock and rebuilt != entry:
        raise PackageDependencyError(
            f"软件包依赖内容与锁不一致，请先 update: {entry.distribution_name}"
        )
    return catalog


def validate_complete_generation(
    *,
    workspace: WorkspaceSource,
    dependency_catalogs: tuple[PackageCatalog, ...],
    compile_catalog: CatalogCompiler,
) -> None:
    """完整校验主包与全部依赖的聚合注册表代际。

    参数：``workspace`` 是主工作区来源；``dependency_catalogs`` 是候选依赖完整
    目录集合；``compile_catalog`` 是整个候选代共享的显式目录编译器。
    返回：无；全部规范身份和别名关系合法时完成。
    异常：主包编译或跨包注册表冲突时传播原始关闭式异常；调用者尚未写文件。
    """

    # ``root_catalog`` 让外部定义不能与当前产品工作区共享规范命名空间。
    root_catalog = compile_dependency_catalog(
        workspace,
        compile_catalog=compile_catalog,
    )
    try:
        compile_registry_snapshot((root_catalog, *dependency_catalogs))
    except (TypeError, ValueError, RuntimeError) as error:
        raise PackageDependencyError("软件包依赖聚合注册表校验失败") from error


def compile_dependency_catalog(
    source: WorkspaceSource,
    *,
    compile_catalog: CatalogCompiler,
) -> PackageCatalog:
    """通过唯一静态编译器规范化依赖错误边界。

    参数：``source`` 是主工作区或显式外部工作区来源；``compile_catalog`` 是组合根
    显式注入的统一编译器。
    返回：完整、不可变且没有导入作者模块的包目录（PackageCatalog）。
    异常：来源、语法、动作合同（Action Contract）或身份无效时统一抛出
    ``PackageDependencyError``，保留原异常作为诊断链。
    """

    try:
        return compile_catalog(source)
    except (TypeError, ValueError, RuntimeError) as error:
        raise PackageDependencyError(
            f"软件包依赖完整静态编译失败: {source.root}"
        ) from error


def catalog_namespace(catalog: PackageCatalog) -> str:
    """读取锁定包目录（PackageCatalog）的社区命名空间排序键。

    参数：``catalog`` 是完成摘要复核的外部包目录。
    返回：稳定社区命名空间。
    异常：无。
    """

    return catalog.namespace


def package_source_namespace(package: ResolvedPackageSource) -> str:
    """读取外部包来源/目录配对的社区命名空间排序键。

    参数：``package`` 是完成锁复核的来源与目录配对。
    返回：配对目录的稳定社区命名空间。
    异常：无。
    """

    return package.catalog.namespace


__all__ = [
    "PackageDependencyManager",
    "catalog_for_entry",
    "compile_dependency_catalog",
    "find_locked_package",
    "load_locked_package_catalogs",
    "load_locked_package_sources",
    "resolve_dependency_source",
    "validate_complete_generation",
]
