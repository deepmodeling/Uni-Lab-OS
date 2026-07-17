"""Task 编排服务的数据契约。"""

from __future__ import annotations

from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

POLICY_RESOURCE_PREFIX = "resource:"
POLICY_WORKSTATION_PREFIX = "workstation:"
POLICY_RESERVED_RESOURCE_PREFIXES = (
    POLICY_RESOURCE_PREFIX,
    POLICY_WORKSTATION_PREFIX,
)


class StrictModel(BaseModel):
    """拒绝契约之外字段的持久化与写入 DTO 基类。"""

    model_config = ConfigDict(extra="forbid")


class Trigger(StrictModel):
    """Task 模板触发器的声明，具体调度语义由后续服务实现。"""

    kind: str
    config: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def reject_legacy_opc_condition(self) -> Trigger:
        """拒绝无法由条件提供者判定的旧前端 DTO。"""
        if self.kind == "opc_condition":
            raise ValueError(
                "legacy opc_condition is unsupported; use kind='opc' with provider_id and variable"
            )
        return self


class Template(StrictModel):
    """由 workflow 节点集合派生的可复用 Task 模板。"""

    id: str
    name: str
    workflow_path: str = ""
    node_ids: list[str] = Field(default_factory=list)
    resources: list[str] = Field(default_factory=list)
    trigger: Trigger | None = None
    input_triggers: list[Trigger] = Field(default_factory=list)
    output_triggers: list[Trigger] = Field(default_factory=list)

    @model_validator(mode="after")
    def reject_policy_resource_namespaces(self) -> Template:
        """普通业务资源不能伪造策略内部占用键。"""
        if any(
            resource.startswith(POLICY_RESERVED_RESOURCE_PREFIXES)
            for resource in self.resources
        ):
            raise ValueError("reserved policy resource prefix is not allowed")
        return self


class TaskInstance(StrictModel):
    """Task 模板的一次实例化记录。"""

    id: str
    template_id: str
    status: Literal[
        "waiting", "pending", "running", "completed", "failed", "cancelled"
    ] = "waiting"
    sample_id: str = ""
    order: int = Field(default=0, ge=0)
    payload: dict[str, Any] = Field(default_factory=dict)
    started_at: int | None = Field(default=None, ge=0)
    finished_at: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_lifecycle_timestamps(self) -> TaskInstance:
        """保证实例状态与真实起止时间的一致性。"""
        if self.finished_at is not None:
            if self.started_at is None:
                raise ValueError("finished_at requires started_at")
            if self.finished_at < self.started_at:
                raise ValueError("finished_at must be greater than or equal to started_at")
        if self.status in {"waiting", "pending"}:
            if self.started_at is not None or self.finished_at is not None:
                raise ValueError(f"{self.status} instances must not have timestamps")
        elif self.status == "running":
            if self.started_at is None or self.finished_at is not None:
                raise ValueError("running instances require started_at and no finished_at")
        elif self.status == "completed":
            if self.started_at is None or self.finished_at is None:
                raise ValueError("completed instances require started_at and finished_at")
        return self


class TaskScheduleEntry(StrictModel):
    """单个 Task 的可持久化甘特排程条目。"""

    instance_id: str
    template_id: str
    sample_id: str
    start_at: int = Field(ge=0)
    end_at: int = Field(ge=0)
    resources: list[str] = Field(default_factory=list)
    state: Literal["planned", "running", "done"]

    @model_validator(mode="after")
    def validate_time_range(self) -> TaskScheduleEntry:
        """甘特条目的结束时间不得早于开始时间。"""
        if self.end_at < self.start_at:
            raise ValueError("end_at must be greater than or equal to start_at")
        return self


class OpcSnapshotState(StrictModel):
    """持久化的受限 OPC 快照，仅用于条件判定。"""

    provider_id: str = Field(min_length=1, max_length=128)
    sequence: int = Field(ge=0)
    values: dict[str, Any] = Field(default_factory=dict, max_length=64)
    updated_at_by_variable: dict[str, float] = Field(default_factory=dict, max_length=64)


class WorkspaceEvent(StrictModel):
    """持久化的调度输入、输出和状态变更事件。"""

    kind: Literal[
        "opc_snapshot", "output", "scheduled", "completed", "template_deleted",
        "scheduled_templates_updated",
    ]
    id: str = Field(default_factory=lambda: uuid4().hex)
    timestamp: int = Field(default=0, ge=0)
    idempotency_key: str = ""
    instance_id: str | None = None
    template_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class Workspace(StrictModel):
    """单个 workflow 的 Task 编排工作区。"""

    workflow_path: str
    templates: list[Template] = Field(default_factory=list)
    task_instances: list[TaskInstance] = Field(default_factory=list)
    events: list[WorkspaceEvent] = Field(default_factory=list)
    scheduled_template_ids: list[str] = Field(default_factory=list)
    scheduler_paused: bool = False
    schedule_entries: list[TaskScheduleEntry] = Field(default_factory=list)
    opc_snapshots: list[OpcSnapshotState] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_unique_ids(self) -> Workspace:
        """禁止模板或实例列表中出现重复 ID。"""
        template_ids = [template.id for template in self.templates]
        if len(template_ids) != len(set(template_ids)):
            raise ValueError("template ids must be unique")
        instance_ids = [instance.id for instance in self.task_instances]
        if len(instance_ids) != len(set(instance_ids)):
            raise ValueError("task instance ids must be unique")
        template_id_set = set(template_ids)
        if any(instance.template_id not in template_id_set for instance in self.task_instances):
            raise ValueError("task instances must reference an existing template")
        sample_orders = [(item.sample_id, item.order) for item in self.task_instances]
        if len(sample_orders) != len(set(sample_orders)):
            raise ValueError("instance orders must be unique within each sample")
        if any(item not in template_id_set for item in self.scheduled_template_ids):
            raise ValueError("scheduled template ids must exist")
        instance_id_set = set(instance_ids)
        instance_by_id = {item.id: item for item in self.task_instances}
        for entry in self.schedule_entries:
            if entry.instance_id not in instance_id_set or entry.template_id not in template_id_set:
                raise ValueError("schedule entries must reference existing instances and templates")
            if instance_by_id[entry.instance_id].template_id != entry.template_id:
                raise ValueError("schedule entry template must match its instance template")
        for event in self.events:
            if event.instance_id is not None:
                instance = instance_by_id.get(event.instance_id)
                if instance is None:
                    raise ValueError("event instances must exist")
                if event.template_id is not None and event.template_id != instance.template_id:
                    raise ValueError("event template must match its instance template")
            elif event.template_id is not None and event.template_id not in template_id_set:
                raise ValueError("event templates must exist")
        return self


class VersionedWorkspaceResponse(StrictModel):
    """带乐观锁版本号的工作区响应。"""

    version: int
    workspace: Workspace


class WorkflowPathQuery(StrictModel):
    """按 workflow 路径读取工作区的查询 DTO。"""

    workflow_path: str


class WorkspaceUpdateRequest(StrictModel):
    """保存工作区的乐观锁请求。"""

    expected_version: int = Field(ge=0)
    workspace: Workspace


class OpcSnapshotRequest(StrictModel):
    """OPC 快照接口预留 DTO，暂不连接 provider。"""

    workflow_path: str
    provider_id: str
    variables: list[str] = Field(default_factory=list)


class TemplateCreateRequest(StrictModel):
    workflow_path: str
    expected_version: int = Field(ge=0)
    template: Template


class TemplateUpdateRequest(StrictModel):
    workflow_path: str
    expected_version: int = Field(ge=0)
    name: str | None = None
    trigger: Trigger | None = None
    input_triggers: list[Trigger] | None = None
    output_triggers: list[Trigger] | None = None


class GenerateInstancesRequest(StrictModel):
    workflow_path: str
    expected_version: int = Field(ge=0)
    template_ids: list[str] = Field(min_length=1)
    sample_ids: list[str] = Field(min_length=1)


class ScheduledTemplatesUpdateRequest(StrictModel):
    workflow_path: str
    expected_version: int = Field(ge=0)
    template_ids: list[str]


class MoveInstanceRequest(StrictModel):
    workflow_path: str
    expected_version: int = Field(ge=0)
    order: int = Field(ge=0)


class ScheduleRequest(StrictModel):
    workflow_path: str
    expected_version: int = Field(ge=0)
    paused: bool | None = None


class AdvanceRequest(ScheduleRequest):
    completed_instance_ids: list[str] = Field(default_factory=list)


class OpcPushRequest(StrictModel):
    workflow_path: str
    expected_version: int = Field(ge=0)
    provider_id: str = Field(min_length=1, max_length=128)
    sequence: int = Field(ge=0)
    values: dict[str, Any] = Field(default_factory=dict, max_length=64)

    @model_validator(mode="after")
    def validate_snapshot_values(self) -> OpcPushRequest:
        """拒绝敏感字段和无界嵌套 OPC 数据。"""
        def visit(value: Any, depth: int = 0) -> None:
            if depth > 3:
                raise ValueError("OPC values may not nest deeper than 3 levels")
            if isinstance(value, str):
                if len(value) > 1024:
                    raise ValueError("OPC string values may not exceed 1024 characters")
                return
            if isinstance(value, (bool, int, float)) or value is None:
                return
            if isinstance(value, dict):
                if len(value) > 32:
                    raise ValueError("OPC nested objects may not exceed 32 keys")
                for key, child in value.items():
                    if not isinstance(key, str) or len(key) > 128:
                        raise ValueError("OPC variable names must be strings up to 128 characters")
                    if any(marker in key.lower() for marker in ("password", "token", "secret", "credential")):
                        raise ValueError("OPC values may not contain sensitive keys")
                    visit(child, depth + 1)
                return
            if isinstance(value, list):
                if len(value) > 32:
                    raise ValueError("OPC arrays may not exceed 32 items")
                for child in value:
                    visit(child, depth + 1)
                return
            raise ValueError("OPC values must be JSON-compatible")

        visit(self.values)
        return self


class WaitingReason(StrictModel):
    """稳定机器码、结构化上下文及兼容展示的等待原因。"""

    code: str
    context: dict[str, Any] = Field(default_factory=dict)
    message: str | None = None


class ConditionResult(StrictModel):
    """单个条件的判定结果及结构化等待原因。"""

    satisfied: bool
    reason: WaitingReason | None = None


class SchedulingResult(StrictModel):
    """调度策略选出的可启动实例及其余实例的等待原因。"""

    startable_instance_ids: list[str] = Field(default_factory=list)
    waiting_reasons: dict[str, WaitingReason] = Field(default_factory=dict)
    entries: list[TaskScheduleEntry] = Field(default_factory=list)
