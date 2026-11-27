"""物料页分类失效通知的 SSE 适配器。"""

from __future__ import annotations

import asyncio
import json

from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from unilabos.persistence.resource_changes import ResourceChangeStore
from unilabos.workflow.event_reader import DurableEventReader


async def resource_change_stream(request: Request, store, *, scope: str) -> StreamingResponse:
    """订阅后再读取首份状态；重连携带游标，窗口过期发送 reset，不靠轮询业务快照。"""
    raw = request.headers.get("last-event-id", "")
    try:
        cursor = int(raw) if raw else None
        if raw and (not raw.isascii() or not raw.isdecimal() or not 0 <= cursor < 2**63):
            raise ValueError
    except ValueError as error:
        raise HTTPException(400, "invalid Last-Event-ID") from error
    source = ResourceChangeStore(store, scope=scope)
    if cursor is None:
        cursor = await run_in_threadpool(source.latest_event_sequence)
    reader = DurableEventReader(source)

    async def stream():
        nonlocal cursor
        yield f"retry: 3000\nid: {cursor}\n: connected\n\n"
        replay = raw == "0"
        while not await request.is_disconnected():
            page = await run_in_threadpool(reader.read_stream, after_sequence=cursor, limit=100, replay_retained=replay)
            replay = False
            cursor = page["next_sequence"]
            if page["reset_reason"]:
                yield f"id: {cursor}\nevent: stream.reset\ndata: {{}}\n\n"
            for event in page["items"]:
                yield f"id: {event['id']}\nevent: {event['event']}\ndata: {json.dumps(event['data'])}\n\n"
            if not page["items"]:
                yield ": keepalive\n\n"
            await asyncio.sleep(0 if page["has_more"] else 0.25)

    return StreamingResponse(
        stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )
