"""本机 MCP 接入：Streamable HTTP /mcp（无状态、POST 返回 JSON；GET 不开 SSE 流，回 405），复用既有 protocol 与管理端口。"""

from __future__ import annotations

import ipaddress
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.routing import Route

from unilabos.server.api.envelope import OPERATION_NOT_ALLOWED, SERVICE_UNAVAILABLE, error_response

from .server import ProtocolMCP


class _LocalEndpoint:
    def __init__(self, app: FastAPI) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("method") == "GET":
            # 不提供 Streamable HTTP 的独立 SSE 流：无状态模式下服务端没有主动推送，那条流也没有 id 和事件存储，
            # 断线即丢。按 MCP 规范回 405，客户端只用 POST（JSON 响应）。
            await error_response(
                OPERATION_NOT_ALLOWED, "MCP 不提供 SSE 流，请用 POST", symbol="method_not_allowed",
                http_status=405, headers={"Allow": "POST"},
            )(scope, receive, send)
            return
        peer = (scope.get("client") or ("", 0))[0]
        try:
            address = ipaddress.ip_address(peer)
            local = address.is_loopback or bool(getattr(address, "ipv4_mapped", None) and address.ipv4_mapped.is_loopback)
        except ValueError:
            local = False
        # 浏览器不能借 CORS 向本机设备发 MCP 写请求；原有前端 HTTP 协议完全不变。
        has_origin = any(key.lower() == b"origin" for key, _ in scope.get("headers", []))
        if not local or has_origin:
            # 拒绝正文与管理 API 同为信封（403 → 1001），保留 HTTP 状态码
            await error_response(
                OPERATION_NOT_ALLOWED, "MCP 仅允许本机非浏览器客户端", http_status=403
            )(scope, receive, send)
            return
        manager = getattr(self.app.state, "mcp_manager", None)
        if manager is None:
            await error_response(SERVICE_UNAVAILABLE, "MCP 尚未就绪", http_status=503)(scope, receive, send)
            return
        await manager.handle_request(scope, receive, send)


def install_mcp(app: FastAPI, document: dict[str, Any] | None = None) -> ProtocolMCP:
    """幂等安装；lifespan 随微后端启动/关闭，不引入额外守护进程。"""
    if getattr(app.state, "protocol_mcp", None) is not None:
        return app.state.protocol_mcp
    if document is None:
        from unilabos.server.openapi_export import export_openapi

        document = export_openapi()
    protocol = ProtocolMCP(app, document)
    app.state.protocol_mcp = protocol
    previous_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with previous_lifespan(application) as state:
            manager = StreamableHTTPSessionManager(
                app=protocol.server, stateless=True, json_response=True,
                security_settings=TransportSecuritySettings(
                    allowed_hosts=["127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*", "[::1]", "[::1]:*"],
                    allowed_origins=[],
                ),
            )
            async with manager.run():
                application.state.mcp_manager = manager
                try:
                    yield state
                finally:
                    application.state.mcp_manager = None

    app.router.lifespan_context = lifespan
    app.router.routes.append(Route("/mcp", _LocalEndpoint(app), methods=["GET", "POST", "DELETE"]))
    return protocol


__all__ = ["install_mcp"]
