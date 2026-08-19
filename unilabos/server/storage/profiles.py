"""调度权威运行模式（SchedulerAuthorityProfile）及启动选择规则。"""

from __future__ import annotations

from enum import Enum


class SchedulerAuthorityConflict(RuntimeError):
    """启动配置会形成双调度权威（Scheduler Authority）。"""


class SchedulerAuthorityProfile(str, Enum):
    """OS 进程对工作流任务（WorkflowTask）权威的显式运行选择。"""

    LOCAL_SCHEDULER = "local_scheduler"
    BACKEND_CONTROLLED = "backend_controlled"
    OFFLINE_RECOVERY = "offline_recovery"

    @classmethod
    def parse(
        cls,
        value: str | SchedulerAuthorityProfile,
    ) -> SchedulerAuthorityProfile:
        """把线格式值解析为规范运行模式，拒绝模糊或未知取值。"""

        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).strip())
        except ValueError as error:
            allowed = ", ".join(item.value for item in cls)
            raise ValueError(
                f"invalid SchedulerAuthorityProfile {value!r}; expected one of: {allowed}"
            ) from error

    @property
    def can_create_local_workflow_task(self) -> bool:
        """本模式是否拥有创建本地可执行工作流任务的权威。"""

        return self is self.LOCAL_SCHEDULER

    @property
    def can_recover_local_workflow_task(self) -> bool:
        """本模式是否允许恢复已经持久化的本地工作流任务。"""

        return self in {self.LOCAL_SCHEDULER, self.OFFLINE_RECOVERY}

    @property
    def can_execute_backend_command(self) -> bool:
        """本模式是否允许消费 Backend 下发的执行命令。"""

        return self is self.BACKEND_CONTROLLED

    @property
    def opens_local_inventory_authority(self) -> bool:
        """本模式是否应打开本地库存权威存储。"""

        return self in {self.LOCAL_SCHEDULER, self.OFFLINE_RECOVERY}


def select_scheduler_authority_profile(
    value: str | SchedulerAuthorityProfile | None,
) -> SchedulerAuthorityProfile:
    """从启动参数确定唯一调度权威档位。

    ``value`` 为空时选择本地调度；后端受控模式由组合根显式传入。
    """

    if value is None or not str(value).strip():
        return SchedulerAuthorityProfile.LOCAL_SCHEDULER
    return SchedulerAuthorityProfile.parse(value)


__all__ = [
    "SchedulerAuthorityConflict",
    "SchedulerAuthorityProfile",
    "select_scheduler_authority_profile",
]
