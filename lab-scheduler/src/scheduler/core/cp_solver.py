"""OR-Tools CP-SAT 精确求解器.

特性:
- 批处理设备: batch-slot formulation (每个 slot 可容纳 ≤ cap 个任务, 同步启停)
- 非批处理设备: 标准 NoOverlap per unit
- 多台同类型设备 → Optional intervals + ExactlyOne (alternative routing)
- time_constraints (min_gap/max_gap) → 线性约束
- 加权完工时间目标 → 线性目标 (权重×100 整数化)
- 支持 reschedule: in-flight steps 锁定时间
"""

from __future__ import annotations

import math
from collections import defaultdict

from ortools.sat.python import cp_model

from scheduler.core.base import SchedulerBase
from scheduler.core.step_algorithms import register_algorithm
from scheduler.models.dag import StepNode
from scheduler.models.resources import DevicePool
from scheduler.models.schedule_result import ScheduledStep

# 权重精度缩放因子 (CP-SAT 目标需要整数)
_WEIGHT_SCALE = 100


def _idur(node) -> int:
    """CP-SAT 使用整数时间网格；时长支持小数后在此取整(向上取整避免 0)."""
    return max(1, math.ceil(node.duration))


@register_algorithm("CP-SAT")
class CPSATScheduler(SchedulerBase):
    """支持批处理约束的 CP-SAT 求解器.

    批处理设备使用 batch-slot formulation:
    - 每台物理设备创建若干 batch slot (optional interval)
    - 每个 slot 可容纳 ≤ cap 个任务, 同步启停
    - slot 之间 NoOverlap (同一设备不能同时两批)
    非批处理设备: 标准 NoOverlap per unit.
    """

    def __init__(
        self,
        device_pool: DevicePool,
        time_limit_s: float = 120.0,
        num_workers: int = 8,
        horizon: int = 10_000,
    ) -> None:
        super().__init__(device_pool)
        self.time_limit_s = time_limit_s
        self.num_workers = num_workers
        self.horizon = horizon

    def _schedule_ready_tasks(self) -> None:
        # CP-SAT 不使用事件驱动循环
        pass

    def schedule(self) -> list[ScheduledStep]:
        """覆盖基类: 直接建模求解, 不走事件仿真循环."""
        model = cp_model.CpModel()

        # ── 收集 steps ──
        all_steps: list[tuple[int, str, StepNode]] = []
        for bi, batch in enumerate(self.batches):
            for nid, node in batch.task_dag.nodes.items():
                if not node.completed:
                    all_steps.append((bi, nid, node))
        if not all_steps:
            return []

        total_dur = sum(_idur(n) for _, _, n in all_steps)
        horizon = max(self.horizon, total_dur * 2)

        # ── 按设备类型分组 ──
        batch_type_steps: dict[str, list[tuple[int, str, StepNode]]] = defaultdict(list)
        normal_steps: list[tuple[int, str, StepNode]] = []
        for bi, nid, node in all_steps:
            # 检查该设备类型的第一个实例的 batch_capacity
            candidates = [iid for iid, inst in self.device_pool.instances.items()
                          if inst.device_type == node.machine_type]
            if candidates:
                inst = self.device_pool.instances[candidates[0]]
                if inst.batch_capacity > 1:
                    batch_type_steps[node.machine_type].append((bi, nid, node))
                else:
                    normal_steps.append((bi, nid, node))
            else:
                normal_steps.append((bi, nid, node))

        # ── 决策变量 ──
        start_vars: dict[str, cp_model.IntVar] = {}
        end_vars: dict[str, cp_model.IntVar] = {}
        for bi, nid, node in all_steps:
            s = model.new_int_var(self.current_time, horizon, f"s_{nid}")
            start_vars[nid] = s
            # 检查是否为批处理设备
            candidates = [iid for iid, inst in self.device_pool.instances.items()
                          if inst.device_type == node.machine_type]
            is_batch = False
            if candidates:
                inst = self.device_pool.instances[candidates[0]]
                is_batch = inst.batch_capacity > 1

            if is_batch:
                # 批处理: end >= start + dur (可能被批次延长)
                e = model.new_int_var(self.current_time, horizon, f"e_{nid}")
                model.add(e >= s + _idur(node))
            else:
                # 普通: end == start + dur
                e = model.new_int_var(self.current_time, horizon, f"e_{nid}")
                model.add(e == s + _idur(node))
            end_vars[nid] = e

        # ── 设备实例按类型分组 ──
        units_by_type: dict[str, list[str]] = defaultdict(list)
        for iid, inst in self.device_pool.instances.items():
            units_by_type[inst.device_type].append(iid)

        # ── 非批处理设备: 标准 assign + NoOverlap ──
        unit_opt_itvs: dict[str, list[cp_model.IntervalVar]] = defaultdict(list)
        assign_vars: dict[tuple[str, str], cp_model.BoolVar] = {}

        for bi, nid, node in normal_steps:
            candidates = units_by_type.get(node.machine_type, [])
            bools: list[cp_model.BoolVar] = []
            for uid in candidates:
                b = model.new_bool_var(f"asgn_{nid}_{uid}")
                assign_vars[(nid, uid)] = b
                bools.append(b)
                opt = model.new_optional_interval_var(
                    start_vars[nid], _idur(node), end_vars[nid], b,
                    f"opt_{nid}_{uid}")
                unit_opt_itvs[uid].append(opt)
            if bools:
                model.add_exactly_one(bools)

        for uid, oitvs in unit_opt_itvs.items():
            if len(oitvs) > 1:
                model.add_no_overlap(oitvs)

        # ── 批处理设备: batch-slot formulation ──
        # 存储每个任务的 (unit, slot_index) assignment bools
        batch_assign: dict[tuple[str, str, int], cp_model.BoolVar] = {}
        # 存储每个 slot 的 vars
        batch_slot_info: dict[tuple[str, int], tuple] = {}

        for dtype, tasks in batch_type_steps.items():
            # 读取 batch_capacity
            units = units_by_type.get(dtype, [])
            if not units:
                continue
            cap = self.device_pool.instances[units[0]].batch_capacity
            N = len(tasks)
            M = max(1, math.ceil(N / cap))  # slots per unit (upper bound)
            max_dur = max(_idur(n) for _, _, n in tasks)

            # 创建 batch slots
            for uid in units:
                slot_intervals: list[cp_model.IntervalVar] = []
                for sb in range(M):
                    bs = model.new_int_var(0, horizon, f"bs_{uid}_{sb}")
                    bsize = model.new_int_var(0, max_dur, f"bsz_{uid}_{sb}")
                    be = model.new_int_var(0, horizon, f"be_{uid}_{sb}")
                    active = model.new_bool_var(f"bact_{uid}_{sb}")
                    itv = model.new_optional_interval_var(
                        bs, bsize, be, active, f"bitv_{uid}_{sb}")
                    batch_slot_info[(uid, sb)] = (bs, bsize, be, active, itv)
                    slot_intervals.append(itv)

                if len(slot_intervals) > 1:
                    model.add_no_overlap(slot_intervals)

            # 每个任务分配到恰好一个 (unit, slot)
            for bi, nid, node in tasks:
                bools_all: list[cp_model.BoolVar] = []
                for uid in units:
                    for sb in range(M):
                        ab = model.new_bool_var(f"ba_{nid}_{uid}_{sb}")
                        batch_assign[(nid, uid, sb)] = ab
                        bools_all.append(ab)
                        bs_v, bsz_v, be_v, _, _ = batch_slot_info[(uid, sb)]
                        # 同步: task start/end == batch slot start/end
                        model.add(start_vars[nid] == bs_v).only_enforce_if(ab)
                        model.add(end_vars[nid] == be_v).only_enforce_if(ab)
                        # batch 时长 >= 此任务时长
                        model.add(bsz_v >= _idur(node)).only_enforce_if(ab)
                if bools_all:
                    model.add_exactly_one(bools_all)

            # 容量约束 + active 联动
            for uid in units:
                for sb in range(M):
                    slot_bools = [
                        batch_assign[(nid, uid, sb)]
                        for _, nid, _ in tasks
                        if (nid, uid, sb) in batch_assign
                    ]
                    if slot_bools:
                        model.add(sum(slot_bools) <= cap)
                        _, _, _, active, _ = batch_slot_info[(uid, sb)]
                        model.add_max_equality(active, slot_bools)

        # ── DAG 约束 ──
        for bi, batch in enumerate(self.batches):
            dag = batch.task_dag
            for edge in dag.edges:
                src_n = dag.nodes.get(edge.source)
                if src_n and src_n.completed and src_n.end is not None:
                    if edge.target in start_vars:
                        model.add(start_vars[edge.target] >= src_n.end)
                elif edge.source in end_vars and edge.target in start_vars:
                    model.add(start_vars[edge.target] >= end_vars[edge.source])

        # ── 时间约束 (max_gap / min_gap) ──
        for bi, batch in enumerate(self.batches):
            for tc in batch.task_dag.time_constraints:
                f_node = batch.task_dag.nodes.get(tc.from_step)
                if f_node and f_node.completed and f_node.end is not None:
                    if tc.to_step in start_vars:
                        if tc.min_gap is not None:
                            model.add(start_vars[tc.to_step] - f_node.end >= tc.min_gap)
                        if tc.max_gap is not None:
                            model.add(start_vars[tc.to_step] - f_node.end <= tc.max_gap)
                elif tc.from_step in end_vars and tc.to_step in start_vars:
                    if tc.min_gap is not None:
                        model.add(start_vars[tc.to_step] - end_vars[tc.from_step] >= tc.min_gap)
                    if tc.max_gap is not None:
                        model.add(start_vars[tc.to_step] - end_vars[tc.from_step] <= tc.max_gap)

        # ── 目标: min Sum(wi * Ci) ──
        task_steps_map: dict[str, list[str]] = defaultdict(list)
        task_weights: dict[str, float] = {}
        for bi, batch in enumerate(self.batches):
            tid = batch.task_dag.task_id
            task_weights[tid] = batch.weight
            for nid in batch.task_dag.nodes:
                if nid in end_vars:
                    task_steps_map[tid].append(nid)

        obj_terms: list = []
        task_c_vars: dict[str, cp_model.IntVar] = {}
        for tid, sids in task_steps_map.items():
            if not sids:
                continue
            cv = model.new_int_var(0, horizon, f"C_{tid}")
            model.add_max_equality(cv, [end_vars[sid] for sid in sids])
            task_c_vars[tid] = cv
            w = int(round(task_weights[tid] * _WEIGHT_SCALE))
            obj_terms.append(w * cv)
        if obj_terms:
            model.minimize(sum(obj_terms))

        # ── 求解 ──
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = self.time_limit_s
        solver.parameters.num_workers = self.num_workers
        status = solver.solve(model)

        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            return []

        # ── 提取结果 ──
        results: list[ScheduledStep] = []

        # 非批处理设备任务
        for bi, nid, node in normal_steps:
            s_val = solver.value(start_vars[nid])
            e_val = solver.value(end_vars[nid])
            chosen = ""
            for uid in units_by_type.get(node.machine_type, []):
                if (nid, uid) in assign_vars and solver.value(assign_vars[(nid, uid)]):
                    chosen = uid
                    break
            node.start, node.end, node.completed, node.assigned_device = s_val, e_val, True, chosen
            results.append(ScheduledStep(step_id=node.step_id, task_id=node.task_id,
                                         start=s_val, end=e_val, device_instance=chosen))

        # 批处理设备任务: 确定 slot 分配, 生成 slot-level device name
        for dtype, tasks in batch_type_steps.items():
            units = units_by_type.get(dtype, [])
            cap = self.device_pool.instances[units[0]].batch_capacity
            N = len(tasks)
            M = max(1, math.ceil(N / cap))

            # 按 (uid, sb) 收集分配到同一 slot 的任务
            slot_members: dict[tuple[str, int], list[tuple[int, str, StepNode]]] = defaultdict(list)
            task_slot: dict[str, tuple[str, int]] = {}
            for bi, nid, node in tasks:
                for uid in units:
                    for sb in range(M):
                        key = (nid, uid, sb)
                        if key in batch_assign and solver.value(batch_assign[key]):
                            task_slot[nid] = (uid, sb)
                            slot_members[(uid, sb)].append((bi, nid, node))
                            break
                    if nid in task_slot:
                        break

            # 为同一 slot 内的任务分配子槽位号
            for (uid, sb), members in slot_members.items():
                for slot_idx, (bi, nid, node) in enumerate(members):
                    s_val = solver.value(start_vars[nid])
                    e_val = solver.value(end_vars[nid])
                    dev_name = f"{uid}_s{slot_idx}"
                    node.start, node.end, node.completed = s_val, e_val, True
                    node.assigned_device = dev_name
                    results.append(ScheduledStep(step_id=node.step_id, task_id=node.task_id,
                                                 start=s_val, end=e_val, device_instance=dev_name))

        if results:
            self.current_time = max(r.end for r in results)
        self.completed = results
        return results
