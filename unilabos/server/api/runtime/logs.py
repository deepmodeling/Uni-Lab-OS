"""进程日志只读 API；Host 专属，分离部署经原有控制面代理，不另开监听端口。"""

from fastapi import APIRouter, HTTPException, Query, Response

from unilabos.server.api.envelope import envelope_router
from unilabos.protocol.runtime.logs import RuntimeLogBatch, RuntimeLogSources
from unilabos.server.services.runtime.logs import (
    LogSourceNotFound,
    LogSourceUnavailable,
    get_runtime_log_service,
)


def create_runtime_logs_router() -> APIRouter:
    router = envelope_router(prefix="/api/v1/hostlink", tags=["runtime-logs"])

    @router.get("/log-sources", response_model=RuntimeLogSources)
    def sources(response: Response) -> RuntimeLogSources:
        response.headers["Cache-Control"] = "no-store"
        return get_runtime_log_service().sources()

    @router.get("/logs", response_model=RuntimeLogBatch)
    def logs(
        response: Response,
        source_id: str = Query(min_length=1, max_length=512),
        cursor: str = Query(default="", max_length=96, pattern=r"^(?:[0-9a-f]{24}:[0-9]{1,16})?$"),
        limit: int = Query(default=300, ge=1, le=1000),
    ) -> RuntimeLogBatch:
        response.headers["Cache-Control"] = "no-store"
        try:
            return get_runtime_log_service().read(source_id, cursor=cursor, limit=limit)
        except LogSourceNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except LogSourceUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc), headers={"Retry-After": "3"}) from exc

    return router
