"""job 下发接口。

service 层产出「该启动的节点 + 解析后的参数」，由 Dispatcher 落地执行：

- ``CallbackDispatcher``：回调函数适配器。接 ws_client 时把回调指向
  MessageProcessor._handle_job_start 同款载荷（device_id/action/action_type/
  action_args/job_id/task_id），即可复用微后端队列与执行适配器。
- ``RecordingDispatcher``：测试/干跑用，记录下发序列。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Protocol


class DispatchPayload(Dict[str, Any]):
    """job_start 形状的下发载荷（与 ws_client JobAddReq 字段对齐）。"""


class Dispatcher(Protocol):
    def dispatch(self, payload: DispatchPayload) -> None:
        ...


class CallbackDispatcher:
    def __init__(self, fn: Callable[[DispatchPayload], None]):
        self._fn = fn

    def dispatch(self, payload: DispatchPayload) -> None:
        self._fn(payload)


class RecordingDispatcher:
    def __init__(self) -> None:
        self.dispatched: List[DispatchPayload] = []

    def dispatch(self, payload: DispatchPayload) -> None:
        self.dispatched.append(payload)


def build_job_start_payload(
    job_id: str,
    task_id: str,
    workflow_id: str,
    node_id: str,
    device_id: str,
    action_name: str,
    action_type: str,
    action_args: Any,
    materials_need_lock: Optional[List[str]] = None,
) -> DispatchPayload:
    """与云端 job_start 消息同形状（engine.SendActionData / ws_client JobAddReq）。"""
    return DispatchPayload(
        job_id=job_id,
        task_id=task_id,
        node_id=node_id,
        workflow_id=workflow_id,
        device_id=device_id,
        action=action_name,
        action_type=action_type,
        action_args=action_args,
        materials_need_lock=list(materials_need_lock or []),
        sample_material={},
    )


__all__ = [
    "CallbackDispatcher",
    "DispatchPayload",
    "Dispatcher",
    "RecordingDispatcher",
    "build_job_start_payload",
]
