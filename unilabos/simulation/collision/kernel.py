"""碰撞内核：显式“运动体–场景 / 运动体–运动体”对的快照检查（python-fcl）。

与机构种类无关：机械臂连杆、导轨滑块、龙门、门盖、上料机构、夹着的物料都只是带世界位姿的刚体。
不规划、不读库存、不推进时钟、不碰硬件；每个实例的访问须由调用方串行化。

检查分级（:meth:`SceneChecker.check`）：
1. 刚体包围盒剪枝（向量化）；
2. 图元包围盒剪枝（向量化，只对通过第 1 级的对）；
3. FCL 窄相位，只对两级包围盒都通过的图元对更新原生位姿并调用。

只豁免调用方显式给出的对象对；同一法兰下的兄弟附件不自动豁免；未知身份、自对、重复豁免、
缺运动体位姿一律报错。最小间隙（:meth:`SceneChecker.clearance`）是单独的查询，不进入 ``check`` 的热路径。
"""

from __future__ import annotations

import hashlib
import math
import time
from dataclasses import dataclass

import numpy as np


def _rows(value, code):
    if hasattr(value, "data") and getattr(value, "code", None) == code:
        return np.frombuffer(value.data, dtype="<f8" if code == "d" else "<i8").reshape(-1, 3)
    return np.asarray(value, dtype=float if code == "d" else np.int64)


def _pose(value):
    matrix = np.asarray(value, dtype=float)
    if matrix.shape != (4, 4) or not np.isfinite(matrix).all() or not np.allclose(matrix[3], [0, 0, 0, 1], atol=1e-10, rtol=0):
        raise ValueError("Expected finite homogeneous pose（需要有限的齐次位姿）")
    r = matrix[:3, :3]
    if np.max(abs(r.T @ r - np.eye(3))) > 1e-6 or np.linalg.det(r) <= 0:
        raise ValueError("Expected rigid pose（需要刚体位姿）")
    # 与原生刚体变换消费者一致：float32 测量位姿的舍入误差按正交化处理。
    if np.max(abs(r.T @ r - np.eye(3))) > 1e-12:
        u, _, vt = np.linalg.svd(r)
        matrix = matrix.copy()
        matrix[:3, :3] = u @ vt
    return matrix


@dataclass
class _Shape:
    native: object
    local_pose: np.ndarray
    lower: np.ndarray
    upper: np.ndarray
    revision: int = -1


class SceneChecker:
    def __init__(self, objects, *, geometry_cache=None):
        import fcl

        objects = tuple(objects)
        self.fcl = fcl
        self.ids = tuple(o.object_id for o in objects)
        if len(set(self.ids)) != len(self.ids) or not self.ids:
            raise ValueError("Nonempty unique object identities required（需要非空且不重复的对象身份）")
        self.index = {name: i for i, name in enumerate(self.ids)}
        self.shapes = []
        self.poses = {}
        self.lower = np.zeros((len(self.ids), 3))
        self.upper = self.lower.copy()
        self._geometry_cache = {} if geometry_cache is None else geometry_cache
        self._request = fcl.CollisionRequest(num_max_contacts=1, enable_contact=False)
        self._policy_key = None
        self._pairs = None
        self.shape_lower = {}
        self.shape_upper = {}
        self.centers = []
        self.extents = []
        self.local_rotations = []
        self.local_translations = []
        self.revisions = np.zeros(len(self.ids), dtype=np.int64)
        for obj in objects:
            entries = []
            for i, g in enumerate(obj.geometry):
                native, low, high = self._geometry(g)
                local = _pose(obj.geometry_poses[i]) if obj.geometry_poses else np.eye(4)
                entries.append(_Shape(fcl.CollisionObject(native), local, low, high))
            self.shapes.append(entries)
            self.centers.append(np.array([(s.lower + s.upper) * 0.5 for s in entries]))
            self.extents.append(np.array([(s.upper - s.lower) * 0.5 for s in entries]))
            self.local_rotations.append(np.array([s.local_pose[:3, :3] for s in entries]))
            self.local_translations.append(np.array([s.local_pose[:3, 3] for s in entries]))
            self._update(obj.object_id, obj.world_pose)

    def _geometry(self, g):
        fcl = self.fcl
        if g.kind == "mesh":
            if g.vertices is None or g.triangles is None:
                raise ValueError("In-memory mesh required（需要内存三角网格）")
            v = _rows(g.vertices, "d") * np.asarray(g.mesh_scale)
            t = _rows(g.triangles, "q")
            if not len(v) or not len(t) or not np.isfinite(v).all() or t.min() < 0 or t.max() >= len(v):
                raise ValueError("Invalid triangle mesh（三角网格无效）")
            key = hashlib.sha256(np.asarray([len(v), len(t)], dtype="<i8").tobytes() + v.tobytes() + t.tobytes()).hexdigest()
            if key in self._geometry_cache:
                return self._geometry_cache[key]
            model = fcl.BVHModel()
            model.beginModel(len(v), len(t))
            model.addSubModel(v, t)
            model.endModel()
            value = (model, v.min(0), v.max(0))
            self._geometry_cache[key] = value
            return value
        if g.kind == "box":
            size = np.asarray(g.size_m)
            return fcl.Box(*size), -size * 0.5, size * 0.5
        if g.kind == "cylinder":
            ext = np.array([g.radius_m, g.radius_m, g.length_m * 0.5])
            return fcl.Cylinder(g.radius_m, g.length_m), -ext, ext
        raise ValueError("Unsupported FCL geometry（FCL 不支持的图元）: " + g.kind)

    def _update(self, identity, value):
        m = _pose(value)
        if identity in self.poses and np.array_equal(m, self.poses[identity]):
            return
        i = self.index[identity]
        rotations = np.einsum("ij,njk->nik", m[:3, :3], self.local_rotations[i])
        translations = self.local_translations[i] @ m[:3, :3].T + m[:3, 3]
        centers = np.einsum("nij,nj->ni", rotations, self.centers[i]) + translations
        extents = np.einsum("nij,nj->ni", np.abs(rotations), self.extents[i]) + 1e-10
        self.shape_lower[i] = centers - extents
        self.shape_upper[i] = centers + extents
        self.lower[i] = self.shape_lower[i].min(axis=0)
        self.upper[i] = self.shape_upper[i].max(axis=0)
        self.poses[identity] = m.copy()
        self.revisions[i] += 1

    def _native_at(self, index, shape_index):
        # 网格只在两级包围盒都通过后才更新原生位姿：远处成千上万的小件只留在 NumPy 里。
        shape = self.shapes[index][shape_index]
        if shape.revision != self.revisions[index]:
            world = self.poses[self.ids[index]] @ shape.local_pose
            shape.native.setTransform(self.fcl.Transform(world[:3, :3], world[:3, 3]))
            shape.revision = self.revisions[index]
        return shape.native

    def _policy(self, moving_ids, allowed_pairs):
        if isinstance(moving_ids, (str, bytes)):
            raise ValueError("Moving identities must be a sequence of ids（运动体身份必须是序列）")
        moving = tuple(moving_ids)
        if not moving or len(set(moving)) != len(moving) or set(moving) - self.index.keys():
            raise ValueError("Invalid moving identities（运动体身份无效）")
        exclusions = set()
        for pair in allowed_pairs:
            if not isinstance(pair, (tuple, list)) or len(pair) != 2 or pair[0] == pair[1] or set(pair) - self.index.keys():
                raise ValueError("Invalid allowed collision pair（允许接触的对象对无效）")
            canonical = tuple(sorted(pair))
            if canonical in exclusions:
                raise ValueError("Duplicate allowed collision pair（允许接触的对象对重复）")
            exclusions.add(canonical)
        key = (tuple(sorted(moving)), tuple(sorted(exclusions)))
        if key != self._policy_key:
            moving_indices = sorted(self.index[x] for x in moving)
            members = set(moving_indices)
            pairs = [(i, j) for i in moving_indices for j in range(len(self.ids)) if i != j and (j not in members or i < j)
                     and tuple(sorted((self.ids[i], self.ids[j]))) not in exclusions]
            self._pairs = np.asarray(pairs, dtype=np.int64).reshape(-1, 2)
            self._policy_key = key
        return moving

    def check(self, poses, *, moving_ids, allowed_pairs=(), stop_at_first=True):
        """每个运动体都要给新位姿；静态体位姿变化显式给出即生效。不在给定状态之间插值。"""
        started = time.perf_counter()
        moving = self._policy(moving_ids, allowed_pairs)
        if set(poses) - self.index.keys() or set(moving) - set(poses):
            raise ValueError("Missing/unknown object poses（缺少或未知的对象位姿）")
        for identity, pose in poses.items():
            self._update(identity, pose)
        pairs = self._pairs
        if len(pairs):
            a, b = pairs.T
            mask = np.all(self.lower[a] <= self.upper[b], axis=1) & np.all(self.lower[b] <= self.upper[a], axis=1)
            candidates = pairs[mask]
        else:
            candidates = pairs
        hits = []
        narrow = 0
        for i, j in candidates:
            found = False
            # 图元级剪枝向量化，避免 CAD 组合体（托盘里上千个三角块）的二次 Python 循环。
            left, right = (i, j) if len(self.shapes[i]) <= len(self.shapes[j]) else (j, i)
            for ai in range(len(self.shapes[left])):
                overlap = (np.all(self.shape_lower[left][ai] <= self.shape_upper[right], axis=1)
                           & np.all(self.shape_lower[right] <= self.shape_upper[left][ai], axis=1))
                for k in np.flatnonzero(overlap):
                    narrow += 1
                    result = self.fcl.CollisionResult()
                    self.fcl.collide(self._native_at(left, ai), self._native_at(right, k), self._request, result)
                    if result.is_collision:
                        found = True
                        break
                if found:
                    break
            if found:
                hits.append((self.ids[i], self.ids[j]))
                if stop_at_first:
                    break
        return dict(collision=bool(hits), pairs=hits, eligible_pairs=len(pairs), aabb_candidates=len(candidates),
                    narrow_phase_calls=narrow, wall_s=time.perf_counter() - started,
                    coverage="moving_scene_and_moving_moving; explicit pair exclusions", continuous_collision_proof=False)

    def clearance(self, *, moving_ids, allowed_pairs=(), margin_m=0.05, pairs=None):
        """当前位姿下的最小间隙：只算包围盒相距不超过 ``margin_m`` 的运动对（米）。

        返回 ``{"min_m", "pair", "evaluated_pairs", "distance_calls"}``；没有对落在 ``margin_m`` 内时
        ``min_m`` 为 None（含义是“大于 margin_m”，不是 0）。相交时 FCL 返回非正值，按 0 报告。
        ``pairs`` 可限定只算某些对象对。先调用 :meth:`check` 更新位姿。
        """
        if not math.isfinite(margin_m) or margin_m < 0:
            raise ValueError("margin_m 必须是非负有限值")
        self._policy(moving_ids, allowed_pairs)
        selected = self._pairs
        if pairs is not None:
            wanted = {tuple(sorted((self.index[a], self.index[b]))) for a, b in pairs}
            selected = np.asarray([p for p in selected if tuple(sorted(p)) in wanted], dtype=np.int64).reshape(-1, 2)
        best, best_pair, evaluated, calls = None, None, 0, 0
        request = self.fcl.DistanceRequest()
        for i, j in selected:
            if np.any(self.lower[i] - margin_m > self.upper[j]) or np.any(self.lower[j] - margin_m > self.upper[i]):
                continue
            evaluated += 1
            for ai in range(len(self.shapes[i])):
                near = (np.all(self.shape_lower[i][ai] - margin_m <= self.shape_upper[j], axis=1)
                        & np.all(self.shape_lower[j] - margin_m <= self.shape_upper[i][ai], axis=1))
                for k in np.flatnonzero(near):
                    calls += 1
                    result = self.fcl.DistanceResult()
                    distance = max(0.0, float(self.fcl.distance(self._native_at(i, ai), self._native_at(j, k), request, result)))
                    if best is None or distance < best:
                        best, best_pair = distance, (self.ids[i], self.ids[j])
        if best is not None and best > margin_m:
            best, best_pair = None, None
        return dict(min_m=best, pair=best_pair, evaluated_pairs=evaluated, distance_calls=calls, margin_m=margin_m)


def joint_samples(points, *, max_step_rad=0.002, max_samples=1000000):
    """保留全部已保存的内部点，并按固定步长细分每一段；越界或非有限值在产出前报错。"""
    values = np.asarray(points, dtype=float)
    if values.ndim != 2 or not len(values) or not values.shape[1] or not np.isfinite(values).all():
        raise ValueError("Invalid joint path（关节路径无效）")
    if not math.isfinite(max_step_rad) or max_step_rad <= 0 or type(max_samples) is not int or max_samples < 1:
        raise ValueError("Invalid sampling bounds（采样界限无效）")
    with np.errstate(over="ignore", invalid="ignore"):
        counts = np.maximum(1, np.ceil(np.max(abs(np.diff(values, axis=0)), axis=1) / max_step_rad))
    if not np.isfinite(counts).all() or np.any(counts > max_samples) or 1 + sum(counts) > max_samples:
        raise ValueError("Sampling budget exceeded; path not checked（采样数超出预算，路径未检查）")
    counts = counts.astype(int)
    yield values[0].tolist()
    for left, right, count in zip(values[:-1], values[1:], counts):
        for step in range(1, count + 1):
            yield (left + (right - left) * (step / count)).tolist()


__all__ = ["SceneChecker", "joint_samples"]
