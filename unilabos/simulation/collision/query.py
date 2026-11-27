"""整站碰撞查询：任意机构的一段关节轨迹 → 逐采样点碰撞结论、最小间隙和轨迹回执。

输入：
* 内核（:class:`SceneChecker` 或三级 :class:`RefinedChecker`），已装入整站的全部碰撞对象；
* 各设备的 :class:`Mechanism`（运动链 + 挂在连杆上的碰撞对象）；
* 轨迹：逐采样点的 ``{设备: {关节属性: 值}}``；可以来自规划器保存的路径、纯仿真的行为表运动流、
  孪生运动流或真机读回（:func:`states_from_motion_feed`）；
* 当前携带关系（:class:`Attachment`）与本阶段显式允许的接触对。

不规划、不插出未给出的连接段（:func:`densify` 只在相邻给定点之间按步长线性细分），
不改库存、不产生抓取事实。离散采样，不证明连续扫掠无碰撞。
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Iterable, Mapping
from pathlib import Path

import numpy as np

from .mechanism import Attachment, Mechanism

# 运动来源 → 结论的性质（与观测样本的 basis 同义）：规划候选、模拟运动、估计运动、观测运动。
SOURCE_KINDS = {"planned": "planned_candidate", "simulated": "simulated_motion", "estimated": "estimated_motion",
                "observed": "observed_motion"}


class CollisionQuery:
    def __init__(self, checker, mechanisms: Iterable[Mechanism]):
        self.checker = checker
        self.mechanisms = {m.device_id: m for m in mechanisms}
        if not self.mechanisms:
            raise ValueError("至少需要一台机构")
        owners = {}
        for m in self.mechanisms.values():
            for identity in m.bodies:
                if identity in owners:
                    raise ValueError(f"对象 {identity} 同时属于 {owners[identity]} 与 {m.device_id}：每个实物只能有一个碰撞所有者")
                if identity not in checker.index:
                    raise ValueError(f"机构 {m.device_id} 的对象 {identity} 不在碰撞场景里")
                owners[identity] = m.device_id
        self.owners = owners

    def _poses(self, state, attachments):
        poses = {}
        links = {}
        for device, values in state.items():
            mechanism = self.mechanisms.get(device)
            if mechanism is None:
                raise ValueError(f"轨迹里的设备 {device} 没有登记运动链")
            links[device] = mechanism.link_world(values)
            for identity, (link, offset) in mechanism.bodies.items():
                poses[identity] = links[device][link] @ offset
        for a in attachments:
            if a.device_id not in links:
                raise ValueError(f"携带物 {a.object_id} 的设备 {a.device_id} 不在本次轨迹里")
            poses[a.object_id] = links[a.device_id][a.link] @ a.link_from_object
        return poses

    def check_states(self, states: Iterable[Mapping[str, Mapping[str, float]]], *, attachments: Iterable[Attachment] = (),
                     allowed_pairs=(), static_poses: Mapping[str, object] | None = None,
                     clearance_margin_m: float | None = None, stop_at_first_collision: bool = False,
                     source: str = "planned", max_hit_records: int = 64) -> dict:
        """逐采样点检查。每个采样点必须给出本次参与运动的全部设备的全部关节（缺省关节按 0）。

        ``static_poses``：本次不动、但位姿与 profile 初值不同的对象（例如 OS 当前布局里的物料），只在首个
        采样点下发一次。``clearance_margin_m`` 给出时，在无碰撞的采样点上求该距离内的最小间隙。
        """
        if source not in SOURCE_KINDS:
            raise ValueError(f"运动来源只能是 {sorted(SOURCE_KINDS)}")
        attachments = tuple(attachments)
        carried = [a.object_id for a in attachments]
        if len(set(carried)) != len(carried) or set(carried) & set(self.owners):
            raise ValueError("携带物重复，或与机构自身的对象重名")
        if set(carried) - set(self.checker.index):
            raise ValueError("携带物不在碰撞场景里")
        allowed = sorted({tuple(sorted(p)) for p in allowed_pairs})
        started = time.perf_counter()
        hits, collision_samples, timings = [], [], []
        best = None
        devices = None
        counters = dict(aabb_candidates=0, narrow_phase_calls=0, outer_hit_pairs=0, exact_narrow_phase_calls=0)
        count = 0
        first = True
        for index, state in enumerate(states):
            if devices is None:
                devices = tuple(sorted(state))
            elif tuple(sorted(state)) != devices:
                raise ValueError("每个采样点必须给出同一组设备的状态")
            tick = time.perf_counter()
            poses = self._poses(state, attachments)
            moving = list(poses)
            if first and static_poses:
                unknown = set(static_poses) - set(self.checker.index)
                if unknown:
                    raise ValueError(f"静态位姿里有未知对象：{sorted(unknown)[:5]}")
                poses = {**{k: v for k, v in static_poses.items() if k not in poses}, **poses}
            result = self.checker.check(poses, moving_ids=moving, allowed_pairs=allowed, stop_at_first=True)
            first = False
            for key in counters:
                counters[key] += int(result.get(key, 0))
            if result["collision"]:
                collision_samples.append(index)
                if len(hits) < max_hit_records:
                    hits.append(dict(sample=index, pairs=[list(p) for p in result["pairs"]]))
            elif clearance_margin_m is not None:
                gap = self.checker.clearance(moving_ids=moving, allowed_pairs=allowed, margin_m=clearance_margin_m)
                if gap["min_m"] is not None and (best is None or gap["min_m"] < best["min_m"]):
                    best = dict(min_m=gap["min_m"], pair=list(gap["pair"]), sample=index)
            timings.append(time.perf_counter() - tick)
            count += 1
            if result["collision"] and stop_at_first_collision:
                break
        if not count:
            raise ValueError("轨迹为空，未检查")
        times = np.asarray(timings)
        return dict(
            status="collision" if collision_samples else "clear",
            source=source, conclusion_kind=SOURCE_KINDS[source],
            devices=list(devices), samples=count, collision_samples=collision_samples, first_hits=hits,
            min_clearance=best if clearance_margin_m is not None else None,
            clearance_margin_m=clearance_margin_m,
            levels=dict(level1_aabb_candidates=counters["aabb_candidates"],
                        level2_outer_hits=counters["outer_hit_pairs"],
                        level3_exact_calls=counters["exact_narrow_phase_calls"] or counters["narrow_phase_calls"]),
            query_total_s=float(times.sum()), query_median_s=float(np.median(times)), query_p95_s=float(np.percentile(times, 95)),
            query_max_s=float(times.max()), wall_s=time.perf_counter() - started,
            allowed_pairs=[list(p) for p in allowed], continuous_collision_proof=False,
        )


def densify(states: Iterable[Mapping[str, Mapping[str, float]]], *, max_step: float | Mapping[str, float],
            max_samples: int = 1_000_000):
    """相邻给定状态之间按步长线性细分（保留全部给定点）。``max_step`` 可按关节属性名分别给出（弧度 / 米）。"""
    previous = None
    produced = 0
    for state in states:
        if previous is not None:
            steps = 1
            for device, values in state.items():
                for name, value in values.items():
                    bound = max_step.get(name) if isinstance(max_step, Mapping) else max_step
                    if bound is None or bound <= 0 or not math.isfinite(bound):
                        raise ValueError(f"关节属性 {name} 没有有效步长")
                    delta = abs(float(value) - float(previous[device][name]))
                    if not math.isfinite(delta):
                        raise ValueError("关节值不是有限数")
                    steps = max(steps, int(math.ceil(delta / bound)))
            if produced + steps > max_samples:
                raise ValueError("Sampling budget exceeded; path not checked（采样数超出预算）")
            for k in range(1, steps):
                f = k / steps
                yield {d: {n: float(previous[d][n]) + (float(v) - float(previous[d][n])) * f for n, v in vals.items()}
                       for d, vals in state.items()}
                produced += 1
        yield {d: dict(vals) for d, vals in state.items()}
        produced += 1
        previous = state


def states_from_motion_feed(path, devices: Iterable[str], *, joints: Mapping[str, Iterable[str]] | None = None,
                            initial: Mapping[str, Mapping[str, float]] | None = None, t_start=None, t_end=None):
    """读运动流（JSONL：表头 + ``{"t", "event", "values": {设备: {属性: 值}}}``），逐行给出完整状态。

    运动流每行只带变化的设备；这里累积成完整状态。``joints`` 限定每台设备取哪些属性（例如只取关节，
    不取派生的矩阵属性）。返回 ``(t, state)``。
    """
    devices = tuple(devices)
    state = {d: dict((initial or {}).get(d, {})) for d in devices}
    keep = {d: set(v) for d, v in (joints or {}).items()}
    with Path(path).open(encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            row = json.loads(line)
            if "t" not in row:
                continue   # 表头
            for device, values in (row.get("values") or {}).items():
                if device not in state:
                    continue
                for name, value in values.items():
                    if isinstance(value, (int, float)) and (device not in keep or name in keep[device]):
                        state[device][name] = float(value)
            t = float(row["t"])
            if (t_start is not None and t < t_start) or (t_end is not None and t > t_end):
                continue
            yield t, {d: dict(v) for d, v in state.items()}


def states_digest(states: Iterable[Mapping[str, Mapping[str, float]]]) -> str:
    digest = hashlib.sha256()
    for state in states:
        digest.update(json.dumps(state, sort_keys=True, separators=(",", ":")).encode())
    return "sha256:" + digest.hexdigest()


def receipt(report: Mapping, *, action_id: str, trajectory_digest: str, scene_digest: str, frames=("world",),
            topology: str, planned: str, units: str = "m,rad", executed: str | None = None,
            observed: str | None = None, replay: bool = False):
    """把查询结论写成 OS 轨迹回执：做过检查即 ``collision_checked``，状态为 clear / collision。"""
    from unilabos.simulation.motion import CollisionStatus, EvidenceLevel, TrajectoryReceipt, verify_trajectory_receipt

    status = CollisionStatus.COLLISION if report["status"] == "collision" else CollisionStatus.CLEAR
    value = TrajectoryReceipt(action_id=action_id, trajectory_digest=trajectory_digest, scene_digest=scene_digest,
                              units=units, frames=tuple(frames), topology=topology, planned=planned, executed=executed,
                              observed=observed, evidence_level=EvidenceLevel.COLLISION_CHECKED,
                              collision_status=status, replay=replay)
    verify_trajectory_receipt(value)
    return value


__all__ = ["CollisionQuery", "densify", "states_from_motion_feed", "states_digest", "receipt", "SOURCE_KINDS"]
