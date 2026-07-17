"""Task 工作区的生命周期与原子调度业务服务。"""

from __future__ import annotations

from collections.abc import Callable, Iterable
import json
import threading
import time
from uuid import uuid4

from .conditions import OpcConditionProvider
from .models import (
    POLICY_RESOURCE_PREFIX,
    POLICY_WORKSTATION_PREFIX,
    SchedulingResult,
    OpcSnapshotState,
    TaskScheduleEntry,
    TaskInstance,
    Template,
    Trigger,
    WaitingReason,
    Workspace,
    WorkspaceEvent,
)
from .policy import FifoResourcePolicy, SchedulingPolicy, constraint_resources
from .store import VersionConflictError, WorkspaceStore


class WorkspaceServiceError(ValueError):
    """业务规则不满足。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


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

    def create_template(
        self, workflow_path: str, expected_version: int, template: Template
    ):
        def operation(workspace: Workspace) -> Workspace:
            if any(item.id == template.id for item in workspace.templates):
                raise WorkspaceServiceError("template_exists", "template already exists")
            item = template.model_copy(update={"workflow_path": workspace.workflow_path})
            return workspace.model_copy(update={"templates": [*workspace.templates, item]})

        return self.store.mutate(
            workflow_path, expected_version=expected_version, operation=operation
        )

    def update_template(
        self,
        workflow_path: str,
        expected_version: int,
        template_id: str,
        *,
        name: str | None = None,
        trigger: Trigger | None = None,
        input_triggers: list[Trigger] | None = None,
        output_triggers: list[Trigger] | None = None,
    ):
        def operation(workspace: Workspace) -> Workspace:
            template = self._template(workspace, template_id)
            updates = {}
            if name is not None:
                if not name.strip():
                    raise WorkspaceServiceError("invalid_template_name", "template name is required")
                updates["name"] = name.strip()
            if trigger is not None:
                updates["trigger"] = trigger
            if input_triggers is not None:
                updates["input_triggers"] = input_triggers
            if output_triggers is not None:
                updates["output_triggers"] = output_triggers
            replacement = template.model_copy(update=updates)
            return workspace.model_copy(
                update={
                    "templates": [
                        replacement if item.id == template_id else item
                        for item in workspace.templates
                    ]
                }
            )

        return self.store.mutate(
            workflow_path, expected_version=expected_version, operation=operation
        )

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

        return self.store.mutate(
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

        return self.store.mutate(
            workflow_path, expected_version=expected_version, operation=operation
        )

    def generate_instances(
        self,
        workflow_path: str,
        expected_version: int,
        template_ids: list[str],
        sample_ids: list[str],
    ):
        def operation(workspace: Workspace) -> Workspace:
            templates = [self._template(workspace, item) for item in template_ids]
            generated: list[TaskInstance] = []
            for sample_id in sample_ids:
                if not sample_id:
                    raise WorkspaceServiceError("invalid_sample_id", "sample_id is required")
                next_order = max(
                    (item.order for item in workspace.task_instances if item.sample_id == sample_id),
                    default=-1,
                ) + 1
                for template in templates:
                    generated.append(
                        TaskInstance(
                            id=uuid4().hex,
                            template_id=template.id,
                            status="waiting",
                            sample_id=sample_id,
                            order=next_order,
                        )
                    )
                    next_order += 1
            return workspace.model_copy(
                update={"task_instances": [*workspace.task_instances, *generated]}
            )

        return self.store.mutate(
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

        return self.store.mutate(
            workflow_path, expected_version=expected_version, operation=operation
        )

    def push_opc_snapshot(
        self,
        workflow_path: str,
        expected_version: int,
        provider_id: str,
        sequence: int,
        values: dict,
    ):
        canonical_path = self.store.get(workflow_path).workspace.workflow_path
        with self._opc_lock:
            current_workspace = self.store.get(canonical_path).workspace
            self._hydrate_conditions(current_workspace)
            previous = self.conditions.export_state(canonical_path, provider_id)
            accepted = self.conditions.can_update(
                canonical_path, provider_id, sequence
            )
            if not accepted:
                return self.store.get(canonical_path), False
            if accepted:
                self.conditions.update(canonical_path, provider_id, sequence, values)
            snapshot_state = self.conditions.export_state(canonical_path, provider_id)

            def operation(workspace: Workspace) -> Workspace:
                event = WorkspaceEvent(
                    kind="opc_snapshot",
                    timestamp=self._clock(),
                    idempotency_key=f"{workspace.workflow_path}/{provider_id}/{sequence}",
                    payload={
                        "provider_id": provider_id,
                        "sequence": sequence,
                        "accepted": accepted,
                        "variable_count": len(values),
                    },
                )
                snapshots = [
                    item for item in workspace.opc_snapshots if item.provider_id != provider_id
                ]
                if snapshot_state is not None:
                    snapshots.append(OpcSnapshotState.model_validate(snapshot_state))
                return workspace.model_copy(
                    update={"events": [*workspace.events, event], "opc_snapshots": snapshots}
                )

            try:
                response = self.store.mutate(
                    canonical_path, expected_version=expected_version, operation=operation
                )
            except Exception:
                if previous is None:
                    self.conditions.clear_state(canonical_path, provider_id)
                else:
                    self.conditions.restore_state(canonical_path, previous)
                raise
            return response, accepted

    def plan(self, workflow_path: str, expected_version: int, *, paused: bool | None = None):
        schedule: SchedulingResult | None = None

        def operation(workspace: Workspace) -> Workspace:
            nonlocal schedule
            self._hydrate_conditions(workspace)
            paused_value = workspace.scheduler_paused if paused is None else paused
            evaluated, reasons = self._evaluate(workspace)
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
                for item in workspace.task_instances
            ]
            provisional = workspace.model_copy(
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

        response = self.store.mutate(
            workflow_path, expected_version=expected_version, operation=operation
        )
        assert schedule is not None
        return response, schedule

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
            events = list(workspace.events)
            instances = []
            for item in workspace.task_instances:
                if item.id in completed_ids:
                    instances.append(
                        item.model_copy(
                            update={"status": "completed", "finished_at": transition_time}
                        )
                    )
                    events.append(
                        WorkspaceEvent(
                            kind="completed",
                            instance_id=item.id,
                            timestamp=transition_time,
                            idempotency_key=f"instance/{item.id}/completed",
                        )
                    )
                    template = self._template(workspace, item.template_id)
                    output_triggers = template.output_triggers or [
                        Trigger(kind="internal", config={})
                    ]
                    events.extend(
                        WorkspaceEvent(
                            kind="output",
                            instance_id=item.id,
                            template_id=item.template_id,
                            timestamp=transition_time,
                            idempotency_key=(
                                f"instance/{item.id}/output/"
                                f"{json.dumps(output_trigger.model_dump(mode='json'), sort_keys=True)}"
                            ),
                            payload={
                                "trigger": output_trigger.model_dump(mode="json"),
                                "delivery": "recorded_without_opc_write",
                            },
                        )
                        for output_trigger in output_triggers
                    )
                else:
                    instances.append(item)
            provisional = workspace.model_copy(
                update={"task_instances": instances, "events": events}
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
                                            for trigger in (
                                                [
                                                    *self._template(
                                                        provisional,
                                                        self._instance(provisional, item_id).template_id,
                                                    ).input_triggers,
                                                    *(
                                                        [
                                                            self._template(
                                                                provisional,
                                                                self._instance(provisional, item_id).template_id,
                                                            ).trigger
                                                        ]
                                                        if self._template(
                                                            provisional,
                                                            self._instance(provisional, item_id).template_id,
                                                        ).trigger is not None
                                                        else []
                                                    ),
                                                ]
                                            )
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

        response = self.store.mutate(
            workflow_path, expected_version=expected_version, operation=operation
        )
        assert schedule is not None
        return response, schedule

    def _evaluate(self, workspace: Workspace) -> tuple[set[str], dict[str, WaitingReason]]:
        satisfied: set[str] = set()
        reasons: dict[str, WaitingReason] = {}
        for instance in workspace.task_instances:
            if instance.status in {"completed", "failed", "cancelled", "running"}:
                continue
            template = self._template(workspace, instance.template_id)
            triggers = [*template.input_triggers]
            if template.trigger is not None:
                triggers.append(template.trigger)
            for trigger in triggers:
                result = self._evaluate_trigger(workspace, trigger)
                if not result.satisfied:
                    reasons[instance.id] = result.reason or WaitingReason(
                        code="condition_unsatisfied"
                    )
                    break
            else:
                satisfied.add(instance.id)
        return satisfied, reasons

    def _hydrate_conditions(self, workspace: Workspace) -> None:
        for state in workspace.opc_snapshots:
            self.conditions.restore_state(workspace.workflow_path, state.model_dump())

    def _evaluate_trigger(self, workspace: Workspace, trigger):
        if trigger.kind.lower() == "opc":
            return self.conditions.evaluate(workspace.workflow_path, trigger)
        system_constraint = self._system_constraint(trigger)
        if system_constraint is not None:
            kind, identifier = system_constraint
            occupied = any(
                (
                    f"{POLICY_RESOURCE_PREFIX}{identifier}"
                    if kind == "resource"
                    else f"{POLICY_WORKSTATION_PREFIX}{identifier}"
                )
                in constraint_resources(
                    self._template(workspace, instance.template_id)
                )
                for instance in workspace.task_instances
                if instance.status == "running"
            )
            from .models import ConditionResult

            return ConditionResult(
                satisfied=not occupied,
                reason=None
                if not occupied
                else WaitingReason(
                    code=f"{kind}_unavailable",
                    context={kind: identifier},
                    message=f"{kind} 不可用：{identifier}",
                ),
            )
        if trigger.kind.lower() in {"resource", "workstation", "internal", "manual"}:
            from .models import ConditionResult

            if "key" in trigger.config:
                expected = trigger.config.get("value")
                satisfied = any(
                    event.kind == "output"
                    and event.payload.get("trigger", {}).get("config", {}).get("key")
                    == trigger.config["key"]
                    and event.payload.get("trigger", {}).get("config", {}).get("value")
                    == expected
                    for event in workspace.events
                )
                return ConditionResult(
                    satisfied=satisfied,
                    reason=None
                    if satisfied
                    else WaitingReason(
                        code=f"{trigger.kind}_condition_unsatisfied",
                        context=dict(trigger.config),
                    ),
                )
            available = trigger.config.get("available", True)
            return ConditionResult(
                satisfied=available is True,
                reason=None
                if available is True
                else WaitingReason(
                    code=f"{trigger.kind}_condition_unsatisfied",
                    context=dict(trigger.config),
                ),
            )
        from .models import ConditionResult

        return ConditionResult(
            satisfied=False,
            reason=WaitingReason(
                code="trigger_kind_unsupported", context={"kind": trigger.kind}
            ),
        )

    @staticmethod
    def _system_constraint(trigger: Trigger) -> tuple[str, str] | None:
        """将系统资源/工位约束与旧版自动派生 internal key 统一为实时约束。"""
        kind = trigger.kind.lower()
        if kind == "resource" and trigger.config.get("resource"):
            return "resource", str(trigger.config["resource"])
        if kind == "workstation" and trigger.config.get("workstation"):
            return "workstation", str(trigger.config["workstation"])
        if kind != "internal":
            return None
        key = str(trigger.config.get("key", ""))
        if key.startswith("资源锁可获取："):
            return "resource", key.removeprefix("资源锁可获取：")
        if key.startswith("工位条件满足："):
            return "workstation", key.removeprefix("工位条件满足：")
        return None

    def _schedule(
        self,
        workspace: Workspace,
        satisfied: set[str],
        condition_reasons: dict[str, WaitingReason],
    ) -> SchedulingResult:
        all_resources = {
            resource
            for template in workspace.templates
            for resource in constraint_resources(template)
        }
        running_resources = {
            resource
            for instance in workspace.task_instances
            if instance.status == "running"
            for resource in constraint_resources(
                self._template(workspace, instance.template_id)
            )
        }
        selected = self.policy.select(
            workspace.templates,
            workspace.task_instances,
            available_resources=all_resources - running_resources,
            condition_satisfied_instance_ids=satisfied,
        )
        return SchedulingResult(
            startable_instance_ids=selected.startable_instance_ids,
            waiting_reasons={**condition_reasons, **selected.waiting_reasons},
        )

    def _build_schedule_entries(self, workspace: Workspace) -> list[TaskScheduleEntry]:
        """按稳定 FIFO 顺序估算每个 Task 的甘特区间。"""
        resource_available_at: dict[str, int] = {}
        sample_available_at: dict[str, int] = {}
        anchor = self._clock()
        instances = sorted(
            workspace.task_instances,
            key=lambda item: (item.order, item.sample_id, item.id),
        )
        fixed_entries: dict[str, TaskScheduleEntry] = {}
        for instance in instances:
            template = self._template(workspace, instance.template_id)
            display_resources = self._display_schedule_resources(template)
            duration = max(15_000, len(template.node_ids) * 15_000)
            if instance.status == "completed":
                start_at = instance.started_at if instance.started_at is not None else 0
                end_at = instance.finished_at if instance.finished_at is not None else start_at + duration
                state = "done"
            elif instance.status == "running":
                start_at = instance.started_at if instance.started_at is not None else 0
                end_at = start_at + duration
                state = "running"
            else:
                continue
            entry = TaskScheduleEntry(
                instance_id=instance.id,
                template_id=template.id,
                sample_id=instance.sample_id,
                start_at=start_at,
                end_at=end_at,
                resources=display_resources,
                state=state,
            )
            fixed_entries[instance.id] = entry
            sample_available_at[instance.sample_id] = max(
                sample_available_at.get(instance.sample_id, anchor), end_at
            )
            for resource in display_resources:
                resource_available_at[resource] = max(
                    resource_available_at.get(resource, anchor), end_at
                )
        entries: list[TaskScheduleEntry] = []
        for instance in instances:
            fixed = fixed_entries.get(instance.id)
            if fixed is not None:
                entries.append(fixed)
                continue
            template = self._template(workspace, instance.template_id)
            display_resources = self._display_schedule_resources(template)
            duration = max(15_000, len(template.node_ids) * 15_000)
            resource_ready_at = max(
                (resource_available_at.get(resource, anchor) for resource in display_resources),
                default=anchor,
            )
            start_at = max(
                sample_available_at.get(instance.sample_id, anchor), resource_ready_at
            )
            end_at = start_at + duration
            entry = TaskScheduleEntry(
                instance_id=instance.id,
                template_id=template.id,
                sample_id=instance.sample_id,
                start_at=start_at,
                end_at=end_at,
                resources=display_resources,
                state="planned",
            )
            entries.append(entry)
            sample_available_at[instance.sample_id] = max(
                sample_available_at.get(instance.sample_id, anchor), end_at
            )
            for resource in display_resources:
                resource_available_at[resource] = max(
                    resource_available_at.get(resource, anchor), end_at
                )
        return entries

    @staticmethod
    def _display_schedule_resources(template: Template) -> list[str]:
        """将有效系统约束投影为公开甘特泳道 ID，绝不泄露策略命名空间。"""
        resources = list(dict.fromkeys(template.resources))
        triggers = [*template.input_triggers]
        if template.trigger is not None:
            triggers.append(template.trigger)
        for trigger in triggers:
            kind = trigger.kind.lower()
            identifier = (
                trigger.config.get("workstation")
                if kind == "workstation"
                else trigger.config.get("resource")
                if kind == "resource"
                else None
            )
            if identifier and str(identifier) not in resources:
                resources.append(str(identifier))
        return resources

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
