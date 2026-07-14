import pytest

from scheduler.api.schemas import (
    Machine, Resources, ScheduleRequest, Step, Task,
)
from scheduler.core.ga_solver import (
    GeneticAlgorithmScheduler, decode_permutation,
)
from scheduler.core.step_algorithms import ALGORITHM_REGISTRY
from scheduler.service.scheduler_service import SchedulerService


def test_ga_registered():
    assert "GA" in ALGORITHM_REGISTRY


def test_ga_solves_small_chain():
    """Two chained steps on a single machine — GA must find the only schedule."""
    svc = SchedulerService()
    req = ScheduleRequest(
        lab_id="L",
        tasks=[Task(task_id="W", priority=1.0, steps=[
            Step(step_id="s1", machine_type="X", duration=3),
            Step(step_id="s2", machine_type="X", duration=2),
        ], dependencies=[("s1", "s2")])],
        resources=Resources(machines=[Machine(type="X", count=1)]),
        algorithm="GA",
    )
    resp = svc.schedule(req)
    assert resp.objective.total_makespan == 5


def test_ga_respects_batch_capacity():
    """3 PCR steps + batch_capacity=2 → makespan = 20."""
    svc = SchedulerService()
    req = ScheduleRequest(
        lab_id="L",
        tasks=[Task(task_id=f"W{i}", priority=1.0, steps=[
            Step(step_id=f"W{i}_s1", machine_type="PCR", duration=10)
        ]) for i in range(3)],
        resources=Resources(machines=[
            Machine(type="PCR", count=1, batch_capacity=2)
        ]),
        algorithm="GA",
    )
    resp = svc.schedule(req)
    assert resp.objective.total_makespan == 20
