"""Tests for CoupledSolver."""

from __future__ import annotations

from scheduler.api.schemas import (
    InFlightStep,
    Machine,
    Resources,
    Robot,
    Step,
    Task,
)
from scheduler.core.coupled_solver import CoupledSolver


def _make_resources(
    machines: list[dict] | None = None,
    robots: list[dict] | None = None,
) -> Resources:
    if machines is None:
        machines = [{"type": "A", "count": 2}, {"type": "B", "count": 1}]
    if robots is None:
        robots = [{"robot_id": "r1", "location": "dock", "capacity": 3}]
    return Resources(
        machines=[Machine(**m) for m in machines],
        robots=[Robot(**r) for r in robots],
    )


class TestCoupledSolverBasic:
    def test_simple_chain_schedule(self):
        """简单链 DAG, 无跨设备转运."""
        tasks = [
            Task(
                task_id="t1",
                priority=1.0,
                steps=[
                    Step(step_id="s1", machine_type="A", duration=10),
                    Step(step_id="s2", machine_type="A", duration=20),
                ],
                dependencies=[("s1", "s2")],
            ),
        ]
        resources = _make_resources(robots=[])

        solver = CoupledSolver()
        response = solver.solve(tasks=tasks, resources=resources)

        assert response.schedule_id
        assert len(response.schedule) == 2
        assert response.objective.total_makespan > 0

    def test_cross_device_with_transfer(self):
        """跨设备 + 样品 → 产生转运条目."""
        tasks = [
            Task(
                task_id="t1",
                priority=1.0,
                steps=[
                    Step(step_id="s1", machine_type="A", duration=10,
                         output_samples=["sample-1"]),
                    Step(step_id="s2", machine_type="B", duration=20,
                         input_samples=["sample-1"]),
                ],
                dependencies=[("s1", "s2")],
            ),
        ]
        resources = _make_resources()

        solver = CoupledSolver()
        response = solver.solve(tasks=tasks, resources=resources)

        # 应包含 step + transfer 条目
        step_entries = [e for e in response.schedule if hasattr(e, "step_id")]
        transfer_entries = [e for e in response.schedule if hasattr(e, "transfer_id")]

        assert len(step_entries) == 2
        assert len(transfer_entries) >= 1

    def test_response_structure(self):
        """验证响应包含所有必需字段."""
        tasks = [
            Task(
                task_id="t1",
                priority=2.0,
                steps=[
                    Step(step_id="s1", machine_type="A", duration=10),
                ],
            ),
        ]
        resources = _make_resources(robots=[])

        solver = CoupledSolver()
        response = solver.solve(tasks=tasks, resources=resources)

        assert response.schedule_id
        assert response.algorithm == "WeightedCriticalPath"
        assert len(response.schedule) >= 1
        assert response.objective.task_completions
        assert response.objective.priority_weighted_cost > 0
        assert response.objective.total_makespan > 0
        assert len(response.execution_order) >= 1


class TestCoupledSolverAlgorithmSelection:
    def test_custom_algorithm(self):
        """可以指定不同的算法."""
        tasks = [
            Task(
                task_id="t1",
                steps=[Step(step_id="s1", machine_type="A", duration=10)],
            ),
        ]
        resources = _make_resources(robots=[])

        solver = CoupledSolver()
        response = solver.solve(
            tasks=tasks, resources=resources, algorithm="Greedy",
        )

        assert response.algorithm == "Greedy"
        assert len(response.schedule) == 1

    def test_cpsat_algorithm(self):
        """使用 CP-SAT 算法."""
        tasks = [
            Task(
                task_id="t1",
                steps=[
                    Step(step_id="s1", machine_type="A", duration=10),
                    Step(step_id="s2", machine_type="A", duration=20),
                ],
                dependencies=[("s1", "s2")],
            ),
        ]
        resources = _make_resources(robots=[])

        solver = CoupledSolver()
        response = solver.solve(
            tasks=tasks, resources=resources, algorithm="CP-SAT",
        )

        assert response.algorithm == "CP-SAT"
        assert len(response.schedule) == 2


class TestCoupledSolverFixedSteps:
    def test_fixed_steps_respected(self):
        """in-flight 步骤锁定设备和时间 (使用 CP-SAT 保证严格时序)."""
        tasks = [
            Task(
                task_id="t1",
                steps=[
                    Step(step_id="s1", machine_type="A", duration=10),
                    Step(step_id="s2", machine_type="A", duration=20),
                ],
                dependencies=[("s1", "s2")],
            ),
        ]
        resources = _make_resources(robots=[])
        fixed = [
            InFlightStep(
                step_id="s1", task_id="t1",
                device="A_0", started_at=0, estimated_end=10,
            ),
        ]

        solver = CoupledSolver()
        response = solver.solve(
            tasks=tasks, resources=resources,
            algorithm="CP-SAT",
            fixed_steps=fixed,
        )

        # s2 应该在 s1.end=10 之后开始
        step_entries = [e for e in response.schedule if hasattr(e, "step_id")]
        s2_entries = [e for e in step_entries if e.step_id == "s2"]
        assert len(s2_entries) == 1
        assert s2_entries[0].start >= 10


class TestCoupledSolverMultiTask:
    def test_two_tasks_both_scheduled(self):
        """多个 task 都被调度."""
        tasks = [
            Task(
                task_id="t1", priority=2.0,
                steps=[
                    Step(step_id="t1-s1", machine_type="A", duration=10),
                    Step(step_id="t1-s2", machine_type="B", duration=20),
                ],
                dependencies=[("t1-s1", "t1-s2")],
            ),
            Task(
                task_id="t2", priority=1.0,
                steps=[
                    Step(step_id="t2-s1", machine_type="A", duration=15),
                ],
            ),
        ]
        resources = _make_resources(robots=[])

        solver = CoupledSolver()
        response = solver.solve(tasks=tasks, resources=resources)

        step_ids = {e.step_id for e in response.schedule if hasattr(e, "step_id")}
        assert step_ids == {"t1-s1", "t1-s2", "t2-s1"}

    def test_objective_includes_all_tasks(self):
        """目标值包含所有 task 的完工时间."""
        tasks = [
            Task(
                task_id="t1", priority=2.0,
                steps=[Step(step_id="s1", machine_type="A", duration=10)],
            ),
            Task(
                task_id="t2", priority=1.0,
                steps=[Step(step_id="s2", machine_type="A", duration=15)],
            ),
        ]
        resources = _make_resources(robots=[])

        solver = CoupledSolver()
        response = solver.solve(tasks=tasks, resources=resources)

        assert "t1" in response.objective.task_completions
        assert "t2" in response.objective.task_completions
