"""V1 调度观测路由：``POST /reschedule``、``GET /timeline``、``GET /scheduler/time-plan``、
``GET /monitor/events``（SSE）、``GET /monitor/snapshot``。口径见 ``services/runtime/workflow/timeline.py``。"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any, Callable, Dict, Literal, Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from unilabos.server.api.envelope import GENERIC, SERVICE_UNAVAILABLE, ApiError, envelope_router
from unilabos.server.services.runtime.workflow.timeline import CHANNELS, ExecutionTimeline


def _default_scheduler() -> Any:
    from unilabos.server.backend.composition import get_scheduler

    return get_scheduler()


def create_scheduling_router(service: Any, *, get_scheduler: Optional[Callable[[], Any]] = None) -> APIRouter:
    router = envelope_router(profile=GENERIC, prefix="/api/v1", tags=["scheduler"])
    timeline = ExecutionTimeline(service)
    scheduler_getter = get_scheduler or (lambda: _default_scheduler())
    counter = {"reschedule": 0}
    lock = threading.Lock()

    def in_flight() -> set[str]:
        with service.write_lock:
            return {row[0] for row in service.connection.execute(
                "SELECT uuid FROM workflow_node_job WHERE deleted_at IS NULL "
                "AND status IN ('dispatched', 'running', 'cancel_requested')"
            ).fetchall()}

    @router.post("/reschedule", summary="手动重排（调试用）")
    def manual_reschedule() -> Dict[str, Any]:
        """重新判定全部等待派发的作业（资源队列与暂停闸门），返回本次同步观察到的新派发。"""

        scheduler = scheduler_getter()
        if scheduler is None:
            raise ApiError(SERVICE_UNAVAILABLE, "scheduler authority is remote")
        before = in_flight()
        reconcile = getattr(scheduler, "_reconcile_resources", None)
        if callable(reconcile):
            reconcile()
        controller = getattr(service, "_task_controller", None)
        if controller is not None:
            with service.write_lock:
                tasks = [row[0] for row in service.connection.execute(
                    "SELECT uuid FROM workflow_task WHERE deleted_at IS NULL AND status IN ('pending', 'running')"
                ).fetchall()]
            for task_uuid in tasks:
                controller(task_uuid)
        with lock:
            counter["reschedule"] += 1
        return {"dispatched": sorted(in_flight() - before)}

    @router.get("/timeline", summary="泳道图数据")
    def get_timeline(window_s: float = Query(default=3600.0)) -> Dict[str, Any]:
        """执行中作业 + 窗口内已完结作业的起止与实测预估，附预估器统计与时间计划状态。"""

        return timeline.timeline(window_s=window_s)

    @router.get("/scheduler/time-plan", summary="读取时间排程计划")
    def time_plan() -> Dict[str, Any]:
        """未配置时间计划引擎：按 V1 同情形返回 ``enabled=false``，候选时间不是启动授权。"""

        return timeline.time_plan()

    @router.get("/monitor/snapshot", summary="监控一次性快照")
    def monitor_snapshot() -> Dict[str, Any]:
        with lock:
            count = counter["reschedule"]
        return timeline.snapshot(reschedule_count=count)

    @router.get("/monitor/events", summary="监控 SSE 事件流（四通道）")
    async def monitor_events(
        request: Request,
        channels: str = Query(default=""),
        backlog: int = Query(default=40),
        follow: bool = Query(default=True),
    ) -> StreamingResponse:
        """``channels`` 逗号分隔过滤（缺省全部）；先回放最近 ``backlog`` 条，再推新事件。"""

        requested = {item.strip() for item in channels.split(",") if item.strip()}
        selected = tuple(sorted((requested & set(CHANNELS)) or CHANNELS))
        replay = timeline.backlog(selected, max(0, min(backlog, 200)))
        cursor = timeline.latest_sequence()

        def frame(event: Dict[str, Any]) -> str:
            payload = json.dumps(event, ensure_ascii=False, default=str)
            return f"id: {event['seq']}\nevent: {event['channel']}\ndata: {payload}\n\n"

        async def stream():
            nonlocal cursor
            yield f"retry: 3000\n: connected channels={','.join(selected)}\n\n"
            for event in replay:
                yield frame(event)
            if not follow:
                return
            last_beat = time.time()
            while not await request.is_disconnected():
                events = await asyncio.to_thread(timeline.monitor_events, after=cursor, channels=selected)
                latest = await asyncio.to_thread(timeline.latest_sequence)
                for event in events:
                    yield frame(event)
                cursor = max([cursor, latest, *[event["seq"] for event in events]])
                if events:
                    last_beat = time.time()
                elif time.time() - last_beat > 15:
                    yield ": keepalive\n\n"
                    last_beat = time.time()
                await asyncio.sleep(0.4)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @router.post("/workflow-node-jobs/{job_uuid}/settle-material-transfer", summary="失败转运的实际位置结算")
    def settle_failed_material_transfer(job_uuid: str, body: FailedMaterialTransferSettlementRequest) -> Dict[str, Any]:
        """V1：操作员核实失败转运的实际库位后完成物理结算（作业保持失败主状态，释放已结算占用）。

        落在 dev 的物理恢复（``/scheduler/physical-recovery/{job}/settle``）上：版本取当前冻结记录，
        ``actual_change_set`` 换成一条搬运回执。
        """

        from unilabos.server.services.runtime.workflow.service import WorkflowError

        job = service.get_workflow_node_job(job_uuid)
        change = body.actual_change_set
        scheduler = scheduler_getter()
        recovery = getattr(scheduler, "_physical_recovery", None) if scheduler is not None else None
        if recovery is None:
            raise ApiError(SERVICE_UNAVAILABLE, "scheduler authority is remote")
        record = recovery.get(job["uuid"])
        if record is None:
            raise WorkflowError("not_found", detail="该作业没有待结算的物理动作")
        receipt = {"material_transfer_receipts": [{
            "material_uuid": change.material_uuid, "target_owner_uuid": change.target_owner_material_uuid,
            "target_site_uuid": change.target_site_uuid,
        }]}
        try:
            result = recovery.confirm_terminal_settlement(job["uuid"], record["version"], body.reason, receipt)
        except (ValueError, RuntimeError) as exc:
            raise WorkflowError("conflict", detail=str(exc)) from exc
        return {"job_uuid": job["uuid"], "settlement": result.model_dump(mode="json")
                if hasattr(result, "model_dump") else result}

    return router


class MaterialTransferChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["material_transfer"]
    material_uuid: str = Field(min_length=1)
    target_owner_material_uuid: str = Field(min_length=1)
    target_site_uuid: str = Field(min_length=1)


class FailedMaterialTransferSettlementRequest(BaseModel):
    """V1：失败转运在设备停止后提交的实际物料位置。"""

    model_config = ConfigDict(extra="forbid")
    actual_change_set: MaterialTransferChange
    reason: str = Field(min_length=1)


__all__ = ["create_scheduling_router"]
