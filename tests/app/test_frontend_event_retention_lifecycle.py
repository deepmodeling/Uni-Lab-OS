"""HTTP 应用启动后无需订阅者也清理事件，关闭后不留下工作任务。"""

import asyncio
import logging
from contextlib import suppress
from pathlib import Path
from threading import Event

import pytest

from unilabos.app.workflow_api import create_workflow_app
from unilabos.config.config import BasicConfig
from unilabos.workflow.event_retention import EventRetentionPolicy, maintain_frontend_events
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.store import WorkflowStore, utc_now


def test_retention_runs_without_sse_and_stops_with_app(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(BasicConfig, "frontend_event_max_count", 2)
    monkeypatch.setattr(BasicConfig, "frontend_event_cleanup_batch_size", 2)
    store = WorkflowStore(tmp_path / "background.db")
    try:
        store.append_forwarded_events([
            {"event": "workflow.runtime.changed", "data": {}, "create_time": utc_now()}
        ] * 9)
        app = create_workflow_app(WorkflowService(store))

        async def check() -> None:
            async with app.router.lifespan_context(app):
                async def wait_for_cleanup() -> None:
                    while store.count_rows("frontend_event") > 2:
                        await asyncio.sleep(0.01)
                await asyncio.wait_for(wait_for_cleanup(), timeout=3)
                assert [e["id"] for e in store.list_events()] == [8, 9]
            assert not [t for t in asyncio.all_tasks() if t.get_name() == "frontend-event-retention"]
        asyncio.run(check())
    finally:
        store.close()


@pytest.mark.parametrize("fail_batch", [False, True])
def test_shutdown_waits_for_current_batch_even_when_it_fails(fail_batch: bool) -> None:
    started = Event()
    release = Event()
    finished = Event()

    class SlowStore:
        def prune_frontend_events(self, **_kwargs) -> int:
            started.set()
            assert release.wait(timeout=5)
            finished.set()
            if fail_batch:
                raise RuntimeError("测试清理失败")
            return 0

    async def check() -> None:
        context = maintain_frontend_events(SlowStore())
        await context.__aenter__()
        shutdown = None
        try:
            async def wait_for_batch() -> None:
                while not started.is_set():
                    await asyncio.sleep(0.01)
            await asyncio.wait_for(wait_for_batch(), timeout=3)
            shutdown = asyncio.create_task(context.__aexit__(None, None, None))
            # 让关闭流程取消工作任务，但数据库事务仍未完成。
            await asyncio.sleep(0.01)
            assert not shutdown.done()
            release.set()
            await asyncio.wait_for(asyncio.shield(shutdown), timeout=1)
            assert finished.is_set()
            assert not [t for t in asyncio.all_tasks() if t.get_name() == "frontend-event-retention"]
        finally:
            release.set()
            if shutdown is None:
                await context.__aexit__(None, None, None)
            elif not shutdown.done():
                shutdown.cancel()
                with suppress(asyncio.CancelledError):
                    await shutdown

    asyncio.run(check())


def test_real_cleanup_failure_logs_error_and_retries(caplog: pytest.LogCaptureFixture) -> None:
    """就绪等待不能掩盖数据库清理失败，失败后仍按配置间隔重试。"""
    retried = Event()
    attempts = 0

    class FailingStore:
        def prune_frontend_events(self, **_kwargs) -> int:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("测试数据库清理失败")
            retried.set()
            return 0

    async def check() -> None:
        async with maintain_frontend_events(FailingStore(), EventRetentionPolicy(interval_seconds=1)):
            async def wait_for_retry() -> None:
                while not retried.is_set():
                    await asyncio.sleep(0.01)
            await asyncio.wait_for(wait_for_retry(), timeout=3)
        assert not [t for t in asyncio.all_tasks() if t.get_name() == "frontend-event-retention"]

    asyncio.run(check())
    errors = [record for record in caplog.records
              if record.name == "unilabos.workflow.event_retention" and record.levelno >= logging.ERROR]
    assert len(errors) == 1
    assert errors[0].exc_info is not None
    assert str(errors[0].exc_info[1]) == "测试数据库清理失败"
