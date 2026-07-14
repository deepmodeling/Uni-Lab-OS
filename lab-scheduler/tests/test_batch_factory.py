import pytest

from scheduler.api.schemas import (
    Machine, Resources, ScheduleRequest, Step, Task,
)
from scheduler.core.batch_factory import make_batch_scheduler
from scheduler.core.step_algorithms import (
    ALGORITHM_REGISTRY, GreedyScheduler, WeightedCriticalPathScheduler,
)
from scheduler.service.scheduler_service import SchedulerService


def test_all_seven_batch_variants_registered():
    expected = {
        "Batch_Greedy", "Batch_CriticalPath", "Batch_WeightedCriticalPath",
        "Batch_DynamicPriority", "Batch_MultiObjective", "Batch_Realtime",
        "Batch_HybridCriticality",
    }
    assert expected.issubset(ALGORITHM_REGISTRY.keys())


def test_batch_scheduler_subclass_of_base():
    cls = make_batch_scheduler(GreedyScheduler)
    assert issubclass(cls, GreedyScheduler)


def test_batch_runs_three_steps_within_capacity():
    svc = SchedulerService()
    req = ScheduleRequest(
        lab_id="L",
        tasks=[
            Task(task_id=f"W{i}", priority=1.0, steps=[
                Step(step_id=f"W{i}_s1", machine_type="PCR", duration=10)
            ])
            for i in range(3)
        ],
        resources=Resources(machines=[
            Machine(type="PCR", count=1, batch_capacity=2),
        ]),
        algorithm="Batch_WeightedCriticalPath",
    )
    resp = svc.schedule(req)
    # 3 steps, capacity 2 → 2 batches → makespan = 20
    assert resp.objective.total_makespan == 20
