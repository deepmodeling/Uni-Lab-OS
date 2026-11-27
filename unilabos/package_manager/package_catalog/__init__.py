"""包目录（PackageCatalog）Module 的公开 Interface。"""

from .compilers.python import compile_package_source
from .material_models import (
    WorkspaceMaterialModelAsset,
    WorkspaceMaterialModelCatalog,
    compile_workspace_material_models,
)
from .material_shapes import (
    compile_catalog_material_shapes,
    compile_workspace_material_shapes,
)
from .model import (
    PackageAsset,
    PackageCatalog,
    PackageCompileError,
    PackageDefinition,
    PackageDefinitionCatalog,
    PackageDiagnostic,
    PackageDistributionIdentity,
)
from .project_metadata import PackageProject, parse_project_metadata
from .registry_snapshot import (
    RegistryActivationPlan,
    RegistryAsset,
    RegistrySnapshot,
    RegistrySnapshotError,
    compile_registry_snapshot,
)
from .sources import WorkspaceSource

__all__ = [
    "PackageAsset",
    "PackageCatalog",
    "PackageCompileError",
    "PackageDefinition",
    "PackageDefinitionCatalog",
    "PackageDiagnostic",
    "PackageDistributionIdentity",
    "PackageProject",
    "RegistryActivationPlan",
    "RegistryAsset",
    "RegistrySnapshot",
    "RegistrySnapshotError",
    "WorkspaceMaterialModelAsset",
    "WorkspaceMaterialModelCatalog",
    "WorkspaceSource",
    "compile_catalog_material_shapes",
    "compile_package_source",
    "compile_registry_snapshot",
    "compile_workspace_material_models",
    "compile_workspace_material_shapes",
    "parse_project_metadata",
]
