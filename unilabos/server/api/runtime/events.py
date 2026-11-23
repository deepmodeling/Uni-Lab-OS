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

事件名按 V1 合同输出（:func:`to_v1_event`）：持久库里 dev 的细粒度事件
``workflow.task|node_run|node_job|manual_confirmation|resource.changed`` 一律发成
``workflow.runtime.changed``，原事件的种类放进 ``data.kind``（``task`` / ``node_run`` /
``node_job`` / ``manual_confirmation`` / ``resource``），原载荷字段保留。
``workflow.authoring.changed`` 在库里就是 V1 名，原样发送：``@workflow`` 源码创作的状态迁移
（``data.cause``），以及工作流定义的变化（``data.change``：``created`` / ``updated`` / ``deleted`` /
``graph_saved`` / ``published``），都与变更同一事务写入。游标与 ``stream.reset`` 协议不变。

物料失效：本进程持有物料权威时，物料账本前进就推一条**不带 id** 的 ``material.changed``
（``data: {"sequence": <账本末尾序号>}``），与日志通知一样不动工作流游标。只维持这一条事件流的
前端据此重读物料；要按账本 sequence 续传、按 aggregate 精确失效的客户端仍用
``/api/v1/materials/events``（``materials.changed``）。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable

from typing import Any, Dict

from fastapi import APIRouter, Header, Request
from fastapi.responses import Response, StreamingResponse

from unilabos.protocol.runtime.logs import RuntimeLogNotice
from unilabos.server.api.envelope import WORKFLOW, envelope_router
from unilabos.server.lifecycle import shutting_down
from unilabos.utils.log_notices import LOG_NOTICE_EVENT, log_notices, merge_notices

STREAM_RESET_EVENT = "stream.reset"
#: V1 前端事件名（``/api/v1/events``）
RUNTIME_CHANGED_EVENT = "workflow.runtime.changed"
AUTHORING_CHANGED_EVENT = "workflow.authoring.changed"
ERROR_HANDLING_CHANGED_EVENT = "station.error_handling.changed"
#: 物料账本前进的瞬时失效通知（无 id，不占工作流游标）
MATERIAL_CHANGED_EVENT = "material.changed"

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


def material_sequence_reader() -> Callable[[], int] | None:
    """本进程持有物料权威时返回"账本末尾序号"读取函数；Host 子进程等没有权威的进程返回 None。"""

    try:
        from unilabos.server.execution_composition import get_materials_service
    except ImportError:
        return None
    reader = getattr(get_materials_service(), "latest_ledger_sequence", None)
    return reader if callable(reader) else None


def _read_material_sequence(reader: Callable[[], int], fallback: int) -> int:
    try:
        return int(reader())
    except Exception:  # noqa: BLE001 - 读不到账本只是这一轮不发失效，不能断开工作流事件流
        return fallback


def material_changed_frame(sequence: int) -> str:
    """不写 SSE id：Last-Event-ID 只表示工作流持久事件的序号。"""

    data = json.dumps({"sequence": sequence}, separators=(",", ":"))
    return f"event: {MATERIAL_CHANGED_EVENT}\ndata: {data}\n\n"


async def runtime_event_stream(
    request: Request,
    service=None,
    cursor: int = 0,
    *,
    watermark: bool = False,
    replay: bool = False,
    material_sequence: Callable[[], int] | None = None,
    material_cursor: int = 0,
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
                if material_sequence is not None:
                    # 账本末尾变了（前进，或库被重置后变小）就发一次失效；只比较末尾，不回放账本。
                    latest_material = _read_material_sequence(material_sequence, material_cursor)
                    if latest_material != material_cursor:
                        material_cursor = latest_material
                        yield material_changed_frame(latest_material)
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
                        yield format_sse_event(to_v1_event(event))
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
            # “快照 → 订阅”之间的变化。纯执行面的 Host 没有 Workflow Authority。
            cursor = service.latest_event_sequence() if service is not None else 0
        # 物料账本基线同样在响应头之前固定：onopen 后的 REST 补读覆盖此前的物料变化。
        material_sequence = material_sequence_reader()
        material_cursor = _read_material_sequence(material_sequence, 0) if material_sequence is not None else 0
        return StreamingResponse(
            runtime_event_stream(
                request,
                service,
                cursor,
                watermark=fresh and service is not None,
                replay=bool(value) and cursor == 0,
                material_sequence=material_sequence,
                material_cursor=material_cursor,
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return router
