"""生命周期排空：复用调度器派发闸门和当前唯一主 runtime 的在途状态。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from unilabos.protocol.runtime.lifecycle import SchedulerDrainStatus


class SchedulerUnavailable(RuntimeError):
    """当前进程没有调度权威，不能在本机执行排空。"""


def active_device_job_ids(scheduler: Any, backend: Any) -> list[str]:
    """合并执行端状态与调度端已获许可/待裁决的设备作业。

    调度端集合覆盖获得派发许可到执行端登记之间的窗口；执行态未知的
    attempt 仍需裁决，不能仅因 Host 断连就被当成已排空。
    """

    jobs: set[str] = set()
    if backend is not None:
        manager = getattr(backend, "device_manager", None)
        if manager is not None:
            jobs.update(job.job_id for job in manager.get_active_jobs())
    else:
        from unilabos.server.backend.edge_control import get_edge_control_service

        service = get_edge_control_service()
        if service is not None:
            jobs.update(service.active_job_ids())
    snapshot = getattr(scheduler, "active_device_job_ids", None)
    if callable(snapshot):
        jobs.update(snapshot())
    return sorted(jobs)


class SchedulerLifecycle:
    """无额外进程或状态库；暂停来源保存在唯一的 BackendScheduler 上。"""

    def __init__(
        self,
        get_scheduler: Callable[[], Any],
        get_execution_backend: Callable[[], Any],
    ) -> None:
        self._get_scheduler = get_scheduler
        self._get_execution_backend = get_execution_backend

    def _scheduler(self) -> Any:
        scheduler = self._get_scheduler()
        if scheduler is None:
            raise SchedulerUnavailable("本进程没有调度权威，请连接 backend 的管理地址")
        return scheduler

    def status(self) -> SchedulerDrainStatus:
        scheduler = self._scheduler()
        reasons = list(scheduler.dispatch_pause_reasons)
        jobs = active_device_job_ids(scheduler, self._get_execution_backend())
        return SchedulerDrainStatus(
            phase="running" if not reasons else ("draining" if jobs else "drained"),
            active_device_job_count=len(jobs),
            active_device_job_ids=jobs,
            requested="drain" in reasons,
            dispatch_paused=bool(reasons),
            pause_reasons=reasons,
        )

    def begin_drain(self) -> SchedulerDrainStatus:
        self._scheduler().pause_dispatch(reason="drain")
        return self.status()

    def resume(self) -> SchedulerDrainStatus:
        self._scheduler().resume_dispatch(reason="drain")
        return self.status()
