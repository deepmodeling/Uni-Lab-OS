"""Schedule result models for internal use."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ScheduledStep:
    """A scheduled step with timing and resource assignment."""

    step_id: str
    task_id: str
    start: int
    end: int
    device_instance: str


@dataclass
class ScheduledTransfer:
    """A scheduled sample transfer."""

    transfer_id: str
    samples: list[str]
    start: int
    end: int
    robot_id: str
    path: list[str]


@dataclass
class ScheduleObjective:
    """Optimization objective metrics."""

    task_completions: dict[str, int] = field(default_factory=dict)
    priority_weighted_cost: float = 0.0
    total_makespan: int = 0
