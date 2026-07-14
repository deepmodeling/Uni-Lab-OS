"""Pydantic request/response models for the scheduler API."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field, computed_field, field_validator


class Priority(str, Enum):
    urgent = "urgent"
    high = "high"
    normal = "normal"
    low = "low"


PRIORITY_WEIGHTS: dict[Priority, float] = {
    Priority.urgent: 300.0,
    Priority.high: 200.0,
    Priority.normal: 100.0,
    Priority.low: 50.0,
}


# ── 请求模型 ──────────────────────────────────────────────


class Step(BaseModel):
    step_id: str
    type: str = "experiment"  # experiment | py_script | tool_call | transfer
    machine_type: str
    duration: float = Field(gt=0, description="预计占用时长 (分钟)")
    input_samples: list[str] = []
    output_samples: list[str] = []


class TimeConstraint(BaseModel):
    """步骤间时间窗约束: min_gap ≤ start[to] - end[from] ≤ max_gap."""

    from_step: str
    to_step: str
    min_gap: int | None = None
    max_gap: int | None = None


class Task(BaseModel):
    task_id: str
    priority: Priority | float = Field(
        default=1.0, description="优先级权重 wᵢ 或 Priority 枚举"
    )
    submitted_at: datetime | None = None
    steps: list[Step]
    dependencies: list[tuple[str, str]] = Field(
        default_factory=list,
        description="步骤依赖 (前驱step_id, 后继step_id)",
    )
    time_constraints: list[TimeConstraint] = Field(
        default_factory=list,
        description="步骤间时间窗约束 (min_gap/max_gap)",
    )

    @field_validator("priority", mode="before")
    @classmethod
    def _normalize_priority(cls, v):
        # 字符串优先级先尝试解析为 Priority 枚举，失败时保持原值由后续校验报错
        if isinstance(v, str):
            try:
                return Priority(v)
            except ValueError:
                pass
        return v

    @computed_field
    @property
    def weight(self) -> float:
        # 将 Priority 枚举映射为权重，其余情况直接转 float
        if isinstance(self.priority, Priority):
            return PRIORITY_WEIGHTS[self.priority]
        return float(self.priority)


class Machine(BaseModel):
    type: str
    count: int = Field(gt=0)
    max_concurrent_samples: int = 1
    batch_capacity: int = Field(default=1, ge=1, description="批处理槽位; 1=非批处理")


class Robot(BaseModel):
    robot_id: str
    location: str
    capacity: int = Field(gt=0)
    available_from: int = 0


class Resources(BaseModel):
    machines: list[Machine]
    robots: list[Robot] = []


class ScheduleRequest(BaseModel):
    lab_id: str
    tasks: list[Task]
    resources: Resources
    algorithm: str = "WeightedCriticalPath"
    current_time: int = 0


class CompletedStep(BaseModel):
    step_id: str
    task_id: str
    status: str  # success | failed
    actual_end: int


class InFlightStep(BaseModel):
    step_id: str
    task_id: str
    device: str
    started_at: int
    estimated_end: int


class RobotState(BaseModel):
    robot_id: str
    location: str
    carrying_samples: list[str] = []
    available_from: int = 0


class RescheduleRequest(ScheduleRequest):
    schedule_id: str
    completed_steps: list[CompletedStep] = []
    in_flight_steps: list[InFlightStep] = []
    current_sample_locations: dict[str, str] = Field(
        default_factory=dict,
        description="sample_id → 当前所在设备",
    )
    robot_states: list[RobotState] = []


# ── 响应模型 ──────────────────────────────────────────────


class StepEntry(BaseModel):
    step_id: str
    task_id: str
    start: int
    end: int
    resource: str  # 分配的设备实例


class TransferEntry(BaseModel):
    transfer_id: str
    samples: list[str]
    start: int
    end: int
    robot_id: str
    path: list[str]


class ExecutionOrderEntry(BaseModel):
    priority: int
    step_id: str
    task_id: str
    device: str
    earliest_start: int


class ObjectiveResult(BaseModel):
    task_completions: dict[str, int]  # task_id → Cᵢ
    priority_weighted_cost: float  # ∑(wᵢ × Cᵢ)
    total_makespan: int


class DeviceLoadEntry(BaseModel):
    """按设备类型聚合的负载度量 (排队论 ρ)."""

    count: int
    busy_minutes: float  # 该类型全部实例总机时
    work_per_machine: float  # busy_minutes / count
    utilization: float  # 经验排队论 ρ = work/(makespan·count)
    load_ratio: float  # 归一到瓶颈的 ρ̂ ∈ [0,1], 瓶颈=1.0


class ScheduleResponse(BaseModel):
    schedule_id: str
    algorithm: str
    schedule: list[StepEntry | TransferEntry]
    objective: ObjectiveResult
    execution_order: list[ExecutionOrderEntry]
    device_utilization: dict[str, float] = {}  # instance_id → busy/makespan (向后兼容)
    device_load: dict[str, DeviceLoadEntry] = {}  # device_type → 负载度量
    bottleneck_type: str | None = None  # work/count 最大的设备类型
    makespan_lower_bound: float = 0.0  # maxⱼ(workⱼ/cⱼ)
    schedule_efficiency: float = 0.0  # LB / makespan = max(utilization)
