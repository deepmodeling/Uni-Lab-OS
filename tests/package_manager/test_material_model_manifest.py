"""模型发布的版本粒度、历史字节与实际 HTTP 路由。"""

import hashlib
import json
from copy import deepcopy
from pathlib import PurePosixPath
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from unilabos.app.scheduler.inventory.backend_api import create_backend_resource_router, create_material_asset_router
from unilabos.app.scheduler.inventory.backend_contract import BackendResourceService
from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.package_manager.package_catalog.material_model_manifest import MaterialModelManifest
from unilabos.package_manager.package_catalog.material_models import WorkspaceMaterialModelCatalog
from unilabos.package_manager.package_catalog.sources import WorkspaceSource


def test_model_publications(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    workspace = tmp_path / "work"
    root = workspace / "pkg/models"
    root.mkdir(parents=True)
    for name, content in {
        "arm.gltf": json.dumps({"images": [{"uri": "body.png"}]}).encode(),
        "body.png": b"old",
        "other.stl": b"mesh",
        "remote.gltf": b'{"images":[{"uri":"https://elsewhere.example/body.png"}]}',
    }.items():
        (root / name).write_bytes(content)

    def publish():
        assets = [
            SimpleNamespace(
                logical_path=p.relative_to(workspace).as_posix(), digest=hashlib.sha256(p.read_bytes()).hexdigest()
            )
            for p in root.iterdir()
        ]
        return MaterialModelManifest.publish(
            WorkspaceSource(workspace), "lab", "pkg", {PurePosixPath("pkg/models")}, assets
        )

    first = publish()
    assert first.revision == publish().revision
    (root / "body.png").write_bytes(b"new")
    second = publish()
    assert first.revision != second.revision
    arm, other = "pkg/models/arm.gltf", "pkg/models/other.stl"
    assert first.versions[arm] != second.versions[arm]
    assert first.versions[other] == second.versions[other]
    assert "pkg/models/remote.gltf" not in second.versions
    assert first.files[arm] == second.files[arm]
    desc = second.describe({"path": second.base + arm, "instances": {"path": second.base + other}})
    assert desc["assetRevision"] == second.revision and desc["version"] == second.versions[arm]
    assert desc["instances"]["version"] == second.versions[other]

    plan = SimpleNamespace(distribution_name="lab", import_package="pkg", source=WorkspaceSource(workspace))
    catalog = WorkspaceMaterialModelCatalog(plan, {}, (PurePosixPath("pkg/models"),), publication=second)
    app = FastAPI()
    app.include_router(create_material_asset_router(material_model_catalog=catalog), prefix="/api/v1")
    client = TestClient(app)
    plain_url = second.base + "pkg/models/body.png"
    plain = client.get(plain_url)
    assert plain.content == b"new"
    assert plain.headers["cache-control"] == "private, max-age=0, must-revalidate"
    assert client.get(plain_url, headers={"If-None-Match": plain.headers["etag"]}).status_code == 304
    for publication, expected in ((first, b"old"), (second, b"new")):
        key = "pkg/models/body.png"
        url = publication.base + key + "?v=" + publication.files[key]
        response = client.get(url)
        assert response.content == expected
        assert response.headers["content-type"] == plain.headers["content-type"]
        assert response.headers["cache-control"] == "private, max-age=2592000, immutable"
        assert response.headers["vary"] == "Authorization, Cookie"
        assert client.get(url.replace("/body.png?", "/other.stl?")).status_code == 404
        assert client.get(url.replace("/lab/", "/unrelated/")).status_code == 404
    (root / "body.png").write_bytes(b"third")
    publish()
    assert client.get(first.base + key + "?v=" + first.files[key]).status_code == 404
    assert client.get(second.base + key + "?v=invalid").status_code == 404


def test_usdc_nested_layer_and_texture(tmp_path, monkeypatch):
    sdf = pytest.importorskip("pxr.Sdf")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    workspace = tmp_path / "work"
    root = workspace / "pkg/model"
    root.mkdir(parents=True)
    entry = sdf.Layer.CreateNew(str(root / "arm.usdc"))
    entry.subLayerPaths = ["body.usda"]
    entry.Save()
    (root / "body.usda").write_text('#usda 1.0\ndef Shader "Body" {\n asset inputs:file = @body.png@\n}\n')
    image = root / "body.png"
    image.write_bytes(b"old")

    def version():
        assets = [
            SimpleNamespace(
                logical_path=p.relative_to(workspace).as_posix(), digest=hashlib.sha256(p.read_bytes()).hexdigest()
            )
            for p in root.iterdir()
        ]
        pub = MaterialModelManifest.publish(
            WorkspaceSource(workspace), "lab", "pkg", {PurePosixPath("pkg/model")}, assets
        )
        return pub.versions["pkg/model/arm.usdc"]

    before = version()
    image.write_bytes(b"new")
    changed_image = version()
    assert changed_image != before
    with (root / "body.usda").open("a") as layer:
        layer.write("\n# layer revision\n")
    assert version() != changed_image


def test_graph_and_template_expose_the_same_release_without_persisting_it(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    root = tmp_path / "pkg/models"
    root.mkdir(parents=True)
    asset = root / "part.stl"
    asset.write_bytes(b"part")
    path = "pkg/models/part.stl"
    publication = MaterialModelManifest.publish(
        WorkspaceSource(tmp_path),
        "lab",
        "pkg",
        {PurePosixPath("pkg/models")},
        [SimpleNamespace(logical_path=path, digest=hashlib.sha256(asset.read_bytes()).hexdigest())],
    )
    declaration = {"path": publication.base + path}
    catalog = SimpleNamespace(
        startup_plan=SimpleNamespace(distribution_name="lab"),
        publication=publication,
        models_by_template={"part": declaration},
    )
    store = InventoryStore(str(tmp_path / "inventory.db"))
    service = BackendResourceService(store)
    template_id = str(uuid4())
    node = {"material": {"config": {}}, "resource_template": {"uuid": template_id}}
    graph = {"nodes": [{"material": {"config": {"rendering": {"model": declaration}}}}, node, deepcopy(node)]}
    monkeypatch.setattr(service, "material_graph", lambda **kwargs: deepcopy(graph))
    reads = []

    def get_template(key):
        reads.append(key)
        return {"name": "part", "model": {"entry": "part.stl"}}

    monkeypatch.setattr(service, "get_resource_template", get_template)
    app = FastAPI()
    app.include_router(create_backend_resource_router(service, material_model_catalog=catalog))
    client = TestClient(app)
    response = client.get("/api/v1/materials/graph")
    data = response.json()["data"]
    assert response.headers["cache-control"] == "private, no-store"
    assert reads == [template_id]
    assert data["modelAssets"] == {
        publication.revision: {"basePath": publication.base, "files": {path: hashlib.sha256(b"part").hexdigest()}}
    }
    projected = data["nodes"][0]["material"]["config"]["rendering"]["model"]
    for model in (projected, data["nodes"][1]["resource_template"]["model"]):
        assert model["version"] == publication.versions[path]
        assert model["assetRevision"] == publication.revision
    assert client.get(publication.base + path + "?v=" + publication.files[path]).content == b"part"
    routes = app.openapi()["paths"]
    assert not any("/model-manifests/" in route or "/model-assets/" in route for route in routes)
    assert declaration == {"path": publication.base + path}
    store.close()


def test_handwritten_template_resolves_entry_from_its_declaring_file(tmp_path):
    path = "pkg/models/arm.stl"
    publication = MaterialModelManifest(
        tmp_path / "model.db", "/api/v1/material-models/lab/", "release", {path: "digest"}, {path: "v1"}
    )
    detail = {
        "name": "custom",
        "model": {"entry": "models/arm.stl"},
        "meta_data": {"unilab": {"source_uri": "package://pkg/declaration.py"}},
    }
    model = publication.describe_template(detail, {})["model"]
    assert model["path"] == publication.base + path
    assert model["version"] == "v1" and model["assetRevision"] == publication.revision
    missing = {**detail, "model": {"entry": "unpublished.stl"}}
    assert publication.describe_template(missing, {}) == missing
