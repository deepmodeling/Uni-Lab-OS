"""HostLink 本地运行时按注册表 ``timeout`` 声明在设备层执行硬超时。"""

from __future__ import annotations

import asyncio
import time

import pytest

from unilabos.backend.hostlink.local_runtime import HostLinkDriverSpec, HostLinkLocalRuntime
from unilabos.backend.runtime.action import ActionContext
from unilabos.backend.runtime.exception import TimeoutException


class SlowDriver:
    def __init__(self, device_id=None, config=None):
        self.device_id = device_id
        self.config = config
        self.cancelled = False
        self.sync_finished = False

    async def slow_async(self, duration: float = 1.0) -> str:
        try:
            await asyncio.sleep(duration)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return "done"

    def slow_sync(self, duration: float = 1.0) -> str:
        time.sleep(duration)
        self.sync_finished = True
        return "done"

    def quick(self) -> str:
        return "quick"


def _runtime() -> HostLinkLocalRuntime:
    runtime = HostLinkLocalRuntime()
    runtime.add_driver(
        HostLinkDriverSpec(
            "slow",
            SlowDriver,
            {},
            action_names=("slow_async", "slow_sync", "quick"),
            action_value_mappings={
                "slow_async": {"type": "UniLabJsonCommandAsync", "timeout": 0.05},
                "slow_sync": {"type": "UniLabJsonCommand", "timeout": 0.05},
                "quick": {"type": "UniLabJsonCommand", "timeout": 5},
            },
        )
    )
    return runtime


def test_hostlink_hard_timeout_cancels_coroutine_action() -> None:
    runtime = _runtime()
    runtime.start()
    try:
        with pytest.raises(TimeoutException) as exc_info:
            runtime.call_action("slow", "slow_async", duration=1.0)
        error = exc_info.value
        assert error.timeout_seconds == 0.05
        assert error.device_id == "slow"
        info = error.to_error_info()
        assert info["exception_type"] == "TimeoutException"
        assert info["category"] == "timeout"
        assert "TimeoutException" in info["exception_mro"]
        driver = runtime.devices["slow"].driver
        # 协程动作被真正取消
        deadline = time.time() + 1.0
        while not driver.cancelled and time.time() < deadline:
            time.sleep(0.01)
        assert driver.cancelled is True
        # 未超时的动作不受影响
        assert runtime.call_action("slow", "quick") == "quick"
    finally:
        runtime.stop()


def test_hostlink_hard_timeout_abandons_sync_action_thread() -> None:
    runtime = _runtime()
    runtime.start()
    try:
        started = time.time()
        with pytest.raises(TimeoutException):
            runtime.call_action("slow", "slow_sync", duration=0.3)
        # 调用方在超时点立刻拿到异常，而不是等同步线程跑完
        assert time.time() - started < 0.25
        # 线程会继续跑完，但结果被丢弃
        deadline = time.time() + 1.0
        driver = runtime.devices["slow"].driver
        while not driver.sync_finished and time.time() < deadline:
            time.sleep(0.01)
        assert driver.sync_finished is True
    finally:
        runtime.stop()


def test_sync_thread_outliving_the_timeout_is_reported_and_its_late_result_delivered() -> None:
    """同步线程超时后仍在跑：异常带 driver_still_running，线程返回后真实结果经动作上下文交回（ADR 0008）。"""

    runtime = _runtime()
    runtime.start()
    late: list = []
    try:
        context = ActionContext(late_result_callback=lambda action_id, value, error: late.append((value, error)))
        with pytest.raises(TimeoutException) as exc_info:
            runtime.call_action("slow", "slow_sync", action_context=context, duration=0.3)
        info = exc_info.value.to_error_info()
        assert exc_info.value.driver_still_running is True and info["driver_still_running"] is True
        assert late == []
        deadline = time.time() + 2.0
        while not late and time.time() < deadline:
            time.sleep(0.01)
        assert late == [("done", None)]

        # 协程动作被真正取消，不算"驱动仍在执行"
        with pytest.raises(TimeoutException) as exc_info:
            runtime.call_action("slow", "slow_async", duration=1.0)
        assert exc_info.value.driver_still_running is False
        assert "driver_still_running" not in exc_info.value.to_error_info()
    finally:
        runtime.stop()
