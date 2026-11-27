"""Workbench Workspace 创作 HTTP 合同测试。"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from unilabos.app.workspace_authoring_api import install_workspace_authoring_api
from unilabos.package_manager import WorkspaceSource, compile_package_source
from unilabos.package_manager.workspace_runtime.authoring_mounts import (
    compile_workspace_package_mount_projection,
)
from unilabos.package_manager.workspace_runtime.package_source import (
    PackageCatalogSource,
)


def test_package_mounts_use_backend_envelope_and_return_fresh_data(tmp_path) -> None:
    package_id = "authoring_lab"
    package_root = tmp_path / package_id
    package_root.mkdir()
    package_root.joinpath("__init__.py").write_text("", encoding="utf-8")
    tmp_path.joinpath("pyproject.toml").write_text(
        '[project]\nname = "authoring-lab"\nversion = "1.0.0"\n',
        encoding="utf-8",
    )
    tmp_path.joinpath("package.yaml").write_text(
        f"package:\n  name: {package_id}\nworkflows: []\n",
        encoding="utf-8",
    )
    source = WorkspaceSource(tmp_path)
    package = PackageCatalogSource(source, compile_package_source(source))
    projection = compile_workspace_package_mount_projection(
        (package,), editable_source=source, dependency_revision="sha256:none"
    )
    app = FastAPI()
    install_workspace_authoring_api(app, projection)
    client = TestClient(app)

    first = client.get("/api/v1/workspace/package-mounts")
    first.json()["data"]["items"][0]["packageId"] = "mutated-client-copy"
    second = client.get("/api/v1/workspace/package-mounts")

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["code"] == 0
    assert second.json()["data"]["editablePackageId"] == package_id
    assert second.json()["data"]["items"][0]["packageId"] == package_id
