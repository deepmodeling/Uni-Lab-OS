"""``history.v1`` FastAPI 路由；历史流只允许追加，不暴露覆盖或删除。"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, FastAPI, HTTPException, Query

from unilabos.server.models.history import HistoryEventRecord
from unilabos.server.protocol.history import (
    HistoryEventAppend,
    HistoryEventQuery,
    HistoryEventType,
    ManualResultReplacement,
    PayloadObjectRead,
    PayloadWrite,
)
from unilabos.server.services.history import (
    HistoryConflictError,
    HistoryNotFoundError,
    HistoryService,
    HistoryValidationError,
)


def _call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except HistoryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except HistoryConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except HistoryValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _payload_read(value) -> PayloadObjectRead:
    return PayloadObjectRead.model_validate(value.model_dump(mode="python"))


def create_history_router(service: HistoryService) -> APIRouter:
    router = APIRouter(prefix="/api/v1/history", tags=["history-v1"])

    @router.post("/payloads", response_model=PayloadObjectRead)
    async def store_payload(value: PayloadWrite):
        return _payload_read(_call(service.store_payload, value))

    @router.get("/payloads/{payload_uuid}", response_model=PayloadObjectRead)
    async def get_payload(payload_uuid: str):
        return _payload_read(_call(service.get_payload, payload_uuid))

    @router.post("/events", response_model=HistoryEventRecord)
    async def append_event(value: HistoryEventAppend):
        return _call(service.append_event, value)

    @router.get("/events", response_model=list[HistoryEventRecord])
    async def query_events(
        after_sequence: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=1000),
        event_types: list[HistoryEventType] = Query(default=[]),
        job_uuid: Optional[str] = Query(default=None, min_length=1),
        endpoint_uuid: Optional[str] = Query(default=None, min_length=1),
        device_uuid: Optional[str] = Query(default=None, min_length=1),
        event_key: Optional[str] = Query(default=None, min_length=1),
        occurred_from_ms: Optional[int] = Query(default=None, ge=0),
        occurred_through_ms: Optional[int] = Query(default=None, ge=0),
    ):
        try:
            query = HistoryEventQuery(
                after_sequence=after_sequence,
                limit=limit,
                event_types=event_types,
                job_uuid=job_uuid,
                endpoint_uuid=endpoint_uuid,
                device_uuid=device_uuid,
                event_key=event_key,
                occurred_from_ms=occurred_from_ms,
                occurred_through_ms=occurred_through_ms,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _call(service.query_events, query)

    @router.get("/events/{event_uuid}", response_model=HistoryEventRecord)
    async def get_event(event_uuid: str):
        return _call(service.get_event, event_uuid)

    @router.post(
        "/events/{event_uuid}/replacement",
        response_model=HistoryEventRecord,
    )
    async def append_replacement(event_uuid: str, value: ManualResultReplacement):
        if value.supersedes_event_uuid != event_uuid:
            raise HTTPException(
                status_code=422,
                detail="superseded event UUID path mismatch",
            )
        return _call(service.append_replacement, value)

    @router.get(
        "/events/{event_uuid}/replacement-chain",
        response_model=list[HistoryEventRecord],
    )
    async def replacement_chain(event_uuid: str):
        return _call(service.replacement_chain, event_uuid)

    return router


def install_history_api(app: FastAPI, service: HistoryService) -> None:
    app.include_router(create_history_router(service))


__all__ = ["create_history_router", "install_history_api"]
