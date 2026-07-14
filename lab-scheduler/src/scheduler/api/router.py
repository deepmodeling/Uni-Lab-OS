"""API 路由定义."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from scheduler.api.schemas import RescheduleRequest, ScheduleRequest, ScheduleResponse
from scheduler.compiler.schemas import CompilerRequest, CompilerResponse
from scheduler.compiler.service import CompilerService
from scheduler.core.step_algorithms import list_algorithms
from scheduler.service.scheduler_service import SchedulerService

router = APIRouter(prefix="/api/v1")

_service = SchedulerService()
_compiler = CompilerService()


@router.post("/schedule", response_model=ScheduleResponse)
async def schedule(req: ScheduleRequest) -> ScheduleResponse:
    try:
        return _service.schedule(req)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.post("/reschedule", response_model=ScheduleResponse)
async def reschedule(req: RescheduleRequest) -> ScheduleResponse:
    try:
        return _service.reschedule(req)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.post("/compile", response_model=CompilerResponse)
async def compile_dag(req: CompilerRequest) -> CompilerResponse:
    try:
        return _compiler.compile(req)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.get("/algorithms")
async def get_algorithms() -> dict[str, list[str]]:
    return {"algorithms": list_algorithms()}


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
