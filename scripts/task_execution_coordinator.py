"""Task 工作区动作认领、后台执行与完成上报协调器。"""

from __future__ import annotations

import hashlib
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from scripts.run_workflow_local import (
    WorkflowNode,
    node_method,
    workflow_node_from_mapping,
)


class TaskApiConflict(RuntimeError):
    """Task API 的正常乐观锁或资源等待冲突。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def deterministic_execution_id(instance_id: str, cursor: int, node_id: str) -> str:
    """根据服务端权威游标生成可跨进程重放的执行 ID。"""
    identity = f"{instance_id}\0{cursor}\0{node_id}".encode()
    return f"task-action-{hashlib.sha256(identity).hexdigest()[:32]}"


def workflow_nodes_from_payload(payload: dict[str, Any]) -> list[WorkflowNode]:
    """按 workflow JSON 契约解析节点，不创建临时文件。"""
    if not isinstance(payload, dict):
        raise ValueError("workflow 内容必须是对象")
    workflow = payload.get("data", payload)
    if not isinstance(workflow, dict):
        raise ValueError("workflow 内容无效")
    nodes = workflow.get("nodes", [])
    if not isinstance(nodes, list):
        raise ValueError("workflow nodes 必须是数组")
    return [workflow_node_from_mapping(item) for item in nodes]


@dataclass
class _InFlightAction:
    future: Future[Any]
    workflow_path: str
    instance_id: str
    node_id: str
    execution_id: str
    result_summary: Any = None
    outcome_prepared: bool = False
    error: dict[str, str] | None = None


@dataclass
class _PendingTerminalReport:
    workflow_path: str
    instance_id: str
    node_id: str
    execution_id: str
    error: dict[str, str]
    retryable: bool = True
    report_error: str | None = None


class TaskExecutionCoordinator:
    """以 Task 工作区为权威状态，异步执行已认领 workflow 节点。"""

    _WAIT_CONFLICTS = {
        "version_conflict",
        "action_already_active",
        "workspace_paused",
    }
    _NON_RETRYABLE_TERMINAL_CONFLICTS = {
        "action_execution_not_found",
        "action_not_active",
        "action_replay_conflict",
        "instance_not_found",
        "instance_not_running",
    }

    def __init__(
        self,
        *,
        task_client: Any,
        node_runner: Callable[
            [WorkflowNode, dict[str, Any], Callable[..., Any]],
            Any,
        ]
        | Callable[
            [WorkflowNode, dict[str, Any], Callable[..., Any], dict[str, Any]],
            Any,
        ],
        device_provider: Callable[[], dict[str, Any]],
        max_workers: int = 4,
    ) -> None:
        self._task_client = task_client
        self._node_runner = node_runner
        self._device_provider = device_provider
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="TaskAction",
        )
        self._lock = threading.RLock()
        self._in_flight: dict[str, _InFlightAction] = {}
        self._pending_terminal_reports: dict[
            str, _PendingTerminalReport
        ] = {}
        self._reported_execution_ids: set[str] = set()
        self._closing = threading.Event()

    def _invoke_node_runner(
        self,
        node: WorkflowNode,
        devices: dict[str, Any],
        action_callable: Callable[..., Any],
        context: dict[str, Any],
    ) -> Any:
        try:
            return self._node_runner(node, devices, action_callable, context)
        except TypeError:
            return self._node_runner(node, devices, action_callable)

    def shutdown(self) -> dict[str, Any]:
        """停止认领，等待实体动作结束，再尽力上报其终态。"""
        self._closing.set()
        with self._lock:
            pass
        self._executor.shutdown(wait=True, cancel_futures=False)
        stats: dict[str, Any] = {
            "success": True,
            "in_flight": 0,
            "completed": 0,
            "failed": 0,
        }
        with self._lock:
            self._harvest_completed(stats)
            self._retry_pending_terminal_report(stats)
            self._update_activity_stats(stats)
            if self._in_flight or self._pending_terminal_reports:
                stats["success"] = False
                stats["message"] = (
                    "动作已结束，但终态上报暂未完成；"
                    "服务端 active execution 将由下次进程安全恢复"
                )
        return stats

    def cycle(
        self,
        *,
        workflow_path: str,
        workflow_nodes: Iterable[WorkflowNode],
        harvest_only: bool = False,
    ) -> dict[str, int | bool]:
        """收割已完成动作并认领新动作；不等待设备动作完成。"""
        if type(harvest_only) is not bool:
            raise TypeError("harvest_only 必须为 bool")
        nodes_by_id = {
            node.uuid: node
            for node in workflow_nodes
            if not node.disabled
        }
        stats: dict[str, int | bool] = {
            "success": True,
            "active": 0,
            "in_flight": 0,
            "claimed": 0,
            "completed": 0,
            "failed": 0,
        }
        with self._lock:
            self._harvest_completed(stats)
            if self._retry_pending_terminal_report(
                stats, workflow_path=workflow_path
            ):
                self._update_activity_stats(
                    stats, workflow_path=workflow_path
                )
                return stats
            if harvest_only:
                self._update_activity_stats(
                    stats, workflow_path=workflow_path
                )
                return stats
            if self._closing.is_set():
                stats["success"] = False
                self._update_activity_stats(
                    stats, workflow_path=workflow_path
                )
                return stats
            response = self._task_client.get_workspace(
                workflow_path=workflow_path
            )
            workspace = _workspace_from_response(response)
            if workspace.get("scheduler_paused") or workspace.get("pause_reason"):
                self._update_activity_stats(
                    stats, workflow_path=workflow_path
                )
                return stats

            templates = {
                str(template.get("id")): template
                for template in workspace.get("templates", [])
                if isinstance(template, dict)
            }
            current_response = response
            if self._recover_orphaned_execution(
                current_response,
                workflow_path=workflow_path,
                workspace=workspace,
                stats=stats,
            ):
                self._update_activity_stats(
                    stats, workflow_path=workflow_path
                )
                return stats

            devices = self._device_provider()

            for instance in workspace.get("task_instances", []):
                if not isinstance(instance, dict) or instance.get("status") != "running":
                    continue
                template = templates.get(str(instance.get("template_id")))
                state = instance.get("execution_state") or {}
                node_ids = template.get("node_ids", []) if template else []
                cursor = int(state.get("cursor", 0))
                if cursor >= len(node_ids):
                    continue
                stats["active"] = int(stats["active"]) + 1
                if state.get("active_execution_id") or self._instance_is_in_flight(
                    str(instance.get("id"))
                ):
                    continue

                node_id = str(node_ids[cursor])
                node = nodes_by_id.get(node_id)
                execution_id = deterministic_execution_id(
                    str(instance.get("id")), cursor, node_id
                )
                if execution_id in self._reported_execution_ids:
                    continue
                if self._closing.is_set():
                    break
                method_name = ""
                action_callable: Callable[..., Any] | None = None
                if node is not None:
                    try:
                        method_name = node_method(node)
                        device = devices.get(node.device_name)
                        if device is not None:
                            action_callable = getattr(
                                device, method_name, None
                            )
                    except (AttributeError, ValueError):
                        action_callable = None
                device = devices.get(node.device_name) if node is not None else None
                if (
                    node is None
                    or device is None
                    or not callable(action_callable)
                ):
                    claimed_response = self._claim(
                        current_response,
                        workflow_path=workflow_path,
                        instance_id=str(instance.get("id")),
                        node_id=node_id,
                        execution_id=execution_id,
                        resources=[],
                    )
                    if claimed_response is None:
                        continue
                    stats["claimed"] = int(stats["claimed"]) + 1
                    current_response = claimed_response
                    self._submit_terminal_report(
                        _PendingTerminalReport(
                            workflow_path=workflow_path,
                            instance_id=str(instance.get("id")),
                            node_id=node_id,
                            execution_id=execution_id,
                            error={
                                "code": "unsupported_action",
                                "message": (
                                    f"不支持的 Task 动作节点: {node_id}"
                                ),
                            },
                        ),
                        expected_version=int(current_response["version"]),
                        stats=stats,
                    )
                    break

                claimed_response = self._claim(
                    current_response,
                    workflow_path=workflow_path,
                    instance_id=str(instance.get("id")),
                    node_id=node_id,
                    execution_id=execution_id,
                    resources=[],
                )
                if claimed_response is None:
                    continue
                current_response = claimed_response
                stats["claimed"] = int(stats["claimed"]) + 1
                try:
                    future = self._executor.submit(
                        self._invoke_node_runner,
                        node,
                        devices,
                        action_callable,
                        {
                            "workflow_path": workflow_path,
                            "instance_id": str(instance.get("id")),
                            "node_id": node_id,
                            "execution_id": execution_id,
                            "sample_id": str(instance.get("sample_id") or ""),
                        },
                    )
                except Exception as exc:
                    pending = _PendingTerminalReport(
                        workflow_path=workflow_path,
                        instance_id=str(instance.get("id")),
                        node_id=node_id,
                        execution_id=execution_id,
                        error={
                            "code": "action_dispatch_failed",
                            "message": str(exc),
                        },
                    )
                    self._submit_terminal_report(
                        pending,
                        expected_version=int(current_response["version"]),
                        stats=stats,
                    )
                    break
                self._in_flight[execution_id] = _InFlightAction(
                    future=future,
                    workflow_path=workflow_path,
                    instance_id=str(instance.get("id")),
                    node_id=node_id,
                    execution_id=execution_id,
                )

            self._update_activity_stats(stats, workflow_path=workflow_path)
            return stats

    def _submit_terminal_report(
        self,
        pending: _PendingTerminalReport,
        *,
        expected_version: int,
        stats: dict[str, Any],
    ) -> bool:
        try:
            self._task_client.fail_action(
                workflow_path=pending.workflow_path,
                expected_version=expected_version,
                instance_id=pending.instance_id,
                node_id=pending.node_id,
                execution_id=pending.execution_id,
                error=pending.error,
            )
        except Exception as exc:
            pending.retryable = not (
                isinstance(exc, TaskApiConflict)
                and exc.code in self._NON_RETRYABLE_TERMINAL_CONFLICTS
            )
            pending.report_error = str(exc)
            self._pending_terminal_reports[pending.execution_id] = pending
            if not pending.retryable:
                stats["success"] = False
            return False
        self._pending_terminal_reports.pop(pending.execution_id, None)
        self._reported_execution_ids.add(pending.execution_id)
        stats["failed"] = int(stats["failed"]) + 1
        return True

    def _retry_pending_terminal_report(
        self,
        stats: dict[str, int | bool],
        *,
        workflow_path: str | None = None,
    ) -> bool:
        pending = next(
            (
                item
                for item in self._pending_terminal_reports.values()
                if workflow_path is None or item.workflow_path == workflow_path
            ),
            None,
        )
        if pending is None:
            return False
        if not pending.retryable:
            stats["success"] = False
            return True
        try:
            response = self._task_client.get_workspace(
                workflow_path=pending.workflow_path
            )
        except Exception as exc:
            pending.report_error = str(exc)
            return True
        self._submit_terminal_report(
            pending,
            expected_version=int(response["version"]),
            stats=stats,
        )
        return True

    def _update_activity_stats(
        self,
        stats: dict[str, Any],
        *,
        workflow_path: str | None = None,
    ) -> None:
        pending_count = sum(
            1
            for item in self._pending_terminal_reports.values()
            if workflow_path is None or item.workflow_path == workflow_path
        )
        stats["in_flight"] = len(self._in_flight) + pending_count
        if "active" in stats and pending_count:
            stats["active"] = max(int(stats["active"]), pending_count)

    def _recover_orphaned_execution(
        self,
        response: dict[str, Any],
        *,
        workflow_path: str,
        workspace: dict[str, Any],
        stats: dict[str, int | bool],
    ) -> bool:
        """失败关闭服务端有记录但本进程无法证明正在执行的动作。"""
        for instance in workspace.get("task_instances", []):
            if not isinstance(instance, dict) or instance.get("status") != "running":
                continue
            state = instance.get("execution_state") or {}
            execution_id = str(state.get("active_execution_id") or "")
            if not execution_id or execution_id in self._in_flight:
                continue
            node_id = str(state.get("active_node_id") or "")
            stats["active"] = int(stats["active"]) + 1
            self._submit_terminal_report(
                _PendingTerminalReport(
                    workflow_path=workflow_path,
                    instance_id=str(instance.get("id")),
                    node_id=node_id,
                    execution_id=execution_id,
                    error={
                        "code": "orphaned_execution",
                        "message": (
                            "服务端存在活动执行但本地无对应 future，无法安全恢复"
                        ),
                    },
                ),
                expected_version=int(response["version"]),
                stats=stats,
            )
            return True
        return False

    def _claim(
        self,
        response: dict[str, Any],
        *,
        workflow_path: str,
        instance_id: str,
        node_id: str,
        execution_id: str,
        resources: list[str],
    ) -> dict[str, Any] | None:
        try:
            return self._task_client.claim_action(
                workflow_path=workflow_path,
                expected_version=int(response["version"]),
                instance_id=instance_id,
                node_id=node_id,
                execution_id=execution_id,
                resources=resources,
            )
        except TaskApiConflict as exc:
            if exc.code in self._WAIT_CONFLICTS:
                return None
            raise

    def _harvest_completed(self, stats: dict[str, int | bool]) -> None:
        for execution_id, action in list(self._in_flight.items()):
            if not action.future.done():
                continue
            if not action.outcome_prepared:
                self._prepare_outcome(action)
            if action.error is None:
                try:
                    response = self._task_client.get_workspace(
                        workflow_path=action.workflow_path
                    )
                    self._task_client.succeed_action(
                        workflow_path=action.workflow_path,
                        expected_version=int(response["version"]),
                        instance_id=action.instance_id,
                        node_id=action.node_id,
                        execution_id=execution_id,
                        result=action.result_summary,
                        release_resources=[],
                    )
                except Exception:
                    continue
                stats["completed"] = int(stats["completed"]) + 1
            else:
                try:
                    response = self._task_client.get_workspace(
                        workflow_path=action.workflow_path
                    )
                    self._task_client.fail_action(
                        workflow_path=action.workflow_path,
                        expected_version=int(response["version"]),
                        instance_id=action.instance_id,
                        node_id=action.node_id,
                        execution_id=execution_id,
                        error=action.error,
                    )
                except Exception:
                    continue
                stats["failed"] = int(stats["failed"]) + 1
            self._reported_execution_ids.add(execution_id)
            del self._in_flight[execution_id]

    def _prepare_outcome(self, action: _InFlightAction) -> None:
        """只判定一次实体动作结果，后续 tick 仅重试对应终态上报。"""
        action.outcome_prepared = True
        try:
            result = action.future.result()
            summary = _json_safe(result)
            failure = _false_result(summary)
            if failure is not None:
                raise RuntimeError(f"动作返回 success=false: {failure}")
            action.result_summary = summary
        except Exception as exc:
            action.error = {
                "code": "action_failed",
                "message": str(exc),
            }

    def _instance_is_in_flight(self, instance_id: str) -> bool:
        return any(
            action.instance_id == instance_id
            for action in self._in_flight.values()
        )


def _workspace_from_response(response: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(response, dict) or not isinstance(response.get("version"), int):
        raise RuntimeError("Task API 未返回有效工作区版本")
    workspace = response.get("workspace")
    if not isinstance(workspace, dict):
        raise RuntimeError("Task API 未返回有效工作区")
    return workspace


def _false_result(
    value: Any,
    *,
    _allow_bare_false: bool = True,
) -> Any | None:
    if _allow_bare_false and type(value) is bool:
        return value if value is False else None
    if isinstance(value, dict):
        if value.get("success") is False:
            return value
        if "result" in value:
            failure = _false_result(
                value["result"],
                _allow_bare_false=True,
            )
            if failure is not None:
                return failure
        for key, nested in value.items():
            if key == "result":
                continue
            failure = _false_result(
                nested,
                _allow_bare_false=False,
            )
            if failure is not None:
                return failure
    if isinstance(value, (list, tuple)):
        if (
            _allow_bare_false
            and value
            and type(value[0]) is bool
            and value[0] is False
        ):
            return value
        for item in value:
            failure = _false_result(
                item,
                _allow_bare_false=False,
            )
            if failure is not None:
                return failure
    return None


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return repr(value)
