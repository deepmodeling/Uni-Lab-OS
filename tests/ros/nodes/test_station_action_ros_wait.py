"""无需启动 ROS，按 executor 逐步推进真实动作包装器，验证无 asyncio loop 的等待。"""

from __future__ import annotations

import ast
import asyncio
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace

import pytest


class DeferredStart(Exception):
    pass


class RosWait:
    def __await__(self):
        yield None


def _wrapper(submit, sleeps):
    # 提取生产嵌套函数，保留执行分支；隔离设备初始化、注册和硬件依赖。
    path = Path(__file__).resolve().parents[3] / "unilabos/ros/nodes/base_device_node.py"
    tree = ast.parse(path.read_text())
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_station_action")

    async def ros_sleep(seconds):
        sleeps.append(seconds)
        await RosWait()

    namespace = {
        "asyncio": asyncio,
        "raw_action": lambda **kwargs: None,
        "json_wrapper": False,
        "submit_with_context": submit,
        "self": SimpleNamespace(_executor=None, sleep=ros_sleep),
        "_guarded_sync": lambda **kwargs: None,
        "ExecutionStartDeferred": DeferredStart,
    }
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), str(path), "exec"), namespace)  # noqa: S102
    return namespace["_station_action"]()


def test_waits_for_driver_result_without_asyncio_event_loop():
    sleeps = []
    pending = Future()
    coro = _wrapper(lambda *args, **kwargs: pending, sleeps)
    try:
        assert coro.send(None) is None
        assert sleeps == [0.05]
        pending.set_result({"success": True})
        with pytest.raises(StopIteration) as completed:
            coro.send(None)
        assert completed.value.value == {"success": True}
    finally:
        coro.close()


def test_defers_driver_start_and_retries_without_asyncio_event_loop():
    sleeps = []
    blocked = Future()
    blocked.set_exception(DeferredStart())
    ready = Future()
    ready.set_result({"success": True})
    attempts = iter([blocked, ready])
    coro = _wrapper(lambda *args, **kwargs: next(attempts), sleeps)
    try:
        assert coro.send(None) is None
        assert sleeps == [0.1]
        with pytest.raises(StopIteration) as completed:
            coro.send(None)
        assert completed.value.value == {"success": True}
    finally:
        coro.close()
