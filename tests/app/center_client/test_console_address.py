"""操作台入口独立于身份和后端连接：选择、保存、重启和握手确认。"""
import asyncio
from dataclasses import replace
from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.app.center_client.test_connection import Center, Runtime
from tests.app.center_client.test_websocket import Connection, hello, setup_receiver
from unilabos.app.center_client.connection import CenterConnection, ConnectionError
from unilabos.app.center_client.connection_api import create_router
from unilabos.app.center_client.console_address import (
    LAUNCHER_CONSOLE_URL_ENV,
    ConsoleAddressSettings,
    detect_console_origin,
)


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for name in ("CENTER_PUBLIC_HTTP_BASE_URL", LAUNCHER_CONSOLE_URL_ENV):
        monkeypatch.delenv(name, raising=False)


def test_custom_deployment_browser_and_launcher_priority(tmp_path, monkeypatch):
    monkeypatch.setenv("CENTER_PUBLIC_HTTP_BASE_URL", "https://deployed.test/")
    monkeypatch.setenv(LAUNCHER_CONSOLE_URL_ENV, "http://192.168.1.20:8002")
    owner = CenterConnection(tmp_path / "station", Runtime)
    state = owner.save_console_address(None, "http://192.168.1.20:4183/")
    assert state["effective_url"] == "https://deployed.test"
    assert state["source"] == "deployment"
    state = owner.save_console_address("https://custom.test/", "http://another.test:4183")
    assert state["mode"] == "custom" and state["effective_url"] == "https://custom.test"
    assert state["automatic_url"] == "https://deployed.test"
    assert owner.save_console_address("")["effective_url"] == "https://deployed.test"
    monkeypatch.delenv("CENTER_PUBLIC_HTTP_BASE_URL")
    assert owner.console_address_status()["effective_url"] == "http://another.test:4183"
    assert owner.console_address_status()["source"] == "browser"
    restored = CenterConnection(owner.directory, Runtime)
    assert restored.console_address_status()["effective_url"] == "http://192.168.1.20:8002"
    assert restored.console_address_status()["source"] == "launcher"
    other = CenterConnection(tmp_path / "another-station", Runtime)
    assert other.console_address_status()["source"] == "launcher"
    assert other.console_address_status()["effective_url"] == "http://192.168.1.20:8002"
    monkeypatch.delenv(LAUNCHER_CONSOLE_URL_ENV)
    assert restored.console_address_status()["effective_url"] == "http://another.test:4183"


def test_restarted_launcher_uses_new_port_instead_of_stale_browser_candidate(tmp_path, monkeypatch):
    monkeypatch.setenv(LAUNCHER_CONSOLE_URL_ENV, "http://192.168.1.20:4183")
    first = CenterConnection(tmp_path / "station", Runtime)
    first.save_console_address(None, "http://192.168.1.20:4183")
    monkeypatch.setenv(LAUNCHER_CONSOLE_URL_ENV, "http://192.168.1.20:4184")
    restarted = CenterConnection(first.directory, Runtime)
    assert restarted.console_address_status()["effective_url"] == "http://192.168.1.20:4184"
    restarted.save_console_address("https://team.test")
    assert CenterConnection(first.directory, Runtime).console_address_status()["effective_url"] == "https://team.test"


def test_invalid_automatic_source_does_not_override_custom_address(tmp_path, monkeypatch):
    monkeypatch.setenv("CENTER_PUBLIC_HTTP_BASE_URL", "https://bad.test/path")
    owner = CenterConnection(tmp_path / "station", Runtime)
    assert owner.save_console_address("https://valid.test")["effective_url"] == "https://valid.test"
    with pytest.raises(ValueError):
        owner.save_console_address(None)
    assert owner.console_address_status()["effective_url"] == "https://valid.test"


def test_update_offline_restart_and_renewal_keep_identity_and_backend(tmp_path):
    center = Center()
    owner = CenterConnection(tmp_path / "station", Runtime, request=center, clock=lambda: center.now.timestamp())
    owner.connect("https://center.test", "a" * 64, http_base_url="http://internal:49881",
                  console_address={"custom_url": None, "browser_url": "http://page.test:4183"})
    owner.step()
    runtime, session = owner.runtime, owner.runtime.session
    key, station = runtime.config.client_key, owner.status()["station_uuid"]
    center.offline = True
    state = owner.save_console_address("https://custom.test")
    assert state["sync_state"] == "pending"
    with pytest.raises(ConnectionError):
        owner.step()
    assert runtime.config.public_http_base_url == "https://custom.test"
    assert owner.runtime is runtime and runtime.session is session
    assert runtime.config.client_key == key and runtime.config.http_base_url == "http://internal:49881"
    center.offline = False
    center.now += timedelta(days=24)
    owner.step()
    assert runtime.config.client_key != key
    assert runtime.config.public_http_base_url == "https://custom.test"
    restored = CenterConnection(owner.directory, Runtime, request=center, clock=lambda: center.now.timestamp())
    restored.step()
    assert restored.status()["station_uuid"] == station
    assert restored.runtime.config.public_http_base_url == "https://custom.test"
    assert [path for path, _ in center.requests].count("/api/v1/stations/pair") == 1
    # HTTP 可达不代表公开地址已被 hello_ack 确认。
    assert restored.status()["state"] == "connected"
    assert restored.console_address_status()["sync_state"] == "pending"


@pytest.mark.parametrize("address", ["https://page.test/yibin", "https://u:p@page.test", "ftp://page.test",
                                   "http://0.0.0.0:8002", "http://[::]:8002", "http://page.test?x=1",
                                   "http://page.test#x", "http://page.test:0", "http://page.test:99999"])
def test_invalid_update_keeps_saved_settings(tmp_path, address):
    owner = CenterConnection(tmp_path / "station", Runtime)
    before = owner.save_console_address("https://valid.test")
    persisted = (owner.directory / "console-address.json").read_bytes()
    with pytest.raises(ValueError):
        owner.save_console_address(address, "http://candidate.test")
    assert owner.console_address_status() == before
    assert (owner.directory / "console-address.json").read_bytes() == persisted


def test_failed_write_keeps_memory_and_persisted_settings(tmp_path, monkeypatch):
    owner = CenterConnection(tmp_path / "station", Runtime)
    before = owner.save_console_address("https://valid.test")
    def failed(*_):
        raise OSError("disk full")
    monkeypatch.setattr(owner, "_write", failed)
    with pytest.raises(OSError):
        owner.save_console_address("https://other.test")
    assert owner.console_address_status() == before
    assert CenterConnection(owner.directory, Runtime).console_address_status() == before


def test_corrupt_console_settings_do_not_break_station_identity_or_backend(tmp_path):
    center = Center()
    owner = CenterConnection(tmp_path / "station", Runtime, request=center)
    owner.connect("https://center.test", "a" * 64, http_base_url="http://internal:49881")
    owner.step()
    settings = owner.directory / "console-address.json"
    settings.write_text("broken json")
    restored = CenterConnection(owner.directory, Runtime, request=center)
    restored.step()
    assert restored.status()["state"] == "connected"
    assert restored.status()["station_uuid"] == center.station
    assert restored.status()["http_base_url"] == "http://internal:49881"
    assert restored.console_address_status()["sync_state"] == "error"
    with pytest.raises(ConnectionError, match="LOCAL_CONSOLE_ADDRESS_UNREADABLE"):
        restored.save_console_address("https://page.test")
    assert settings.read_text() == "broken json"


def test_console_api_saves_without_pairing_and_requires_explicit_clear(tmp_path):
    owner = CenterConnection(tmp_path / "station", Runtime)
    app = FastAPI()
    app.include_router(create_router(lambda: owner))
    api = TestClient(app)
    path = "/api/v1/center-connection/console-address"
    payload = {"custom_url": "https://page.test", "browser_url": "http://local.test:4183"}
    assert api.post(path, json=payload).status_code == 403
    headers = {"X-Unilab-Local-Request": "1"}
    assert api.post(path, json=payload, headers={**headers, "Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert api.post(path, json=payload, headers=headers).json()["data"]["source"] == "custom"
    assert api.post(path, json={"browser_url": "http://other.test"}, headers=headers).status_code == 422
    assert api.get(path).json()["data"]["effective_url"] == "https://page.test"
    assert api.post(path, json={"custom_url": "https://bad.test/path"}, headers=headers).status_code == 422
    assert api.post(path, json={"custom_url": None}, headers=headers).json()["data"]["effective_url"] == "http://local.test:4183"
    assert owner.status()["state"] == "unconfigured" and owner.runtime is None


def test_missing_address_does_not_guess_a_port(tmp_path):
    owner = CenterConnection(tmp_path / "station", Runtime)
    assert owner.console_address_status()["effective_url"] is None
    assert owner.console_address_status()["source"] == "none"


def test_legacy_runtime_accepts_address_change_without_enrollment(tmp_path):
    center = Center()
    original = CenterConnection(tmp_path / "original", Runtime, request=center)
    original.connect("https://center.test", "a" * 64)
    original.step()
    owner = CenterConnection(tmp_path / "legacy", Runtime, runtime=original.runtime, request=center)
    owner.save_console_address("https://legacy-page.test")
    owner.step()
    assert owner.runtime is original.runtime
    assert owner.runtime.config.public_http_base_url == "https://legacy-page.test"
    assert len(center.requests) == 1


@pytest.mark.parametrize("address", [None, "https://console.test"])
def test_address_sync_requires_valid_hello_ack(tmp_path, address):
    _, client, store, receiver = setup_receiver(tmp_path)
    receiver.config = replace(receiver.config, public_http_base_url=address)
    try:
        assert not receiver.public_address_synced(address)
        with pytest.raises(EOFError):
            asyncio.run(receiver._exchange(Connection([]), client.session))
        assert not receiver.public_address_synced(address)
        with pytest.raises(EOFError):
            asyncio.run(receiver._exchange(Connection([hello(client.session)]), client.session))
        assert receiver.public_address_synced(address)
        assert not receiver.public_address_synced("https://changed.test")
    finally:
        store.close()


def test_launcher_uses_actual_port_and_does_not_guess_between_interfaces(monkeypatch):
    assert detect_console_origin("192.168.1.20", 4183) == "http://192.168.1.20:4183"
    assert detect_console_origin("::1", 4184) == "http://[::1]:4184"
    import socket
    addresses = ["192.168.1.20", "127.0.0.1"]
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_: [(socket.AF_INET, socket.SOCK_DGRAM, 0, "", (ip, 0)) for ip in addresses])
    assert detect_console_origin("0.0.0.0", 4183) == "http://192.168.1.20:4183"
    addresses.append("10.10.0.2")
    assert detect_console_origin("0.0.0.0", 4183) is None


def test_old_configuration_is_automatic():
    assert ConsoleAddressSettings.from_record({}).resolve(None, None)["mode"] == "auto"
