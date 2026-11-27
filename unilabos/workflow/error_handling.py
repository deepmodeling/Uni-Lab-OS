"""工站错误处置的应用入口，组合持久控制、既有单动作和调度桥。"""

from __future__ import annotations

import logging
from typing import Any

from unilabos.workflow.device_action_run import DeviceActionRunService
from unilabos.workflow.error_control import StationExecutionControl
from unilabos.workflow.store import WorkflowStore
from unilabos.workflow.task_scheduler_bridge import TaskSchedulerBridge


class ErrorHandlingService:
    def __init__(
        self,
        store: WorkflowStore,
        bridge: TaskSchedulerBridge,
        actions: DeviceActionRunService,
    ) -> None:
        self.store = store
        self.bridge = bridge
        self.actions = actions

    @property
    def control(self) -> StationExecutionControl:
        """使用调度桥拥有的控制器，组合服务时不提前读取运行组件。"""
        return self.bridge.error_control

    def manual_actions(self, session_id: str, device_id: str) -> list[dict[str, Any]]:
        self.control.get_session(session_id)
        return self.actions.list_manual_actions(device_id)

    def execute_manual(
        self,
        session_id: str,
        request: dict[str, Any],
        *,
        idempotency_key: str,
        operator_id: str,
    ) -> dict[str, Any]:
        accepted = self.control.accept_manual(
            session_id,
            request,
            idempotency_key=idempotency_key,
            operator_id=operator_id,
            prepare=self.actions.prepare_manual,
        )
        return self.apply_command(accepted["command_uuid"])

    def apply_command(self, command_uuid: str) -> dict[str, Any]:
        """恢复已受理命令，只复用保存的执行身份，不创建新任务或新选择。"""
        accepted = self.control.get_command(command_uuid)
        if (
            "manual_command_id" not in accepted["result"]
            or "execution_task_uuid" not in accepted["result"]
        ):
            return self.bridge.apply_error_command(accepted)
        # 重放也补齐已保存但尚未提交的同一任务，桥内按持久身份防止重复发送。
        task = self.store.get_task(accepted["result"]["execution_task_uuid"])
        dispatch_error = None
        if task["status"] == "pending":
            try:
                self.bridge.submit(task)
            except Exception as error:
                # 请求已持久受理；执行故障返回原命令，不能让客户端误建另一动作。
                logging.getLogger(__name__).exception(
                    "已受理手动命令派发失败，保留原身份"
                )
                dispatch_error = str(error)
        result = self.control.get_command(accepted["command_uuid"])
        return {
            **result,
            "dispatch_error": dispatch_error,
            "manual_action": self.control.manual_action(
                accepted["result"]["manual_command_id"]
            ),
        }

    def complete(
        self,
        session_id: str,
        request: dict[str, Any],
        *,
        idempotency_key: str,
        operator_id: str,
    ) -> dict[str, Any]:
        return self.control.complete(
            session_id,
            request,
            idempotency_key=idempotency_key,
            operator_id=operator_id,
        )

    def stop_manual(
        self,
        command_id: str,
        request: dict[str, Any],
        *,
        idempotency_key: str,
        operator_id: str,
    ) -> dict[str, Any]:
        accepted = self.control.accept_stop(
            command_id,
            request,
            idempotency_key=idempotency_key,
            operator_id=operator_id,
        )
        result = self.bridge.apply_error_command(accepted)
        return {**result, "manual_action": self.control.manual_action(command_id)}

    def resume(
        self, request: dict[str, Any], *, idempotency_key: str, operator_id: str
    ) -> dict[str, Any]:
        result = self.control.resume(
            request, idempotency_key=idempotency_key, operator_id=operator_id
        )
        self.bridge.reschedule()
        return result
