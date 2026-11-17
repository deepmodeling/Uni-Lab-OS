"""V1 进程管理路由：``/v1/snapshot``、``/v1/operations``、``/v1/operations/{id}``、``/v1/logs/{component}``。

路径按 V1 原样不带 ``/api`` 前缀；响应与其它管理 API 一样是统一信封（2026-11-17 起）：成功
``{"code": 0, "data": <快照 / 操作文档 / 日志尾部>}``（提交操作 HTTP 202），失败 HTTP 200 +
``{"code": <业务码>, "error": {"msg", "code": <V1 符号码>, "details"?}}``。V1 的符号码（``operation_not_found``、
``operation_conflict``、``revision_conflict``、``invalid_request``、``component_unavailable`` …）放在
``error.code``，业务码按 ``api/envelope.py`` 的 ``DOMAIN_ERRORS`` 换算（不存在 4001、冲突 3003、暂不可读 5001、
其余 1000）。未知 ``/v1/*`` 路径与其它不存在的路由一样由应用级兜底返回 HTTP 404 + 信封 4001。不鉴权。

只挂在调度权威（有 Workflow Authority 的进程）上：默认 ``unilab``、``--role backend`` 与
``--no-safe-restart`` 单进程；配置了 ``--address`` 的 Host 子进程不挂。
"""

from __future__ import annotations

import json
from typing import Any, Callable

from fastapi import APIRouter, FastAPI, Request
from fastapi.concurrency import run_in_threadpool

from unilabos.protocol.runtime.process_management import (
    DEFAULT_LOG_MAX_BYTES,
    MAX_LOG_MAX_BYTES,
    MAX_REQUEST_BYTES,
    ProcessManagementError,
    WorkspaceLogTail,
    WorkspaceOperation,
    WorkspaceOperationRequest,
    WorkspaceSnapshot,
)
from unilabos.server.api.envelope import (
    INVALID_PARAMETER,
    ApiError,
    EnvelopeResponse,
    envelope_router,
    ok_body,
    to_api_error,
)

_NO_STORE = {"Cache-Control": "no-store"}


def _ok(payload: Any, status: int = 200) -> EnvelopeResponse:
    return EnvelopeResponse(content=ok_body(payload), status_code=status, headers=_NO_STORE)


def error_response(error: ProcessManagementError) -> EnvelopeResponse:
    """进程管理错误 → 失败信封（HTTP 200，V1 符号码在 ``error.code``）。"""

    api_error = to_api_error(error) or ApiError(INVALID_PARAMETER, error.message, symbol=error.code)
    api_error.headers = dict(_NO_STORE)
    return api_error.to_response()


def create_process_management_router(get_service: Callable[[], Any]) -> APIRouter:
    router = envelope_router(tags=["process-management"])

    def service() -> Any:
        value = get_service()
        if value is None:
            raise ProcessManagementError("host_not_found", "本进程没有进程管理服务")
        return value

    @router.get("/v1/snapshot", response_model=WorkspaceSnapshot, summary="工作区进程树快照（V1 进程管理协议）")
    def snapshot() -> EnvelopeResponse:
        """调度权威（backend）、Host（edge）与各受管设备进程（managed:<id>）的阶段、pid 与修订号。"""

        try:
            return _ok(service().snapshot())
        except ProcessManagementError as exc:
            return error_response(exc)

    @router.post(
        "/v1/operations",
        response_model=WorkspaceOperation,
        status_code=202,
        summary="提交幂等工作区操作（V1 进程管理协议）",
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {"application/json": {"schema": WorkspaceOperationRequest.model_json_schema()}},
            }
        },
    )
    async def submit(request: Request) -> EnvelopeResponse:
        """受理后进入单一 FIFO 执行通道；同 operationId 同请求重放返回原记录，不同请求 3003
        ``operation_conflict``，``expectedRevision`` 过期 3003 ``revision_conflict``。"""

        try:
            raw = await request.body()
            if not 1 <= len(raw) <= MAX_REQUEST_BYTES:
                raise ProcessManagementError("invalid_request", "请求体大小无效")
            try:
                payload = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise ProcessManagementError("invalid_request", "请求体不是有效 JSON") from exc
            return _ok(await run_in_threadpool(service().submit, payload), 202)
        except ProcessManagementError as exc:
            return error_response(exc)

    @router.get(
        "/v1/operations/{operation_id}",
        response_model=WorkspaceOperation,
        summary="查询工作区操作（V1 进程管理协议）",
    )
    def operation(operation_id: str) -> EnvelopeResponse:
        """不存在时 4001 ``operation_not_found``。"""

        try:
            return _ok(service().operation(operation_id))
        except ProcessManagementError as exc:
            return error_response(exc)

    @router.get(
        "/v1/logs/{component}",
        response_model=WorkspaceLogTail,
        summary="有界读取组件日志尾部（V1 进程管理协议）",
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
    def logs(component: str, request: Request) -> EnvelopeResponse:
        """组件：backend / edge / plc / renderer / managed:<process_id>；只读应用选定的日志，不接受路径。
        Host 不在线或日志暂不可读时 5001 ``component_unavailable``，未知组件 1000 ``component_invalid``。"""

        try:
            raw = request.query_params.get("maxBytes")
            try:
                max_bytes = DEFAULT_LOG_MAX_BYTES if raw is None else int(raw)
            except ValueError as exc:
                raise ProcessManagementError("invalid_request", "请求参数无效") from exc
            return _ok(service().log_tail(component, max_bytes))
        except ProcessManagementError as exc:
            return error_response(exc)

    return router


def install_process_management_api(app: FastAPI, workflow_service: Any, **service_options: Any) -> Any:
    """在调度权威应用上挂载 V1 进程管理路由；服务实例挂在 ``app.state.process_management``。"""

    from unilabos.server.services.process_management import ProcessManagementService

    service = ProcessManagementService(workflow_service, **service_options)
    app.state.process_management = service
    app.include_router(create_process_management_router(lambda: service))
    return service


__all__ = ["create_process_management_router", "error_response", "install_process_management_api"]
