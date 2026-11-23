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
（``data: {"sequence": <账本末尾序号>, "material_uuids": [...]}``），与日志通知一样不动工作流游标。
``material_uuids`` 是这段账本里变了的物料（Site 的变化归到所属物料）；列不准时（条目过多、模板变化、
库被重置）省略它，表示整体重读。只维持这一条事件流的前端据此重读物料；要按账本 sequence 续传的
客户端仍用 ``/api/v1/materials/events``（``materials.changed``）。
"""

from __future__ import annotations

import asyncio
import json
import time

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


#: 一次通知最多列出的账本条目；超过就不列物料，让客户端整体重读
MATERIAL_NOTICE_WINDOW = 500


class MaterialNotices:
    """本进程物料权威的账本视图：末尾序号，以及一段区间里哪些物料变了。"""

    def __init__(self, service: Any):
        self._service = service

    def latest(self) -> int:
        return int(self._service.latest_ledger_sequence())

    def changed_materials(self, after: int, upto: int) -> list[str] | None:
        """``(after, upto]`` 里变了的物料 uuid（Site 的变化归到它的所属物料）。

        返回 None 表示列不准（条目超过窗口、模板变了），客户端应整体重读。
        """

        rows = self._service.changes(after_sequence=after, limit=MATERIAL_NOTICE_WINDOW + 1)
        rows = [row for row in rows if row.sequence <= upto]
        if len(rows) > MATERIAL_NOTICE_WINDOW:
            return None
        changed: set[str] = set()
        sites: set[str] = set()
        for row in rows:
            if row.aggregate_type == "material":
                changed.add(row.aggregate_uuid)
            elif row.aggregate_type == "site":
                sites.add(row.aggregate_uuid)
            elif row.aggregate_type == "resource_template":
                # 模板变化可能改变很多物料的展示，不逐个列
                return None
        # 同一个 Site 在一段账本里常出现多次（占用、释放），每个只查一次所属物料
        for site_uuid in sorted(sites):
            site = self._service.get_site(site_uuid, include_deleted=True)
            if site is not None:
                changed.add(site.owner_material_uuid)
        return sorted(changed)


def material_notices() -> MaterialNotices | None:
    """本进程持有物料权威时返回账本视图；Host 子进程等没有权威的进程返回 None。"""

    try:
        from unilabos.server.execution_composition import get_materials_service
    except ImportError:
        return None
    service = get_materials_service()
    if service is None or not callable(getattr(service, "latest_ledger_sequence", None)):
        return None
    return MaterialNotices(service)


def _latest_material_sequence(materials: MaterialNotices, fallback: int) -> int:
    try:
        return materials.latest()
    except Exception:  # noqa: BLE001 - 读不到账本只是这一轮不发失效，不能断开工作流事件流
        return fallback


def _changed_materials(materials: MaterialNotices, after: int, upto: int) -> list[str] | None:
    try:
        return materials.changed_materials(after, upto)
    except Exception:  # noqa: BLE001 - 列不出来就让客户端整体重读
        return None


def _poll_materials(materials: MaterialNotices, cursor: int) -> tuple[int, list[str] | None]:
    """账本末尾，以及 ``(cursor, 末尾]`` 里变了的物料；末尾变小（库被重置）时不列。

    在工作线程里跑（``asyncio.to_thread``）：读账本要拿物料库的锁，写事务持锁期间读会等，
    放在事件循环上会卡住这个进程的全部连接。
    """

    latest = _latest_material_sequence(materials, cursor)
    if latest <= cursor:
        return latest, None
    return latest, _changed_materials(materials, cursor, latest)


def material_changed_frame(sequence: int, material_uuids: list[str] | None = None) -> str:
    """不写 SSE id：Last-Event-ID 只表示工作流持久事件的序号。

    ``material_uuids`` 缺省表示"列不准，整体重读"；给出时客户端只重读这些物料。
    """

    payload: Dict[str, Any] = {"sequence": sequence}
    if material_uuids is not None:
        payload["material_uuids"] = material_uuids
    data = json.dumps(payload, separators=(",", ":"))
    return f"event: {MATERIAL_CHANGED_EVENT}\ndata: {data}\n\n"


async def runtime_event_stream(
    request: Request,
    service=None,
    cursor: int = 0,
    *,
    watermark: bool = False,
    replay: bool = False,
    materials: MaterialNotices | None = None,
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
                if materials is not None:
                    # 账本末尾变了就发一次失效，列出这段区间里变了的物料；库被重置（末尾变小）时不列，整体重读。
                    latest_material, changed = await asyncio.to_thread(_poll_materials, materials, material_cursor)
                    if latest_material != material_cursor:
                        material_cursor = latest_material
                        yield material_changed_frame(latest_material, changed)
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
        materials = material_notices()
        material_cursor = (
            await asyncio.to_thread(_latest_material_sequence, materials, 0) if materials is not None else 0
        )
        return StreamingResponse(
            runtime_event_stream(
                request,
                service,
                cursor,
                watermark=fresh and service is not None,
                replay=bool(value) and cursor == 0,
                materials=materials,
                material_cursor=material_cursor,
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return router
