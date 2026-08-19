"""``telemetry.v1`` FastAPI 路由；只暴露合法追加和只读查询。"""

from __future__ import annotations

from typing import Literal, Optional, TypeVar

from fastapi import APIRouter, FastAPI, HTTPException, Query

from unilabos.server.protocol.telemetry import (
    TelemetryEventQuery,
    TelemetryIngestRequest,
)
from unilabos.server.services.telemetry import (
    TelemetryConflictError,
    TelemetryService,
    TelemetryValidationError,
)


ValueT = TypeVar("ValueT")


def _call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except TelemetryConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except TelemetryValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _required(value: Optional[ValueT], detail: str) -> ValueT:
    if value is None:
        raise HTTPException(status_code=404, detail=detail)
    return value


def _event_query(**values: object) -> TelemetryEventQuery:
    try:
        return TelemetryEventQuery.model_validate(values)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def create_telemetry_router(service: TelemetryService) -> APIRouter:
    """创建 telemetry router，不提供 event/state/cursor 的任意改写或删除。"""

    router = APIRouter(prefix="/api/v1/telemetry", tags=["telemetry-v1"])

    @router.post("/events")
    async def ingest(request: TelemetryIngestRequest):
        return _call(service.ingest, request)

    @router.get("/events")
    async def query_events(
        after_sequence: int = Query(default=0, ge=0),
        endpoint_uuid: Optional[str] = Query(default=None, min_length=1),
        device_uuid: Optional[str] = Query(default=None, min_length=1),
        event_type: Optional[
            Literal["state", "property_sample", "connection", "alarm"]
        ] = Query(default=None),
        source_epoch: Optional[str] = Query(default=None, min_length=1),
        source_generation: Optional[int] = Query(default=None, ge=0),
        observed_from_ms: Optional[int] = Query(default=None, ge=0),
        observed_to_ms: Optional[int] = Query(default=None, ge=0),
        limit: int = Query(default=100, ge=1, le=1000),
    ):
        query = _event_query(
            after_sequence=after_sequence,
            endpoint_uuid=endpoint_uuid,
            device_uuid=device_uuid,
            event_type=event_type,
            source_epoch=source_epoch,
            source_generation=source_generation,
            observed_from_ms=observed_from_ms,
            observed_to_ms=observed_to_ms,
            limit=limit,
        )
        return _call(service.query_events, query)

    @router.get("/events/{event_uuid}")
    async def get_event(event_uuid: str):
        return _required(
            service.get_event(event_uuid),
            f"telemetry event {event_uuid!r} was not found",
        )

    @router.get("/sources/{endpoint_uuid}/cursor")
    async def get_source_cursor(endpoint_uuid: str):
        return _required(
            service.get_source_cursor(endpoint_uuid),
            f"telemetry source {endpoint_uuid!r} was not found",
        )

    @router.get("/states")
    async def list_device_states(
        endpoint_uuid: Optional[str] = Query(default=None, min_length=1),
    ):
        return service.list_device_states(endpoint_uuid)

    @router.get("/states/{endpoint_uuid}/{device_uuid}")
    async def get_device_state(endpoint_uuid: str, device_uuid: str):
        return _required(
            service.get_device_state(endpoint_uuid, device_uuid),
            f"device state {endpoint_uuid!r}/{device_uuid!r} was not found",
        )

    return router


def install_telemetry_api(app: FastAPI, service: TelemetryService) -> None:
    app.include_router(create_telemetry_router(service))


__all__ = ["create_telemetry_router", "install_telemetry_api"]
