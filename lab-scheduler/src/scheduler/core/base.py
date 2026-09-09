"""事件驱动离散仿真调度基类.

从 mix_real.py 的 SchedulerBase 提取并适配:
- 使用 DevicePool 支持 instance-level 设备分配
- 支持 priority_weight (浮点) 优先级
- 返回结构化 ScheduledStep 列表而非 print
"""

from __future__ import annotations

import heapq
from abc import ABC, abstractmethod
from collections import defaultdict
from dataclasses import dataclass, field

from scheduler.core.metrics import LoadSummary, compute_device_load
from scheduler.models.dag import Edge, StepNode, TaskDAG
from scheduler.models.resources import DevicePool
from scheduler.models.schedule_result import ScheduledStep


@dataclass
class BatchInfo:
    """一个已提交的 DAG 批次的运行时信息."""

    task_dag: TaskDAG
    submit_time: int = 0
    priority: float = 0.0
    weight: float = 1.0


class SchedulerBase(ABC):
    """事件驱动离散仿真调度器基类.

    子类只需实现 ``_schedule_ready_tasks`` 方法来定义调度策略.
    """

    def __init__(self, device_pool: DevicePool) -> None:
        self.device_pool = device_pool
        self.event_queue: list[tuple] = []
        self.current_time: int = 0
        self.completed: list[ScheduledStep] = []
        self.batches: list[BatchInfo] = []
        # 设备占用跟踪 (用于利用率计算)
        self._device_busy_time: dict[str, int] = defaultdict(int)

    # ── 批次管理 ────────────────────────────────────────

    def add_batch(self, task_dag: TaskDAG, submit_time: int = 0) -> None:
        """注册一个 DAG 批次."""
        task_dag.build_adjacency()
        batch = BatchInfo(
            task_dag=task_dag,
            submit_time=submit_time,
            priority=task_dag.priority,
            weight=task_dag.weight if task_dag.weight != 1.0 else task_dag.priority,
        )
        self.batches.append(batch)
        self._init_ready_nodes(len(self.batches) - 1)

    def _init_ready_nodes(self, batch_idx: int) -> None:
        dag = self.batches[batch_idx].task_dag
        for node_id, node in dag.nodes.items():
            if not node.ready and not node.completed:
                parents = dag.parents.get(node_id, [])
                if all(dag.nodes[p].completed for p in parents):
                    node.ready = True

    # ── 设备管理 (委托给 DevicePool) ───────────────────

    def _get_device(self, machine_type: str) -> str | None:
        """获取一个可用设备实例 ID, 无可用时返回 None."""
        return self.device_pool.get_available(machine_type, self.current_time)

    def _filter_ready_by_device_lock(
        self,
        ready: list[tuple[int, str, StepNode]],
    ) -> list[tuple[int, str, StepNode]]:
        """Return ready nodes whose device lock can be acquired *now*.

        Device availability is a hard admission gate, not another priority
        score.  A node whose device type is currently busy is therefore
        excluded from this dispatch round before any priority/heuristic
        ordering is applied.  The node itself remains ``ready`` in its DAG;
        it is reconsidered after the next completion event releases a device.

        The check is intentionally non-mutating.  The actual lock acquisition
        still happens in ``_do_allocate`` and every algorithm re-checks the
        device immediately before allocation, which also handles two ready
        nodes competing for the last instance of a device type in one round.
        """
        return [
            item for item in ready
            if self._get_device(item[2].machine_type) is not None
        ]

    def _get_earliest_device(self, machine_type: str) -> tuple[str, int] | None:
        """获取最早可用的设备实例及其可用时间."""
        return self.device_pool.get_earliest(machine_type)

    def _allocate_device(self, instance_id: str, until: int) -> None:
        self.device_pool.allocate(instance_id, until)

    # ── 调度核心 ────────────────────────────────────────

    @abstractmethod
    def _schedule_ready_tasks(self) -> None:
        """子类实现: 从就绪任务中选择并分配设备."""

    def schedule(self) -> list[ScheduledStep]:
        """执行事件驱动仿真, 返回调度结果."""
        self._schedule_ready_tasks()

        event_counter = 0
        while True:
            if self.event_queue:
                time, _, (batch_idx, node_id) = heapq.heappop(self.event_queue)
                self.current_time = time

                dag = self.batches[batch_idx].task_dag
                node = dag.nodes[node_id]

                # 记录设备忙碌时间
                if node.start is not None and node.end is not None:
                    self._device_busy_time[node.assigned_device or ""] += (
                        node.end - node.start
                    )

                # 更新子节点就绪状态
                for child_id in dag.children.get(node_id, []):
                    child = dag.nodes[child_id]
                    if not child.completed:
                        parents = dag.parents.get(child_id, [])
                        if all(dag.nodes[p].completed for p in parents):
                            child.ready = True
                            self._schedule_ready_tasks()

                self._schedule_ready_tasks()
            else:
                # 查找下一个待提交批次
                future = [
                    b.submit_time
                    for b in self.batches
                    if b.submit_time > self.current_time
                ]
                if not future:
                    break
                self.current_time = min(future)
                self._schedule_ready_tasks()

            event_counter += 1
            if event_counter > 100_000:
                break  # 安全阀

        return self.completed

    def _do_allocate(
        self,
        batch_idx: int,
        node_id: str,
        node: StepNode,
        device_id: str,
    ) -> None:
        """通用分配逻辑: 分配设备、更新节点状态、推入事件."""
        start = self.current_time
        end = start + node.duration
        self._allocate_device(device_id, end)

        node.completed = True
        node.ready = False
        node.start = start
        node.end = end
        node.assigned_device = device_id

        heapq.heappush(
            self.event_queue, (end, id(node), (batch_idx, node_id))
        )
        self.completed.append(
            ScheduledStep(
                step_id=node.step_id,
                task_id=node.task_id,
                start=start,
                end=end,
                device_instance=device_id,
            )
        )

    # ── 工具方法 ────────────────────────────────────────

    def _collect_ready(self) -> list[tuple[int, str, StepNode]]:
        """收集所有批次中当前就绪且未完成的节点."""
        ready: list[tuple[int, str, StepNode]] = []
        for batch_idx, batch in enumerate(self.batches):
            if self.current_time >= batch.submit_time:
                self._init_ready_nodes(batch_idx)
                for node_id, node in batch.task_dag.nodes.items():
                    if node.ready and not node.completed:
                        ready.append((batch_idx, node_id, node))
        return ready

    def get_device_utilization(self) -> dict[str, float]:
        """计算设备利用率."""
        if self.current_time <= 0:
            return {}
        result: dict[str, float] = {}
        for did, busy in self._device_busy_time.items():
            result[did] = round(busy / self.current_time, 4)
        return result

    def get_device_load(self) -> LoadSummary:
        """计算按设备类型聚合的负载度量 (排队论 ρ).

        汇总每个 instance 的机时到其设备类型, 配合 DevicePool 台数与 makespan
        (= ``self.current_time``), 返回:
        - 经验排队论 ρ (utilization), 受暖机/排空瞬态影响;
        - 归一到瓶颈的 load_ratio (瞬态无关, 瓶颈=1.0);
        - 瓶颈类型、makespan 下界与调度效率.
        详见 ``scheduler.core.metrics``.
        """
        busy_by_type: dict[str, float] = defaultdict(float)
        for iid, busy in self._device_busy_time.items():
            inst = self.device_pool.instances.get(iid)
            dtype = inst.device_type if inst is not None else iid
            busy_by_type[dtype] += busy
        count_by_type = {
            dtype: len(iids)
            for dtype, iids in self.device_pool._type_index.items()
        }
        return compute_device_load(
            dict(busy_by_type), count_by_type, float(self.current_time)
        )
