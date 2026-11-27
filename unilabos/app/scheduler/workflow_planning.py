"""把现有工作流运行态只读转换为时间规划视图。

本模块不判断条件、不物化循环、不批准人工确认，也不创建执行权限。调用方须在
调度锁内、现有控制推进完成后读取；所得节点是深拷贝，可交后台规划器使用。
未知条件、人工等待和未结束循环后的步骤不能被预测成确定执行路径。
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Collection, Mapping
from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING

from unilabos.app.scheduler.models import WorkflowNode
from unilabos.workflow.resource_lock_key import (
    device_lock_key,
    material_lock_key,
    require_canonical_resource_lock_key,
)
from unilabos.workflow.resource_lock_plan import deserialize_resource_plan

if TYPE_CHECKING:
    from unilabos.app.scheduler.dag_state import WorkflowRun


_FINISHED = frozenset({"success", "skipped", "failed", "canceled", "timeout"})
_SUCCESSFUL = frozenset({"success", "skipped"})
_CONTROL_KINDS = frozenset({"condition", "repeat_until"})


@dataclass(frozen=True, slots=True)
class PlanningResourceInterval:
    """规划中的连续资源区间；held 只来自调用方提供的实际持有记录。

    release_known 仅表示退出动作已进入本轮可预测路径，不是实际释放证据，也不
    表示其耗时已经有可信估计。规划器仍须检查时长；未知退出保持开放占用。
    """

    interval_id: str
    resource_keys: tuple[str, ...]
    member_node_ids: tuple[str, ...]
    acquire_node_id: str
    release_node_id: str
    held: bool
    release_known: bool
    explicit_boundary: bool


@dataclass(frozen=True, slots=True)
class PlanningInformationPrefix:
    """产生下一项控制所需信息的可见前缀；不表示整个任务完成。"""

    prefix_id: str
    target_node_ids: tuple[str, ...]
    member_node_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WorkflowPlanningView:
    """同一锁内取得的规划输入；所有节点身份都是当前实际运行实例身份。"""

    workflow_id: str
    nodes: tuple[WorkflowNode, ...]
    dependencies: Mapping[str, tuple[str, ...]]
    node_states: Mapping[str, str]
    blocked: Mapping[str, tuple[str, ...]]
    full_tail_known: bool
    continuous_intervals: tuple[PlanningResourceInterval, ...]
    manual_gate_node_ids: tuple[str, ...]
    information_prefixes: tuple[PlanningInformationPrefix, ...] = ()


def build_workflow_planning_view(
    run: WorkflowRun,
    *,
    approved_manual_node_ids: Collection[str] = (),
    held_interval_keys: Mapping[str, Collection[str]] | None = None,
    step_target_node_id: str | None = None,
    plan_paused_steps: bool = False,
) -> WorkflowPlanningView:
    """复制当前已物化且路径确定的未完成节点，不改变原运行态。

    参数：run 为当前工作流；人工批准集合必须来自正式确认事实；持有表应合并
    连续持锁记录与在途 Job 的区间引用；单步目标必须来自已有单步许可。
    返回：候选节点、依赖、阻塞原因及连续区间。人工等待节点可出现在
    manual_gate_node_ids 中，供原流程登记请求，但不能据此给其后继承诺时间。
    异常：重复实例身份或无效资源计划抛 ValueError，不能猜测或遗漏资源。
    """

    approved = set(approved_manual_node_ids)
    held = {identity: set(keys) for identity, keys in (held_interval_keys or {}).items()}
    nodes: dict[str, WorkflowNode] = {}
    states: dict[str, str] = {}
    pending: dict[str, tuple[str, ...]] = {}
    children: dict[str, set[str]] = defaultdict(set)
    round_exits: dict[str, tuple[str, ...]] = {}

    def collect(current: WorkflowRun) -> None:
        # snapshot 不推进 ready 状态；循环子图尚无公开遍历接口，私有读取只集中于此。
        snapshot = current.snapshot()
        for node_id, item in snapshot["nodes"].items():
            if node_id in nodes:
                raise ValueError(f"规划视图出现重复节点实例：{node_id}")
            node = current.node(node_id)
            if node is None:
                raise ValueError(f"规划快照缺少节点定义：{node_id}")
            nodes[node_id] = node
            states[node_id] = str(item["state"])
            pending[node_id] = tuple(item["pending_parents"])
        for edge in snapshot["edges"]:
            children[str(edge["source"])].add(str(edge["target"]))
        for control_id, runtime in current._repeat_runtime.items():
            if runtime.round_run is not None:
                body = runtime.round_run.snapshot()
                body_ids = set(body["nodes"])
                non_exits = {str(edge["source"]) for edge in body["edges"]
                             if str(edge["target"]) in body_ids}
                round_exits[control_id] = tuple(sorted(body_ids - non_exits))
                collect(runtime.round_run)

    collect(run)
    blocked: dict[str, set[str]] = defaultdict(set)
    boundaries: dict[str, str] = {}
    for node_id, node in nodes.items():
        state = states[node_id]
        if state in _SUCCESSFUL:
            continue
        if state in {"failed", "canceled", "timeout", "error_waiting"}:
            boundaries[node_id] = f"predecessor_not_successful:{node_id}"
        elif node.executor_kind == "condition":
            boundaries[node_id] = f"condition_unresolved:{node_id}"
        elif node.executor_kind == "repeat_until":
            boundaries[node_id] = f"loop_tail_unknown:{node_id}"
        elif node.is_manual_confirm() and node_id not in approved:
            boundaries[node_id] = f"human_confirmation_required:{node_id}"

    # 仅沿现有实例图传播边界。已物化的当前循环体有自己的图，不展开未来轮次。
    for source, reason in boundaries.items():
        queue = deque(children[source])
        seen: set[str] = set()
        while queue:
            node_id = queue.popleft()
            if node_id in seen:
                continue
            seen.add(node_id)
            if states.get(node_id) not in _FINISHED:
                blocked[node_id].add(reason)
            queue.extend(children[node_id])

    preview_step = plan_paused_steps and run.execution_mode == "step" and run.state.value in {"running", "paused"}
    state_allows_work = run.state.value == "running" or preview_step
    gate_open = run.dispatch_gate == "active" or preview_step
    manual_gates: list[str] = []
    for node_id, node in nodes.items():
        state = states[node_id]
        if state in _FINISHED:
            continue
        permitted = state_allows_work and (gate_open or node_id == step_target_node_id)
        if not permitted and state != "dispatched":
            blocked[node_id].add("workflow_dispatch_paused")
        if (
            node.is_manual_confirm()
            and node_id not in approved
            and state in {"pending", "ready"}
            and not pending[node_id]
            and not blocked[node_id]
            and permitted
        ):
            manual_gates.append(node_id)
        if node_id in boundaries:
            blocked[node_id].add(boundaries[node_id])

    visible = {
        node_id
        for node_id, node in nodes.items()
        if states[node_id] not in _FINISHED
        and node.executor_kind not in _CONTROL_KINDS
        and not blocked[node_id]
    }
    # 继承来自被阻塞节点的依赖，防止不完整/过时图把后继泄漏为根节点。
    changed = True
    while changed:
        changed = False
        for node_id in tuple(visible):
            unavailable = [
                parent
                for parent in pending[node_id]
                if states.get(parent) not in _SUCCESSFUL and parent not in visible
            ]
            if unavailable:
                blocked[node_id].update(f"dependency_unavailable:{parent}" for parent in unavailable)
                visible.remove(node_id)
                changed = True

    intervals = _resource_intervals(run, visible, nodes, held, states)
    unresolved = any(
        states[node_id] not in _FINISHED and (node_id in boundaries or blocked[node_id])
        for node_id in nodes
    )
    prefixes: list[PlanningInformationPrefix] = []
    seen_targets: set[tuple[str, ...]] = set()
    for source in sorted(boundaries):
        if (nodes[source].executor_kind not in _CONTROL_KINDS
                and not nodes[source].is_manual_confirm()):
            continue
        frontier: set[str] = set()
        seen: set[str] = set()
        queue = deque(round_exits.get(source, pending[source]))
        while queue:
            current_id = queue.popleft()
            if current_id in seen or states.get(current_id) in _SUCCESSFUL:
                continue
            seen.add(current_id)
            if current_id in visible:
                frontier.add(current_id)
            else:
                queue.extend(round_exits.get(current_id, pending.get(current_id, ())))
        targets = tuple(sorted(frontier))
        if not targets or targets in seen_targets:
            continue
        members = set(frontier)
        queue = deque(frontier)
        while queue:
            for parent in pending[queue.popleft()]:
                if parent in visible and parent not in members:
                    members.add(parent)
                    queue.append(parent)
        seen_targets.add(targets)
        prefixes.append(PlanningInformationPrefix(source, targets, tuple(sorted(members))))
    return WorkflowPlanningView(
        workflow_id=run.spec.workflow_id,
        nodes=tuple(deepcopy(nodes[node_id]) for node_id in sorted(visible)),
        dependencies=MappingProxyType({
            node_id: tuple(parent for parent in pending[node_id] if parent in visible)
            for node_id in sorted(visible)
        }),
        node_states=MappingProxyType(dict(sorted(states.items()))),
        blocked=MappingProxyType({
            node_id: tuple(sorted(reasons))
            for node_id, reasons in sorted(blocked.items()) if reasons
        }),
        full_tail_known=not unresolved,
        continuous_intervals=intervals,
        manual_gate_node_ids=tuple(sorted(manual_gates)),
        information_prefixes=tuple(prefixes),
    )


def _resource_intervals(
    run: WorkflowRun,
    visible: set[str],
    nodes: Mapping[str, WorkflowNode],
    held: Mapping[str, set[str]],
    states: Mapping[str, str],
) -> tuple[PlanningResourceInterval, ...]:
    """把冻结资源区间映射到当前轮实例，保留未找到释放点的实际持有。

    冻结计划的释放点是区间链按拓扑序的最后一个成员。区间以条件分支收尾时，
    该成员可能落在未选中的分支里并被跳过；运行时按"全部成员完成"释放，
    规划视图须同样以剩余成员的最后一个作为有效释放点，否则区间被当作
    开放占用延伸到排程视界末端，后续同资源动作无解。
    """

    result: list[PlanningResourceInterval] = []
    handled: set[str] = set()
    if run.spec.resource_plan:
        plan = deserialize_resource_plan(run.spec.resource_plan)
        resources = {item.resource_id: item for item in plan.resources}
        for interval in plan.intervals:
            resource = resources.get(interval.resource_id)
            if resource is None:
                raise ValueError(f"资源区间缺少资源定义：{interval.interval_id}")
            canonical_key = resource.canonical_key
            kind = resource.kind.strip().lower()
            if not canonical_key.startswith("/") and resource.instance_uuid:
                if kind in {"device", "motion", "tool", "robot", "rail"}:
                    canonical_key = device_lock_key(resource.instance_uuid)
                elif kind in {"material", "container", "sample"}:
                    canonical_key = material_lock_key(resource.instance_uuid)
            canonical_key = str(require_canonical_resource_lock_key(canonical_key))
            members = tuple(dict.fromkeys(
                run.resource_runtime_node_uuid(item) for item in interval.node_uuids
            ))
            acquire = run.resource_runtime_node_uuid(interval.acquire_node_uuid)
            release = run.resource_runtime_node_uuid(interval.release_node_uuid)
            actual_keys = held.get(interval.interval_id, set())
            if not actual_keys and not any(item in visible for item in members):
                continue
            keys = {canonical_key, *actual_keys}
            for key in keys:
                require_canonical_resource_lock_key(key)
            release_known = release in visible
            if not release_known and states.get(release) == "skipped":
                remaining = [
                    item for item in members
                    if item in nodes and states.get(item) not in _FINISHED
                ]
                # 静态释放点所在分支未被选中：剩余成员全部可见时，退出点即其中拓扑序最后者。
                if remaining and all(item in visible for item in remaining):
                    release = remaining[-1]
                    release_known = True
            result.append(PlanningResourceInterval(
                interval_id=interval.interval_id,
                resource_keys=tuple(sorted(keys)),
                member_node_ids=tuple(item for item in members if item in nodes),
                acquire_node_id=acquire,
                release_node_id=release,
                held=bool(actual_keys),
                release_known=release_known,
                explicit_boundary=interval.explicit_boundary,
            ))
            handled.add(interval.interval_id)
    # 计划已归档、节点已退出或旧版本区间仍有物理持有时，不能遗漏这部分资源。
    for interval_id, keys in sorted(held.items()):
        if interval_id in handled or not keys:
            continue
        for key in keys:
            require_canonical_resource_lock_key(key)
        result.append(PlanningResourceInterval(
            interval_id=interval_id,
            resource_keys=tuple(sorted(keys)),
            member_node_ids=(),
            acquire_node_id="",
            release_node_id="",
            held=True,
            release_known=False,
            explicit_boundary=True,
        ))
    return tuple(result)


__all__ = [
    "PlanningInformationPrefix",
    "PlanningResourceInterval",
    "WorkflowPlanningView",
    "build_workflow_planning_view",
]
