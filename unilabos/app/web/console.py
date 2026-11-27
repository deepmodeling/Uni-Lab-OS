"""Uni-Lab-OS 内置 React 控制台的静态下发边界。"""

import os
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.datastructures import Headers
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from unilabos.app.center_client.config import LAUNCHER_HTTP_BASE_URL_ENV, _normalize_http_base_url
from unilabos.app.center_client.console_address import local_browser_origin
from unilabos.config.config import BasicConfig
from unilabos.utils.log import info

DEFAULT_CONSOLE_DIRECTORY = Path(__file__).parent / "static" / "console"
_CONSOLE_ROUTE_NAME = "unilab-console"


class ConsoleStaticFiles(StaticFiles):
    """下发静态文件并设置缓存策略；缺失资源保持 404。"""

    async def get_response(self, path: str, scope: Scope) -> Response:
        """入口及无哈希资源每次校验，构建后的哈希资源可长期缓存。"""

        response = await super().get_response(path, scope)

        requested_path = path.strip("/")
        if response.status_code == 200:
            if requested_path in {"", ".", "index.html", "og.png"}:
                response.headers["Cache-Control"] = "no-cache"
            else:
                response.headers["Cache-Control"] = (
                    "public, max-age=31536000, immutable"
                )
        return response


def _normalise_host(value: str) -> str:
    """把配置或请求中的 hostname 归一为无方括号、无尾点的小写形式。"""

    return value.strip().lower().removeprefix("[").removesuffix("]").rstrip(".")


def _allowed_frontend_hosts() -> set[str]:
    return {
        _normalise_host(item)
        for item in str(BasicConfig.frontend_allowed_hosts).split(",")
        if item.strip()
    }


def _effective_port(scheme: str, port: int | None) -> int | None:
    if port is not None:
        return port
    return {"http": 80, "https": 443}.get(scheme)


def _is_same_origin(request: Request, origin: str) -> bool:
    try:
        parsed = urlsplit(origin)
        origin_hostname = parsed.hostname
        origin_port = parsed.port
        request_hostname = request.url.hostname
        request_port = request.url.port
    except ValueError:
        return False
    if (
        parsed.scheme not in {"http", "https"}
        or not origin_hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        return False
    return (
        parsed.scheme == request.url.scheme
        and _normalise_host(origin_hostname)
        == _normalise_host(request_hostname or "")
        and _effective_port(parsed.scheme, origin_port)
        == _effective_port(request.url.scheme, request_port)
    )


async def _enforce_console_security(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    allowed_hosts = _allowed_frontend_hosts()
    try:
        request_host = _normalise_host(request.url.hostname or "")
    except ValueError:
        request_host = ""
    if allowed_hosts and request_host not in allowed_hosts:
        return JSONResponse(
            status_code=403,
            content={"detail": "request host is not allowed"},
        )

    if BasicConfig.frontend_same_origin and request.url.path.startswith("/api/"):
        fetch_site = request.headers.get("sec-fetch-site", "").lower()
        origin = request.headers.get("origin")
        if fetch_site == "cross-site" or (
            origin is not None and not _is_same_origin(request, origin)
        ):
            return JSONResponse(
                status_code=403,
                content={"detail": "cross-site browser request is not allowed"},
            )
    return await call_next(request)


class DesktopOriginMiddleware:
    """桌面直连入口只接受实际接入地址和显式公布地址，HTTP 与 WebSocket 共用边界。"""

    def __init__(self, app: ASGIApp, advertised_origin: str | None) -> None:
        self.app = app
        self.advertised_origin = advertised_origin

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        host, port = scope["server"]
        origin = "http://" + headers.get("host", "").lower()
        target = origin.removesuffix(":80")
        allowed = {local_browser_origin(host, port).removesuffix(":80"), self.advertised_origin}
        source = headers.get("origin", origin).lower().removesuffix(":80")
        if target not in allowed or source != target or headers.get("sec-fetch-site") == "cross-site":
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            else:
                await JSONResponse({"detail": "不允许的桌面访问来源"}, status_code=403)(scope, receive, send)
            return
        if scope["type"] == "http":
            try:
                length = int(headers.get("content-length", "0"))
            except ValueError:
                await JSONResponse({"detail": "请求长度无效"}, status_code=400)(scope, receive, send)
                return
            if not 0 <= length <= 64 * 1024 * 1024 or "transfer-encoding" in headers:
                await JSONResponse({"detail": "请求正文过大或传输格式不支持"}, status_code=413)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def install_console_security(app: FastAPI) -> bool:
    """安装可配置的 Host 与浏览器同源边界；重复调用保持幂等。"""

    if getattr(app.state, "unilab_console_security_installed", False):
        return False
    app.add_middleware(BaseHTTPMiddleware, dispatch=_enforce_console_security)
    if os.environ.get("UNILABOS_DESKTOP_WEB_HOST"):
        advertised = os.environ.get(LAUNCHER_HTTP_BASE_URL_ENV)
        app.add_middleware(
            DesktopOriginMiddleware,
            advertised_origin=_normalize_http_base_url(advertised).lower().removesuffix(":80")
            if advertised else None,
        )
    app.state.unilab_console_security_installed = True
    return True


def install_console_ui(
    app: FastAPI,
    directory: Path | None = None,
) -> bool:
    """从根地址直接下发已编译页面，不为旧 ``/console`` 地址提供跳转。

    参数：``app`` 是 Web 组合根；``directory`` 可在测试中注入临时构建产物。
    返回：本次是否完成首次安装。缺少 ``index.html`` 时保持 API 可用并返回
    ``False``；重复调用同样返回 ``False``。
    """

    console_directory = Path(directory or os.environ.get("UNILABOS_DESKTOP_CONSOLE_DIR") or DEFAULT_CONSOLE_DIRECTORY)
    if not (console_directory / "index.html").is_file():
        info(f"[Web] React 控制台构建产物不存在，跳过挂载: {console_directory}")
        return False
    if any(
        getattr(route, "name", None) == _CONSOLE_ROUTE_NAME
        for route in app.routes
    ):
        return False

    static_files = ConsoleStaticFiles(directory=console_directory)

    @app.api_route(
        "/",
        methods=["GET", "HEAD"],
        include_in_schema=False,
        name=_CONSOLE_ROUTE_NAME,
    )
    async def console_index(request: Request) -> Response:
        return await static_files.get_response("index.html", request.scope)

    # 与 developer-web 的独立页面导航对应，刷新时从同一入口恢复当前页面。
    for page in ("devices", "reagents", "materials", "workflows", "tasks", "schedule"):
        app.add_api_route(
            f"/{page}",
            console_index,
            methods=["GET", "HEAD"],
            include_in_schema=False,
            name=f"unilab-console-{page}",
        )

    app.mount(
        "/assets",
        ConsoleStaticFiles(directory=console_directory / "assets"),
        name="unilab-console-assets",
    )

    @app.api_route(
        "/og.png",
        methods=["GET", "HEAD"],
        include_in_schema=False,
        name="unilab-console-preview",
    )
    async def console_preview(request: Request) -> Response:
        return await static_files.get_response("og.png", request.scope)

    # 中心 iframe 使用独立的桥接页入口 /embed/<module>。
    @app.api_route(
        "/embed",
        methods=["GET", "HEAD"],
        include_in_schema=False,
        name="unilab-console-embed",
    )
    async def console_embed(request: Request) -> Response:
        return await static_files.get_response("index.html", request.scope)

    @app.api_route(
        "/embed/{rest:path}",
        methods=["GET", "HEAD"],
        include_in_schema=False,
        name="unilab-console-embed-spa",
    )
    async def console_embed_spa(rest: str, request: Request) -> Response:
        del rest  # 路由匹配用；实际返回统一的 SPA 入口
        return await static_files.get_response("index.html", request.scope)

    return True
