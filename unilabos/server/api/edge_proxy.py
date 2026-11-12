"""调度权威进程把 Host 专有路由经控制面 WS 交给 Host 在进程内执行。

默认拓扑下 ``unilab`` 进程是调度权威（Scheduler / Workflow / Registry / Materials），
持有浏览器连接的管理端口；Host 作为子进程运行，**不监听任何端口**：它只主动发 HTTP
（物料 / 注册表 / 工作流上报、请求结果回送）和维持一条控制 WS。浏览器请求 Host 专有的
路由（设备、遥测、历史、HostLink、驱动包、受管进程）时，权威把请求作为 ``backend_http``
下发到 Host，Host 对自己的 ASGI 应用执行后用 HTTP 把结果 POST 回权威，这里再回给浏览器。

转发是字节级透传：Host 自己的路由已经输出统一信封（``api/envelope.py``），这里不再包装。
Host 不在线时返回 HTTP 200 + 业务码 5001（带 ``Retry-After``），前端按能力面降级。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from fastapi import APIRouter, Request, Response

from unilabos.protocol.runtime.control import EdgeHttpResponse

logger = logging.getLogger(__name__)

#: ``/api/v1/<首段>`` 命中即交给 Host 执行。其余域由权威进程自己回答：workflow /
#: registry / materials / inventory / graphs（Graph Authority 在 materials.db）/ lab（布局在 materials.db，
#: 网格在 runtime.db，跨 Host 重启保留）/ scheduler / restart / health / ping / error-decisions。
EDGE_ROUTE_PREFIXES = (
    "runtime",
    "telemetry",
    "history",
    "hostlink",
    "status-incidents",
    "driver-packages",
    "device-processes",
    # V1 工作区只读面：驱动包挂载与工作目录浏览都描述 Host 的工作区
    "workspace",
    "file-browser",
)

_ALL_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
# 逐跳头 / 由本进程重新计算的头 / CORS 由本进程中间件统一加
_DROP_REQUEST_HEADERS = {"host", "content-length", "transfer-encoding", "connection", "keep-alive"}
_DROP_RESPONSE_HEADERS = {
    "content-length",
    "transfer-encoding",
    "connection",
    "keep-alive",
    "content-encoding",
    "access-control-allow-origin",
    "access-control-allow-credentials",
    "access-control-allow-methods",
    "access-control-allow-headers",
    "access-control-expose-headers",
    "vary",
}
#: 受管进程启动 / 驱动包卸载这类同步动作可能要几秒
PROXY_TIMEOUT_S = 60.0
_UNAVAILABLE_DETAIL = "Host 执行进程暂不可用（启动或重启中），请稍后重试"

_enabled = False
# Host 启动 / 重启期间前端每秒轮询多条 Host 路由，逐条打日志只会淹掉控制台：
# 只在"开始拒绝"与"恢复"两个状态切换点各记一条。
_host_offline_logged = False


def configure_edge_proxy(enabled: bool) -> None:
    """本进程是否把 Host 专有路由交给控制面上的 Host 执行。"""

    global _enabled, _host_offline_logged
    _enabled = bool(enabled)
    _host_offline_logged = False


def _note_host_offline(method: str, path: str) -> None:
    global _host_offline_logged
    if _host_offline_logged:
        return
    _host_offline_logged = True
    logger.info(
        "[EdgeProxy] Host 尚未接入控制面，Host 专有路由暂返回 503（首个请求 %s %s）",
        method,
        path,
    )


def _note_host_online() -> None:
    global _host_offline_logged
    if not _host_offline_logged:
        return
    _host_offline_logged = False
    logger.info("[EdgeProxy] Host 已接入，Host 专有路由恢复转发")


def edge_proxy_enabled() -> bool:
    return _enabled


def edge_http(
    method: str,
    path: str,
    *,
    headers: Optional[dict[str, str]] = None,
    body: bytes = b"",
    timeout: float = PROXY_TIMEOUT_S,
) -> Optional[EdgeHttpResponse]:
    """进程内调用：让 Host 执行一条请求；Host 不在线 / 超时返回 None。"""

    from unilabos.server.backend.edge_control import get_edge_control_service

    service = get_edge_control_service()
    if service is None or not service.connected:
        return None
    return service.http_request(method, path, headers=headers, body=body, timeout=timeout)


def edge_response_data(response: EdgeHttpResponse) -> Any:
    """Host 执行结果 → 信封 ``data``。

    Host 自己的路由输出统一信封，进程内调用方（权威经控制面读 Host 数据）用它解包；
    ``code != 0`` 或非信封的 HTTP 错误抛 ``EnvelopeError``（``status_code`` 为 HTTP 语义状态码）。
    """

    from unilabos.client.utils.envelope import decode_json_body

    return decode_json_body(response.body_bytes(), http_status=response.status_code)


def _unavailable() -> Response:
    from unilabos.server.api.envelope import SERVICE_UNAVAILABLE, error_response

    return error_response(
        SERVICE_UNAVAILABLE,
        _UNAVAILABLE_DETAIL,
        headers={"Retry-After": "3"},
    )


async def _forward(request: Request) -> Response:
    if not _enabled:
        return _unavailable()
    path = request.url.path
    if request.url.query:
        path = f"{path}?{request.url.query}"
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in _DROP_REQUEST_HEADERS
    }
    body = await request.body()
    from unilabos.server.backend.edge_control import get_edge_control_service

    service = get_edge_control_service()
    if service is None or not service.connected:
        _note_host_offline(request.method, path)
        return _unavailable()
    # http_request 阻塞等 Host 回结果；放线程池，别占住事件循环
    upstream = await asyncio.to_thread(
        service.http_request,
        request.method,
        path,
        headers=headers,
        body=body,
        timeout=PROXY_TIMEOUT_S,
    )
    if upstream is None:
        # Host 在线但超时 / 中途掉线：EdgeControl 已按请求告警，这里不再重复
        return _unavailable()
    _note_host_online()
    response_headers = {
        key: value
        for key, value in upstream.headers.items()
        if key.lower() not in _DROP_RESPONSE_HEADERS
    }
    return Response(
        content=upstream.body_bytes(),
        status_code=upstream.status_code,
        headers=response_headers,
    )


def create_edge_proxy_router() -> APIRouter:
    """Host 专有路由的透传路由器；必须先于本进程其它路由挂载才能优先匹配。

    普通 ``APIRouter``（不是信封路由）：响应体是 Host 已经包好信封的原始字节。
    """

    router = APIRouter(include_in_schema=False)

    async def _proxy(request: Request, path: str = "") -> Response:
        return await _forward(request)

    for prefix in EDGE_ROUTE_PREFIXES:
        router.add_api_route(f"/api/v1/{prefix}", _proxy, methods=_ALL_METHODS)
        router.add_api_route(f"/api/v1/{prefix}/{{path:path}}", _proxy, methods=_ALL_METHODS)
    return router


__all__ = [
    "EDGE_ROUTE_PREFIXES",
    "configure_edge_proxy",
    "create_edge_proxy_router",
    "edge_http",
    "edge_proxy_enabled",
    "edge_response_data",
]
