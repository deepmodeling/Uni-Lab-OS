"""``/api/v1/events``：一个整数序列上的前端失效通知（工作流、物料、日志），每一帧都带 id。

订阅语义对齐 v1.0 ADR 0006（前端事件订阅）：通知只表示 REST 状态失效，新页面经
REST 读取当前状态，不需要回放历史。

- 缺少或空白 ``Last-Event-ID``：在发送响应头之前固定最新持久序号，先发一个只含
  ``id`` 的水位帧，此后只推新通知；
- 显式 ``Last-Event-ID: 0``：回放当前保留的全部历史；其他合法游标从其之后续传；
- 游标大于最新序号（例如数据库被重置）发送 ``stream.reset``（``reason`` 为
  ``cursor_ahead``）；游标早于保留窗口（被保留清理越过）发送 ``cursor_expired``。
  ``id`` 为新水位，连接保持，客户端必须重读 REST。

保留窗口边界与事件页在同一 SQL 快照内读取，每页都检查，慢连接也不会被清理静默越过。

所有通知都是 ``frontend_event`` 的行，同一个序列：

- 事件名按 V1 合同输出（:func:`to_v1_event`）：持久库里 dev 的细粒度事件
  ``workflow.task|node_run|node_job|manual_confirmation|resource.changed`` 一律发成
  ``workflow.runtime.changed``，原事件的种类放进 ``data.kind``（``task`` / ``node_run`` /
  ``node_job`` / ``manual_confirmation`` / ``resource``），原载荷字段保留。
  ``workflow.authoring.changed`` 在库里就是 V1 名，原样发送：``@workflow`` 源码创作的状态迁移
  （``data.cause``），以及工作流定义的变化（``data.change``：``created`` / ``updated`` / ``deleted`` /
  ``graph_saved`` / ``published``），都与变更同一事务写入。
- ``material.changed``（``data: {"sequence": <物料账本末尾>, "material_uuids": [...]}``）与
  ``runtime.logs.changed``（``RuntimeLogNotice``）由权威进程的通知写入器合并、限速后写入
  （``server/services/runtime/workflow/frontend_notices.py``）。``material_uuids`` 带这个键：恰好是这段账本里
  变了的物料，客户端只重读这些（空列表就是没有物料变化）；不带：列不准，整体重读。``data.sequence`` 是物料账本
  的序号，不是这条流的游标。

写入器写完一行会唤醒本进程的连接立即读；工作流事件按约 1 秒一轮读取。
"""

from __future__ import annotations

import asyncio
import time

from typing import Any, Dict

from fastapi import APIRouter, Header, Request
from fastapi.responses import Response, StreamingResponse

from unilabos.server.api.envelope import WORKFLOW, envelope_router
from unilabos.server.lifecycle import shutting_down
from unilabos.server.services.runtime.workflow.frontend_notices import MATERIAL_CHANGED_EVENT, appended
from unilabos.utils.log_notices import LOG_NOTICE_EVENT

STREAM_RESET_EVENT = "stream.reset"
#: V1 前端事件名（``/api/v1/events``）
RUNTIME_CHANGED_EVENT = "workflow.runtime.changed"
AUTHORING_CHANGED_EVENT = "workflow.authoring.changed"
ERROR_HANDLING_CHANGED_EVENT = "station.error_handling.changed"

#: dev 持久事件名 → (V1 事件名, data.kind)。不在表里的事件名原样发送。
_V1_EVENTS: Dict[str, tuple[str, str | None]] = {
    "workflow.task.changed": (RUNTIME_CHANGED_EVENT, "task"),
    "workflow.node_run.changed": (RUNTIME_CHANGED_EVENT, "node_run"),
    "workflow.node_job.changed": (RUNTIME_CHANGED_EVENT, "node_job"),
    "workflow.manual_confirmation.changed": (RUNTIME_CHANGED_EVENT, "manual_confirmation"),
    "workflow.resource.changed": (RUNTIME_CHANGED_EVENT, "resource"),
}


def to_v1_event(event: Dict[str, Any]) -> Dict[str, Any]:
    """持久通知 → V1 事件名；dev 细粒度种类放进 ``data.kind``，其余载荷字段不动。"""

    mapped = _V1_EVENTS.get(str(event.get("event") or ""))
    if mapped is None:
        return event
    name, kind = mapped
    data = dict(event.get("data") or {}) if isinstance(event.get("data"), dict) else {"value": event.get("data")}
    if kind is not None:
        data["kind"] = kind
    return {**event, "event": name, "data": data}


async def runtime_event_stream(
    request: Request,
    service=None,
    cursor: int = 0,
    *,
    watermark: bool = False,
    replay: bool = False,
):
    from unilabos.server.api.runtime.workflow import format_sse_event

    loop = asyncio.get_running_loop()
    wake = asyncio.Event()
    closed = False

    def woken() -> None:
        # 写入器线程上调用：只把唤醒交回事件循环
        if not closed:
            try:
                loop.call_soon_threadsafe(wake.set)
            except RuntimeError:  # 事件循环已关闭
                pass

    release = appended.subscribe(woken)
    next_poll = 0.0
    heartbeat = time.monotonic() + 15
    try:
        yield "retry: 3000\n: connected\n\n"
        if watermark:
            # 只设置浏览器续传水位，不触发业务失效；空闲连接断线后也能从这里续传。
            yield f"id: {cursor}\n\n"
        while not shutting_down() and not await request.is_disconnected():
            now = time.monotonic()
            if now >= next_poll:
                next_poll = now + 1
                if service is not None:
                    oldest, latest, events = service.read_event_window(after_id=cursor, limit=101)
                    reason = None
                    if cursor > latest:
                        reason = "cursor_ahead"
                    elif cursor < oldest - 1 and not (replay and cursor == 0):
                        reason = "cursor_expired"
                    # 只有显式从 0 开始的首轮允许回放保留窗口，之后按普通游标检查。
                    replay = False
                    if reason is not None:
                        # 无法续传：给出新水位，由客户端重读 REST 校准。
                        cursor = latest
                        yield format_sse_event({"id": cursor, "event": STREAM_RESET_EVENT, "data": {"reason": reason}})
                        next_poll = now
                        continue
                    for event in events[:100]:
                        cursor = event["id"]
                        yield format_sse_event(to_v1_event(event))
                    if len(events) > 100:
                        next_poll = now
            try:
                await asyncio.wait_for(wake.wait(), timeout=max(.01, next_poll - time.monotonic()))
            except TimeoutError:
                if time.monotonic() >= heartbeat:
                    yield ": keepalive\n\n"
                    heartbeat = time.monotonic() + 15
                continue
            # 写入器刚追加了通知：马上读，不等下一轮
            wake.clear()
            next_poll = 0.0
    finally:
        closed = True
        release()


def create_runtime_events_router(service=None) -> APIRouter:
    # SSE 流本身不包信封；游标非法等失败仍回工作流口径的失败信封
    router = envelope_router(tags=["runtime-events"], profile=WORKFLOW)

    @router.get("/events", name="runtime_events")
    async def events(
        request: Request,
        last_event_id: str | None = Header(
            default=None,
            alias="Last-Event-ID",
            description="不填时只接收新通知；填 0 回放保留的历史，其他序号从其之后续传。",
        ),
    ) -> Response:
        from unilabos.server.api.runtime.workflow import _GO_WHITE_SPACE, _error, _parse_non_negative_int64_decimal
        from unilabos.server.services.runtime.workflow.service import WorkflowError

        try:
            raw = next((value for name, value in request.scope["headers"] if name.lower() == b"last-event-id"), None)
            value = (raw.decode("utf-8") if raw is not None else last_event_id or "").strip(_GO_WHITE_SPACE)
            cursor = _parse_non_negative_int64_decimal(value) if value else None
        except (ValueError, UnicodeError):
            return _error(WorkflowError("invalid_input"))
        fresh = cursor is None
        if fresh:
            # 必须在发送响应头之前固定水位，客户端 onopen 后的 REST 补读才能覆盖
            # “快照 → 订阅”之间的变化。纯执行面的 Host 没有 Workflow Authority，也就没有这条序列。
            cursor = service.latest_event_sequence() if service is not None else 0
        return StreamingResponse(
            runtime_event_stream(
                request,
                service,
                cursor,
                watermark=fresh and service is not None,
                replay=bool(value) and cursor == 0,
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return router


__all__ = [
    "AUTHORING_CHANGED_EVENT",
    "ERROR_HANDLING_CHANGED_EVENT",
    "LOG_NOTICE_EVENT",
    "MATERIAL_CHANGED_EVENT",
    "RUNTIME_CHANGED_EVENT",
    "STREAM_RESET_EVENT",
    "create_runtime_events_router",
    "runtime_event_stream",
    "to_v1_event",
]
