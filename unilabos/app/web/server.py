"""
Web服务器模块

提供Web服务器功能，网页信息服务 + mqtt代替
"""

import webbrowser

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import Response

from unilabos.utils.fastapi.log_adapter import setup_fastapi_logging
from unilabos.utils.log import info, error
from unilabos.utils.tracing import install_http_tracing

RECOMMENDED_FRONTENDS = (
    {
        "name": "OpenLab",
        "url": "https://xuwznln.github.io/OpenLab-site/",
        "description": "面向 Uni-Lab OS 微后端的社区实验室前端。",
    },
)

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
        "name": "Uni-Lab OS Documentation",
        "url": "https://deepmodeling.github.io/Uni-Lab-OS/",
        "description": "GitHub Pages 上的官方接入、设备和部署文档。",
    },
)

# 创建FastAPI应用
app = FastAPI(
    title="UniLab Microbackend API",
    description="Backend-only API service for Uni-Lab frontends and schedulers.",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
)
install_http_tracing(app)

edge_routes_mounted = False
materials_routes_mounted = False
server_routes_mounted = False

# noinspection PyTypeChecker
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept", "Last-Event-ID"],
)


@app.middleware("http")
async def log_requests(request: Request, call_next) -> Response:
    """
    记录HTTP请求日志的中间件

    Args:
        request: 当前HTTP请求对象
        call_next: 下一个处理函数

    Returns:
        Response: HTTP响应对象
    """
    # # 打印请求信息
    # info(f"[Web] Request: {request.method} {request.url}", stack_level=1)
    # debug(f"[Web] Headers: {request.headers}", stack_level=1)
    #
    # # 使用日志模块记录请求体（如果需要）
    # body = await request.body()
    # if body:
    #     debug(f"[Web] Body: {body}", stack_level=1)

    # 调用下一个中间件或路由处理函数
    response = await call_next(request)

    # # 打印响应信息
    # info(f"[Web] Response status: {response.status_code}", stack_level=1)

    return response


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def frontend_catalog() -> str:
    """无内置前端；仅提供可连接当前微后端的入口导航。"""

    frontend_cards = "".join(
        (
            '<a class="card" href="{url}" target="_blank" rel="noreferrer">'
            '<strong>{name}</strong><span>{description}</span>'
            '<code>{url}</code></a>'
        ).format(**item)
        for item in RECOMMENDED_FRONTENDS
    )
    developer_cards = "".join(
        (
            '<a class="card" href="{url}" target="_blank" rel="noreferrer">'
            '<strong>{name}</strong><span>{description}</span>'
            '<code>{url}</code></a>'
        ).format(**item)
        for item in DEVELOPER_LINKS
    )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>UniLab Microbackend</title>
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
  </style>
</head>
<body><main>
  <h1>UniLab Microbackend</h1>
  <p>此进程只提供后端能力，不再内置状态页或工作流前端。请选择 API 工具，
  或从 GitHub Pages 部署的社区前端连接当前地址。</p>
  <h2>推荐前端</h2>
  <section class="grid">{frontend_cards}</section>
  <h2>开发与接入</h2>
  <section class="grid">{developer_cards}</section>
</main></body>
</html>"""


def setup_server() -> FastAPI:
    """
    设置服务器

    Returns:
        FastAPI: 配置好的FastAPI应用实例
    """
    global edge_routes_mounted, materials_routes_mounted, server_routes_mounted

    # Scheduler 路由只暴露微后端执行观测面；本地不创建 Workflow/DAG 权威。
    if not edge_routes_mounted:
        try:
            from unilabos.server.scheduler.api import create_scheduler_router
            from unilabos.server.scheduler.integration import (
                get_edge_backend,
                get_edge_scheduler,
            )

            app.include_router(
                create_scheduler_router(
                    get_edge_scheduler,
                    get_edge_backend,
                    get_history=lambda: None,
                    include_execution_shaped_workflow_routes=False,
                )
            )
            edge_routes_mounted = True
        except Exception as exc:  # noqa: BLE001 - 保留基础管理 API
            error(f"[Web] 挂载微后端执行路由失败: {exc}")

    if not server_routes_mounted:
        try:
            from unilabos.server.api import install_server_apis
            from unilabos.server.composition import get_server_services
            from unilabos.server.scheduler.integration import get_materials_service

            services = get_server_services()
            if services is not None:
                include_materials = get_materials_service() is not None
                install_server_apis(
                    app,
                    services,
                    include_materials=include_materials,
                )
                server_routes_mounted = True
                materials_routes_mounted = include_materials
        except Exception as exc:  # noqa: BLE001 - 保留基础管理 API
            error(f"[Web] 挂载微后端四库 API 失败: {exc}")

    # 兼容仅单独装配 MaterialsService 的测试或嵌入式调用。
    if not materials_routes_mounted:
        try:
            from unilabos.server.api.materials import install_materials_api
            from unilabos.server.scheduler.integration import get_materials_service

            materials_service = get_materials_service()
            if materials_service is not None:
                install_materials_api(app, materials_service)
                materials_routes_mounted = True
        except Exception as exc:  # noqa: BLE001 - 保留基础管理 API
            error(f"[Web] 挂载 Materials Provider 失败: {exc}")

    return app


def start_server(host: str = "0.0.0.0", port: int = 8002, open_browser: bool = True) -> bool:
    """
    启动服务器

    Args:
        host: 服务器主机
        port: 服务器端口
        open_browser: 是否自动打开浏览器

    Returns:
        bool: True if restart was requested, False otherwise
    """
    import threading
    import time
    from uvicorn import Config, Server

    # 设置服务器
    setup_server()

    # 配置日志
    log_config = setup_fastapi_logging()

    # 启动前打开浏览器
    if open_browser:
        # noinspection HttpUrlsUsage
        url = f"http://{host if host != '0.0.0.0' else 'localhost'}:{port}/"
        info(f"[Web] 正在打开浏览器访问: {url}")
        try:
            webbrowser.open(url)
        except Exception as e:
            error(f"[Web] 无法打开浏览器: {str(e)}")

    # 启动服务器
    info(f"[Web] 启动FastAPI服务器: {host}:{port}")

    # 使用支持重启的模式
    config = Config(app=app, host=host, port=port, log_config=log_config)
    server = Server(config)

    # 启动服务器线程
    server_thread = threading.Thread(target=server.run, daemon=True, name="uvicorn_server")
    server_thread.start()

    # info("[Web] Server started, monitoring for restart requests...")

    # 监控重启标志
    import unilabos.app.main as main_module

    while server_thread.is_alive():
        if hasattr(main_module, "_restart_requested") and main_module._restart_requested:
            info(
                f"[Web] Restart requested via WebSocket, reason: {getattr(main_module, '_restart_reason', 'unknown')}"
            )
            main_module._restart_requested = False

            # 停止服务器
            server.should_exit = True
            server_thread.join(timeout=5)

            info("[Web] Server stopped, ready for restart")
            return True

        time.sleep(1)

    return False


# 当脚本直接运行时启动服务器
if __name__ == "__main__":
    start_server()
