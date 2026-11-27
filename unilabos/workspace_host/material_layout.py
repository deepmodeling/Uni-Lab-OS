"""Workspace-owned Material layout preview and compare-and-swap apply.

The device graph is the only durable layout source.  This module deliberately
does not invent a second database: previews are disposable runtime artifacts,
while a successful apply rewrites the selected graph with one CAS-protected
atomic replace.  Stable graph ``id`` values are the public source identities;
display names are never accepted as mutation targets.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from copy import deepcopy
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from .model import WorkspaceHostError, WorkspacePaths, utc_timestamp

LAYOUT_CHANGE_SCHEMA = "unilab-material-layout-change/v1"
LAYOUT_PREVIEW_SCHEMA = "unilab-material-layout-preview/v1"
LAYOUT_APPLY_SCHEMA = "unilab-material-layout-apply/v1"


class MaterialLayoutWorkspace:
    """Compile layout candidates and atomically apply a proven preview."""

    def __init__(self, paths: WorkspacePaths, graph_path: str | Path) -> None:
        self.paths = paths
        candidate = Path(graph_path)
        if not candidate.is_absolute():
            candidate = paths.workspace / candidate
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as error:
            raise WorkspaceHostError(
                "layout_graph_not_found", f"设备图不存在：{candidate}"
            ) from error
        if not resolved.is_relative_to(paths.workspace):
            raise WorkspaceHostError(
                "layout_graph_outside_workspace", "设备图必须位于当前工作区"
            )
        self.graph_path = resolved
        self.preview_directory = paths.runtime / "layout-previews"

    def inspect(self) -> dict[str, object]:
        raw, graph = self._read_graph()
        return {
            "schemaVersion": LAYOUT_PREVIEW_SCHEMA,
            "workspacePath": str(self.paths.workspace),
            "graphPath": str(self.graph_path),
            "revision": _revision(raw),
            "nodes": _layout_nodes(graph),
        }

    def preview(
        self,
        change_set: object,
        *,
        expected_revision: str,
    ) -> dict[str, object]:
        raw, graph = self._read_graph()
        actual_revision = _revision(raw)
        _require_revision(expected_revision, actual_revision)
        normalized = _normalize_change_set(change_set, self.paths.workspace)
        candidate = deepcopy(graph)
        before = {node["sourceNodeId"]: node for node in _layout_nodes(graph)}
        changed = _apply_changes(candidate, normalized)
        after_nodes = _layout_nodes(candidate)
        after = {node["sourceNodeId"]: node for node in after_nodes}
        diagnostics = _layout_diagnostics(candidate, changed)
        structural_diff = [
            {
                "sourceNodeId": source_id,
                "before": before[source_id],
                "after": after[source_id],
            }
            for source_id in changed
        ]
        candidate_bytes = _encode_graph(candidate)
        candidate_revision = _revision(candidate_bytes)
        preview_identity = hashlib.sha256(
            _canonical_json(
                {
                    "sourceRevision": actual_revision,
                    "candidateRevision": candidate_revision,
                    "changeSet": normalized,
                }
            )
        ).hexdigest()
        artifact = {
            "schemaVersion": LAYOUT_PREVIEW_SCHEMA,
            "previewId": preview_identity,
            "createdAt": utc_timestamp(),
            "workspacePath": str(self.paths.workspace),
            "graphPath": str(self.graph_path),
            "sourceRevision": actual_revision,
            "candidateRevision": candidate_revision,
            "changeSet": normalized,
            "candidate": {
                "nodes": after_nodes,
                "view": normalized.get("view"),
            },
            "diagnostics": diagnostics,
            "structuralDiff": structural_diff,
        }
        self.preview_directory.mkdir(parents=True, exist_ok=True)
        artifact_path = self.preview_directory / f"{preview_identity}.json"
        _atomic_write(artifact_path, _canonical_pretty_json(artifact), mode=0o600)
        return {
            **artifact,
            "previewArtifact": {
                "path": str(artifact_path),
                "sha256": hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
            },
        }

    def apply(
        self,
        preview_id: str,
        *,
        expected_revision: str,
    ) -> dict[str, object]:
        if not preview_id or any(character not in "0123456789abcdef" for character in preview_id):
            raise WorkspaceHostError("layout_preview_invalid", "preview identity 无效")
        artifact_path = self.preview_directory / f"{preview_id}.json"
        try:
            artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise WorkspaceHostError(
                "layout_preview_not_found", f"布局 preview 不存在：{preview_id}"
            ) from error
        if not isinstance(artifact, Mapping) or artifact.get("previewId") != preview_id:
            raise WorkspaceHostError("layout_preview_invalid", "布局 preview 内容无效")
        raw, graph = self._read_graph()
        actual_revision = _revision(raw)
        _require_revision(expected_revision, actual_revision)
        if artifact.get("sourceRevision") != actual_revision:
            raise WorkspaceHostError(
                "layout_revision_conflict",
                "布局源已在 preview 后变化，拒绝覆盖",
                details={
                    "expected": artifact.get("sourceRevision"),
                    "actual": actual_revision,
                    "previewId": preview_id,
                },
            )
        normalized = artifact.get("changeSet")
        candidate = deepcopy(graph)
        changed = _apply_changes(candidate, normalized)
        candidate_bytes = _encode_graph(candidate)
        candidate_revision = _revision(candidate_bytes)
        if candidate_revision != artifact.get("candidateRevision"):
            raise WorkspaceHostError(
                "layout_preview_invalid", "布局 preview 与规范化候选摘要不一致"
            )
        diagnostics = _layout_diagnostics(candidate, changed)
        blocking = [item for item in diagnostics if item.get("severity") == "error"]
        if blocking:
            raise WorkspaceHostError(
                "layout_candidate_invalid",
                "布局候选包含阻塞诊断",
                details={"diagnostics": blocking},
            )
        _atomic_write(self.graph_path, candidate_bytes)
        return {
            "schemaVersion": LAYOUT_APPLY_SCHEMA,
            "previewId": preview_id,
            "graphPath": str(self.graph_path),
            "sourceRevision": actual_revision,
            "revision": candidate_revision,
            "changedSourceNodeIds": changed,
            "changeSet": normalized,
            "diagnostics": diagnostics,
        }

    def _read_graph(self) -> tuple[bytes, dict[str, Any]]:
        try:
            raw = self.graph_path.read_bytes()
            graph = json.loads(raw)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise WorkspaceHostError(
                "layout_graph_invalid", f"无法读取设备图：{self.graph_path}"
            ) from error
        if not isinstance(graph, dict) or not isinstance(graph.get("nodes"), list):
            raise WorkspaceHostError(
                "layout_graph_invalid", "设备图根必须包含 nodes 数组"
            )
        return raw, graph


def _normalize_change_set(value: object, workspace: Path) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise WorkspaceHostError("layout_change_invalid", "布局 change set 必须是 object")
    schema = value.get("schemaVersion", LAYOUT_CHANGE_SCHEMA)
    if schema != LAYOUT_CHANGE_SCHEMA:
        raise WorkspaceHostError("layout_change_invalid", "布局 change set schemaVersion 无效")
    raw_nodes = value.get("nodes")
    if not isinstance(raw_nodes, Sequence) or isinstance(raw_nodes, (str, bytes)):
        raise WorkspaceHostError("layout_change_invalid", "布局 change set.nodes 必须是数组")
    nodes: list[dict[str, object]] = []
    identities: set[str] = set()
    for index, raw_node in enumerate(raw_nodes):
        if not isinstance(raw_node, Mapping):
            raise WorkspaceHostError("layout_change_invalid", f"nodes[{index}] 必须是 object")
        source_id = str(raw_node.get("sourceNodeId") or "").strip()
        if not source_id or source_id in identities:
            raise WorkspaceHostError(
                "layout_change_invalid", f"nodes[{index}].sourceNodeId 缺失或重复"
            )
        identities.add(source_id)
        node: dict[str, object] = {"sourceNodeId": source_id}
        if "positionMm" in raw_node:
            node["positionMm"] = _vector(raw_node["positionMm"], f"nodes[{index}].positionMm")
        if "rotationDegXYZ" in raw_node:
            node["rotationDegXYZ"] = _vector(
                raw_node["rotationDegXYZ"], f"nodes[{index}].rotationDegXYZ"
            )
        if "assetRef" in raw_node:
            node["assetRef"] = _asset_ref(raw_node["assetRef"], workspace, index)
        if len(node) == 1:
            raise WorkspaceHostError(
                "layout_change_invalid", f"nodes[{index}] 没有布局变更"
            )
        nodes.append(node)
    result: dict[str, object] = {
        "schemaVersion": LAYOUT_CHANGE_SCHEMA,
        "nodes": nodes,
    }
    if value.get("view") is not None:
        result["view"] = _view(value["view"])
    return result


def _vector(value: object, field: str) -> list[float]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) != 3:
        raise WorkspaceHostError("layout_change_invalid", f"{field} 必须是三个有限数")
    result: list[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item):
            raise WorkspaceHostError("layout_change_invalid", f"{field} 必须是三个有限数")
        result.append(float(item))
    return result


def _asset_ref(value: object, workspace: Path, index: int) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise WorkspaceHostError("layout_change_invalid", f"nodes[{index}].assetRef 必须是 object")
    path = str(value.get("path") or "").strip()
    if not path:
        raise WorkspaceHostError("layout_change_invalid", f"nodes[{index}].assetRef.path 缺失")
    if not path.startswith("/api/v1/material-models/") or (
        "\\" in path or ".." in PurePosixPath(path).parts
    ):
        raise WorkspaceHostError(
            "layout_asset_invalid",
            "模板资产必须使用 /api/v1/material-models/ 公共路由",
        )
    source_path = str(value.get("sourcePath") or "").strip()
    if source_path:
        candidate = (workspace / source_path).resolve()
        if not candidate.is_relative_to(workspace) or not candidate.is_file():
            raise WorkspaceHostError(
                "layout_asset_not_found",
                f"模板资产不在工作区或不存在：{source_path}",
            )
    result: dict[str, object] = {"path": path}
    if source_path:
        result["sourcePath"] = source_path
    for key in ("format", "meshDir", "macro"):
        if value.get(key) is not None:
            result[key] = str(value[key])
    return result


def _view(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise WorkspaceHostError("layout_change_invalid", "view 必须是 object")
    mode = str(value.get("mode") or "2.5d")
    if mode not in {"2d", "2.5d", "3d", "split"}:
        raise WorkspaceHostError("layout_change_invalid", "view.mode 无效")
    camera = str(value.get("cameraPreset") or "default")
    if camera not in {"default", "top"}:
        raise WorkspaceHostError("layout_change_invalid", "view.cameraPreset 无效")
    result: dict[str, object] = {"mode": mode, "cameraPreset": camera}
    viewport = value.get("viewport")
    if viewport is not None:
        if not isinstance(viewport, Mapping):
            raise WorkspaceHostError("layout_change_invalid", "view.viewport 必须是 object")
        width = viewport.get("width")
        height = viewport.get("height")
        if not isinstance(width, int) or not 320 <= width <= 4096:
            raise WorkspaceHostError("layout_change_invalid", "view.viewport.width 无效")
        if not isinstance(height, int) or not 240 <= height <= 4096:
            raise WorkspaceHostError("layout_change_invalid", "view.viewport.height 无效")
        result["viewport"] = {"width": width, "height": height}
    return result


def _layout_nodes(graph: Mapping[str, Any]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    identities: set[str] = set()
    for index, raw in enumerate(graph.get("nodes", [])):
        if not isinstance(raw, Mapping):
            raise WorkspaceHostError("layout_graph_invalid", f"nodes[{index}] 必须是 object")
        source_id = str(raw.get("id") or "").strip()
        if not source_id or source_id in identities:
            raise WorkspaceHostError(
                "layout_graph_invalid", f"nodes[{index}].id 缺失或重复"
            )
        identities.add(source_id)
        pose = raw.get("pose") if isinstance(raw.get("pose"), Mapping) else {}
        position = pose.get("position") if isinstance(pose.get("position"), Mapping) else raw.get("position")
        position = position if isinstance(position, Mapping) else {}
        rotation = pose.get("rotation") if isinstance(pose.get("rotation"), Mapping) else {}
        config = raw.get("config") if isinstance(raw.get("config"), Mapping) else {}
        rendering = config.get("rendering") if isinstance(config.get("rendering"), Mapping) else {}
        asset_ref = rendering.get("model") if isinstance(rendering.get("model"), Mapping) else None
        result.append(
            {
                "sourceNodeId": source_id,
                "sourceType": str(raw.get("type") or "resource"),
                "positionMm": _mapping_vector(position),
                "rotationDegXYZ": _mapping_vector(rotation),
                "dimensionsMm": [
                    _finite_or_zero(config.get("size_x")),
                    _finite_or_zero(config.get("size_y")),
                    _finite_or_zero(config.get("size_z")),
                ],
                "parentSourceNodeId": str(raw.get("parent") or "") or None,
                "assetRef": deepcopy(dict(asset_ref)) if asset_ref else None,
            }
        )
    return result


def _apply_changes(graph: dict[str, Any], change_set: object) -> list[str]:
    if not isinstance(change_set, Mapping) or not isinstance(change_set.get("nodes"), list):
        raise WorkspaceHostError("layout_preview_invalid", "preview change set 无效")
    source = {
        str(node.get("id")): node
        for node in graph["nodes"]
        if isinstance(node, dict) and node.get("id")
    }
    changed: list[str] = []
    for change in change_set["nodes"]:
        source_id = str(change["sourceNodeId"])
        node = source.get(source_id)
        if node is None:
            raise WorkspaceHostError(
                "layout_source_not_found", f"布局节点不存在：{source_id}"
            )
        if "positionMm" in change:
            x, y, z = change["positionMm"]
            mapped = {"x": x, "y": y, "z": z}
            if "position" in node:
                node["position"] = mapped
            else:
                node.setdefault("pose", {})["position"] = mapped
        if "rotationDegXYZ" in change:
            x, y, z = change["rotationDegXYZ"]
            node.setdefault("pose", {})["rotation"] = {"x": x, "y": y, "z": z}
        if "assetRef" in change:
            asset = {
                key: value
                for key, value in change["assetRef"].items()
                if key != "sourcePath"
            }
            node.setdefault("config", {}).setdefault("rendering", {})["model"] = deepcopy(asset)
        changed.append(source_id)
    return changed


def _layout_diagnostics(
    graph: Mapping[str, Any], changed: Sequence[str]
) -> list[dict[str, object]]:
    nodes = {node["sourceNodeId"]: node for node in _layout_nodes(graph)}
    diagnostics: list[dict[str, object]] = []
    for source_id in changed:
        node = nodes[source_id]
        dimensions = node["dimensionsMm"]
        if node["sourceType"] == "device" and any(value <= 0 for value in dimensions):
            diagnostics.append(
                {
                    "severity": "warning",
                    "code": "layout_dimensions_missing",
                    "sourceNodeId": source_id,
                    "message": "设备缺少完整外包尺寸，无法证明碰撞约束",
                }
            )
    changed_set = set(changed)
    physical = [
        node
        for node in nodes.values()
        if node["sourceType"] in {"device", "deck", "warehouse"}
        and all(value > 0 for value in node["dimensionsMm"])
    ]
    for index, left in enumerate(physical):
        for right in physical[index + 1 :]:
            if not ({left["sourceNodeId"], right["sourceNodeId"]} & changed_set):
                continue
            if left["parentSourceNodeId"] or right["parentSourceNodeId"]:
                continue
            if _overlaps_xy(left, right):
                diagnostics.append(
                    {
                        "severity": "warning",
                        "code": "layout_bounds_overlap",
                        "sourceNodeIds": [left["sourceNodeId"], right["sourceNodeId"]],
                        "message": "设备外包在 XY 平面相交，请结合真实模型截图复核",
                    }
                )
    return diagnostics


def _overlaps_xy(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    lx, ly, _ = left["positionMm"]
    lw, lh, _ = left["dimensionsMm"]
    rx, ry, _ = right["positionMm"]
    rw, rh, _ = right["dimensionsMm"]
    return lx < rx + rw and rx < lx + lw and ly < ry + rh and ry < ly + lh


def _mapping_vector(value: Mapping[str, Any]) -> list[float]:
    return [_finite_or_zero(value.get(axis)) for axis in ("x", "y", "z")]


def _finite_or_zero(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value) if math.isfinite(value) else 0.0


def _require_revision(expected: str, actual: str) -> None:
    if expected != actual:
        raise WorkspaceHostError(
            "layout_revision_conflict",
            "布局 revision 已变化，拒绝覆盖",
            details={"expected": expected, "actual": actual},
        )


def _revision(raw: bytes) -> str:
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _canonical_pretty_json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


def _encode_graph(graph: object) -> bytes:
    return (json.dumps(graph, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _atomic_write(path: Path, content: bytes, *, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


__all__ = [
    "LAYOUT_APPLY_SCHEMA",
    "LAYOUT_CHANGE_SCHEMA",
    "LAYOUT_PREVIEW_SCHEMA",
    "MaterialLayoutWorkspace",
]
