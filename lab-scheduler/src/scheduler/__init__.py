"""uni-lab-scheduler public API.

Direct library usage:

    from scheduler import (
        SchedulerService, ScheduleRequest, Task, Step,
        Machine, Robot, Resources, Priority,
    )

Or HTTP via uvicorn scheduler.main:app.
"""

import scheduler.core  # noqa: F401  (side-effect: registers all algorithms)

from scheduler.api.schemas import (
    Machine,
    ObjectiveResult,
    PRIORITY_WEIGHTS,
    Priority,
    RescheduleRequest,
    Resources,
    Robot,
    ScheduleRequest,
    ScheduleResponse,
    Step,
    StepEntry,
    Task,
    TimeConstraint,
    TransferEntry,
)
from scheduler.compiler import (
    CompiledContainer,
    CompiledStepMetadata,
    CompilerRequest,
    CompilerResponse,
    CompilerService,
    CompilerStep,
    ContainerTypeSpec,
    DurationModel,
    MaterialOutput,
    MaterialRef,
    MaterialSpec,
)
from scheduler.core.step_algorithms import get_algorithm, list_algorithms
from scheduler.service.scheduler_service import SchedulerService

__all__ = [
    # Schemas
    "Step", "Task", "Machine", "Robot", "Resources",
    "TimeConstraint", "Priority", "PRIORITY_WEIGHTS",
    "ScheduleRequest", "RescheduleRequest", "ScheduleResponse",
    "StepEntry", "TransferEntry", "ObjectiveResult",
    # Compiler
    "CompiledContainer", "CompiledStepMetadata",
    "CompilerRequest", "CompilerResponse", "CompilerService",
    "CompilerStep", "ContainerTypeSpec", "DurationModel",
    "MaterialOutput", "MaterialRef", "MaterialSpec",
    # Service
    "SchedulerService",
    # Algorithm discovery
    "list_algorithms", "get_algorithm",
]
