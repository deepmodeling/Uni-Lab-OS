"""前端失效通知（frontend_event）的保留清理，对齐 v1.0 ADR 0006。

通知只表示 REST 状态失效，不是审计记录。清理按时间与数量共同限制重连窗口：
后台线程随本机 Workflow Authority 启停，没有订阅者也运行；每轮只占用一个有界
事务，积压时连续清理，追平后按间隔检查。清理失败记录错误并在下一轮重试。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Protocol

logger = logging.getLogger(__name__)


class EventRetentionStore(Protocol):
    def prune_frontend_events(self, *, before: str, max_count: int, batch_size: int) -> int: ...


@dataclass(frozen=True)
class EventRetentionPolicy:
    max_age_seconds: int = 86_400
    max_count: int = 10_000
    interval_seconds: int = 60
    batch_size: int = 1_000

    def __post_init__(self) -> None:
        values = (self.max_age_seconds, self.max_count, self.interval_seconds, self.batch_size)
        if any(type(value) is not int or value < 1 for value in values):
            raise ValueError("前端事件保留配置必须为正整数")

    @classmethod
    def configured(cls) -> "EventRetentionPolicy":
        from unilabos.config.config import BasicConfig

        return cls(
            max_age_seconds=int(BasicConfig.frontend_event_retention_seconds),
            max_count=int(BasicConfig.frontend_event_max_count),
            interval_seconds=int(BasicConfig.frontend_event_cleanup_interval_seconds),
            batch_size=int(BasicConfig.frontend_event_cleanup_batch_size),
        )


class FrontendEventRetention:
    """按策略清理 frontend_event 的后台线程。"""

    def __init__(self, store: EventRetentionStore, policy: Optional[EventRetentionPolicy] = None) -> None:
        self._store = store
        self._policy = policy or EventRetentionPolicy.configured()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def policy(self) -> EventRetentionPolicy:
        return self._policy

    def run_once(self, now: Optional[datetime] = None) -> int:
        """执行一个有界清理事务，返回删除条数。"""

        current = now or datetime.now(timezone.utc)
        before = (current - timedelta(seconds=self._policy.max_age_seconds)).isoformat(
            timespec="microseconds"
        ).replace("+00:00", "Z")
        return self._store.prune_frontend_events(
            before=before,
            max_count=self._policy.max_count,
            batch_size=self._policy.batch_size,
        )

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="frontend-event-retention", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """请求停止并等待当前事务结束，再由调用方关闭数据库连接。"""

        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout)

    def _loop(self) -> None:
        while not self._stop.is_set():
            deleted = 0
            try:
                deleted = self.run_once()
            except Exception:  # noqa: BLE001 - 清理失败不影响服务，下一轮重试
                logger.exception("[EventRetention] 前端事件清理失败，将在下一轮重试")
            # 积压时短暂让出锁后继续下一批；追平后按间隔检查。
            wait = 0.01 if deleted >= self._policy.batch_size else self._policy.interval_seconds
            if self._stop.wait(wait):
                break


__all__ = ["EventRetentionPolicy", "FrontendEventRetention"]
