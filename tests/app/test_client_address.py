from __future__ import annotations

from unilabos.client.runtime.workflow import (
    derive_workflow_websocket_url,
    normalize_workflow_api_url,
)
from unilabos.config.config import HTTPConfig
from unilabos.server.backend.legacy_adaptor.url import (
    build_backend_websocket_url,
    build_legacy_backend_websocket_url,
)
from unilabos.utils.address import derive_websocket_address, resolve_address


def test_address_normalization_has_no_builtin_default() -> None:
    # 地址解析只执行规范化，不注入云端默认值或环境别名。
    assert resolve_address("http://edge:8002/") == "http://edge:8002"
    assert resolve_address("") == ""
    assert resolve_address(None) == ""
    assert normalize_workflow_api_url("http://edge:8002") == (
        "http://edge:8002/api/v1"
    )


def test_runtime_v1_websocket_shares_the_http_port(monkeypatch) -> None:
    """runtime.v1 的 HTTP API 与 WS 控制面是同一个微后端服务，同 host 同端口。"""

    monkeypatch.setattr(HTTPConfig, "remote_addr", "http://backend:8081/api/v1")
    monkeypatch.setattr(HTTPConfig, "schedule_addr", "")

    expected = "ws://backend:8081/api/v1/ws/schedule"
    assert build_backend_websocket_url() == expected
    assert derive_workflow_websocket_url("http://backend:8081/api/v1") == expected
    assert derive_websocket_address("https://backend:8081") == (
        "wss://backend:8081/api/v1/ws/schedule"
    )


def test_port_offset_is_only_applied_by_the_legacy_adaptor(monkeypatch) -> None:
    """旧云端的 ``+1`` 端口约定只存在于 legacy 适配器的地址构造。"""

    monkeypatch.setattr(HTTPConfig, "remote_addr", "https://legacy.example:8002/api/v1")
    monkeypatch.setattr(HTTPConfig, "schedule_addr", "")

    assert build_legacy_backend_websocket_url() == (
        "wss://legacy.example:8003/api/v1/ws/schedule"
    )
    assert build_backend_websocket_url() == (
        "wss://legacy.example:8002/api/v1/ws/schedule"
    )
    # 没有显式端口时两者都沿用原 netloc。
    monkeypatch.setattr(HTTPConfig, "remote_addr", "https://legacy.example/api/v1")
    assert build_legacy_backend_websocket_url() == (
        "wss://legacy.example/api/v1/ws/schedule"
    )


def test_low_level_schedule_override_preserves_explicit_path(monkeypatch) -> None:
    monkeypatch.setattr(HTTPConfig, "remote_addr", "https://backend/api/v1")
    monkeypatch.setattr(
        HTTPConfig,
        "schedule_addr",
        "wss://notices.example/control/ws?tenant=lab-1",
    )

    assert build_backend_websocket_url() == (
        "wss://notices.example/control/ws?tenant=lab-1"
    )
    assert build_legacy_backend_websocket_url() == (
        "wss://notices.example/control/ws?tenant=lab-1"
    )
