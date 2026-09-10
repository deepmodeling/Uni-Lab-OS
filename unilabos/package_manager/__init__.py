"""工作区（Workspace）和包目录（PackageCatalog）的正式惰性门面。"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .cli import (
        PackageCLIError as PackageCLIError,
    )
    from .cli import (
        build_package as build_package,
    )
    from .cli import (
        cmd_package as cmd_package,
    )
    from .cli import inspect_package as inspect_package
    from .cli import (
        register_package_subcommands as register_package_subcommands,
    )
    from .cli import upload_package as upload_package
    from .driver_runtime import (
        DriverActivationError as DriverActivationError,
    )
    from .driver_runtime import (
        PythonDriverActivation as PythonDriverActivation,
    )
    from .driver_runtime import (
        activate_python_driver as activate_python_driver,
    )
    from .package_catalog import (
        PackageCatalog as PackageCatalog,
    )
    from .package_catalog import (
        PackageCompileError as PackageCompileError,
    )
    from .package_catalog import (
        RegistryActivationPlan as RegistryActivationPlan,
    )
    from .package_catalog import (
        RegistrySnapshot as RegistrySnapshot,
    )
    from .package_catalog import (
        RegistrySnapshotError as RegistrySnapshotError,
    )
    from .package_catalog import (
        WorkspaceSource as WorkspaceSource,
    )
    from .package_catalog import (
        compile_registry_snapshot as compile_registry_snapshot,
    )
    from .package_catalog.material_models import (
        WorkspaceMaterialModelAsset as WorkspaceMaterialModelAsset,
    )
    from .package_catalog.material_models import (
        WorkspaceMaterialModelCatalog as WorkspaceMaterialModelCatalog,
    )
    from .package_catalog.material_models import (
        compile_workspace_material_models as compile_workspace_material_models,
    )
    from .package_catalog.material_shapes import (
        compile_catalog_material_shapes as compile_catalog_material_shapes,
    )
    from .package_catalog.material_shapes import (
        compile_workspace_material_shapes as compile_workspace_material_shapes,
    )
    from .package_catalog.project_metadata import (
        PackageProject as PackageProject,
    )
    from .package_catalog.project_metadata import (
        parse_project_metadata as parse_project_metadata,
    )
    from .package_distribution import (
        LockedPackage as LockedPackage,
    )
    from .package_distribution import (
        PackageDependencyError as PackageDependencyError,
    )
    from .package_distribution import (
        PackageDependencyLock as PackageDependencyLock,
    )
    from .package_distribution import (
        PackageDependencyManager as PackageDependencyManager,
    )
    from .package_distribution import (
        load_locked_package_catalogs as load_locked_package_catalogs,
    )
    from .workspace_runtime import (
        PreparedWorkspaceProductGeneration as PreparedWorkspaceProductGeneration,
    )
    from .workspace_runtime import (
        StableWorkspaceFileMonitor as StableWorkspaceFileMonitor,
    )
    from .workspace_runtime import (
        StableWorkspaceGenerationMonitor as StableWorkspaceGenerationMonitor,
    )
    from .workspace_runtime import (
        WorkspaceGenerationChangedError as WorkspaceGenerationChangedError,
    )
    from .workspace_runtime import (
        WorkspaceGenerationPublisher as WorkspaceGenerationPublisher,
    )
    from .workspace_runtime import (
        WorkspaceInputGeneration as WorkspaceInputGeneration,
    )
    from .workspace_runtime import (
        WorkspacePackageRuntime as WorkspacePackageRuntime,
    )
    from .workspace_runtime import (
        WorkspaceProductLifecycle as WorkspaceProductLifecycle,
    )
    from .workspace_runtime import (
        WorkspaceRefreshCoordinator as WorkspaceRefreshCoordinator,
    )
    from .workspace_runtime import (
        WorkspaceRefreshResult as WorkspaceRefreshResult,
    )
    from .workspace_runtime import (
        WorkspaceRegistryRuntime as WorkspaceRegistryRuntime,
    )
    from .workspace_runtime import (
        WorkspaceRuntimeStatus as WorkspaceRuntimeStatus,
    )
    from .workspace_runtime import (
        WorkspaceStartupPlan as WorkspaceStartupPlan,
    )
    from .workspace_runtime import (
        close_workspace_product_lifecycle as close_workspace_product_lifecycle,
    )
    from .workspace_runtime import (
        compile_package_source as compile_package_source,
    )
    from .workspace_runtime import (
        compile_workspace_startup as compile_workspace_startup,
    )
    from .workspace_runtime import (
        compose_workspace_product_lifecycle as compose_workspace_product_lifecycle,
    )
    from .workspace_runtime import (
        get_workspace_product_lifecycle as get_workspace_product_lifecycle,
    )
    from .workspace_runtime import (
        install_workspace_product_lifecycle as install_workspace_product_lifecycle,
    )
    from .workspace_runtime import (
        prepare_stable_workspace_product_generation as prepare_stable_workspace_product_generation,
    )
    from .workspace_runtime import (
        prepare_workspace_registry_runtime as prepare_workspace_registry_runtime,
    )
    from .workspace_runtime import (
        prepare_workspace_startup as prepare_workspace_startup,
    )

# ``_EXPORT_GROUPS`` 按所有权 Module 集中维护公开名字，防止门面出现重复映射。
_EXPORT_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        ".cli",
        (
            "PackageCLIError",
            "build_package",
            "cmd_package",
            "inspect_package",
            "register_package_subcommands",
            "upload_package",
        ),
    ),
    (
        ".driver_runtime",
        (
            "DriverActivationError",
            "PythonDriverActivation",
            "activate_python_driver",
        ),
    ),
    (
        ".package_catalog",
        (
            "PackageCatalog",
            "PackageCompileError",
            "RegistryActivationPlan",
            "RegistrySnapshot",
            "RegistrySnapshotError",
            "WorkspaceSource",
            "compile_registry_snapshot",
        ),
    ),
    (
        ".package_distribution",
        (
            "LockedPackage",
            "PackageDependencyError",
            "PackageDependencyLock",
            "PackageDependencyManager",
            "load_locked_package_catalogs",
        ),
    ),
    (
        ".package_catalog.project_metadata",
        ("PackageProject", "parse_project_metadata"),
    ),
    (
        ".package_catalog.material_models",
        (
            "WorkspaceMaterialModelAsset",
            "WorkspaceMaterialModelCatalog",
            "compile_workspace_material_models",
        ),
    ),
    (
        ".package_catalog.material_shapes",
        (
            "compile_catalog_material_shapes",
            "compile_workspace_material_shapes",
        ),
    ),
    (
        ".workspace_runtime",
        (
            "PreparedWorkspaceProductGeneration",
            "StableWorkspaceFileMonitor",
            "StableWorkspaceGenerationMonitor",
            "WorkspaceGenerationChangedError",
            "WorkspaceGenerationPublisher",
            "WorkspaceInputGeneration",
            "WorkspacePackageRuntime",
            "WorkspaceProductLifecycle",
            "WorkspaceRefreshCoordinator",
            "WorkspaceRefreshResult",
            "WorkspaceRegistryRuntime",
            "WorkspaceRuntimeStatus",
            "WorkspaceStartupPlan",
            "close_workspace_product_lifecycle",
            "compile_package_source",
            "compile_workspace_startup",
            "compose_workspace_product_lifecycle",
            "get_workspace_product_lifecycle",
            "install_workspace_product_lifecycle",
            "prepare_stable_workspace_product_generation",
            "prepare_workspace_registry_runtime",
            "prepare_workspace_startup",
        ),
    ),
)

# ``_export_module_by_name`` 是公开名字到唯一所有权 Module 的惰性解析索引。
_export_module_by_name: dict[str, str] = {}
for _module_name, _export_names in _EXPORT_GROUPS:
    for _export_name in _export_names:
        if _export_name in _export_module_by_name:
            raise RuntimeError(f"包管理惰性门面存在重复公开名字: {_export_name}")
        _export_module_by_name[_export_name] = _module_name

# ``__all__`` 直接按所有权组声明顺序派生，不维护第三份容易漂移的名字清单。
__all__ = [
    export_name for _, export_names in _EXPORT_GROUPS for export_name in export_names
]


def __getattr__(name: str) -> Any:
    """按首次访问加载并缓存一个包管理公开名字。

    参数：``name`` 是 ``__all__`` 中的公开类、函数或协议名字。
    返回：从唯一所有权 Module 取得并缓存在根门面的原始对象。
    异常：名字不属于公开 Interface 时抛出 ``AttributeError``；所有权 Module
    导入或公开对象缺失时传播原始异常，不回退到其他层或重复猜测。
    """

    try:
        # ``owner_module_name`` 是该公开名字唯一登记的相对 Module 身份。
        owner_module_name = _export_module_by_name[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    owner_module = import_module(owner_module_name, __name__)
    exported_value = getattr(owner_module, name)
    globals()[name] = exported_value
    return exported_value


def __dir__() -> list[str]:
    """返回包含尚未加载公开名字的稳定模块目录。

    参数：无。
    返回：已存在模块全局名字与 ``__all__`` 的去重排序列表。
    异常：无；目录查询不会触发任何所有权 Module 导入。
    """

    return sorted(set(globals()) | set(__all__))


del _export_name, _export_names, _module_name
