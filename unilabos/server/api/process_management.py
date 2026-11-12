"""V1 进程管理路由：``/v1/snapshot``、``/v1/operations``、``/v1/operations/{id}``、``/v1/logs/{component}``。

按 V1 原样：路径不带 ``/api`` 前缀；成功是裸 JSON（快照 / 操作文档 / 日志尾部，提交操作 HTTP 202），
失败用 HTTP 状态码 + ``{"error": {"code", "message", "details"?}}``（不存在 404、冲突 409、其余 400；
dev 增加组件日志暂不可读 503）。与 ``/health``、``/readiness`` 一样是统一信封的例外，见
``docs/developer_guide/api_v1_migration.md``。

鉴权按 dev 约定：写操作（``POST /v1/operations``）由 ``ControlTokenMiddleware`` 检查
``X-UniLab-Token``，拒绝时为 HTTP 401 + V1 形状的 ``{"error": {"code": "unauthorized"}}``；读操作与其它
dev GET 一样不需要 token（V1 的 Host 本地 token 对读也生效，dev 不沿用）。

只挂在调度权威（有 Workflow Authority 的进程）上：默认 ``unilab``、``--role backend`` 与
``--no-safe-restart`` 单进程；配置了 ``--address`` 的 Host 子进程不挂。
"""

from __future__ import annotations

import json
from typing import Any, Callable

from fastapi import APIRouter, FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from unilabos.protocol.runtime.process_management import (
    DEFAULT_LOG_MAX_BYTES,
    MAX_LOG_MAX_BYTES,
    MAX_REQUEST_BYTES,
    ProcessManagementError,
    ProcessManagementErrorBody,
    WorkspaceLogTail,
    WorkspaceOperation,
    WorkspaceOperationRequest,
    WorkspaceSnapshot,
    error_http_status,
)

_NO_STORE = {"Cache-Control": "no-store"}
_ERRORS: dict[int | str, dict[str, Any]] = {
    400: {"model": ProcessManagementErrorBody, "description": "参数无效（error.code 如 invalid_request / component_invalid）"},
}


def _json(status: int, payload: Any) -> JSONResponse:
    return JSONResponse(content=payload, status_code=status, headers=_NO_STORE)


def error_response(error: ProcessManagementError) -> JSONResponse:
    return _json(error_http_status(error.code), {"error": error.as_dict()})


def create_process_management_router(get_service: Callable[[], Any]) -> APIRouter:
    router = APIRouter(tags=["process-management"])

    def service() -> Any:
        value = get_service()
        if value is None:
            raise ProcessManagementError("host_not_found", "本进程没有进程管理服务")
        return value

    @router.get(
        "/v1/snapshot",
        response_model=None,
        summary="工作区进程树快照（V1 进程管理协议）",
        responses={200: {"model": WorkspaceSnapshot}},
    )
    def snapshot() -> JSONResponse:
        """调度权威（backend）、Host（edge）与各受管设备进程（managed:<id>）的阶段、pid 与修订号。"""

        try:
            return _json(200, service().snapshot())
        except ProcessManagementError as exc:
            return error_response(exc)

    @router.post(
        "/v1/operations",
        response_model=None,
        status_code=202,
        summary="提交幂等工作区操作（V1 进程管理协议）",
        responses={
            202: {"model": WorkspaceOperation},
            **_ERRORS,
            409: {"model": ProcessManagementErrorBody, "description": "operation_conflict / revision_conflict"},
        },
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {"application/json": {"schema": WorkspaceOperationRequest.model_json_schema()}},
            }
        },
    )
    async def submit(request: Request) -> JSONResponse:
        """受理后进入单一 FIFO 执行通道；同 operationId 同请求重放返回原记录。"""

        try:
            raw = await request.body()
            if not 1 <= len(raw) <= MAX_REQUEST_BYTES:
                raise ProcessManagementError("invalid_request", "请求体大小无效")
            try:
                payload = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise ProcessManagementError("invalid_request", "请求体不是有效 JSON") from exc
            return _json(202, await run_in_threadpool(service().submit, payload))
        except ProcessManagementError as exc:
            return error_response(exc)

    @router.get(
        "/v1/operations/{operation_id}",
        response_model=None,
        summary="查询工作区操作（V1 进程管理协议）",
        responses={
            200: {"model": WorkspaceOperation},
            **_ERRORS,
            404: {"model": ProcessManagementErrorBody, "description": "operation_not_found"},
        },
    )
    def operation(operation_id: str) -> JSONResponse:
        try:
            return _json(200, service().operation(operation_id))
        except ProcessManagementError as exc:
            return error_response(exc)

    @router.get(
        "/v1/logs/{component}",
        response_model=None,
        summary="有界读取组件日志尾部（V1 进程管理协议）",
        responses={
            200: {"model": WorkspaceLogTail},
            **_ERRORS,
            503: {"model": ProcessManagementErrorBody, "description": "component_unavailable：Host 不在线或日志暂不可读"},
        },
        openapi_extra={
            "parameters": [
                {
                    "name": "maxBytes",
                    "in": "query",
                    "required": False,
                    "description": "返回内容的字节上限（1–4194304，缺省 65536）；读取端单次最多 256 KiB / 1000 行",
                    "schema": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_LOG_MAX_BYTES,
                        "default": DEFAULT_LOG_MAX_BYTES,
                    },
                }
            ]
        },
    )
    def logs(component: str, request: Request) -> JSONResponse:
        """组件：backend / edge / plc / renderer / managed:<process_id>；只读应用选定的日志，不接受路径。"""

        try:
            raw = request.query_params.get("maxBytes")
            try:
                max_bytes = DEFAULT_LOG_MAX_BYTES if raw is None else int(raw)
            except ValueError as exc:
                raise ProcessManagementError("invalid_request", "请求参数无效") from exc
            return _json(200, service().log_tail(component, max_bytes))
        except ProcessManagementError as exc:
            return error_response(exc)

    @router.api_route("/v1/{path:path}", methods=["GET", "POST"], include_in_schema=False)
    def not_found(path: str) -> JSONResponse:
        # V1 未知路径：404 + {"error": {"code": "not_found"}}
        return _json(404, {"error": {"code": "not_found"}})

    return router


def install_process_management_api(app: FastAPI, workflow_service: Any, **service_options: Any) -> Any:
    """在调度权威应用上挂载 V1 进程管理路由；服务实例挂在 ``app.state.process_management``。"""

    from unilabos.server.services.process_management import ProcessManagementService

    service = ProcessManagementService(workflow_service, **service_options)
    app.state.process_management = service
    app.include_router(create_process_management_router(lambda: service))
    return service


__all__ = ["create_process_management_router", "error_response", "install_process_management_api"]
