"""
Web服务器模块

提供Web服务器功能，网页信息服务 + mqtt代替
"""

import asyncio
import os
import threading
import webbrowser
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from starlette.responses import JSONResponse, Response

from unilabos.app.runtime_capabilities import get_runtime_capabilities
from unilabos.app.web.openapi_docs import install_openapi_documentation
from unilabos.config.config import BasicConfig
from unilabos.utils.fastapi.log_adapter import setup_fastapi_logging
from unilabos.utils.log import error, info
from unilabos.utils.tracing import install_http_tracing, trace_ui_base_url

# 创建FastAPI应用
app = FastAPI(
    title="UniLab API",
    description="UniLab API Service",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
)
install_openapi_documentation(app)
install_http_tracing(app)

# 创建页面路由
pages = None
workflow_routes_mounted = False
resource_contract_routes_mounted = False
workspace_material_asset_routes_mounted = False
workspace_authoring_routes_mounted = False
local_edge_control_routes_mounted = False
workflow_runtime_required = False
workflow_runtime_deferred = False
workflow_runtime_catalog_required = False
workflow_runtime_phase = "pending"
workflow_runtime_error: str | None = None
workflow_runtime_loaded = 0
workflow_runtime_total = 0
_workflow_runtime_values: dict[str, Any] = {}
_workflow_runtime_lock = threading.RLock()
_workflow_runtime_waiters: set[asyncio.Future[bool]] = set()
_LEGACY_API_ROUTES_MOUNTED_STATE = "_unilabos_legacy_api_routes_mounted"


def setup_api_routes(application: FastAPI | None = None) -> None:
    """兼容旧启动适配器的 API 路由安装入口。

    参数：``application`` 是要挂载路由的 FastAPI 应用，省略时使用进程应用。
    返回：无；实际路由表仍由 ``unilabos.app.web.api`` 维护。异常：下层路由导入
    或挂载失败原样传播。使用延迟导入避免服务模块初始化时引入设备/调度依赖。
    """

    target = application or app
    # 旧启动适配器仍可能显式调用这个模块级入口，而正式产品装配也会在
    # ``setup_server`` 中安装同一套路由。把挂载世代记在目标 FastAPI 实例上，
    # 让两条入口共享幂等闸门，避免重复 include_router 以及重复创建 startup
    # 广播任务；失败时不写标记，调用方仍可安全重试。
    if getattr(target.state, _LEGACY_API_ROUTES_MOUNTED_STATE, False):
        return
    from unilabos.app.web.api import setup_api_routes as _install_api_routes

    _install_api_routes(target)
    setattr(target.state, _LEGACY_API_ROUTES_MOUNTED_STATE, True)


class WorkflowRuntimeStarting(RuntimeError):
    """工作流运行时尚未完成原子发布。"""


class _DeferredWorkflowRuntimePort:
    """把预挂载 HTTP 路由的属性访问延迟到运行时发布之后。"""

    def __init__(self, value_name: str) -> None:
        """绑定待发布值名称；参数是内部端口名，返回无。"""

        self._value_name = value_name

    def __getattr__(self, attribute: str) -> Any:
        """转发到已发布端口；启动中访问抛稳定 503 异常。"""

        with _workflow_runtime_lock:
            value = _workflow_runtime_values.get(self._value_name)
            phase = workflow_runtime_phase
        if value is None:
            raise WorkflowRuntimeStarting(
                f"workflow runtime {self._value_name} is {phase}"
            )
        return getattr(value, attribute)


async def _wait_for_workflow_runtime_ready() -> bool:
    """等待服务发布；初始化失败或禁用时返回 False，不占用线程池。"""
    loop = asyncio.get_running_loop()
    with _workflow_runtime_lock:
        if workflow_runtime_phase == "ready":
            return True
        if workflow_runtime_phase in {"failed", "disabled"}:
            return False
        # 与发布共用一把锁，避免检查状态后、登记之前错过发布通知。
        waiter = loop.create_future()
        _workflow_runtime_waiters.add(waiter)
    try:
        return await waiter
    finally:
        with _workflow_runtime_lock:
            _workflow_runtime_waiters.discard(waiter)


def _notify_workflow_runtime_waiters(ready: bool) -> None:
    """持有运行时锁时调用，把结果送回各等待者所属的事件循环。"""
    def resolve(waiter: asyncio.Future[bool]) -> None:
        # 发布通知入队后，应用仍可能先关闭并取消等待者。
        if not waiter.done():
            waiter.set_result(ready)

    for waiter in _workflow_runtime_waiters:
        waiter.get_loop().call_soon_threadsafe(resolve, waiter)


def _set_workflow_runtime_phase(
    phase: str,
    *,
    runtime_error: str | None = None,
) -> None:
    """原子推进启动阶段；参数是阶段与净化错误文本，返回无。"""

    global workflow_runtime_phase, workflow_runtime_error
    with _workflow_runtime_lock:
        workflow_runtime_phase = phase
        workflow_runtime_error = runtime_error
        if phase in {"failed", "disabled"}:
            _notify_workflow_runtime_waiters(False)


def _report_workflow_activation_progress(loaded: int, total: int) -> None:
    """原子发布工作流启动进度；参数是已完成编译数和总数，返回无。"""

    if total < 0 or loaded < 0 or loaded > total:
        raise ValueError("工作流启动进度无效")
    global workflow_runtime_loaded, workflow_runtime_total
    with _workflow_runtime_lock:
        workflow_runtime_loaded = loaded
        workflow_runtime_total = total


def _reset_workflow_activation_progress() -> None:
    """清空上一次启动留下的工作流计数。"""

    _report_workflow_activation_progress(0, 0)


def _publish_workflow_runtime(
    *,
    service: Any,
    template_projection: Any | None,
) -> None:
    """一次发布工作流服务、模板投影和可信转换端口。"""

    global workflow_runtime_phase, workflow_runtime_error
    authoring_transform = getattr(service, "compiler", None)
    if workflow_runtime_catalog_required and (
        template_projection is None or authoring_transform is None
    ):
        raise RuntimeError("本地工作流模板目录未完整装配")
    if (
        BasicConfig.process_role == "workspace_backend"
        and BasicConfig.control_plane == "backend"
    ):
        from unilabos.config.config import EdgeControlConfig
        from unilabos.workflow.station_event_http import (
            start_station_event_projection,
        )

        start_station_event_projection(
            service.runtime_store,
            backend_address=str(EdgeControlConfig.backend_addr or ""),
            api_key=str(
                EdgeControlConfig.backend_api_key
                or EdgeControlConfig.api_key
                or ""
            ),
            station_key=str(
                EdgeControlConfig.edge_key or BasicConfig.machine_name or ""
            ),
            timeout=float(EdgeControlConfig.request_timeout),
        )
    with _workflow_runtime_lock:
        _workflow_runtime_values.update(
            {
                "service": service,
                "template_projection": template_projection,
                "authoring_transform": authoring_transform,
            }
        )
        workflow_runtime_error = None
        workflow_runtime_phase = "ready"
        _notify_workflow_runtime_waiters(True)


@app.exception_handler(WorkflowRuntimeStarting)
async def workflow_runtime_starting_handler(
    _request: Request,
    _error: WorkflowRuntimeStarting,
) -> JSONResponse:
    """启动中的工作流接口返回可重试的 HTTP 503，不伪装成业务失败。"""

    with _workflow_runtime_lock:
        phase = workflow_runtime_phase
    failed = phase == "failed"
    return JSONResponse(
        status_code=503,
        content={
            "status": "failed" if failed else "starting",
            "phase": phase,
            "error": {
                "code": (
                    "workflow_runtime_start_failed"
                    if failed
                    else "workflow_runtime_not_ready"
                ),
                "message": (
                    "工作流运行时启动失败，请查看 backend.log 中的完整上下文"
                    if failed
                    else f"工作流运行时正在启动（当前阶段：{phase}），请稍候重试"
                ),
            },
        },
        headers={"Retry-After": "1"},
    )


@app.get("/api/v1/readiness", tags=["api"])
def api_readiness() -> Response:
    """返回完整 Backend 合同就绪性；与只证明进程存活的 health 分离。"""

    with _workflow_runtime_lock:
        required = workflow_runtime_required
        phase = workflow_runtime_phase
        runtime_error = workflow_runtime_error
        loaded = workflow_runtime_loaded
        total = workflow_runtime_total
    ready = not required or phase == "ready"
    payload: dict[str, Any] = {
        "status": "ready" if ready else "starting",
        "capabilities": get_runtime_capabilities().as_dict(),
        "phase": "ready" if not required else phase,
        "workflowRuntime": "disabled" if not required else phase,
        "workflowProgress": {"loaded": loaded, "total": total},
    }
    if os.environ.get("UNILABOS_DESKTOP_WEB_HOST"):
        payload["generation"] = os.environ.get("UNILABOS_WORKBENCH_GENERATION")
    signoz_ui_url = trace_ui_base_url()
    if signoz_ui_url:
        payload["observability"] = {"traceUiUrl": signoz_ui_url}
    if runtime_error is not None:
        payload["status"] = "failed"
        detail = str(runtime_error).strip()
        payload["error"] = {
            "code": "workflow_runtime_start_failed",
            "message": (
                f"工作流运行时初始化失败：{detail}；请查看 backend.log 中的完整上下文"
                if detail
                else "工作流运行时初始化失败；请查看 backend.log 中的完整上下文"
            ),
        }
    return JSONResponse(status_code=200 if ready else 503, content=payload)

# noinspection PyTypeChecker
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
    allow_headers=[
        "Authorization",
        "Content-Type",
        "Accept",
        "Idempotency-Key",
        "X-Workflow-Filename",
        "Last-Event-ID",
        "trace_id",
        "traceparent",
        "tracestate",
    ],
    expose_headers=["trace_id", "span_id"],
)
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=5)


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


def _compose_configured_workflow_runtime() -> tuple[Any, Any | None]:
    """按当前产品配置组合唯一工作流服务及可选模板投影。"""

    from unilabos.app.runtime_storage import get_runtime_storage_directory
    from unilabos.app.scheduler.integration import (
        get_edge_scheduler,
        get_inventory_service,
    )
    from unilabos.workflow.composition import (
        compose_local_workflow_template_runtime,
        compose_workflow_runtime,
    )

    workflow_runtime_directory = (
        get_runtime_storage_directory() or BasicConfig.working_dir
    )
    template_projection = None
    inventory_service = get_inventory_service()
    edge_scheduler = get_edge_scheduler()
    source_plan_arguments: dict[str, Any] = {}
    if BasicConfig.workflow_source_discovery_plan is not None:
        source_plan_arguments["editable_source_discovery_plan"] = (
            BasicConfig.workflow_source_discovery_plan
        )
    source_plan_arguments["start_source_monitor"] = (
        BasicConfig.workflow_source_discovery_plan is None
    )
    source_plan_arguments["workflow_activation_progress"] = (
        _report_workflow_activation_progress
    )
    if inventory_service is not None and edge_scheduler is not None:
        from unilabos.registry.registry import lab_registry

        # 当前 Runtime 能力允许父工作流直接组合当前包中尚未发布的子工作流；这只是
        # 本地编译可见性，不会改变发布目录或 HTTP 可见范围。该参数只传给带模板
        # 投影的本地组合入口；无库存的遗留入口仍使用原有签名。
        source_plan_arguments["allow_unpublished_composite_sources"] = (
            get_runtime_capabilities().can_run_unpublished
        )
        workflow_service, template_projection = (
            compose_local_workflow_template_runtime(
                workflow_runtime_directory,
                inventory_store=inventory_service.store,
                registry=lab_registry,
                scheduler=edge_scheduler,
                editable_package_roots=(
                    BasicConfig.workflow_editable_package_roots
                ),
                **source_plan_arguments,
            )
        )
    else:
        workflow_service = compose_workflow_runtime(
            workflow_runtime_directory,
            editable_package_roots=(BasicConfig.workflow_editable_package_roots),
            **source_plan_arguments,
        )
    return workflow_service, template_projection


def _install_workflow_runtime_api(
    *,
    service: Any,
    template_projection: Any | None,
    authoring_transform: Any | None,
    wait_until_ready: Callable[[], Awaitable[bool]] | None = None,
) -> None:
    """一次挂载 Workflow HTTP 合同；参数是三个同代运行时端口。"""

    global workflow_routes_mounted
    from unilabos.app.workflow_api import install_workflow_api

    install_workflow_api(
        app,
        service,
        template_snapshot_provider=template_projection,
        authoring_transform=authoring_transform,
        wait_until_ready=wait_until_ready,
    )
    workflow_routes_mounted = True


def _mount_deferred_workflow_runtime_api() -> None:
    """在 Uvicorn 启动前挂载延迟端口，保证路由表之后不再动态改变。"""

    global workflow_runtime_catalog_required
    from unilabos.app.scheduler.integration import (
        get_edge_scheduler,
        get_inventory_service,
    )

    full_local_runtime = (
        get_inventory_service() is not None and get_edge_scheduler() is not None
    )
    workflow_runtime_catalog_required = full_local_runtime
    _install_workflow_runtime_api(
        service=_DeferredWorkflowRuntimePort("service"),
        wait_until_ready=_wait_for_workflow_runtime_ready,
        template_projection=(
            _DeferredWorkflowRuntimePort("template_projection")
            if full_local_runtime
            else None
        ),
        authoring_transform=(
            _DeferredWorkflowRuntimePort("authoring_transform")
            if full_local_runtime
            else None
        ),
    )


def initialize_deferred_workflow_runtime() -> None:
    """完成重型工作流激活并原子发布给已经监听的 HTTP 路由。"""

    if not workflow_runtime_required or not workflow_runtime_deferred:
        return
    _set_workflow_runtime_phase("activating_workflows")
    try:
        workflow_service, template_projection = (
            _compose_configured_workflow_runtime()
        )
        _publish_workflow_runtime(
            service=workflow_service,
            template_projection=template_projection,
        )
    except Exception as runtime_exception:
        _set_workflow_runtime_phase(
            "failed",
            runtime_error=str(runtime_exception),
        )
        error(f"[Web] 初始化 Backend Workflow 合同失败: {runtime_exception}")
        raise


def setup_server(*, defer_workflow_initialization: bool = False) -> FastAPI:
    """装配当前产品配置允许的 Web 路由和本地工作流运行时。

    参数：``defer_workflow_initialization`` 仅供真实服务器先开启存活接口再执行
    重型工作流激活。返回：进程唯一 FastAPI 应用；重复调用复用已挂载路由。工作流
    源码（Workflow Source）授权形状或组合失败时关闭该合同路由，但不阻止无关
    Edge 路由继续装配，错误写入产品日志。
    异常：基础 FastAPI 路由装配错误原样传播；工作流本地组合错误在本函数内记录
    并保持工作流接口关闭，不回退到第二套运行时。
    """
    global pages, resource_contract_routes_mounted, workflow_routes_mounted
    global workspace_material_asset_routes_mounted
    global local_edge_control_routes_mounted, workspace_authoring_routes_mounted
    global workflow_runtime_deferred, workflow_runtime_required
    global workflow_runtime_catalog_required
    from unilabos.app.control_plane import (
        should_mount_embedded_scheduler_routes,
        should_mount_workspace_authoring_routes,
    )

    if BasicConfig.control_plane != "local":
        raise RuntimeError("当前 OS 仅支持 local 控制面，不再支持 backend 模式")

    embedded_scheduler_enabled = should_mount_embedded_scheduler_routes()
    workspace_authoring_enabled = should_mount_workspace_authoring_routes()

    # 创建页面路由
    if pages is None:
        pages = app.router

    # 正式 Backend 进程不导入依赖 ROS 消息和设备注册表的遗留本机 API；它只
    # 暴露轻量存活接口以及下方按控制面条件装配的工作区接口。
    if BasicConfig.control_plane == "backend":
        if not any(
            getattr(route, "path", "") == "/api/v1/health"
            for route in app.routes
        ):
            app.add_api_route(
                "/api/v1/health",
                _backend_process_health,
                methods=["GET"],
                tags=["api"],
            )
    else:
        setup_api_routes(app)

    if (
        workspace_authoring_enabled
        and not workspace_authoring_routes_mounted
        and BasicConfig.workspace_package_mount_projection is not None
    ):
        from unilabos.app.workspace_authoring_api import (
            install_workspace_authoring_api,
        )

        install_workspace_authoring_api(
            app,
            BasicConfig.workspace_package_mount_projection,
        )
        workspace_authoring_routes_mounted = True

    # Backend 模式的 Workspace 进程只承担 Authoring 与包资产读取；模型路由不依赖
    # Inventory，因此可独立挂载而不会产生第二套物料写权威。
    if (
        workspace_authoring_enabled
        and not embedded_scheduler_enabled
        and not workspace_material_asset_routes_mounted
        and BasicConfig.workspace_package_mount_projection is not None
    ):
        from unilabos.app.scheduler.inventory.backend_api import (
            create_material_asset_router,
        )

        app.include_router(
            create_material_asset_router(
                material_shapes=BasicConfig.workspace_material_shapes,
                material_model_catalog=(
                    BasicConfig.workspace_material_model_catalog
                ),
            ),
            prefix="/api/v1",
            tags=["workspace-material-assets"],
        )
        workspace_material_asset_routes_mounted = True

    # 共享 Workflow Interface 必须先于 Edge-only scheduler adapter 挂载，
    # /workflows 表示定义，/workflow-tasks 表示运行。
    if (
        embedded_scheduler_enabled
        and not workflow_routes_mounted
        and BasicConfig.working_dir
    ):
        workflow_runtime_required = True
        workflow_runtime_deferred = defer_workflow_initialization
        _reset_workflow_activation_progress()
        _set_workflow_runtime_phase("mounting_routes")
        if defer_workflow_initialization:
            _mount_deferred_workflow_runtime_api()
        else:
            try:
                workflow_service, template_projection = (
                    _compose_configured_workflow_runtime()
                )
                workflow_runtime_catalog_required = template_projection is not None
                _install_workflow_runtime_api(
                    service=workflow_service,
                    template_projection=template_projection,
                    authoring_transform=workflow_service.compiler,
                )
                _publish_workflow_runtime(
                    service=workflow_service,
                    template_projection=template_projection,
                )
            except Exception as runtime_exception:  # noqa: BLE001
                _set_workflow_runtime_phase(
                    "failed",
                    runtime_error=str(runtime_exception),
                )
                error(
                    "[Web] 挂载 Backend Workflow 合同失败: "
                    f"{runtime_exception}"
                )
    elif not embedded_scheduler_enabled or not BasicConfig.working_dir:
        workflow_runtime_required = False
        workflow_runtime_deferred = False
        workflow_runtime_catalog_required = False
        _reset_workflow_activation_progress()
        _set_workflow_runtime_phase("disabled")

    # 事项查询仅依赖中心运行时，不依赖可选的本地库存模块。
    from unilabos.app.center_client.attention_api import create_router as create_center_attention_router
    from unilabos.app.center_client.connection_api import create_router as create_center_connection_router
    from unilabos.workflow.composition import get_center_connection, get_center_runtime

    if not getattr(app.state, "center_attention_routes_mounted", False):
        app.include_router(create_center_attention_router(get_center_runtime))
        app.include_router(create_center_connection_router(get_center_connection))
        app.state.center_attention_routes_mounted = True

    # 正式 Backend 控制面只保留基础设备诊断路由，不导入本地 Scheduler 模块。
    if embedded_scheduler_enabled:
        try:
            from unilabos.app.scheduler.api import create_scheduler_router
            from unilabos.app.scheduler.integration import (
                get_edge_backend,
                get_edge_scheduler,
                get_inventory_service,
                get_material_model_catalog,
                get_material_shapes,
            )

            app.include_router(
                create_scheduler_router(
                    get_edge_scheduler,
                    get_edge_backend,
                    include_execution_shaped_workflow_routes=False,
                )
            )
            inventory_service = get_inventory_service()
            if inventory_service is not None:
                from unilabos.app.scheduler.inventory.api import (
                    create_legacy_material_router,
                )
                from unilabos.app.scheduler.inventory.api import (
                    create_router as create_inventory_router,
                )
                from unilabos.app.scheduler.inventory.backend_api import (
                    install_backend_resource_api,
                )
                from unilabos.app.scheduler.inventory.backend_contract import (
                    BackendResourceService,
                )
                from unilabos.app.scheduler.inventory.compound_source import (
                    configured_compound_source,
                )
                from unilabos.app.scheduler.inventory.layout import create_lab_router
                from unilabos.app.scheduler.monitor import monitor_bus

                if not resource_contract_routes_mounted:
                    install_backend_resource_api(
                        app,
                        BackendResourceService(
                            inventory_service.store,
                            edge_id=inventory_service.edge_id,
                            lab_id=inventory_service.lab_id,
                            monitor=monitor_bus,
                        ),
                        material_shapes=get_material_shapes(),
                        material_model_catalog=get_material_model_catalog(),
                        compound_source=configured_compound_source(),
                    )
                    resource_contract_routes_mounted = True
                from unilabos.app.center_client.inventory_api import create_router as create_center_handoff_router
                from unilabos.workflow.composition import get_center_runtime
                app.include_router(create_center_handoff_router(get_center_runtime))
                from unilabos.app.center_client.registration_api import (
                    create_router as create_container_registration_router,
                )
                app.include_router(create_container_registration_router(get_center_runtime))
                from unilabos.app.center_client.material_attention_api import (
                    create_router as create_material_attention_router,
                )
                app.include_router(create_material_attention_router(get_center_runtime))
                from unilabos.app.center_client.material_settlement_api import (
                    create_router as create_material_settlement_router,
                )
                app.include_router(create_material_settlement_router(get_center_runtime))
                app.include_router(create_inventory_router(inventory_service))
                app.include_router(create_legacy_material_router(inventory_service))
                app.include_router(create_lab_router(inventory_service))

            edge_backend = get_edge_backend()
            if not local_edge_control_routes_mounted:
                from unilabos.app.edge_control.local_authority import (
                    LocalEdgeControlAuthority,
                    create_local_edge_control_router,
                )

                if isinstance(edge_backend, LocalEdgeControlAuthority):
                    app.include_router(
                        create_local_edge_control_router(edge_backend)
                    )
                    local_edge_control_routes_mounted = True
        except Exception as e:  # noqa: BLE001 - 调度器路由失败不影响设备诊断
            error(f"[Web] 挂载本地调试 Scheduler 路由失败: {str(e)}")

    # React 实验运营控制台只属于拥有本地 Scheduler/Inventory 的工作区进程。
    # 控制台页面和静态资源先于遗留根页面注册，API 与诊断页面保留各自的路由。
    if embedded_scheduler_enabled:
        from unilabos.app.web.console import (
            install_console_security,
            install_console_ui,
        )

        install_console_security(app)
        install_console_ui(app)

    # 设备本机页面只属于本地控制面；正式 Backend 不为展示层导入设备注册表。
    if BasicConfig.control_plane != "backend":
        try:
            from unilabos.app.web.pages import setup_web_pages

            setup_web_pages(pages)
            # info("[Web] 已加载Web UI模块")
        except ImportError as e:
            info(f"[Web] 未找到Web页面模块: {str(e)}")
        except Exception as e:
            error(f"[Web] 加载Web页面模块时出错: {str(e)}")

    # 路由会按启动配置动态挂载；清掉旧缓存，确保 Swagger 展示本次实际路由。
    app.openapi_schema = None
    return app


def _backend_process_health() -> dict[str, str]:
    """返回正式 Backend 进程存活状态，不触碰本地工站调度或设备注册表。"""

    return {"status": "ok", "scheduler": "disabled"}


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
    import time

    from uvicorn import Config, Server

    # 先固定完整路由表，但把工作流冷启动留到监听端口之后执行；这样 health
    # 证明进程存活，readiness 则持续公开真实激活阶段。
    setup_server(defer_workflow_initialization=True)

    # 配置日志
    log_config = setup_fastapi_logging()

    host = os.environ.get("UNILABOS_DESKTOP_WEB_HOST", host)

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
    config = Config(app=app, host=host, port=port, log_config=log_config,
                    proxy_headers=not bool(os.environ.get("UNILABOS_DESKTOP_WEB_HOST")))
    server = Server(config)

    # 启动服务器线程
    server_thread = threading.Thread(target=server.run, daemon=True, name="uvicorn_server")
    server_thread.start()

    startup_deadline = time.monotonic() + 5.0
    while (
        server_thread.is_alive()
        and not server.started
        and time.monotonic() < startup_deadline
    ):
        time.sleep(0.01)
    if not server.started:
        server.should_exit = True
        server_thread.join(timeout=5)
        raise RuntimeError("FastAPI 监听端口启动失败")

    try:
        initialize_deferred_workflow_runtime()
    except Exception:
        # readiness 已原子发布 failed；继续保留 health、诊断、设备等无关接口，
        # 由需要完整工作流合同的 Workspace Host 决定是否终止托管进程。
        pass

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
