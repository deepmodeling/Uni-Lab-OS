"""OS 本地库存权威（Inventory Authority）的物料 HTTP 客户端合同。"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from unilabos.app.web.client import HTTPClient
from unilabos.config.config import BasicConfig


class _Response:
    def __init__(self, payload: Any, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.text = repr(payload)

    def json(self) -> Any:
        return self._payload


class _Session:
    def __init__(self, responses: List[_Response]):
        self.responses = list(responses)
        self.calls: List[Dict[str, Any]] = []

    def post(self, url: str, **kwargs: Any) -> _Response:
        self.calls.append({"method": "POST", "url": url, **kwargs})
        return self.responses.pop(0)

    def get(self, url: str, **kwargs: Any) -> _Response:
        self.calls.append({"method": "GET", "url": url, **kwargs})
        return self.responses.pop(0)


@pytest.fixture(autouse=True)
def _local_os_authority(monkeypatch: pytest.MonkeyPatch) -> None:
    """固定测试中的 OS 主机身份和本地 HTTP 端口。

    参数：``monkeypatch`` 隔离全局配置。返回：无。异常：无；每个用例结束后由
    pytest 恢复配置。
    """

    monkeypatch.setattr(BasicConfig, "is_host_mode", True)
    monkeypatch.setattr(BasicConfig, "port", 8092)


def _client(responses: List[_Response]) -> tuple[HTTPClient, _Session]:
    """建立只查询 OS 本地库存的客户端夹具。

    参数：``responses`` 是本地接口的顺序响应。返回：客户端及记录请求的会话。
    异常：无。
    """

    client = HTTPClient(
        remote_addr="https://formal.example/api/v1",
        auth="test-token",
    )
    session = _Session(responses)
    client._session = session  # type: ignore[assignment]
    return client, session


def test_microbackend_uses_local_legacy_compat_endpoint() -> None:
    """证明物料（Material）查询固定请求当前 OS，而不访问正式后端。"""

    client, session = _client(
        [_Response({"code": 0, "data": {"nodes": [{"uuid": "edge-a"}]}})],
    )

    nodes = client.material_query(uuids=["edge-a"], with_children=False)

    assert nodes == [{"uuid": "edge-a"}]
    assert session.calls == [
        {
            "method": "POST",
            "url": "http://127.0.0.1:8092/api/v1/edge/material/query",
            "json": {"uuids": ["edge-a"], "with_children": False},
            "timeout": 10,
        }
    ]


def test_resource_tree_query_cannot_switch_to_formal_backend() -> None:
    """证明资源树兼容调用同样只能读取 OS 本地库存权威。"""

    client, session = _client(
        [_Response({"code": 0, "data": {"nodes": [{"uuid": "local-a"}]}})],
    )

    assert client.resource_tree_get(["local-a"], True) == [{"uuid": "local-a"}]
    assert (
        session.calls[0]["url"] == "http://127.0.0.1:8092/api/v1/edge/material/query"
    )
    assert session.calls[0]["json"] == {
        "uuids": ["local-a"],
        "with_children": True,
    }


def test_backend_id_query_and_legacy_resource_get_envelope() -> None:
    """证明只有显式旧云端资源入口仍保留原正式后端请求形状。"""

    client, session = _client(
        [_Response({"code": 0, "data": [{"id": "rack-a", "uuid": "u-a"}]})],
    )

    result = client.resource_get("rack-a", with_children=True)

    assert result == {"code": 0, "data": [{"id": "rack-a", "uuid": "u-a"}]}
    assert session.calls[0] == {
        "method": "GET",
        "url": "https://formal.example/api/v1/lab/material",
        "params": {"id": "rack-a", "with_children": True},
        "timeout": 10,
    }


def test_empty_local_query_never_falls_through_to_formal_backend() -> None:
    """证明本地未命中不会把 OS 变成正式后端（Backend）代理。"""

    client, session = _client([_Response({"nodes": []})])

    nodes = client.material_query(uuids=["cloud-a"])

    assert nodes == []
    assert [call["url"] for call in session.calls] == [
        "http://127.0.0.1:8092/api/v1/edge/material/query",
    ]


def test_microbackend_failure_returns_empty_for_host_memory_fallback() -> None:
    """证明本地接口失败时返回空集合，允许主机内存兼容路径接管。"""

    client, _session = _client(
        [_Response({"detail": "inventory disabled"}, status_code=503)],
    )

    assert client.material_query(resource_id="local-rack") == []


def test_default_slave_client_cannot_open_a_direct_material_channel(
    monkeypatch,
) -> None:
    """证明从节点不能绕过 HostLink 直接打开物料（Material）HTTP 通道。"""

    monkeypatch.setattr(BasicConfig, "is_host_mode", False)
    client = HTTPClient(auth="test-token")
    session = _Session(
        [_Response({"code": 0, "data": {"nodes": [{"uuid": "forbidden"}]}})]
    )
    client._session = session  # type: ignore[assignment]

    assert client.material_query(uuids=["forbidden"]) == []
    assert session.calls == []
