"""Edge ↔ Backend 链路诊断：拓扑由 Backend 会话抽象，适配器只按会话描述的链路做 ping-pong。

- 会话 ``describe_links()``：同一 Backend 地址（--address，缺省本机 Backend 端口）上的两条网络
  链路——HTTP 数据面与 runtime.v1 控制 WebSocket（连着才可测）。Backend 与 Host 分进程，
  不存在"进程内控制面"；
- 控制面 ping/pong 簿记在会话上：发送失败立即返回、pong 直接唤醒等待者、迟到 / 伪造的丢弃。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Optional

import pytest

from unilabos.backend.runtime.host_adapter import HostAdapterBase
from unilabos.config.config import BasicConfig, HTTPConfig
from unilabos.server.backend.legacy_adaptor import session as session_module
from unilabos.server.backend.legacy_adaptor.session import BackendLink, BaseBackendClient


class _Session(BaseBackendClient):
    """最小会话替身：可配置控制面 ping 的发送结果 / 是否回 pong，HTTP ping 可替换。"""

    def __init__(self, *, sent: bool = True, reply: bool = True, connected: bool = True, url: str = "ws://backend/ws") -> None:
        super().__init__()
        self.is_disabled = False
        self.sent = sent
        self.reply = reply
        self._connected = connected
        self.websocket_url = url
        self.pings: list[tuple[str, float]] = []
        self.http_calls = 0
        self.http_ok = True

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def publish_device_status(self, device_status: dict, device_id: str, property_name: str) -> None: ...

    def publish_job_status(self, feedback_data: dict, job_id: str, status: str, return_info: Optional[dict] = None) -> None: ...

    def is_connected(self) -> bool:  # type: ignore[override]
        return self._connected

    def send_ping(self, ping_id: str, timestamp: float) -> bool:
        self.pings.append((ping_id, timestamp))
        if not self.sent:
            return False
        if self.reply:
            threading.Timer(
                0.005,
                self.handle_pong,
                args=({"ping_id": ping_id, "client_timestamp": timestamp, "server_timestamp": time.time()},),
            ).start()
        return True

    def http_ping(self, timeout_s: float = 3.0) -> Optional[float]:  # type: ignore[override]
        self.http_calls += 1
        return time.time() if self.http_ok else None


@pytest.fixture
def default_address(monkeypatch):
    """没给 --address：Backend 就是本机自己的 Backend 端口。"""

    monkeypatch.setattr(HTTPConfig, "remote_addr", "")
    monkeypatch.setattr(HTTPConfig, "backend_port", 18081)
    monkeypatch.setattr(BasicConfig, "port", 18002)


@pytest.fixture
def configured_address(monkeypatch):
    """--address 显式指向别处的 Backend。"""

    monkeypatch.setattr(HTTPConfig, "remote_addr", "http://backend.lab:8081/")


def _adapter(monkeypatch, session: _Session) -> HostAdapterBase:
    host = HostAdapterBase()
    monkeypatch.setattr(session_module, "get_backend_client", lambda: session)
    monkeypatch.setattr(host, "_PING_TIMEOUT_SECONDS", 0.2)
    return host


def _no_leftovers(session: BaseBackendClient) -> None:
    assert session._ping_waiters == {}
    assert session._ping_responses == {}


# ── 会话：拓扑描述 ──


def test_default_address_is_own_backend_port_for_both_links(default_address) -> None:
    """没给 --address：HTTP 与控制 WS 都指向本机 Backend 端口，而不是 Host 自己的管理端口。"""

    session = _Session(connected=False, url="")
    assert session.address_source() == "default"
    assert session.backend_url() == "http://127.0.0.1:18081"
    links = {link.name: link for link in session.describe_links()}
    assert links["http"].target == "http://127.0.0.1:18081/api/v1/ping" and links["http"].available
    assert links["control"].transport == "websocket"
    assert links["control"].target == "ws://127.0.0.1:18081/api/v1/ws/schedule"
    assert not links["control"].available and links["control"].reason == "not_connected"
    assert {link.transport for link in session.describe_links()} == {"http", "websocket"}


def test_configured_address_drives_both_links(configured_address) -> None:
    connected = _Session(connected=True, url="ws://backend.lab:8081/api/v1/ws/schedule")
    assert connected.address_source() == "configured"
    assert connected.backend_url() == "http://backend.lab:8081"
    links = {link.name: link for link in connected.describe_links()}
    assert links["http"].target == "http://backend.lab:8081/api/v1/ping"
    assert links["control"].transport == "websocket" and links["control"].available
    assert links["control"].target == "ws://backend.lab:8081/api/v1/ws/schedule"

    offline = {link.name: link for link in _Session(connected=False, url="").describe_links()}
    assert not offline["control"].available and offline["control"].reason == "not_connected"
    # 客户端还没建 websocket_url 时，控制面目标按同一 Backend 地址派生
    assert offline["control"].target == "ws://backend.lab:8081/api/v1/ws/schedule"


# ── 会话：控制面 ping / pong 簿记 ──


def test_control_ping_is_woken_by_matching_pong_and_cleans_up() -> None:
    session = _Session()
    started = time.monotonic()
    server_ts = session.ping_control_link(timeout_s=1.0)
    assert server_ts is not None and server_ts <= time.time()
    assert time.monotonic() - started < 0.5
    _no_leftovers(session)


def test_control_ping_returns_none_on_send_failure_timeout_or_exception() -> None:
    assert _Session(sent=False).ping_control_link(timeout_s=0.2) is None
    started = time.monotonic()
    assert _Session(reply=False).ping_control_link(timeout_s=0.2) is None
    assert 0.2 <= time.monotonic() - started < 1.0

    class _Broken(_Session):
        def send_ping(self, *_args: Any) -> bool:
            raise RuntimeError("socket closed")

    broken = _Broken()
    assert broken.ping_control_link(timeout_s=0.2) is None
    _no_leftovers(broken)


def test_control_ping_stops_waiting_when_connection_drops() -> None:
    session = _Session(reply=False)
    threading.Timer(0.05, lambda: setattr(session, "_connected", False)).start()
    started = time.monotonic()
    assert session.ping_control_link(timeout_s=5.0) is None
    assert time.monotonic() - started < 1.0
    _no_leftovers(session)


def test_unsolicited_or_malformed_pong_is_ignored() -> None:
    session = _Session()
    assert session.handle_pong({"ping_id": "late", "client_timestamp": 1.0, "server_timestamp": 2.0}) is False
    _no_leftovers(session)
    waiter = threading.Event()
    session._ping_waiters["p1"] = waiter
    assert session.handle_pong({"ping_id": "p1", "client_timestamp": 1.0}) is False
    assert session.handle_pong({"ping_id": "p1", "client_timestamp": 1.0, "server_timestamp": 2.0, "junk": 1}) is False
    assert not waiter.is_set()
    assert session.handle_pong({"ping_id": "p1", "client_timestamp": 1.0, "server_timestamp": 2.0}) is True
    assert waiter.is_set()
    assert session._ping_responses["p1"] == {"ping_id": "p1", "client_timestamp": 1.0, "server_timestamp": 2.0}


def test_adapter_handle_pong_response_delegates_to_session(monkeypatch) -> None:
    session = _Session()
    host = _adapter(monkeypatch, session)
    waiter = threading.Event()
    session._ping_waiters["p9"] = waiter
    host.handle_pong_response({"ping_id": "p9", "client_timestamp": 1.0, "server_timestamp": 2.0})
    assert waiter.is_set()


# ── 适配器：按会话链路做诊断 ──


def test_default_address_latency_targets_own_backend_port(monkeypatch, default_address) -> None:
    session = _Session(connected=False, url="")
    host = _adapter(monkeypatch, session)
    result = host.test_latency()
    assert result["address_source"] == "default" and result["backend_url"] == "http://127.0.0.1:18081"
    assert result["status"] == "success" and result["test_count"] == 5
    assert session.http_calls == 5 and session.pings == []
    assert result["links"]["http"]["status"] == "success"
    assert result["links"]["control"]["transport"] == "websocket"
    assert result["links"]["control"]["status"] == "not_connected"


def test_configured_address_latency_measures_http_and_websocket(monkeypatch, configured_address) -> None:
    session = _Session(connected=True, url="ws://backend.lab:8081/api/v1/ws/schedule")
    host = _adapter(monkeypatch, session)
    result = host.test_latency()
    assert result["address_source"] == "configured" and result["backend_url"] == "http://backend.lab:8081"
    assert result["links"]["http"]["status"] == "success"
    assert result["links"]["control"]["status"] == "success"
    assert result["links"]["control"]["target"] == "ws://backend.lab:8081/api/v1/ws/schedule"
    assert len(session.pings) == 5
    _no_leftovers(session)


def test_disconnected_control_is_skipped_but_http_still_reported(monkeypatch, configured_address) -> None:
    session = _Session(connected=False, url="ws://backend.lab:8081/api/v1/ws/schedule")
    host = _adapter(monkeypatch, session)
    result = host.test_latency()
    assert result["status"] == "success"  # HTTP 数据面通了
    assert result["links"]["control"]["status"] == "not_connected"
    assert session.pings == []


def test_http_down_falls_back_to_control_link_stats(monkeypatch, configured_address) -> None:
    session = _Session(connected=True)
    session.http_ok = False
    host = _adapter(monkeypatch, session)
    result = host.test_latency()
    assert result["links"]["http"]["status"] == "all_timeout"
    assert result["links"]["control"]["status"] == "success"
    assert result["status"] == "success" and result["test_count"] == 5  # 顶层退回控制面统计


def test_nothing_reachable_is_reported(monkeypatch, configured_address) -> None:
    session = _Session(connected=False, url="")
    session.http_ok = False
    host = _adapter(monkeypatch, session)
    result = host.test_latency()
    assert result["status"] == "all_timeout" and result["test_count"] == 0
    assert result["links"]["http"]["status"] == "all_timeout"
    assert result["links"]["control"]["status"] == "not_connected"


def test_http_ping_hits_the_ping_endpoint(monkeypatch, default_address) -> None:
    """真实 http_ping：拼 /api/v1/ping?client_timestamp=…，解析 server_timestamp。"""

    import io
    import json
    import urllib.request

    seen: list[str] = []

    class _Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    def fake_urlopen(url: str, timeout: float):
        seen.append(url)
        # /api/v1/ping 走统一信封
        return _Response(json.dumps({"code": 0, "data": {"client_timestamp": 1.5, "server_timestamp": 42.0}}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    session = _Session()
    assert BaseBackendClient.http_ping(session) == 42.0
    # 目标是 Backend 端口（缺省地址），不是 Host 自己的管理端口 18002
    assert len(seen) == 1 and seen[0].startswith("http://127.0.0.1:18081/api/v1/ping?client_timestamp=")
