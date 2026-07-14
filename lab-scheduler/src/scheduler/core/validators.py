"""时间窗约束校验工具."""

from __future__ import annotations

from scheduler.models.dag import TaskDAG
from scheduler.models.schedule_result import ScheduledStep


def validate_time_constraints(
    results: list[ScheduledStep], dags: list[TaskDAG]
) -> list[str]:
    """校验 results 是否满足所有 dag 的 min_gap/max_gap 约束.

    返回违规字符串列表; 空表示无违规.
    """
    end_by_step = {r.step_id: r.end for r in results}
    start_by_step = {r.step_id: r.start for r in results}
    violations: list[str] = []

    for dag in dags:
        for tc in dag.time_constraints:
            if tc.from_step not in end_by_step or tc.to_step not in start_by_step:
                continue
            gap = start_by_step[tc.to_step] - end_by_step[tc.from_step]
            if tc.min_gap is not None and gap < tc.min_gap:
                violations.append(
                    f"min_gap violated: {tc.from_step}->{tc.to_step} "
                    f"gap={gap} < min={tc.min_gap}"
                )
            if tc.max_gap is not None and gap > tc.max_gap:
                violations.append(
                    f"max_gap violated: {tc.from_step}->{tc.to_step} "
                    f"gap={gap} > max={tc.max_gap}"
                )
    return violations
