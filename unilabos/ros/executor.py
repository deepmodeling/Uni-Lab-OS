"""设备 Runtime 的 ROS 执行器与线程存活检查。"""

from __future__ import annotations

from threading import Thread
from typing import Any

from rclpy.exceptions import InvalidHandle
from rclpy.executors import MultiThreadedExecutor
from rclpy.impl.implementation_singleton import rclpy_implementation as _rclpy
from rclpy.timer import Timer

_DESTROYED_TIMER = object()


class RuntimeExecutor(MultiThreadedExecutor):
    """允许等待队列中的定时器在回调取出前已被动作协程取消或释放。"""

    def _take_timer(self, timer: Timer) -> Any:
        """只丢弃 Humble 等待队列中已取消或销毁定时器的过期事件。"""
        try:
            return super()._take_timer(timer)
        except InvalidHandle:
            return _DESTROYED_TIMER
        except _rclpy.RCLError as error:
            # RCLError 未暴露 RCL 返回码，只识别 call_timer 的取消错误；
            # 不能按事后 is_canceled() 判断，其他线程可能已经 reset 了定时器。
            if str(error).partition(", at ")[0] != "failed to call timer: timer is canceled":
                raise
            return _DESTROYED_TIMER

    async def _execute_timer(self, timer: Timer, taken: Any) -> None:
        """过期事件不再调用用户回调；其他回调与异常保留 rclpy 原语义。"""
        if taken is not _DESTROYED_TIMER:
            await super()._execute_timer(timer, taken)


def ensure_executor_thread_running(thread: Thread) -> None:
    """执行线程退出时终止 Runtime 启动/运行循环，触发既有崩溃处理。"""

    if not thread.is_alive():
        raise RuntimeError(f"ROS executor thread terminated unexpectedly: {thread.name}")
