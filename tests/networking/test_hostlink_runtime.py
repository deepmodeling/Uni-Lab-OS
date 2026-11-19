import os
from dataclasses import dataclass

import pytest

from unilabos.config.config import BasicConfig, HostLinkConfig
from unilabos.backend.hostlink.runtime import (
    setup_hostlink_client,
    setup_hostlink_server,
    shutdown_hostlink,
    startup_device_ids,
)


@pytest.fixture(autouse=True)
def isolated_hostlink_config(monkeypatch):
    shutdown_hostlink()
    monkeypatch.setattr(HostLinkConfig, "enable", True)
    monkeypatch.setattr(HostLinkConfig, "host", "")
    monkeypatch.setattr(HostLinkConfig, "port", 0)
    monkeypatch.setattr(HostLinkConfig, "bind", "127.0.0.1")
    monkeypatch.setattr(HostLinkConfig, "advertise_ip", "127.0.0.1")
    monkeypatch.setattr(HostLinkConfig, "heartbeat_interval", 0.05)
    monkeypatch.setattr(HostLinkConfig, "heartbeat_timeout", 1.0)
    monkeypatch.setattr(HostLinkConfig, "connect_timeout", 0.5)
    monkeypatch.setattr(HostLinkConfig, "request_timeout", 0.5)
    monkeypatch.setattr(HostLinkConfig, "ros_assist_apply", True)
    monkeypatch.setattr(HostLinkConfig, "ros_domain_id", "61")
    monkeypatch.setattr(HostLinkConfig, "ros_discovery_range", "OFF")
    monkeypatch.setattr(HostLinkConfig, "ros_static_peers", "")
    monkeypatch.setattr(HostLinkConfig, "ros_discovery_server", "off")
    monkeypatch.setattr(BasicConfig, "machine_name", "test-machine")
    monkeypatch.setattr(BasicConfig, "slave_no_host", False)
    monkeypatch.delenv("ROS_DOMAIN_ID", raising=False)
    monkeypatch.delenv("ROS_STATIC_PEERS", raising=False)
    monkeypatch.delenv("ROS_AUTOMATIC_DISCOVERY_RANGE", raising=False)
    yield
    shutdown_hostlink()


def test_runtime_syncs_domain_and_discovers_devices(monkeypatch) -> None:
    server = setup_hostlink_server()
    assert server is not None
    monkeypatch.setattr(HostLinkConfig, "host", "127.0.0.1")
    monkeypatch.setattr(HostLinkConfig, "port", server.port)

    client, domain_id = setup_hostlink_client(["centrifuge-1"], wait_for_host=True)

    assert client is not None and client.online
    assert domain_id == 61
    assert os.environ["ROS_DOMAIN_ID"] == "61"
    assert os.environ["ROS_AUTOMATIC_DISCOVERY_RANGE"] == "OFF"
    assert "127.0.0.1" in os.environ["ROS_STATIC_PEERS"].split(";")
    assert server.has_device("centrifuge-1")


@dataclass
class _Content:
    id: str
    type: str


@dataclass
class _Node:
    res_content: _Content


class _Tree:
    all_nodes = [
        _Node(_Content("pump-1", "device")),
        _Node(_Content("plate-1", "resource")),
        _Node(_Content("pump-2", "device")),
    ]


def test_startup_device_ids_only_reports_devices() -> None:
    assert startup_device_ids(_Tree()) == ["pump-1", "pump-2"]
