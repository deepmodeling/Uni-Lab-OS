"""Explicit Material scene visual baseline comparison and approval."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any, Mapping


class MaterialVisualRegressionError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: object = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {"code": self.code, "message": str(self)}
        if self.details is not None:
            result["details"] = self.details
        return result


def compare_material_capture(
    candidate: str | Path,
    baseline: str | Path,
    *,
    threshold: float,
) -> dict[str, Any]:
    """Compare pixels and stable scene facts without silently updating baseline."""

    if not 0 <= threshold <= 1:
        raise MaterialVisualRegressionError(
            "visual_threshold_invalid", "视觉变化阈值必须在 0 与 1 之间"
        )
    candidate_path = Path(candidate).expanduser().resolve()
    baseline_path = Path(baseline).expanduser().resolve()
    candidate_metadata = _metadata(candidate_path)
    baseline_metadata = _metadata(baseline_path)
    try:
        from PIL import Image, ImageChops
    except ImportError as error:  # pragma: no cover - optional renderer extra
        raise MaterialVisualRegressionError(
            "visual_comparator_unavailable", "视觉比较需要 Pillow"
        ) from error
    try:
        with Image.open(candidate_path) as candidate_image:
            candidate_rgba = candidate_image.convert("RGBA")
        with Image.open(baseline_path) as baseline_image:
            baseline_rgba = baseline_image.convert("RGBA")
    except OSError as error:
        raise MaterialVisualRegressionError(
            "visual_artifact_invalid", f"无法读取视觉产物：{error}"
        ) from error
    if candidate_rgba.size != baseline_rgba.size:
        pixel_fraction = 1.0
        changed_pixels = candidate_rgba.width * candidate_rgba.height
        dimension_match = False
    else:
        difference = ImageChops.difference(candidate_rgba, baseline_rgba)
        changed_pixels = sum(
            1 for pixel in difference.getdata() if max(pixel) > 16
        )
        pixel_fraction = changed_pixels / max(1, candidate_rgba.width * candidate_rgba.height)
        dimension_match = True
    structural_diff = _structural_diff(candidate_metadata, baseline_metadata)
    passed = dimension_match and pixel_fraction <= threshold and not structural_diff
    return {
        "schemaVersion": "unilab-material-visual-regression/v1",
        "passed": passed,
        "threshold": threshold,
        "pixelDifferenceFraction": pixel_fraction,
        "changedPixels": changed_pixels,
        "dimensionMatch": dimension_match,
        "candidate": _artifact_identity(candidate_path, candidate_metadata),
        "baseline": _artifact_identity(baseline_path, baseline_metadata),
        "structuralDiff": structural_diff,
    }


def approve_material_baseline(
    candidate: str | Path, baseline: str | Path
) -> dict[str, Any]:
    """Explicitly replace both baseline PNG and its structural metadata."""

    candidate_path = Path(candidate).expanduser().resolve()
    baseline_path = Path(baseline).expanduser().resolve()
    candidate_metadata_path = _metadata_path(candidate_path)
    _metadata(candidate_path)
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_copy(candidate_path, baseline_path)
    _atomic_copy(candidate_metadata_path, _metadata_path(baseline_path))
    return {
        "schemaVersion": "unilab-material-visual-baseline/v1",
        "status": "approved",
        "candidate": str(candidate_path),
        "baseline": str(baseline_path),
        "sha256": hashlib.sha256(baseline_path.read_bytes()).hexdigest(),
        "metadataPath": str(_metadata_path(baseline_path)),
    }


def _structural_diff(
    candidate: Mapping[str, Any], baseline: Mapping[str, Any]
) -> list[dict[str, object]]:
    candidate_scene = candidate.get("scene")
    baseline_scene = baseline.get("scene")
    candidate_scene = candidate_scene if isinstance(candidate_scene, Mapping) else {}
    baseline_scene = baseline_scene if isinstance(baseline_scene, Mapping) else {}
    candidate_nodes = _node_facts(candidate_scene.get("nodes"))
    baseline_nodes = _node_facts(baseline_scene.get("nodes"))
    result: list[dict[str, object]] = []
    if candidate_nodes != baseline_nodes:
        result.append(
            {
                "field": "scene.nodes",
                "candidate": candidate_nodes,
                "baseline": baseline_nodes,
            }
        )
    for field in ("templateRevision",):
        if candidate_scene.get(field) != baseline_scene.get(field):
            result.append(
                {
                    "field": f"scene.{field}",
                    "candidate": candidate_scene.get(field),
                    "baseline": baseline_scene.get(field),
                }
            )
    if candidate_scene.get("sourceIdentity") != baseline_scene.get("sourceIdentity"):
        result.append(
            {
                "field": "scene.sourceIdentity",
                "candidate": candidate_scene.get("sourceIdentity"),
                "baseline": baseline_scene.get("sourceIdentity"),
            }
        )
    return result


def _node_facts(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    facts = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        facts.append(
            {
                "materialId": item.get("materialId"),
                "sourceNodeId": item.get("sourceNodeId"),
                "sourceTemplateId": item.get("sourceTemplateId"),
                "kind": item.get("kind"),
                "placement": item.get("placement"),
                "dimensionsMm": item.get("dimensionsMm"),
                "bounds": item.get("bounds"),
                "selected": item.get("selected"),
                "sites": sorted(
                    (
                        {
                            "siteId": site.get("siteId"),
                            "kind": site.get("kind"),
                            "worldPose": site.get("worldPose"),
                            "sizeMm": site.get("sizeMm"),
                            "capacity": site.get("capacity"),
                            "allowedTemplateIds": sorted(
                                str(value)
                                for value in site.get("allowedTemplateIds", [])
                            ),
                            "occupiedMaterialIds": sorted(
                                str(value)
                                for value in site.get("occupiedMaterialIds", [])
                            ),
                            "visible": site.get("visible"),
                        }
                        for site in item.get("sites", [])
                        if isinstance(site, Mapping)
                    ),
                    key=lambda site: str(site["siteId"]),
                ),
            }
        )
    return sorted(facts, key=lambda item: str(item["materialId"]))


def _artifact_identity(path: Path, metadata: Mapping[str, Any]) -> dict[str, object]:
    return {
        "path": str(path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "layoutRevision": metadata.get("layoutRevision"),
        "templateRevision": metadata.get("templateRevision"),
        "rendererVersion": metadata.get("rendererVersion"),
    }


def _metadata(path: Path) -> dict[str, Any]:
    metadata_path = _metadata_path(path)
    try:
        value = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MaterialVisualRegressionError(
            "visual_metadata_missing",
            f"截图缺少有效结构元数据：{metadata_path}",
        ) from error
    if not isinstance(value, dict):
        raise MaterialVisualRegressionError(
            "visual_metadata_invalid", f"截图元数据必须是 object：{metadata_path}"
        )
    return value


def _metadata_path(path: Path) -> Path:
    return path.with_suffix(f"{path.suffix}.json")


def _atomic_copy(source: Path, destination: Path) -> None:
    temporary = destination.with_name(f"{destination.name}.{os.getpid()}.tmp")
    try:
        shutil.copyfile(source, temporary)
        os.replace(temporary, destination)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


__all__ = [
    "MaterialVisualRegressionError",
    "approve_material_baseline",
    "compare_material_capture",
]
