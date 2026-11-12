"""实验室布局 API（``/api/v1/lab/layout``，统一信封，runtime.db）。

一个 Host 一份布局文档，所有连接同一微后端的浏览器共享：

- ``GET``：从未保存时返回 ``revision = 0`` 的空布局；
- ``PUT``：整份替换，``revision`` 乐观锁，不匹配为业务码 3003（``error.msg`` 带当前版本）；
- ``DELETE``：重置为未保存状态 → ``{"code": 0}``。
"""

from __future__ import annotations

from fastapi import APIRouter, FastAPI, HTTPException, Response

from unilabos.server.api.envelope import envelope_router
from unilabos.protocol.runtime.lab import LabLayoutRead, LabLayoutWrite
from unilabos.server.services.runtime.lab import LabLayoutConflict, LabLayoutService


def create_lab_router(service: LabLayoutService) -> APIRouter:
    router = envelope_router(prefix="/api/v1/lab", tags=["lab-v1"])

    @router.get("/layout", response_model=LabLayoutRead)
    async def get_layout() -> LabLayoutRead:
        return service.get_layout()

    @router.put("/layout", response_model=LabLayoutRead)
    async def put_layout(value: LabLayoutWrite) -> LabLayoutRead:
        try:
            return service.save_layout(value)
        except LabLayoutConflict as exc:
            raise HTTPException(
                status_code=409,
                detail=f"布局已被他人修改（当前 revision {exc.current}，提交的是 {exc.expected}），请刷新后重试",
            ) from exc

    @router.delete("/layout", status_code=204, response_class=Response)
    async def reset_layout() -> Response:
        service.reset_layout()
        return Response(status_code=204)

    return router


def install_lab_api(app: FastAPI, service: LabLayoutService) -> None:
    app.include_router(create_lab_router(service))


__all__ = ["create_lab_router", "install_lab_api"]
