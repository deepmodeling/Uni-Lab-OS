"""微后端 HTTP Application 与 Uvicorn 生命周期。"""

import asyncio
import errno
import json
from urllib.parse import quote
import socket
import sys
import threading
import webbrowser

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.requests import ClientDisconnect
from starlette.responses import Response

from unilabos.config.config import BasicConfig, HTTPConfig
from unilabos.config.control_token import TOKEN_HEADER, authority_token, token_accepted
from unilabos.server.api.envelope import SERVICE_UNAVAILABLE, UNAUTHORIZED, error_response
from unilabos.server import lifecycle
from unilabos.utils.fastapi.log_adapter import setup_fastapi_logging
from unilabos.utils.log import info, error
from unilabos.utils.tracing import install_http_tracing

# 优雅停机上限（秒）：等在途 HTTP 请求收尾，但不为 SSE 长连接无限等待。
GRACEFUL_SHUTDOWN_TIMEOUT_S = 5

#: 内置兜底：索引读不到（离线 / 内网）时「推荐前端」至少有这一张卡。
RECOMMENDED_FRONTENDS = (
    {
        "name": "OpenLab",
        "url": "https://xuwznln.github.io/OpenLabSite/",
        "description": "面向 Uni-Lab OS 微后端的社区实验室前端。",
        # 本页经 URL 片段把控制令牌交给它（见 _TOKEN_HANDOFF_SCRIPT）
        "token_handoff": True,
    },
)

#: 前端站点索引（awesome-lab-sites/v1）。导航页在浏览器里直接读它补充「推荐前端」
#: 卡片——与 OpenLab 读 awesome-lab-devices 同一套模式，Edge 进程本身不出网。
#: 内网部署可改成镜像地址；索引里与兜底卡同 URL 的条目只刷新文案，不重复出卡。
SITE_INDEX_URL = "https://raw.githubusercontent.com/Xuwznln/awesome-lab-sites/main/index.json"
SITE_INDEX_REPO_URL = "https://github.com/Xuwznln/awesome-lab-sites"

DEVELOPER_LINKS = (
    {
        "name": "OpenAPI Explorer",
        "url": "/api/docs",
        "description": "在浏览器中查看并调用当前微后端 API。",
    },
    {
        "name": "ReDoc",
        "url": "/api/redoc",
        "description": "适合阅读完整 HTTP 契约的只读 API 文档。",
    },
    {
        "name": "DB Debug",
        "url": "/api/docs#/debug",
        "description": "四库 SQLite 只读浏览：表清单、行数与最新行数据。",
    },
    {
        "name": "Uni-Lab OS Documentation",
        "url": "https://deepmodeling.github.io/Uni-Lab-OS/",
        "description": "GitHub Pages 上的官方接入、设备和部署文档。",
    },
)

app = FastAPI(
    title="Uni-Lab-OS Microbackend API",
    description="Backend-only API service for Uni-Lab frontends and schedulers.",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
)
install_http_tracing(app)

edge_routes_mounted = False
materials_routes_mounted = False
server_routes_mounted = False
workflow_routes_mounted = False
registry_routes_mounted = False
driver_packages_mounted = False
edge_proxy_mounted = False

def cors_allow_origins() -> list[str]:
    """``BasicConfig.cors_allow_origins``：逗号分隔的来源；空或含 ``*`` 即全部允许（默认）。"""

    raw = str(getattr(BasicConfig, "cors_allow_origins", "*") or "*")
    origins = [item.strip().rstrip("/") for item in raw.split(",") if item.strip()]
    return ["*"] if not origins or "*" in origins else origins


class ConfiguredCORSMiddleware:
    """按 ``BasicConfig.cors_allow_origins`` 放行跨域请求（默认全部来源）。

    app 模块导入时配置文件可能还没加载，所以在第一个请求到达时才按配置构造 CORSMiddleware。
    """

    def __init__(self, app) -> None:
        self._app = app
        self._inner = None

    async def __call__(self, scope, receive, send) -> None:
        if self._inner is None:
            self._inner = CORSMiddleware(
                self._app,
                allow_origins=cors_allow_origins(),
                allow_credentials=True,
                allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
                # Authorization 留给 V1 工站 api_key；控制面 token 只认 X-UniLab-Token
                allow_headers=[
                    "Authorization",
                    "Content-Type",
                    "Accept",
                    "Last-Event-ID",
                    "Idempotency-Key",
                    "X-Workflow-Filename",
                    TOKEN_HEADER,
                ],
            )
        await self._inner(scope, receive, send)


#: 需要控制面 token 的 HTTP 方法：管理 API 的一切写操作。读（GET / HEAD）和预检（OPTIONS）不需要。
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
#: 不经 token 检查的写入口：MCP 端点自带 Host 白名单并拒绝一切浏览器 Origin（见 server/mcp）。
TOKEN_EXEMPT_PATHS = ("/mcp",)


class ControlTokenMiddleware:
    """管理 API 的写请求必须在请求头 ``X-UniLab-Token`` 出示控制面 token。

    CORS 默认放行所有来源，任何网页都能向本机后端发请求；没有 token 时，随便打开的一个网页就能
    创建任务、启动设备。token 见 ``unilabos/config/control_token.py``；本进程没有 token（单元测试、
    嵌入式装配）时不检查。``Authorization: Bearer`` 留给 V1 工站调用的 api_key，这里不读取。
    被拒的请求返回 HTTP 401 + 信封 ``{"code": 1002, "error": {"msg": ...}}``，经外层 CORS 中间件
    补上跨域头，浏览器能读到原因。
    """

    def __init__(self, app) -> None:
        self._app = app

    async def __call__(self, scope, receive, send) -> None:
        if (
            scope.get("type") == "http"
            and scope.get("method") in WRITE_METHODS
            and not any(
                scope.get("path", "") == prefix or scope.get("path", "").startswith(prefix + "/")
                for prefix in TOKEN_EXEMPT_PATHS
            )
        ):
            from starlette.datastructures import Headers

            if not token_accepted(Headers(scope=scope)):
                message = (
                    f"写操作需要控制面 token：请求头 {TOKEN_HEADER}: <token>。"
                    "在运行 unilab 的机器上执行 `unilab token` 查看"
                )
                response = error_response(
                    UNAUTHORIZED,
                    message,
                    http_status=401,
                    headers={"WWW-Authenticate": f'{TOKEN_HEADER} realm="unilab"'},
                )
                await response(scope, receive, send)
                return
        await self._app(scope, receive, send)


# 中间件后加的在外层：先登记 token 检查、再登记 CORS，401 响应也带跨域头。
# noinspection PyTypeChecker
app.add_middleware(ControlTokenMiddleware)
# noinspection PyTypeChecker
app.add_middleware(ConfiguredCORSMiddleware)


@app.exception_handler(ClientDisconnect)
async def _on_client_disconnect(_request: Request, _exc: ClientDisconnect) -> Response:
    """客户端在请求体读完之前断开（刷新页面、取消请求）。

    FastAPI 自己解析 body 时会把这种情况吞成 400，但 Workflow / Registry 路由为了
    按 Backend 规则预读 JSON 会手动 ``await request.body()``，异常就会一路冒到
    uvicorn 并被打成整段 ``Exception in ASGI application`` 堆栈。响应此时已经发不
    出去，这里只让请求正常收尾；499 沿用 nginx 的 "Client Closed Request" 语义，
    仅供中间件与追踪记录。
    """

    return Response(status_code=499)


@app.middleware("http")
async def reset_maintenance_gate(request: Request, call_next):
    from unilabos.server.backend.reset import get_reset_controller

    controller = get_reset_controller()
    if controller is not None and controller.pending and request.method != "OPTIONS":
        if request.url.path not in ("/api/v1/reset", "/api/v1/health"):
            # 进程级维护闸门（不是某个路由的业务失败）：保留 HTTP 503，正文仍是信封 5001
            return error_response(SERVICE_UNAVAILABLE, "正在全量重置，停止接受业务请求", http_status=503)
    return await call_next(request)


@app.middleware("http")
async def log_requests(request: Request, call_next) -> Response:
    """执行 HTTP 请求链，为应用级请求中间件保留统一入口。"""

    return await call_next(request)


@app.middleware("http")
async def allow_private_network(request: Request, call_next) -> Response:
    """公网托管的前端（GitHub Pages）访问局域网微后端时，Chrome 的 Private Network Access
    预检会带 ``Access-Control-Request-Private-Network: true``，必须原样应答否则请求被丢弃。
    CORSMiddleware 不认识这个头，这里单独补上。"""

    response = await call_next(request)
    if request.headers.get("access-control-request-private-network", "").lower() == "true":
        response.headers["Access-Control-Allow-Private-Network"] = "true"
    return response


def _render_cards(items) -> str:
    return "".join(
        (
            '<a class="card" href="{url}" target="_blank" rel="noreferrer"{handoff}>'
            '<strong>{name}</strong><span>{description}</span>'
            '<code>{url}</code></a>'
        ).format(handoff=' data-token-handoff="1"' if item.get("token_handoff") else "", **item)
        for item in items
    )


#: 浏览器以 ``/#token=<t>`` 打开本页时（``unilab`` 启动时自动打开的地址），把 token 经 URL 片段交给
#: 内置推荐前端（``data-token-handoff``），站点索引里的第三方前端拿不到；随后从地址栏抹掉片段。
#: 片段不会发给任何服务器，本页也不把 token 写进 DOM 文本。
_TOKEN_HANDOFF_SCRIPT = """
<script>
(function () {
  var match = /(?:^#|&)token=([^&]+)/.exec(window.location.hash || "");
  if (!match) return;
  var token = decodeURIComponent(match[1]);
  try { history.replaceState(null, "", window.location.pathname + window.location.search); } catch (e) {}
  var backend = window.location.origin;
  document.querySelectorAll("a.card[data-token-handoff]").forEach(function (card) {
    var base = card.getAttribute("href").split("#")[0];
    card.setAttribute("href", base + "#/?backend=" + encodeURIComponent(backend) + "&token=" + encodeURIComponent(token));
  });
  var note = document.getElementById("token-note");
  if (note) note.textContent = "已为推荐前端带上本机的控制令牌（写操作需要它）。";
})();
</script>
"""


#: 浏览器侧读取站点索引并补卡。只用 DOM API 写入文本，索引内容不进 innerHTML。
_SITE_INDEX_SCRIPT = """
<script>
(function () {
  var url = __SITE_INDEX_URL__;
  var grid = document.getElementById("frontends");
  var note = document.getElementById("frontends-note");
  if (!grid || typeof fetch !== "function") return;
  var controller = typeof AbortController === "function" ? new AbortController() : null;
  var timer = controller ? setTimeout(function () { controller.abort(); }, 8000) : null;
  fetch(url, { cache: "no-cache", signal: controller ? controller.signal : undefined })
    .then(function (response) {
      if (!response.ok) throw new Error("HTTP " + response.status);
      return response.json();
    })
    .then(function (index) {
      var sites = index && Array.isArray(index.sites) ? index.sites : [];
      var byUrl = {};
      grid.querySelectorAll("a.card").forEach(function (card) { byUrl[card.getAttribute("href")] = card; });
      sites.forEach(function (site) {
        if (!site || typeof site.url !== "string" || !/^https:\\/\\//.test(site.url)) return;
        var name = site.name || site.id || site.url;
        var existing = byUrl[site.url];
        if (existing) {
          existing.querySelector("strong").textContent = name;
          if (site.description) existing.querySelector("span").textContent = site.description;
          return;
        }
        var card = document.createElement("a");
        card.className = "card";
        card.href = site.url;
        card.target = "_blank";
        card.rel = "noreferrer";
        var strong = document.createElement("strong");
        strong.textContent = name;
        var span = document.createElement("span");
        span.textContent = site.description || "";
        var code = document.createElement("code");
        code.textContent = site.url;
        card.appendChild(strong);
        card.appendChild(span);
        card.appendChild(code);
        grid.appendChild(card);
        byUrl[site.url] = card;
      });
      if (note) {
        note.textContent = " · " + sites.length + " 个站点" + (index.updated_at ? "，更新于 " + index.updated_at : "");
      }
    })
    .catch(function (error) {
      if (note) note.textContent = " · 不可达（" + ((error && error.message) || error) + "），仅显示内置推荐";
    })
    .then(function () { if (timer) clearTimeout(timer); });
})();
</script>
"""


def _render_site_index_note(site_index_url: str) -> str:
    return (
        '<p class="index-note">站点目录来自 '
        f'<a href="{SITE_INDEX_REPO_URL}" target="_blank" rel="noreferrer">awesome-lab-sites</a>'
        f'<span id="frontends-note"></span> · <code>{site_index_url}</code></p>'
    )


def _render_catalog_page(title: str, intro: str, sections, *, site_index_url: str = "") -> str:
    """``sections`` 为 ``(section_id, heading, items)``；``site_index_url`` 非空时，
    id 为 ``frontends`` 的分区会在浏览器里读取站点索引补卡。"""

    body = "".join(
        f'<h2>{heading}</h2><section class="grid" id="{section_id}">{_render_cards(items)}</section>'
        + (_render_site_index_note(site_index_url) if site_index_url and section_id == "frontends" else "")
        for section_id, heading, items in sections
    )
    script = (
        _SITE_INDEX_SCRIPT.replace("__SITE_INDEX_URL__", json.dumps(site_index_url))
        if site_index_url
        else ""
    ) + _TOKEN_HANDOFF_SCRIPT
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    body {{ margin: 0; font: 16px/1.55 system-ui, sans-serif; color: #18212f; background: #f6f8fb; }}
    main {{ max-width: 760px; margin: 10vh auto; padding: 0 24px; }}
    h1 {{ margin-bottom: 8px; }}
    h2 {{ margin: 30px 0 10px; font-size: 18px; }}
    p {{ color: #526173; }}
    .grid {{ display: grid; gap: 14px; margin-top: 28px; }}
    .card {{ display: grid; gap: 5px; padding: 18px; color: inherit; text-decoration: none;
      background: white; border: 1px solid #dce3ec; border-radius: 10px; }}
    .card:hover {{ border-color: #4c78ff; box-shadow: 0 5px 18px #25385816; }}
    .card span {{ color: #526173; }}
    code {{ color: #3157c8; overflow-wrap: anywhere; }}
    .index-note {{ margin: 10px 0 0; font-size: 13px; }}
    .index-note code {{ font-size: 12px; }}
  </style>
</head>
<body><main>
  <h1>{title}</h1>
  <p>{intro}</p>
  <p id="token-note" class="index-note"></p>
  {body}
</main>{script}</body>
</html>"""


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def frontend_catalog() -> str:
    """返回社区前端与开发工具的入口导航。

    前端只以独立静态站（GitHub Pages）形式部署，本进程不托管页面。
    配置了 --address 的 Host 只提供执行面，Workflow/调度 API 在它的 Backend 地址上；
    页面退化为指向 Backend 的路标，避免用户把前端连到本进程。
    """

    remote = (HTTPConfig.remote_addr or "").rstrip("/")
    if remote:
        sections = (
            (
                "authority",
                "调度权威",
                (
                    {
                        "name": "Backend 管理页",
                        "url": f"{remote}/",
                        "description": "Workflow/调度 API 与前端接入地址；前端请连接这里。",
                    },
                ),
            ),
            ("developer", "本进程调试（仅设备侧 API）", DEVELOPER_LINKS),
        )
        return _render_catalog_page(
            "Uni-Lab-OS Edge 进程",
            f"本进程运行设备执行与数据面，调度权威位于 <code>{remote}</code>。"
            "工作流编排与前端请访问调度权威地址。",
            sections,
        )
    return _render_catalog_page(
        "Uni-Lab-OS Microbackend",
        "此进程提供 Uni-Lab-OS 后端 API。请选择 API 工具，或使用部署在 GitHub "
        "Pages 上的社区前端连接当前地址。",
        (
            ("frontends", "推荐前端", RECOMMENDED_FRONTENDS),
            ("developer", "开发与接入", DEVELOPER_LINKS),
        ),
        site_index_url=SITE_INDEX_URL,
    )


def _has_execution_face() -> bool:
    """本进程是否带设备执行面（Host）；--role backend 为 False。"""
    try:
        from unilabos.server.backend.composition import get_execution_backend

        return get_execution_backend() is not None
    except Exception:  # noqa: BLE001
        return False


def setup_server() -> FastAPI:
    """幂等挂载当前运行角色所需的 API 路由。"""
    global edge_routes_mounted, materials_routes_mounted, server_routes_mounted
    global workflow_routes_mounted, registry_routes_mounted, driver_packages_mounted
    global edge_proxy_mounted

    from unilabos.server.api.edge_proxy import create_edge_proxy_router, edge_proxy_enabled

    # 调度权威带 Host 子进程：Host 专有路由（runtime / telemetry / history / lab /
    # graphs / hostlink / status-incidents / 驱动包 / 受管进程）转发给子进程。
    # 必须最先挂载，才能优先于下面本进程自己的同名路由。
    proxying = edge_proxy_enabled()
    if proxying and not edge_proxy_mounted:
        app.include_router(create_edge_proxy_router())
        edge_proxy_mounted = True

    # 驱动包台账 / 受管设备进程：只在带执行面（加载注册表、跑 HostLink server）的
    # Host 进程有意义，--role backend 不挂载。
    if not driver_packages_mounted and not proxying and _has_execution_face():
        try:
            from unilabos.server.api.device_processes import create_device_processes_router
            from unilabos.server.api.driver_package_graphs import (
                create_driver_package_graphs_router,
            )
            from unilabos.server.api.driver_packages import create_driver_packages_router
            from unilabos.server.api.host_relay import create_host_relay_router
            from unilabos.server.api.runtime.logs import create_runtime_logs_router

            app.include_router(create_driver_packages_router())
            app.include_router(create_driver_package_graphs_router())
            app.include_router(create_device_processes_router())
            app.include_router(create_runtime_logs_router())
            from unilabos.server.api.workspace import create_workspace_router

            # V1 工作区只读面（驱动包挂载 / 工作目录浏览）
            app.include_router(create_workspace_router())
            # 调度权威（materials 权威）把物料投影下行经这里送到设备
            app.include_router(create_host_relay_router())
            driver_packages_mounted = True
        except Exception as exc:  # noqa: BLE001 - 保留基础管理 API
            error(f"[Microbackend] 挂载驱动包 / 设备进程路由失败: {exc}")

    # Backend 诊断面不复制 Runtime/History/Telemetry 数据 API。
    if not edge_routes_mounted:
        try:
            from unilabos.server.api.runtime import create_backend_router
            from unilabos.server.backend.composition import (
                get_execution_backend,
                get_scheduler,
            )

            app.include_router(
                create_backend_router(
                    get_scheduler,
                    get_execution_backend,
                )
            )
            edge_routes_mounted = True
        except Exception as exc:  # noqa: BLE001 - 保留基础管理 API
            error(f"[Microbackend] 挂载执行观测路由失败: {exc}")

    # 调度权威随本进程装配时挂载 Workflow Authority 写 API；配置了 --address 的 Host
    # get_workflow_service() 为 None，不挂载（写入口在 Backend 地址上）。
    if not workflow_routes_mounted:
        try:
            from unilabos.server.backend.composition import get_workflow_service
            from unilabos.server.api.runtime.workflow import install_workflow_api

            workflow_service = get_workflow_service()
            if workflow_service is not None:
                install_workflow_api(app, workflow_service)
                workflow_routes_mounted = True
        except Exception as exc:  # noqa: BLE001 - 保留基础管理 API
            error(f"[Microbackend] 挂载本机 Workflow Authority 失败: {exc}")

    # Registry Authority 与 Workflow Authority 同归属：调度权威随本进程装配时挂载
    # 条目级注册表版本 API；只提供执行面的 Host 不挂载。
    if not registry_routes_mounted:
        try:
            from unilabos.server.api.runtime.registry import install_registry_api
            from unilabos.server.services.runtime.registry import get_registry_service

            if get_registry_service() is not None:
                install_registry_api(app)
                registry_routes_mounted = True
        except Exception as exc:  # noqa: BLE001 - 保留基础管理 API
            error(f"[Microbackend] 挂载 Registry Authority 失败: {exc}")

    if not server_routes_mounted:
        try:
            from unilabos.server.api import install_server_apis
            from unilabos.server.composition import get_server_services
            from unilabos.server.backend.composition import get_materials_service

            services = get_server_services()
            if services is not None:
                include_materials = get_materials_service() is not None
                install_server_apis(
                    app,
                    services,
                    include_materials=include_materials,
                    include_host_data=not proxying,
                )
                server_routes_mounted = True
                materials_routes_mounted = include_materials
        except Exception as exc:  # noqa: BLE001 - 保留基础管理 API
            error(f"[Microbackend] 挂载四库 API 失败: {exc}")

    # 支持只装配 MaterialsService 的测试或嵌入式运行方式。
    if not materials_routes_mounted:
        try:
            from unilabos.server.api.materials import install_materials_api
            from unilabos.server.backend.composition import get_materials_service

            materials_service = get_materials_service()
            if materials_service is not None:
                install_materials_api(app, materials_service)
                materials_routes_mounted = True
        except Exception as exc:  # noqa: BLE001 - 保留基础管理 API
            error(f"[Microbackend] 挂载 Materials Provider 失败: {exc}")

    if not any(getattr(route, "path", "") == "/api/v1/events" for route in app.routes):
        from unilabos.server.api.runtime.events import create_runtime_events_router

        # 仅执行面的 Host 也使用同一通知通道，但不伪造 Workflow Authority。
        app.include_router(create_runtime_events_router(), prefix="/api/v1")
    from unilabos.server.mcp import install_mcp

    install_mcp(app)
    _routes_ready.set()
    return app


_uvicorn_server = None
# 不监听端口的 Host 子进程：主线程在这里等停机请求，管理 API 由权威经控制 WS 下发执行
_control_plane_stop = threading.Event()
_serving_over_control_plane = False
# 设备 runtime 线程致命失败：服务循环不再进入 / 立即返回（与停机请求不同，服务尚未开始也生效）
_abort_serving = threading.Event()
# setup_server() 挂完路由才置位。Host 子进程里控制 WS 先于主线程的 setup_server 连上权威，
# 极快的动作会在路由挂好之前就跑完并上报；权威随即经 backend_http 来拉结果 payload，
# 若此时对着还没挂 /history 的 app 执行就会 404。代理执行前先等这个事件。
_routes_ready = threading.Event()


def wait_routes_ready(timeout: float) -> bool:
    """等待本进程管理 API 路由挂载完成；返回是否就绪。"""

    return _routes_ready.wait(timeout)


def request_server_shutdown() -> bool:
    """请求管理 API 停机（安静点重启用）。

    uvicorn 主循环每个 tick 检查 ``should_exit``，置位后 ``start_server``
    返回，main 的正常退出链路（关库、停 backend）随之执行；不监听端口的 Host 子进程
    则唤醒 :func:`serve_over_control_plane` 的等待。

    Returns:
        bool: 服务正在运行且已收到停机请求。
    """
    server = _uvicorn_server
    if server is not None:
        lifecycle.begin_shutdown()
        server.should_exit = True
        return True
    if _serving_over_control_plane:
        lifecycle.begin_shutdown()
        _control_plane_stop.set()
        return True
    return False


def abort_serving() -> None:
    """设备 runtime 线程致命失败时由该线程调用：让主线程的服务循环尽快返回。

    与 :func:`request_server_shutdown` 的区别是不依赖服务是否已经开始——backend 线程
    往往在主线程进入 ``start_server`` / ``serve_over_control_plane`` 之前就失败了，
    只置 should_exit 会让主线程随后进入一个永远不会被唤醒的等待。
    """

    _abort_serving.set()
    lifecycle.begin_shutdown()
    request_server_shutdown()


def serve_over_control_plane() -> None:
    """Host 子进程形态：挂好路由但不绑端口，阻塞到收到停机请求。

    请求由调度权威经控制 WS（``backend_http``）下发，WS 客户端对本进程的 ASGI 应用
    在进程内执行；本函数只负责让主线程活着并响应 ``request_server_shutdown``。
    """

    global _serving_over_control_plane
    setup_server()
    _control_plane_stop.clear()
    if not _abort_serving.is_set():
        lifecycle.reset_shutdown_state()
    _serving_over_control_plane = True
    try:
        # 分段等待：Windows 上无限期 wait 收不到信号处理器（Ctrl+Break / SIGTERM）
        while not _abort_serving.is_set() and not _control_plane_stop.wait(0.5):
            pass
    finally:
        _serving_over_control_plane = False


def browser_landing_url(host: str, port: int) -> str:
    """启动时浏览器应打开的页面。

    配置了 --address 的 Host 直达 Backend 的管理页（本进程 / 只是路标）；
    否则打开本进程页面。
    """

    remote = (HTTPConfig.remote_addr or "").rstrip("/")
    if remote:
        return f"{remote}/"
    # noinspection HttpUrlsUsage
    url = f"http://{host if host != '0.0.0.0' else 'localhost'}:{port}/"
    token = authority_token()
    # 片段不发给服务器；导航页把它交给内置推荐前端后立即从地址栏抹掉
    return f"{url}#token={quote(token, safe='')}" if token else url


class ManagementPortInUseError(OSError):
    """管理端 HTTP 端口已被其他进程占用。"""

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        super().__init__(
            errno.EADDRINUSE,
            f"管理端 HTTP 端口 {host}:{port} 已被占用。"
            f"请关闭占用该端口的程序（可能是上一个未退出的 Uni-Lab 进程），"
            f"或用 --port <其他端口> 换一个端口重新启动，例如 --port {port + 1}",
        )


_ADDR_IN_USE_ERRNOS = {errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", 10048)}


def _is_addr_in_use(exc: OSError) -> bool:
    return exc.errno in _ADDR_IN_USE_ERRNOS or getattr(exc, "winerror", None) == 10048


def ensure_port_available(host: str, port: int) -> None:
    """启动 Uvicorn 之前先试绑一次端口，把 WinError 10048 之类的原始错误换成可操作的提示。

    Raises:
        ManagementPortInUseError: 端口已被占用。
    """
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    probe = socket.socket(family, socket.SOCK_STREAM)
    try:
        # POSIX 与 Uvicorn 使用相同的 TIME_WAIT 复用语义；仍拒绝活动监听者。
        # Windows 的 SO_REUSEADDR 会允许共享监听端口，因此不启用。
        if sys.platform != "win32":
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind((host, port))
    except OSError as exc:
        if _is_addr_in_use(exc):
            raise ManagementPortInUseError(host, port) from exc
        # 其他绑定失败（无权限、地址不存在）交给 uvicorn 报原始错误
    finally:
        probe.close()


def start_server(host: str = "0.0.0.0", port: int = 8002, open_browser: bool = True) -> None:
    """
    启动服务器

    Args:
        host: 服务器主机
        port: 服务器端口
        open_browser: 是否自动打开浏览器

    Raises:
        ManagementPortInUseError: 端口已被占用，消息中包含 --port 的修改建议。
    """
    from uvicorn import Config, Server

    if _abort_serving.is_set():
        return
    ensure_port_available(host, port)
    lifecycle.reset_shutdown_state()

    class _SignalAwareServer(Server):
        """Ctrl+C / SIGTERM 由 uvicorn 自己的信号处理器接住：在这里同步打停机信号，
        让 SSE 长连接在 uvicorn 开始等待之前就自行结束。"""

        def handle_exit(self, sig, frame) -> None:  # type: ignore[override]
            lifecycle.begin_shutdown()
            super().handle_exit(sig, frame)

    # 设置服务器
    setup_server()

    # 配置日志
    log_config = setup_fastapi_logging()

    # 写操作需要控制面 token：调度权威 / 单进程模式在这里确定（已由 backend_main 确定时沿用）
    from unilabos.config.control_token import ensure_authority_token
    from unilabos.config.working_dir import resolve_working_dir

    ensure_authority_token(BasicConfig.working_dir or resolve_working_dir(None))

    # 启动前打开浏览器
    if open_browser:
        url = browser_landing_url(host, port)
        # 日志会转发给前端：只记不带片段（token）的地址
        info(f"[Web] 正在打开浏览器访问: {url.split('#', 1)[0]}")
        try:
            webbrowser.open(url)
        except Exception as e:
            error(f"[Microbackend] 无法打开浏览器: {str(e)}")

    # 启动服务器
    info(f"[Microbackend] 启动 FastAPI: {host}:{port}")

    # SSE 长连接会在停机信号后一个心跳周期内自行结束（见 server.lifecycle）；这里的
    # 上限只是兜底：万一还有别的在途请求拖着，超过后强制关闭，安静点重启才走得下去。
    config = Config(
        app=app,
        host=host,
        port=port,
        log_config=log_config,
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_TIMEOUT_S,
    )
    server = _SignalAwareServer(config)

    global _uvicorn_server
    _uvicorn_server = server
    # backend 线程可能恰好在上面几行之间失败：should_exit 让 uvicorn 起来后立刻退出
    if _abort_serving.is_set():
        server.should_exit = True
    try:
        if sys.platform == "win32":
            from unilabos.server.windows_event_loop import create_event_loop

            # 显式传给 Runner，不设置全局策略；也不依赖不同 Uvicorn 版本的 loop 配置格式。
            with asyncio.Runner(loop_factory=create_event_loop) as runner:
                runner.run(server.serve())
        else:
            server.run()
    except SystemExit:
        # uvicorn 绑定失败时自行 sys.exit(1)；预检和真正绑定之间端口仍可能被抢占，
        # 再探测一次即可区分「端口被占」与其他启动失败。
        if not server.started:
            ensure_port_available(host, port)
        raise
    finally:
        _uvicorn_server = None


# 当脚本直接运行时启动服务器
if __name__ == "__main__":
    start_server()
