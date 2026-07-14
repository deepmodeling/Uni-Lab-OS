"""Tests for ExactDP solver."""

from __future__ import annotations

import pytest

from scheduler.core.device_pool import build_device_pool
from scheduler.core.step_algorithms import get_algorithm
from scheduler.models.dag import Edge, StepNode, TaskDAG


def _make_dag(
    task_id: str = "t1",
    priority: float = 1.0,
    steps: list[dict] | None = None,
    deps: list[tuple[str, str]] | None = None,
) -> TaskDAG:
    if steps is None:
        steps = [
            {"step_id": "s1", "machine_type": "A", "duration": 10},
            {"step_id": "s2", "machine_type": "B", "duration": 20},
        ]
    if deps is None:
        deps = [("s1", "s2")]

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


@pytest.fixture
def dp_cls():
    return get_algorithm("ExactDP")


class TestExactDPBasic:
    def test_single_step(self, dp_cls):
        pool = build_device_pool([{"type": "A", "count": 1}])
        dag = _make_dag(
            steps=[{"step_id": "s1", "machine_type": "A", "duration": 10}],
            deps=[],
        )
        scheduler = dp_cls(pool)
        scheduler.add_batch(dag, submit_time=0)
        results = scheduler.schedule()

        assert len(results) == 1
        assert results[0].start == 0
        assert results[0].end == 10

    def test_chain_precedence(self, dp_cls):
        """s1→s2: DP 必须先完成 s1 再开始 s2."""
        pool = build_device_pool([{"type": "A", "count": 1}])
        dag = _make_dag(
            steps=[
                {"step_id": "s1", "machine_type": "A", "duration": 10},
                {"step_id": "s2", "machine_type": "A", "duration": 20},
            ],
            deps=[("s1", "s2")],
        )
        scheduler = dp_cls(pool)
        scheduler.add_batch(dag, submit_time=0)
        results = scheduler.schedule()

        assert len(results) == 2
        by_step = {r.step_id: r for r in results}
        assert by_step["s1"].end == 10
        assert by_step["s2"].start >= 10
        assert by_step["s2"].end == 30

    def test_parallel_independent(self, dp_cls):
        """两个独立步骤, 2台设备 → 并行."""
        pool = build_device_pool([{"type": "A", "count": 2}])
        dag = _make_dag(
            steps=[
                {"step_id": "s1", "machine_type": "A", "duration": 10},
                {"step_id": "s2", "machine_type": "A", "duration": 10},
            ],
            deps=[],
        )
        scheduler = dp_cls(pool)
        scheduler.add_batch(dag, submit_time=0)
        results = scheduler.schedule()

        assert len(results) == 2
        # 两个都应该从 0 开始 (并行)
        assert all(r.start == 0 for r in results)


class TestExactDPOptimality:
    def test_priority_ordering(self, dp_cls):
        """单设备, 高优先级 task 应该先执行 (最小化加权完工时间)."""
        pool = build_device_pool([{"type": "A", "count": 1}])
        dag_high = _make_dag(
            task_id="high", priority=10.0,
            steps=[{"step_id": "h1", "machine_type": "A", "duration": 5}],
            deps=[],
        )
        dag_low = _make_dag(
            task_id="low", priority=1.0,
            steps=[{"step_id": "l1", "machine_type": "A", "duration": 5}],
            deps=[],
        )

        scheduler = dp_cls(pool)
        scheduler.add_batch(dag_high, submit_time=0)
        scheduler.add_batch(dag_low, submit_time=0)
        results = scheduler.schedule()

        by_step = {r.step_id: r for r in results}
        # 高优先级先完成
        assert by_step["h1"].end <= by_step["l1"].end

    def test_consistent_with_cpsat(self, dp_cls):
        """DP 和 CP-SAT 对简单 DAG 应产生相同的最优目标值."""
        cpsat_cls = get_algorithm("CP-SAT")
        pool_spec = [{"type": "A", "count": 1}, {"type": "B", "count": 1}]

        dag_spec = dict(
            task_id="t1", priority=2.0,
            steps=[
                {"step_id": "s1", "machine_type": "A", "duration": 10},
                {"step_id": "s2", "machine_type": "B", "duration": 20},
            ],
            deps=[("s1", "s2")],
        )

        # DP
        dp_pool = build_device_pool(pool_spec)
        dp_dag = _make_dag(**dag_spec)
        dp_sched = dp_cls(dp_pool)
        dp_sched.add_batch(dp_dag, submit_time=0)
        dp_results = dp_sched.schedule()

        # CP-SAT
        cp_pool = build_device_pool(pool_spec)
        cp_dag = _make_dag(**dag_spec)
        cp_sched = cpsat_cls(cp_pool)
        cp_sched.add_batch(cp_dag, submit_time=0)
        cp_results = cp_sched.schedule()

        # 两者的 makespan 应该相同
        dp_makespan = max(r.end for r in dp_results)
        cp_makespan = max(r.end for r in cp_results)
        assert dp_makespan == cp_makespan


class TestExactDPLimits:
    def test_rejects_over_20_steps(self, dp_cls):
        """N>20 应该抛出 ValueError."""
        pool = build_device_pool([{"type": "A", "count": 5}])
        steps = [
            {"step_id": f"s{i}", "machine_type": "A", "duration": 1}
            for i in range(21)
        ]
        dag = _make_dag(steps=steps, deps=[])

        scheduler = dp_cls(pool)
        scheduler.add_batch(dag, submit_time=0)
        with pytest.raises(ValueError, match="ExactDP only supports"):
            scheduler.schedule()

    def test_empty_schedule(self, dp_cls):
        """无步骤 → 空结果."""
        pool = build_device_pool([{"type": "A", "count": 1}])
        dag = _make_dag(steps=[], deps=[])
        scheduler = dp_cls(pool)
        scheduler.add_batch(dag, submit_time=0)
        results = scheduler.schedule()
        assert results == []


class TestExactDPMultiTask:
    def test_two_tasks(self, dp_cls):
        """两个 task 都被完整调度."""
        pool = build_device_pool([{"type": "A", "count": 2}])
        dag1 = _make_dag(
            task_id="t1", priority=2.0,
            steps=[
                {"step_id": "t1-s1", "machine_type": "A", "duration": 10},
                {"step_id": "t1-s2", "machine_type": "A", "duration": 5},
            ],
            deps=[("t1-s1", "t1-s2")],
        )
        dag2 = _make_dag(
            task_id="t2", priority=1.0,
            steps=[
                {"step_id": "t2-s1", "machine_type": "A", "duration": 8},
            ],
            deps=[],
        )

        scheduler = dp_cls(pool)
        scheduler.add_batch(dag1, submit_time=0)
        scheduler.add_batch(dag2, submit_time=0)
        results = scheduler.schedule()

        assert len(results) == 3
        step_ids = {r.step_id for r in results}
        assert step_ids == {"t1-s1", "t1-s2", "t2-s1"}

        # 验证依赖
        by_step = {r.step_id: r for r in results}
        assert by_step["t1-s2"].start >= by_step["t1-s1"].end
