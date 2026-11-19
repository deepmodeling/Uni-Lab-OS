"""调度权威只在本进程计时的时长：墙钟跳变不影响。

覆盖 Edge 控制面等待空闲的期限和监控 SSE 的保活间隔。只改写墙钟（``time.time``），单调时钟照常走。
"""

from __future__ import annotations

import asyncio
import itertools
import threading
import time

from unilabos.server.api.runtime import scheduling_v1
from unilabos.server.edge_dispatch import EdgeControlService


def _jump_wall_clock(monkeypatch, offset_s: float) -> None:
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + offset_s)


def _jumping_wall_clock(monkeypatch, step_s: float) -> None:
    """每读一次墙钟，它都比上一次再前跳 ``step_s`` 秒。"""

    real_time = time.time
    reads = itertools.count(1)
    monkeypatch.setattr(time, "time", lambda: real_time() + step_s * next(reads))


def test_edge_wait_idle_deadline_ignores_forward_wall_clock_jumps(monkeypatch) -> None:
    service = EdgeControlService()
    with service._lock:
        service._inflight["job-1"] = {}

    def finish() -> None:
        time.sleep(0.2)
        with service._lock:
            service._inflight.clear()

    threading.Thread(target=finish, daemon=True).start()
    _jumping_wall_clock(monkeypatch, 13.0)

    assert service.wait_idle(timeout=5.0) is True


class _QuietTimeline:
    """没有任何事件的监控时间线。"""

    def __init__(self, _service) -> None:
        pass

    def backlog(self, _channels, _count):
        return []

    def latest_sequence(self) -> int:
        return 0

    def monitor_events(self, *, after, channels):
        return []


class _Request:
    async def is_disconnected(self) -> bool:
        return False


def test_monitor_stream_keepalive_ignores_forward_wall_clock_jump(monkeypatch) -> None:
    monkeypatch.setattr(scheduling_v1, "ExecutionTimeline", _QuietTimeline)
    router = scheduling_v1.create_scheduling_router(service=None, get_scheduler=lambda: None)
    endpoint = next(route.endpoint for route in router.routes if route.path.endswith("/monitor/events"))

    async def next_chunk_after_jump():
        response = await endpoint(_Request(), channels="", backlog=0, follow=True)
        stream = response.body_iterator

        async def next_chunk():
            return await anext(stream)

        try:
            assert (await next_chunk()).startswith("retry:")
            pending = asyncio.get_running_loop().create_task(next_chunk())
            # 先让事件流进入轮询循环、记下上次保活时间，再让墙钟前跳超过 15 s 的保活间隔；
            # 单调时钟此时才过去不到 2 s
            await asyncio.sleep(0.2)
            _jump_wall_clock(monkeypatch, 16.0)
            try:
                return await asyncio.wait_for(pending, timeout=1.5)
            except TimeoutError:
                return None
        finally:
            await stream.aclose()

    assert asyncio.run(next_chunk_after_jump()) is None
