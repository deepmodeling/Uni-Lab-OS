"""Edge Runtime 首次控制面连接等待预算。"""

from __future__ import annotations

import threading
from collections.abc import Callable

import pytest

from unilabos.config.config import EdgeControlConfig
from unilabos.ros import runtime


class _Bridge:
    def __init__(self, connected: Callable[[], object]) -> None:
        self._connected = connected

    def is_connected(self) -> bool:
        return bool(self._connected())


def test_edge_control_readiness_timeout_keeps_backward_compatible_default() -> None:
    assert EdgeControlConfig.readiness_timeout == 30.0


def test_edge_control_readiness_waits_for_configured_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """连接耗时超过旧 30 秒但未超过部署预算时仍可正常就绪。"""

    clock = [0.0]
    executor_checks: list[threading.Thread] = []
    thread = threading.current_thread()
    monkeypatch.setattr(EdgeControlConfig, "readiness_timeout", 120.0)
    monkeypatch.setattr(runtime.time, "monotonic", lambda: clock[0])

    def advance(seconds: float) -> None:
        assert seconds == 0.05
        clock[0] += 10.0

    monkeypatch.setattr(runtime.time, "sleep", advance)
    monkeypatch.setattr(
        runtime,
        "ensure_executor_thread_running",
        executor_checks.append,
    )

    runtime._wait_for_control_plane(
        [_Bridge(lambda: clock[0] >= 60.0)],
        thread,
    )

    assert clock[0] == 60.0
    assert executor_checks == [thread] * 7


def test_edge_control_readiness_still_fails_at_configured_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [0.0]
    monkeypatch.setattr(EdgeControlConfig, "readiness_timeout", 0.1)
    monkeypatch.setattr(runtime.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        runtime.time,
        "sleep",
        lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    )
    monkeypatch.setattr(
        runtime,
        "ensure_executor_thread_running",
        lambda _thread: None,
    )

    with pytest.raises(RuntimeError, match="failed to connect its control plane"):
        runtime._wait_for_control_plane(
            [_Bridge(lambda: False)],
            threading.current_thread(),
        )

    assert clock[0] == pytest.approx(0.1)
