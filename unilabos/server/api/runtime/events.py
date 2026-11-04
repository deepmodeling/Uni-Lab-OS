"""复用 /events：持久工作流通知 + 瞬时日志失效通知，不混用游标。

订阅语义对齐 v1.0 ADR 0006（前端事件订阅）：通知只表示 REST 状态失效，新页面经
REST 读取当前状态，不需要回放历史。

- 缺少或空白 ``Last-Event-ID``：在发送响应头之前固定最新持久序号，先发一个只含
  ``id`` 的水位帧，此后只推新通知；
- 显式 ``Last-Event-ID: 0``：回放当前保留的全部历史；其他合法游标从其之后续传；
- 游标大于最新序号（例如数据库被重置）发送 ``stream.reset``（``reason`` 为
  ``cursor_ahead``）；游标早于保留窗口（被保留清理越过）发送 ``cursor_expired``。
  ``id`` 为新水位，连接保持，客户端必须重读 REST。

保留窗口边界与事件页在同一 SQL 快照内读取，每页都检查，慢连接也不会被清理静默越过。
"""

from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, Header, Request
from fastapi.responses import Response, StreamingResponse

from unilabos.protocol.runtime.logs import RuntimeLogNotice
from unilabos.server.lifecycle import shutting_down
from unilabos.utils.log_notices import LOG_NOTICE_EVENT, log_notices, merge_notices

STREAM_RESET_EVENT = "stream.reset"


async def runtime_event_stream(
    request: Request, service=None, cursor: int = 0, *, watermark: bool = False, replay: bool = False
):
    from unilabos.server.api.runtime.workflow import format_sse_event

    loop = asyncio.get_running_loop()
    pending: asyncio.Queue[RuntimeLogNotice] = asyncio.Queue(maxsize=1)
    closed = False

    def put(notice: RuntimeLogNotice):
        if closed:
            return
        if pending.full():
            notice = merge_notices(pending.get_nowait(), notice)
        pending.put_nowait(notice)

    def receive(notice: RuntimeLogNotice):
        if not closed:
            loop.call_soon_threadsafe(put, notice)

    release = log_notices.subscribe(receive)
    next_workflow = 0.0
    heartbeat = time.monotonic() + 15
    try:
        yield "retry: 3000\n: connected\n\n"
        if watermark:
            # 只设置浏览器续传水位，不触发业务失效；空闲连接断线后也能从这里续传。
            yield f"id: {cursor}\n\n"
        while not shutting_down() and not await request.is_disconnected():
            now = time.monotonic()
            if now >= next_workflow:
                next_workflow = now + 1
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
                        next_workflow = now
                        continue
                    for event in events[:100]:
                        cursor = event["id"]
                        yield format_sse_event(event)
                    if len(events) > 100:
                        next_workflow = now
            try:
                notice = await asyncio.wait_for(pending.get(), timeout=max(.01, next_workflow - time.monotonic()))
            except TimeoutError:
                if time.monotonic() >= heartbeat:
                    yield ": keepalive\n\n"
                    heartbeat = time.monotonic() + 15
                continue
            # 不写 SSE id：Last-Event-ID 继续只表示工作流持久事件的序号。
            yield f"event: {LOG_NOTICE_EVENT}\ndata: {notice.model_dump_json()}\n\n"
    finally:
        closed = True
        release()


def create_runtime_events_router(service=None) -> APIRouter:
    router = APIRouter(tags=["runtime-events"])

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
            # “快照 → 订阅”之间的变化。纯执行面的 Host 没有 Workflow Authority。
            cursor = service.latest_event_sequence() if service is not None else 0
        return StreamingResponse(
            runtime_event_stream(
                request, service, cursor, watermark=fresh and service is not None, replay=bool(value) and cursor == 0
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return router
