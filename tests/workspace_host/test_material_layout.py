"""AIW-07 Material layout source identity and CAS regression tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from unilabos.workspace_host.material_layout import (
    LAYOUT_CHANGE_SCHEMA,
    MaterialLayoutWorkspace,
)
from unilabos.workspace_host.model import WorkspaceHostError, WorkspacePaths


@pytest.fixture
def layout(tmp_path: Path) -> MaterialLayoutWorkspace:
    workspace = tmp_path / "workspace"
    graph = workspace / "deployment" / "graphs" / "lab.json"
    graph.parent.mkdir(parents=True)
    graph.write_text(
        json.dumps(
            {
                "nodes": [
                    {
                        "id": "device-a",
                        "name": "同名展示不参与身份",
                        "type": "device",
                        "class": "package.device_a",
                        "position": {"x": 10, "y": 20, "z": 0},
                        "config": {"size_x": 100, "size_y": 80, "size_z": 40},
                    },
                    {
                        "id": "device-b",
                        "name": "同名展示不参与身份",
                        "type": "device",
                        "class": "package.device_b",
                        "pose": {
                            "position": {"x": 300, "y": 200, "z": 0},
                            "rotation": {"x": 0, "y": 0, "z": 0},
                        },
                        "config": {"size_x": 50, "size_y": 50, "size_z": 50},
                    },
                ]
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return MaterialLayoutWorkspace(WorkspacePaths.resolve(workspace), graph)


def test_preview_is_non_mutating_and_apply_writes_exact_proven_candidate(
    layout: MaterialLayoutWorkspace,
) -> None:
    before = layout.graph_path.read_bytes()
    revision = layout.inspect()["revision"]
    preview = layout.preview(
        {
            "schemaVersion": LAYOUT_CHANGE_SCHEMA,
            "nodes": [
                {
                    "sourceNodeId": "device-a",
                    "positionMm": [120, 75, 5],
                    "rotationDegXYZ": [0, 0, 90],
                }
            ],
            "view": {
                "mode": "3d",
                "cameraPreset": "top",
                "viewport": {"width": 1200, "height": 800},
            },
        },
        expected_revision=str(revision),
    )

    assert layout.graph_path.read_bytes() == before
    assert preview["sourceRevision"] == revision
    assert preview["structuralDiff"][0]["sourceNodeId"] == "device-a"
    assert Path(preview["previewArtifact"]["path"]).is_file()

    applied = layout.apply(
        str(preview["previewId"]), expected_revision=str(revision)
    )
    graph = json.loads(layout.graph_path.read_text(encoding="utf-8"))
    node = graph["nodes"][0]
    assert node["position"] == {"x": 120.0, "y": 75.0, "z": 5.0}
    assert node["pose"]["rotation"] == {"x": 0.0, "y": 0.0, "z": 90.0}
    assert applied["revision"] == preview["candidateRevision"]


def test_apply_rejects_a_concurrent_human_edit(layout: MaterialLayoutWorkspace) -> None:
    revision = str(layout.inspect()["revision"])
    preview = layout.preview(
        {
            "schemaVersion": LAYOUT_CHANGE_SCHEMA,
            "nodes": [{"sourceNodeId": "device-b", "positionMm": [1, 2, 3]}],
        },
        expected_revision=revision,
    )
    layout.graph_path.write_text(
        layout.graph_path.read_text(encoding="utf-8") + "\n",
        encoding="utf-8",
    )

    with pytest.raises(WorkspaceHostError) as caught:
        layout.apply(str(preview["previewId"]), expected_revision=revision)

    assert caught.value.code == "layout_revision_conflict"


def test_change_targets_source_identity_and_validates_template_asset(
    layout: MaterialLayoutWorkspace,
) -> None:
    asset = layout.paths.workspace / "models" / "device.glb"
    asset.parent.mkdir()
    asset.write_bytes(b"glTF")
    revision = str(layout.inspect()["revision"])
    preview = layout.preview(
        {
            "schemaVersion": LAYOUT_CHANGE_SCHEMA,
            "nodes": [
                {
                    "sourceNodeId": "device-b",
                    "assetRef": {
                        "path": "/api/v1/material-models/layout-lab/models/device.glb",
                        "sourcePath": "models/device.glb",
                        "format": "gltf",
                    },
                }
            ],
        },
        expected_revision=revision,
    )
    assert preview["candidate"]["nodes"][1]["assetRef"]["path"] == (
        "/api/v1/material-models/layout-lab/models/device.glb"
    )
    assert "sourcePath" not in preview["candidate"]["nodes"][1]["assetRef"]

    with pytest.raises(WorkspaceHostError) as caught:
        layout.preview(
            {
                "schemaVersion": LAYOUT_CHANGE_SCHEMA,
                "nodes": [{"sourceNodeId": "同名展示不参与身份", "positionMm": [0, 0, 0]}],
            },
            expected_revision=revision,
        )
    assert caught.value.code == "layout_source_not_found"
