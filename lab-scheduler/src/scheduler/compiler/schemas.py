"""Pydantic schemas for compiling user-authored material steps into execution DAGs."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from scheduler.api.schemas import (
    Priority,
    Resources,
    ScheduleRequest,
    TimeConstraint,
)

DispatchStrategy = Literal[
    "auto",
    "one",
    "per_input_container",
    "per_output_container",
]


class ContainerTypeSpec(BaseModel):
    """容器类型定义: capacity 表示单个容器最多承载的物料单位数."""

    type: str
    capacity: int = Field(gt=0)


class MaterialSpec(BaseModel):
    """初始物料定义."""

    material_id: str
    container_type: str
    amount: int = Field(gt=0)
    count: int | None = Field(default=None, gt=0)
    container_id: str | None = None


class MaterialRef(BaseModel):
    """步骤输入物料引用."""

    material_id: str
    container_type: str | None = None
    amount: int | None = Field(default=None, gt=0)


class MaterialOutput(BaseModel):
    """步骤输出物料定义."""

    material_id: str
    container_type: str
    amount: int = Field(gt=0)
    count: int | None = Field(default=None, gt=0)


class DurationModel(BaseModel):
    """设备下发单元的时长估算模型."""

    fixed: float | None = Field(default=None, gt=0)
    setup: float = Field(default=0, ge=0)
    per_item: float = Field(default=1.0, ge=0)
    items_per_cycle: int = Field(default=1, gt=0)
    teardown: float = Field(default=0, ge=0)


class CompilerStep(BaseModel):
    """用户编写的物料处理步骤.

    编译后每个生成 node 都必须对应一次真实设备下发.
    """

    step_id: str
    machine_type: str
    type: str = "experiment"
    inputs: list[MaterialRef] = Field(default_factory=list)
    outputs: list[MaterialOutput] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    dispatch_strategy: DispatchStrategy = "auto"
    duration_model: DurationModel = Field(default_factory=DurationModel)


class CompiledContainer(BaseModel):
    """编译后的具体容器实例."""

    container_id: str
    material_id: str
    container_type: str
    amount: int
    producer_node_id: str | None = None


class CompiledStepMetadata(BaseModel):
    """真实 DAG node 的生成溯源."""

    node_id: str
    source_step_id: str
    dispatch_index: int
    dispatch_count: int
    dispatch_strategy: DispatchStrategy
    machine_type: str
    duration: float
    input_containers: list[str] = Field(default_factory=list)
    output_containers: list[str] = Field(default_factory=list)


class CompilerRequest(BaseModel):
    """Compiler service request.

    输出的 schedule_request 是冻结后的真实 DAG, 可直接提交 scheduler.
    """

    lab_id: str
    task_id: str = "compiled-task"
    priority: Priority | float = 1.0
    algorithm: str = "WeightedCriticalPath"
    current_time: int = 0
    container_types: list[ContainerTypeSpec] = Field(default_factory=list)
    initial_materials: list[MaterialSpec] = Field(default_factory=list)
    steps: list[CompilerStep]
    resources: Resources
    time_constraints: list[TimeConstraint] = Field(default_factory=list)


class CompilerResponse(BaseModel):
    """Compiler service response."""

    schedule_request: ScheduleRequest
    metadata: list[CompiledStepMetadata]
    containers: list[CompiledContainer]
    warnings: list[str] = Field(default_factory=list)
