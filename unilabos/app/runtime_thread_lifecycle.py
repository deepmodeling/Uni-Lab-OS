"""设备 Runtime 关键线程的进程级失败边界，不依赖 ROS 或业务清理。"""

from __future__ import annotations

import os
import threading
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any, NoReturn

from unilabos.app.runtime_topology import (
    publish_edge_runtime_ready_signal as _publish_ready,
)


class _RuntimeLifecycle:
    """每次启动独立持有停止标记，旧代线程不能误用新代的存活状态。"""

    def __init__(self) -> None:
        self.stopping = threading.Event()
        self.ready_lock = threading.Lock()

    def clear_ready(self) -> None:
        """撤销就绪文件；文件无法删除也不能阻止后续退出。"""
        value = os.environ.get("UNILABOS_EDGE_READY_FILE")
        if value:
            try:
                Path(value).unlink(missing_ok=True)
            except OSError as error:
                _write_diagnostic(f"无法撤销 Runtime 就绪文件: {error}\n")

    def stop(self) -> None:
        """先标记正常停止，并与启动阶段发布 readiness 互斥。"""
        with self.ready_lock:
            self.stopping.set()
            self.clear_ready()


_current_lifecycle = _RuntimeLifecycle()
_thread_context = threading.local()


def _lifecycle() -> _RuntimeLifecycle:
    return getattr(_thread_context, "lifecycle", _current_lifecycle)


def _write_diagnostic(message: str) -> None:
    """直接写入受管 stderr，避免进入已损坏的业务日志锁。"""
    try:
        os.write(2, message.encode("utf-8", errors="replace"))
    except OSError:
        pass


def start_runtime_generation() -> None:
    """为进程内重启创建新代，并删除上一次运行留下的 readiness。"""
    global _current_lifecycle
    _current_lifecycle = _RuntimeLifecycle()
    _current_lifecycle.clear_ready()


def begin_runtime_shutdown() -> None:
    """正常信号退出和重启必须先调用，再关闭 ROS 与通信服务。"""
    _lifecycle().stop()


def runtime_shutdown_requested() -> bool:
    """读取当前代是否已进入显式正常停止。"""
    return _lifecycle().stopping.is_set()


def publish_edge_runtime_ready_signal() -> None:
    """仅允许仍存活的当前代发布就绪，关闭与初始化交错时不重新发布。"""
    lifecycle = _lifecycle()
    with lifecycle.ready_lock:
        if not lifecycle.stopping.is_set():
            _publish_ready()


def fail_runtime_thread(name: str, error: BaseException | None = None) -> NoReturn:
    """关键线程异常或意外返回后非零退出，不等待可能卡住的设备工作线程。"""
    try:
        _lifecycle().stop()
        detail = (
            "".join(traceback.format_exception(error)) if error else "线程意外返回\n"
        )
        _write_diagnostic(
            f"Runtime 关键线程 {name} 已终止，进程将以状态 1 退出\n{detail}"
        )
    finally:
        # SystemExit 只退出当前线程，解释器关闭还会等待 ThreadPoolExecutor；
        # 致命故障不可再调用设备、ROS 或业务关闭钩子，交由监督器启动新代恢复。
        os._exit(1)


def start_runtime_thread(
    *,
    target: Callable[..., Any],
    name: str,
    args: tuple[Any, ...] = (),
) -> threading.Thread:
    """只监督设备 backend 和 ROS executor，其他 Web/开发线程保持原生命周期。"""
    lifecycle = _lifecycle()

    def run() -> None:
        _thread_context.lifecycle = lifecycle
        try:
            target(*args)
        except BaseException as error:
            if not lifecycle.stopping.is_set():
                fail_runtime_thread(name, error)
        else:
            if not lifecycle.stopping.is_set():
                fail_runtime_thread(name)

    thread = threading.Thread(target=run, name=name, daemon=True)
    thread.start()
    return thread
