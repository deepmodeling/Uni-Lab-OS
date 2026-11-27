"""AIW-07 explicit visual baseline regression contracts."""

from __future__ import annotations

import json
from pathlib import Path

from PIL import Image

from unilabos.client.material_visual_regression import (
    approve_material_baseline,
    compare_material_capture,
)


def test_compare_requires_pixels_and_stable_scene_facts(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline.png"
    candidate = tmp_path / "candidate.png"
    _artifact(baseline, color=(255, 255, 255, 255), template_revision="template-1")
    _artifact(candidate, color=(255, 255, 255, 255), template_revision="template-1")
    assert compare_material_capture(candidate, baseline, threshold=0)["passed"]

    Image.new("RGBA", (10, 10), (0, 0, 0, 255)).save(candidate)
    pixel_result = compare_material_capture(candidate, baseline, threshold=0.01)
    assert not pixel_result["passed"]
    assert pixel_result["pixelDifferenceFraction"] == 1

    _artifact(candidate, color=(255, 255, 255, 255), template_revision="template-2")
    structural_result = compare_material_capture(candidate, baseline, threshold=1)
    assert not structural_result["passed"]
    assert structural_result["structuralDiff"][0]["field"] == (
        "scene.templateRevision"
    )


def test_baseline_changes_only_through_explicit_approval(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.png"
    baseline = tmp_path / "baselines" / "material.png"
    _artifact(candidate, color=(80, 120, 160, 255), template_revision="template-7")

    approved = approve_material_baseline(candidate, baseline)

    assert approved["status"] == "approved"
    assert baseline.read_bytes() == candidate.read_bytes()
    assert baseline.with_suffix(".png.json").is_file()


def test_structural_layout_change_fails_even_when_pixel_threshold_allows_it(
    tmp_path: Path,
) -> None:
    baseline = tmp_path / "baseline.png"
    candidate = tmp_path / "candidate.png"
    _artifact(baseline, color=(255, 255, 255, 255), template_revision="template-1")
    _artifact(candidate, color=(255, 255, 255, 255), template_revision="template-1")
    metadata_path = candidate.with_suffix(".png.json")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["scene"]["nodes"][0]["placement"] = {
        "kind": "root",
        "localPose": {"positionMm": [100, 0, 0]},
    }
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    result = compare_material_capture(candidate, baseline, threshold=1)

    assert not result["passed"]
    assert result["structuralDiff"][0]["field"] == "scene.nodes"


def _artifact(path: Path, *, color: tuple[int, int, int, int], template_revision: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA", (10, 10), color).save(path)
    metadata = {
        "schemaVersion": "unilab-material-capture-artifact/v1",
        "layoutRevision": "layout-1",
        "templateRevision": template_revision,
        "rendererVersion": "test/1",
        "scene": {
            "templateRevision": template_revision,
            "nodes": [
                {
                    "materialId": "material-1",
                    "sourceNodeId": "device-1",
                    "sourceTemplateId": "template-1",
                    "kind": "device",
                    "sites": [{"siteId": "site-1"}],
                }
            ],
        },
    }
    path.with_suffix(".png.json").write_text(
        json.dumps(metadata), encoding="utf-8"
    )
