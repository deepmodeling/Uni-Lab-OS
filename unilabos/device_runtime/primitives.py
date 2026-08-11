"""Small ROS-shaped runtime primitives implemented with the Python stdlib."""

from __future__ import annotations

import asyncio
import inspect
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional


@dataclass(frozen=True)
class TimeMessage:
    sec: int
    nanosec: int


class DeviceTime:
    def __init__(self, *, nanoseconds: Optional[int] = None) -> None:
        self.nanoseconds = int(time.time_ns() if nanoseconds is None else nanoseconds)

    def seconds_nanoseconds(self) -> tuple[int, int]:
        return divmod(self.nanoseconds, 1_000_000_000)

    def to_msg(self) -> TimeMessage:
        seconds, nanoseconds = self.seconds_nanoseconds()
        return TimeMessage(sec=seconds, nanosec=nanoseconds)


class DeviceClock:
    def now(self) -> DeviceTime:
        return DeviceTime()


@dataclass(frozen=True)
class DeviceParameterValue:
    value: Any

    @property
    def bool_value(self) -> bool:
        return bool(self.value) if isinstance(self.value, bool) else False

    @property
    def integer_value(self) -> int:
        return (
            int(self.value)
            if isinstance(self.value, int) and not isinstance(self.value, bool)
            else 0
        )

    @property
    def double_value(self) -> float:
        return (
            float(self.value)
            if isinstance(self.value, (int, float)) and not isinstance(self.value, bool)
            else 0.0
        )

    @property
    def string_value(self) -> str:
        return self.value if isinstance(self.value, str) else ""

    @property
    def byte_array_value(self) -> list[int]:
        return list(self.value) if isinstance(self.value, (bytes, bytearray)) else []

    @property
    def bool_array_value(self) -> list[bool]:
        return (
            list(self.value)
            if isinstance(self.value, list)
            and all(isinstance(v, bool) for v in self.value)
            else []
        )

    @property
    def integer_array_value(self) -> list[int]:
        return (
            list(self.value)
            if isinstance(self.value, list)
            and all(isinstance(v, int) and not isinstance(v, bool) for v in self.value)
            else []
        )

    @property
    def double_array_value(self) -> list[float]:
        return (
            [float(v) for v in self.value]
            if isinstance(self.value, list)
            and all(
                isinstance(v, (int, float)) and not isinstance(v, bool)
                for v in self.value
            )
            else []
        )

    @property
    def string_array_value(self) -> list[str]:
        return (
            list(self.value)
            if isinstance(self.value, list)
            and all(isinstance(v, str) for v in self.value)
            else []
        )


@dataclass(frozen=True)
class DeviceParameter:
    name: str
    value: Any = None

    def get_parameter_value(self) -> DeviceParameterValue:
        return DeviceParameterValue(self.value)


@dataclass(frozen=True)
class SetParametersResult:
    successful: bool = True
    reason: str = ""


class DeviceRate:
    def __init__(self, frequency: float) -> None:
        if float(frequency) <= 0:
            raise ValueError("rate frequency 必须大于 0")
        self._period = 1.0 / float(frequency)

    def sleep(self) -> None:
        time.sleep(self._period)


class DeviceTimer:
    """A repeating timer scheduled by a backend-neutral ``DeviceNode``."""

    def __init__(
        self,
        node: Any,
        period: float,
        callback: Callable[[], Any],
        *,
        autostart: bool = True,
    ) -> None:
        if float(period) <= 0:
            raise ValueError("timer period 必须大于 0")
        self._node = node
        self._period = float(period)
        self._callback = callback
        self._lock = threading.Lock()
        self._future: Any = None
        self._cancelled = True
        self._last_call_ns = 0
        self._next_call_ns = 0
        if autostart:
            self.reset()

    @property
    def timer_period_ns(self) -> int:
        return int(self._period * 1_000_000_000)

    def reset(self) -> None:
        with self._lock:
            previous = self._future
            self._cancelled = False
            self._next_call_ns = time.time_ns() + self.timer_period_ns
            self._future = self._node.create_task(self._run())
        if previous is not None:
            previous.cancel()

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            future = self._future
            self._future = None
        if future is not None:
            future.cancel()

    def is_canceled(self) -> bool:
        return self._cancelled

    def is_ready(self) -> bool:
        return not self._cancelled and time.time_ns() >= self._next_call_ns

    def time_since_last_call(self) -> Optional[int]:
        if not self._last_call_ns:
            return None
        return time.time_ns() - self._last_call_ns

    def time_until_next_call(self) -> Optional[int]:
        if self._cancelled:
            return None
        return max(0, self._next_call_ns - time.time_ns())

    async def _run(self) -> None:
        try:
            while not self._cancelled:
                await self._node.sleep(self._period)
                if self._cancelled:
                    break
                self._last_call_ns = time.time_ns()
                self._next_call_ns = self._last_call_ns + self.timer_period_ns
                result = self._callback()
                if inspect.isawaitable(result):
                    await result
        except asyncio.CancelledError:
            pass


__all__ = [
    "DeviceClock",
    "DeviceParameter",
    "DeviceParameterValue",
    "DeviceRate",
    "DeviceTime",
    "DeviceTimer",
    "SetParametersResult",
    "TimeMessage",
]
