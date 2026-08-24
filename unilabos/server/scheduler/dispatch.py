"""job 下发接口。

service 层产出「该启动的节点 + 解析后的参数」，由 Dispatcher 落地执行：

- ``CallbackDispatcher``：回调函数适配器，消费 Backend ``execute_job``
  载荷（device_id/action/action_type/action_args/job_id/task_id）。
- ``RecordingDispatcher``：测试/干跑用，记录下发序列。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Protocol


class DispatchPayload(Dict[str, Any]):
    """Backend ``execute_job`` 的执行器下发载荷。"""


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
    inventory_requirements: Optional[List[Dict[str, Any]]] = None,
    inventory_reservation_uuid: Optional[str] = None,
    scheduler_revision: int = 0,
) -> DispatchPayload:
    """构造执行器消费的 Backend ``execute_job`` 载荷。"""
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
        inventory_requirements=list(inventory_requirements or []),
        inventory_reservation_uuid=inventory_reservation_uuid,
        scheduler_revision=scheduler_revision,
        sample_material={},
    )


__all__ = [
    "CallbackDispatcher",
    "DispatchPayload",
    "Dispatcher",
    "RecordingDispatcher",
    "build_job_start_payload",
]
