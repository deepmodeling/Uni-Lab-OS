"""批处理调度器工厂.

为每个非批 SchedulerBase 子类生成一个 Batch_<Name> 子类. 调度时把同
machine_type 的就绪节点打包为批次, 槽位数从 DeviceInstance.batch_capacity 读取.
"""

from __future__ import annotations

import heapq
from collections import defaultdict

from scheduler.core.base import SchedulerBase
from scheduler.core.step_algorithms import (
    ALGORITHM_REGISTRY,
    CriticalPathScheduler,
    DynamicPriorityScheduler,
    GreedyScheduler,
    HybridCriticalityScheduler,
    MultiObjectiveScheduler,
    RealtimeScheduler,
    WeightedCriticalPathScheduler,
)
from scheduler.models.schedule_result import ScheduledStep


def _derive_batch_caps(device_pool) -> dict[str, int]:
    """Per-type max batch_capacity; types with cap=1 are excluded."""
    caps: dict[str, int] = {}
    for inst in device_pool.instances.values():
        cur = caps.get(inst.device_type, 1)
        if inst.batch_capacity > cur:
            caps[inst.device_type] = inst.batch_capacity
    return {dt: c for dt, c in caps.items() if c > 1}


def make_batch_scheduler(base_cls: type[SchedulerBase]) -> type[SchedulerBase]:
    """Return a dynamic subclass of base_cls with batch-aware allocation.

    Faithful port of V3's make_batch_scheduler. Behavior:
    - At __init__, build self._batch_caps from device_pool (per-type max cap, exclude cap=1).
    - Override _init_ready_nodes to require parent.end <= current_time (fixes
      premature ready-marking caused by batch completion).
    - Override _compute_earliest_start to honor min_gap.
    - Override _do_allocate to use _compute_earliest_start as start.
    - Override _schedule_ready_tasks: partition ready into batch vs normal,
      batch path packs up to cap per slot then calls super for normals.
    - Override _do_batch_allocate: same start/end across the batch; slot ids
      are f"{device_id}_s{slot_idx}".
    """

    class BatchScheduler(base_cls):  # type: ignore[misc, valid-type]
        algorithm_name = f"Batch_{base_cls.algorithm_name}"

        def __init__(self, device_pool):
            super().__init__(device_pool)
            self._batch_caps = _derive_batch_caps(device_pool)

        def _init_ready_nodes(self, batch_idx):
            """覆盖: 父节点不仅要 completed, end 也必须 <= current_time.

            修复批处理提前标记 completed 导致子节点过早就绪的问题.
            """
            dag = self.batches[batch_idx].task_dag
            for node_id, node in dag.nodes.items():
                if not node.ready and not node.completed:
                    parents = dag.parents.get(node_id, [])
                    if all(
                        dag.nodes[p].completed
                        and dag.nodes[p].end is not None
                        and dag.nodes[p].end <= self.current_time
                        for p in parents
                    ) if parents else True:
                        node.ready = True

        def _compute_earliest_start(self, batch_idx, node_id):
            """根据 min_gap 约束计算该步骤的最早启动时间."""
            dag = self.batches[batch_idx].task_dag
            earliest = self.current_time
            for tc in dag.time_constraints:
                if tc.to_step == node_id and tc.min_gap is not None:
                    from_node = dag.nodes.get(tc.from_step)
                    if from_node and from_node.completed and from_node.end is not None:
                        earliest = max(earliest, from_node.end + tc.min_gap)
            return earliest

        def _do_allocate(self, batch_idx, node_id, node, device_id):
            """覆盖: start 取 max(current_time, min_gap 约束)."""
            start = self._compute_earliest_start(batch_idx, node_id)
            end = start + node.duration
            self._allocate_device(device_id, end)
            node.completed = True
            node.ready = False
            node.start = start
            node.end = end
            node.assigned_device = device_id
            heapq.heappush(self.event_queue, (end, id(node), (batch_idx, node_id)))
            self.completed.append(ScheduledStep(
                step_id=node.step_id, task_id=node.task_id,
                start=start, end=end, device_instance=device_id,
            ))

        def _schedule_ready_tasks(self):
            # Device lock is a hard gate for both batch and normal actions.
            # Busy-device nodes remain ready in their DAG and are retried
            # after the next completion event.
            ready = self._filter_ready_by_device_lock(self._collect_ready())

            # 分离: 批处理设备 vs 普通设备
            batch_by_type: dict[str, list] = defaultdict(list)
            for bi, ni, n in ready:
                if n.machine_type in self._batch_caps:
                    pri = self.batches[bi].weight
                    batch_by_type[n.machine_type].append((bi, ni, n, pri))

            # 分配批处理任务
            for dtype, tasks in batch_by_type.items():
                cap = self._batch_caps[dtype]
                # 按优先级降序、时长升序排序 (通用策略)
                tasks.sort(key=lambda x: (-x[3], x[2].duration))
                remaining = tasks
                while remaining:
                    did = self._get_device(dtype)
                    if did is None:
                        break
                    batch = remaining[:cap]
                    remaining = remaining[cap:]
                    self._do_batch_allocate(batch, did)

            # 批处理任务已标记 completed, 调用原算法处理剩余普通任务
            super()._schedule_ready_tasks()

        def _do_batch_allocate(self, batch_items, device_id):
            """同时启动一批任务, 统一结束时间. 尊重 min_gap."""
            # 批内所有任务的最早启动时间取 max
            earliest = self.current_time
            for bi, ni, node, _ in batch_items:
                earliest = max(earliest, self._compute_earliest_start(bi, ni))
            batch_dur = max(item[2].duration for item in batch_items)
            start = earliest
            end = start + batch_dur
            self._allocate_device(device_id, end)

            for slot_idx, (bi, ni, node, _) in enumerate(batch_items):
                slot_id = f"{device_id}_s{slot_idx}"
                node.completed = True
                node.ready = False
                node.start = start
                node.end = end  # 关键: 所有任务统一结束
                node.assigned_device = slot_id

                heapq.heappush(
                    self.event_queue, (end, id(node), (bi, ni))
                )
                self.completed.append(ScheduledStep(
                    step_id=node.step_id,
                    task_id=node.task_id,
                    start=start,
                    end=end,
                    device_instance=slot_id,
                ))

    BatchScheduler.__name__ = f"Batch{base_cls.__name__}"
    BatchScheduler.__qualname__ = BatchScheduler.__name__
    return BatchScheduler


# 为每个启发式算法创建批处理版本
_HEURISTICS = [
    GreedyScheduler, CriticalPathScheduler, WeightedCriticalPathScheduler,
    DynamicPriorityScheduler, MultiObjectiveScheduler,
    RealtimeScheduler, HybridCriticalityScheduler,
]

for _base in _HEURISTICS:
    _cls = make_batch_scheduler(_base)
    ALGORITHM_REGISTRY[_cls.algorithm_name] = _cls
