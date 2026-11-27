"""三级碰撞检查：包围盒 → 简化（保守外包 outer）→ 精确（exact）+ 可选最小间隙。

* 第 1 级：刚体与图元包围盒剪枝（:class:`~unilabos.simulation.collision.kernel.SceneChecker` 内）；
* 第 2 级：保守外包（凸包、基本体、实体盒）上的 FCL 窄相位；
* 第 3 级：只对外包命中的对，按需加载该对的精确几何确认；外包命中本身不是碰撞结论。

精确几何的解码与构建计入第一次需要它的查询；之后同一对、同一位姿直接复用结果。
任何失败都直接报错，不静默退回外包结论。
"""

import time

import numpy as np

from .kernel import SceneChecker


class RefinedChecker:
    def __init__(self, outer_objects, exact_objects):
        self.outer = SceneChecker(outer_objects)
        if set(self.outer.ids) != set(exact_objects):
            raise ValueError("Exact/outer object identities differ（精确与外包的对象身份不一致）")
        self.exact_objects = exact_objects
        self.pair_checkers = {}
        self.geometry_cache = {}
        self.pair_pose_results = {}
        self.index = self.outer.index
        self.fcl = self.outer.fcl

    def _pair_checker(self, key):
        if key not in self.pair_checkers:
            self.pair_checkers[key] = SceneChecker([self.exact_objects[key[0]], self.exact_objects[key[1]]],
                                                   geometry_cache=self.geometry_cache)
            return self.pair_checkers[key], True
        return self.pair_checkers[key], False

    def check(self, poses, *, moving_ids, allowed_pairs=(), stop_at_first=True):
        started = time.perf_counter()
        broad = self.outer.check(poses, moving_ids=moving_ids, allowed_pairs=allowed_pairs, stop_at_first=False)
        hits = []
        narrow = 0
        cooked = 0
        reused = 0
        for a, b in broad["pairs"]:
            key = tuple(sorted((a, b)))
            checker, created = self._pair_checker(key)
            cooked += created
            poses_key = tuple(np.asarray(self.outer.poses[identity], dtype="<f8").tobytes() for identity in key)
            previous = self.pair_pose_results.get(key)
            if previous is not None and previous[0] == poses_key:
                collision = previous[1]
                reused += 1
            else:
                result = checker.check({a: self.outer.poses[a], b: self.outer.poses[b]}, moving_ids=[a, b])
                narrow += result["narrow_phase_calls"]
                collision = result["collision"]
                self.pair_pose_results[key] = (poses_key, collision)
            if collision:
                hits.append((a, b))
                if stop_at_first:
                    break
        return dict(broad, collision=bool(hits), pairs=hits, outer_hit_pairs=len(broad["pairs"]),
                    exact_narrow_phase_calls=narrow, new_exact_pair_caches=cooked,
                    cached_exact_pair_count=len(self.pair_checkers), cached_exact_pose_results=reused,
                    wall_s=time.perf_counter() - started,
                    coverage="conservative_outer_then_exact_source; moving_scene_and_moving_moving")

    def clearance(self, *, moving_ids, allowed_pairs=(), margin_m=0.05):
        """最小间隙：外包在 ``margin_m`` 内的对，再用精确几何求距离（外包距离只是下界，不报告）。"""
        rough = self.outer.clearance(moving_ids=moving_ids, allowed_pairs=allowed_pairs, margin_m=margin_m)
        if rough["min_m"] is None:
            return dict(rough, level="outer_beyond_margin")
        # 外包距离 ≤ 精确距离，所以只需精算外包落在 margin 内的对。
        near = []
        for i, j in self.outer._pairs:
            if np.any(self.outer.lower[i] - margin_m > self.outer.upper[j]) or np.any(self.outer.lower[j] - margin_m > self.outer.upper[i]):
                continue
            near.append(tuple(sorted((self.outer.ids[i], self.outer.ids[j]))))
        best, best_pair, calls = None, None, 0
        for key in near:
            checker, _ = self._pair_checker(key)
            checker.check({key[0]: self.outer.poses[key[0]], key[1]: self.outer.poses[key[1]]}, moving_ids=list(key))
            result = checker.clearance(moving_ids=list(key), margin_m=margin_m)
            calls += result["distance_calls"]
            if result["min_m"] is not None and (best is None or result["min_m"] < best):
                best, best_pair = result["min_m"], key
        return dict(min_m=best, pair=best_pair, evaluated_pairs=len(near), distance_calls=calls, margin_m=margin_m,
                    level="exact")


__all__ = ["RefinedChecker"]
