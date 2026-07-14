"""Bitmask DP 精确求解器 (N ≤ 20).

用于小规模实例的最优解验证, 作为 benchmark baseline.
不考虑 instance-level 设备分配, 仅按类型计数.
"""

from __future__ import annotations

from scheduler.core.base import SchedulerBase
from scheduler.core.step_algorithms import register_algorithm
from scheduler.models.schedule_result import ScheduledStep

MAX_STEPS = 20


@register_algorithm("ExactDP")
class ExactDPScheduler(SchedulerBase):
    """Bitmask DP 精确求解器 (N ≤ 20)."""

    def _schedule_ready_tasks(self) -> None:
        pass

    def schedule(self) -> list[ScheduledStep]:
        """覆盖基类: 枚举所有可行调度顺序, 返回最优."""
        # 收集所有未完成的 steps
        steps: list[tuple[int, str]] = []
        for batch_idx, batch in enumerate(self.batches):
            for nid, node in batch.task_dag.nodes.items():
                if not node.completed:
                    steps.append((batch_idx, nid))

        n = len(steps)
        if n == 0:
            return []
        if n > MAX_STEPS:
            raise ValueError(
                f"ExactDP only supports ≤{MAX_STEPS} steps, got {n}. "
                "Use CP-SAT or heuristic algorithms instead."
            )

        # 预计算: step index mapping
        step_idx = {(bi, nid): i for i, (bi, nid) in enumerate(steps)}

        # 前驱列表 (index-based)
        prereqs: list[list[int]] = [[] for _ in range(n)]
        for i, (batch_idx, nid) in enumerate(steps):
            dag = self.batches[batch_idx].task_dag
            for parent_id in dag.parents.get(nid, []):
                parent_node = dag.nodes[parent_id]
                if parent_node.completed:
                    continue
                key = (batch_idx, parent_id)
                if key in step_idx:
                    prereqs[i].append(step_idx[key])

        prereq_mask: list[int] = [0] * n
        for i in range(n):
            for p in prereqs[i]:
                prereq_mask[i] |= 1 << p

        # Task 信息
        task_step_masks: dict[str, int] = {}
        task_weights: dict[str, float] = {}
        step_task: list[str] = []
        for i, (batch_idx, nid) in enumerate(steps):
            dag = self.batches[batch_idx].task_dag
            tid = dag.task_id
            task_step_masks.setdefault(tid, 0)
            task_step_masks[tid] |= 1 << i
            task_weights[tid] = dag.weight
            step_task.append(tid)

        # Step properties
        durations: list[int] = []
        machine_types: list[str] = []
        for batch_idx, nid in steps:
            node = self.batches[batch_idx].task_dag.nodes[nid]
            durations.append(node.duration)
            machine_types.append(node.machine_type)

        # 设备容量 (按类型)
        type_capacity: dict[str, int] = {}
        for inst in self.device_pool.instances.values():
            type_capacity.setdefault(inst.device_type, 0)
            type_capacity[inst.device_type] += 1

        unique_types = sorted(type_capacity.keys())
        type_to_idx = {t: i for i, t in enumerate(unique_types)}

        full_mask = (1 << n) - 1
        INF = float("inf")
        base_time = self.current_time

        # DP state: (mask, device_avail_tuple, step_end_times_tuple)
        # step_end_times 只需要存已完成 step 的 end time (用 tuple of n ints, 0 for not done)
        # 简化: 将 (mask, device_avail, step_ends) 合成一个可 hash 的 key

        memo: dict[tuple, tuple[float, int]] = {}

        def _state_key(
            mask: int, avail: list[list[int]], ends: list[int]
        ) -> tuple:
            return (
                mask,
                tuple(tuple(sorted(a)) for a in avail),
                tuple(ends),
            )

        def dp(
            mask: int, avail: list[list[int]], ends: list[int]
        ) -> tuple[float, int]:
            if mask == full_mask:
                return 0.0, -1

            key = _state_key(mask, avail, ends)
            if key in memo:
                return memo[key]

            best_cost = INF
            best_choice = -1

            for i in range(n):
                if mask & (1 << i):
                    continue
                if (mask & prereq_mask[i]) != prereq_mask[i]:
                    continue

                mt = machine_types[i]
                ti = type_to_idx.get(mt)
                if ti is None or not avail[ti]:
                    continue

                # 计算最早开始时间: max(设备空闲, 所有前驱完成)
                dev_times = avail[ti]
                earliest_dev_idx = min(
                    range(len(dev_times)), key=lambda x: dev_times[x]
                )
                earliest_start = max(dev_times[earliest_dev_idx], base_time)

                # 前驱约束: 必须等所有前驱完成
                for p in prereqs[i]:
                    earliest_start = max(earliest_start, ends[p])

                end_time = earliest_start + durations[i]

                # 更新状态
                new_avail = [list(a) for a in avail]
                new_avail[ti][earliest_dev_idx] = end_time

                new_ends = list(ends)
                new_ends[i] = end_time

                new_mask = mask | (1 << i)

                # 计算 task 完工代价
                step_cost = 0.0
                for tid, tmask in task_step_masks.items():
                    if (new_mask & tmask) == tmask and (mask & tmask) != tmask:
                        # task 全部完成 — completion time = max end of task's steps
                        c_task = 0
                        for j in range(n):
                            if tmask & (1 << j):
                                c_task = max(c_task, new_ends[j])
                        step_cost += task_weights[tid] * c_task

                sub_cost, _ = dp(new_mask, new_avail, new_ends)
                total = step_cost + sub_cost

                if total < best_cost:
                    best_cost = total
                    best_choice = i

            memo[key] = (best_cost, best_choice)
            return best_cost, best_choice

        # 初始状态
        init_avail = [
            [base_time] * type_capacity.get(t, 0) for t in unique_types
        ]
        init_ends = [0] * n

        dp(0, init_avail, init_ends)

        # 回溯最优路径
        order: list[int] = []
        mask = 0
        avail = [list(a) for a in init_avail]
        ends = list(init_ends)

        while mask != full_mask:
            key = _state_key(mask, avail, ends)
            _, choice = memo.get(key, (INF, -1))
            if choice == -1:
                break
            order.append(choice)

            mt = machine_types[choice]
            ti = type_to_idx[mt]
            dev_times = avail[ti]
            eidx = min(range(len(dev_times)), key=lambda x: dev_times[x])
            start_t = max(dev_times[eidx], base_time)
            for p in prereqs[choice]:
                start_t = max(start_t, ends[p])
            end_t = start_t + durations[choice]
            avail[ti][eidx] = end_t
            ends[choice] = end_t
            mask |= 1 << choice

        # 构建结果
        results: list[ScheduledStep] = []
        r_avail = [list(a) for a in init_avail]
        r_ends = list(init_ends)

        for step_i in order:
            batch_idx, nid = steps[step_i]
            node = self.batches[batch_idx].task_dag.nodes[nid]

            mt = machine_types[step_i]
            ti = type_to_idx[mt]
            dev_times = r_avail[ti]
            eidx = min(range(len(dev_times)), key=lambda x: dev_times[x])
            start_t = max(dev_times[eidx], base_time)
            for p in prereqs[step_i]:
                start_t = max(start_t, r_ends[p])
            end_t = start_t + durations[step_i]
            r_avail[ti][eidx] = end_t
            r_ends[step_i] = end_t

            device_name = f"{mt}_{eidx}"
            node.start = start_t
            node.end = end_t
            node.completed = True
            node.assigned_device = device_name

            results.append(
                ScheduledStep(
                    step_id=node.step_id,
                    task_id=node.task_id,
                    start=start_t,
                    end=end_t,
                    device_instance=device_name,
                )
            )

        if results:
            self.current_time = max(r.end for r in results)

        self.completed = results
        return results
