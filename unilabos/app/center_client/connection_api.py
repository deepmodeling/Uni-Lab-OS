"""工站头像设置使用的本地连接管理接口，不代理中心管理员权限。"""
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .connection import ConnectionError


class ConsoleAddressRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    custom_url: str | None = Field(max_length=2048)
    browser_url: str | None = Field(default=None, max_length=2048)


class ConnectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    center_url: str = Field(max_length=2048)
    enrollment_code: str = Field(min_length=64, max_length=64, repr=False)
    server_ca_pem: str = Field(default="", max_length=65536, repr=False)
    http_base_url: str | None = Field(default=None, max_length=2048)
    console_address: ConsoleAddressRequest | None = None


def create_router(get_connection: Callable[[], Any]) -> APIRouter:
    router = APIRouter(prefix="/api/v1/center-connection", tags=["Center connection"])

    def owner():
        value = get_connection()
        if value is None:
            raise HTTPException(503, "工作流运行时尚未就绪")
        return value

    def local_mutation(request: Request):
        if (request.headers.get("x-unilab-local-request") != "1"
                or request.headers.get("sec-fetch-site") == "cross-site"):
            raise HTTPException(403, "请从工站操作台设置中心连接")

    @router.get("")
    def status():
        return {"code": 0, "data": owner().status()}

    @router.post("")
    def connect(body: ConnectRequest, request: Request):
        local_mutation(request)
        try:
            return {"code": 0, "data": owner().connect(**body.model_dump())}
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        except ConnectionError as error:
            raise HTTPException(409, error.code) from error

    @router.post("/retry")
    def retry(request: Request):
        local_mutation(request)
        try:
            return {"code": 0, "data": owner().retry()}
        except ConnectionError as error:
            raise HTTPException(409, error.code) from error

    @router.get("/console-address")
    def console_address_status():
        return {"code": 0, "data": owner().console_address_status()}

    @router.post("/console-address")
    def save_console_address(body: ConsoleAddressRequest, request: Request):
        local_mutation(request)
        try:
            return {"code": 0, "data": owner().save_console_address(**body.model_dump())}
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        except ConnectionError as error:
            raise HTTPException(409, error.code) from error

    return router
