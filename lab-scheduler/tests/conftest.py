"""Shared test fixtures."""

from __future__ import annotations

import pytest

from scheduler.api.schemas import (
    Machine,
    Resources,
    ScheduleRequest,
    Step,
    Task,
)


@pytest.fixture
def simple_resources() -> Resources:
    return Resources(
        machines=[
            Machine(type="A", count=2),
            Machine(type="B", count=1),
        ]
    )


@pytest.fixture
def simple_task() -> Task:
    return Task(
        task_id="task-1",
        priority=1.0,
        steps=[
            Step(step_id="s1", machine_type="A", duration=10),
            Step(step_id="s2", machine_type="B", duration=20),
            Step(step_id="s3", machine_type="A", duration=15),
        ],
        dependencies=[("s1", "s2"), ("s2", "s3")],
    )


@pytest.fixture
def two_tasks() -> list[Task]:
    return [
        Task(
            task_id="task-1",
            priority=2.0,
            steps=[
                Step(step_id="t1-s1", machine_type="A", duration=10),
                Step(step_id="t1-s2", machine_type="B", duration=20),
            ],
            dependencies=[("t1-s1", "t1-s2")],
        ),
        Task(
            task_id="task-2",
            priority=1.0,
            steps=[
                Step(step_id="t2-s1", machine_type="A", duration=15),
                Step(step_id="t2-s2", machine_type="A", duration=10),
            ],
            dependencies=[("t2-s1", "t2-s2")],
        ),
    ]


@pytest.fixture
def simple_schedule_request(simple_task, simple_resources) -> ScheduleRequest:
    return ScheduleRequest(
        lab_id="lab-test",
        tasks=[simple_task],
        resources=simple_resources,
    )


@pytest.fixture
def multi_task_request(two_tasks, simple_resources) -> ScheduleRequest:
    return ScheduleRequest(
        lab_id="lab-test",
        tasks=two_tasks,
        resources=simple_resources,
    )
