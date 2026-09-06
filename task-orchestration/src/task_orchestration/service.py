"""Task 工作区的生命周期与原子调度业务服务。"""

from __future__ import annotations

from collections.abc import Callable, Iterable
import json
import threading
import time
from typing import Any
from uuid import uuid4

from .conditions import OpcConditionProvider
from .models import (
    NodeExecutionRecord,
    SchedulingResult,
    PlcRegistration,
    OpcSnapshotState,
    TaskDependency,
    TaskScheduleEntry,
    TaskInstance,
    Template,
    Trigger,
    WaitingReason,
    Workspace,
    WorkspaceEvent,
    WorkspacePauseReason,
)
from .policy import FifoResourcePolicy, SchedulingPolicy
from .store import WorkspaceStore


class WorkspaceServiceError(ValueError):
    """业务规则不满足。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _json_values_equal(left, right) -> bool:
    """递归比较 JSON 值，同时保留 bool、int、float 等类型差异。"""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            _json_values_equal(left[key], right[key]) for key in left
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _json_values_equal(left_item, right_item)
            for left_item, right_item in zip(left, right)
        )
    return left == right


class WorkspaceService:
    """把状态判定、调度决策和写入收敛到同一个 sidecar 事务。"""

    def __init__(
        self,
        store: WorkspaceStore,
        *,
        conditions: OpcConditionProvider | None = None,
        policy: SchedulingPolicy | None = None,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self.store = store
        self.conditions = conditions or OpcConditionProvider()
        self.policy = policy or FifoResourcePolicy()
        self._opc_lock = threading.RLock()
        self._clock = clock or (lambda: int(time.time() * 1000))

    def _mutate(
        self,
        workflow_path: str,
        *,
        expected_version: int,
        operation: Callable[[Workspace], Workspace],
    ):
        """所有服务写事务在业务操作前统一清除旧 Task 资源租约。"""
        return self.store.mutate(
            workflow_path,
            expected_version=expected_version,
            operation=lambda workspace: operation(
                self._normalize_runtime_resources(workspace)
            ),
        )

    def _mutate_latest(
        self,
        workflow_path: str,
        *,
        operation: Callable[[Workspace], Workspace],
    ):
        return self.store.mutate_latest(
            workflow_path,
            operation=lambda workspace: operation(
                self._normalize_runtime_resources(workspace)
            ),
        )

    def _mutate_idempotent(
        self,
        workflow_path: str,
        *,
        expected_version: int,
        operation: Callable[[Workspace], Workspace | None],
    ):
        def normalize_and_apply(workspace: Workspace) -> Workspace | None:
            had_legacy_leases = bool(workspace.dynamic_resource_leases)
            normalized = self._normalize_runtime_resources(workspace)
            updated = operation(normalized)
            if updated is None and had_legacy_leases:
                return normalized
            return updated

        return self.store.mutate_idempotent(
            workflow_path,
            expected_version=expected_version,
            operation=normalize_and_apply,
        )

    def reset_workspace(self, workflow_path: str):
        """原子清理 sidecar 及对应 OPC 内存序列，允许 runtime 从 1 重启。"""
        with self._opc_lock:
            response = self.store.reset(workflow_path)
            self.conditions.clear_workflow(workflow_path)
            return response

    def create_template(
        self, workflow_path: str, expected_version: int, template: Template
    ):
        def operation(workspace: Workspace) -> Workspace:
            if any(item.id == template.id for item in workspace.templates):
                raise WorkspaceServiceError("template_exists", "template already exists")
            item = self._canonicalize_template_triggers(workspace, template)
            self._validate_template_conditions(workspace, item)
            item = item.model_copy(
                update={
                    "workflow_path": workspace.workflow_path,
                    "resources": [],
                }
            )
            return workspace.model_copy(update={"templates": [*workspace.templates, item]})

        return self._mutate(
            workflow_path, expected_version=expected_version, operation=operation
        )

    def update_template(
        self,
        workflow_path: str,
        expected_version: int,
        template_id: str,
        *,
        name: str | None = None,
        input_triggers: list[Trigger] | None = None,
        output_triggers: list[Trigger] | None = None,
        dependencies: list[TaskDependency] | None = None,
    ):
        """热更新共享模板；未启动实例在下一轮调度采用新的依赖。"""

        def operation(workspace: Workspace) -> Workspace:
            template = self._template(workspace, template_id)
            updates = {}
            if name is not None:
                if not name.strip():
                    raise WorkspaceServiceError("invalid_template_name", "template name is required")
                updates["name"] = name.strip()
            if input_triggers is not None:
                updates["input_triggers"] = self._canonicalize_triggers(
                    workspace, input_triggers, "input"
                )
            if output_triggers is not None:
                updates["output_triggers"] = self._canonicalize_triggers(
                    workspace, output_triggers, "output"
                )
            if dependencies is not None:
                updates["dependencies"] = dependencies
            updates["resources"] = []
            try:
                replacement = template.validated_copy(update=updates)
            except ValueError as exc:
                raise WorkspaceServiceError(
                    "invalid_task_dependencies", str(exc)
                ) from exc
            return workspace.model_copy(
                update={
                    "templates": [
                        replacement if item.id == template_id else item
                        for item in workspace.templates
                    ]
                }
            )

        return self._mutate(
            workflow_path, expected_version=expected_version, operation=operation
        )

    def register_plc_variables(
        self,
        workflow_path: str,
        expected_version: int,
        registration: PlcRegistration,
    ):
        """记录 PLC runtime 当前注册表；忽略浏览器版本，始终原子写入最新工作区。"""
        del expected_version
        canonical_path = self.store.get(workflow_path).workspace.workflow_path
        with self._opc_lock:
            runtime_changed = False

            def operation(workspace: Workspace) -> Workspace:
                nonlocal runtime_changed
                previous_registration = self._registration(
                    workspace, registration.plc_device_id
                )
                runtime_changed = (
                    previous_registration is not None
                    and previous_registration.runtime_url != registration.runtime_url
                )
                registrations = [
                    item
                    for item in workspace.plc_registrations
                    if item.plc_device_id != registration.plc_device_id
                ]
                registrations.append(registration)
                templates = [
                    self._canonicalize_template_triggers(
                        workspace.model_copy(update={"plc_registrations": registrations}),
                        template,
                    )
                    for template in workspace.templates
                ]
                return workspace.model_copy(
                    update={
                        "plc_registrations": registrations,
                        "templates": templates,
                        "opc_snapshots": [
                            item
                            for item in workspace.opc_snapshots
                            if item.plc_device_id != registration.plc_device_id
                        ] if runtime_changed else workspace.opc_snapshots,
                        "scheduler_paused": (
                            True if runtime_changed else workspace.scheduler_paused
                        ),
                    }
                )

            response = self._mutate_latest(canonical_path, operation=operation)
            if runtime_changed:
                self.conditions.clear_state(canonical_path, registration.plc_device_id)
            return response

    def delete_template(self, workflow_path: str, expected_version: int, template_id: str):
        def operation(workspace: Workspace) -> Workspace:
            self._template(workspace, template_id)
            deleted_instance_ids = [
                item.id
                for item in workspace.task_instances
                if item.template_id == template_id
            ]
            return workspace.model_copy(
                update={
                    "templates": [
                        item for item in workspace.templates if item.id != template_id
                    ],
                    "task_instances": [
                        item
                        for item in workspace.task_instances
                        if item.template_id != template_id
                    ],
                    "scheduled_template_ids": [
                        item
                        for item in workspace.scheduled_template_ids
                        if item != template_id
                    ],
                    "schedule_entries": [
                        item
                        for item in workspace.schedule_entries
                        if item.template_id != template_id
                    ],
                    "events": [
                        *workspace.events,
                        WorkspaceEvent(
                            kind="template_deleted",
                            timestamp=self._clock(),
                            idempotency_key=f"template/{template_id}/delete/{expected_version}",
                            payload={
                                "deleted_template_id": template_id,
                                "deleted_instance_ids": deleted_instance_ids,
                            },
                        ),
                    ],
                }
            )

        return self._mutate(
            workflow_path, expected_version=expected_version, operation=operation
        )

    def delete_templates(
        self, workflow_path: str, expected_version: int, template_ids: list[str]
    ):
        """原子删除多个模板及其关联实例、排程和待排选择。"""
        ordered_template_ids = list(dict.fromkeys(template_ids))

        def operation(workspace: Workspace) -> Workspace:
            for template_id in ordered_template_ids:
                self._template(workspace, template_id)
            deleted_template_ids = set(ordered_template_ids)
            deleted_instance_ids = [
                item.id
                for item in workspace.task_instances
                if item.template_id in deleted_template_ids
            ]
            deleted_instance_id_set = set(deleted_instance_ids)
            return workspace.model_copy(
                update={
                    "templates": [
                        item
                        for item in workspace.templates
                        if item.id not in deleted_template_ids
                    ],
                    "task_instances": [
                        item
                        for item in workspace.task_instances
                        if item.template_id not in deleted_template_ids
                    ],
                    "scheduled_template_ids": [
                        item
                        for item in workspace.scheduled_template_ids
                        if item not in deleted_template_ids
                    ],
                    "schedule_entries": [
                        item
                        for item in workspace.schedule_entries
                        if item.template_id not in deleted_template_ids
                    ],
                    "pause_reason": (
                        None
                        if workspace.pause_reason is not None
                        and workspace.pause_reason.instance_id
                        in deleted_instance_id_set
                        else workspace.pause_reason
                    ),
                    "events": [
                        *[
                            event
                            for event in workspace.events
                            if event.instance_id not in deleted_instance_id_set
                            and event.template_id not in deleted_template_ids
                        ],
                        WorkspaceEvent(
                            kind="templates_deleted",
                            timestamp=self._clock(),
                            idempotency_key=(
                                f"templates/delete/{expected_version}/"
                                f"{','.join(ordered_template_ids)}"
                            ),
                            payload={
                                "deleted_template_ids": ordered_template_ids,
                                "deleted_instance_ids": deleted_instance_ids,
                            },
                        ),
                    ],
                }
            )

        return self._mutate(
            workflow_path, expected_version=expected_version, operation=operation
        )

    def update_scheduled_templates(
        self,
        workflow_path: str,
        expected_version: int,
        template_ids: list[str],
    ):
        """按客户端指定顺序更新本次待排模板，禁止修改实例状态。"""
        def operation(workspace: Workspace) -> Workspace:
            for template_id in template_ids:
                self._template(workspace, template_id)
            event = WorkspaceEvent(
                kind="scheduled_templates_updated",
                timestamp=self._clock(),
                idempotency_key=(
                    f"{workspace.workflow_path}/scheduled-templates/{expected_version}"
                ),
                payload={"template_ids": template_ids},
            )
            return workspace.model_copy(
                update={
                    "scheduled_template_ids": template_ids,
                    "events": [*workspace.events, event],
                }
            )

        return self._mutate(
            workflow_path, expected_version=expected_version, operation=operation
        )

    def generate_instances(
        self,
        workflow_path: str,
        expected_version: int,
        template_ids: list[str],
        sample_ids: list[str],
        *,
        sample_start_interval_seconds: float = 0,
        template_node_parameters: dict[str, dict[str, dict[str, Any]]] | None = None,
        sample_template_node_parameters: dict[
            str, dict[str, dict[str, dict[str, Any]]]
        ] | None = None,
    ):
        def operation(workspace: Workspace) -> Workspace:
            templates = [self._template(workspace, item) for item in template_ids]
            remembered_parameters = template_node_parameters or {}
            sample_parameters = sample_template_node_parameters or {}
            unknown_templates = set(remembered_parameters) - set(template_ids)
            if unknown_templates:
                raise WorkspaceServiceError(
                    "unknown_template",
                    f"parameter defaults reference unselected templates: {sorted(unknown_templates)}",
                )
            unknown_samples = set(sample_parameters) - set(sample_ids)
            if unknown_samples:
                raise WorkspaceServiceError(
                    "unknown_sample",
                    f"parameter defaults reference unselected samples: {sorted(unknown_samples)}",
                )
            for sample_id, template_parameters in sample_parameters.items():
                unknown_sample_templates = set(template_parameters) - set(template_ids)
                if unknown_sample_templates:
                    raise WorkspaceServiceError(
                        "unknown_template",
                        "sample parameter defaults reference unselected templates "
                        f"for {sample_id}: {sorted(unknown_sample_templates)}",
                    )
            for template in templates:
                parameter_sets = [remembered_parameters.get(template.id, {})]
                parameter_sets.extend(
                    parameters.get(template.id, {})
                    for parameters in sample_parameters.values()
                )
                for node_parameters in parameter_sets:
                    unknown_nodes = set(node_parameters) - set(template.node_ids)
                    if unknown_nodes:
                        raise WorkspaceServiceError(
                            "unknown_action_node",
                            f"action nodes are not in template {template.id}: {sorted(unknown_nodes)}",
                        )
            generated: list[TaskInstance] = []
            batch_anchor = self._clock()
            interval_ms = round(sample_start_interval_seconds * 1_000)
            for sample_index, sample_id in enumerate(sample_ids):
                if not sample_id:
                    raise WorkspaceServiceError("invalid_sample_id", "sample_id is required")
                sample_not_before = batch_anchor + sample_index * interval_ms
                next_order = max(
                    (item.order for item in workspace.task_instances if item.sample_id == sample_id),
                    default=-1,
                ) + 1
                for template in templates:
                    per_sample_parameters = sample_parameters.get(sample_id, {})
                    if template.id in per_sample_parameters:
                        instance_parameters = per_sample_parameters[template.id]
                    else:
                        instance_parameters = remembered_parameters.get(template.id, {})
                    generated.append(
                        TaskInstance(
                            id=uuid4().hex,
                            template_id=template.id,
                            status="waiting",
                            sample_id=sample_id,
                            order=next_order,
                            not_before=sample_not_before,
                            payload={
                                "node_parameters": instance_parameters
                            } if instance_parameters else {},
                        )
                    )
                    next_order += 1
            updated = workspace.model_copy(
                update={"task_instances": [*workspace.task_instances, *generated]}
            )
            return updated.model_copy(
                update={"schedule_entries": self._build_schedule_entries(updated)}
            )

        return self._mutate(
            workflow_path, expected_version=expected_version, operation=operation
        )

    def clear_instances(self, workflow_path: str, expected_version: int):
        """清空 Task 队列实例；保留模板、排程模板列表与 OPC/PLC 注册。"""

        def operation(workspace: Workspace) -> Workspace:
            if not workspace.scheduler_paused:
                raise WorkspaceServiceError(
                    "scheduler_active",
                    "pause scheduler before clearing task queue",
                )
            deleted_instance_ids = [item.id for item in workspace.task_instances]
            if not deleted_instance_ids and workspace.pause_reason is None:
                return workspace
            retained_events = [
                item for item in workspace.events if item.instance_id is None
            ]
            event = WorkspaceEvent(
                kind="instances_cleared",
                timestamp=self._clock(),
                idempotency_key=(
                    f"{workspace.workflow_path}/instances/clear/{expected_version}"
                ),
                payload={"deleted_instance_ids": deleted_instance_ids},
            )
            return workspace.model_copy(
                update={
                    "task_instances": [],
                    "schedule_entries": [],
                    "pause_reason": None,
                    "scheduler_paused": True,
                    "dynamic_resource_leases": [],
                    "events": [*retained_events, event],
                }
            )

        return self._mutate(
            workflow_path, expected_version=expected_version, operation=operation
        )

    def reset_instances_progress(self, workflow_path: str, expected_version: int):
        """保留 Task、顺序与参数，以新实例 ID 重置整队执行进度。"""

        def operation(workspace: Workspace) -> Workspace:
            if not workspace.scheduler_paused:
                raise WorkspaceServiceError(
                    "scheduler_active",
                    "pause scheduler before resetting task progress",
                )
            if any(
                instance.execution_state.active_execution_id is not None
                or instance.execution_state.active_node_id is not None
                or any(
                    record.status == "running"
                    for record in instance.execution_state.records
                )
                for instance in workspace.task_instances
            ):
                raise WorkspaceServiceError(
                    "actions_in_flight",
                    "wait for active actions before resetting task progress",
                )

            reset_at = self._clock()
            not_before_values = [
                instance.not_before
                for instance in workspace.task_instances
                if instance.not_before is not None
            ]
            first_not_before = min(not_before_values, default=reset_at)
            reset_instances = [
                TaskInstance(
                    id=uuid4().hex,
                    template_id=instance.template_id,
                    status="waiting",
                    sample_id=instance.sample_id,
                    order=instance.order,
                    payload=instance.payload,
                    not_before=(
                        reset_at
                        if instance.not_before is None
                        else reset_at + max(0, instance.not_before - first_not_before)
                    ),
                )
                for instance in workspace.task_instances
            ]
            retained_events = [
                event for event in workspace.events if event.instance_id is None
            ]
            event = WorkspaceEvent(
                kind="instances_progress_reset",
                timestamp=reset_at,
                idempotency_key=(
                    f"{workspace.workflow_path}/instances/reset-progress/"
                    f"{expected_version}"
                ),
                payload={"reset_instance_count": len(reset_instances)},
            )
            return workspace.validated_copy(
                update={
                    "task_instances": reset_instances,
                    "events": [*retained_events, event],
                    "scheduler_paused": True,
                    "pause_reason": None,
                    "schedule_entries": [],
                    "dynamic_resource_leases": [],
                }
            )

        return self._mutate(
            workflow_path, expected_version=expected_version, operation=operation
        )

    def move_instance(
        self, workflow_path: str, expected_version: int, instance_id: str, order: int
    ):
        def operation(workspace: Workspace) -> Workspace:
            instance = self._instance(workspace, instance_id)
            if instance.status not in {"waiting", "pending"}:
                raise WorkspaceServiceError(
                    "instance_not_reorderable",
                    "only waiting or pending instances can be reordered",
                )
            peers = sorted(
                (item for item in workspace.task_instances if item.sample_id == instance.sample_id),
                key=lambda item: (item.order, item.id),
            )
            if order < 0 or order >= len(peers):
                raise WorkspaceServiceError("invalid_order", "order is outside sample sequence")
            peers.remove(instance)
            peers.insert(order, instance)
            orders = {item.id: index for index, item in enumerate(peers)}
            return workspace.model_copy(
                update={
                    "task_instances": [
                        item.model_copy(update={"order": orders[item.id]})
                        if item.id in orders
                        else item
                        for item in workspace.task_instances
                    ]
                }
            )

        return self._mutate(
            workflow_path, expected_version=expected_version, operation=operation
        )

    def update_instance_parameters(
        self,
        workflow_path: str,
        expected_version: int,
        instance_id: str,
        node_parameters: dict[str, dict[str, Any]],
    ):
        """保存单个未运行实例的 Action 节点参数覆盖。"""
        def operation(workspace: Workspace) -> Workspace:
            instance = self._instance(workspace, instance_id)
            if instance.status not in {"waiting", "pending"}:
                raise WorkspaceServiceError(
                    "instance_parameters_locked",
                    "only waiting or pending instances can update action parameters",
                )
            template = self._template(workspace, instance.template_id)
            unknown_nodes = set(node_parameters) - set(template.node_ids)
            if unknown_nodes:
                raise WorkspaceServiceError(
                    "unknown_action_node",
                    f"action nodes are not in template: {sorted(unknown_nodes)}",
                )
            payload = {
                **instance.payload,
                "node_parameters": node_parameters,
            }
            updated_instance = instance.validated_copy(update={"payload": payload})
            event = WorkspaceEvent(
                kind="instance_parameters_updated",
                timestamp=self._clock(),
                instance_id=instance.id,
                template_id=template.id,
                idempotency_key=(
                    f"{workspace.workflow_path}/instances/{instance.id}/parameters/"
                    f"{expected_version}"
                ),
                payload={"node_ids": sorted(node_parameters)},
            )
            return workspace.validated_copy(
                update={
                    "task_instances": self._replace_instance(
                        workspace, updated_instance
                    ),
                    "events": [*workspace.events, event],
                }
            )

        return self._mutate(
            workflow_path, expected_version=expected_version, operation=operation
        )

    def push_opc_snapshot(
        self,
        workflow_path: str,
        expected_version: int,
        plc_device_id: str,
        sequence: int,
        values: dict,
    ):
        canonical_path = self.store.get(workflow_path).workspace.workflow_path
        with self._opc_lock:
            current_workspace = self.store.get(canonical_path).workspace
            canonical_values = self._canonicalize_snapshot_values(
                current_workspace, plc_device_id, values
            )
            self._hydrate_conditions(current_workspace)
            previous = self.conditions.export_state(canonical_path, plc_device_id)
            accepted = self.conditions.can_update(
                canonical_path, plc_device_id, sequence
            )
            if not accepted:
                return self.store.get(canonical_path), False
            if accepted:
                self.conditions.update(
                    canonical_path, plc_device_id, sequence, canonical_values
                )
            snapshot_state = self.conditions.export_state(canonical_path, plc_device_id)

            def operation(workspace: Workspace) -> Workspace:
                event = WorkspaceEvent(
                    kind="opc_snapshot",
                    timestamp=self._clock(),
                    idempotency_key=f"{workspace.workflow_path}/{plc_device_id}/{sequence}",
                    payload={
                        "plc_device_id": plc_device_id,
                        "sequence": sequence,
                        "accepted": accepted,
                        "variable_count": len(canonical_values),
                    },
                )
                snapshots = [
                    item for item in workspace.opc_snapshots if item.plc_device_id != plc_device_id
                ]
                if snapshot_state is not None:
                    snapshots.append(OpcSnapshotState.model_validate(snapshot_state))
                return workspace.model_copy(
                    update={"events": [*workspace.events, event], "opc_snapshots": snapshots}
                )

            try:
                response = self._mutate(
                    canonical_path, expected_version=expected_version, operation=operation
                )
            except Exception:
                if previous is None:
                    self.conditions.clear_state(canonical_path, plc_device_id)
                else:
                    self.conditions.restore_state(canonical_path, previous)
                raise
            return response, accepted

    def plan(
        self,
        workflow_path: str,
        expected_version: int,
        *,
        paused: bool | None = None,
        acknowledge_peer_failure: bool = False,
    ):
        schedule: SchedulingResult | None = None

        def operation(workspace: Workspace) -> Workspace:
            nonlocal schedule
            self._hydrate_conditions(workspace)
            workspace_for_plan = workspace
            if (
                paused is False
                and workspace.pause_reason is not None
                and acknowledge_peer_failure
            ):
                if not any(item.status == "running" for item in workspace.task_instances):
                    raise WorkspaceServiceError(
                        "workspace_recovery_required",
                        "no running instance remains after peer failure",
                    )
                workspace_for_plan = workspace.model_copy(
                    update={
                        "pause_reason": None,
                        "scheduler_paused": False,
                    }
                )
            elif paused is False and workspace_for_plan.pause_reason is not None:
                raise WorkspaceServiceError(
                    "workspace_recovery_required",
                    "failed workspace requires an explicit recovery operation",
                )
            paused_value = (
                workspace_for_plan.scheduler_paused if paused is None else paused
            )
            evaluated, reasons = self._evaluate(workspace_for_plan)
            instances = [
                item.model_copy(
                    update={
                        "status": (
                            item.status
                            if item.status not in {"waiting", "pending"}
                            else ("pending" if item.id in evaluated else "waiting")
                        )
                    }
                )
                for item in workspace_for_plan.task_instances
            ]
            provisional = workspace_for_plan.model_copy(
                update={"task_instances": instances, "scheduler_paused": paused_value}
            )
            schedule = self._schedule(provisional, evaluated, reasons)
            schedule = schedule.model_copy(
                update={"entries": self._build_schedule_entries(provisional)}
            )
            return provisional.model_copy(
                update={
                    "schedule_entries": schedule.entries,
                }
            )

        response = self._mutate(
            workflow_path, expected_version=expected_version, operation=operation
        )
        assert schedule is not None
        return response, schedule

    def claim_action(
        self,
        workflow_path: str,
        expected_version: int,
        instance_id: str,
        node_id: str,
        execution_id: str,
        resources: Iterable[str],
    ):
        """原子认领节点动作；resources 仅作为旧 API 兼容输入。"""
        del resources

        def operation(workspace: Workspace) -> Workspace | None:
            instance = self._instance(workspace, instance_id)
            existing = self._execution_record(instance, execution_id)
            if existing is not None:
                if existing.node_id == node_id:
                    return None
                raise WorkspaceServiceError(
                    "action_replay_conflict",
                    "execution_id was already used with different claim semantics",
                )
            if workspace.scheduler_paused or workspace.pause_reason is not None:
                raise WorkspaceServiceError(
                    "workspace_paused", "workspace scheduler is paused"
                )
            if instance.status != "running":
                raise WorkspaceServiceError(
                    "instance_not_running", "action instance must be running"
                )
            template = self._template(workspace, instance.template_id)
            state = instance.execution_state
            if state.cursor >= len(template.node_ids):
                raise WorkspaceServiceError(
                    "action_cursor_exhausted", "task has no remaining action nodes"
                )
            expected_node_id = template.node_ids[state.cursor]
            if node_id != expected_node_id:
                raise WorkspaceServiceError(
                    "action_node_mismatch",
                    f"expected action node {expected_node_id}, got {node_id}",
                )
            if state.active_execution_id is not None:
                raise WorkspaceServiceError(
                    "action_already_active", "task already has an active action"
                )

            transition_time = self._clock()
            record = NodeExecutionRecord(
                node_id=node_id,
                attempt=1 + sum(
                    item.node_id == node_id for item in state.records
                ),
                execution_id=execution_id,
                status="running",
                started_at=transition_time,
                resources=[],
            )
            updated_state = state.validated_copy(
                update={
                    "records": [*state.records, record],
                    "active_node_id": node_id,
                    "active_execution_id": execution_id,
                }
            )
            updated_instance = instance.validated_copy(
                update={"execution_state": updated_state}
            )
            return workspace.validated_copy(
                update={
                    "task_instances": self._replace_instance(
                        workspace, updated_instance
                    ),
                    "dynamic_resource_leases": [],
                }
            )

        return self._mutate_idempotent(
            workflow_path,
            expected_version=expected_version,
            operation=operation,
        )

    def succeed_action(
        self,
        workflow_path: str,
        expected_version: int,
        instance_id: str,
        node_id: str,
        execution_id: str,
        *,
        result,
        release_resources: Iterable[str],
    ):
        """幂等完成活动动作，推进游标并通过输出条件门控 Task 完成。"""
        del release_resources

        def operation(workspace: Workspace) -> Workspace | None:
            instance = self._instance(workspace, instance_id)
            record = self._execution_record(instance, execution_id)
            if record is None:
                raise WorkspaceServiceError(
                    "action_execution_not_found", "action execution was not found"
                )
            if record.status != "running":
                if (
                    record.status == "succeeded"
                    and record.node_id == node_id
                    and _json_values_equal(record.result, result)
                ):
                    return None
                raise WorkspaceServiceError(
                    "action_replay_conflict",
                    "execution_id was already completed with different semantics",
                )
            self._require_active_execution(instance, node_id, execution_id)

            transition_time = self._clock()
            updated_record = record.validated_copy(
                update={
                    "status": "succeeded",
                    "finished_at": transition_time,
                    "result": result,
                    "release_resources": [],
                }
            )
            state = instance.execution_state
            updated_state = state.validated_copy(
                update={
                    "cursor": state.cursor + 1,
                    "records": [
                        updated_record
                        if item.execution_id == execution_id
                        else item
                        for item in state.records
                    ],
                    "active_node_id": None,
                    "active_execution_id": None,
                }
            )
            template = self._template(workspace, instance.template_id)
            completed = updated_state.cursor == len(template.node_ids)
            updated_instance = instance.validated_copy(
                update={
                    "execution_state": updated_state,
                    "status": "completed" if completed else "running",
                    "finished_at": transition_time if completed else None,
                }
            )
            events = list(workspace.events)
            if completed:
                events.extend(
                    self._completion_events(
                        instance=updated_instance,
                        template=template,
                        timestamp=transition_time,
                    )
                )
            return workspace.validated_copy(
                update={
                    "task_instances": self._replace_instance(
                        workspace, updated_instance
                    ),
                    "dynamic_resource_leases": [],
                    "events": events,
                }
            )

        return self._mutate_idempotent(
            workflow_path,
            expected_version=expected_version,
            operation=operation,
        )

    def fail_action(
        self,
        workflow_path: str,
        expected_version: int,
        instance_id: str,
        node_id: str,
        execution_id: str,
        *,
        error,
    ):
        """幂等失败活动动作，并在同一事务内暂停整个工作区。"""

        def operation(workspace: Workspace) -> Workspace | None:
            instance = self._instance(workspace, instance_id)
            record = self._execution_record(instance, execution_id)
            if record is None:
                raise WorkspaceServiceError(
                    "action_execution_not_found", "action execution was not found"
                )
            if record.status != "running":
                if (
                    record.status == "failed"
                    and record.node_id == node_id
                    and _json_values_equal(record.error, error)
                ):
                    return None
                raise WorkspaceServiceError(
                    "action_replay_conflict",
                    "execution_id was already completed with different semantics",
                )
            self._require_active_execution(instance, node_id, execution_id)
            transition_time = self._clock()
            updated_record = record.validated_copy(
                update={
                    "status": "failed",
                    "finished_at": transition_time,
                    "error": error,
                }
            )
            state = instance.execution_state
            updated_state = state.validated_copy(
                update={
                    "records": [
                        updated_record
                        if item.execution_id == execution_id
                        else item
                        for item in state.records
                    ],
                    "active_node_id": None,
                    "active_execution_id": None,
                }
            )
            updated_instance = instance.validated_copy(
                update={
                    "status": "failed",
                    "finished_at": transition_time,
                    "execution_state": updated_state,
                }
            )
            pause_reason = WorkspacePauseReason(
                code="action_failed",
                message="Action execution failed; workspace paused",
                instance_id=instance_id,
                node_id=node_id,
                execution_id=execution_id,
                timestamp=transition_time,
                detail={"error": error},
            )
            others_still_running = any(
                item.status == "running" and item.id != instance_id
                for item in workspace.task_instances
            )
            workspace_updates: dict[str, Any] = {
                "task_instances": self._replace_instance(
                    workspace, updated_instance
                ),
                "dynamic_resource_leases": [],
            }
            if not others_still_running:
                workspace_updates["scheduler_paused"] = True
                workspace_updates["pause_reason"] = pause_reason
            return workspace.validated_copy(update=workspace_updates)

        return self._mutate_idempotent(
            workflow_path,
            expected_version=expected_version,
            operation=operation,
        )

    def advance(
        self,
        workflow_path: str,
        expected_version: int,
        completed_instance_ids: Iterable[str] = (),
    ):
        schedule: SchedulingResult | None = None
        completed_ids = set(completed_instance_ids)

        def operation(workspace: Workspace) -> Workspace:
            nonlocal schedule
            self._hydrate_conditions(workspace)
            transition_time = self._clock()
            running = {item.id for item in workspace.task_instances if item.status == "running"}
            invalid = completed_ids - running
            if invalid:
                raise WorkspaceServiceError(
                    "instance_not_running", f"instances are not running: {sorted(invalid)}"
                )
            unfinished = []
            for instance_id in completed_ids:
                instance = self._instance(workspace, instance_id)
                template = self._template(workspace, instance.template_id)
                if (
                    instance.execution_state.cursor != len(template.node_ids)
                    or instance.execution_state.active_execution_id is not None
                    or instance.execution_state.active_node_id is not None
                ):
                    unfinished.append(instance_id)
            if unfinished:
                raise WorkspaceServiceError(
                    "execution_not_finished",
                    f"instances still have unfinished actions: {sorted(unfinished)}",
                )
            exhausted_ids = {
                item.id
                for item in workspace.task_instances
                if item.status == "running"
                and item.execution_state.active_execution_id is None
                and item.execution_state.cursor
                == len(self._template(workspace, item.template_id).node_ids)
            }
            completion_ids = completed_ids | exhausted_ids
            events = list(workspace.events)
            instances = []
            for item in workspace.task_instances:
                if item.id in completion_ids:
                    template = self._template(workspace, item.template_id)
                    instances.append(
                        item.validated_copy(
                            update={"status": "completed", "finished_at": transition_time}
                        )
                    )
                    events.extend(
                        self._completion_events(
                            instance=item,
                            template=template,
                            timestamp=transition_time,
                        )
                    )
                else:
                    instances.append(item)
            provisional = workspace.validated_copy(
                update={
                    "task_instances": instances,
                    "events": events,
                    "dynamic_resource_leases": [],
                }
            )
            evaluated, reasons = self._evaluate(provisional)
            states = [
                item.model_copy(
                    update={
                        "status": (
                            item.status
                            if item.status not in {"waiting", "pending"}
                            else ("pending" if item.id in evaluated else "waiting")
                        )
                    }
                )
                for item in provisional.task_instances
            ]
            provisional = provisional.model_copy(update={"task_instances": states})
            schedule = self._schedule(provisional, evaluated, reasons)
            if not provisional.scheduler_paused:
                startable = set(schedule.startable_instance_ids)
                provisional = provisional.model_copy(
                    update={
                        "task_instances": [
                            item.model_copy(
                                update={"status": "running", "started_at": transition_time}
                            )
                            if item.id in startable
                            else item
                            for item in provisional.task_instances
                        ],
                        "events": [
                            *provisional.events,
                            *(
                                WorkspaceEvent(
                                    kind="scheduled",
                                    instance_id=item_id,
                                    template_id=self._instance(provisional, item_id).template_id,
                                    timestamp=transition_time,
                                    idempotency_key=f"instance/{item_id}/scheduled",
                                    payload={
                                        "satisfied_triggers": [
                                            trigger.model_dump(mode="json")
                                            for trigger in self._template(
                                                provisional,
                                                self._instance(provisional, item_id).template_id,
                                            ).input_triggers
                                        ],
                                    },
                                )
                                for item_id in schedule.startable_instance_ids
                            ),
                        ],
                    }
                )
            schedule = schedule.model_copy(
                update={"entries": self._build_schedule_entries(provisional)}
            )
            return provisional.model_copy(
                update={
                    "schedule_entries": schedule.entries,
                }
            )

        response = self._mutate(
            workflow_path, expected_version=expected_version, operation=operation
        )
        assert schedule is not None
        return response, schedule

    def _evaluate(self, workspace: Workspace) -> tuple[set[str], dict[str, WaitingReason]]:
        satisfied: set[str] = set()
        reasons: dict[str, WaitingReason] = {}
        now = self._clock()
        for instance in workspace.task_instances:
            if instance.status in {"completed", "failed", "cancelled", "running"}:
                continue
            if instance.not_before is not None and now < instance.not_before:
                reasons[instance.id] = WaitingReason(
                    code="not_before_pending",
                    context={
                        "not_before": instance.not_before,
                        "remaining_ms": instance.not_before - now,
                    },
                    message=(
                        f"等待样品错峰启动时间：{instance.not_before}"
                    ),
                )
                continue
            template = self._template(workspace, instance.template_id)
            reason = self._evaluate_triggers(workspace, template.input_triggers)
            if reason is None:
                satisfied.add(instance.id)
            else:
                reasons[instance.id] = reason
        return satisfied, reasons

    def _evaluate_triggers(
        self, workspace: Workspace, triggers: Iterable[Trigger]
    ) -> WaitingReason | None:
        for trigger in triggers:
            result = self.conditions.evaluate(workspace.workflow_path, trigger)
            if not result.satisfied:
                return result.reason or WaitingReason(code="condition_unsatisfied")
        return None

    def _hydrate_conditions(self, workspace: Workspace) -> None:
        for state in workspace.opc_snapshots:
            self.conditions.restore_state(workspace.workflow_path, state.model_dump())

    @staticmethod
    def _registration(
        workspace: Workspace, plc_device_id: str
    ) -> PlcRegistration | None:
        return next(
            (
                item
                for item in workspace.plc_registrations
                if item.plc_device_id == plc_device_id
            ),
            None,
        )

    def _validate_template_conditions(
        self, workspace: Workspace, template: Template
    ) -> None:
        self._validate_triggers(workspace, template.input_triggers, "input")
        self._validate_triggers(workspace, template.output_triggers, "output")

    def _canonicalize_template_triggers(
        self, workspace: Workspace, template: Template
    ) -> Template:
        """将模板中的别名条件转换为可持久化的 CSV 真实节点名。"""
        return template.model_copy(
            update={
                "input_triggers": self._canonicalize_triggers(
                    workspace, template.input_triggers, "input"
                ),
                "output_triggers": self._canonicalize_triggers(
                    workspace, template.output_triggers, "output"
                ),
            }
        )

    def _canonicalize_triggers(
        self, workspace: Workspace, triggers: Iterable[Trigger], phase: str
    ) -> list[Trigger]:
        trigger_list = list(triggers)
        self._validate_triggers(workspace, trigger_list, phase)
        canonical_triggers: list[Trigger] = []
        for trigger in trigger_list:
            config = dict(trigger.config)
            registration = self._registration(
                workspace, str(config["plc_device_id"])
            )
            assert registration is not None
            config["variable"] = registration.canonical_variable_name(
                str(config["variable"])
            )
            canonical_triggers.append(trigger.model_copy(update={"config": config}))
        return canonical_triggers

    def _validate_triggers(
        self, workspace: Workspace, triggers: Iterable[Trigger], phase: str
    ) -> None:
        trigger_list = list(triggers)
        for trigger in trigger_list:
            config = trigger.config
            plc_device_id = str(config["plc_device_id"])
            variable = str(config["variable"])
            registration = self._registration(workspace, plc_device_id)
            canonical_variable = (
                registration.canonical_variable_name(variable)
                if registration is not None
                else variable
            )
            if registration is None or canonical_variable not in registration.variables:
                raise WorkspaceServiceError(
                    "unregistered_plc_variable",
                    f"PLC variable is not registered: {plc_device_id}.{variable}",
                )

    def _validate_snapshot_variables(
        self, workspace: Workspace, plc_device_id: str, variables: Iterable[str]
    ) -> None:
        registration = self._registration(workspace, plc_device_id)
        if registration is None:
            raise WorkspaceServiceError(
                "plc_not_registered",
                f"PLC runtime has not registered variables: {plc_device_id}",
            )
        unregistered = sorted(
            {
                registration.canonical_variable_name(variable)
                for variable in variables
            }
            - set(registration.variables)
        )
        if unregistered:
            raise WorkspaceServiceError(
                "unregistered_plc_variable",
                f"PLC snapshot contains unregistered variables: {', '.join(unregistered)}",
            )

    def _canonicalize_snapshot_values(
        self, workspace: Workspace, plc_device_id: str, values: dict
    ) -> dict:
        """在入库前将 runtime 别名键规整为 CSV 真实节点名。"""
        self._validate_snapshot_variables(workspace, plc_device_id, values.keys())
        registration = self._registration(workspace, plc_device_id)
        assert registration is not None
        canonical_values: dict = {}
        for variable, value in values.items():
            canonical_variable = registration.canonical_variable_name(str(variable))
            if (
                canonical_variable in canonical_values
                and canonical_values[canonical_variable] != value
            ):
                raise WorkspaceServiceError(
                    "ambiguous_plc_variable",
                    f"PLC snapshot contains conflicting values for: {canonical_variable}",
                )
            canonical_values[canonical_variable] = value
        return canonical_values

    def _schedule(
        self,
        workspace: Workspace,
        satisfied: set[str],
        condition_reasons: dict[str, WaitingReason],
    ) -> SchedulingResult:
        selected = self.policy.select(
            workspace.templates,
            workspace.task_instances,
            available_resources=set(),
            condition_satisfied_instance_ids=satisfied,
        )
        return SchedulingResult(
            startable_instance_ids=selected.startable_instance_ids,
            waiting_reasons={**condition_reasons, **selected.waiting_reasons},
        )

    def _build_schedule_entries(self, workspace: Workspace) -> list[TaskScheduleEntry]:
        """按稳定 FIFO 顺序估算每个 Task 的甘特区间。"""
        sample_available_at: dict[str, int] = {}
        anchor = self._clock()
        instances = sorted(
            workspace.task_instances,
            key=lambda item: (item.order, item.sample_id, item.id),
        )
        fixed_entries: dict[str, TaskScheduleEntry] = {}
        for instance in instances:
            template = self._template(workspace, instance.template_id)
            duration = max(15_000, len(template.node_ids) * 15_000)
            if instance.status == "completed":
                start_at = instance.started_at if instance.started_at is not None else 0
                end_at = instance.finished_at if instance.finished_at is not None else start_at + duration
                state = "done"
            elif instance.status == "running":
                start_at = instance.started_at if instance.started_at is not None else 0
                end_at = max(start_at + duration, anchor)
                state = "running"
            else:
                continue
            entry = TaskScheduleEntry(
                instance_id=instance.id,
                template_id=template.id,
                sample_id=instance.sample_id,
                start_at=start_at,
                end_at=end_at,
                resources=[],
                state=state,
            )
            fixed_entries[instance.id] = entry
            sample_available_at[instance.sample_id] = max(
                sample_available_at.get(instance.sample_id, anchor), end_at
            )
        entries: list[TaskScheduleEntry] = []
        for instance in instances:
            fixed = fixed_entries.get(instance.id)
            if fixed is not None:
                entries.append(fixed)
                continue
            template = self._template(workspace, instance.template_id)
            duration = max(15_000, len(template.node_ids) * 15_000)
            earliest_start = (
                instance.not_before
                if instance.not_before is not None
                else anchor
            )
            start_at = max(
                sample_available_at.get(instance.sample_id, anchor),
                earliest_start,
            )
            end_at = start_at + duration
            entry = TaskScheduleEntry(
                instance_id=instance.id,
                template_id=template.id,
                sample_id=instance.sample_id,
                start_at=start_at,
                end_at=end_at,
                resources=[],
                state="planned",
            )
            entries.append(entry)
            sample_available_at[instance.sample_id] = max(
                sample_available_at.get(instance.sample_id, anchor), end_at
            )
        return entries

    @staticmethod
    def _normalize_runtime_resources(workspace: Workspace) -> Workspace:
        """写事务统一丢弃旧 Task 动态资源租约。"""
        if not workspace.dynamic_resource_leases:
            return workspace
        return workspace.model_copy(update={"dynamic_resource_leases": []})

    @staticmethod
    def _execution_record(
        instance: TaskInstance, execution_id: str
    ) -> NodeExecutionRecord | None:
        return next(
            (
                record
                for record in instance.execution_state.records
                if record.execution_id == execution_id
            ),
            None,
        )

    @staticmethod
    def _require_active_execution(
        instance: TaskInstance, node_id: str, execution_id: str
    ) -> None:
        state = instance.execution_state
        if (
            instance.status != "running"
            or state.active_node_id != node_id
            or state.active_execution_id != execution_id
        ):
            raise WorkspaceServiceError(
                "action_not_active",
                "action must match the active execution",
            )

    @staticmethod
    def _replace_instance(
        workspace: Workspace, replacement: TaskInstance
    ) -> list[TaskInstance]:
        return [
            replacement if item.id == replacement.id else item
            for item in workspace.task_instances
        ]

    @staticmethod
    def _completion_events(
        *,
        instance: TaskInstance,
        template: Template,
        timestamp: int,
    ) -> list[WorkspaceEvent]:
        return [
            WorkspaceEvent(
                kind="completed",
                instance_id=instance.id,
                timestamp=timestamp,
                idempotency_key=f"instance/{instance.id}/completed",
            ),
            *[
                WorkspaceEvent(
                    kind="output",
                    instance_id=instance.id,
                    template_id=instance.template_id,
                    timestamp=timestamp,
                    idempotency_key=(
                        f"instance/{instance.id}/output/"
                        f"{json.dumps(trigger.model_dump(mode='json'), sort_keys=True)}"
                    ),
                    payload={
                        "trigger": trigger.model_dump(mode="json"),
                        "delivery": "recorded_without_opc_write",
                    },
                )
                for trigger in template.output_triggers
            ],
        ]

    @staticmethod
    def _template(workspace: Workspace, template_id: str) -> Template:
        for template in workspace.templates:
            if template.id == template_id:
                return template
        raise WorkspaceServiceError("template_not_found", f"template not found: {template_id}")

    @staticmethod
    def _instance(workspace: Workspace, instance_id: str) -> TaskInstance:
        for instance in workspace.task_instances:
            if instance.id == instance_id:
                return instance
        raise WorkspaceServiceError("instance_not_found", f"instance not found: {instance_id}")
