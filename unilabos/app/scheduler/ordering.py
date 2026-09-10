"""OS 本地调度器（Scheduler）的稳定任务排序接口。

入参 = ready tasks + 设备锁状态 + 优先级；出参 = 有序 task 列表。
``StableLocalOrderer`` 先按设备锁是否可准入，再按资源解阻类别、权重降序、提交
时间升序和稳定节点身份排序；OS 不调用独立 ``uni-lab-scheduler`` 服务。
"""

from __future__ import annotations

from typing import List, Protocol, Set

from unilabos.app.scheduler.models import ReadyTask


class OrderingContext:
    """一次重排的资源上下文。"""

    def __init__(
        self,
        busy_device_action_keys: Set[str],
        busy_resource_lock_keys: Set[str] | None = None,
    ):
        # 当前被占用的 device_action_key（已下发未完结 job 持有的锁）
        self.busy_device_action_keys = busy_device_action_keys
        # 当前被占用的物料/库位等共享资源锁。
        self.busy_resource_lock_keys = busy_resource_lock_keys or set()

    def device_is_busy(self, task: ReadyTask) -> bool:
        """返回 task 的动作锁或设备锁是否已被占用。

        设备锁是 priority 的硬前置条件：priority 只能比较当前可准入的
        candidates，不能把一个已被在途作业占用的设备“排”出来。
        """

        return (
            task.node.device_action_key in self.busy_device_action_keys
            or task.node.device_lock_key in self.busy_device_action_keys
        )

    def resource_is_busy(self, task: ReadyTask) -> bool:
        """返回 task 是否等待已持有的物料或库位锁。"""

        return bool(
            set(getattr(task, "resource_lock_keys", ()) or ())
            & self.busy_resource_lock_keys
        )


class TaskOrderer(Protocol):
    def order(self, ready: List[ReadyTask], ctx: OrderingContext) -> List[ReadyTask]:
        """返回下发顺序（可含全部 ready；service 层负责跳过锁忙的节点）。"""
        ...


class StableLocalOrderer:
    """稳定排序：设备锁 → 物料/库位锁 → 资源解阻 → 权重 → 提交时间。

        ``device_busy`` 和 ``resource_busy`` 是硬准入条件；它们只是把已经
        不可准入的候选放到队尾，service 层仍会再次检查并跳过它们。资源解阻
        动作只在设备锁、物料锁都可准入时领先所有用户 priority。
    """

    def order(self, ready: List[ReadyTask], ctx: OrderingContext) -> List[ReadyTask]:
        return sorted(
            ready,
            key=lambda t: (
                ctx.device_is_busy(t),
                ctx.resource_is_busy(t),
                not t.is_resource_unblocking,
                -t.priority_weight,
                t.submitted_at,
                t.workflow_id,
                t.node.id,
            ),
        )


__all__ = [
    "OrderingContext",
    "StableLocalOrderer",
    "TaskOrderer",
]
