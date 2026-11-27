"""把一份锁定的碰撞 profile 放到 OS 解析出的资产实例位姿上。

只做初始几何装配；运行时运动仍由调用方给内核新的世界位姿。几何共享，身份和位姿按实例独立。
"""

from collections.abc import Mapping
from dataclasses import replace

import numpy as np


class CollisionInstance(Mapping):
    def __init__(self, catalog, instance_id, world_from_asset):
        if not isinstance(instance_id, str) or not instance_id or "::" in instance_id:
            raise ValueError("Nonempty unambiguous instance id required（实例身份须非空且不含 ::）")
        pose = np.asarray(world_from_asset, dtype=float)
        if (pose.shape != (4, 4) or not np.isfinite(pose).all()
                or not np.allclose(pose[3], [0, 0, 0, 1], atol=1e-12, rtol=0)
                or not np.allclose(pose[:3, :3].T @ pose[:3, :3], np.eye(3), atol=1e-7, rtol=0)
                or np.linalg.det(pose[:3, :3]) <= 0):
            raise ValueError("Rigid metre-valued world_from_asset required（需要米制刚体位姿）")
        self.catalog = catalog
        self.instance_id = instance_id
        self.world_from_asset = pose.copy()
        self.source_ids = {instance_id + "::" + key: key for key in catalog.objects}
        self.object_bodies = {key: instance_id + "::" + catalog.object_bodies[source] for key, source in self.source_ids.items()}

    def __iter__(self):
        return iter(self.source_ids)

    def __len__(self):
        return len(self.source_ids)

    def __getitem__(self, identity):
        source = self.catalog.objects[self.source_ids[identity]]
        pose = self.world_from_asset @ np.asarray(source.world_pose)
        return replace(source, object_id=identity, world_pose=tuple(map(tuple, pose)))


__all__ = ["CollisionInstance"]
