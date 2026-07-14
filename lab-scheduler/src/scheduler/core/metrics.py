"""设备负载/利用率度量 (排队论 ρ).

两个口径并存, 在饱和稳态极限 (瓶颈 ρ→1.0) 下重合:

- ``utilization``  经验排队论 ρ_i = work_i / (makespan · c_i) = λ_i·E[S_i]/c_i.
  绝对量, 受全局暖机/排空瞬态影响, 瓶颈通常 < 1.0.
- ``load_ratio`` (ρ̂)  (work_i/c_i) / maxⱼ(workⱼ/cⱼ), 归一到瓶颈 (瓶颈=1.0).
  makespan 相消 => 瞬态无关; 用于判断"谁是约束".

派生指标:
- ``makespan_lower_bound`` = maxⱼ(workⱼ/cⱼ)  最忙单机总工时 (makespan 理论下界).
- ``schedule_efficiency`` = LB / makespan = max(utilization) = 瓶颈真实 ρ.
  衡量整体调度贴近下界的程度; 1 - eff 即不可避免的灌注+排空+依赖开销占比.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class DeviceLoad:
    """单一设备类型的负载度量."""

    device_type: str
    count: int
    busy_minutes: float  # 该类型全部实例的总机时 Σ(end-start)
    work_per_machine: float  # busy_minutes / count
    utilization: float  # 经验排队论 ρ = work / (makespan · count)
    load_ratio: float  # 归一到瓶颈的 ρ̂ ∈ [0, 1], 瓶颈=1.0


@dataclass(frozen=True)
class LoadSummary:
    """整体负载汇总: 瓶颈、调度效率与逐类型度量."""

    makespan: float
    makespan_lower_bound: float  # maxⱼ(workⱼ/cⱼ)
    schedule_efficiency: float  # LB / makespan = max(utilization)
    bottleneck_type: str | None
    by_type: dict[str, DeviceLoad] = field(default_factory=dict)


def compute_device_load(
    busy_by_type: dict[str, float],
    count_by_type: dict[str, int],
    makespan: float,
) -> LoadSummary:
    """从总机时与台数计算负载度量 (纯函数, 无瞬态依赖外的副作用).

    Args:
        busy_by_type: 设备类型 → 该类型全部实例的总机时 (分钟).
        count_by_type: 设备类型 → 台数; 决定输出包含哪些类型.
        makespan: 调度总时长 (分钟), 用于绝对利用率.
    """
    if makespan <= 0 or not count_by_type:
        return LoadSummary(
            makespan=max(float(makespan), 0.0),
            makespan_lower_bound=0.0,
            schedule_efficiency=0.0,
            bottleneck_type=None,
            by_type={},
        )

    work_per_machine: dict[str, float] = {}
    for dtype, count in count_by_type.items():
        c = max(int(count), 1)
        work_per_machine[dtype] = float(busy_by_type.get(dtype, 0.0)) / c

    max_wpm = max(work_per_machine.values(), default=0.0)
    bottleneck = (
        max(work_per_machine, key=lambda d: work_per_machine[d])
        if max_wpm > 0
        else None
    )

    by_type: dict[str, DeviceLoad] = {}
    for dtype, count in count_by_type.items():
        c = max(int(count), 1)
        work = float(busy_by_type.get(dtype, 0.0))
        wpm = work_per_machine[dtype]
        util = work / (makespan * c)
        ratio = wpm / max_wpm if max_wpm > 0 else 0.0
        by_type[dtype] = DeviceLoad(
            device_type=dtype,
            count=c,
            busy_minutes=round(work, 4),
            work_per_machine=round(wpm, 4),
            utilization=round(util, 4),
            load_ratio=round(ratio, 4),
        )

    eff = max_wpm / makespan if makespan > 0 else 0.0
    return LoadSummary(
        makespan=round(float(makespan), 4),
        makespan_lower_bound=round(max_wpm, 4),
        schedule_efficiency=round(eff, 4),
        bottleneck_type=bottleneck,
        by_type=by_type,
    )
