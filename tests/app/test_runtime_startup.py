from __future__ import annotations

from unilabos.app.runtime_startup import run_runtime
from unilabos.config.config import BasicConfig


class _Thread:
    def __init__(self) -> None:
        self.join_count = 0

    def join(self) -> None:
        self.join_count += 1


def test_slave_runtime_waits_for_backend_without_starting_web(monkeypatch) -> None:
    thread = _Thread()
    monkeypatch.setattr(BasicConfig, "is_host_mode", False)
    monkeypatch.setattr(
        "unilabos.app.backend.start_backend",
        lambda **_args: thread,
    )

    assert run_runtime({"visual": "disable"}) is None
    assert thread.join_count == 1


def test_host_runtime_starts_management_api(monkeypatch) -> None:
    import unilabos.server.api.app as web

    thread = _Thread()
    calls = []
    monkeypatch.setattr(BasicConfig, "is_host_mode", True)
    monkeypatch.setattr(BasicConfig, "disable_browser", True)
    monkeypatch.setattr(BasicConfig, "port", 9002)
    monkeypatch.setattr(
        "unilabos.app.backend.start_backend",
        lambda **_args: thread,
    )
    monkeypatch.setattr(
        web,
        "start_server",
        lambda **kwargs: calls.append(kwargs) or True,
    )

    assert run_runtime({"visual": "disable"}) is None
    assert thread.join_count == 0
    assert calls == [{"open_browser": False, "port": 9002}]
