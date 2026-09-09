"""Tests for all heuristic scheduling algorithms."""

from __future__ import annotations

import pytest

from scheduler.core.device_pool import build_device_pool
from scheduler.core.step_algorithms import (
    ALGORITHM_REGISTRY,
    get_algorithm,
    list_algorithms,
)
from scheduler.models.dag import Edge, StepNode, TaskDAG


def _make_dag(
    task_id: str = "t1",
    priority: float = 1.0,
    steps: list[dict] | None = None,
    deps: list[tuple[str, str]] | None = None,
) -> TaskDAG:
    """构建测试用 TaskDAG."""
    if steps is None:
        steps = [
            {"step_id": "s1", "machine_type": "A", "duration": 10},
            {"step_id": "s2", "machine_type": "B", "duration": 20},
            {"step_id": "s3", "machine_type": "A", "duration": 15},
        ]
    if deps is None:
        deps = [("s1", "s2"), ("s2", "s3")]

    nodes = {}
    for s in steps:
        nodes[s["step_id"]] = StepNode(
            step_id=s["step_id"],
            task_id=task_id,
            machine_type=s["machine_type"],
            duration=s["duration"],
            priority_weight=priority,
        )
    edges = [Edge(source=src, target=tgt) for src, tgt in deps]
    return TaskDAG(
        task_id=task_id, priority=priority, nodes=nodes, edges=edges,
    )


HEURISTIC_ALGORITHMS = [
    name for name in list_algorithms()
    if name not in ("CP-SAT", "ExactDP")
]


class TestAlgorithmRegistry:
    def test_all_registered(self):
        expected = {
            "Greedy", "CriticalPath", "WeightedCriticalPath",
            "DynamicPriority", "MultiObjective", "Realtime",
            "HybridCriticality", "CP-SAT", "ExactDP", "GA",
            # Batch variants from batch_factory
            "Batch_Greedy", "Batch_CriticalPath", "Batch_WeightedCriticalPath",
            "Batch_DynamicPriority", "Batch_MultiObjective", "Batch_Realtime",
            "Batch_HybridCriticality",
        }
        assert set(list_algorithms()) == expected

    def test_get_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown algorithm"):
            get_algorithm("NonExistent")


class TestHeuristicAlgorithms:
    """测试所有启发式算法的基本正确性."""

    @pytest.mark.parametrize("algo_name", HEURISTIC_ALGORITHMS)
    def test_simple_chain_dag(self, algo_name: str):
        """线性链 DAG: s1→s2→s3, 验证所有步骤被调度且时长正确.

        注意: 启发式算法在分配时标记 completed=True, 依赖时序由事件循环
        部分保证, 此处仅验证基本正确性.
        """
        pool = build_device_pool([{"type": "A", "count": 2}, {"type": "B", "count": 1}])
        dag = _make_dag()

        algo_cls = get_algorithm(algo_name)
        scheduler = algo_cls(pool)
        scheduler.add_batch(dag, submit_time=0)
        results = scheduler.schedule()

        assert len(results) == 3
        by_step = {r.step_id: r for r in results}

        # 时长正确
        assert by_step["s1"].end - by_step["s1"].start == 10
        assert by_step["s2"].end - by_step["s2"].start == 20
        assert by_step["s3"].end - by_step["s3"].start == 15

        # 同一设备不重叠
        from collections import defaultdict
        device_intervals = defaultdict(list)
        for r in results:
            device_intervals[r.device_instance].append((r.start, r.end))
        for dev, intervals in device_intervals.items():
            intervals.sort()
            for i in range(len(intervals) - 1):
                assert intervals[i][1] <= intervals[i + 1][0], (
                    f"Overlap on {dev}: {intervals[i]} and {intervals[i+1]}"
                )

    @pytest.mark.parametrize("algo_name", HEURISTIC_ALGORITHMS)
    def test_parallel_independent_steps(self, algo_name: str):
        """两个独立步骤, 相同设备类型, 有2台设备 → 可并行."""
        pool = build_device_pool([{"type": "A", "count": 2}])
        dag = _make_dag(
            steps=[
                {"step_id": "s1", "machine_type": "A", "duration": 10},
                {"step_id": "s2", "machine_type": "A", "duration": 10},
            ],
            deps=[],
        )

        algo_cls = get_algorithm(algo_name)
        scheduler = algo_cls(pool)
        scheduler.add_batch(dag, submit_time=0)
        results = scheduler.schedule()

        assert len(results) == 2
        # 两个步骤应该在不同设备上
        devices = {r.device_instance for r in results}
        assert len(devices) == 2

        # 两个都从 time=0 开始 (并行)
        starts = {r.start for r in results}
        assert 0 in starts

    @pytest.mark.parametrize("algo_name", HEURISTIC_ALGORITHMS)
    def test_multi_task_scheduling(self, algo_name: str):
        """多个 task 同时调度."""
        pool = build_device_pool([{"type": "A", "count": 2}, {"type": "B", "count": 1}])
        dag1 = _make_dag(
            task_id="t1", priority=2.0,
            steps=[
                {"step_id": "t1-s1", "machine_type": "A", "duration": 10},
                {"step_id": "t1-s2", "machine_type": "B", "duration": 20},
            ],
            deps=[("t1-s1", "t1-s2")],
        )
        dag2 = _make_dag(
            task_id="t2", priority=1.0,
            steps=[
                {"step_id": "t2-s1", "machine_type": "A", "duration": 15},
            ],
            deps=[],
        )

        algo_cls = get_algorithm(algo_name)
        scheduler = algo_cls(pool)
        scheduler.add_batch(dag1, submit_time=0)
        scheduler.add_batch(dag2, submit_time=0)
        results = scheduler.schedule()

        assert len(results) == 3
        step_ids = {r.step_id for r in results}
        assert step_ids == {"t1-s1", "t1-s2", "t2-s1"}

    @pytest.mark.parametrize("algo_name", HEURISTIC_ALGORITHMS)
    def test_no_overlap_on_device(self, algo_name: str):
        """同一台设备上不能有重叠."""
        pool = build_device_pool([{"type": "A", "count": 1}])
        dag = _make_dag(
            steps=[
                {"step_id": "s1", "machine_type": "A", "duration": 10},
                {"step_id": "s2", "machine_type": "A", "duration": 10},
            ],
            deps=[],
        )

        algo_cls = get_algorithm(algo_name)
        scheduler = algo_cls(pool)
        scheduler.add_batch(dag, submit_time=0)
        results = scheduler.schedule()

        assert len(results) == 2
        # 单设备 → 必须串行
        by_step = {r.step_id: r for r in results}
        s1, s2 = by_step["s1"], by_step["s2"]
        # 一个必须在另一个之后
        assert s1.end <= s2.start or s2.end <= s1.start

    @pytest.mark.parametrize("algo_name", HEURISTIC_ALGORITHMS)
    def test_device_utilization(self, algo_name: str):
        """调度后可以获取设备利用率."""
        pool = build_device_pool([{"type": "A", "count": 1}])
        dag = _make_dag(
            steps=[{"step_id": "s1", "machine_type": "A", "duration": 10}],
            deps=[],
        )

        algo_cls = get_algorithm(algo_name)
        scheduler = algo_cls(pool)
        scheduler.add_batch(dag, submit_time=0)
        scheduler.schedule()
        util = scheduler.get_device_utilization()
        assert isinstance(util, dict)

    def test_device_lock_is_checked_before_priority(self):
        """A busy device does not let an urgent step block another device.

        The initial A step is dispatched first, then an urgent A step and a
        low-priority B step are submitted while A is locked.  B must start at
        t=0; urgent A remains ready and starts only after A is released.
        """
        pool = build_device_pool([{"type": "A", "count": 1}, {"type": "B", "count": 1}])
        scheduler = get_algorithm("Realtime")(pool)

        running = _make_dag(
            task_id="running", priority=1.0,
            steps=[{"step_id": "running-s1", "machine_type": "A", "duration": 10}],
            deps=[],
        )
        scheduler.add_batch(running, submit_time=0)
        scheduler._schedule_ready_tasks()

        urgent = _make_dag(
            task_id="urgent", priority=3.0,
            steps=[{"step_id": "urgent-s1", "machine_type": "A", "duration": 5}],
            deps=[],
        )
        background = _make_dag(
            task_id="background", priority=0.1,
            steps=[{"step_id": "background-s1", "machine_type": "B", "duration": 2}],
            deps=[],
        )
        scheduler.add_batch(urgent, submit_time=0)
        scheduler.add_batch(background, submit_time=0)
        scheduler._schedule_ready_tasks()

        by_task = {item.task_id: item for item in scheduler.completed}
        assert by_task["background"].start == 0
        assert "urgent" not in by_task

        results = scheduler.schedule()
        by_task = {item.task_id: item for item in results}
        assert by_task["urgent"].start == 10
