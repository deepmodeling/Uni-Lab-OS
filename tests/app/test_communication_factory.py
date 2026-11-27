from unittest.mock import patch

from unilabos.app.communication import CommunicationClientFactory


def test_edge_control_protocol_uses_production_client_factory() -> None:
    sentinel = object()

    with patch.object(
        CommunicationClientFactory,
        "_create_edge_control_client",
        return_value=sentinel,
    ) as factory:
        client = CommunicationClientFactory.create_client("edge_control")

    assert client is sentinel
    factory.assert_called_once_with()


def test_supported_protocols_include_production_edge_control() -> None:
    assert CommunicationClientFactory.get_supported_protocols() == [
        "local",
        "edge_control",
    ]


def test_local_protocol_never_creates_cloud_client() -> None:
    """本地默认通信协议返回空适配器，不创建云端 WebSocket。"""

    client = CommunicationClientFactory.create_client("local")

    assert client.is_disabled


def test_legacy_websocket_protocol_is_rejected() -> None:
    """已移除的云端 WebSocket 协议必须关闭失败。"""

    import pytest

    with pytest.raises(ValueError, match="移除云端"):
        CommunicationClientFactory.create_client("websocket")
