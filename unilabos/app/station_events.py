"""一个 HTTP 订阅承载工站三类通知，持久游标仍按来源独立推进。"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import time
from dataclasses import dataclass
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from unilabos.persistence.resource_changes import ResourceChangeStore
from unilabos.workflow.event_reader import MAX_EVENT_SEQUENCE, DurableEventReader

SCOPES = ("runtime", "inventory", "workflow")


@dataclass(frozen=True)
class StationEventSource:
    """适配既有来源；只暴露水位及带保留窗口的增量读取。"""

    latest: Callable[[], int]
    read: Callable[..., dict[str, Any]]


def encode_cursor(station_id: str, cursors: dict[str, int]) -> str:
    payload = json.dumps(
        {"v": 1, "station": station_id, "cursors": cursors},
        separators=(",", ":"), ensure_ascii=True,
    ).encode("ascii")
    return "v1." + base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_cursor(value: str, station_id: str) -> dict[str, int]:
    """严格拒绝跨工站、来源缺失、未知版本和非法整数，避免错误跳过通知。"""
    try:
        if not value.startswith("v1.") or len(value) > 2048:
            raise ValueError
        raw = value[3:]
        def unique_object(pairs):
            result = {}
            for key, item in pairs:
                if key in result:
                    raise ValueError("duplicate cursor field")
                result[key] = item
            return result

        payload = json.loads(
            base64.b64decode(raw + "=" * (-len(raw) % 4), altchars=b"-_", validate=True),
            object_pairs_hook=unique_object,
        )
        if (
            not isinstance(payload, dict)
            or set(payload) != {"v", "station", "cursors"}
            or type(payload["v"]) is not int or payload["v"] != 1
            or payload["station"] != station_id
        ):
            raise ValueError
        cursors = payload["cursors"]
        if not isinstance(cursors, dict) or set(cursors) != set(SCOPES):
            raise ValueError
        if any(type(value) is not int or not 0 <= value <= MAX_EVENT_SEQUENCE for value in cursors.values()):
            raise ValueError
        return cursors
    except (ValueError, TypeError, KeyError, UnicodeError, binascii.Error):
        raise HTTPException(400, "invalid station Last-Event-ID") from None


async def station_event_stream(
    request: Request, station_id: str, sources: dict[str, StationEventSource],
    *, poll_interval: float = 0.25, heartbeat_interval: float = 15,
) -> StreamingResponse:
    """先固定所有来源水位再开放订阅；每个输出帧只确认已输出的来源序号。"""
    headers = request.headers.getlist("last-event-id")
    if len(headers) > 1:
        raise HTTPException(400, "multiple Last-Event-ID headers")
    raw = headers[0] if headers else ""
    cursors = decode_cursor(raw, station_id) if raw else {
        scope: await run_in_threadpool(sources[scope].latest) for scope in SCOPES
    }

    async def stream():
        yield f"retry: 3000\nid: {encode_cursor(station_id, cursors)}\n: connected\n\n"
        heartbeat_at = time.monotonic()
        while not await request.is_disconnected():
            has_more = False
            for scope in SCOPES:
                page = await run_in_threadpool(
                    sources[scope].read, after_sequence=cursors[scope], limit=100,
                )
                if page["reset_reason"]:
                    cursors[scope] = page["next_sequence"]
                    payload = json.dumps({"scope": scope, "reason": page["reset_reason"]})
                    yield f"id: {encode_cursor(station_id, cursors)}\nevent: stream.reset\ndata: {payload}\n\n"
                for event in page["items"]:
                    # 不能在读页后整体前推：客户端可能只收到本页第一条就断开。
                    cursors[scope] = event["id"]
                    payload = json.dumps(event["data"], ensure_ascii=False)
                    yield f"id: {encode_cursor(station_id, cursors)}\nevent: {event['event']}\ndata: {payload}\n\n"
                has_more = has_more or page["has_more"]
            if time.monotonic() - heartbeat_at >= heartbeat_interval:
                yield ": keepalive\n\n"
                heartbeat_at = time.monotonic()
            await asyncio.sleep(0 if has_more else poll_interval)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
    })


def create_station_events_router(
    service: Any, inventory_store_provider: Callable[[], Any | None],
) -> APIRouter:
    """复用同一工站控制器的范围检查，不建立新的权限或存储权威。"""
    router = APIRouter()

    @router.get(
        "/stations/{station_id}/events/stream", response_class=StreamingResponse,
        responses={200: {"content": {"text/event-stream": {"schema": {"type": "string"}}}}},
        openapi_extra={"parameters": [{
            "name": "Last-Event-ID", "in": "header", "required": False,
            "schema": {"type": "string"}, "description": "上次收到的不透明复合游标；省略时从最新水位订阅。",
        }]},
    )
    async def events(station_id: str, request: Request) -> StreamingResponse:
        handling = getattr(service, "error_handling", None)
        if handling is None:
            raise HTTPException(503, "工站错误处置服务尚未就绪")
        if not await run_in_threadpool(handling.control.accepts_station_id, station_id):
            raise HTTPException(404, "工站不存在")
        inventory = inventory_store_provider()
        if inventory is None:
            raise HTTPException(503, "工站库存服务尚未就绪")
        inventory_source = ResourceChangeStore(inventory, scope="inventory")
        workflow_source = ResourceChangeStore(handling.control.store, scope="workflow")
        sources = {
            "runtime": StationEventSource(service.latest_event_sequence, service.read_event_stream),
            "inventory": StationEventSource(inventory_source.latest_event_sequence, DurableEventReader(inventory_source).read_stream),
            "workflow": StationEventSource(workflow_source.latest_event_sequence, DurableEventReader(workflow_source).read_stream),
        }
        return await station_event_stream(request, station_id, sources)

    return router


def configured_inventory_store():
    """仅在请求时解析启动装配好的库存，保持冷启动与隔离测试行为。"""
    from unilabos.app.scheduler.integration import get_inventory_service

    inventory = get_inventory_service()
    return inventory.store if inventory is not None else None
