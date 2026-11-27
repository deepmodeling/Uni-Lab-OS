"""从设备包部署图建立唯一的 OS 资产场景，不包含领域模型或规划算法。"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping

from . import scene_assembly as assembly


def assemble_document(document: Mapping[str, Any], *, asset_root: str | Path):
    """解析现有 scene.node_bindings；资产局部帧在节点变换之后应用一次。"""
    import numpy as np
    from pxr import Gf, UsdGeom

    root = Path(asset_root).resolve()
    specification = document["scene"]
    graph = assembly.read_scene_graph(document)
    entries = specification["node_bindings"]
    if set(entries) != set(graph.nodes):
        raise ValueError("Every graph node requires one explicit asset/logical binding")
    bindings, assets, transforms = {}, {}, {}
    for identity, entry in entries.items():
        kind = entry["kind"]
        if kind == "logical":
            bindings[identity] = assembly.ClassBinding("logical", reason=entry.get("reason", "logical"))
            continue
        if kind != "model":
            raise ValueError("Missing scene geometry: " + identity)
        path = (root / entry["path"]).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError("Asset outside package or missing: " + str(path))
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if entry.get("sha256", digest) != digest:
            raise ValueError("Asset digest differs: " + identity)
        matrix = np.asarray(entry.get("asset_transform", np.eye(4)), dtype=float)
        if (matrix.shape != (4,4) or not np.isfinite(matrix).all()
                or not np.allclose(matrix[3], [0,0,0,1], atol=1e-9)
                or not np.allclose(matrix[:3,:3].T @ matrix[:3,:3], np.eye(3), atol=1e-6)
                or np.linalg.det(matrix[:3,:3]) < 0):
            raise ValueError("Asset transform must be rigid: " + identity)
        transforms[identity] = matrix
        bindings[identity] = assembly.ClassBinding(
            "model", family=entry["family"],
            asset=assembly.Asset(str(path), entry["prim_path"], frame="asset_frame"))
        assets[identity] = {"path": str(path), "prim_path": entry["prim_path"], "sha256": digest}
    plan = assembly.plan_assembly(graph, class_bindings={}, node_bindings=bindings,
                                  calibration=assembly.EMPTY_CALIBRATION,
                                  root_path=specification["root_path"])
    stage = assembly.build_stage(plan)
    for identity, matrix in transforms.items():
        model = stage.GetPrimAtPath(plan.placements[identity].prim_path + "/Model")
        if not model or (not model.GetChildren() and not model.IsA(UsdGeom.Gprim)):
            raise ValueError("Empty asset reference: " + identity)
        if not np.array_equal(matrix, np.eye(4)):
            UsdGeom.Xformable(model).AddTransformOp().Set(Gf.Matrix4d(*matrix.T.ravel().tolist()))
        role = entries[identity].get("collision_role", "static_world")
        model.SetCustomDataByKey("planning_role", role)
        model.SetCustomDataByKey("asset_sha256", assets[identity]["sha256"])
    receipt = {"schema": "unilab.scene-assembly/v1", "counts": plan.counts(),
               "assets": assets, "authority": "OS deployment graph",
               "physics_qualified": False}
    return stage, plan, receipt
