"""从 OS 部署图装配整站碰撞场景：每个节点的世界位姿 × 资产局部帧 → 碰撞实例。

位姿与 :mod:`unilabos.simulation.scene_assembly` 同一口径（部署图毫米、z-up、父链累加、行向量朝向），
这里换算成米制列向量矩阵。资产的 ``asset_transform`` 在节点变换之后应用一次（与
:func:`unilabos.simulation.scene_document.assemble_document` 一致）。碰撞几何保存在资产局部坐标，
所以同一资产的多个实例共享几何，各自只有位姿不同。
"""

from __future__ import annotations

from collections import ChainMap
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .instance import CollisionInstance
from .profile import CollisionProfile

MM_PER_M = 1000.0


def node_world_poses(document: Mapping) -> dict[str, np.ndarray]:
    """部署图（或含 ``nodes`` 的图）→ ``{节点: world_from_node}``（米、列向量）。不含校准偏移。"""
    from unilabos.simulation import scene_assembly as assembly

    graph = assembly.read_scene_graph(document)
    poses = {}
    for identity, (rotation_row, position_mm) in assembly._world_frames(graph).items():  # noqa: SLF001 - 同一口径
        m = np.eye(4)
        m[:3, :3] = np.asarray(rotation_row, dtype=float).T   # 行向量朝向 → 列向量
        m[:3, 3] = np.asarray(position_mm, dtype=float) / MM_PER_M
        poses[identity] = m
    return poses


@dataclass
class StationCollisionScene:
    """整站碰撞场景：外包对象列表 + 精确对象映射（按需加载） + 每个节点的实例。"""

    outer_objects: list
    exact_objects: Mapping
    instances: dict[str, tuple[CollisionInstance, CollisionInstance]]
    world_from_asset: dict[str, np.ndarray]

    def checker(self):
        """三级检查内核（需要 python-fcl）。"""
        from .refined import RefinedChecker

        return RefinedChecker(self.outer_objects, ChainMap({}, self.exact_objects))

    def object_ids(self, node: str) -> list[str]:
        return list(self.instances[node][0])

    def body_of(self, node: str, object_id: str) -> str:
        return self.instances[node][0].object_bodies[object_id]


def station_scene(document: Mapping, profiles: Mapping[str, tuple], *, asset_transforms: Mapping[str, object] | None = None,
                  profile_type=CollisionProfile, extra_outer=(), extra_exact: Mapping | None = None) -> StationCollisionScene:
    """``profiles``：``{节点: (outer 的 planning-geometry.json, exact 的 planning-geometry.json[, 期望摘要对])}``。

    只为给出 profile 的节点建实例；没有碰撞几何的节点不会被悄悄当成空气——调用方应在验收中列出它们。
    ``asset_transforms`` 缺省取 ``document["scene"]["node_bindings"][节点]["asset_transform"]``。
    ``extra_outer`` / ``extra_exact`` 用来并入不在部署图里的对象（例如机器人连杆的 URDF 碰撞体）。
    """
    world = node_world_poses(document)
    bindings = ((document.get("scene") or {}).get("node_bindings") or {}) if isinstance(document, Mapping) else {}
    outer_objects, exact_objects, instances, placements = list(extra_outer), dict(extra_exact or {}), {}, {}
    for node, entry in profiles.items():
        if node not in world:
            raise ValueError(f"节点 {node} 不在部署图里")
        outer_path, exact_path, *digests = entry
        digests = (digests[0] if digests else (None, None))
        outer = profile_type(Path(outer_path), expected_sha256=digests[0])
        exact = profile_type(Path(exact_path), expected_sha256=digests[1], lazy=True)
        if outer.metadata.get("source_geometry_sha256") != exact.metadata.get("source_geometry_sha256"):
            raise ValueError(f"节点 {node} 的 outer 与 exact 来自不同来源（source_geometry_sha256 不一致）")
        local = np.asarray((asset_transforms or {}).get(node, (bindings.get(node) or {}).get("asset_transform", np.eye(4))),
                           dtype=float)
        placement = world[node] @ local
        oi, ei = CollisionInstance(outer, node, placement), CollisionInstance(exact, node, placement)
        clash = set(oi) & ({o.object_id for o in outer_objects} | set(exact_objects))
        if clash:
            raise ValueError(f"碰撞对象身份重复：{sorted(clash)[:3]}")
        outer_objects.extend(oi.values())
        exact_objects.update(ei)
        instances[node] = (oi, ei)
        placements[node] = placement
    return StationCollisionScene(outer_objects, exact_objects, instances, placements)


__all__ = ["node_world_poses", "station_scene", "StationCollisionScene"]
