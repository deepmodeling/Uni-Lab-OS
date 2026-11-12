"""V1 工作区物料布局（``material.layout.inspect / preview / apply``）在 dev 物料权威上的实现。

V1 的布局源是工作区设备图文件：preview 计算候选图并落一个可丢弃的 preview 文件，apply 用
图文件字节摘要做比较并交换后原子替换文件。dev 的布局权威是 materials.db 的物料树，所以：

- **布局源 / 修订**：范围内各根物料树（缺省为全部根物料，可用 ``rootMaterialUuid`` 收窄）的
  ``state_hash`` 汇总成 ``sha256:<hex>``，即 V1 的 ``revision`` / ``expectedRevision``；
- **节点身份**：``sourceNodeId`` 是 ``material_uuid``（稳定身份）；也接受范围内的 ``resource_id``
  （V1 图节点 id 在 dev 落成物料的 ``resource_id``，未删除物料间唯一），展示名永不作为目标；
- **preview**：在内存中改出候选树，用 dev 快照比较（``compare_material_snapshot`` 同一套 section
  哈希）给出逐节点差异与候选修订，不写物料库；preview 文档存在 runtime.db 的操作记录里；
- **apply**：在 materials.db 的同一个写事务里重读、核对 ``expectedRevision`` 与 preview 的源修订，
  重建候选并核对候选修订，再按根物料树走 ``apply_material_snapshot``（每个变化节点带
  ``expected_state_hash`` 前置条件）。任一步不符整体回滚，不会部分写入。

坐标约定：``positionMm`` 写 ``position_x/y/z``（相对父物料的实际位置）并同步 ``position3d_x/y/z``
（与 V1 图只给 ``position`` 时导入的结果一致）；``rotationDegXYZ`` 写 ``rotation_x/y/z``；
``assetRef`` 写物料 ``config.rendering.model``。
"""

from __future__ import annotations

import hashlib
import json
import math
from copy import deepcopy
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Optional, Sequence

from unilabos.protocol.base import canonical_hash
from unilabos.protocol.materials import (
    ACTOR_HUMAN,
    AggregatePrecondition,
    InventoryMutation,
    MaterialAggregateRead,
    MaterialSnapshot,
    MaterialTreeRead,
)
from unilabos.protocol.runtime.workspace_host import WorkspaceHostError
from unilabos.server.services.materials.snapshot import material_sections, snapshot_state_hash

LAYOUT_CHANGE_SCHEMA = "unilab-material-layout-change/v1"
LAYOUT_PREVIEW_SCHEMA = "unilab-material-layout-preview/v1"
LAYOUT_APPLY_SCHEMA = "unilab-material-layout-apply/v1"
ASSET_ROUTE_PREFIX = "/api/v1/material-models/"
_PHYSICAL_TYPES = frozenset({"device", "deck", "warehouse"})


class MaterialLayout:
    """一个布局范围（全部根物料或单棵树）上的 inspect / preview / apply。"""

    def __init__(
        self,
        materials: Any,
        *,
        root_material_uuid: Optional[str] = None,
        workspace: Optional[str | Path] = None,
    ) -> None:
        if materials is None:
            raise WorkspaceHostError("layout_unavailable", "本进程没有物料权威，不能编辑布局")
        self.materials = materials
        self.root_material_uuid = root_material_uuid or None
        self.workspace = Path(workspace).expanduser().resolve() if workspace else None

    # ── 读取 ────────────────────────────────────────────────────────

    def _trees(self) -> list[MaterialTreeRead]:
        from unilabos.server.services.materials.core import MaterialNotFoundError

        if self.root_material_uuid:
            roots = [self.root_material_uuid]
        else:
            roots = sorted(node.material.material_uuid for node in self.materials.list_materials(roots_only=True))
        trees = []
        for root in roots:
            try:
                trees.append(self.materials.get_tree(root))
            except MaterialNotFoundError as exc:
                raise WorkspaceHostError("layout_source_not_found", f"布局根物料不存在：{root}") from exc
        return trees

    def inspect(self) -> dict[str, Any]:
        trees = self._trees()
        return {
            "schemaVersion": LAYOUT_PREVIEW_SCHEMA,
            "rootMaterialUuid": self.root_material_uuid,
            "revision": layout_revision(trees),
            "nodes": _layout_nodes(trees),
        }

    def preview(self, change_set: object, *, expected_revision: str) -> dict[str, Any]:
        """只读：校验修订与变更集，返回候选布局与差异；不写物料库。"""

        trees = self._trees()
        actual_revision = layout_revision(trees)
        _require_revision(expected_revision, actual_revision)
        normalized = _normalize_change_set(change_set, trees, self.workspace)
        candidate, changed = _apply_changes(trees, normalized)
        before = {node["sourceNodeId"]: node for node in _layout_nodes(trees)}
        after_nodes = _layout_nodes(candidate)
        after = {node["sourceNodeId"]: node for node in after_nodes}
        candidate_revision = layout_revision(candidate)
        artifact = {
            "schemaVersion": LAYOUT_PREVIEW_SCHEMA,
            "previewId": _preview_identity(actual_revision, candidate_revision, normalized),
            "rootMaterialUuid": self.root_material_uuid,
            "sourceRevision": actual_revision,
            "candidateRevision": candidate_revision,
            "changeSet": normalized,
            "candidate": {"nodes": after_nodes, "view": normalized.get("view")},
            "diagnostics": _layout_diagnostics(after_nodes, changed),
            "structuralDiff": [
                {"sourceNodeId": source_id, "before": before[source_id], "after": after[source_id]}
                for source_id in changed
            ],
        }
        artifact["previewArtifact"] = {
            "storage": "runtime.db:workspace_host_operation",
            "sha256": hashlib.sha256(_canonical_json(artifact)).hexdigest(),
        }
        return artifact

    def apply(
        self,
        preview: Mapping[str, Any],
        *,
        expected_revision: str,
        command_uuid: str,
    ) -> dict[str, Any]:
        """在一个 materials.db 写事务内比较并交换，按根物料树应用快照。"""

        from unilabos.server.services.materials.core import (
            MaterialConflictError,
            MaterialsServiceError,
            MaterialValidationError,
        )

        preview_id = str(preview.get("previewId") or "")
        try:
            with self.materials.write():
                trees = self._trees()
                actual_revision = layout_revision(trees)
                _require_revision(expected_revision, actual_revision)
                if preview.get("sourceRevision") != actual_revision:
                    raise WorkspaceHostError(
                        "layout_revision_conflict",
                        "布局源已在 preview 后变化，拒绝覆盖",
                        details={
                            "expected": preview.get("sourceRevision"),
                            "actual": actual_revision,
                            "previewId": preview_id,
                        },
                    )
                normalized = preview.get("changeSet")
                candidate, changed = _apply_changes(trees, normalized)
                candidate_revision = layout_revision(candidate)
                if candidate_revision != preview.get("candidateRevision"):
                    raise WorkspaceHostError("layout_preview_invalid", "布局 preview 与规范化候选摘要不一致")
                diagnostics = _layout_diagnostics(_layout_nodes(candidate), changed)
                blocking = [item for item in diagnostics if item.get("severity") == "error"]
                if blocking:
                    raise WorkspaceHostError(
                        "layout_candidate_invalid", "布局候选包含阻塞诊断", details={"diagnostics": blocking}
                    )
                originals = {tree.root_material_uuid: tree for tree in trees}
                applied: list[dict[str, Any]] = []
                for tree in candidate:
                    original = originals[tree.root_material_uuid]
                    if tree.state_hash == original.state_hash:
                        continue
                    before = {node.material.material_uuid: node for node in original.nodes}
                    preconditions = [
                        # 每个变化节点按读到的聚合状态做前置条件：同一事务内必然成立，
                        # 防止将来有人把读取挪出事务后静默覆盖并发修改。
                        AggregatePrecondition(
                            aggregate_type="material",
                            aggregate_uuid=node.material.material_uuid,
                            expected_state_hash=before[node.material.material_uuid].state_hash,
                        )
                        for node in tree.nodes
                        if material_sections(node) != material_sections(before[node.material.material_uuid])
                    ]
                    mutation = InventoryMutation(
                        command_uuid=f"{command_uuid}:{tree.root_material_uuid}",
                        effect_key="material.layout.apply",
                        operation="apply_material_snapshot",
                        actor_type=ACTOR_HUMAN,
                        preconditions=preconditions,
                    )
                    result = self.materials.apply_snapshot(
                        mutation,
                        MaterialSnapshot(
                            root_material_uuid=tree.root_material_uuid,
                            nodes=tree.nodes,
                            state_hash=tree.state_hash,
                        ),
                    )
                    applied.append({
                        "rootMaterialUuid": tree.root_material_uuid,
                        "commandUuid": mutation.command_uuid,
                        "ledgerSequenceStart": result.ledger_sequence_start,
                        "ledgerSequenceEnd": result.ledger_sequence_end,
                    })
                revision = layout_revision(self._trees())
        except MaterialValidationError as exc:
            raise WorkspaceHostError("layout_candidate_invalid", f"物料权威拒绝布局候选：{exc}") from exc
        except MaterialConflictError as exc:
            raise WorkspaceHostError(
                "layout_apply_conflict", f"物料状态冲突，布局未写入：{exc}", details={"previewId": preview_id}
            ) from exc
        except MaterialsServiceError as exc:
            raise WorkspaceHostError("layout_apply_failed", f"布局写入失败：{exc}") from exc
        return {
            "schemaVersion": LAYOUT_APPLY_SCHEMA,
            "previewId": preview_id,
            "rootMaterialUuid": self.root_material_uuid,
            "sourceRevision": actual_revision,
            "revision": revision,
            "changedSourceNodeIds": changed,
            "changeSet": normalized,
            "diagnostics": diagnostics,
            "applied": applied,
        }


# ── 纯函数 ──────────────────────────────────────────────────────────


def layout_revision(trees: Sequence[MaterialTreeRead]) -> str:
    """布局修订：范围内每棵树的快照状态哈希按根排序后汇总。"""

    digest = canonical_hash(
        [
            {"root_material_uuid": tree.root_material_uuid, "state_hash": tree.state_hash}
            for tree in sorted(trees, key=lambda item: item.root_material_uuid)
        ]
    )
    return f"sha256:{digest}"


def _preview_identity(source_revision: str, candidate_revision: str, change_set: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        _canonical_json(
            {"sourceRevision": source_revision, "candidateRevision": candidate_revision, "changeSet": change_set}
        )
    ).hexdigest()


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _require_revision(expected: object, actual: str) -> None:
    if not isinstance(expected, str) or not expected.strip():
        raise WorkspaceHostError("invalid_request", "expectedRevision 必须是非空字符串")
    if expected != actual:
        raise WorkspaceHostError(
            "layout_revision_conflict",
            "布局 revision 已变化，拒绝覆盖",
            details={"expected": expected, "actual": actual},
        )


def _finite_or_zero(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value) if math.isfinite(value) else 0.0


def _layout_node(node: MaterialAggregateRead, root: str) -> dict[str, Any]:
    material = node.material
    position = node.position
    rendering = material.config.get("rendering") if isinstance(material.config, Mapping) else None
    asset_ref = rendering.get("model") if isinstance(rendering, Mapping) else None
    return {
        "sourceNodeId": material.material_uuid,
        "resourceId": material.resource_id,
        "name": material.display_name or material.name,
        "sourceType": material.resource_type or "resource",
        "positionMm": [
            _finite_or_zero(position.position_x),
            _finite_or_zero(position.position_y),
            _finite_or_zero(position.position_z),
        ],
        "rotationDegXYZ": [
            _finite_or_zero(position.rotation_x),
            _finite_or_zero(position.rotation_y),
            _finite_or_zero(position.rotation_z),
        ],
        "dimensionsMm": [
            _finite_or_zero(position.size_width),
            _finite_or_zero(position.size_height),
            _finite_or_zero(position.size_depth),
        ],
        "parentSourceNodeId": material.parent_material_uuid,
        "rootMaterialUuid": root,
        "assetRef": deepcopy(dict(asset_ref)) if isinstance(asset_ref, Mapping) else None,
    }


def _layout_nodes(trees: Sequence[MaterialTreeRead]) -> list[dict[str, Any]]:
    return [_layout_node(node, tree.root_material_uuid) for tree in trees for node in tree.nodes]


def _resolve_source(trees: Sequence[MaterialTreeRead], value: str, index: int) -> str:
    """``sourceNodeId`` → ``material_uuid``：先按 uuid，再按范围内唯一的 resource_id。"""

    by_uuid = {node.material.material_uuid for tree in trees for node in tree.nodes}
    if value in by_uuid:
        return value
    matches = [node.material.material_uuid for tree in trees for node in tree.nodes if node.material.resource_id == value]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise WorkspaceHostError(
            "layout_source_ambiguous",
            f"nodes[{index}].sourceNodeId 在布局范围内对应多个物料，请改用 material_uuid：{value}",
            details={"candidates": sorted(matches)},
        )
    raise WorkspaceHostError("layout_source_not_found", f"布局节点不存在：{value}")


def _normalize_change_set(
    value: object, trees: Sequence[MaterialTreeRead], workspace: Optional[Path]
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise WorkspaceHostError("layout_change_invalid", "布局 change set 必须是 object")
    if value.get("schemaVersion", LAYOUT_CHANGE_SCHEMA) != LAYOUT_CHANGE_SCHEMA:
        raise WorkspaceHostError("layout_change_invalid", "布局 change set schemaVersion 无效")
    raw_nodes = value.get("nodes")
    if not isinstance(raw_nodes, Sequence) or isinstance(raw_nodes, (str, bytes)):
        raise WorkspaceHostError("layout_change_invalid", "布局 change set.nodes 必须是数组")
    nodes: list[dict[str, Any]] = []
    identities: set[str] = set()
    for index, raw_node in enumerate(raw_nodes):
        if not isinstance(raw_node, Mapping):
            raise WorkspaceHostError("layout_change_invalid", f"nodes[{index}] 必须是 object")
        requested = str(raw_node.get("sourceNodeId") or "").strip()
        if not requested:
            raise WorkspaceHostError("layout_change_invalid", f"nodes[{index}].sourceNodeId 缺失或重复")
        source_id = _resolve_source(trees, requested, index)
        if source_id in identities:
            raise WorkspaceHostError("layout_change_invalid", f"nodes[{index}].sourceNodeId 缺失或重复")
        identities.add(source_id)
        node: dict[str, Any] = {"sourceNodeId": source_id}
        if "positionMm" in raw_node:
            node["positionMm"] = _vector(raw_node["positionMm"], f"nodes[{index}].positionMm")
        if "rotationDegXYZ" in raw_node:
            node["rotationDegXYZ"] = _vector(raw_node["rotationDegXYZ"], f"nodes[{index}].rotationDegXYZ")
        if "assetRef" in raw_node:
            node["assetRef"] = _asset_ref(raw_node["assetRef"], workspace, index)
        if len(node) == 1:
            raise WorkspaceHostError("layout_change_invalid", f"nodes[{index}] 没有布局变更")
        nodes.append(node)
    result: dict[str, Any] = {"schemaVersion": LAYOUT_CHANGE_SCHEMA, "nodes": nodes}
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


def _asset_ref(value: object, workspace: Optional[Path], index: int) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise WorkspaceHostError("layout_change_invalid", f"nodes[{index}].assetRef 必须是 object")
    path = str(value.get("path") or "").strip()
    if not path:
        raise WorkspaceHostError("layout_change_invalid", f"nodes[{index}].assetRef.path 缺失")
    if not path.startswith(ASSET_ROUTE_PREFIX) or "\\" in path or ".." in PurePosixPath(path).parts:
        raise WorkspaceHostError("layout_asset_invalid", "模板资产必须使用 /api/v1/material-models/ 公共路由")
    source_path = str(value.get("sourcePath") or "").strip()
    if source_path:
        if workspace is None:
            raise WorkspaceHostError("layout_asset_not_found", f"没有工作目录，无法核对模板资产：{source_path}")
        candidate = (workspace / source_path).resolve()
        if not candidate.is_relative_to(workspace) or not candidate.is_file():
            raise WorkspaceHostError("layout_asset_not_found", f"模板资产不在工作区或不存在：{source_path}")
    result: dict[str, Any] = {"path": path}
    if source_path:
        result["sourcePath"] = source_path
    for key in ("format", "meshDir", "macro"):
        if value.get(key) is not None:
            result[key] = str(value[key])
    return result


def _view(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise WorkspaceHostError("layout_change_invalid", "view 必须是 object")
    mode = str(value.get("mode") or "2.5d")
    if mode not in {"2d", "2.5d", "3d", "split"}:
        raise WorkspaceHostError("layout_change_invalid", "view.mode 无效")
    camera = str(value.get("cameraPreset") or "default")
    if camera not in {"default", "top"}:
        raise WorkspaceHostError("layout_change_invalid", "view.cameraPreset 无效")
    result: dict[str, Any] = {"mode": mode, "cameraPreset": camera}
    viewport = value.get("viewport")
    if viewport is not None:
        if not isinstance(viewport, Mapping):
            raise WorkspaceHostError("layout_change_invalid", "view.viewport 必须是 object")
        width = viewport.get("width")
        height = viewport.get("height")
        if isinstance(width, bool) or not isinstance(width, int) or not 320 <= width <= 4096:
            raise WorkspaceHostError("layout_change_invalid", "view.viewport.width 无效")
        if isinstance(height, bool) or not isinstance(height, int) or not 240 <= height <= 4096:
            raise WorkspaceHostError("layout_change_invalid", "view.viewport.height 无效")
        result["viewport"] = {"width": width, "height": height}
    return result


def _apply_changes(
    trees: Sequence[MaterialTreeRead], change_set: object
) -> tuple[list[MaterialTreeRead], list[str]]:
    """把规范化变更集应用到树副本；返回候选树（state_hash 为候选快照哈希）与变化节点。"""

    if not isinstance(change_set, Mapping) or not isinstance(change_set.get("nodes"), list):
        raise WorkspaceHostError("layout_preview_invalid", "preview change set 无效")
    changes = {}
    for change in change_set["nodes"]:
        if not isinstance(change, Mapping) or not change.get("sourceNodeId"):
            raise WorkspaceHostError("layout_preview_invalid", "preview change set 无效")
        changes[str(change["sourceNodeId"])] = change
    known = {node.material.material_uuid for tree in trees for node in tree.nodes}
    missing = sorted(set(changes) - known)
    if missing:
        raise WorkspaceHostError("layout_source_not_found", f"布局节点不存在：{missing[0]}")
    candidate: list[MaterialTreeRead] = []
    for tree in trees:
        nodes = []
        touched = False
        for node in tree.nodes:
            change = changes.get(node.material.material_uuid)
            if change is None:
                nodes.append(node)
                continue
            touched = True
            nodes.append(_changed_node(node, change))
        if not touched:
            candidate.append(tree)
            continue
        snapshot = MaterialSnapshot(root_material_uuid=tree.root_material_uuid, nodes=nodes)
        candidate.append(tree.model_copy(update={"nodes": nodes, "state_hash": snapshot_state_hash(snapshot)}))
    order = [str(change["sourceNodeId"]) for change in change_set["nodes"]]
    return candidate, order


def _changed_node(node: MaterialAggregateRead, change: Mapping[str, Any]) -> MaterialAggregateRead:
    position_update: dict[str, Any] = {}
    if "positionMm" in change:
        x, y, z = (float(value) for value in change["positionMm"])
        position_update.update(
            position_x=x, position_y=y, position_z=z, position3d_x=x, position3d_y=y, position3d_z=z
        )
    if "rotationDegXYZ" in change:
        x, y, z = (float(value) for value in change["rotationDegXYZ"])
        position_update.update(rotation_x=x, rotation_y=y, rotation_z=z)
    update: dict[str, Any] = {}
    if position_update:
        update["position"] = node.position.model_copy(update=position_update)
    if "assetRef" in change:
        asset = {key: value for key, value in dict(change["assetRef"]).items() if key != "sourcePath"}
        config = deepcopy(dict(node.material.config))
        rendering = dict(config.get("rendering") or {}) if isinstance(config.get("rendering"), Mapping) else {}
        rendering["model"] = asset
        config["rendering"] = rendering
        update["material"] = node.material.model_copy(update={"config": config})
    return node.model_copy(update=update)


def _layout_diagnostics(nodes: Sequence[Mapping[str, Any]], changed: Sequence[str]) -> list[dict[str, Any]]:
    """V1 同口径：变化的设备缺尺寸告警；顶层物理节点 XY 外包相交告警（都不阻塞）。"""

    by_id = {node["sourceNodeId"]: node for node in nodes}
    diagnostics: list[dict[str, Any]] = []
    for source_id in changed:
        node = by_id.get(source_id)
        if node is None:
            continue
        if node["sourceType"] == "device" and any(value <= 0 for value in node["dimensionsMm"]):
            diagnostics.append({
                "severity": "warning",
                "code": "layout_dimensions_missing",
                "sourceNodeId": source_id,
                "message": "设备缺少完整外包尺寸，无法证明碰撞约束",
            })
    changed_set = set(changed)
    physical = [
        node for node in nodes
        if node["sourceType"] in _PHYSICAL_TYPES and all(value > 0 for value in node["dimensionsMm"])
    ]
    for index, left in enumerate(physical):
        for right in physical[index + 1:]:
            if not ({left["sourceNodeId"], right["sourceNodeId"]} & changed_set):
                continue
            if left["parentSourceNodeId"] or right["parentSourceNodeId"]:
                continue
            if _overlaps_xy(left, right):
                diagnostics.append({
                    "severity": "warning",
                    "code": "layout_bounds_overlap",
                    "sourceNodeIds": [left["sourceNodeId"], right["sourceNodeId"]],
                    "message": "设备外包在 XY 平面相交，请结合真实模型截图复核",
                })
    return diagnostics


def _overlaps_xy(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    lx, ly, _ = left["positionMm"]
    lw, lh, _ = left["dimensionsMm"]
    rx, ry, _ = right["positionMm"]
    rw, rh, _ = right["dimensionsMm"]
    return lx < rx + rw and rx < lx + lw and ly < ry + rh and ry < ly + lh


__all__ = [
    "LAYOUT_APPLY_SCHEMA",
    "LAYOUT_CHANGE_SCHEMA",
    "LAYOUT_PREVIEW_SCHEMA",
    "MaterialLayout",
    "layout_revision",
]
