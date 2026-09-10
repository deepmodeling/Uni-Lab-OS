"""Edge 微后端对 HostLink、Slave 生命周期与 ROS 下发的所有权回归。"""

from __future__ import annotations

import os
import socket
import threading
import time

import pytest

from unilabos.app.scheduler.host_network import (
    SERVICE_OWNER,
    get_host_network_service,
    require_slave_startup_device_ids,
    setup_host_network_service,
    setup_slave_network_client,
    shutdown_network_services,
    startup_device_ids,
)
from unilabos.config.config import BasicConfig, HostLinkConfig
from unilabos.hostlink.client import HostLinkClient, get_hostlink_client
from unilabos.hostlink.protocol import ActionType
from unilabos.hostlink.server import get_hostlink_server


class _Content:
    def __init__(
        self, uuid: str, resource_id: str, resource_type: str = "resource"
    ) -> None:
        self.uuid = uuid
        self.id = resource_id
        self.type = resource_type

    def model_dump(self, by_alias: bool = True) -> dict[str, object]:
        return {
            "uuid": self.uuid,
            "id": self.id,
            "name": self.id,
            "type": self.type,
        }


class _Node:
    def __init__(
        self,
        uuid: str,
        resource_id: str,
        children=None,
        resource_type: str = "resource",
    ) -> None:
        self.res_content = _Content(uuid, resource_id, resource_type)
        self.children = children or []

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()


class _Tree:
    def __init__(self, root: _Node) -> None:
        self.root = root

    def get_all_nodes(self):
        return list(self.root.walk())


class _TreeSet:
    def __init__(self) -> None:
        plate = _Node("u-plate", "plate_1")
        self.trees = [
            _Tree(_Node("u-host", "host_node", [plate], resource_type="device"))
        ]

    @property
    def root_nodes(self):
        return [tree.root for tree in self.trees]

    @property
    def all_nodes(self):
        return [node for tree in self.trees for node in tree.get_all_nodes()]


@pytest.fixture(autouse=True)
def isolated_network(monkeypatch):
    shutdown_network_services()
    monkeypatch.setattr(HostLinkConfig, "enable", True)
    monkeypatch.setattr(HostLinkConfig, "bind", "127.0.0.1")
    monkeypatch.setattr(HostLinkConfig, "port", 0)
    monkeypatch.setattr(HostLinkConfig, "host", "")
    monkeypatch.setattr(HostLinkConfig, "advertise_ip", "127.0.0.1")
    monkeypatch.setattr(HostLinkConfig, "heartbeat_interval", 0.1)
    monkeypatch.setattr(HostLinkConfig, "heartbeat_timeout", 2.0)
    monkeypatch.setattr(HostLinkConfig, "connect_timeout", 1.0)
    monkeypatch.setattr(HostLinkConfig, "request_timeout", 2.0)
    monkeypatch.setattr(HostLinkConfig, "ros_assist_apply", True)
    monkeypatch.setattr(HostLinkConfig, "ros_domain_id", "73")
    monkeypatch.setattr(HostLinkConfig, "ros_discovery_range", "OFF")
    monkeypatch.setattr(HostLinkConfig, "ros_static_peers", "")
    # Most unit tests exercise HostLink ownership without spawning an external
    # Fast DDS process.  The managed-server lifecycle has a dedicated test.
    monkeypatch.setattr(HostLinkConfig, "ros_discovery_server", "off")
    monkeypatch.setattr(HostLinkConfig, "ros_discovery_port", 0)
    monkeypatch.setattr(BasicConfig, "machine_name", "edge-host-test")
    monkeypatch.setattr(BasicConfig, "is_host_mode", True)
    monkeypatch.setattr(BasicConfig, "slave_no_host", False)
    for key in (
        "ROS_DOMAIN_ID",
        "ROS_AUTOMATIC_DISCOVERY_RANGE",
        "ROS_STATIC_PEERS",
        "ROS_DISCOVERY_SERVER",
        "ROS_SUPER_CLIENT",
    ):
        monkeypatch.delenv(key, raising=False)
    yield
    shutdown_network_services()


def test_host_microbackend_owns_listener_material_and_ros(monkeypatch):
    from fastapi.testclient import TestClient

    from unilabos.app.scheduler.api import create_app
    from unilabos.app.web.client import http_client

    material_calls: list[dict[str, object]] = []

    def empty_material_source(**kwargs):
        material_calls.append(kwargs)
        return []

    monkeypatch.setattr(http_client, "material_query", empty_material_source)
    tree = _TreeSet()
    service = setup_host_network_service(lambda: tree)
    assert service is not None
    assert get_host_network_service() is service
    assert get_hostlink_server() is service.server
    assert setup_host_network_service(lambda: tree) is service

    client = HostLinkClient(
        "127.0.0.1",
        service.server.port,
        machine_name="slave-a",
        heartbeat_interval=0.1,
        connect_timeout=1.0,
        request_timeout=2.0,
    )
    try:
        assert client.connect_blocking(timeout=3.0)
        assert client.hello_info["owner"] == SERVICE_OWNER
        assert client.hello_ros_info().domain_id == 73
        assert client.hello_ros_info().automatic_discovery_range == "OFF"

        # HTTP 物料组件未命中时，微后端再转到 HostNode 挂接的运行时树。
        nodes = client.get_resource(res_id="host_node", with_children=True)
        assert [node["id"] for node in nodes] == ["host_node", "plate_1"]
        assert material_calls == [
            {
                "uuids": None,
                "resource_id": "host_node",
                "with_children": True,
            }
        ]

        ros_response = client.request(ActionType.ROS_INFO)
        assert ros_response["owner"] == SERVICE_OWNER
        assert ros_response["ros"]["domain_id"] == 73

        status = TestClient(create_app()).get("/api/v1/hostlink/peers").json()
        assert status["role"] == "host"
        assert status["owner"] == SERVICE_OWNER
        assert status["host_id"] == "edge-host-test"
        assert status["protocol_version"] == 1
        assert status["ros"]["domain_id"] == 73
        assert status["peers"][0]["node_id"] == "slave-a"
        assert status["peers"][0]["machine_name"] == "slave-a"
    finally:
        client.close()


def test_slave_microbackend_applies_host_ros_config_before_ros_init():
    service = setup_host_network_service(lambda: _TreeSet())
    assert service is not None
    HostLinkConfig.host = "127.0.0.1"
    HostLinkConfig.port = service.server.port

    client, domain_id = setup_slave_network_client(device_ids=["sensor-b", "pump-a"])
    assert client is not None
    assert client.online is True
    assert client.node_id == "device:pump-a"
    assert client.device_ids == ["pump-a", "sensor-b"]
    assert get_hostlink_client() is client
    assert domain_id == 73
    assert os.environ["ROS_DOMAIN_ID"] == "73"
    assert os.environ["ROS_AUTOMATIC_DISCOVERY_RANGE"] == "OFF"
    assert os.environ["ROS_STATIC_PEERS"] == "127.0.0.1"
    assert service.server.peers()[0]["device_ids"] == ["pump-a", "sensor-b"]

    # main() 与 ROS slave() 都可调用，服务装配必须保持单例。
    same_client, same_domain = setup_slave_network_client()
    assert same_client is client
    assert same_domain == domain_id


def test_startup_device_ids_uses_every_reported_device_node():
    config = _TreeSet()
    config.trees[0].root.children.append(
        _Node("u-balance", "balance_1", resource_type="device")
    )
    assert startup_device_ids(config) == ["host_node", "balance_1"]


def test_empty_slave_graph_is_rejected_but_extractor_remains_compatible():
    empty = type("EmptyDeviceConfig", (), {"all_nodes": []})()
    assert startup_device_ids(empty) == []
    with pytest.raises(ValueError, match="至少包含一个 type=device"):
        require_slave_startup_device_ids(empty)


def test_normal_slave_waits_until_delayed_host_is_ready():
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        delayed_port = reservation.getsockname()[1]

    HostLinkConfig.host = "127.0.0.1"
    HostLinkConfig.port = delayed_port
    result: dict[str, object] = {}

    def connect_slave() -> None:
        result["value"] = setup_slave_network_client()

    thread = threading.Thread(target=connect_slave, daemon=True)
    thread.start()
    time.sleep(0.3)
    assert thread.is_alive(), "normal Slave must wait instead of starting ROS locally"

    service = setup_host_network_service(lambda: _TreeSet())
    assert service is not None
    thread.join(timeout=5)
    assert not thread.is_alive()
    client, domain_id = result["value"]
    assert isinstance(client, HostLinkClient)
    assert client.online is True
    assert domain_id == 73


def test_slave_no_host_starts_offline_and_keeps_background_reconnect():
    BasicConfig.slave_no_host = True
    with socket.socket() as unavailable:
        unavailable.bind(("127.0.0.1", 0))
        HostLinkConfig.host = "127.0.0.1"
        HostLinkConfig.port = unavailable.getsockname()[1]

        started_at = time.monotonic()
        client, domain_id = setup_slave_network_client()
        elapsed = time.monotonic() - started_at

    assert client is not None
    assert elapsed < 0.5
    assert domain_id is None
    assert client.online is False
    assert (
        client._manager_thread is not None
    )  # background Host reconnect remains active
    assert client._manager_thread.is_alive()
    assert "ROS_DOMAIN_ID" not in os.environ


def test_required_host_wait_can_be_stopped_cleanly():
    with socket.socket() as unavailable:
        unavailable.bind(("127.0.0.1", 0))
        HostLinkConfig.host = "127.0.0.1"
        HostLinkConfig.port = unavailable.getsockname()[1]
        result: dict[str, object] = {}

        thread = threading.Thread(
            target=lambda: result.setdefault("value", setup_slave_network_client()),
            daemon=True,
        )
        thread.start()
        time.sleep(0.2)
        assert thread.is_alive()
        shutdown_network_services()

    thread.join(timeout=3)
    assert not thread.is_alive()
    client, domain_id = result["value"]
    assert isinstance(client, HostLinkClient)
    assert domain_id is None


def test_edge_shutdown_releases_hostlink_port():
    from unilabos.app.scheduler.integration import shutdown_edge_services

    service = setup_host_network_service(lambda: _TreeSet())
    assert service is not None
    port = service.server.port

    shutdown_edge_services()
    assert get_host_network_service() is None
    assert get_hostlink_server() is None
    assert get_hostlink_client() is None

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", port))


def test_host_managed_discovery_reuses_explicit_hostlink_port(monkeypatch):
    from unilabos.hostlink.ros_assist import FastDDSDiscoveryServer

    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        link_port = reservation.getsockname()[1]

    starts: list[tuple[str, int]] = []
    stops: list[int] = []

    def fake_start(self):
        starts.append((self.bind, self.port))
        return self

    def fake_stop(self):
        stops.append(self.port)

    monkeypatch.setattr(FastDDSDiscoveryServer, "start", fake_start)
    monkeypatch.setattr(FastDDSDiscoveryServer, "stop", fake_stop)
    monkeypatch.setattr(HostLinkConfig, "port", link_port)
    monkeypatch.setattr(HostLinkConfig, "ros_discovery_server", "")
    monkeypatch.setattr(HostLinkConfig, "ros_discovery_port", 0)

    service = setup_host_network_service(lambda: _TreeSet())
    assert service is not None
    endpoint = f"127.0.0.1:{link_port}"
    assert starts == [("127.0.0.1", link_port)]
    assert service.server.port == link_port
    assert service.ros_info.discovery_server == endpoint
    assert service.ros_info.discovery_server_managed is True
    assert os.environ["ROS_DISCOVERY_SERVER"] == endpoint
    assert os.environ["ROS_SUPER_CLIENT"] == "TRUE"

    client = HostLinkClient(
        "127.0.0.1",
        link_port,
        machine_name="directed-slave",
        connect_timeout=1.0,
        request_timeout=2.0,
    )
    try:
        assert client.connect_blocking(timeout=3.0)
        hello_ros = client.hello_ros_info()
        assert hello_ros.discovery_server == endpoint
        assert hello_ros.discovery_server_managed is True
    finally:
        client.close()
        shutdown_network_services()
    assert stops == [link_port]
