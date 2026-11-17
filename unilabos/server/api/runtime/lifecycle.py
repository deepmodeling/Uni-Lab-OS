"""调度权威的生命周期入口，沿用 V1 Scheduler 排空路径。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException

from unilabos.server.api.envelope import envelope_router
from unilabos.protocol.runtime.lifecycle import SchedulerDrainStatus
from unilabos.server.backend.lifecycle import SchedulerLifecycle, SchedulerUnavailable


def create_scheduler_lifecycle_router(
    get_scheduler: Callable[[], Any],
    get_execution_backend: Callable[[], Any],
) -> APIRouter:
    router = envelope_router()
    lifecycle = SchedulerLifecycle(get_scheduler, get_execution_backend)

    def call(operation: Callable[[], SchedulerDrainStatus]) -> SchedulerDrainStatus:
        try:
            return operation()
        except SchedulerUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.get("/scheduler/drain", response_model=SchedulerDrainStatus)
    def drain_status() -> SchedulerDrainStatus:
        """只读查询；没有调度器时拒绝，不能把未知状态当作已排空。"""
        return call(lifecycle.status)

    @router.post("/scheduler/drain", response_model=SchedulerDrainStatus)
    def begin_drain() -> SchedulerDrainStatus:
        """幂等停止新派发，保留在途设备作业及排队中的任务。"""
        return call(lifecycle.begin_drain)

    @router.post("/scheduler/drain/resume", response_model=SchedulerDrainStatus)
    def resume_from_drain() -> SchedulerDrainStatus:
        """解除显式排空；重启、重置或手工暂停仍保留。"""
        return call(lifecycle.resume)

    return router
