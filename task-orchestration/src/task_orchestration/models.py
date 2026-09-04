"""Task 编排服务的数据契约。"""

from __future__ import annotations

from typing import Any, Literal, Self
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

POLICY_RESOURCE_PREFIX = "resource:"
POLICY_WORKSTATION_PREFIX = "workstation:"


class StrictModel(BaseModel):
    """拒绝契约之外字段的持久化与写入 DTO 基类。"""

    model_config = ConfigDict(extra="forbid")

    def validated_copy(self, *, update: dict[str, Any] | None = None) -> Self:
        """复制模型并重新执行完整字段及模型校验。"""
        data = self.model_dump(round_trip=True)
        if update:
            data.update(update)
        return type(self).model_validate(data)


class Trigger(StrictModel):
    """由已注册 PLC 变量判定的 Task 条件。"""

    kind: Literal["opc"]
    config: dict[str, Any]

    @model_validator(mode="after")
    def validate_plc_variable_condition(self) -> Trigger:
        """拒绝旧触发器及未绑定 PLC 注册变量的 OPC 条件。"""
        required = ("plc_device_id", "variable", "value")
        missing = [
            key
            for key in required
            if key not in self.config or self.config[key] == ""
        ]
        if missing:
            raise ValueError(
                f"OPC trigger missing required fields: {', '.join(missing)}"
            )
        return self


class TaskDependency(StrictModel):
    """Task 启动前必须完成的前置 Task 或其中的指定节点。"""

    template_id: str
    node_id: str | None = None

    @field_validator("template_id", "node_id")
    @classmethod
    def reject_blank_ids(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("task dependency ids must not be blank")
        return value


class Template(StrictModel):
    """由 workflow 节点集合派生的可复用 Task 模板。"""

    id: str
    name: str
    workflow_path: str = ""
    node_ids: list[str] = Field(default_factory=list)
    resources: list[str] = Field(default_factory=list)
    input_triggers: list[Trigger] = Field(default_factory=list)
    output_triggers: list[Trigger] = Field(default_factory=list)
    dependencies: list[TaskDependency] | None = None

    @model_validator(mode="after")
    def validate_dependencies(self) -> Template:
        """None 保留旧串行语义；空列表表示显式无前置 Task。"""
        if self.dependencies is None:
            return self
        keys = [
            (dependency.template_id, dependency.node_id)
            for dependency in self.dependencies
        ]
        if len(keys) != len(set(keys)):
            raise ValueError("task dependencies must be unique")
        if any(
            dependency.template_id == self.id
            for dependency in self.dependencies
        ):
            raise ValueError("task template must not depend on itself")
        return self


class NodeExecutionRecord(StrictModel):
    """单个 workflow 节点的一次幂等执行记录。"""

    node_id: str
    attempt: int = Field(ge=1)
    execution_id: str
    status: Literal["pending", "running", "succeeded", "failed"] = "pending"
    started_at: int | None = Field(default=None, ge=0)
    finished_at: int | None = Field(default=None, ge=0)
    result: JsonValue = None
    error: JsonValue = None
    resources: list[str] = Field(default_factory=list)
    release_resources: list[str] = Field(default_factory=list)

    @field_validator("node_id", "execution_id")
    @classmethod
    def reject_blank_ids(cls, value: str) -> str:
        """节点与执行 ID 必须可用于稳定幂等匹配。"""
        if not value.strip():
            raise ValueError("node and execution ids must not be blank")
        return value

    @field_validator("resources", "release_resources")
    @classmethod
    def validate_resource_names(cls, values: list[str]) -> list[str]:
        """动作资源必须是非空且无重复的稳定标识。"""
        if any(not value.strip() for value in values):
            raise ValueError("action resources must not be blank")
        if len(values) != len(set(values)):
            raise ValueError("action resources must be unique")
        return values

    @model_validator(mode="after")
    def validate_lifecycle_timestamps(self) -> NodeExecutionRecord:
        """保证节点执行状态与起止时间一致。"""
        if self.finished_at is not None:
            if self.started_at is None:
                raise ValueError("finished_at requires started_at")
            if self.finished_at < self.started_at:
                raise ValueError("finished_at must be greater than or equal to started_at")
        if self.status == "pending":
            if self.started_at is not None or self.finished_at is not None:
                raise ValueError("pending records must not have timestamps")
        elif self.status == "running":
            if self.started_at is None or self.finished_at is not None:
                raise ValueError("running records require started_at and no finished_at")
        elif self.started_at is None or self.finished_at is None:
            raise ValueError("terminal records require started_at and finished_at")
        return self


class ResourceLease(StrictModel):
    """节点执行期间持有的动态资源租约。"""

    resource: str
    instance_id: str
    node_id: str
    execution_id: str
    acquired_at: int = Field(ge=0)

    @field_validator("resource", "instance_id", "node_id", "execution_id")
    @classmethod
    def reject_blank_ids(cls, value: str) -> str:
        """租约定位与释放所需标识不得为空。"""
        if not value.strip():
            raise ValueError("resource lease ids must not be blank")
        return value


class TaskExecutionState(StrictModel):
    """Task 的可恢复状态；cursor 是下一个待执行节点在 node_ids 中的下标。"""

    cursor: int = Field(default=0, ge=0)
    records: list[NodeExecutionRecord] = Field(default_factory=list)
    active_execution_id: str | None = None
    active_node_id: str | None = None

    @field_validator("active_execution_id", "active_node_id")
    @classmethod
    def reject_blank_active_ids(cls, value: str | None) -> str | None:
        """已设置的活动标识不得为空白。"""
        if value is not None and not value.strip():
            raise ValueError("active execution ids must not be blank")
        return value

    @model_validator(mode="after")
    def validate_active_execution(self) -> TaskExecutionState:
        """活动游标必须唯一对应一条 running 执行记录。"""
        execution_ids = [record.execution_id for record in self.records]
        if len(execution_ids) != len(set(execution_ids)):
            raise ValueError("execution ids must be unique")

        active_ids_present = (
            self.active_execution_id is not None,
            self.active_node_id is not None,
        )
        if active_ids_present[0] != active_ids_present[1]:
            raise ValueError("active execution and node ids must be set together")

        running_records = [
            record for record in self.records if record.status == "running"
        ]
        if self.active_execution_id is None:
            if running_records:
                raise ValueError("running records require an active execution")
            return self

        matching_records = [
            record
            for record in running_records
            if record.execution_id == self.active_execution_id
            and record.node_id == self.active_node_id
        ]
        if len(running_records) != 1 or len(matching_records) != 1:
            raise ValueError("active execution must match the only running record")
        return self


class WorkspacePauseReason(StrictModel):
    """工作区暂停的稳定机器码与节点执行上下文。"""

    code: str
    message: str
    instance_id: str | None = None
    node_id: str | None = None
    execution_id: str | None = None
    timestamp: int = Field(ge=0)
    detail: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator(
        "code", "message", "instance_id", "node_id", "execution_id"
    )
    @classmethod
    def reject_blank_values(cls, value: str | None) -> str | None:
        """暂停原因的已提供文本字段不得为空白。"""
        if value is not None and not value.strip():
            raise ValueError("pause reason values must not be blank")
        return value


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
    not_before: int | None = Field(default=None, ge=0)
    started_at: int | None = Field(default=None, ge=0)
    finished_at: int | None = Field(default=None, ge=0)
    execution_state: TaskExecutionState = Field(default_factory=TaskExecutionState)

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
        if self.status != "running" and (
            self.execution_state.active_execution_id is not None
            or self.execution_state.active_node_id is not None
            or any(
                record.status == "running"
                for record in self.execution_state.records
            )
        ):
            raise ValueError(
                "only running task instances may have active execution"
            )
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
    """由 PLC 运行时分发的 OPC 快照，仅用于条件判定。"""

    plc_device_id: str = Field(min_length=1, max_length=128)
    sequence: int = Field(ge=0)
    values: dict[str, Any] = Field(default_factory=dict, max_length=64)
    updated_at_by_variable: dict[str, float] = Field(default_factory=dict, max_length=64)


class PlcRegistration(StrictModel):
    """PLC runtime 分发给排程服务的当前变量注册名单。"""

    plc_device_id: str = Field(min_length=1, max_length=128)
    runtime_url: str = Field(min_length=1, max_length=2048)
    variables: list[str] = Field(min_length=1, max_length=4096)
    aliases: dict[str, str] = Field(default_factory=dict, max_length=4096)

    @model_validator(mode="after")
    def validate_variables(self) -> PlcRegistration:
        if any(not variable.strip() for variable in self.variables):
            raise ValueError("PLC registered variables must not be empty")
        if len(self.variables) != len(set(self.variables)):
            raise ValueError("PLC registered variables must be unique")
        if any(not alias.strip() or not canonical.strip()
               for alias, canonical in self.aliases.items()):
            raise ValueError("PLC variable aliases must not be empty")
        unknown_canonical_names = set(self.aliases.values()) - set(self.variables)
        if unknown_canonical_names:
            raise ValueError(
                "PLC variable aliases must resolve to registered variables: "
                f"{sorted(unknown_canonical_names)}"
            )
        return self

    def canonical_variable_name(self, variable: str) -> str:
        """将界面/动作别名解析为 CSV 中的真实变量名。"""
        return self.aliases.get(variable, variable)


class WorkspaceEvent(StrictModel):
    """持久化的调度输入、输出和状态变更事件。"""

    kind: Literal[
        "opc_snapshot", "output", "scheduled", "completed", "template_deleted",
        "templates_deleted",
        "scheduled_templates_updated", "instances_cleared",
        "instances_progress_reset", "instance_parameters_updated",
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
    dynamic_resource_leases: list[ResourceLease] = Field(default_factory=list)
    pause_reason: WorkspacePauseReason | None = None
    schedule_entries: list[TaskScheduleEntry] = Field(default_factory=list)
    opc_snapshots: list[OpcSnapshotState] = Field(default_factory=list)
    plc_registrations: list[PlcRegistration] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_unique_ids(self) -> Workspace:
        """校验工作区内所有持久化引用与执行状态的一致性。"""
        template_ids = [template.id for template in self.templates]
        if len(template_ids) != len(set(template_ids)):
            raise ValueError("template ids must be unique")
        instance_ids = [instance.id for instance in self.task_instances]
        if len(instance_ids) != len(set(instance_ids)):
            raise ValueError("task instance ids must be unique")
        template_id_set = set(template_ids)
        if any(instance.template_id not in template_id_set for instance in self.task_instances):
            raise ValueError("task instances must reference an existing template")
        template_by_id = {template.id: template for template in self.templates}
        for instance in self.task_instances:
            template = template_by_id[instance.template_id]
            if instance.execution_state.cursor > len(template.node_ids):
                raise ValueError(
                    "execution cursor must not exceed template node count"
                )
            if (
                instance.status == "completed"
                and instance.execution_state.cursor != len(template.node_ids)
            ):
                raise ValueError(
                    "completed instance execution must be finished"
                )
            if (
                instance.status == "completed"
                and not instance.execution_state.records
            ):
                continue
            if any(
                record.node_id not in template.node_ids
                for record in instance.execution_state.records
            ):
                raise ValueError("execution record node must belong to template")
            if instance.execution_state.active_node_id is not None and (
                instance.execution_state.cursor >= len(template.node_ids)
                or instance.execution_state.active_node_id
                != template.node_ids[instance.execution_state.cursor]
            ):
                raise ValueError(
                    "active execution must point to cursor node"
                )
            succeeded_records = [
                record
                for record in instance.execution_state.records
                if record.status == "succeeded"
            ]
            predecessor_nodes = template.node_ids[:instance.execution_state.cursor]
            if any(
                sum(record.node_id == node_id for record in succeeded_records) != 1
                for node_id in predecessor_nodes
            ):
                raise ValueError(
                    "cursor predecessors must each have one succeeded record"
                )
            if any(
                record.node_id in template.node_ids[instance.execution_state.cursor:]
                for record in succeeded_records
            ):
                raise ValueError(
                    "cursor and succeeded records are inconsistent"
                )
            if [record.node_id for record in succeeded_records] != predecessor_nodes:
                raise ValueError(
                    "succeeded records must follow template node order"
                )
        sample_orders = [(item.sample_id, item.order) for item in self.task_instances]
        if len(sample_orders) != len(set(sample_orders)):
            raise ValueError("instance orders must be unique within each sample")
        if any(item not in template_id_set for item in self.scheduled_template_ids):
            raise ValueError("scheduled template ids must exist")
        instance_id_set = set(instance_ids)
        instance_by_id = {item.id: item for item in self.task_instances}
        leased_resources: set[str] = set()
        for lease in self.dynamic_resource_leases:
            instance = instance_by_id.get(lease.instance_id)
            if instance is None:
                raise ValueError("dynamic resource lease instance must exist")
            template = template_by_id[instance.template_id]
            if lease.node_id not in template.node_ids:
                raise ValueError(
                    "dynamic resource lease node must belong to template"
                )
            matching_record = next(
                (
                    record
                    for record in instance.execution_state.records
                    if record.node_id == lease.node_id
                    and record.execution_id == lease.execution_id
                ),
                None,
            )
            if matching_record is None:
                raise ValueError(
                    "dynamic resource lease must match an execution record"
                )
            if lease.resource not in matching_record.resources:
                raise ValueError(
                    "dynamic resource lease must be declared by execution"
                )
            if lease.resource in leased_resources:
                raise ValueError(
                    "dynamic resources may have only one active lease"
                )
            leased_resources.add(lease.resource)

        if self.pause_reason is not None:
            reason = self.pause_reason
            if reason.instance_id is None:
                if reason.node_id is not None:
                    raise ValueError("pause reason node requires instance_id")
                if reason.execution_id is not None:
                    raise ValueError(
                        "pause reason execution requires instance_id"
                    )
            else:
                instance = instance_by_id.get(reason.instance_id)
                if instance is None:
                    raise ValueError("pause reason instance must exist")
                template = template_by_id[instance.template_id]
                if reason.node_id is None:
                    if reason.execution_id is not None:
                        raise ValueError(
                            "pause reason execution requires node_id"
                        )
                else:
                    if reason.node_id not in template.node_ids:
                        raise ValueError(
                            "pause reason node must belong to template"
                        )
                    if reason.execution_id is not None and not any(
                        record.node_id == reason.node_id
                        and record.execution_id == reason.execution_id
                        for record in instance.execution_state.records
                    ):
                        raise ValueError(
                            "pause reason must match an execution record"
                        )
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
        plc_device_ids = [item.plc_device_id for item in self.plc_registrations]
        if len(plc_device_ids) != len(set(plc_device_ids)):
            raise ValueError("PLC registrations must be unique by device id")
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


class WorkspaceResetRequest(StrictModel):
    """删除单个 workflow 的 Task sidecar。"""

    workflow_path: str = Field(min_length=1)


class OpcSnapshotRequest(StrictModel):
    """OPC 快照接口预留 DTO，暂不连接 provider。"""

    workflow_path: str
    plc_device_id: str
    variables: list[str] = Field(default_factory=list)


class TemplateCreateRequest(StrictModel):
    workflow_path: str
    expected_version: int = Field(ge=0)
    template: Template


class TemplateUpdateRequest(StrictModel):
    workflow_path: str
    expected_version: int = Field(ge=0)
    name: str | None = None
    input_triggers: list[Trigger] | None = None
    output_triggers: list[Trigger] | None = None
    dependencies: list[TaskDependency] | None = None


class TemplatesDeleteRequest(StrictModel):
    workflow_path: str
    expected_version: int = Field(ge=0)
    template_ids: list[str] = Field(min_length=1)


class GenerateInstancesRequest(StrictModel):
    workflow_path: str
    expected_version: int = Field(ge=0)
    template_ids: list[str] = Field(min_length=1)
    sample_ids: list[str] = Field(min_length=1)
    sample_start_interval_seconds: float = Field(default=0, ge=0, le=86_400)
    template_node_parameters: dict[str, dict[str, dict[str, Any]]] = Field(
        default_factory=dict
    )
    sample_template_node_parameters: dict[
        str, dict[str, dict[str, dict[str, Any]]]
    ] = Field(default_factory=dict)


class ClearInstancesRequest(StrictModel):
    workflow_path: str
    expected_version: int = Field(ge=0)


class ResetInstancesProgressRequest(StrictModel):
    workflow_path: str
    expected_version: int = Field(ge=0)


class ScheduledTemplatesUpdateRequest(StrictModel):
    workflow_path: str
    expected_version: int = Field(ge=0)
    template_ids: list[str]


class MoveInstanceRequest(StrictModel):
    workflow_path: str
    expected_version: int = Field(ge=0)
    order: int = Field(ge=0)


class InstanceParametersUpdateRequest(StrictModel):
    """更新单个未运行 Task 实例的 Action 节点参数覆盖。"""

    workflow_path: str
    expected_version: int = Field(ge=0)
    node_parameters: dict[str, dict[str, Any]] = Field(default_factory=dict)


class ScheduleRequest(StrictModel):
    workflow_path: str
    expected_version: int = Field(ge=0)
    paused: bool | None = None
    acknowledge_peer_failure: bool = False


class AdvanceRequest(ScheduleRequest):
    completed_instance_ids: list[str] = Field(default_factory=list)


class ActionRequestBase(StrictModel):
    """动作状态转换的共同乐观锁与幂等定位字段。"""

    workflow_path: str = Field(min_length=1)
    expected_version: int = Field(ge=0)
    instance_id: str = Field(min_length=1)
    node_id: str = Field(min_length=1)
    execution_id: str = Field(min_length=1)

    @field_validator("workflow_path", "instance_id", "node_id", "execution_id")
    @classmethod
    def reject_blank_action_ids(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("action request ids must not be blank")
        return value


class ActionClaimRequest(ActionRequestBase):
    """原子认领并启动节点；resources 仅保留旧 API 兼容。"""

    resources: list[str] = Field(default_factory=list)


class ActionSucceedRequest(ActionRequestBase):
    """完成活动动作；release_resources 仅保留旧 API 兼容。"""

    result: JsonValue = None
    release_resources: list[str] = Field(default_factory=list)


class ActionFailRequest(ActionRequestBase):
    """失败活动动作并携带 JSON 兼容错误详情。"""

    error: JsonValue


class OpcPushRequest(StrictModel):
    workflow_path: str
    expected_version: int = Field(ge=0)
    plc_device_id: str = Field(min_length=1, max_length=128)
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


class PlcRegistrationRequest(StrictModel):
    """PLC runtime 在快照前注册可供模板引用的变量。"""

    workflow_path: str
    expected_version: int = Field(default=0, ge=0)
    registration: PlcRegistration


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
