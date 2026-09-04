"""可替换的 Task 调度策略及其基础 FIFO 实现。"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from typing import Protocol

from .models import (
    POLICY_RESOURCE_PREFIX,
    POLICY_WORKSTATION_PREFIX,
    SchedulingResult,
    TaskInstance,
    Template,
    WaitingReason,
)


def policy_resource_key(resource: str) -> str:
    """兼容策略接口的普通资源输入，并保留显式内部键。"""
    if resource.startswith((POLICY_RESOURCE_PREFIX, POLICY_WORKSTATION_PREFIX)):
        return resource
    return f"{POLICY_RESOURCE_PREFIX}{resource}"


def constraint_resources(template: Template) -> set[str]:
    """保留旧策略接口，但 Task 层不再产生任何资源约束。"""
    del template
    return set()


class SchedulingPolicy(Protocol):
    """从条件已满足的候选中选择本轮可以启动的实例。"""

    def select(
        self,
        templates: Iterable[Template],
        instances: Iterable[TaskInstance],
        *,
        available_resources: Collection[str],
        condition_satisfied_instance_ids: Collection[str],
    ) -> SchedulingResult:
        """返回可启动实例和其余候选的等待原因。"""


class FifoResourcePolicy:
    """按 order、sample、instance id 排序的资源互斥 FIFO 策略。

    此策略只产生纯决策，不会变更实例状态或占用资源。调用方必须在同一
    workspace 锁内验证并应用该决策，确保选择与状态变更具备原子性。
    """

    def select(
        self,
        templates: Iterable[Template],
        instances: Iterable[TaskInstance],
        *,
        available_resources: Collection[str],
        condition_satisfied_instance_ids: Collection[str],
    ) -> SchedulingResult:
        template_by_id = {template.id: template for template in templates}
        all_instances = list(instances)
        satisfied_ids = set(condition_satisfied_instance_ids)
        del available_resources
        candidates = sorted(
            (
                instance
                for instance in all_instances
                if instance.id in satisfied_ids and instance.status == "pending"
            ),
            key=lambda instance: (instance.order, instance.sample_id, instance.id),
        )
        startable_instance_ids: list[str] = []
        waiting_reasons: dict[str, WaitingReason] = {}

        for instance in candidates:
            template = template_by_id.get(instance.template_id)
            if template is None:
                waiting_reasons[instance.id] = WaitingReason(
                    code="template_missing",
                    context={"template_id": instance.template_id},
                    message=f"模板不存在：{instance.template_id}",
                )
                continue

            dependency_reason = self._dependency_waiting_reason(
                instance,
                template,
                template_by_id,
                all_instances,
            )
            if dependency_reason is not None:
                waiting_reasons[instance.id] = dependency_reason
                continue

            startable_instance_ids.append(instance.id)

        return SchedulingResult(
            startable_instance_ids=startable_instance_ids,
            waiting_reasons=waiting_reasons,
        )

    @classmethod
    def _dependency_waiting_reason(
        cls,
        instance: TaskInstance,
        template: Template,
        template_by_id: dict[str, Template],
        all_instances: Iterable[TaskInstance],
    ) -> WaitingReason | None:
        if template.dependencies is None:
            predecessor_order = cls._unfinished_predecessor_order(
                instance,
                all_instances,
            )
            if predecessor_order is None:
                return None
            return WaitingReason(
                code="sample_order_pending",
                context={
                    "sample_id": instance.sample_id,
                    "predecessor_order": predecessor_order,
                },
                message=(
                    f"等待样品 {instance.sample_id} 的 order "
                    f"{predecessor_order} 完成"
                ),
            )

        for dependency in template.dependencies:
            dependency_template = template_by_id.get(dependency.template_id)
            if dependency_template is None:
                return WaitingReason(
                    code="task_dependency_template_missing",
                    context={
                        "dependency_template_id": dependency.template_id,
                    },
                    message=f"前置 Task 模板不存在：{dependency.template_id}",
                )
            if (
                dependency.node_id is not None
                and dependency.node_id not in dependency_template.node_ids
            ):
                return WaitingReason(
                    code="task_dependency_node_missing",
                    context={
                        "dependency_template_id": dependency.template_id,
                        "dependency_node_id": dependency.node_id,
                    },
                    message=(
                        f"前置 Task {dependency.template_id} 不包含动作 "
                        f"{dependency.node_id}"
                    ),
                )

            predecessor = cls._latest_predecessor_instance(
                instance,
                dependency.template_id,
                all_instances,
            )
            if predecessor is None:
                return WaitingReason(
                    code="task_dependency_instance_missing",
                    context={
                        "sample_id": instance.sample_id,
                        "dependency_template_id": dependency.template_id,
                    },
                    message=(
                        f"样品 {instance.sample_id} 缺少前置 Task "
                        f"{dependency.template_id}"
                    ),
                )

            if dependency.node_id is None:
                if predecessor.status == "completed":
                    continue
                return WaitingReason(
                    code="task_dependency_pending",
                    context={
                        "dependency_instance_id": predecessor.id,
                        "dependency_template_id": dependency.template_id,
                        "dependency_status": predecessor.status,
                    },
                    message=f"等待前置 Task {dependency.template_id} 完成",
                )

            node_index = dependency_template.node_ids.index(dependency.node_id)
            if predecessor.execution_state.cursor > node_index:
                continue
            return WaitingReason(
                code="task_dependency_node_pending",
                context={
                    "dependency_instance_id": predecessor.id,
                    "dependency_template_id": dependency.template_id,
                    "dependency_node_id": dependency.node_id,
                },
                message=(
                    f"等待前置 Task {dependency.template_id} 的动作 "
                    f"{dependency.node_id} 完成"
                ),
            )
        return None

    @staticmethod
    def _latest_predecessor_instance(
        instance: TaskInstance,
        dependency_template_id: str,
        all_instances: Iterable[TaskInstance],
    ) -> TaskInstance | None:
        matching = [
            other
            for other in all_instances
            if other.sample_id == instance.sample_id
            and other.template_id == dependency_template_id
            and other.order < instance.order
        ]
        return max(matching, key=lambda other: other.order, default=None)

    @staticmethod
    def _unfinished_predecessor_order(
        instance: TaskInstance,
        all_instances: Iterable[TaskInstance],
    ) -> int | None:
        pending_orders = sorted(
            other.order
            for other in all_instances
            if other.sample_id == instance.sample_id
            and other.order < instance.order
            and other.status != "completed"
        )
        return pending_orders[0] if pending_orders else None
