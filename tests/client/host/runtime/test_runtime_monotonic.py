"""设备运行时只在本进程计时的时长：墙钟跳变不影响。

覆盖设备互斥锁的等待时长和链路诊断的往返时延。只改写墙钟（``time.time``），单调时钟照常走。
"""

from __future__ import annotations

import asyncio
import re
import time
from types import SimpleNamespace

from unilabos.client.host.legacy_adaptor.session import BackendLink
from unilabos.client.host.runtime.async_utils import DeviceAsyncMutex
from unilabos.client.host.runtime.host_adapter import HostAdapterBase


def _jump_wall_clock(monkeypatch, offset_s: float) -> None:
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + offset_s)


class _Node:
    """DeviceAsyncMutex 用到的节点调用：等待 Future、调度唤醒协程、记录 debug 日志。"""

    def __init__(self) -> None:
        self.debug_lines: list[str] = []

    def lab_logger(self):
        return SimpleNamespace(debug=self.debug_lines.append)

    def create_wait_future(self):
        return asyncio.get_running_loop().create_future()

    def create_task(self, coroutine):
        return asyncio.get_running_loop().create_task(coroutine)


def test_mutex_wait_time_ignores_backward_wall_clock_step(monkeypatch) -> None:
    async def scenario() -> list[str]:
        node = _Node()
        mutex = DeviceAsyncMutex("pump")
        await mutex.acquire(node, "first")
        second = asyncio.get_running_loop().create_task(mutex.acquire(node, "second"))
        await asyncio.sleep(0.05)
        _jump_wall_clock(monkeypatch, -30.0)
        mutex.release(node)
        await asyncio.wait_for(second, 2.0)
        return node.debug_lines

    lines = asyncio.run(scenario())

    waited = [int(match.group(1)) for line in lines if (match := re.search(r"waited=(-?\d+)ms", line))]
    assert len(waited) == 1
    assert 0 <= waited[0] < 2000


def test_link_round_trip_ignores_forward_wall_clock_jump(monkeypatch) -> None:
    host = HostAdapterBase()
    monkeypatch.setattr(host, "_PING_TIMEOUT_SECONDS", 0.2)
    real_time = time.time
    offset = [0.0]
    monkeypatch.setattr(time, "time", lambda: real_time() + offset[0])

    def ping(_timeout_s: float) -> float:
        server_timestamp = time.time()
        # 每轮往返途中墙钟都前跳 13 s
        offset[0] += 13.0
        return server_timestamp

    link = BackendLink(name="http", transport="http", target="http://backend.test", available=True, ping=ping)
    result = host._probe_link(link)

    assert result["test_count"] == 5
    assert 0 <= result["avg_rtt_ms"] < 1000
    assert abs(result["avg_time_diff_ms"]) < 1000
