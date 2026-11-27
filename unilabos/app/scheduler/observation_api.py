"""只读孪生状态访问；控制和 provider 写入不暴露给观察端。"""

from __future__ import annotations

import asyncio
import json
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from unilabos.runtime.observations import Selector, StateError

from .device_observations import DeviceObservationService


def create_observation_router(
    get_runtime, get_sources, *, prefix="/device-state/observations"
) -> APIRouter:
    router = APIRouter(prefix=prefix, tags=["device-state"])

    def runtime() -> DeviceObservationService:
        instance = get_runtime()
        if instance is None:
            raise HTTPException(503, "device observations not enabled")
        return instance

    def call(function, *args):
        try:
            return {"code": 0, "data": function(*args)}
        except StateError as error:
            return JSONResponse(
                {"code": 400, "error": {"msg": str(error), "reason": error.reason}}
            )

    @router.get("/snapshot")
    def snapshot():
        return call(runtime().snapshot)

    @router.get("/operations/{operation_id}")
    def operation(operation_id: str):
        runtime()
        return call(get_sources().operation, operation_id)

    @router.get("/actions/{job_id}")
    def action(job_id: str):
        runtime()
        return call(get_sources().legacy_action, job_id)

    @router.get("/resolve")
    def resolve(value: str = Query(min_length=1), by: Literal["id", "name"] = "id"):
        return call(runtime().resolve, Selector(by=by, value=value))

    @router.get("/changes")
    def changes(
        runtime_session: str,
        after: int = Query(ge=0),
        limit: int = Query(default=1000, ge=1, le=1000),
    ):
        return call(runtime().changes, runtime_session, after, limit)

    @router.get("/events")
    def events(
        request: Request,
        runtime_session: str | None = None,
        after: int | None = Query(default=None, ge=0),
    ):
        instance = runtime()
        # 基线在响应头之前获取。基线游标后的更新不会落在订阅间隙。
        baseline = instance.snapshot()

        async def stream():
            session = runtime_session or baseline["runtime_session"]
            cursor = after
            if cursor is None:
                cursor = baseline["cursor"]
                session = baseline["runtime_session"]
                yield "event: snapshot\ndata: " + json.dumps(baseline) + "\n\n"
            while not await request.is_disconnected():
                try:
                    page = await asyncio.to_thread(instance.changes, session, cursor)
                except StateError as error:
                    yield "event: stream.reset\ndata: " + json.dumps(
                        {"reason": error.reason}
                    ) + "\n\n"
                    return
                for event in page["events"]:
                    yield "event: update\ndata: " + json.dumps(event) + "\n\n"
                cursor = page["cursor"]
                if not page["has_more"]:
                    yield ": keepalive\n\n"
                    await asyncio.sleep(0.05)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )

    return router
