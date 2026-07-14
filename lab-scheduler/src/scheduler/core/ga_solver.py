"""遗传算法调度器 + 排列解码器.

GA 染色体 = step 索引的排列, OX 交叉 + 锦标赛选择. decode_permutation 把
一个排列还原为完整时刻表 (事件驱动仿真, 支持 batch_capacity 与 min_gap).
"""

from __future__ import annotations

import copy
import heapq
import random
import time
from collections import defaultdict
from typing import Callable

from scheduler.core.base import SchedulerBase
from scheduler.core.step_algorithms import register_algorithm
from scheduler.models.dag import TaskDAG
from scheduler.models.resources import DeviceInstance, DevicePool
from scheduler.models.schedule_result import ScheduledStep


def _deep_copy_dag(dag: TaskDAG) -> TaskDAG:
    """深拷贝 DAG 并重置节点状态."""
    d = copy.deepcopy(dag)
    for n in d.nodes.values():
        n.reset()
    d.build_adjacency()
    return d


def _compute_weighted_objective(
    results: list[ScheduledStep], dags: list[tuple[TaskDAG, int]]
) -> tuple[float, dict[str, int]]:
    """计算加权完工时间目标."""
    tw = {dag.task_id: dag.weight for dag, _ in dags}
    tc = {}
    for r in results:
        if r.task_id not in tc or r.end > tc[r.task_id]:
            tc[r.task_id] = r.end
    total = sum(tw.get(tid, 1.0) * ct for tid, ct in tc.items())
    return total, tc


def decode_permutation(
    perm: list[int],
    dags: list[tuple[TaskDAG, int]],
    pool_factory: Callable[[], DevicePool],
) -> tuple[list[ScheduledStep], float]:
    """将排列解码为调度结果 (事件驱动仿真).

    perm: step 全局索引的排列, 决定就绪任务的调度优先顺序.
    dags: [(TaskDAG, submit_time), ...]
    pool_factory: 返回新 DevicePool 的工厂函数
    返回: (results: list[ScheduledStep], objective: float)
    """
    # 建立全局 step 列表
    all_steps = []  # [(batch_idx, node_id)]
    for bi, (dag_orig, _) in enumerate(dags):
        for nid in dag_orig.nodes:
            all_steps.append((bi, nid))

    n = len(all_steps)
    assert len(perm) == n

    # step index -> 排列中的优先级 (越小越优先)
    priority_rank = [0] * n
    for rank, idx in enumerate(perm):
        priority_rank[idx] = rank

    # 深拷贝 DAG 并重建邻接
    sim_dags = []
    for dag_orig, st in dags:
        d = _deep_copy_dag(dag_orig)
        sim_dags.append(d)

    # 设备池
    pool = pool_factory()

    # 从 pool 推导 batch_caps
    batch_caps: dict[str, int] = {}
    for inst in pool.instances.values():
        cur = batch_caps.get(inst.device_type, 1)
        if inst.batch_capacity > cur:
            batch_caps[inst.device_type] = inst.batch_capacity
    batch_caps = {dt: c for dt, c in batch_caps.items() if c > 1}

    # step -> (batch_idx, node_id) 的优先级
    step_prio = {}
    for gi, (bi, nid) in enumerate(all_steps):
        step_prio[(bi, nid)] = priority_rank[gi]

    # 事件驱动仿真
    current_time = 0
    event_queue = []  # (end_time, tie, (bi, nid))
    results = []

    def get_device(dtype, at_time):
        candidates = pool._type_index.get(dtype, [])
        best = None
        for iid in candidates:
            if pool.instances[iid].available_from <= at_time:
                if best is None or pool.instances[iid].available_from < pool.instances[best].available_from:
                    best = iid
        return best

    def get_ready():
        ready = []
        for bi, dag in enumerate(sim_dags):
            for nid, node in dag.nodes.items():
                if not node.completed and not node.ready:
                    parents = dag.parents.get(nid, [])
                    if all(
                        dag.nodes[p].completed
                        and dag.nodes[p].end is not None
                        and dag.nodes[p].end <= current_time
                        for p in parents
                    ) if parents else True:
                        node.ready = True
                if node.ready and not node.completed:
                    ready.append((bi, nid, node))
        return ready

    def compute_earliest(bi, nid):
        dag = sim_dags[bi]
        earliest = current_time
        for tc in dag.time_constraints:
            if tc.to_step == nid and tc.min_gap is not None:
                fn = dag.nodes.get(tc.from_step)
                if fn and fn.completed and fn.end is not None:
                    earliest = max(earliest, fn.end + tc.min_gap)
        return earliest

    def schedule_step():
        ready = get_ready()
        if not ready:
            return False

        # 按排列优先级排序
        ready.sort(key=lambda x: step_prio[(x[0], x[1])])

        # 分离批处理 vs 普通
        batch_by_type = defaultdict(list)
        normal = []
        for bi, nid, node in ready:
            if node.machine_type in batch_caps:
                batch_by_type[node.machine_type].append((bi, nid, node))
            else:
                normal.append((bi, nid, node))

        scheduled_any = False

        # 批处理
        for dtype, tasks in batch_by_type.items():
            cap = batch_caps[dtype]
            remaining = list(tasks)
            while remaining:
                did = get_device(dtype, current_time)
                if did is None:
                    break
                batch = remaining[:cap]
                remaining = remaining[cap:]

                earliest = current_time
                for bi, nid, node in batch:
                    earliest = max(earliest, compute_earliest(bi, nid))
                batch_dur = max(n.duration for _, _, n in batch)
                start = earliest
                end = start + batch_dur
                pool.allocate(did, end)

                for slot_idx, (bi, nid, node) in enumerate(batch):
                    slot_id = f"{did}_s{slot_idx}"
                    node.completed = True
                    node.ready = False
                    node.start = start
                    node.end = end
                    node.assigned_device = slot_id
                    heapq.heappush(event_queue, (end, id(node), (bi, nid)))
                    results.append(ScheduledStep(
                        step_id=node.step_id, task_id=node.task_id,
                        start=start, end=end, device_instance=slot_id))
                scheduled_any = True

        # 普通任务
        for bi, nid, node in normal:
            did = get_device(node.machine_type, current_time)
            if did is None:
                continue
            start = compute_earliest(bi, nid)
            end = start + node.duration
            pool.allocate(did, end)
            node.completed = True
            node.ready = False
            node.start = start
            node.end = end
            node.assigned_device = did
            heapq.heappush(event_queue, (end, id(node), (bi, nid)))
            results.append(ScheduledStep(
                step_id=node.step_id, task_id=node.task_id,
                start=start, end=end, device_instance=did))
            scheduled_any = True

        return scheduled_any

    # 主循环
    schedule_step()
    iters = 0
    while event_queue and iters < 100_000:
        t, _, (bi, nid) = heapq.heappop(event_queue)
        current_time = t
        dag = sim_dags[bi]
        for child_id in dag.children.get(nid, []):
            child = dag.nodes[child_id]
            if not child.completed:
                parents = dag.parents.get(child_id, [])
                if all(
                    dag.nodes[p].completed
                    and dag.nodes[p].end is not None
                    and dag.nodes[p].end <= current_time
                    for p in parents
                ):
                    child.ready = True
        schedule_step()
        iters += 1

    # 计算目标
    if results:
        obj, _ = _compute_weighted_objective(results, dags)
    else:
        obj = float("inf")
    return results, obj


class _GeneticAlgorithm:
    """排列编码遗传算法.

    染色体: step 索引的排列, 决定调度优先顺序.
    解码: decode_permutation 事件驱动仿真.
    交叉: Order Crossover (OX).
    变异: swap + insert.
    选择: 锦标赛选择 (size=3).
    """

    def __init__(self, dags, pool_factory,
                 pop_size=60, elite_count=5, mutation_rate=0.15,
                 tournament_size=3, seed=42):
        self.dags = dags
        self.pool_factory = pool_factory
        self.pop_size = pop_size
        self.elite_count = elite_count
        self.mutation_rate = mutation_rate
        self.tournament_size = tournament_size
        self.rng = random.Random(seed)

        # 总 step 数
        self.n_steps = sum(len(d.nodes) for d, _ in dags)

    def _eval(self, perm):
        _, obj = decode_permutation(perm, self.dags, self.pool_factory)
        return obj

    def _random_perm(self):
        p = list(range(self.n_steps))
        self.rng.shuffle(p)
        return p

    def _ox_crossover(self, p1, p2):
        """Order Crossover (OX)."""
        n = len(p1)
        if n < 2:
            return list(p1)
        a, b = sorted(self.rng.sample(range(n), 2))
        child = [-1] * n
        child[a:b+1] = p1[a:b+1]
        used = set(child[a:b+1])
        pos = (b + 1) % n
        for gene in p2[b+1:] + p2[:b+1]:
            if gene not in used:
                child[pos] = gene
                used.add(gene)
                pos = (pos + 1) % n
        return child

    def _mutate(self, perm):
        p = list(perm)
        n = len(p)
        if n < 2:
            return p
        if self.rng.random() < 0.5:
            # swap
            i, j = self.rng.sample(range(n), 2)
            p[i], p[j] = p[j], p[i]
        else:
            # insert
            i = self.rng.randint(0, n-1)
            j = self.rng.randint(0, n-1)
            gene = p.pop(i)
            p.insert(j, gene)
        return p

    def _tournament(self, pop, fits):
        indices = self.rng.sample(range(len(pop)), self.tournament_size)
        best = min(indices, key=lambda i: fits[i])
        return pop[best]

    def run(self, time_limit_s=10.0):
        """运行 GA, 返回 (best_results, best_obj).

        time_limit_s: 时间限制 (秒)
        """
        t0 = time.perf_counter()

        # 初始化种群
        pop = []
        while len(pop) < self.pop_size:
            pop.append(self._random_perm())

        # 评估初始种群
        fits = [self._eval(p) for p in pop]
        best_idx = min(range(len(fits)), key=lambda i: fits[i])
        best_obj = fits[best_idx]
        best_perm = list(pop[best_idx])

        gen = 0
        while True:
            elapsed = time.perf_counter() - t0
            if elapsed >= time_limit_s:
                break
            gen += 1

            # 选择 + 交叉 + 变异
            new_pop = []
            # 精英保留
            ranked = sorted(range(len(pop)), key=lambda i: fits[i])
            for i in ranked[:self.elite_count]:
                new_pop.append(list(pop[i]))

            while len(new_pop) < self.pop_size:
                p1 = self._tournament(pop, fits)
                p2 = self._tournament(pop, fits)
                child = self._ox_crossover(p1, p2)
                if self.rng.random() < self.mutation_rate:
                    child = self._mutate(child)
                new_pop.append(child)

            pop = new_pop
            fits = [self._eval(p) for p in pop]

            gen_best_idx = min(range(len(fits)), key=lambda i: fits[i])
            if fits[gen_best_idx] < best_obj:
                best_obj = fits[gen_best_idx]
                best_perm = list(pop[gen_best_idx])

        # 解码最佳排列获取完整结果
        best_results, _ = decode_permutation(
            best_perm, self.dags, self.pool_factory)

        return best_results, best_obj


@register_algorithm("GA")
class GeneticAlgorithmScheduler(SchedulerBase):
    """遗传算法调度器 (SchedulerBase 包装).

    将 GA 包装为 SchedulerBase 子类以注册到 ALGORITHM_REGISTRY.
    """

    def __init__(self, device_pool: DevicePool, time_limit_s: float = 10.0):
        super().__init__(device_pool)
        self.time_limit_s = time_limit_s

    def _schedule_ready_tasks(self) -> None:
        # GA 不使用事件驱动循环
        pass

    def schedule(self) -> list[ScheduledStep]:
        """覆盖基类: 直接运行 GA, 不走事件仿真循环."""
        # 构建 dags 列表
        dags = [(b.task_dag, b.submit_time) for b in self.batches]
        if not dags:
            return []

        # 构建 pool_factory (快照当前 device_pool 配置)
        device_types = {}
        for inst in self.device_pool.instances.values():
            if inst.device_type not in device_types:
                device_types[inst.device_type] = {
                    "count": 0,
                    "batch_capacity": inst.batch_capacity,
                }
            device_types[inst.device_type]["count"] += 1

        def pool_factory():
            pool = DevicePool()
            for dtype, info in device_types.items():
                pool.add_device_type(dtype, info["count"], info["batch_capacity"])
            return pool

        # 运行 GA
        ga = _GeneticAlgorithm(dags, pool_factory)
        results, obj = ga.run(self.time_limit_s)

        # 写回节点状态
        for r in results:
            for batch in self.batches:
                if r.task_id == batch.task_dag.task_id:
                    node = batch.task_dag.nodes.get(r.step_id)
                    if node:
                        node.completed = True
                        node.start = r.start
                        node.end = r.end
                        node.assigned_device = r.device_instance

        self.completed = results
        if results:
            self.current_time = max(r.end for r in results)
        return results
