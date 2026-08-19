"""
Web服务器模块

提供Web服务器功能，网页信息服务 + mqtt代替
"""

import webbrowser

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import Response

from unilabos.utils.fastapi.log_adapter import setup_fastapi_logging
from unilabos.utils.log import info, error
from unilabos.utils.tracing import install_http_tracing
from unilabos.app.web.api import setup_api_routes
from unilabos.app.web.pages import setup_web_pages
from unilabos.config.config import BasicConfig

# 创建FastAPI应用
app = FastAPI(
    title="UniLab API",
    description="UniLab API Service",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
)
install_http_tracing(app)

# 创建页面路由
pages = None
workflow_routes_mounted = False
edge_routes_mounted = False
resource_contract_routes_mounted = False
workflow_history_projection = None

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


def setup_server() -> FastAPI:
    """
    设置服务器

    Returns:
        FastAPI: 配置好的FastAPI应用实例
    """
    global pages, edge_routes_mounted, resource_contract_routes_mounted
    global workflow_history_projection, workflow_routes_mounted

    # 创建页面路由
    if pages is None:
        pages = app.router

    # 设置API路由
    setup_api_routes(app)

    # Workflow 定义/运行使用同一 Workflow Authority；Scheduler 路由只提供
    # 执行与可观测面，避免 /workflows 的定义和运行语义互相覆盖。
    if not workflow_routes_mounted and BasicConfig.working_dir:
        try:
            from unilabos.app.workflow_api import install_workflow_api
            from unilabos.storage.paths import RuntimeStoragePaths
            from unilabos.storage.profiles import SchedulerAuthorityProfile
            from unilabos.workflow.composition import compose_workflow_runtime

            storage_paths = BasicConfig.runtime_storage_paths
            if storage_paths is None:
                storage_paths = RuntimeStoragePaths.resolve(
                    {"working_dir": BasicConfig.working_dir}
                )
                BasicConfig.runtime_storage_paths = storage_paths
            workflow_service = compose_workflow_runtime(
                storage_paths,
                authority_profile=SchedulerAuthorityProfile.parse(
                    BasicConfig.scheduler_authority_profile
                ),
            )
            from unilabos.app.scheduler.integration import bind_workflow_executor

            bind_workflow_executor(workflow_service)
            install_workflow_api(app, workflow_service)

            from unilabos.app.scheduler.history import WorkflowHistoryStore

            workflow_history_projection = WorkflowHistoryStore(
                str(storage_paths.workflow_db), read_only=True
            )
            workflow_routes_mounted = True
        except Exception as exc:  # noqa: BLE001 - 保留基础管理 API
            error(f"[Web] 挂载 Workflow Provider 失败: {exc}")

    # Scheduler / Inventory / Backend-shaped Resource / Lab 共用主进程组合根。
    if not edge_routes_mounted:
        try:
            from unilabos.app.scheduler.api import create_scheduler_router
            from unilabos.app.scheduler.integration import (
                get_edge_backend,
                get_edge_scheduler,
                get_inventory_service,
            )

            app.include_router(
                create_scheduler_router(
                    get_edge_scheduler,
                    get_edge_backend,
                    get_history=lambda: workflow_history_projection,
                    include_execution_shaped_workflow_routes=False,
                )
            )
            inventory_service = get_inventory_service()
            if inventory_service is not None:
                from unilabos.app.scheduler.inventory.backend_api import (
                    install_backend_resource_api,
                )
                from unilabos.app.scheduler.inventory.backend_contract import (
                    BackendResourceService,
                )
                from unilabos.app.scheduler.inventory.api import (
                    create_legacy_material_router,
                    create_router as create_inventory_router,
                )
                from unilabos.app.scheduler.inventory.layout import create_lab_router

                if not resource_contract_routes_mounted:
                    install_backend_resource_api(
                        app, BackendResourceService(inventory_service.store)
                    )
                    resource_contract_routes_mounted = True
                app.include_router(create_inventory_router(inventory_service))
                app.include_router(create_legacy_material_router(inventory_service))
                app.include_router(create_lab_router(inventory_service))
            edge_routes_mounted = True
        except Exception as exc:  # noqa: BLE001 - 保留基础管理 API
            error(f"[Web] 挂载 Edge Providers 失败: {exc}")

    # 设置页面路由
    try:
        setup_web_pages(pages)
        # info("[Web] 已加载Web UI模块")
    except ImportError as e:
        info(f"[Web] 未找到Web页面模块: {str(e)}")
    except Exception as e:
        error(f"[Web] 加载Web页面模块时出错: {str(e)}")

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
        url = f"http://{host if host != '0.0.0.0' else 'localhost'}:{port}/status"
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
