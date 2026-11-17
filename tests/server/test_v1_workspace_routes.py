"""V1 工作区只读路由（/workspace/package-mounts、/file-browser）契约测试。"""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.api_envelope import error_of, ok_data
from unilabos.server.api.workspace import create_workspace_router


def test_package_mounts_and_file_browser(tmp_path) -> None:
    package_dir = tmp_path / "pkgs" / "acme_driver"
    package_dir.mkdir(parents=True)
    (tmp_path / "work" / "sub").mkdir(parents=True)
    (tmp_path / "work" / "run.py").write_text("print(1)\n")
    packages = SimpleNamespace(inventory=lambda: {"packages": [
        {"name": "acme", "version": "1.2.0", "enabled": True, "dirs_exist": True, "source_kind": "local",
         "package_dirs": [str(package_dir)], "device_ids": ["acme.pump"], "sha256": "abc"},
        {"name": "off", "version": "0.1", "enabled": False, "dirs_exist": True, "package_dirs": [str(package_dir)]},
    ]})
    app = FastAPI()
    app.include_router(create_workspace_router(working_dir=tmp_path / "work", driver_packages=packages))
    client = TestClient(app)
    mounts = ok_data(client.get("/api/v1/workspace/package-mounts"))
    assert mounts["schemaVersion"] == "workspace-package-mounts/v1" and len(mounts["items"]) == 1
    item = mounts["items"][0]
    assert item["distributionName"] == "acme" and item["packageRootUri"] == package_dir.resolve().as_uri()
    assert item["readOnly"] is True and mounts["mountRevision"]
    listing = ok_data(client.get("/api/v1/file-browser"))
    assert [entry["name"] for entry in listing["items"]] == ["sub", "run.py"]
    assert listing["items"][1]["is_python"] is True
    sub = ok_data(client.get("/api/v1/file-browser", params={"path": "sub"}))
    assert sub["items"][0]["is_parent"] is True
    error_of(client.get("/api/v1/file-browser", params={"path": "../"}), 1000)
    error_of(client.get("/api/v1/file-browser", params={"path": "missing"}), 4001)
    error_of(client.get("/api/v1/file-browser", params={"path": "run.py"}), 1000)
