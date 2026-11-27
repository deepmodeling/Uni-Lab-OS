"""工作区软件包精确源码挂载合同测试。"""

from __future__ import annotations

from pathlib import Path

from unilabos.package_manager import WorkspaceSource, compile_package_source
from unilabos.package_manager.workspace_runtime.authoring_mounts import (
    compile_workspace_package_mount_projection,
)
from unilabos.package_manager.workspace_runtime.package_source import (
    PackageCatalogSource,
)


def _write_package(root: Path, package_id: str) -> PackageCatalogSource:
    package_root = root / package_id
    package_root.mkdir(parents=True)
    package_root.joinpath("__init__.py").write_text("", encoding="utf-8")
    root.joinpath("pyproject.toml").write_text(
        f'[project]\nname = "{package_id.replace("_", "-")}"\nversion = "1.2.3"\n',
        encoding="utf-8",
    )
    root.joinpath("package.yaml").write_text(
        f"package:\n  name: {package_id}\nworkflows: []\n",
        encoding="utf-8",
    )
    source = WorkspaceSource(root)
    return PackageCatalogSource(source=source, catalog=compile_package_source(source))


def test_projection_publishes_exact_editable_and_dependency_mounts(tmp_path: Path) -> None:
    editable = _write_package(tmp_path / "workspace", "editable_lab")
    dependency = _write_package(tmp_path / "dependency", "dependency_lab")

    projection = compile_workspace_package_mount_projection(
        (editable, dependency),
        editable_source=editable.source,
        dependency_revision="sha256:dependency-lock",
    )
    wire = projection.to_dict()

    assert wire["schemaVersion"] == "workspace-package-mounts/v1"
    assert wire["editablePackageId"] == "editable_lab"
    assert wire["dependencyRevision"] == "sha256:dependency-lock"
    assert str(wire["catalogRevision"]).startswith("sha256:")
    assert str(wire["mountRevision"]).startswith("sha256:")
    assert wire["items"] == [
        {
            "packageId": "dependency_lab",
            "distributionName": "dependency-lab",
            "version": "1.2.3",
            "namespace": "community.dependency_lab",
            "editable": False,
            "readOnly": True,
            "sourceKind": "workspace",
            "importRootUri": dependency.source.root.as_uri(),
            "packageRootUri": (dependency.source.root / "dependency_lab").as_uri(),
            "contentDigest": dependency.catalog.content_digest,
            "catalogDigest": dependency.catalog.catalog_digest,
        },
        {
            "packageId": "editable_lab",
            "distributionName": "editable-lab",
            "version": "1.2.3",
            "namespace": "community.editable_lab",
            "editable": True,
            "readOnly": False,
            "sourceKind": "workspace",
            "importRootUri": editable.source.root.as_uri(),
            "packageRootUri": (editable.source.root / "editable_lab").as_uri(),
            "contentDigest": editable.catalog.content_digest,
            "catalogDigest": editable.catalog.catalog_digest,
        },
    ]


def test_catalog_revision_survives_workspace_move_but_mount_revision_changes(
    tmp_path: Path,
) -> None:
    first = _write_package(tmp_path / "first" / "workspace", "portable_lab")
    second = _write_package(tmp_path / "second" / "workspace", "portable_lab")

    first_projection = compile_workspace_package_mount_projection(
        (first,), editable_source=first.source, dependency_revision="sha256:none"
    )
    second_projection = compile_workspace_package_mount_projection(
        (second,), editable_source=second.source, dependency_revision="sha256:none"
    )

    assert first_projection.catalog_revision == second_projection.catalog_revision
    assert first_projection.mount_revision != second_projection.mount_revision
