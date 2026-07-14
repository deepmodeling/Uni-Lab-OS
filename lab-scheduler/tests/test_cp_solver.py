"""Tests for CP-SAT solver."""

from __future__ import annotations

import pytest

from scheduler.core.device_pool import build_device_pool
from scheduler.core.step_algorithms import get_algorithm
from scheduler.models.dag import Edge, StepNode, TaskDAG, TimeConstraint


def _make_dag(
    task_id: str = "t1",
    priority: float = 1.0,
    steps: list[dict] | None = None,
    deps: list[tuple[str, str]] | None = None,
    time_constraints: list[TimeConstraint] | None = None,
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
        task_id=task_id,
        priority=priority,
        weight=priority,  # 设置 weight 字段与 priority 相同
        nodes=nodes,
        edges=edges,
        time_constraints=time_constraints or [],
    )


@pytest.fixture
def cpsat_cls():
    return get_algorithm("CP-SAT")


class TestCPSATBasic:
    def test_single_step(self, cpsat_cls):
        pool = build_device_pool([{"type": "A", "count": 1}])
        dag = _make_dag(
            steps=[{"step_id": "s1", "machine_type": "A", "duration": 10}],
            deps=[],
        )
        scheduler = cpsat_cls(pool)
        scheduler.add_batch(dag, submit_time=0)
        results = scheduler.schedule()

        assert len(results) == 1
        assert results[0].step_id == "s1"
        assert results[0].start == 0
        assert results[0].end == 10
        assert results[0].device_instance == "A_0"

    def test_chain_dag_precedence(self, cpsat_cls):
        """s1→s2→s3, 验证 CP-SAT 满足依赖约束."""
        pool = build_device_pool([{"type": "A", "count": 2}, {"type": "B", "count": 1}])
        dag = _make_dag(
            steps=[
                {"step_id": "s1", "machine_type": "A", "duration": 10},
                {"step_id": "s2", "machine_type": "B", "duration": 20},
                {"step_id": "s3", "machine_type": "A", "duration": 15},
            ],
            deps=[("s1", "s2"), ("s2", "s3")],
        )
        scheduler = cpsat_cls(pool)
        scheduler.add_batch(dag, submit_time=0)
        results = scheduler.schedule()

        assert len(results) == 3
        by_step = {r.step_id: r for r in results}
        assert by_step["s2"].start >= by_step["s1"].end
        assert by_step["s3"].start >= by_step["s2"].end

    def test_no_overlap_on_single_device(self, cpsat_cls):
        """单台设备, 两个独立步骤 → 不能重叠."""
        pool = build_device_pool([{"type": "A", "count": 1}])
        dag = _make_dag(
            steps=[
                {"step_id": "s1", "machine_type": "A", "duration": 10},
                {"step_id": "s2", "machine_type": "A", "duration": 10},
            ],
            deps=[],
        )
        scheduler = cpsat_cls(pool)
        scheduler.add_batch(dag, submit_time=0)
        results = scheduler.schedule()

        assert len(results) == 2
        by_step = {r.step_id: r for r in results}
        s1, s2 = by_step["s1"], by_step["s2"]
        assert s1.end <= s2.start or s2.end <= s1.start


class TestCPSATTimeConstraints:
    def test_min_gap_enforced(self, cpsat_cls):
        """min_gap=5: s2 最早在 s1.end + 5 开始."""
        pool = build_device_pool([{"type": "A", "count": 2}])
        dag = _make_dag(
            steps=[
                {"step_id": "s1", "machine_type": "A", "duration": 10},
                {"step_id": "s2", "machine_type": "A", "duration": 10},
            ],
            deps=[("s1", "s2")],
            time_constraints=[
                TimeConstraint(from_step="s1", to_step="s2", min_gap=5),
            ],
        )
        scheduler = cpsat_cls(pool)
        scheduler.add_batch(dag, submit_time=0)
        results = scheduler.schedule()

        by_step = {r.step_id: r for r in results}
        gap = by_step["s2"].start - by_step["s1"].end
        assert gap >= 5

    def test_max_gap_enforced(self, cpsat_cls):
        """max_gap=3: s2 必须在 s1.end + 3 之前开始."""
        pool = build_device_pool([{"type": "A", "count": 2}])
        dag = _make_dag(
            steps=[
                {"step_id": "s1", "machine_type": "A", "duration": 10},
                {"step_id": "s2", "machine_type": "A", "duration": 10},
            ],
            deps=[("s1", "s2")],
            time_constraints=[
                TimeConstraint(from_step="s1", to_step="s2", max_gap=3),
            ],
        )
        scheduler = cpsat_cls(pool)
        scheduler.add_batch(dag, submit_time=0)
        results = scheduler.schedule()

        by_step = {r.step_id: r for r in results}
        gap = by_step["s2"].start - by_step["s1"].end
        assert gap <= 3

    def test_min_and_max_gap(self, cpsat_cls):
        """min_gap=2, max_gap=5 同时生效."""
        pool = build_device_pool([{"type": "A", "count": 2}])
        dag = _make_dag(
            steps=[
                {"step_id": "s1", "machine_type": "A", "duration": 10},
                {"step_id": "s2", "machine_type": "A", "duration": 10},
            ],
            deps=[("s1", "s2")],
            time_constraints=[
                TimeConstraint(from_step="s1", to_step="s2", min_gap=2, max_gap=5),
            ],
        )
        scheduler = cpsat_cls(pool)
        scheduler.add_batch(dag, submit_time=0)
        results = scheduler.schedule()

        by_step = {r.step_id: r for r in results}
        gap = by_step["s2"].start - by_step["s1"].end
        assert 2 <= gap <= 5


class TestCPSATAlternativeRouting:
    def test_parallel_on_multiple_devices(self, cpsat_cls):
        """两个独立步骤, 2台 A 设备 → 并行执行."""
        pool = build_device_pool([{"type": "A", "count": 2}])
        dag = _make_dag(
            steps=[
                {"step_id": "s1", "machine_type": "A", "duration": 10},
                {"step_id": "s2", "machine_type": "A", "duration": 10},
            ],
            deps=[],
        )
        scheduler = cpsat_cls(pool)
        scheduler.add_batch(dag, submit_time=0)
        results = scheduler.schedule()

        assert len(results) == 2
        # 应该分配到不同设备
        devices = {r.device_instance for r in results}
        assert len(devices) == 2
        # 都从 time=0 开始
        assert all(r.start == 0 for r in results)


class TestCPSATObjective:
    def test_weighted_completion_minimized(self, cpsat_cls):
        """高优先级 task 应该先完成."""
        pool = build_device_pool([{"type": "A", "count": 1}])
        dag_high = _make_dag(
            task_id="high",
            priority=10.0,
            steps=[{"step_id": "h1", "machine_type": "A", "duration": 10}],
            deps=[],
        )
        dag_low = _make_dag(
            task_id="low",
            priority=1.0,
            steps=[{"step_id": "l1", "machine_type": "A", "duration": 10}],
            deps=[],
        )

        scheduler = cpsat_cls(pool)
        scheduler.add_batch(dag_high, submit_time=0)
        scheduler.add_batch(dag_low, submit_time=0)
        results = scheduler.schedule()

        by_step = {r.step_id: r for r in results}
        # 高优先级应该先执行
        assert by_step["h1"].end <= by_step["l1"].end

    def test_empty_schedule(self, cpsat_cls):
        """无步骤 → 空结果."""
        pool = build_device_pool([{"type": "A", "count": 1}])
        dag = _make_dag(steps=[], deps=[])
        scheduler = cpsat_cls(pool)
        scheduler.add_batch(dag, submit_time=0)
        results = scheduler.schedule()
        assert results == []


class TestCPSATMultiTask:
    def test_two_tasks_all_scheduled(self, cpsat_cls):
        """两个 task 都被完整调度."""
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
                {"step_id": "t2-s2", "machine_type": "A", "duration": 10},
            ],
            deps=[("t2-s1", "t2-s2")],
        )

        scheduler = cpsat_cls(pool)
        scheduler.add_batch(dag1, submit_time=0)
        scheduler.add_batch(dag2, submit_time=0)
        results = scheduler.schedule()

        assert len(results) == 4
        step_ids = {r.step_id for r in results}
        assert step_ids == {"t1-s1", "t1-s2", "t2-s1", "t2-s2"}


class TestCPSATBatchCapacity:
    """批处理容量约束测试 (Phase 2 regression tests)."""

    def test_cp_sat_three_steps_batch_capacity_2_uses_two_batches(self, cpsat_cls):
        """3 个 PCR 步骤, 1 台 PCR 设备 batch_capacity=2 → 需要 2 批次, makespan=20."""
        from scheduler.models.resources import DevicePool

        pool = DevicePool()
        pool.add_device_type("PCR", count=1, batch_capacity=2)

        dag = _make_dag(
            task_id="t1",
            priority=1.0,
            steps=[
                {"step_id": "s1", "machine_type": "PCR", "duration": 10},
                {"step_id": "s2", "machine_type": "PCR", "duration": 10},
                {"step_id": "s3", "machine_type": "PCR", "duration": 10},
            ],
            deps=[],
        )

        scheduler = cpsat_cls(pool, time_limit_s=10.0)
        scheduler.add_batch(dag, submit_time=0)
        results = scheduler.schedule()

        assert len(results) == 3
        makespan = max(r.end for r in results)
        # 3 个步骤, 容量 2 → 第一批 2 个 (0-10), 第二批 1 个 (10-20)
        assert makespan == 20, f"Expected makespan=20 with batch_capacity=2, got {makespan}"

    def test_cp_sat_capacity_1_degrades_to_noverlap(self, cpsat_cls):
        """batch_capacity=1 必须退化为 NoOverlap 行为, 与 Phase 1 结果一致."""
        from scheduler.models.resources import DevicePool

        pool = DevicePool()
        pool.add_device_type("A", count=1, batch_capacity=1)

        dag = _make_dag(
            task_id="t1",
            priority=1.0,
            steps=[
                {"step_id": "s1", "machine_type": "A", "duration": 3},
                {"step_id": "s2", "machine_type": "A", "duration": 5},
            ],
            deps=[],
        )

        scheduler = cpsat_cls(pool, time_limit_s=10.0)
        scheduler.add_batch(dag, submit_time=0)
        results = scheduler.schedule()

        assert len(results) == 2
        makespan = max(r.end for r in results)
        # capacity=1 → 串行执行, makespan = 3 + 5 = 8
        assert makespan == 8, f"Expected makespan=8 with capacity=1, got {makespan}"

        # 验证无重叠
        by_step = {r.step_id: r for r in results}
        s1, s2 = by_step["s1"], by_step["s2"]
        assert s1.end <= s2.start or s2.end <= s1.start, "Steps must not overlap with capacity=1"
