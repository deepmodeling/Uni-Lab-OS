"""生物监测实验室调度基准测试 V3 — 设备多通量 + 批处理约束.

批处理模型:
  同一物理设备的多个样品必须同时开始处理, 同时结束.
  批次时长 = max(各样品处理时间).
  批次结束前设备不可用, 样品不可取出.
  槽位不满也允许启动 (不等待凑满).
  甘特图展示每个槽位的占用情况.
"""

from __future__ import annotations
from ortools.sat.python import cp_model
from scheduler.visualization import (
    plot_dual_gantt, plot_convergence, plot_convergence_multi, plot_algorithm_comparison
)
from scheduler.core.ga_solver import GeneticAlgorithmScheduler
from scheduler.core.cp_solver import CPSATScheduler
from scheduler.core.batch_factory import make_batch_scheduler
from scheduler.core.step_algorithms import (
    get_algorithm, list_algorithms, ALGORITHM_REGISTRY,
    GreedyScheduler, CriticalPathScheduler, WeightedCriticalPathScheduler,
    DynamicPriorityScheduler, MultiObjectiveScheduler,
    RealtimeScheduler, HybridCriticalityScheduler,
)
from scheduler.core.base import SchedulerBase
from scheduler.models.schedule_result import ScheduledStep
from scheduler.models.resources import DeviceInstance, DevicePool
from scheduler.models.dag import Edge, StepNode, TaskDAG, TimeConstraint
import numpy as np
from matplotlib.patches import Rectangle
import matplotlib.pyplot as plt

import copy
import heapq
import math
import os
import sys
import time
import random
from collections import defaultdict

import matplotlib
matplotlib.use("Agg")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# 使用公共 API 的批处理和求解器

import scheduler.core.cp_solver  # noqa: F401
import scheduler.core.dp_solver  # noqa: F401

# CP-SAT 权重缩放因子 (与 cp_solver.py 保持一致)
_WEIGHT_SCALE = 100


# ══════════════════════════════════════════════════════════
# 0. 工具函数
# ══════════════════════════════════════════════════════════

def compute_weighted_objective(results, dags):
    tw = {dag.task_id: dag.weight for dag, _ in dags}
    tc = {}
    for r in results:
        if r.task_id not in tc or r.end > tc[r.task_id]:
            tc[r.task_id] = r.end
    total = sum(tw.get(tid, 1.0) * ct for tid, ct in tc.items())
    return total, tc


def deep_copy_dag(dag):
    d = copy.deepcopy(dag)
    for n in d.nodes.values():
        n.reset()
    d.build_adjacency()
    return d


# ══════════════════════════════════════════════════════════
# 1. 设备配置
# ══════════════════════════════════════════════════════════

# A. Baseline: 每台容量=1
BASELINE_PHYS = {
    "Denso_Arm": 3, "Agilent_vWork": 3, "LightCycler_480": 3,
    "Incubation_Carrier": 6, "Centrifuge": 3,
    "Washer": 3, "Reader": 3, "Sealer": 3,
}

# 多通量设备及其每台容量
BATCH_CAPS = {
    "LightCycler_480": 2,      # PCR: 双模块, 2 板同时
    "Incubation_Carrier": 4,   # 孵育器: 4 层搁架
    "Centrifuge": 2,            # 离心机: 双转子
}


def make_pool():
    """每台物理设备是一个 instance, 多槽位容量由 batch_capacity 指定."""
    pool = DevicePool()
    for dt, n in BASELINE_PHYS.items():
        cap = BATCH_CAPS.get(dt, 1)
        pool.add_device_type(dt, n, batch_capacity=cap)
    return pool


# ══════════════════════════════════════════════════════════
# 2. 批处理调度器注册 (使用公共 API)
# ══════════════════════════════════════════════════════════

HEURISTIC_CLASSES = [
    GreedyScheduler, CriticalPathScheduler, WeightedCriticalPathScheduler,
    DynamicPriorityScheduler, MultiObjectiveScheduler,
    RealtimeScheduler, HybridCriticalityScheduler,
]

BATCH_SCHEDULERS = {
    f"Batch_{cls.algorithm_name}": make_batch_scheduler(cls)
    for cls in HEURISTIC_CLASSES
}


# ══════════════════════════════════════════════════════════
# 2c. 时间约束验证
# ══════════════════════════════════════════════════════════

def validate_time_constraints(results, dags):
    """验证调度结果是否满足所有时间约束. 返回违反列表."""
    # 建立 step_id -> (start, end) 映射
    step_times = {}
    for r in results:
        step_times[r.step_id] = (r.start, r.end)

    violations = []
    for dag, _ in dags:
        for tc in dag.time_constraints:
            if tc.from_step not in step_times or tc.to_step not in step_times:
                continue
            _, f_end = step_times[tc.from_step]
            t_start, _ = step_times[tc.to_step]
            gap = t_start - f_end
            if tc.max_gap is not None and gap > tc.max_gap:
                violations.append(
                    f"  max_gap VIOLATED: {tc.from_step} -> {tc.to_step}  "
                    f"gap={gap}s > max={tc.max_gap}s  (excess={gap - tc.max_gap}s)")
            if tc.min_gap is not None and gap < tc.min_gap:
                violations.append(
                    f"  min_gap VIOLATED: {tc.from_step} -> {tc.to_step}  "
                    f"gap={gap}s < min={tc.min_gap}s")
    return violations


# ══════════════════════════════════════════════════════════
# 2e. CP-SAT 带回调 (收敛跟踪)
# ══════════════════════════════════════════════════════════

class _SolutionTracker(cp_model.CpSolverSolutionCallback):
    """记录 CP-SAT 求解过程中每个可行解的 (时间, 目标值)."""

    def __init__(self, t0):
        super().__init__()
        self._t0 = t0
        self.history = []

    def on_solution_callback(self):
        elapsed = time.perf_counter() - self._t0
        # CP-SAT 目标是 _WEIGHT_SCALE 倍, 还原为原始尺度
        obj = self.objective_value / _WEIGHT_SCALE
        self.history.append((elapsed, obj))


def run_ga_with_tracking(dags, batch_caps, time_limit_s):
    """运行 GA 并跟踪收敛历史. 返回 (results, obj, history)."""
    from scheduler.core.ga_solver import _GeneticAlgorithm, decode_permutation

    # 构建 pool_factory
    def pool_factory():
        pool = DevicePool()
        for dt, n in BASELINE_PHYS.items():
            cap = batch_caps.get(dt, 1)
            pool.add_device_type(dt, n, batch_capacity=cap)
        return pool

    # 运行 GA 并跟踪历史
    ga = _GeneticAlgorithm(dags, pool_factory, pop_size=60, seed=42)
    t0 = time.perf_counter()

    # 初始化种群
    pop = [ga._random_perm() for _ in range(ga.pop_size)]
    fits = [ga._eval(p) for p in pop]
    best_idx = min(range(len(fits)), key=lambda i: fits[i])
    best_obj = fits[best_idx]
    best_perm = list(pop[best_idx])

    history = [(0.0, best_obj)]

    gen = 0
    while True:
        elapsed = time.perf_counter() - t0
        if elapsed >= time_limit_s:
            break
        gen += 1

        # 选择 + 交叉 + 变异
        new_pop = []
        ranked = sorted(range(len(pop)), key=lambda i: fits[i])
        for i in ranked[:ga.elite_count]:
            new_pop.append(list(pop[i]))

        while len(new_pop) < ga.pop_size:
            p1 = ga._tournament(pop, fits)
            p2 = ga._tournament(pop, fits)
            child = ga._ox_crossover(p1, p2)
            if ga.rng.random() < ga.mutation_rate:
                child = ga._mutate(child)
            new_pop.append(child)

        pop = new_pop
        fits = [ga._eval(p) for p in pop]

        gen_best_idx = min(range(len(fits)), key=lambda i: fits[i])
        if fits[gen_best_idx] < best_obj:
            best_obj = fits[gen_best_idx]
            best_perm = list(pop[gen_best_idx])
            history.append((time.perf_counter() - t0, best_obj))

    # 解码最佳排列
    best_results, _ = decode_permutation(best_perm, dags, pool_factory)

    return best_results, best_obj, history


def run_cpsat_with_tracking(dags, batch_caps, time_limit_s):
    """运行 CPSATScheduler 并跟踪中间解. 返回 (results, obj, history)."""
    pool = make_pool()
    sched = CPSATScheduler(pool, time_limit_s=time_limit_s)
    for dag, st in dags:
        sched.add_batch(deep_copy_dag(dag), submit_time=st)

    # 重建求解过程以插入 callback
    model = cp_model.CpModel()

    all_steps = []
    for bi, batch in enumerate(sched.batches):
        for nid, node in batch.task_dag.nodes.items():
            if not node.completed:
                all_steps.append((bi, nid, node))
    if not all_steps:
        return [], 0, []

    total_dur = sum(n.duration for _, _, n in all_steps)
    horizon = max(sched.horizon, total_dur * 2)

    batch_type_steps = defaultdict(list)
    normal_steps = []
    for bi, nid, node in all_steps:
        if node.machine_type in batch_caps:
            batch_type_steps[node.machine_type].append((bi, nid, node))
        else:
            normal_steps.append((bi, nid, node))

    start_vars, end_vars = {}, {}
    for bi, nid, node in all_steps:
        s = model.new_int_var(0, horizon, f"s_{nid}")
        start_vars[nid] = s
        e = model.new_int_var(0, horizon, f"e_{nid}")
        if node.machine_type in batch_caps:
            model.add(e >= s + node.duration)
        else:
            model.add(e == s + node.duration)
        end_vars[nid] = e

    units_by_type = defaultdict(list)
    for iid, inst in pool.instances.items():
        units_by_type[inst.device_type].append(iid)

    unit_opt_itvs = defaultdict(list)
    assign_vars = {}
    for bi, nid, node in normal_steps:
        candidates = units_by_type.get(node.machine_type, [])
        bools = []
        for uid in candidates:
            b = model.new_bool_var(f"asgn_{nid}_{uid}")
            assign_vars[(nid, uid)] = b
            bools.append(b)
            opt = model.new_optional_interval_var(
                start_vars[nid], node.duration, end_vars[nid], b, f"opt_{nid}_{uid}")
            unit_opt_itvs[uid].append(opt)
        if bools:
            model.add_exactly_one(bools)
    for uid, oitvs in unit_opt_itvs.items():
        if len(oitvs) > 1:
            model.add_no_overlap(oitvs)

    batch_assign = {}
    batch_slot_info = {}
    for dtype, tasks in batch_type_steps.items():
        cap = batch_caps[dtype]
        units = units_by_type.get(dtype, [])
        N = len(tasks)
        M = max(1, math.ceil(N / cap))
        max_dur = max(n.duration for _, _, n in tasks)
        for uid in units:
            slot_intervals = []
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

        for bi, nid, node in tasks:
            bools_all = []
            for uid in units:
                for sb in range(M):
                    ab = model.new_bool_var(f"ba_{nid}_{uid}_{sb}")
                    batch_assign[(nid, uid, sb)] = ab
                    bools_all.append(ab)
                    bs_v, bsz_v, be_v, _, _ = batch_slot_info[(uid, sb)]
                    model.add(start_vars[nid] == bs_v).only_enforce_if(ab)
                    model.add(end_vars[nid] == be_v).only_enforce_if(ab)
                    model.add(bsz_v >= node.duration).only_enforce_if(ab)
            if bools_all:
                model.add_exactly_one(bools_all)

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

    for bi, batch in enumerate(sched.batches):
        dag = batch.task_dag
        for edge in dag.edges:
            src_n = dag.nodes.get(edge.source)
            if src_n and src_n.completed and src_n.end is not None:
                if edge.target in start_vars:
                    model.add(start_vars[edge.target] >= src_n.end)
            elif edge.source in end_vars and edge.target in start_vars:
                model.add(start_vars[edge.target] >= end_vars[edge.source])

    for bi, batch in enumerate(sched.batches):
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

    task_steps_map = defaultdict(list)
    task_weights = {}
    for bi, batch in enumerate(sched.batches):
        tid = batch.task_dag.task_id
        task_weights[tid] = batch.priority
        for nid in batch.task_dag.nodes:
            if nid in end_vars:
                task_steps_map[tid].append(nid)

    obj_terms = []
    for tid, sids in task_steps_map.items():
        if not sids:
            continue
        cv = model.new_int_var(0, horizon, f"C_{tid}")
        model.add_max_equality(cv, [end_vars[sid] for sid in sids])
        w = int(round(task_weights[tid] * _WEIGHT_SCALE))
        obj_terms.append(w * cv)
    if obj_terms:
        model.minimize(sum(obj_terms))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_s
    solver.parameters.num_workers = 8
    t0 = time.perf_counter()
    tracker = _SolutionTracker(t0)
    status = solver.solve(model, tracker)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return [], float("inf"), tracker.history

    # 提取结果
    results = []
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

    for dtype, tasks in batch_type_steps.items():
        units = units_by_type.get(dtype, [])
        N = len(tasks)
        M = max(1, math.ceil(N / batch_caps[dtype]))
        slot_members = defaultdict(list)
        task_slot = {}
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
        obj, _ = compute_weighted_objective(results, [(b.task_dag, 0) for b in sched.batches])
    else:
        obj = float("inf")

    return results, obj, tracker.history


# ══════════════════════════════════════════════════════════
# 3. DAG 构建
# ══════════════════════════════════════════════════════════

def _chain(tid, pri, steps, tcs=None):
    nodes, edges = {}, []
    for i, (sid, mt, dur) in enumerate(steps):
        nodes[sid] = StepNode(step_id=sid, task_id=tid, machine_type=mt, duration=dur)
        if i > 0:
            edges.append(Edge(source=steps[i-1][0], target=sid))
    dag = TaskDAG(task_id=tid, priority=pri, nodes=nodes, edges=edges,
                  time_constraints=tcs or [])
    dag.build_adjacency()
    return dag


def make_assay1(s="", p=1.0):
    # 试剂配制后须尽快送去 PCR; PCR 完成后需冷却再取出
    tcs = [
        TimeConstraint(from_step=f"a1{s}_prep", to_step=f"a1{s}_trp", max_gap=60),
        TimeConstraint(from_step=f"a1{s}_qpcr", to_step=f"a1{s}_tro", min_gap=15, max_gap=30),
    ]
    return _chain(f"PCR{s}", p, [
        (f"a1{s}_trv", "Denso_Arm", 20), (f"a1{s}_prep", "Agilent_vWork", 180),
        (f"a1{s}_trp", "Denso_Arm", 20), (f"a1{s}_qpcr", "LightCycler_480", 900),
        (f"a1{s}_tro", "Denso_Arm", 15)], tcs)


def make_assay2(s="", p=1.0):
    # 配制→送孵育要快; 孵育后需平衡30s; PCR后冷却
    tcs = [
        TimeConstraint(from_step=f"a2{s}_prep", to_step=f"a2{s}_tri", max_gap=60),
        TimeConstraint(from_step=f"a2{s}_inc",  to_step=f"a2{s}_trp", min_gap=30, max_gap=120),
        TimeConstraint(from_step=f"a2{s}_qpcr", to_step=f"a2{s}_tro", min_gap=15, max_gap=30),
    ]
    return _chain(f"PCR_Inc{s}", p, [
        (f"a2{s}_trv", "Denso_Arm", 20), (f"a2{s}_prep", "Agilent_vWork", 180),
        (f"a2{s}_tri", "Denso_Arm", 20), (f"a2{s}_inc", "Incubation_Carrier", 500),
        (f"a2{s}_trp", "Denso_Arm", 20), (f"a2{s}_qpcr", "LightCycler_480", 900),
        (f"a2{s}_tro", "Denso_Arm", 15)], tcs)


def make_elisa(s="", p=1.0):
    # 包被→孵育快; 孵育后需平衡; 洗涤后沥干10s再操作; 显色→读板窗口
    tcs = [
        TimeConstraint(from_step=f"el{s}_coat", to_step=f"el{s}_ti1", max_gap=60),
        TimeConstraint(from_step=f"el{s}_inc1", to_step=f"el{s}_tw1", min_gap=20),
        TimeConstraint(from_step=f"el{s}_w1",   to_step=f"el{s}_tv2", min_gap=10, max_gap=120),
        TimeConstraint(from_step=f"el{s}_sadd", to_step=f"el{s}_ti2", max_gap=60),
        TimeConstraint(from_step=f"el{s}_inc2", to_step=f"el{s}_tw2", min_gap=20),
        TimeConstraint(from_step=f"el{s}_w2",   to_step=f"el{s}_tv3", min_gap=10, max_gap=120),
        TimeConstraint(from_step=f"el{s}_chrom", to_step=f"el{s}_trd", max_gap=300),
    ]
    return _chain(f"ELISA{s}", p, [
        (f"el{s}_tv1", "Denso_Arm", 20), (f"el{s}_coat", "Agilent_vWork", 120),
        (f"el{s}_ti1", "Denso_Arm", 20), (f"el{s}_inc1", "Incubation_Carrier", 600),
        (f"el{s}_tw1", "Denso_Arm", 20), (f"el{s}_w1", "Washer", 90),
        (f"el{s}_tv2", "Denso_Arm", 20), (f"el{s}_sadd", "Agilent_vWork", 150),
        (f"el{s}_ti2", "Denso_Arm", 20), (f"el{s}_inc2", "Incubation_Carrier", 1800),
        (f"el{s}_tw2", "Denso_Arm", 20), (f"el{s}_w2", "Washer", 120),
        (f"el{s}_tv3", "Denso_Arm", 20), (f"el{s}_chrom", "Agilent_vWork", 100),
        (f"el{s}_trd", "Denso_Arm", 20), (f"el{s}_read", "Reader", 60),
        (f"el{s}_tro", "Denso_Arm", 15)], tcs)


def make_blood(s="", p=2.0):
    # 离心后转子减速+样品沉降30s; 分液后尽快送PCR
    tcs = [
        TimeConstraint(from_step=f"bp{s}_cen",  to_step=f"bp{s}_tv",  min_gap=30, max_gap=120),
        TimeConstraint(from_step=f"bp{s}_aliq", to_step=f"bp{s}_tp",  max_gap=60),
        TimeConstraint(from_step=f"bp{s}_pcr",  to_step=f"bp{s}_to",  min_gap=15),
    ]
    return _chain(f"Blood{s}", p, [
        (f"bp{s}_tc", "Denso_Arm", 20), (f"bp{s}_cen", "Centrifuge", 600),
        (f"bp{s}_tv", "Denso_Arm", 20), (f"bp{s}_aliq", "Agilent_vWork", 200),
        (f"bp{s}_tp", "Denso_Arm", 20), (f"bp{s}_pcr", "LightCycler_480", 1200),
        (f"bp{s}_to", "Denso_Arm", 15)], tcs)


def make_pathogen(s="", p=3.0):
    # 提取→封板快; 封板胶固化10s; PCR后冷却
    tcs = [
        TimeConstraint(from_step=f"pd{s}_ext",  to_step=f"pd{s}_ts",  max_gap=60),
        TimeConstraint(from_step=f"pd{s}_seal", to_step=f"pd{s}_tp",  min_gap=10, max_gap=120),
        TimeConstraint(from_step=f"pd{s}_pcr",  to_step=f"pd{s}_to",  min_gap=15),
    ]
    return _chain(f"Pathogen{s}", p, [
        (f"pd{s}_tv", "Denso_Arm", 20), (f"pd{s}_ext", "Agilent_vWork", 300),
        (f"pd{s}_ts", "Denso_Arm", 20), (f"pd{s}_seal", "Sealer", 45),
        (f"pd{s}_tp", "Denso_Arm", 20), (f"pd{s}_pcr", "LightCycler_480", 1500),
        (f"pd{s}_to", "Denso_Arm", 15)], tcs)


def make_sw_elisa(s="", p=1.5):
    # 包被→孵育; 每次孵育后平衡20s; 每次洗涤后沥干10s; 显色→读板窗口
    tcs = [
        TimeConstraint(from_step=f"se{s}_coat", to_step=f"se{s}_ti1", max_gap=60),
        TimeConstraint(from_step=f"se{s}_inc1", to_step=f"se{s}_tw1", min_gap=20),
        TimeConstraint(from_step=f"se{s}_w1",   to_step=f"se{s}_tv2", min_gap=10, max_gap=120),
        TimeConstraint(from_step=f"se{s}_ab1",  to_step=f"se{s}_ti2", max_gap=60),
        TimeConstraint(from_step=f"se{s}_inc2", to_step=f"se{s}_tw2", min_gap=20),
        TimeConstraint(from_step=f"se{s}_w2",   to_step=f"se{s}_tv3", min_gap=10, max_gap=120),
        TimeConstraint(from_step=f"se{s}_ab2",  to_step=f"se{s}_ti3", max_gap=60),
        TimeConstraint(from_step=f"se{s}_inc3", to_step=f"se{s}_tw3", min_gap=20),
        TimeConstraint(from_step=f"se{s}_w3",   to_step=f"se{s}_tv4", min_gap=10, max_gap=120),
        TimeConstraint(from_step=f"se{s}_chrom", to_step=f"se{s}_trd", max_gap=300),
    ]
    return _chain(f"SwELISA{s}", p, [
        (f"se{s}_tv1", "Denso_Arm", 20), (f"se{s}_coat", "Agilent_vWork", 120),
        (f"se{s}_ti1", "Denso_Arm", 20), (f"se{s}_inc1", "Incubation_Carrier", 900),
        (f"se{s}_tw1", "Denso_Arm", 20), (f"se{s}_w1", "Washer", 90),
        (f"se{s}_tv2", "Denso_Arm", 20), (f"se{s}_ab1", "Agilent_vWork", 120),
        (f"se{s}_ti2", "Denso_Arm", 20), (f"se{s}_inc2", "Incubation_Carrier", 1200),
        (f"se{s}_tw2", "Denso_Arm", 20), (f"se{s}_w2", "Washer", 120),
        (f"se{s}_tv3", "Denso_Arm", 20), (f"se{s}_ab2", "Agilent_vWork", 120),
        (f"se{s}_ti3", "Denso_Arm", 20), (f"se{s}_inc3", "Incubation_Carrier", 600),
        (f"se{s}_tw3", "Denso_Arm", 20), (f"se{s}_w3", "Washer", 120),
        (f"se{s}_tv4", "Denso_Arm", 20), (f"se{s}_chrom", "Agilent_vWork", 100),
        (f"se{s}_trd", "Denso_Arm", 20), (f"se{s}_read", "Reader", 60),
        (f"se{s}_tro", "Denso_Arm", 15)], tcs)


def make_20_dags():
    random.seed(42)
    makers = [make_assay1, make_assay2, make_elisa, make_blood, make_pathogen, make_sw_elisa]
    default_p = [1.0, 1.0, 1.0, 2.0, 3.0, 1.5]
    dist = [0]*4 + [1]*4 + [2]*4 + [3]*3 + [4]*3 + [5]*2
    dags = []
    for i, idx in enumerate(dist):
        p = default_p[idx] + random.uniform(-0.3, 0.3)
        dags.append((makers[idx](s=f"_{i:02d}", p=round(p, 2)), 0))
    return dags


# ══════════════════════════════════════════════════════════
# 5. 执行
# ══════════════════════════════════════════════════════════

def run_one(dags, scheduler_cls, **kwargs):
    pool = make_pool()
    sched = scheduler_cls(pool, **kwargs)
    for dag, st in dags:
        sched.add_batch(deep_copy_dag(dag), submit_time=st)
    t0 = time.perf_counter()
    res = sched.schedule()
    elapsed = time.perf_counter() - t0
    return res, elapsed


def _report(alg_name, res, elapsed, dags, out, prefix=""):
    """打印结果 + 验证时间约束 + 画图. 返回 (ms, obj, t_ms, n_violations)."""
    ms = max(r.end for r in res)
    obj, tc = compute_weighted_objective(res, dags)
    t_ms = elapsed * 1000
    violations = validate_time_constraints(res, dags)
    nv = len(violations)
    tc_tag = f"  TC_viol={nv}" if nv > 0 else "  TC=OK"
    print(f"  [{alg_name:25s}] ms={ms:>6d}  obj={obj:>10.0f}  t={t_ms:.1f}ms{tc_tag}")
    if violations:
        for v in violations[:8]:
            print(v)
        if nv > 8:
            print(f"    ... and {nv - 8} more")
    plot_dual_gantt(res, dags, f"{alg_name} (ms={ms})",
                    os.path.join(out, f"{prefix}{alg_name}.png"), obj, tc)
    return (ms, obj, t_ms, nv)


def run_benchmark_v3():
    out = os.path.join(os.path.dirname(__file__), "..", "benchmark_output_v3")
    os.makedirs(out, exist_ok=True)

    dags = make_20_dags()
    total_steps = sum(len(d.nodes) for d, _ in dags)
    total_tcs = sum(len(d.time_constraints) for d, _ in dags)
    heuristic_algs = [cls.algorithm_name for cls in HEURISTIC_CLASSES]

    print(f"20 batches, {total_steps} steps, {total_tcs} time constraints")
    print(f"Batch devices: {BATCH_CAPS}\n")

    stats = {}

    # ── 启发式算法 ──
    print(f"{'='*70}")
    print("  Heuristic Batch-Slot (同步启停, 允许部分填充)")
    print(f"{'='*70}")
    for alg in heuristic_algs:
        batch_cls = BATCH_SCHEDULERS.get(f"Batch_{alg}")
        if batch_cls is None:
            continue
        res, elapsed = run_one(dags, batch_cls)
        if res:
            stats[alg] = _report(alg, res, elapsed, dags, out)

    # ── GA 遗传算法 ──
    print(f"\n{'='*70}")
    print("  GA Genetic Algorithm (120s)")
    print(f"{'='*70}")
    res, elapsed = run_one(dags, GeneticAlgorithmScheduler, time_limit_s=120.0)
    if res:
        stats["GA"] = _report("GA", res, elapsed, dags, out)

    # ── CP-SAT 精确求解 ──
    print(f"\n{'='*70}")
    print("  CP-SAT Batch-Slot (120s, 精确求解)")
    print(f"{'='*70}")
    res, elapsed = run_one(dags, CPSATScheduler, time_limit_s=120.0)
    if res:
        stats["CP-SAT"] = _report("CP-SAT", res, elapsed, dags, out)

    # ── 对比图 ──
    plot_algorithm_comparison(stats, os.path.join(out, "00_algorithm_comparison.png"))

    # ── GA vs CP-SAT 收敛对比 ──
    print(f"\n{'='*70}")
    print("  GA vs CP-SAT Convergence Comparison")
    print(f"{'='*70}")

    time_limits = [2, 5, 10, 30, 60, 120]
    convergence_data = {}

    for tl in time_limits:
        print(f"\n  --- Time limit = {tl}s ---")
        # GA
        _, ga_final, ga_h = run_ga_with_tracking(dags, BATCH_CAPS, float(tl))
        print(f"    GA:     obj={ga_final:.0f}")

        # CP-SAT with tracking
        _, cp_final, cp_h = run_cpsat_with_tracking(dags, BATCH_CAPS, float(tl))
        print(f"    CP-SAT: obj={cp_final:.0f}")

        convergence_data[tl] = {"GA": ga_h, "CP-SAT": cp_h}

        # 单独画收敛图
        plot_convergence(
            ga_h, cp_h,
            f"GA vs CP-SAT (limit={tl}s)",
            os.path.join(out, f"convergence_{tl}s.png"))

    # 综合对比图
    plot_convergence_multi(convergence_data,
                           os.path.join(out, "00_convergence_comparison.png"))

    # 汇总表
    print(f"\n{'='*70}")
    print("  Convergence Summary")
    print(f"{'='*70}")
    print(f"  {'Limit':>6s}  {'GA obj':>10s}  {'CP-SAT obj':>10s}  {'GA/CP':>8s}")
    for tl in time_limits:
        data = convergence_data[tl]
        ga_final = data["GA"][-1][1] if data["GA"] else float("inf")
        cp_final = data["CP-SAT"][-1][1] if data["CP-SAT"] else float("inf")
        ratio = ga_final / cp_final if cp_final > 0 else float("inf")
        print(f"  {tl:>5d}s  {ga_final:>10.0f}  {cp_final:>10.0f}  {ratio:>7.3f}x")

    print(f"\nAll output: {out}/")
    return stats


def test_biolab_benchmark_v3():
    stats = run_benchmark_v3()
    assert len(stats) > 0, "no results"


if __name__ == "__main__":
    run_benchmark_v3()
