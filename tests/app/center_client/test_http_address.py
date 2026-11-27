"""工站向中心报告可访问的本地 HTTP origin；配置和真实 hello 使用同一字段。"""
from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from tests.app.center_client.test_center_client import config
from tests.app.center_client.test_websocket import Connection, hello, setup_receiver
from unilabos.app.center_client.config import CenterClientConfig, CenterConfigError


@pytest.mark.parametrize("public", [None, "", "http://203.0.113.10:30193/", "https://station.example/", "https://[2001:db8::1]:8443"])
def test_http_address_from_environment_is_sent_in_hello(tmp_path, public):
    server, client, store, receiver = setup_receiver(tmp_path)
    original = client._config
    cfg = CenterClientConfig.from_env({
        "CENTER_ENDPOINT": original.endpoint, "CENTER_CLIENT_CERT": str(original.client_cert),
        "CENTER_CLIENT_KEY": str(original.client_key), "CENTER_SERVER_CA": str(original.server_ca),
        "CENTER_MACHINE_SECRET": original.machine_secret, "CENTER_INSTANCE_UUID": original.instance_uuid,
        "CENTER_STATION_UUID": original.station_uuid, "CENTER_HTTP_BASE_URL": "http://station.example:8002/",
        **({"CENTER_PUBLIC_HTTP_BASE_URL": public} if public is not None else {}),
    })
    receiver.config = cfg
    sent = []
    try:
        with pytest.raises(EOFError):
            asyncio.run(receiver._exchange(Connection([hello(client.session)], sent.append), client.session))
        assert sent[0]["payload"]["http_base_url"] == "http://station.example:8002"
        if public:
            assert sent[0]["payload"]["public_http_base_url"] == public.rstrip("/")
        else:
            assert cfg.public_http_base_url is None
            assert "public_http_base_url" not in sent[0]["payload"]
    finally:
        store.close()


def test_unconfigured_station_preserves_legacy_hello(tmp_path):
    _, client, store, receiver = setup_receiver(tmp_path)
    sent = []
    try:
        with pytest.raises(EOFError):
            asyncio.run(receiver._exchange(Connection([hello(client.session)], sent.append), client.session))
        assert "http_base_url" not in sent[0]["payload"]
        assert "public_http_base_url" not in sent[0]["payload"]
    finally:
        store.close()


@pytest.mark.parametrize("address", ["https://station:8443", "http://user:password@station", "http://station/api",
    "http://station?x=1", "http://station?", "http://station#fragment", "http://station#", "http://",
    "http://station:99999", "http://station:0", "http://station:invalid", "http://station\n", "not-a-url"])
def test_reject_http_address_outside_center_contract(tmp_path, address):
    with pytest.raises(CenterConfigError, match="CENTER_HTTP_BASE_URL"):
        replace(config(tmp_path), http_base_url=address)


@pytest.mark.parametrize("address", ["http://station:8002", "http://127.0.0.1:8002/", "http://[::1]:8002"])
def test_accept_http_origins(tmp_path, address):
    assert config(tmp_path, http_base_url=address).http_base_url == address.rstrip("/")


def test_public_address_never_supplies_the_private_address(tmp_path):
    _, client, store, receiver = setup_receiver(tmp_path)
    receiver.config = replace(client._config, public_http_base_url="https://public.example/")
    sent = []
    try:
        with pytest.raises(EOFError):
            asyncio.run(receiver._exchange(Connection([hello(client.session)], sent.append), client.session))
        assert "http_base_url" not in sent[0]["payload"]
        assert sent[0]["payload"]["public_http_base_url"] == "https://public.example"
    finally:
        store.close()


@pytest.mark.parametrize("address", ["", "ftp://station", "//station", "http://", "http://user:password@station",
    "http://station/api", "http://station//", "http://station?", "http://station#", "http://station:0",
    "http://station:65536", "http://station:invalid", "http://station:", "http://station\n",
    "http://station\\path", "https://[invalid]:8443"])
def test_reject_invalid_public_address(tmp_path, address):
    with pytest.raises(CenterConfigError, match="CENTER_PUBLIC_HTTP_BASE_URL"):
        config(tmp_path, public_http_base_url=address)
