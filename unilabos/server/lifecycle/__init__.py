"""进程内管理服务的停机信号。

uvicorn 的优雅停机只是"不再接新连接、等在途请求结束"：浏览器长期挂着的 SSE
（``/api/v1/events``、``/api/v1/materials/events``）永远不会自己结束，于是每次停机都要
等满 ``timeout_graceful_shutdown``，再被 uvicorn 强行取消，控制台留下一串
"Cancel N running task(s)" 与 CancelledError 堆栈。长连接处理器在每个心跳周期检查
本模块的信号，停机开始就主动收尾，uvicorn 便能在等待上限之前正常结束。

信号来源：uvicorn 收到 SIGINT / SIGTERM（``handle_exit``）、安静点重启
（``request_server_shutdown``）、设备 runtime 致命失败（``abort_serving``）。
本模块不依赖任何服务模块，API 路由与日志适配器都可以直接导入。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Callable

logger = logging.getLogger(__name__)

_shutdown = threading.Event()
_listeners: list[Callable[[], None]] = []
_lock = threading.Lock()


def begin_shutdown() -> None:
    """标记管理服务开始停机并通知监听者；幂等，重复调用不再通知。

    可能在信号处理器里被调用：监听者必须只做非阻塞的"发出请求"类动作。
    """

    with _lock:
        if _shutdown.is_set():
            return
        _shutdown.set()
        listeners = list(_listeners)
    for listener in listeners:
        try:
            listener()
        except Exception:  # noqa: BLE001 - 一个监听者出错不能拦住停机
            logger.exception("[Lifecycle] 停机监听者执行失败")


def on_shutdown(listener: Callable[[], None]) -> Callable[[], None]:
    """注册停机开始时的回调（如：立刻请求 Host 子进程退出，与本进程停机并行）。

    Returns:
        取消注册的函数。
    """

    with _lock:
        _listeners.append(listener)

    def _remove() -> None:
        with _lock:
            if listener in _listeners:
                _listeners.remove(listener)

    return _remove


def shutting_down() -> bool:
    return _shutdown.is_set()


def reset_shutdown_state() -> None:
    """新一轮服务启动前清掉上一轮的停机标记（同进程重启 / 测试）。"""

    _shutdown.clear()


async def sleep_unless_shutting_down(seconds: float) -> bool:
    """长连接的心跳等待：睁着眼睛睡，停机开始就提前醒来。

    Returns:
        True 表示可以继续下一轮；False 表示服务正在停机，处理器应立即收尾。
    """

    if _shutdown.is_set():
        return False
    # threading.Event 没有 await 接口；分片轮询让唤醒延迟有上界，又不必为它起线程
    step = min(0.2, seconds)
    remaining = seconds
    while remaining > 0:
        await asyncio.sleep(min(step, remaining))
        if _shutdown.is_set():
            return False
        remaining -= step
    return True


__all__ = [
    "begin_shutdown",
    "on_shutdown",
    "reset_shutdown_state",
    "shutting_down",
    "sleep_unless_shutting_down",
]
