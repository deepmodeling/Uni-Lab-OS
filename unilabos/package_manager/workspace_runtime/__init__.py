"""工作区运行时（Workspace Runtime）的公开 Interface。"""

from .activation import (
    WorkspaceRegistryRuntime,
    prepare_workspace_registry_runtime,
    publish_registry_snapshot,
)
from .authoring_mounts import (
    WorkspacePackageMount,
    WorkspacePackageMountProjection,
    compile_workspace_package_mount_projection,
)
from .discovery import (
    WorkspaceSource,
    WorkspaceStartupPlan,
    compile_package_source,
    compile_workspace_startup,
    prepare_workspace_startup,
    project_catalog_startup_plan,
)
from .generation import (
    WorkspaceGenerationIdentity,
    WorkspaceGenerationPublisher,
    WorkspaceInputGeneration,
    WorkspacePackageRuntime,
    WorkspaceRefreshResult,
    WorkspaceRuntimeStatus,
    candidate_fingerprint,
    restart_reasons,
)
from .lifecycle import (
    PreparedWorkspaceProductGeneration,
    WorkspaceGenerationChangedError,
    WorkspaceProductLifecycle,
    close_workspace_product_lifecycle,
    compose_workspace_product_lifecycle,
    get_workspace_product_lifecycle,
    install_workspace_product_lifecycle,
    prepare_stable_workspace_product_generation,
)
from .monitor import (
    StableWorkspaceFileMonitor,
    StableWorkspaceGenerationMonitor,
    WorkspaceRefreshCoordinator,
)

__all__ = [
    "PreparedWorkspaceProductGeneration",
    "StableWorkspaceFileMonitor",
    "StableWorkspaceGenerationMonitor",
    "WorkspaceGenerationChangedError",
    "WorkspaceGenerationIdentity",
    "WorkspaceGenerationPublisher",
    "WorkspaceInputGeneration",
    "WorkspacePackageRuntime",
    "WorkspacePackageMount",
    "WorkspacePackageMountProjection",
    "WorkspaceProductLifecycle",
    "WorkspaceRefreshCoordinator",
    "WorkspaceRefreshResult",
    "WorkspaceRegistryRuntime",
    "WorkspaceRuntimeStatus",
    "WorkspaceSource",
    "WorkspaceStartupPlan",
    "candidate_fingerprint",
    "close_workspace_product_lifecycle",
    "compile_package_source",
    "compile_workspace_package_mount_projection",
    "compile_workspace_startup",
    "compose_workspace_product_lifecycle",
    "get_workspace_product_lifecycle",
    "install_workspace_product_lifecycle",
    "prepare_stable_workspace_product_generation",
    "prepare_workspace_registry_runtime",
    "prepare_workspace_startup",
    "project_catalog_startup_plan",
    "publish_registry_snapshot",
    "restart_reasons",
]
