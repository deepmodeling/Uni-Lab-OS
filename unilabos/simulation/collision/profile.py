"""读取碰撞 profile（``planning-geometry.json`` + 同目录 ``collision.npz``）。

格式与 :func:`unilabos.simulation.collision_snapshot.export_collision_snapshot` 及资产管线生成的
``collision/{source,exact,outer}`` 相同：米制、列向量矩阵、刚体局部坐标的三角网格与基本体。
读取时核对元数据与数组摘要；``lazy=True`` 时只在对象被取用时才解码网格（大场景加载的主要收益）。

对象和图元类型可替换（``body_type`` / ``geometry_type`` / ``rows_type``），机器人领域库可以传入
自己的合同类，从而与 OS 共用同一份读取与校验逻辑。
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from collections.abc import Mapping
from pathlib import Path

import numpy as np

from .geometry import IDENTITY, Body, Geometry, PackedRows


class _ArrayArchive:
    def __init__(self, archive):
        self.archive = archive

    def __getitem__(self, key):
        # NpzFile 的成员列表查询在数万个小网格时是二次复杂度；直接查 ZipFile 中央目录，数值与类型不变。
        with self.archive.open(key + ".npy") as source:
            return np.lib.format.read_array(source, allow_pickle=False)


class _LazyObjects(Mapping):
    """摘要已核对的不可变数组包；只物化被请求的对象。"""

    def __init__(self, arrays, groups, build):
        self.arrays, self.groups, self.build, self.cache = arrays, groups, build, {}
        self.source_stat = (arrays.stat().st_size, arrays.stat().st_mtime_ns)

    def __iter__(self):
        return iter(self.groups)

    def __len__(self):
        return len(self.groups)

    def __getitem__(self, identity):
        if identity not in self.cache:
            if (self.arrays.stat().st_size, self.arrays.stat().st_mtime_ns) != self.source_stat:
                raise ValueError("CAD array changed after digest verification（数组在摘要核对后被改动）")
            body, shapes = self.groups[identity]
            with zipfile.ZipFile(self.arrays) as archive:
                self.cache[identity] = self.build(identity, body, shapes, _ArrayArchive(archive))
        return self.cache[identity]


class CollisionProfile:
    def __init__(self, geometry_file, *, expected_sha256=None, lazy=False,
                 body_type=Body, geometry_type=Geometry, rows_type=PackedRows):
        self.body_type, self.geometry_type, self.rows_type = body_type, geometry_type, rows_type
        self.path = Path(geometry_file)
        raw = self.path.read_bytes()
        self.digest = hashlib.sha256(raw).hexdigest()
        if expected_sha256 is not None and self.digest != expected_sha256:
            raise ValueError("CAD metadata digest differs（元数据摘要不符）")
        self.metadata = json.loads(raw)
        data = self.metadata
        if data.get("status") != "exported" or data.get("unsupported"):
            raise ValueError("Incomplete CAD export（导出不完整）")
        if data.get("units") != "metres" or data.get("matrix_convention") != "column_vectors":
            raise ValueError("CAD requires metres and column-vector transforms（需要米制与列向量矩阵）")
        arrays = self.path.parent / data["arrays"]
        if arrays.resolve().parent != self.path.parent.resolve():
            raise ValueError("CAD arrays must be a sibling artifact（数组须与元数据同目录）")
        if hashlib.sha256(arrays.read_bytes()).hexdigest() != data["arrays_sha256"]:
            raise ValueError("CAD array digest differs（数组摘要不符）")
        self.bodies = {body["path"]: body for body in data["bodies"]}
        if len(self.bodies) != len(data["bodies"]):
            raise ValueError("Duplicate CAD body（刚体重复）")
        self.object_bodies = {}
        declarations = {}
        for body in data["bodies"]:
            groups = {}
            for shape in body["shapes"]:
                identity = shape.get("collision_object_id")
                if identity is None:
                    identity = shape["prim"] if body["role"] == "static_world" else body["path"]
                if not isinstance(identity, str) or not identity:
                    raise ValueError("Invalid collision object identity（碰撞对象身份无效）")
                groups.setdefault(identity, []).append(shape)
            for identity, shapes in groups.items():
                if identity in declarations:
                    raise ValueError("Duplicate CAD object: " + identity)
                declarations[identity] = (body, shapes)
                self.object_bodies[identity] = body["path"]
        catalog = _LazyObjects(arrays, declarations, self._build)
        if lazy:
            self.objects = catalog
        else:
            self.objects = {}
            with zipfile.ZipFile(arrays) as archive:
                indexed = _ArrayArchive(archive)
                for identity, (body, shapes) in declarations.items():
                    self.objects[identity] = self._build(identity, body, shapes, indexed)

    def _build(self, identity, body, shapes, arrays):
        rows = [self.shape(s, arrays) for s in shapes]
        return self.body_type(identity, body["world_from_body_initial"], tuple(g for g, _ in rows), role=body["role"],
                              geometry_poses=tuple(p for _, p in rows))

    def shape(self, shape, arrays):
        kind = shape["type"]
        if kind == "Mesh":
            if shape.get("point_frame") != "body_local":
                raise ValueError("Mesh points must already be body-local（网格点须为刚体局部坐标）")
            vertices = arrays[shape["vertices"]]
            counts = arrays[shape["face_counts"]]
            indices = arrays[shape["face_indices"]]
            holes = set(shape.get("hole_faces", []))
            if sum(counts) != len(indices):
                raise ValueError("Invalid face topology（面拓扑无效）")
            if np.any(counts != 3):
                raise ValueError("Nontriangular source collider（源碰撞体不是三角网格）")
            triangles = indices.reshape(-1, 3)
            if holes:
                if min(holes) < 0 or max(holes) >= len(counts):
                    raise ValueError("Invalid hole face（孔洞面索引无效）")
                mask = np.ones(len(counts), dtype=bool)
                mask[list(holes)] = False
                triangles = triangles[mask]
            return self.geometry_type("mesh",
                                      vertices=self.rows_type(np.asarray(vertices, dtype="<f8").tobytes(), "d"),
                                      triangles=self.rows_type(np.asarray(triangles, dtype="<i8").tobytes(), "q")), IDENTITY
        matrix = np.array(shape["body_from_shape"], dtype=float)
        scale = np.linalg.norm(matrix[:3, :3], axis=0)
        if np.any(scale <= 0):
            raise ValueError("Invalid primitive scale（基本体缩放无效）")
        matrix[:3, :3] /= scale
        if not np.allclose(matrix[:3, :3].T @ matrix[:3, :3], np.eye(3), atol=1e-7):
            raise ValueError("Sheared primitive requires exact mesh（剪切的基本体须改用网格）")
        params = shape["parameters"]
        if kind == "Cube":
            geometry = self.geometry_type("box", size_m=tuple(scale * params["size"]))
        elif kind == "Cylinder":
            axis = {"X": 0, "Y": 1, "Z": 2}[params["axis"]]
            radial = [i for i in range(3) if i != axis]
            if abs(scale[radial[0]] - scale[radial[1]]) > 1e-9:
                raise ValueError("Elliptical cylinder requires exact mesh（椭圆柱须改用网格）")
            alignment = np.eye(4)
            if axis == 0:
                alignment[:3, :3] = [[0, 0, 1], [0, 1, 0], [-1, 0, 0]]
            if axis == 1:
                alignment[:3, :3] = [[1, 0, 0], [0, 0, 1], [0, -1, 0]]
            matrix = matrix @ alignment
            geometry = self.geometry_type("cylinder", radius_m=params["radius"] * scale[radial[0]],
                                          length_m=params["height"] * scale[axis])
        else:
            raise ValueError("Unsupported exact CAD shape（不支持的形状）: " + kind)
        return geometry, tuple(map(tuple, matrix.tolist()))


__all__ = ["CollisionProfile"]
