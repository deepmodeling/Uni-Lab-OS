"""6 种步骤调度算法, 从 mix_real.py 适配到新结构.

每个算法继承 SchedulerBase, 只需实现 _schedule_ready_tasks().
"""

from __future__ import annotations

import heapq
from collections import defaultdict

from scheduler.core.base import SchedulerBase
from scheduler.models.dag import StepNode
from scheduler.models.resources import DevicePool


# ── 算法注册表 ──────────────────────────────────────────

ALGORITHM_REGISTRY: dict[str, type[SchedulerBase]] = {}


def register_algorithm(name: str):
    """装饰器: 将调度算法注册到全局注册表."""

    def _wrap(cls: type[SchedulerBase]):
        ALGORITHM_REGISTRY[name] = cls
        cls.algorithm_name = name
        return cls

    return _wrap


def get_algorithm(name: str) -> type[SchedulerBase]:
    if name not in ALGORITHM_REGISTRY:
        available = ", ".join(sorted(ALGORITHM_REGISTRY))
        raise ValueError(f"Unknown algorithm '{name}'. Available: {available}")
    return ALGORITHM_REGISTRY[name]


def list_algorithms() -> list[str]:
    return sorted(ALGORITHM_REGISTRY.keys())


# ── 1. Greedy: 高优先级 + 最短任务优先 ──────────────────


@register_algorithm("Greedy")
class GreedyScheduler(SchedulerBase):
    def _schedule_ready_tasks(self) -> None:
        ready = self._filter_ready_by_device_lock(self._collect_ready())
        # 排序: 批次优先级降序, 时长升序
        scored = []
        for batch_idx, node_id, node in ready:
            priority = self.batches[batch_idx].weight
            scored.append((-priority, node.duration, batch_idx, node_id, node))
        scored.sort()

        for _, _, batch_idx, node_id, node in scored:
            device_id = self._get_device(node.machine_type)
            if device_id is not None:
                self._do_allocate(batch_idx, node_id, node, device_id)


# ── 2. CriticalPath: 关键路径最长优先 ───────────────────


@register_algorithm("CriticalPath")
class CriticalPathScheduler(SchedulerBase):
    def __init__(self, device_pool: DevicePool) -> None:
        super().__init__(device_pool)
        self._critical_paths: dict[int, dict[str, int]] = {}

    def add_batch(self, task_dag, submit_time: int = 0) -> None:
        super().add_batch(task_dag, submit_time)
        batch_idx = len(self.batches) - 1
        self._critical_paths[batch_idx] = self._compute_critical_paths(batch_idx)

    def _compute_critical_paths(self, batch_idx: int) -> dict[str, int]:
        """计算每个节点到终点的最长路径长度."""
        dag = self.batches[batch_idx].task_dag
        graph: dict[str, list[str]] = defaultdict(list)
        in_degree: dict[str, int] = defaultdict(int)

        for edge in dag.edges:
            graph[edge.source].append(edge.target)
            in_degree[edge.target] += 1

        dist = {nid: node.duration for nid, node in dag.nodes.items()}
        queue = [nid for nid in dag.nodes if in_degree[nid] == 0]

        while queue:
            u = queue.pop(0)
            for v in graph[u]:
                if dist[v] < dist[u] + dag.nodes[v].duration:
                    dist[v] = dist[u] + dag.nodes[v].duration
                in_degree[v] -= 1
                if in_degree[v] == 0:
                    queue.append(v)

        return dist

    def _schedule_ready_tasks(self) -> None:
        ready = self._filter_ready_by_device_lock(self._collect_ready())
        scored = []
        for batch_idx, node_id, node in ready:
            priority = self.batches[batch_idx].weight
            cp_len = self._critical_paths.get(batch_idx, {}).get(node_id, 0)
            scored.append((-priority, -cp_len, batch_idx, node_id, node))
        scored.sort()

        for _, _, batch_idx, node_id, node in scored:
            device_id = self._get_device(node.machine_type)
            if device_id is not None:
                self._do_allocate(batch_idx, node_id, node, device_id)


# ── 3. WeightedCriticalPath: 带权重的关键路径 (默认算法) ─


@register_algorithm("WeightedCriticalPath")
class WeightedCriticalPathScheduler(CriticalPathScheduler):
    """与 CriticalPath 相同, 但排序时将权重乘以关键路径长度."""

    def _schedule_ready_tasks(self) -> None:
        ready = self._filter_ready_by_device_lock(self._collect_ready())
        scored = []
        for batch_idx, node_id, node in ready:
            priority = self.batches[batch_idx].weight
            cp_len = self._critical_paths.get(batch_idx, {}).get(node_id, 0)
            weighted_score = priority * cp_len
            scored.append((-weighted_score, batch_idx, node_id, node))
        scored.sort()

        for _, batch_idx, node_id, node in scored:
            device_id = self._get_device(node.machine_type)
            if device_id is not None:
                self._do_allocate(batch_idx, node_id, node, device_id)


# ── 4. DynamicPriority: 后继节点数动态优先 ──────────────


@register_algorithm("DynamicPriority")
class DynamicPriorityScheduler(SchedulerBase):
    def _calculate_node_priority(self, node_id: str, batch_idx: int) -> int:
        dag = self.batches[batch_idx].task_dag
        return len(dag.children.get(node_id, []))

    def _schedule_ready_tasks(self) -> None:
        ready = self._filter_ready_by_device_lock(self._collect_ready())
        heap = []
        for batch_idx, node_id, node in ready:
            batch_priority = self.batches[batch_idx].weight
            node_priority = self._calculate_node_priority(node_id, batch_idx)
            total = batch_priority * 1000 + node_priority
            heap.append((-total, batch_idx, node_id, node))

        heapq.heapify(heap)
        while heap:
            _, batch_idx, node_id, node = heapq.heappop(heap)
            device_id = self._get_device(node.machine_type)
            if device_id is not None:
                self._do_allocate(batch_idx, node_id, node, device_id)


# ── 5. MultiObjective: 多目标加权评分 ───────────────────


@register_algorithm("MultiObjective")
class MultiObjectiveScheduler(SchedulerBase):
    def _calculate_score(self, node: StepNode, batch_priority: float) -> float:
        duration_score = 1.0 / max(node.duration, 1)
        base_score = 0.6 * 0.5 + 0.4 * duration_score  # 简化利用率为 0.5
        return base_score * (2**batch_priority)

    def _schedule_ready_tasks(self) -> None:
        ready = self._filter_ready_by_device_lock(self._collect_ready())
        scored = []
        for batch_idx, node_id, node in ready:
            priority = self.batches[batch_idx].weight
            device_id = self._get_device(node.machine_type)
            if device_id is not None:
                score = self._calculate_score(node, priority)
                scored.append((-score, batch_idx, node_id, node, device_id))

        heapq.heapify(scored)
        while scored:
            _, batch_idx, node_id, node, device_id = heapq.heappop(scored)
            # 重新检查设备 (可能已被同轮分配)
            actual = self._get_device(node.machine_type)
            if actual is not None:
                self._do_allocate(batch_idx, node_id, node, actual)


# ── 6. Realtime: 优先级 + FIFO ───────────────────────────


@register_algorithm("Realtime")
class RealtimeScheduler(SchedulerBase):
    def _schedule_ready_tasks(self) -> None:
        ready = self._filter_ready_by_device_lock(self._collect_ready())
        scored = []
        for batch_idx, node_id, node in ready:
            priority = self.batches[batch_idx].weight
            submit_time = self.batches[batch_idx].submit_time
            scored.append((-priority, submit_time, batch_idx, node_id, node))
        scored.sort()

        for _, _, batch_idx, node_id, node in scored:
            device_id = self._get_device(node.machine_type)
            if device_id is not None:
                self._do_allocate(batch_idx, node_id, node, device_id)


# ── 7. HybridCriticality: 混合关键度 (EDF + SJF) ────────


@register_algorithm("HybridCriticality")
class HybridCriticalityScheduler(SchedulerBase):
    critical_threshold_ratio: float = 2.0

    def _classify(
        self, node: StepNode, batch: "BatchInfo"
    ) -> tuple[str, int]:
        """返回 (criticality, deadline)."""
        from scheduler.core.base import BatchInfo

        submit_time = batch.submit_time
        deadline = submit_time + node.duration * 5
        remaining = deadline - self.current_time
        ratio = remaining / max(node.duration, 1)
        criticality = "critical" if ratio < self.critical_threshold_ratio else "non-critical"
        return criticality, deadline

    def _schedule_ready_tasks(self) -> None:
        ready = self._filter_ready_by_device_lock(self._collect_ready())
        critical: list[tuple] = []
        non_critical: list[tuple] = []

        for batch_idx, node_id, node in ready:
            batch = self.batches[batch_idx]
            crit, deadline = self._classify(node, batch)
            if crit == "critical":
                critical.append((deadline, -batch.weight, batch_idx, node_id, node))
            else:
                non_critical.append((-batch.weight, node.duration, batch_idx, node_id, node))

        critical.sort()
        non_critical.sort()

        for task_list in (critical, non_critical):
            for *_, batch_idx, node_id, node in task_list:
                device_id = self._get_device(node.machine_type)
                if device_id is not None:
                    self._do_allocate(batch_idx, node_id, node, device_id)
