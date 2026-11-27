"""包分发（Package Distribution）的公开 Interface。"""

from .adapters import (
    HttpClientPublicationAdapter,
    PackageBuilder,
    PublicationPort,
    install_package,
    publish_build,
    upload_package,
)
from .build import (
    PackageBuildArtifact,
    PackageBuildError,
    audit_package_wheel,
    build_workspace_package,
)
from .dependency_manager import (
    PackageDependencyManager,
    load_locked_package_catalogs,
    load_locked_package_sources,
)
from .inspection import CatalogCompiler, inspect_package
from .models import (
    DEPENDENCY_DECLARATION_FILE,
    DEPENDENCY_LOCK_FILE,
    LockedPackage,
    PackageDependencyError,
    PackageDependencyLock,
    ResolvedPackageSource,
)

__all__ = [
    "DEPENDENCY_DECLARATION_FILE",
    "DEPENDENCY_LOCK_FILE",
    "CatalogCompiler",
    "HttpClientPublicationAdapter",
    "LockedPackage",
    "PackageBuildArtifact",
    "PackageBuildError",
    "PackageBuilder",
    "PackageDependencyError",
    "PackageDependencyLock",
    "PackageDependencyManager",
    "PublicationPort",
    "ResolvedPackageSource",
    "audit_package_wheel",
    "build_workspace_package",
    "inspect_package",
    "install_package",
    "load_locked_package_catalogs",
    "load_locked_package_sources",
    "publish_build",
    "upload_package",
]
