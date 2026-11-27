"""真实产品入口传递工作流源码（Workflow Source）授权目录的合同测试。"""

from __future__ import annotations

import importlib
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from unilabos.app import main as app_main
from unilabos.app import runtime_storage
from unilabos.config.config import BasicConfig
from unilabos.workflow import composition
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.store import WorkflowStore

WORKFLOW_UUID = "11111111-1111-4111-8111-111111111111"


@pytest.fixture(autouse=True)
def clean_product_runtime(monkeypatch: pytest.MonkeyPatch) -> Any:
    """隔离配置单例、FastAPI 组合根和工作流运行时。

    参数：``monkeypatch`` 在用例结束时恢复产品配置。返回：pytest 生命周期值；
    前后均关闭进程级工作流权威。
    异常：测试装配或清理失败时传播原异常，禁止污染后续用例。
    """

    composition.reset_workflow_service_for_test()
    runtime_storage.close_runtime_storage_session()
    monkeypatch.setattr(BasicConfig, "working_dir", "")
    if hasattr(BasicConfig, "workflow_editable_package_roots"):
        monkeypatch.setattr(BasicConfig, "workflow_editable_package_roots", ())
    monkeypatch.setattr(BasicConfig, "workflow_source_discovery_plan", None)
    try:
        yield
    finally:
        composition.reset_workflow_service_for_test()
        runtime_storage.close_runtime_storage_session()


def _seed_workflow(working_dir: Path) -> None:
    """为真实 Web 组合根创建一个既有工作流定义。

    参数：``working_dir`` 决定产品使用的 ``workflow_history.db``。返回：无；
    创建后立即关闭播种服务。
    异常：数据库创建、工作流写入或关闭失败时传播原异常。
    """

    service = WorkflowService(WorkflowStore(working_dir / "workflow_history.db"))
    try:
        service.create_workflow(
            workflow_uuid=WORKFLOW_UUID,
            name="production source wiring",
            tags=[],
            description=None,
            meta_data={},
        )
    finally:
        service.close()


def _write_package(selected_root: Path) -> None:
    """写入真实源码发现器可消费的单工作流包。

    参数：``selected_root`` 是 BasicConfig 明确授权的选择目录。返回：无；
    manifest 和 Python 源码均在返回前完整落盘。
    """

    source_path = selected_root / "production_lab" / "workflows" / "demo.py"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("value = 'production'\n", encoding="utf-8")
    selected_root.joinpath("package.yaml").write_text(
        "package:\n"
        "  name: production_lab\n"
        "workflows:\n"
        f"  - workflow_uuid: {WORKFLOW_UUID}\n"
        "    source: production_lab/workflows/demo.py\n",
        encoding="utf-8",
    )


def _reload_server() -> Any:
    """重建 FastAPI 产品模块以清除上一用例的路由装配状态。

    参数：无。返回：重新载入的 ``unilabos.app.web.server`` 模块。
    """

    return importlib.reload(importlib.import_module("unilabos.app.web.server"))


def test_basic_config_declares_an_empty_tuple_allowlist_by_default() -> None:
    """产品缺省配置必须使用类型固定的空 tuple 表示无源码授权。

    参数：无。返回：无；禁止缺省扫描工作区或使用可变列表。
    """

    assert BasicConfig.workflow_editable_package_roots == ()
    assert isinstance(BasicConfig.workflow_editable_package_roots, tuple)


def test_cli_rejects_workflow_editable_package_root(tmp_path: Path) -> None:
    """公共启动命令必须拒绝工作区（Workspace）已经覆盖的源码授权参数。

    参数：``tmp_path`` 提供一个有效目录值，确保失败来自未知参数而不是缺少参数值。
    返回：无；证明工作流源码（Workflow Source）授权不再形成第二个 CLI 入口。
    """

    parser = app_main.parse_args()

    with pytest.raises(SystemExit):
        parser.parse_args(
            ["--workflow_editable_package_root", str(tmp_path / "editable")]
        )


def test_main_rejects_a_non_tuple_config_allowlist() -> None:
    """本地配置或环境注入的非 tuple 授权形状必须关闭失败。

    参数：无。返回：无；列表不能被主流程静默当作当前来源授权。
    """

    BasicConfig.workflow_editable_package_roots = ["unsafe-shape"]

    with pytest.raises(TypeError):
        app_main.configure_workflow_editable_package_roots({})


def test_real_web_server_fails_closed_without_template_catalog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实 FastAPI 产品缺少模板目录时不得发布不可编译的工作流权威。

    参数：``tmp_path`` 隔离产品数据库与包；``monkeypatch`` 固定配置并移除本地
    模板目录权威。返回：无；断言服务不发布且 readiness 明确失败。异常：产品
    静默发布无编译器的工作流服务时测试失败。
    """

    working_dir = tmp_path / "runtime"
    selected_root = tmp_path / "editable"
    _seed_workflow(working_dir)
    _write_package(selected_root)
    monkeypatch.setattr(BasicConfig, "working_dir", str(working_dir))
    monkeypatch.setattr(
        BasicConfig,
        "workflow_editable_package_roots",
        (str(selected_root),),
    )
    scheduler_integration = importlib.import_module(
        "unilabos.app.scheduler.integration"
    )

    def no_inventory_service() -> None:
        """表示本用例没有本地库存权威（Inventory Authority）。"""

        return

    def no_edge_scheduler() -> None:
        """表示本用例没有本地边缘调度器（Edge Scheduler）。"""

        return

    monkeypatch.setattr(
        scheduler_integration,
        "get_inventory_service",
        no_inventory_service,
    )
    monkeypatch.setattr(
        scheduler_integration,
        "get_edge_scheduler",
        no_edge_scheduler,
    )

    server = _reload_server()
    application = server.setup_server()

    assert composition.get_workflow_service() is None
    assert not server.workflow_routes_mounted
    readiness = TestClient(application).get("/api/v1/readiness")
    assert readiness.status_code == 503
    assert readiness.json()["status"] == "failed"
    assert readiness.json()["error"]["code"] == "workflow_runtime_start_failed"
    assert "工作流运行时初始化失败" in readiness.json()["error"]["message"]


def test_real_web_server_rejects_invalid_root_shape_without_mounting_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Web 产品入口收到无效授权形状时不得创建第二种隐式解释。

    参数：``tmp_path`` 提供工作目录；``monkeypatch`` 注入列表形状。
    返回：无；组合失败后不发布工作流权威。
    """

    monkeypatch.setattr(BasicConfig, "working_dir", str(tmp_path / "runtime"))
    monkeypatch.setattr(
        BasicConfig,
        "workflow_editable_package_roots",
        [str(tmp_path / "editable")],
    )

    _reload_server().setup_server()

    assert composition.get_workflow_service() is None


def test_local_product_composition_forwards_the_exact_configured_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """本地调度产品入口必须向模板组合根显式传递同一授权 tuple。

    参数：``tmp_path`` 提供产品工作目录和授权目录；``monkeypatch`` 用最小
    产品替身隔离模板投影细节。返回：无；遗漏参数或发生隐式根目录推导即失败。
    异常：组合依赖、授权根或接口安装身份不一致时测试失败。
    """

    working_dir = tmp_path / "runtime"
    selected_root = tmp_path / "editable"
    configured_roots = (str(selected_root),)
    runtime_paths = runtime_storage.prepare_runtime_storage_session(
        {}, working_dir=str(working_dir)
    )
    runtime_directory = str(Path(runtime_paths.workflow_history_db).parent)
    captured: dict[str, Any] = {}
    authoring_transform = object()
    workflow_service = SimpleNamespace(compiler=authoring_transform)
    template_projection = object()
    inventory_service = SimpleNamespace(store=object())
    edge_scheduler = object()
    monkeypatch.setattr(BasicConfig, "working_dir", str(working_dir))
    monkeypatch.setattr(
        BasicConfig,
        "workflow_editable_package_roots",
        configured_roots,
        raising=False,
    )
    scheduler_integration = importlib.import_module(
        "unilabos.app.scheduler.integration"
    )

    def current_inventory_service() -> object:
        """返回触发本地模板组合路径的库存权威（Inventory Authority）替身。"""

        return inventory_service

    def current_edge_scheduler() -> object:
        """返回触发本地模板组合路径的边缘调度器（Edge Scheduler）替身。"""

        return edge_scheduler

    def compose_local_runtime(
        observed_working_dir: str,
        *,
        inventory_store: object,
        registry: object,
        scheduler: object,
        editable_package_roots: tuple[str, ...],
        start_source_monitor: bool,
        workflow_activation_progress: object,
        allow_unpublished_composite_sources: bool,
    ) -> tuple[object, object]:
        """记录本地组合根收到的工作目录、依赖和源码授权。

        参数：所有参数对应产品入口的完整显式依赖。返回：工作流服务
        （WorkflowService）和模板投影替身；不产生持久状态。
        异常：无。
        """

        captured.update(
            working_dir=observed_working_dir,
            inventory_store=inventory_store,
            registry=registry,
            scheduler=scheduler,
            editable_package_roots=editable_package_roots,
            start_source_monitor=start_source_monitor,
            workflow_activation_progress=workflow_activation_progress,
            allow_unpublished_composite_sources=allow_unpublished_composite_sources,
        )
        return workflow_service, template_projection

    def unexpected_fallback(*args: object, **kwargs: object) -> object:
        """拒绝本地模板组合失败后静默进入普通组合路径。

        参数：``args`` 与 ``kwargs`` 捕获任何意外调用。返回：永不返回；立即
        失败以暴露产品装配分叉。
        """

        raise AssertionError("unexpected workflow runtime fallback")

    def install_api(
        target_app: object,
        installed_service: object,
        *,
        template_snapshot_provider: object,
        authoring_transform: object,
    ) -> None:
        """记录产品只挂载本地组合返回的同一工作流服务和纯转换引擎。

        参数：应用、服务、模板投影和可信创作引擎均来自产品入口。返回：无；
        仅执行身份断言，证明 HTTP 纯转换不会另建第二个目录代际。
        """

        assert target_app is server.app
        assert installed_service is workflow_service
        assert template_snapshot_provider is template_projection
        assert authoring_transform is workflow_service.compiler

    monkeypatch.setattr(
        scheduler_integration,
        "get_inventory_service",
        current_inventory_service,
    )
    monkeypatch.setattr(
        scheduler_integration,
        "get_edge_scheduler",
        current_edge_scheduler,
    )
    monkeypatch.setattr(
        composition,
        "compose_local_workflow_template_runtime",
        compose_local_runtime,
    )
    monkeypatch.setattr(
        composition,
        "compose_workflow_runtime",
        unexpected_fallback,
    )
    workflow_api = importlib.import_module("unilabos.app.workflow_api")
    monkeypatch.setattr(workflow_api, "install_workflow_api", install_api)
    server = _reload_server()

    server.setup_server()

    assert captured["working_dir"] == runtime_directory
    assert captured["inventory_store"] is inventory_service.store
    assert captured["scheduler"] is edge_scheduler
    assert captured["editable_package_roots"] == configured_roots
    assert captured["start_source_monitor"] is True
    assert callable(captured["workflow_activation_progress"])
    assert isinstance(captured["allow_unpublished_composite_sources"], bool)


def test_local_composition_failure_never_falls_back_to_uncompiled_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """本地模板/来源组合失败时不得静默发布无模板编译器的第二套服务。

    参数：``tmp_path`` 提供真实产品工作目录；``monkeypatch`` 注入本地组合安全
    失败并记录回退与 API 挂载。返回：无；失败必须使工作流合同关闭且不调用回退。
    异常：失败后发生回退、挂载接口或发布部分服务时测试失败。
    """

    monkeypatch.setattr(BasicConfig, "working_dir", str(tmp_path / "runtime"))
    monkeypatch.setattr(BasicConfig, "workflow_editable_package_roots", ())
    inventory_service = SimpleNamespace(store=object())
    edge_scheduler = object()
    fallback_called = False
    api_installed = False
    scheduler_integration = importlib.import_module(
        "unilabos.app.scheduler.integration"
    )

    def current_inventory_service() -> object:
        """返回触发本地模板组合分支的库存权威（Inventory Authority）替身。"""

        return inventory_service

    def current_edge_scheduler() -> object:
        """返回触发本地模板组合分支的调度器（Scheduler）替身。"""

        return edge_scheduler

    def fail_local_composition(
        *args: object, **kwargs: object
    ) -> tuple[object, object]:
        """注入本地模板投影或来源安全校验失败。

        参数：``args`` 与 ``kwargs`` 捕获产品组合依赖。返回：永不返回；抛出
        ``RuntimeError`` 模拟不得降级的启动失败。
        异常：固定抛出 ``RuntimeError``。
        """

        raise RuntimeError("注入的本地工作流安全组合失败")

    def record_fallback(*args: object, **kwargs: object) -> object:
        """记录任何不允许的普通工作流运行时回退。

        参数：``args`` 与 ``kwargs`` 捕获回退依赖。返回：服务替身，便于旧实现
        继续到 API 挂载后由断言同时识别两项错误行为。
        """

        nonlocal fallback_called
        fallback_called = True
        return object()

    def record_api_install(*args: object, **kwargs: object) -> None:
        """记录失败后不应发生的工作流 HTTP 合同挂载。

        参数：``args`` 与 ``kwargs`` 捕获应用、服务与模板投影。返回：无。
        """

        nonlocal api_installed
        api_installed = True

    monkeypatch.setattr(
        scheduler_integration,
        "get_inventory_service",
        current_inventory_service,
    )
    monkeypatch.setattr(
        scheduler_integration,
        "get_edge_scheduler",
        current_edge_scheduler,
    )
    monkeypatch.setattr(
        composition,
        "compose_local_workflow_template_runtime",
        fail_local_composition,
    )
    monkeypatch.setattr(
        composition,
        "compose_workflow_runtime",
        record_fallback,
    )
    workflow_api = importlib.import_module("unilabos.app.workflow_api")
    monkeypatch.setattr(workflow_api, "install_workflow_api", record_api_install)

    _reload_server().setup_server()

    assert not fallback_called
    assert not api_installed
    assert composition.get_workflow_service() is None


def test_real_server_keeps_liveness_after_deferred_workflow_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """工作流冷启动失败不得关闭 health 和其他已装配 HTTP 路由。

    参数：``monkeypatch`` 用内存 Uvicorn 替身隔离端口。返回：无；断言真实
    ``start_server`` 进入正常监控循环，health 存活且 readiness 明确失败。
    异常：初始化错误重新终止整个服务器时测试失败。
    """

    server = _reload_server()

    class FakeConfig:
        def __init__(self, **_kwargs: object) -> None:
            return

    class FakeServer:
        def __init__(self, _config: object) -> None:
            self.started = False
            self.should_exit = False

        def run(self) -> None:
            self.started = True
            deadline = time.monotonic() + 2.0
            while not self.should_exit and time.monotonic() < deadline:
                time.sleep(0.001)

    def setup_deferred_server(*, defer_workflow_initialization: bool = False) -> Any:
        assert defer_workflow_initialization
        server.setup_api_routes(server.app)
        server.workflow_runtime_required = True
        server.workflow_runtime_deferred = True
        return server.app

    def fail_workflow_runtime() -> None:
        server._set_workflow_runtime_phase(
            "failed",
            runtime_error="fixture catalog failure",
        )
        raise RuntimeError("fixture catalog failure")

    uvicorn = importlib.import_module("uvicorn")
    monkeypatch.setattr(server, "setup_server", setup_deferred_server)
    monkeypatch.setattr(
        server,
        "initialize_deferred_workflow_runtime",
        fail_workflow_runtime,
    )
    monkeypatch.setattr(uvicorn, "Config", FakeConfig)
    monkeypatch.setattr(uvicorn, "Server", FakeServer)
    monkeypatch.setattr(app_main, "_restart_requested", True)
    monkeypatch.setattr(app_main, "_restart_reason", "test cleanup")

    assert server.start_server(open_browser=False)

    client = TestClient(server.app)
    health = client.get("/api/v1/health")
    readiness = client.get("/api/v1/readiness")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert readiness.status_code == 503
    assert readiness.json()["status"] == "failed"
    assert readiness.json()["error"]["code"] == "workflow_runtime_start_failed"
    assert "工作流运行时初始化失败" in readiness.json()["error"]["message"]


def test_center_attention_route_mounts_without_inventory_and_only_once(tmp_path, monkeypatch):
    """中心事项查询只依赖正常运行时，库存模块缺省不应让中心收到 404。"""
    from tests.app.center_client.test_attention_api import snapshot_runtime
    runtime = snapshot_runtime(tmp_path, {'manual_confirmation_count': 0, 'exception_count': 0,
        'items': {'manual_confirmations': [], 'exceptions': []}})
    integration = importlib.import_module('unilabos.app.scheduler.integration')
    monkeypatch.setattr(integration, 'get_inventory_service', lambda: None)
    monkeypatch.setattr(integration, 'get_edge_scheduler', lambda: None)
    monkeypatch.setattr(composition, 'get_center_runtime', lambda: runtime)
    try:
        server = _reload_server()
        application = server.setup_server()
        # 核对事项路由的重复挂载，而不依赖 FastAPI 展开嵌套路由的内部布局。
        include_router = application.include_router
        repeated_attention_mounts = []
        def record_mount(router, *args, **kwargs):
            if any(getattr(route, 'path', None) == '/api/v1/center-attentions' for route in router.routes):
                repeated_attention_mounts.append(router)
            return include_router(router, *args, **kwargs)
        monkeypatch.setattr(application, 'include_router', record_mount)
        assert server.setup_server() is application
        assert repeated_attention_mounts == []
        response = TestClient(application).get('/api/v1/center-attentions')
        assert response.status_code == 200, response.text
        assert response.json()['data']['snapshot']['exception_count'] == 0
    finally:
        runtime.store.close()
