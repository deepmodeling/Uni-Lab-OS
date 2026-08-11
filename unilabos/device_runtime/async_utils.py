"""Backend 无关的异步函数调度辅助。"""

from __future__ import annotations

import asyncio
from concurrent.futures import CancelledError as FutureCancelledError
import inspect
import traceback
from typing import Any, Awaitable, Callable, Optional


TaskScheduler = Callable[[Awaitable[Any]], Any]
TraceCallback = Callable[[Any], None]
ErrorCallback = Callable[[str], None]


def schedule_async_func(
    scheduler: TaskScheduler,
    func: Any,
    trace_error: bool = True,
    inner_trace_callback: Optional[TraceCallback] = None,
    error_callback: Optional[ErrorCallback] = None,
    **kwargs: Any,
) -> Any:
    """用 backend 提供的 scheduler 执行函数或 awaitable，并返回其 Future。"""

    if not callable(func) and kwargs:
        raise TypeError("awaitable 对象不能再接收额外关键字参数")

    task_name = str(
        getattr(func, "__qualname__", "")
        or getattr(func, "__name__", "")
        or type(func).__name__
    )

    async def invoke() -> Any:
        try:
            result = func(**kwargs) if callable(func) else func
            if inspect.isawaitable(result):
                result = await result
        except BaseException as exc:
            if inner_trace_callback is not None:
                inner_trace_callback(exc)
            raise
        if inner_trace_callback is not None:
            inner_trace_callback(result)
        return result

    coroutine = invoke()
    try:
        future = scheduler(coroutine)
    except BaseException:
        coroutine.close()
        raise

    if trace_error:
        def report_error(done_future: Any) -> None:
            try:
                done_future.result()
            except (asyncio.CancelledError, FutureCancelledError):
                return
            except BaseException as exc:
                if error_callback is not None:
                    detail = "".join(
                        traceback.format_exception(type(exc), exc, exc.__traceback__)
                    )
                    error_callback(f"异步任务 {task_name} 执行失败\n{detail}")

        future.add_done_callback(report_error)

    return future


__all__ = ["schedule_async_func"]
