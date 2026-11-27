"""单个工作流的 DAG 状态机，对齐 Go dagEngine 的图推进语义。

对应关系（snapshot dag.go）：

- ``build``            ↔ buildTask（依赖表 + 传参边 + 环检测）
- ``ready_nodes``      ↔ canRunNodes（入度 0 且未消费）
- ``mark_finished``    ↔ clearFinishedNode（从依赖表中删除完成节点）
- ``resolve_params``   ↔ parsePreNodeParam（gjson/sjson + ``@@@``）

差异：Go 用协程 + callbackChan 串行推进；Edge 版把「取 ready → 排序 → 下发」交给
service 层在每次触发点统一执行，本类只维护图状态，无并发副作用。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set
from uuid import NAMESPACE_URL, UUID, uuid5

from unilabos.app.scheduler.models import (
    DATA_KEY_SPLIT,
    Handle,
    HandlePair,
    NodeState,
    RepeatUntilRegion,
    WorkflowEdge,
    WorkflowNode,
    WorkflowSpec,
    WorkflowState,
)
from unilabos.app.scheduler.param_resolver import (
    ParamResolveError,
    json_get_exists,
    json_set,
    resolve_parent_params,
)
from unilabos.workflow.control_expression import (
    ConditionEvaluationError,
    evaluate_condition_expression,
)


class WorkflowCycleError(Exception):
    """对齐 Go code.WorkflowHasCircularErr。"""


@dataclass
class _RepeatRuntime:
    """单个 RepeatUntil 区域的进程内逐轮状态。"""

    iteration_index: int = -1
    carry: dict[str, Any] | None = None
    round_run: "WorkflowRun | None" = None
    runtime_to_template: dict[str, str] | None = None


class WorkflowRun:
    """一个已提交工作流的运行态。"""

    def __init__(
        self,
        spec: WorkflowSpec,
        *,
        execution_mode: str | None = None,
        dispatch_gate: str | None = None,
    ):
        self.spec = spec
        # ``run_mode`` 是冻结的创建事实；运行期间的自动/单步切换只修改
        # ``execution_mode`` 与 ``dispatch_gate``，不能反向篡改 ExecutionPlan。
        self.execution_mode = execution_mode or (
            "step" if spec.run_mode == "step" else "normal"
        )
        if self.execution_mode not in {"normal", "switching_to_step", "step"}:
            raise ValueError(f"invalid workflow execution mode: {self.execution_mode}")
        self.dispatch_gate = dispatch_gate or (
            "paused"
            if self.execution_mode in {"switching_to_step", "step"}
            else "active"
        )
        if self.dispatch_gate not in {"active", "paused"}:
            raise ValueError(f"invalid workflow dispatch gate: {self.dispatch_gate}")
        # 单步任务和 normal→step 排空态必须停在首个节点之前；普通任务暂停时
        # 保持 RUNNING/WAITING_MATERIAL，由独立闸门阻止新的 Job 派发。
        self.state = (
            WorkflowState.PAUSED
            if self.execution_mode in {"switching_to_step", "step"}
            else WorkflowState.RUNNING
        )

        self._nodes: Dict[str, WorkflowNode] = {}
        # node_id -> 未完成父节点集合（Go d.dependencies 等价，入度表）
        self._pending_parents: Dict[str, Set[str]] = {}
        # 已消费（已进入 ready 下发流程）的节点（Go d.consumedNode 等价）
        self._consumed: Set[str] = set()
        self._node_states: Dict[str, NodeState] = {}
        # node_id -> 传参边（Go d.nodeParentPairs 等价）
        self._parent_pairs: Dict[str, List[HandlePair]] = {}
        # node_id -> 执行返回值（Go nodeMap[..].ReturnInfo.ReturnValue 等价）
        self._ret_values: Dict[str, Any] = {}
        # 仅保存执行合同明确为 ResourceSlot 同节点透传的预测值。它们不参与
        # 条件求值，也不冒充设备返回值；真实回执写入 _ret_values 后优先覆盖。
        self._static_resource_values: Dict[str, Any] = {}
        # 取消意图与业务终态分离；在途节点返回成功/失败也不能抹掉用户取消事实。
        self._cancel_requested = False
        self._failed_nodes: Set[str] = set()
        self._node_errors: Dict[str, str] = {}
        self._selected_branches: Dict[str, str | None] = {}
        self._repeat_runtime: Dict[str, _RepeatRuntime] = {
            region_uuid: _RepeatRuntime()
            for region_uuid in spec.repeat_regions
        }

        self._build()
        if not self._nodes:
            self.state = WorkflowState.SUCCESS

    # ── 构图（Go buildTask 等价） ──────────────────────────────

    def _build(self) -> None:
        spec = self.spec
        for node in spec.nodes:
            if node.disabled:
                continue
            self._nodes[node.id] = node
            self._node_states[node.id] = NodeState.PENDING

        # 三级 handle 寻址：uuid（旧协议）→ (node_id, handle_key)（新协议，
        # 对齐 workflow_edge.source_handle_key + workflow_handle_template 模板内唯一）
        # → 全局唯一 handle_key（payload 未带 node_id 且 key 不歧义时的兜底）。
        by_uuid: Dict[str, Handle] = {}
        by_node_key: Dict[tuple, Handle] = {}
        by_key: Dict[str, Handle] = {}
        ambiguous_keys: set = set()
        for h in spec.handles:
            if h.uuid:
                by_uuid[h.uuid] = h
            if h.handle_key:
                if h.node_id:
                    by_node_key[(h.node_id, h.handle_key)] = h
                if h.handle_key in by_key:
                    ambiguous_keys.add(h.handle_key)
                else:
                    by_key[h.handle_key] = h

        def find_handle(
            handle_uuid: str, node_id: str, handle_key: str
        ) -> Optional[Handle]:
            if handle_uuid and handle_uuid in by_uuid:
                return by_uuid[handle_uuid]
            if handle_key:
                scoped = by_node_key.get((node_id, handle_key))
                if scoped is not None:
                    return scoped
                if handle_key not in ambiguous_keys:
                    return by_key.get(handle_key)
            return None

        children: Dict[str, List[str]] = {}

        for node_id in self._nodes:
            self._pending_parents[node_id] = set()

        for edge in spec.edges:
            src, dst = edge.source_node_id, edge.target_node_id
            # 过滤无效边（Go loadData 里 sourceNodeExist && targetNodeExist）
            if src not in self._nodes or dst not in self._nodes:
                continue
            self._pending_parents[dst].add(src)
            children.setdefault(src, []).append(dst)

            # 传参边过滤规则与 Go buildNodeHandlePair 一致
            source_handle = find_handle(
                edge.source_handle_uuid, src, edge.source_handle_key
            )
            target_handle = find_handle(
                edge.target_handle_uuid, dst, edge.target_handle_key
            )
            if source_handle is None or target_handle is None:
                continue
            if (
                source_handle.data_source != "executor"
                or source_handle.handle_key == "ready"
                or source_handle.data_key == ""
            ):
                continue
            if target_handle.handle_key == "ready" or target_handle.data_key == "":
                continue
            self._parent_pairs.setdefault(dst, []).append(
                HandlePair(
                    source_node_id=src,
                    source_handle=source_handle,
                    target_handle=target_handle,
                )
            )

        self._detect_cycle(children)
        self._build_static_resource_passthrough()

    @staticmethod
    def _is_resource_slot(handle: Handle) -> bool:
        """只有执行计划显式标注的 ResourceSlot 才允许静态透传。"""

        return str(handle.value_type or "").strip().lower() == "resourceslot"

    @staticmethod
    def _is_concrete_resource_value(value: Any) -> bool:
        """ResourceSlot 必须带稳定实例身份，空占位符不能进入预测。"""

        return isinstance(value, Mapping) and bool(
            str(value.get("uuid") or value.get("material_uuid") or "").strip()
        )

    def _build_static_resource_passthrough(self) -> None:
        """固定点传播已证明的 ResourceSlot 输入/输出透传值。

        透传元数据来自发布时的合同校验，且只读取节点当前冻结参数；没有明确
        输入值或类型不为 ResourceSlot 时保持未知，绝不根据动作名称猜测结果。
        """

        handles = {handle.uuid: handle for handle in self.spec.handles if handle.uuid}
        params = {node_id: deepcopy(node.param) for node_id, node in self._nodes.items()}
        outputs: Dict[str, Any] = {}
        for _ in range(len(self._nodes) + 1):
            changed = False
            # 先沿显式 ResourceSlot 边传播，再根据同节点透传声明生成输出。
            for target_id, pairs in self._parent_pairs.items():
                target_param = params[target_id]
                for pair in pairs:
                    if not (
                        self._is_resource_slot(pair.source_handle)
                        and self._is_resource_slot(pair.target_handle)
                    ):
                        continue
                    source_output = outputs.get(pair.source_node_id)
                    exists, value = json_get_exists(
                        source_output, pair.source_handle.data_key
                    )
                    if not exists or not self._is_concrete_resource_value(value):
                        continue
                    target_key = pair.target_handle.data_key.split(DATA_KEY_SPLIT)[-1]
                    try:
                        current_exists, current = json_get_exists(target_param, target_key)
                        if current_exists and current != value:
                            # 冻结参数与透传事实冲突时不猜测，以运行时校验为准。
                            continue
                        updated = json_set(target_param, target_key, deepcopy(value))
                    except ParamResolveError:
                        continue
                    if updated != target_param:
                        params[target_id] = updated
                        target_param = updated
                        changed = True
            for node_id, node in self._nodes.items():
                passthrough = node.material_passthrough_handles
                if not passthrough:
                    continue
                output = outputs.get(node_id, {})
                if not isinstance(output, Mapping):
                    output = {}
                output = deepcopy(dict(output))
                for output_uuid, input_uuid in passthrough.items():
                    source = handles.get(output_uuid)
                    target = handles.get(input_uuid)
                    if source is None or target is None or not (
                        self._is_resource_slot(source) and self._is_resource_slot(target)
                    ):
                        continue
                    exists, value = json_get_exists(params[node_id], target.data_key)
                    if not exists or not self._is_concrete_resource_value(value):
                        continue
                    try:
                        updated = json_set(output, source.data_key, deepcopy(value))
                    except ParamResolveError:
                        continue
                    if updated != output:
                        output = updated
                        changed = True
                if output and output != outputs.get(node_id):
                    outputs[node_id] = output
                    changed = True
            if not changed:
                break
        self._static_resource_values = outputs

    def _detect_cycle(self, children: Dict[str, List[str]]) -> None:
        visited: Set[str] = set()
        rec_stack: Set[str] = set()

        def dfs(node_id: str) -> bool:
            visited.add(node_id)
            rec_stack.add(node_id)
            for child in children.get(node_id, []):
                if child not in visited:
                    if dfs(child):
                        return True
                elif child in rec_stack:
                    return True
            rec_stack.discard(node_id)
            return False

        for node_id in self._nodes:
            if node_id not in visited:
                if dfs(node_id):
                    raise WorkflowCycleError(
                        f"workflow {self.spec.workflow_id} has circular dependency"
                    )

    # ── 推进（Go canRunNodes / clearFinishedNode 等价） ────────

    def ready_nodes(self) -> List[WorkflowNode]:
        """入度 0 且未消费的节点（不修改消费标记，消费发生在 mark_dispatched）。"""
        if self.state is not WorkflowState.RUNNING:
            return []
        ready: List[WorkflowNode] = []
        for node_id, parents in self._pending_parents.items():
            if parents:
                continue
            if node_id in self._consumed:
                continue
            ready.append(self._nodes[node_id])
            self._node_states[node_id] = NodeState.READY
        for runtime in self._repeat_runtime.values():
            if runtime.round_run is not None:
                ready.extend(runtime.round_run.ready_nodes())
        return ready

    def mark_dispatched(self, node_id: str) -> None:
        """节点已下发（Go canRunNodes 的 consumedNode 标记 + createJobs）。"""
        owner = self._round_owner(node_id)
        if owner is not None:
            owner.mark_dispatched(node_id)
            return
        self._consumed.add(node_id)
        self._node_states[node_id] = NodeState.DISPATCHED

    def mark_finished(self, node_id: str, ret_value: Any = None) -> None:
        """节点成功完成：记录返回值并从依赖表中清除（Go clearFinishedNode）。"""
        owner = self._round_owner(node_id)
        if owner is not None:
            owner.mark_finished(node_id, ret_value)
            self._settle_canceled_repeat(owner)
            return
        if node_id not in self._nodes:
            return
        self._consumed.add(node_id)
        self._ret_values[node_id] = ret_value
        self._node_states[node_id] = NodeState.SUCCESS
        self._pending_parents.pop(node_id, None)
        for parents in self._pending_parents.values():
            parents.discard(node_id)
        if self._is_all_done():
            if self._cancel_requested:
                self.state = WorkflowState.CANCELED
            elif self._failed_nodes:
                self.state = WorkflowState.FAILED
            else:
                self.state = WorkflowState.SUCCESS

    def mark_skipped(self, node_id: str, *, reason: str) -> None:
        """把未激活控制路径节点结算为跳过，并满足其下游结构依赖。"""

        if node_id not in self._nodes:
            raise ValueError(f"条件区域引用未知节点：{node_id}")
        if self._node_states[node_id] is NodeState.DISPATCHED:
            raise ValueError(f"已派发节点不能被条件跳过：{node_id}")
        self._consumed.add(node_id)
        self._node_states[node_id] = NodeState.SKIPPED
        self._node_errors[node_id] = reason
        self._pending_parents.pop(node_id, None)
        for parents in self._pending_parents.values():
            parents.discard(node_id)
        if self.state is WorkflowState.RUNNING and self._is_all_done():
            self.state = WorkflowState.SUCCESS

    def mark_failed(self, node_id: str, *, reason: str = "") -> None:
        """节点失败：整个工作流失败（对齐 Go errChan → jobsCtxCancel 全停语义）。"""
        owner = self._round_owner(node_id)
        if owner is not None:
            owner.mark_failed(node_id, reason=reason)
            for region_uuid, runtime in self._repeat_runtime.items():
                if runtime.round_run is owner:
                    self.mark_failed(region_uuid, reason=reason or "loop_body_failed")
                    break
            return
        if node_id not in self._nodes:
            return
        self._consumed.add(node_id)
        self._failed_nodes.add(node_id)
        self._node_states[node_id] = NodeState.FAILED
        if reason:
            self._node_errors[node_id] = reason
        if self._cancel_requested:
            self.state = (
                WorkflowState.CANCELED
                if self._is_all_done()
                else WorkflowState.CANCELING
            )
        else:
            self.state = WorkflowState.FAILED

    def abort_execution_process(self) -> None:
        """执行进程退出后终结内存图，保留已完成节点，不触发设备取消。"""
        for node_id, state in self._node_states.items():
            if state == NodeState.DISPATCHED:
                self._node_states[node_id] = NodeState.FAILED
            elif state in {NodeState.PENDING, NodeState.READY}:
                self._node_states[node_id] = NodeState.CANCELED
        for runtime in self._repeat_runtime.values():
            if runtime.round_run is not None:
                runtime.round_run.abort_execution_process()
        self.state = WorkflowState.FAILED

    def hold_error(self, node_id: str) -> None:
        """消费失败的本次执行但保留依赖，任务不提前进入终态。"""
        owner = self._round_owner(node_id)
        if owner is not None:
            owner.hold_error(node_id)
            return
        if node_id not in self._nodes:
            raise ValueError("错误节点不属于当前运行")
        self._consumed.add(node_id)
        self._node_states[node_id] = NodeState.ERROR_WAITING

    def retry_node(self, node_id: str, job_id: str) -> None:
        """只为具体节点实例替换 attempt，保留前置结果与当前执行模式。"""
        owner = self._round_owner(node_id)
        if owner is not None:
            owner.retry_node(node_id, job_id)
            return
        node = self._nodes.get(node_id)
        if node is None:
            raise ValueError("重试节点不属于当前运行")
        if node.job_id == job_id:
            return
        node.job_id = job_id
        self._consumed.discard(node_id)
        self._failed_nodes.discard(node_id)
        self._node_errors.pop(node_id, None)
        self._node_states[node_id] = NodeState.PENDING

    def restore_execution_facts(self, jobs: Sequence[Mapping[str, Any]]) -> None:
        """仅重建已落盘的有效执行事实；循环物化不提交任何设备动作。"""
        jobs_by_id = {str(job["uuid"]): job for job in jobs}
        facts = {
            str((job.get("control_data") or {}).get("runtime_node_id") or job["workflow_node_uuid"]): job
            for job in jobs if job.get("current_attempt", True)
        }
        restored: set[str] = set()
        while True:
            progressed = False
            for node_id, job in facts.items():
                node = self.node(node_id)
                if node is None or node_id in restored:
                    continue
                node.job_id = str(job["uuid"])
                status = job["status"]
                if job.get("awaiting_error_decision"):
                    self.hold_error(node_id)
                    restored.add(node_id)
                    progressed = True
                    continue
                if node.executor_kind == "repeat_until" and status not in {"succeeded", "skipped", "failed", "timeout", "canceled"}:
                    continue
                if status == "succeeded":
                    return_info = job.get("return_info") or {}
                    # 与正常完成回调一致，只解包一层执行回执；本地输入和
                    # 物料来源的平铺结果保持原样，不能把回执元数据当作节点输出。
                    ret_value = (
                        return_info.get("return_value", return_info)
                        if isinstance(return_info, Mapping)
                        else return_info
                    )
                    self.mark_finished(node_id, ret_value)
                elif status == "skipped":
                    self.mark_skipped(node_id, reason="persisted_branch_decision")
                elif status in {"failed", "timeout"}:
                    self.hold_error(node_id)
                elif status == "canceled":
                    self.mark_canceled(node_id)
                elif status in {"dispatched", "running", "cancel_requested"} or job.get("manual_continuation_pending"):
                    self.mark_dispatched(node_id)
                restored.add(node_id)
                progressed = True
            evaluation = self.prepare_local_control()
            if evaluation is not None and evaluation.get("control_type") == "repeat_until":
                control = facts.get(str(evaluation["node_id"]))
                can_replay = False
                if evaluation["phase"] == "materialize":
                    can_replay = all(item["runtime_node_id"] in facts for item in evaluation["iteration_jobs"])
                elif control is not None:
                    # 重试更换控制 Job，但已经成功的轮次及其决定仍是恢复事实。
                    # 沿显式 retry 链读取，不能把旧 attempt 的终态赋给新 attempt。
                    data = control.get("control_data") or {}
                    previous = control
                    visited = {str(control["uuid"])}
                    while "condition_result" not in data:
                        retry_of = str(previous.get("retry_of_job_uuid") or "")
                        if retry_of in visited or retry_of not in jobs_by_id:
                            break
                        visited.add(retry_of)
                        previous = jobs_by_id[retry_of]
                        data = previous.get("control_data") or {}
                    can_replay = "condition_result" in data and int(data.get("iteration_index", -1)) >= evaluation["iteration_index"]
                if can_replay:
                    self.commit_local_control(evaluation)
                    progressed = True
            if not progressed:
                break

    def advance_local_controls(self) -> list[dict[str, Any]]:
        """连续求值当前就绪的条件节点，不越过物理执行边界。"""

        evaluations: list[dict[str, Any]] = []
        while self.state is WorkflowState.RUNNING:
            evaluation = self.prepare_local_control()
            if evaluation is None:
                break
            self.commit_local_control(evaluation)
            evaluations.append(evaluation)
        return evaluations

    def prepare_local_control(self, deferred: set[str] | None = None) -> dict[str, Any] | None:
        """只读准备下一个条件决定，供持久层在内存提交前落盘。"""

        for region_uuid, runtime in self._repeat_runtime.items():
            if runtime.round_run is None:
                if (
                    runtime.iteration_index >= 0
                    and self._node_states.get(region_uuid) is NodeState.DISPATCHED
                ):
                    try:
                        return self._prepare_repeat_materialization(
                            self._nodes[region_uuid]
                        )
                    except (ConditionEvaluationError, TypeError, ValueError) as error:
                        return self._repeat_failure_evaluation(
                            region_uuid,
                            runtime,
                            error=ConditionEvaluationError.code,
                            message=str(error),
                        )
                continue
            nested = runtime.round_run.prepare_local_control(deferred)
            if nested is not None:
                owner_path = nested.get("owner_repeat_node_ids")
                if not isinstance(owner_path, list):
                    owner_path = []
                nested["owner_repeat_node_ids"] = [region_uuid, *owner_path]
                return nested
            if runtime.round_run.state is WorkflowState.FAILED:
                return self._repeat_failure_evaluation(
                    region_uuid,
                    runtime,
                    error="loop_body_failed",
                    message="循环体节点失败",
                )
            if runtime.round_run.state is WorkflowState.SUCCESS:
                try:
                    return self._prepare_repeat_evaluation(region_uuid, runtime)
                except (ConditionEvaluationError, TypeError, ValueError) as error:
                    return self._repeat_failure_evaluation(
                        region_uuid,
                        runtime,
                        error=ConditionEvaluationError.code,
                        message=str(error),
                    )

        # 与原生命令一致：本轮已返回 pending（如物料被其他任务占用）的来源不在同一轮重评。
        ready_source = next((node for node in self.ready_nodes() if node.executor_kind == "material_source" and node.id not in (deferred or set())), None)
        if ready_source is not None:
            return {"node_id": ready_source.id, "control_type": "material_source", "skipped_node_ids": []}
        ready_command = next((node for node in self.ready_nodes() if node.executor_kind == "workflow_command" and node.id not in (deferred or set())), None)
        if ready_command is not None:
            return {
                "control_type": "workflow_command", "node_id": ready_command.id,
                "command_name": ready_command.action_name,
                "arguments": deepcopy(self.resolve_params(ready_command.id)),
                "input_jobs": {
                    pair.target_handle.data_key: self._nodes[pair.source_node_id].job_id
                    for pair in self._parent_pairs.get(ready_command.id, [])
                },
                "skipped_node_ids": [],
            }
        ready_condition = next(
            (node for node in self.ready_nodes() if node.executor_kind == "condition"),
            None,
        )
        if ready_condition is None:
            ready_repeat = next(
                (
                    node
                    for node in self.ready_nodes()
                    if node.executor_kind == "repeat_until"
                    and node.id in self._repeat_runtime
                ),
                None,
            )
            if ready_repeat is None:
                return None
            runtime = self._repeat_runtime[ready_repeat.id]
            try:
                return self._prepare_repeat_materialization(ready_repeat)
            except (ConditionEvaluationError, TypeError, ValueError) as error:
                return self._repeat_failure_evaluation(
                    ready_repeat.id,
                    runtime,
                    error=ConditionEvaluationError.code,
                    message=str(error),
                )
        try:
            selected_label, skipped = self._evaluate_condition_node(ready_condition)
        except (ConditionEvaluationError, TypeError, ValueError) as error:
            return {
                "node_id": ready_condition.id,
                "error": ConditionEvaluationError.code,
                "message": str(error),
                "skipped_node_ids": self._pending_after_control_failure(
                    ready_condition.id
                ),
            }
        return {
            "node_id": ready_condition.id,
            "selected_branch": selected_label,
            "skipped_node_ids": skipped,
        }

    def _repeat_failure_evaluation(
        self,
        region_uuid: str,
        runtime: _RepeatRuntime,
        *,
        error: str,
        message: str,
    ) -> dict[str, Any]:
        """构造可持久化、可立即提交的循环失败决定。"""

        return {
            "control_type": "repeat_until",
            "phase": "evaluate",
            "node_id": region_uuid,
            "iteration_index": max(runtime.iteration_index, 0),
            "condition_result": None,
            "carry": deepcopy(runtime.carry or {}),
            "next_carry": None,
            "error": error,
            "message": message,
            "skipped_node_ids": self._pending_after_control_failure(region_uuid),
        }

    def commit_local_control(self, evaluation: dict[str, Any]) -> None:
        """在外部持久投影成功后提交已准备的条件决定。"""

        node_id = str(evaluation.get("node_id") or "")
        owner_repeat_node_ids = evaluation.get("owner_repeat_node_ids")
        if isinstance(owner_repeat_node_ids, list) and owner_repeat_node_ids:
            owner_repeat_node_id = str(owner_repeat_node_ids[0])
            runtime = self._repeat_runtime.get(owner_repeat_node_id)
            if runtime is None or runtime.round_run is None:
                raise ValueError("嵌套本地控制决定缺少活动循环轮次")
            nested = dict(evaluation)
            remaining = list(owner_repeat_node_ids[1:])
            if remaining:
                nested["owner_repeat_node_ids"] = remaining
            else:
                nested.pop("owner_repeat_node_ids", None)
            runtime.round_run.commit_local_control(nested)
            return
        if evaluation.get("control_type") == "material_source":
            if evaluation.get("error"):
                self.mark_failed(node_id, reason=str(evaluation["error"]))
            else:
                result = evaluation.get("result")
                if not isinstance(result, dict) or type(result.get("sufficient")) is not bool:
                    raise ValueError("运行时物料来源缺少持久检查结果")
                self.mark_finished(node_id, result)
            return
        if evaluation.get("control_type") == "repeat_until":
            self._commit_repeat_control(evaluation)
            return
        if evaluation.get("control_type") == "workflow_command":
            if evaluation.get("error"):
                self.mark_failed(node_id, reason=str(evaluation["error"]))
            else:
                self.mark_finished(node_id, dict(evaluation["result"]))
            return
        skipped = evaluation.get("skipped_node_ids")
        if node_id not in self._nodes or not isinstance(skipped, list):
            raise ValueError("本地条件决定结构无效")
        if evaluation.get("error"):
            self.mark_failed(node_id, reason=ConditionEvaluationError.code)
            reason = ConditionEvaluationError.code
        else:
            selected_label = evaluation.get("selected_branch")
            self._selected_branches[node_id] = (
                str(selected_label) if selected_label is not None else None
            )
            self.mark_finished(node_id, {"selected_branch": selected_label})
            reason = "branch_not_selected"
        for skipped_node_id in skipped:
            self.mark_skipped(str(skipped_node_id), reason=reason)

    def _pending_after_control_failure(self, failed_node_id: str) -> list[str]:
        """只读列出尚未越过派发边界的其余节点。"""

        return [
            node_id
            for node_id in self._nodes
            if node_id != failed_node_id
            and self._node_states[node_id] in {NodeState.PENDING, NodeState.READY}
        ]

    def _evaluate_condition_node(
        self,
        node: WorkflowNode,
    ) -> tuple[str | None, list[str]]:
        """选择首个严格为真的分支并返回所有未选节点。"""

        variables = node.param.get("variables", {})
        bindings = node.param.get("bindings", {})
        branches = node.param.get("branches")
        if (
            not isinstance(variables, dict)
            or not isinstance(bindings, dict)
            or not isinstance(branches, list)
            or not branches
        ):
            raise ConditionEvaluationError("条件区域参数无效")
        variables = dict(variables)
        for name, binding in bindings.items():
            if not isinstance(name, str) or not isinstance(binding, dict):
                raise ConditionEvaluationError("条件变量绑定无效")
            if binding.get("kind") == "node_result":
                source_uuid = str(binding.get("node_uuid") or "")
                if source_uuid not in self._ret_values:
                    raise ConditionEvaluationError(f"条件结果尚未就绪：{name}")
                # 直连 Edge 入口可能绕过 WorkflowSpecCompiler；节点结果绑定必须
                # 无条件覆盖任何预填同名变量，不能被不可信输入遮蔽。
                variables[name] = self._resolve_repeat_binding(binding, runtime=None)
            elif binding.get("kind") == "workflow_input":
                if name not in variables:
                    raise ConditionEvaluationError(f"条件输入不存在：{name}")
            else:
                raise ConditionEvaluationError("条件变量绑定类型无效")
        selected_index: int | None = None
        default_index: int | None = None
        for index, branch in enumerate(branches):
            if not isinstance(branch, dict):
                raise ConditionEvaluationError("条件分支必须是对象")
            condition = branch.get("condition")
            if condition is None:
                if default_index is not None or index != len(branches) - 1:
                    raise ConditionEvaluationError("else 必须是唯一最后分支")
                default_index = index
                continue
            if selected_index is None and evaluate_condition_expression(
                condition,
                variables=variables,
            ):
                selected_index = index
                break
        if selected_index is None:
            selected_index = default_index
        skipped: list[str] = []
        for index, branch in enumerate(branches):
            node_uuids = branch.get("node_uuids")
            if not isinstance(node_uuids, list) or any(
                not isinstance(value, str) for value in node_uuids
            ):
                raise ConditionEvaluationError("条件分支节点集合无效")
            if index != selected_index:
                # RepeatUntil 的惰性 body 模板不属于当前运行 DAG，也尚未创建
                # Job；只跳过当前层真实存在的控制节点和动作节点。
                skipped.extend(
                    node_uuid for node_uuid in node_uuids if node_uuid in self._nodes
                )
        selected_label = (
            str(branches[selected_index].get("label"))
            if selected_index is not None
            else None
        )
        return selected_label, skipped

    def _round_owner(self, node_id: str) -> "WorkflowRun | None":
        """返回当前持有动态轮次节点的子 DAG。"""

        for runtime in self._repeat_runtime.values():
            round_run = runtime.round_run
            if round_run is not None and (
                node_id in round_run._nodes or round_run._round_owner(node_id) is not None
            ):
                return round_run
        return None

    def _prepare_repeat_materialization(self, node: WorkflowNode) -> dict[str, Any]:
        """只读生成下一轮节点身份和幂等作业描述。"""

        region = self.spec.repeat_regions[node.id]
        runtime = self._repeat_runtime[node.id]
        iteration_index = runtime.iteration_index + 1
        if runtime.carry is None:
            raw_initial = node.param.get("initial_carry")
            if not isinstance(raw_initial, dict):
                raise ConditionEvaluationError("循环初始 carry 无效")
            carry = {
                key: self._resolve_repeat_binding(binding, runtime=None)
                for key, binding in raw_initial.items()
            }
        else:
            carry = deepcopy(runtime.carry)
        self._validate_repeat_carry(carry)
        jobs: list[dict[str, Any]] = []
        handle_by_uuid = {handle.uuid: handle for handle in region.handles}
        for template_node in region.nodes:
            runtime_node_id = self._repeat_identity(
                node.id, iteration_index, template_node.id, "node"
            )
            job_uuid = self._repeat_identity(
                node.id, iteration_index, template_node.id, "job"
            )
            jobs.append(
                {
                    "job_uuid": job_uuid,
                    "workflow_node_uuid": template_node.id,
                    "runtime_node_id": runtime_node_id,
                    "executor_kind": template_node.executor_kind,
                    "iteration_index": iteration_index,
                    "control_path": node.id,
                    "param": self._repeat_template_param(
                        template_node,
                        carry=carry,
                        region_uuid=node.id,
                        handle_by_uuid=handle_by_uuid,
                        inbound_bindings=region.inbound_bindings.get(template_node.id, ()),
                    ),
                    "execution_policy": deepcopy(template_node.execution_policy),
                    "material_uuid": template_node.device_material_uuid or None,
                }
            )
        return {
            "control_type": "repeat_until",
            "phase": "materialize",
            "node_id": node.id,
            "iteration_index": iteration_index,
            "carry": carry,
            "iteration_jobs": jobs,
            "skipped_node_ids": [],
        }

    def _prepare_repeat_evaluation(
        self,
        region_uuid: str,
        runtime: _RepeatRuntime,
    ) -> dict[str, Any]:
        """在整轮成功后计算严格布尔退出条件和下一版 carry。"""

        node = self._nodes[region_uuid]
        round_run = runtime.round_run
        assert round_run is not None
        bindings = node.param.get("bindings")
        until_expression = node.param.get("until")
        if not isinstance(bindings, dict) or not isinstance(until_expression, dict):
            raise ConditionEvaluationError("循环条件合同无效")
        variables: dict[str, Any] = {}
        template_to_runtime = {
            template_uuid: runtime_uuid
            for runtime_uuid, template_uuid in (runtime.runtime_to_template or {}).items()
        }
        for name, binding in bindings.items():
            if not isinstance(binding, dict):
                raise ConditionEvaluationError("循环条件绑定无效")
            if binding.get("kind") == "workflow_input":
                preset = node.param.get("variables", {})
                if not isinstance(preset, dict) or name not in preset:
                    raise ConditionEvaluationError(f"循环条件输入不存在：{name}")
                variables[name] = deepcopy(preset[name])
            elif binding.get("kind") == "node_result":
                runtime_id = template_to_runtime.get(str(binding.get("node_uuid") or ""))
                if runtime_id is None or runtime_id not in round_run._ret_values:
                    raise ConditionEvaluationError(f"循环条件结果尚未就绪：{name}")
                variables[name] = self._resolve_repeat_binding(binding, runtime=runtime)
            else:
                raise ConditionEvaluationError("循环条件绑定类型无效")
        normalized_until = self._bind_repeat_expression_carry(
            until_expression,
            region_uuid=region_uuid,
            carry=runtime.carry or {},
            variables=variables,
        )
        satisfied = evaluate_condition_expression(
            normalized_until,
            variables=variables,
        )
        next_bindings = node.param.get("next_carry")
        if not isinstance(next_bindings, dict):
            raise ConditionEvaluationError("循环下一版 carry 无效")
        next_carry = {
            key: self._resolve_repeat_binding(binding, runtime=runtime)
            for key, binding in next_bindings.items()
        }
        self._validate_repeat_carry(next_carry)
        maximum = node.param.get("max_iterations")
        if not satisfied and runtime.iteration_index + 1 >= int(maximum):
            return self._repeat_failure_evaluation(
                region_uuid,
                runtime,
                error="loop_iteration_limit_exceeded",
                message="循环达到最大迭代次数仍未满足退出条件",
            )
        return {
            "control_type": "repeat_until",
            "phase": "evaluate",
            "node_id": region_uuid,
            "iteration_index": runtime.iteration_index,
            "condition_result": satisfied,
            "carry": deepcopy(runtime.carry or {}),
            "next_carry": next_carry,
            "skipped_node_ids": [],
        }

    def _commit_repeat_control(self, evaluation: dict[str, Any]) -> None:
        """在持久投影成功后物化轮次或提交退出/续轮决定。"""

        region_uuid = str(evaluation.get("node_id") or "")
        runtime = self._repeat_runtime.get(region_uuid)
        if runtime is None:
            raise ValueError("RepeatUntil 决定引用未知区域")
        phase = evaluation.get("phase")
        if phase == "materialize":
            self._commit_repeat_materialization(region_uuid, runtime, evaluation)
            return
        if phase != "evaluate":
            raise ValueError("RepeatUntil 决定阶段无效")
        if evaluation.get("error"):
            self.mark_failed(region_uuid, reason=str(evaluation["error"]))
            for skipped_node_id in evaluation.get("skipped_node_ids", []):
                self.mark_skipped(str(skipped_node_id), reason=str(evaluation["error"]))
            return
        if evaluation.get("condition_result") is True:
            self.mark_finished(
                region_uuid,
                {
                    "iteration_index": runtime.iteration_index,
                    "carry": deepcopy(runtime.carry or {}),
                },
            )
            runtime.round_run = None
            return
        if evaluation.get("condition_result") is not False:
            raise ValueError("RepeatUntil 退出条件必须是严格布尔值")
        next_carry = evaluation.get("next_carry")
        if not isinstance(next_carry, dict):
            raise ValueError("RepeatUntil 下一版 carry 无效")
        runtime.carry = deepcopy(next_carry)
        runtime.round_run = None
        runtime.runtime_to_template = None

    def _commit_repeat_materialization(
        self,
        region_uuid: str,
        runtime: _RepeatRuntime,
        evaluation: dict[str, Any],
    ) -> None:
        """用已经持久化的独立作业身份建立本轮子 DAG。"""

        region = self.spec.repeat_regions[region_uuid]
        iteration_index = evaluation.get("iteration_index")
        carry = evaluation.get("carry")
        raw_jobs = evaluation.get("iteration_jobs")
        if (
            isinstance(iteration_index, bool)
            or not isinstance(iteration_index, int)
            or iteration_index != runtime.iteration_index + 1
            or not isinstance(carry, dict)
            or not isinstance(raw_jobs, list)
        ):
            raise ValueError("RepeatUntil 轮次物化决定无效")
        jobs_by_template = {
            str(item.get("workflow_node_uuid")): item
            for item in raw_jobs
            if isinstance(item, dict)
        }
        if set(jobs_by_template) != {node.id for node in region.nodes}:
            raise ValueError("RepeatUntil 轮次作业没有完整覆盖模板")
        runtime_by_template = {
            template_uuid: str(item.get("runtime_node_id") or "")
            for template_uuid, item in jobs_by_template.items()
        }
        handle_by_uuid = {handle.uuid: handle for handle in region.handles}
        nodes: list[WorkflowNode] = []
        for template_node in region.nodes:
            cloned = deepcopy(template_node)
            cloned.id = runtime_by_template[template_node.id]
            cloned.job_id = str(jobs_by_template[template_node.id].get("job_uuid") or "")
            cloned.param = self._repeat_template_param(
                cloned,
                carry=carry,
                region_uuid=region_uuid,
                handle_by_uuid=handle_by_uuid,
                inbound_bindings=region.inbound_bindings.get(template_node.id, ()),
            )
            self._remap_control_param(cloned.param, runtime_by_template)
            self._replace_control_region_ids(
                cloned.param,
                {
                    region.control_node_id: region_uuid,
                    **({template_node.id: cloned.id} if cloned.executor_kind == "repeat_until" else {}),
                },
            )
            cloned.param = self._bind_enclosing_repeat_carry(
                cloned.param,
                region_uuid=region_uuid,
                carry=carry,
            )
            nodes.append(cloned)
        edges = [
            WorkflowEdge(
                uuid=self._repeat_identity(
                    region_uuid, iteration_index, edge.uuid, "edge"
                ),
                source_node_id=runtime_by_template[edge.source_node_id],
                target_node_id=runtime_by_template[edge.target_node_id],
                source_handle_uuid=edge.source_handle_uuid,
                target_handle_uuid=edge.target_handle_uuid,
                source_handle_key=edge.source_handle_key,
                target_handle_key=edge.target_handle_key,
            )
            for edge in region.edges
        ]
        handles = [
            Handle(
                uuid=handle.uuid,
                data_source=handle.data_source,
                handle_key=handle.handle_key,
                data_key=handle.data_key,
                node_id=runtime_by_template[handle.node_id],
                io_type=handle.io_type,
            )
            for handle in region.handles
        ]
        runtime.iteration_index = iteration_index
        runtime.carry = deepcopy(carry)
        runtime.runtime_to_template = {
            runtime_uuid: template_uuid
            for template_uuid, runtime_uuid in runtime_by_template.items()
        }
        runtime.round_run = WorkflowRun(
            WorkflowSpec(
                workflow_id=f"{self.spec.workflow_id}:{region_uuid}:{iteration_index}",
                task_id=self.spec.task_id,
                nodes=nodes,
                edges=edges,
                handles=handles,
                priority=self.spec.priority,
                submitted_at=self.spec.submitted_at,
                lab_id=self.spec.lab_id,
                run_mode="normal",
                resource_plan=deepcopy(self.spec.resource_plan),
                resource_coordinator_node_ids=list(
                    self.spec.resource_coordinator_node_ids
                ),
                repeat_regions={
                    runtime_by_template[child_template_uuid]: (
                        self._instantiate_nested_repeat_region(
                            child_region,
                            template_control_uuid=child_template_uuid,
                            runtime_control_uuid=runtime_by_template[
                                child_template_uuid
                            ],
                            enclosing_template_uuid=region.control_node_id,
                            enclosing_runtime_uuid=region_uuid,
                            enclosing_carry=carry,
                            runtime_by_template=runtime_by_template,
                            outer_run=self,
                        )
                    )
            for child_template_uuid, child_region in region.repeat_regions.items()
                },
            )
        )
        self._consumed.add(region_uuid)
        self._node_states[region_uuid] = NodeState.DISPATCHED

    @staticmethod
    def _instantiate_nested_repeat_region(
        region: RepeatUntilRegion,
        *,
        template_control_uuid: str,
        runtime_control_uuid: str,
        enclosing_template_uuid: str,
        enclosing_runtime_uuid: str,
        enclosing_carry: dict[str, Any],
        runtime_by_template: Mapping[str, str] | None = None,
        outer_run: "WorkflowRun | None" = None,
    ) -> RepeatUntilRegion:
        """为父轮次克隆直接嵌套循环，并重写本轮可见的控制身份。

        跨区域值边的来源若是父轮次的模板节点，改写为本轮运行身份（父轮次子 DAG
        的返回值按运行身份索引）；若来自更外层（父轮次子 DAG 看不到的节点），
        此刻它必然已完成，直接取值固化为字面量。
        """

        cloned = deepcopy(region)
        cloned.control_node_id = runtime_control_uuid
        remap = dict(runtime_by_template or {})

        def rebind_inbound(current: RepeatUntilRegion) -> None:
            # 更深层嵌套的循环同样看不到父轮次之外的节点，一并改写/固化。
            for bindings in current.inbound_bindings.values():
                for binding in bindings:
                    if "literal" in binding:
                        continue
                    source_id = str(binding.get("source_node_id") or "")
                    if source_id in remap:
                        binding["source_node_id"] = remap[source_id]
                    elif outer_run is not None and outer_run._has_ret_value(source_id):
                        binding["literal"] = deepcopy(outer_run.ret_value(source_id))
            for nested in current.repeat_regions.values():
                rebind_inbound(nested)

        rebind_inbound(cloned)
        replacements = {
            template_control_uuid: runtime_control_uuid,
            enclosing_template_uuid: enclosing_runtime_uuid,
        }

        def rewrite(current: RepeatUntilRegion) -> None:
            handles = {handle.uuid: handle for handle in current.handles}
            for body_node in current.nodes:
                WorkflowRun._replace_control_region_ids(
                    body_node.carry_bindings,
                    replacements,
                )
                WorkflowRun._replace_control_region_ids(body_node.param, replacements)
                body_node.param = WorkflowRun._bind_enclosing_repeat_carry(
                    body_node.param,
                    region_uuid=enclosing_runtime_uuid,
                    carry=enclosing_carry,
                )
                for handle_uuid, binding in list(body_node.carry_bindings.items()):
                    if (
                        binding.get("control_region_uuid")
                        != enclosing_runtime_uuid
                    ):
                        continue
                    handle = handles.get(handle_uuid)
                    key = str(binding.get("key") or "")
                    if handle is None or key not in enclosing_carry:
                        raise ConditionEvaluationError(
                            "嵌套循环的外层 carry 绑定无法解析"
                        )
                    target_key = (handle.data_key or handle.handle_key).split("@@@")[-1]
                    body_node.param[target_key] = deepcopy(enclosing_carry[key])
                    del body_node.carry_bindings[handle_uuid]
            for nested in current.repeat_regions.values():
                rewrite(nested)

        rewrite(cloned)
        return cloned

    @staticmethod
    def _replace_control_region_ids(value: Any, replacements: dict[str, str]) -> None:
        """原地重写结构化绑定中的控制区域身份。"""

        if isinstance(value, dict):
            current = value.get("control_region_uuid")
            if isinstance(current, str) and current in replacements:
                value["control_region_uuid"] = replacements[current]
            for child in value.values():
                WorkflowRun._replace_control_region_ids(child, replacements)
        elif isinstance(value, list):
            for child in value:
                WorkflowRun._replace_control_region_ids(child, replacements)

    @staticmethod
    def _bind_enclosing_repeat_carry(
        value: Any,
        *,
        region_uuid: str,
        carry: dict[str, Any],
    ) -> Any:
        """把当前轮可见的外层 carry 冻结为本轮私有字面值。"""

        if isinstance(value, dict):
            if (
                set(value) == {"carry", "control_region_uuid"}
                and value.get("control_region_uuid") == region_uuid
            ):
                key = str(value.get("carry") or "")
                if key not in carry:
                    raise ConditionEvaluationError("循环条件 carry 键不存在")
                return {"lit": deepcopy(carry[key])}
            if (
                value.get("kind") == "loop_carry"
                and value.get("control_region_uuid") == region_uuid
            ):
                key = str(value.get("key") or "")
                if key not in carry:
                    raise ConditionEvaluationError("循环 carry 键不存在")
                return {"kind": "literal", "value": deepcopy(carry[key])}
            return {
                key: WorkflowRun._bind_enclosing_repeat_carry(
                    child,
                    region_uuid=region_uuid,
                    carry=carry,
                )
                for key, child in value.items()
            }
        if isinstance(value, list):
            return [
                WorkflowRun._bind_enclosing_repeat_carry(
                    child,
                    region_uuid=region_uuid,
                    carry=carry,
                )
                for child in value
            ]
        return deepcopy(value)

    def _repeat_template_param(
        self,
        node: WorkflowNode,
        *,
        carry: dict[str, Any],
        region_uuid: str,
        handle_by_uuid: dict[str, Handle],
        inbound_bindings: Sequence[Mapping[str, Any]] = (),
    ) -> dict[str, Any]:
        """把当前 carry 与区域外来源输出注入一个冻结动作模板参数副本。

        ``inbound_bindings`` 来自 ``RepeatUntilRegion.inbound_bindings``：跨区域
        值边在顶层 DAG 与区域模板里都不存在，只能在物化时从外层已完成结果取值。
        区域控制节点本身依赖这些来源节点，因此物化时它们必定已完成；取不到即
        失败关闭，不允许把缺参数的作业派发出去再被 Schema 拒绝。
        """

        param = deepcopy(node.param)
        for handle_uuid, binding in node.carry_bindings.items():
            if binding.get("control_region_uuid") != region_uuid:
                continue
            handle = handle_by_uuid.get(handle_uuid)
            key = binding.get("key")
            if handle is None or key not in carry:
                raise ValueError("RepeatUntil 动作 carry 绑定无法解析")
            target_key = (handle.data_key or handle.handle_key).split("@@@")[-1]
            param[target_key] = deepcopy(carry[str(key)])
        for binding in inbound_bindings:
            if "literal" in binding:
                ret_value = binding["literal"]
                source_id = str(binding.get("source_node_id") or "literal")
            else:
                source_id = str(binding.get("source_node_id") or "")
                if not self._has_ret_value(source_id):
                    raise ValueError(f"循环体外部来源节点尚未完成：{source_id}")
                ret_value = self.ret_value(source_id)
            pair = HandlePair(
                source_node_id=source_id,
                source_handle=Handle(
                    data_source="executor",
                    handle_key=str(binding.get("source_data_key") or ""),
                    data_key=str(binding.get("source_data_key") or ""),
                    node_id=source_id,
                    io_type="source",
                ),
                target_handle=Handle(
                    data_source="executor",
                    handle_key=str(binding.get("target_data_key") or ""),
                    data_key=str(binding.get("target_data_key") or ""),
                    node_id=node.id,
                    io_type="target",
                ),
            )
            try:
                param = resolve_parent_params(param, [pair], {source_id: ret_value})
            except ParamResolveError as error:
                raise ValueError(f"循环体外部来源值无法解析：{error}") from error
        return param

    def _has_ret_value(self, node_id: str) -> bool:
        """判断一个（可能属于某轮次子 DAG 的）节点是否已有返回值。"""

        if node_id in self._ret_values:
            return True
        owner = self._round_owner(node_id)
        return owner is not None and owner._has_ret_value(node_id)

    def _resolve_repeat_binding(
        self,
        binding: Any,
        *,
        runtime: _RepeatRuntime | None,
    ) -> Any:
        """从冻结输入、区域外结果、当前轮结果或 carry 解析一个值。"""

        if not isinstance(binding, dict):
            raise ConditionEvaluationError("循环 carry 来源必须是对象")
        kind = binding.get("kind")
        if kind == "literal":
            return deepcopy(binding.get("value"))
        if kind == "workflow_input":
            parameter = str(binding.get("parameter") or "")
            variables = next(
                (
                    node.param.get("variables", {})
                    for node in self._nodes.values()
                    if node.executor_kind == "repeat_until"
                    and isinstance(node.param.get("variables", {}), dict)
                    and parameter in node.param.get("variables", {})
                ),
                {},
            )
            if parameter not in variables:
                raise ConditionEvaluationError(f"循环输入不存在：{parameter}")
            return deepcopy(variables[parameter])
        if kind == "loop_carry":
            if runtime is None or runtime.carry is None:
                raise ConditionEvaluationError("循环 carry 尚未建立")
            key = str(binding.get("key") or "")
            if key not in runtime.carry:
                raise ConditionEvaluationError(f"循环 carry 键不存在：{key}")
            return deepcopy(runtime.carry[key])
        if kind == "node_result":
            template_uuid = str(binding.get("node_uuid") or "")
            if runtime is None:
                value = self._ret_values.get(template_uuid)
                if template_uuid not in self._ret_values:
                    raise ConditionEvaluationError("循环初始来源节点尚未完成")
            else:
                round_run = runtime.round_run
                runtime_id = next(
                    (
                        runtime_uuid
                        for runtime_uuid, candidate in (
                            runtime.runtime_to_template or {}
                        ).items()
                        if candidate == template_uuid
                    ),
                    None,
                )
                if round_run is None or runtime_id not in round_run._ret_values:
                    raise ConditionEvaluationError("循环当前轮来源节点尚未完成")
                value = round_run._ret_values[str(runtime_id)]
            for path in binding.get("result_path", []):
                if isinstance(value, dict) and path in value:
                    value = value[path]
                else:
                    try:
                        value = getattr(value, str(path))
                    except (AttributeError, TypeError) as error:
                        raise ConditionEvaluationError("循环结果路径不存在") from error
            return deepcopy(value)
        raise ConditionEvaluationError("循环 carry 来源类型无效")

    @staticmethod
    def _validate_repeat_carry(value: Any) -> None:
        """拒绝把 Claim、Fence 或派发租约作为跨轮执行权携带。"""

        forbidden_keys = {
            "claim_uuid",
            "dispatch_credentials",
            "dispatch_permit",
            "execution_claim",
            "execution_locks",
            "fence",
            "fences",
            "fencing_token",
            "lease_token",
        }
        if isinstance(value, dict):
            if forbidden_keys & {str(key) for key in value}:
                raise ConditionEvaluationError("循环 carry 禁止携带运行时执行权")
            for child in value.values():
                WorkflowRun._validate_repeat_carry(child)
        elif isinstance(value, list):
            for child in value:
                WorkflowRun._validate_repeat_carry(child)

    @staticmethod
    def _remap_control_param(
        param: dict[str, Any], runtime_by_template: dict[str, str]
    ) -> None:
        """把轮内条件区域引用的模板节点 UUID 改写为当前轮运行节点 UUID。"""

        branches = param.get("branches")
        if isinstance(branches, list):
            for branch in branches:
                if not isinstance(branch, dict):
                    continue
                for field in ("node_uuids", "entry_node_uuids", "exit_node_uuids"):
                    values = branch.get(field)
                    if isinstance(values, list):
                        branch[field] = [
                            runtime_by_template.get(str(value), str(value))
                            for value in values
                        ]
        for field in ("bindings", "initial_carry"):
            bindings = param.get(field)
            if isinstance(bindings, dict):
                for binding in bindings.values():
                    if isinstance(binding, dict) and binding.get("kind") == "node_result":
                        source = str(binding.get("node_uuid") or "")
                        binding["node_uuid"] = runtime_by_template.get(source, source)

    @staticmethod
    def _bind_repeat_expression_carry(
        value: Any,
        *,
        region_uuid: str,
        carry: dict[str, Any],
        variables: dict[str, Any],
    ) -> Any:
        """把封闭 carry 表达式替换为本次求值的私有变量。"""

        if isinstance(value, dict):
            if set(value) == {"carry", "control_region_uuid"}:
                if value.get("control_region_uuid") != region_uuid:
                    raise ConditionEvaluationError("循环条件引用了其他区域 carry")
                key = value.get("carry")
                if not isinstance(key, str) or key not in carry:
                    raise ConditionEvaluationError("循环条件 carry 键不存在")
                variable_name = f"__repeat_carry_{len(variables)}"
                variables[variable_name] = deepcopy(carry[key])
                return {"var": variable_name}
            return {
                key: WorkflowRun._bind_repeat_expression_carry(
                    child,
                    region_uuid=region_uuid,
                    carry=carry,
                    variables=variables,
                )
                for key, child in value.items()
            }
        if isinstance(value, list):
            return [
                WorkflowRun._bind_repeat_expression_carry(
                    child,
                    region_uuid=region_uuid,
                    carry=carry,
                    variables=variables,
                )
                for child in value
            ]
        return deepcopy(value)

    def _repeat_identity(
        self,
        region_uuid: str,
        iteration_index: int,
        template_identity: str,
        kind: str,
    ) -> str:
        """由任务、控制路径、轮次和模板身份生成幂等运行身份。"""

        try:
            namespace = UUID(self.spec.task_id)
        except (ValueError, TypeError, AttributeError):
            namespace = NAMESPACE_URL
        return str(
            uuid5(
                namespace,
                f"{kind}:{region_uuid}:{iteration_index}:{template_identity}",
            )
        )

    def cancel(self) -> None:
        """停止后续派发，但保留已派发节点直到设备返回明确终态。"""

        self._cancel_requested = True
        for runtime in self._repeat_runtime.values():
            if runtime.round_run is not None:
                runtime.round_run.cancel()
        for node_id, state in self._node_states.items():
            if state in (NodeState.PENDING, NodeState.READY):
                self._node_states[node_id] = NodeState.CANCELED
        for runtime in self._repeat_runtime.values():
            self._settle_canceled_repeat(runtime.round_run)
        self.state = (
            WorkflowState.CANCELED if self._is_all_done() else WorkflowState.CANCELING
        )

    def mark_canceled(self, node_id: str) -> None:
        """用设备明确取消终态结算一个已派发节点。

        参数：``node_id`` 是当前在途节点身份。返回无。异常：未知节点幂等忽略；
        该方法只消费已派发节点，不会把取消请求误当成安全停止证明。
        """

        owner = self._round_owner(node_id)
        if owner is not None:
            owner.mark_canceled(node_id)
            self._settle_canceled_repeat(owner)
            return
        if node_id not in self._nodes:
            return
        self._consumed.add(node_id)
        self._node_states[node_id] = NodeState.CANCELED
        self._pending_parents.pop(node_id, None)
        for parents in self._pending_parents.values():
            parents.discard(node_id)
        self.state = (
            WorkflowState.CANCELED if self._is_all_done() else WorkflowState.CANCELING
        )

    def _settle_canceled_repeat(self, round_run: "WorkflowRun | None") -> None:
        """把已完全取消的活动轮次收敛到其父 RepeatUntil 节点。"""

        if round_run is None or round_run.state is not WorkflowState.CANCELED:
            return
        for region_uuid, runtime in self._repeat_runtime.items():
            if runtime.round_run is not round_run:
                continue
            self._consumed.add(region_uuid)
            self._node_states[region_uuid] = NodeState.CANCELED
            self._pending_parents.pop(region_uuid, None)
            for parents in self._pending_parents.values():
                parents.discard(region_uuid)
            runtime.round_run = None
            runtime.runtime_to_template = None
            break

    def _is_all_done(self) -> bool:
        return all(
            state
            in (
                NodeState.SUCCESS,
                NodeState.SKIPPED,
                NodeState.FAILED,
                NodeState.CANCELED,
            )
            for state in self._node_states.values()
        )

    # ── 传参（Go parsePreNodeParam 等价） ─────────────────────

    def resolve_params(self, node_id: str) -> Any:
        """返回覆写父节点传参后的节点参数（不修改原 spec）。"""
        owner = self._round_owner(node_id)
        if owner is not None:
            return owner.resolve_params(node_id)
        node = self._nodes[node_id]
        pairs = self._parent_pairs.get(node_id, [])
        if not pairs:
            return node.param
        # 一旦真实回执到达，同一来源的静态预测必须整体失效；不能用预测字段
        # 掩盖回执缺少必需 ResourceSlot 的事实。
        parent_values = dict(self._static_resource_values)
        parent_values.update(self._ret_values)
        return resolve_parent_params(node.param, pairs, parent_values)

    # ── 查询 ──────────────────────────────────────────────────

    def node(self, node_id: str) -> Optional[WorkflowNode]:
        node = self._nodes.get(node_id)
        if node is not None:
            return node
        owner = self._round_owner(node_id)
        return owner.node(node_id) if owner is not None else None

    def resource_template_node_uuid(self, runtime_uuid: str) -> str:
        """把本轮派生节点映射回冻结资源计划中的模板身份。"""
        for runtime in self._repeat_runtime.values():
            if runtime_uuid in (runtime.runtime_to_template or {}):
                return runtime.runtime_to_template[runtime_uuid]
            if runtime.round_run is not None:
                result = runtime.round_run.resource_template_node_uuid(runtime_uuid)
                if result != runtime_uuid:
                    return result
        return runtime_uuid

    def resource_runtime_node_uuid(self, template_uuid: str) -> str:
        """读取当前轮节点身份，用实际完成状态判定资源边界。"""
        for runtime in self._repeat_runtime.values():
            for actual, template in (runtime.runtime_to_template or {}).items():
                if template == template_uuid:
                    return actual
            if runtime.round_run is not None:
                result = runtime.round_run.resource_runtime_node_uuid(template_uuid)
                if result != template_uuid:
                    return result
        return template_uuid

    def node_state(self, node_id: str) -> Optional[NodeState]:
        state = self._node_states.get(node_id)
        if state is not None:
            return state
        owner = self._round_owner(node_id)
        return owner.node_state(node_id) if owner is not None else None

    def ret_value(self, node_id: str) -> Any:
        if node_id in self._ret_values:
            return self._ret_values[node_id]
        owner = self._round_owner(node_id)
        return owner.ret_value(node_id) if owner is not None else None

    def snapshot(self) -> Dict[str, Any]:
        """当前运行态快照（API 查询用；含图结构供前端画布渲染）。"""
        return {
            "workflow_id": self.spec.workflow_id,
            "task_id": self.spec.task_id,
            "state": self.state.value,
            "execution_mode": self.execution_mode,
            "dispatch_gate": self.dispatch_gate,
            "nodes": {
                node_id: {
                    "state": state.value,
                    "pending_parents": sorted(
                        self._pending_parents.get(node_id, set())
                    ),
                    "device_id": self._nodes[node_id].device_id,
                    "action_name": self._nodes[node_id].action_name,
                    "node_type": self._nodes[node_id].node_type,
                    "reason": self._node_errors.get(node_id),
                    "selected_branch": self._selected_branches.get(node_id),
                }
                for node_id, state in self._node_states.items()
            },
            "edges": [
                {"source": e.source_node_id, "target": e.target_node_id}
                for e in self.spec.edges
                if e.source_node_id in self._nodes and e.target_node_id in self._nodes
            ],
        }


__all__ = ["WorkflowCycleError", "WorkflowRun"]
