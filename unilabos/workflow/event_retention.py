"""随 HTTP 应用启停的前端失效通知保留策略；不依赖是否有浏览器连接。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from starlette.concurrency import run_in_threadpool

logger = logging.getLogger(__name__)


class EventRetentionStore(Protocol):
    def prune_frontend_events(
        self, *, before: str, max_count: int, batch_size: int,
    ) -> int: ...


@dataclass(frozen=True)
class EventRetentionPolicy:
    """时间与数量共同限制重连窗口，清理每次仅占用一个有界事务。"""

    max_age_seconds: int = 86_400
    max_count: int = 10_000
    interval_seconds: int = 60
    batch_size: int = 1_000

    def __post_init__(self) -> None:
        if any(type(value) is not int or value < 1 for value in (
            self.max_age_seconds, self.max_count, self.interval_seconds, self.batch_size,
        )):
            raise ValueError("前端事件保留配置必须为正整数")

    @classmethod
    def configured(cls) -> EventRetentionPolicy:
        from unilabos.config.config import BasicConfig

        return cls(
            max_age_seconds=BasicConfig.frontend_event_retention_seconds,
            max_count=BasicConfig.frontend_event_max_count,
            interval_seconds=BasicConfig.frontend_event_cleanup_interval_seconds,
            batch_size=BasicConfig.frontend_event_cleanup_batch_size,
        )


@asynccontextmanager
async def maintain_frontend_events(
    store: EventRetentionStore, policy: EventRetentionPolicy | None = None,
    *, wait_until_ready: Callable[[], Awaitable[bool]] | None = None,
) -> AsyncIterator[None]:
    """后台等待就绪再清理，等待返回 False 表示启动失败或功能已禁用。

    就绪等待不阻塞 HTTP 启动，且与清理任务一同取消；真正的清理失败仍记
    错误日志并在下轮重试。宿主负责报告运行时初始化失败。
    """
    policy = policy or EventRetentionPolicy.configured()

    async def maintain() -> None:
        if wait_until_ready is not None and not await wait_until_ready():
            return
        while True:
            deleted = 0
            try:
                before = (datetime.now(timezone.utc) - timedelta(
                    seconds=policy.max_age_seconds,
                )).isoformat().replace("+00:00", "Z")
                # 数据库锁与 I/O 不占用 SSE 所在事件循环。
                batch = asyncio.create_task(run_in_threadpool(
                    store.prune_frontend_events, before=before,
                    max_count=policy.max_count, batch_size=policy.batch_size,
                ))
                try:
                    deleted = await asyncio.shield(batch)
                except asyncio.CancelledError:
                    # 先等当前有界事务结束，再允许宿主关闭数据库连接。
                    # 即使事务失败，也不能吞掉关闭信号并进入下一轮清理。
                    with suppress(Exception):
                        await batch
                    raise
            except Exception:
                logger.exception("前端事件清理失败，将在下一轮重试")
            await asyncio.sleep(0 if deleted >= policy.batch_size else policy.interval_seconds)

    worker = asyncio.create_task(maintain(), name="frontend-event-retention")
    try:
        yield
    finally:
        worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker
