"""调度权威运行模式。"""

from __future__ import annotations

from enum import Enum


class SchedulerAuthorityProfile(str, Enum):
    """OS 进程对工作流任务（WorkflowTask）权威的显式运行选择。"""

    LOCAL_SCHEDULER = "local_scheduler"
    BACKEND_CONTROLLED = "backend_controlled"

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

        return self is self.LOCAL_SCHEDULER

    @property
    def can_execute_backend_command(self) -> bool:
        """本模式是否允许消费 Backend 下发的执行命令。"""

        return self is self.BACKEND_CONTROLLED

__all__ = ["SchedulerAuthorityProfile"]
