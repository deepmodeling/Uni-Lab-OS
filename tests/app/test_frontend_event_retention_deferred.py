"""真实延迟装配路径的事件清理就绪、失败与关闭合同。"""

import asyncio
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI

from unilabos.app.web import server
from unilabos.config.config import BasicConfig
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.store import WorkflowStore, utc_now

RETENTION_LOGGER = "unilabos.workflow.event_retention"


@pytest.fixture
def deferred_app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    """走产品占位端口装配入口，仅隔离无关的调度器和进程配置。"""
    from unilabos.app.scheduler import integration

    app = FastAPI()
    monkeypatch.setattr(server, "app", app)
    monkeypatch.setattr(server, "_workflow_runtime_values", {})
    monkeypatch.setattr(server, "workflow_runtime_phase", "mounting_routes")
    monkeypatch.setattr(server, "workflow_runtime_error", None)
    monkeypatch.setattr(server, "workflow_runtime_required", True)
    monkeypatch.setattr(server, "workflow_runtime_deferred", True)
    monkeypatch.setattr(server, "workflow_runtime_catalog_required", False)
    monkeypatch.setattr(server, "workflow_routes_mounted", False)
    monkeypatch.setattr(integration, "get_inventory_service", lambda: None)
    monkeypatch.setattr(integration, "get_edge_scheduler", lambda: None)
    monkeypatch.setattr(BasicConfig, "control_plane", "local")
    monkeypatch.setattr(BasicConfig, "frontend_event_max_count", 2)
    monkeypatch.setattr(BasicConfig, "frontend_event_cleanup_batch_size", 2)
    server._mount_deferred_workflow_runtime_api()
    return app


@pytest.fixture
def retained_service(tmp_path: Path) -> Iterator[WorkflowService]:
    store = WorkflowStore(tmp_path / "deferred.db")
    store.append_forwarded_events([
        {"event": "workflow.runtime.changed", "data": {}, "create_time": utc_now()}
    ] * 9)
    service = WorkflowService(store)
    try:
        yield service
    finally:
        service.close()


def retention_tasks() -> list[asyncio.Task]:
    return [task for task in asyncio.all_tasks() if task.get_name() == "frontend-event-retention"]


def assert_no_retention_errors(caplog: pytest.LogCaptureFixture) -> None:
    assert not [record for record in caplog.records
                if record.name == RETENTION_LOGGER and record.levelno >= logging.ERROR]


@pytest.mark.parametrize("publish_before_start", [False, True])
def test_deferred_cleanup_starts_on_publication_without_error_or_retry_delay(
    deferred_app: FastAPI, retained_service: WorkflowService,
    caplog: pytest.LogCaptureFixture, publish_before_start: bool,
) -> None:
    """HTTP 启动不等待服务；发布前安静等待，跨线程发布后立即清理。"""
    caplog.set_level(logging.ERROR, logger=RETENTION_LOGGER)

    async def publish() -> None:
        await asyncio.to_thread(
            server._publish_workflow_runtime,
            service=retained_service, template_projection=None,
        )

    async def check() -> None:
        if publish_before_start:
            await publish()
        async with deferred_app.router.lifespan_context(deferred_app):
            await asyncio.sleep(0)
            assert_no_retention_errors(caplog)
            if not publish_before_start:
                assert retained_service.runtime_store.count_rows("frontend_event") == 9
                assert retention_tasks()
                await publish()

            async def wait_for_cleanup() -> None:
                while retained_service.runtime_store.count_rows("frontend_event") > 2:
                    await asyncio.sleep(0.01)
            await asyncio.wait_for(wait_for_cleanup(), timeout=2)
            assert_no_retention_errors(caplog)
        assert not retention_tasks()

    # 覆盖误把就绪等待放在 lifespan yield 前面、导致 HTTP 启动死锁的情况。
    asyncio.run(asyncio.wait_for(check(), timeout=3), debug=True)


@pytest.mark.parametrize("fail_before_start", [False, True])
def test_initialization_failure_stops_cleanup_wait_without_duplicate_error(
    deferred_app: FastAPI, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture, fail_before_start: bool,
) -> None:
    """真实初始化失败由启动路径报告，清理等待者退出且不重复报错。"""
    caplog.set_level(logging.ERROR, logger=RETENTION_LOGGER)

    def fail_composition() -> None:
        raise RuntimeError("测试运行时初始化失败")

    monkeypatch.setattr(server, "_compose_configured_workflow_runtime", fail_composition)

    async def fail() -> None:
        with pytest.raises(RuntimeError, match="测试运行时初始化失败"):
            await asyncio.to_thread(server.initialize_deferred_workflow_runtime)

    async def check() -> None:
        if fail_before_start:
            await fail()
        async with deferred_app.router.lifespan_context(deferred_app):
            await asyncio.sleep(0)
            if not fail_before_start:
                await fail()
            async def wait_for_exit() -> None:
                while retention_tasks():
                    await asyncio.sleep(0.01)
            await asyncio.wait_for(wait_for_exit(), timeout=1)
            assert_no_retention_errors(caplog)
            assert server.api_readiness().status_code == 503
            assert server.workflow_runtime_phase == "failed"
            assert sum("初始化 Backend Workflow 合同失败" in record.getMessage()
                       for record in caplog.records) == 1

    asyncio.run(asyncio.wait_for(check(), timeout=3), debug=True)


@pytest.mark.parametrize("publish_during_shutdown", [False, True])
def test_shutdown_cancels_wait_and_publication_does_not_start_cleanup(
    deferred_app: FastAPI, retained_service: WorkflowService,
    caplog: pytest.LogCaptureFixture, publish_during_shutdown: bool,
) -> None:
    """服务尚未就绪也能及时关闭，之后发布不能唤醒遗留任务。"""
    caplog.set_level(logging.ERROR, logger=RETENTION_LOGGER)

    async def check() -> None:
        async with deferred_app.router.lifespan_context(deferred_app):
            await asyncio.sleep(0)
            assert retention_tasks()
            if publish_during_shutdown:
                # 通知已入队但尚未交付时关闭，不能完成已被取消的 Future。
                server._publish_workflow_runtime(
                    service=retained_service, template_projection=None,
                )
        assert not retention_tasks()

    asyncio.run(asyncio.wait_for(check(), timeout=3), debug=True)
    if not publish_during_shutdown:
        # 事件循环已关闭；任何遗留登记都会在跨线程通知时触发异常。
        server._publish_workflow_runtime(service=retained_service, template_projection=None)
    assert retained_service.runtime_store.count_rows("frontend_event") == 9
    assert_no_retention_errors(caplog)
    assert not [record for record in caplog.records
                if record.name == "asyncio" and record.levelno >= logging.ERROR]
