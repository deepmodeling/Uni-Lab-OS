"""ROS2 HostLink networking must be owned by the Edge microbackend."""

from __future__ import annotations

import asyncio
import os
import socket
import threading
import time

import pytest

from unilabos.server.scheduler.host_network import (
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
        self,
        uuid: str,
        resource_id: str,
        resource_type: str = "resource",
    ) -> None:
        self.uuid = uuid
        self.id = resource_id
        self.type = resource_type

    def model_dump(self, by_alias: bool = True) -> dict[str, object]:
        del by_alias
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
    def __init__(self, host_node_id: str = "host_node") -> None:
        plate = _Node("u-plate", "plate_1")
        self.trees = [
            _Tree(
                _Node(
                    "u-host",
                    host_node_id,
                    [plate],
                    resource_type="device",
                )
            )
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
    monkeypatch.setattr(HostLinkConfig, "heartbeat_interval", 0.05)
    monkeypatch.setattr(HostLinkConfig, "heartbeat_timeout", 1.0)
    monkeypatch.setattr(HostLinkConfig, "connect_timeout", 0.5)
    monkeypatch.setattr(HostLinkConfig, "request_timeout", 1.0)
    monkeypatch.setattr(HostLinkConfig, "ros_assist_apply", True)
    monkeypatch.setattr(HostLinkConfig, "ros_domain_id", "73")
    monkeypatch.setattr(HostLinkConfig, "ros_discovery_range", "OFF")
    monkeypatch.setattr(HostLinkConfig, "ros_static_peers", "")
    # Managed Fast DDS lifecycle is tested with a fake process below.
    monkeypatch.setattr(HostLinkConfig, "ros_discovery_server", "off")
    monkeypatch.setattr(HostLinkConfig, "ros_discovery_port", 0)
    monkeypatch.setattr(BasicConfig, "backend", "ros2")
    monkeypatch.setattr(BasicConfig, "machine_name", "edge-host-test")
    monkeypatch.setattr(BasicConfig, "host_node_name", "host_node")
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


def test_host_microbackend_owns_listener_material_and_ros(
    tmp_path, monkeypatch
) -> None:
    from fastapi.testclient import TestClient

    from unilabos.resources import materials
    from unilabos.resources.container import RegularContainer
    from unilabos.server.scheduler.api import create_app
    from unilabos.server.clients.materials import (
        HostLinkMaterialsClient,
        LocalMaterialsClient,
    )
    from unilabos.server.services.materials import MaterialsService

    monkeypatch.setattr(BasicConfig, "host_node_name", "west_lab")
    material_service = MaterialsService(tmp_path / "materials.db")
    gateway = LocalMaterialsClient(material_service)
    beaker = RegularContainer(
        name="authority-beaker",
        size_x=10,
        size_y=10,
        size_z=20,
        max_volume=100,
    )
    beaker.unilabos_extra = {
        "unilabos_resource_class": "authority-beaker"
    }
    created = materials.create(beaker, gateway=gateway)
    material_uuid = created.result.data.root_material_uuid
    service = setup_host_network_service(material_gateway=gateway)
    assert service is not None
    assert get_host_network_service() is service
    assert get_hostlink_server() is service.server
    assert setup_host_network_service(material_gateway=gateway) is service

    client = HostLinkClient(
        "127.0.0.1",
        service.server.port,
        machine_name="slave-a",
        heartbeat_interval=0.05,
        connect_timeout=0.5,
        request_timeout=1.0,
    )
    try:
        assert client.connect_blocking(timeout=2.0)
        assert client.hello_info["owner"] == SERVICE_OWNER
        assert client.hello_info["host_node_id"] == "west_lab"
        assert client.hello_ros_info().domain_id == 73

        tree = HostLinkMaterialsClient(client).get_tree(material_uuid)
        assert tree.root_material_uuid == material_uuid
        assert [node.material.resource_id for node in tree.nodes] == [
            "authority-beaker"
        ]

        ros_response = client.request(ActionType.ROS_INFO)
        assert ros_response["owner"] == SERVICE_OWNER
        assert ros_response["ros"]["domain_id"] == 73

        status = TestClient(create_app()).get("/api/v1/hostlink/peers").json()
        assert status["role"] == "host"
        assert status["owner"] == SERVICE_OWNER
        assert status["host_id"] == "edge-host-test"
        assert status["host_node_id"] == "west_lab"
        assert status["ros"]["domain_id"] == 73
        assert status["peers"][0]["node_id"] == "slave-a"
    finally:
        client.close()
        material_service.close()


def test_slave_microbackend_applies_host_ros_config_before_ros_init() -> None:
    service = setup_host_network_service()
    assert service is not None
    HostLinkConfig.host = "127.0.0.1"
    HostLinkConfig.port = service.server.port

    client, domain_id = setup_slave_network_client(
        device_ids=["sensor-b", "pump-a"]
    )
    assert client is not None and client.online
    assert client.node_id == "device:pump-a"
    assert client.device_ids == ["pump-a", "sensor-b"]
    assert get_hostlink_client() is client
    assert domain_id == 73
    assert os.environ["ROS_DOMAIN_ID"] == "73"
    assert os.environ["ROS_AUTOMATIC_DISCOVERY_RANGE"] == "OFF"
    assert os.environ["ROS_STATIC_PEERS"] == "127.0.0.1"
    assert service.server.peers()[0]["device_ids"] == ["pump-a", "sensor-b"]

    same_client, same_domain = setup_slave_network_client()
    assert same_client is client
    assert same_domain == domain_id


def test_slave_material_create_is_proxied_by_host_authority(
    tmp_path, monkeypatch
) -> None:
    from unilabos.device_runtime.resource import AuthorityResourceService
    from unilabos.resources.container import RegularContainer
    from unilabos.server.clients.materials import LocalMaterialsClient
    from unilabos.server.services.materials import MaterialsService

    material_service = MaterialsService(tmp_path / "materials.db")
    service = setup_host_network_service(
        material_gateway=LocalMaterialsClient(material_service)
    )
    assert service is not None
    HostLinkConfig.host = "127.0.0.1"
    HostLinkConfig.port = service.server.port
    monkeypatch.setattr(BasicConfig, "is_host_mode", False)
    client, _ = setup_slave_network_client(device_ids=["liquid-handler-1"])
    assert client is not None and client.online

    beaker = RegularContainer(
        name="custom-beaker-1",
        size_x=10,
        size_y=10,
        size_z=20,
        max_volume=100,
    )
    beaker.unilabos_extra = {"unilabos_resource_class": "custom-beaker"}
    try:
        resource_service = AuthorityResourceService()
        created = asyncio.run(
            resource_service.create_resources(
                "liquid-handler-1",
                "device-uuid",
                beaker,
            )
        )

        assert not getattr(beaker, "unilabos_uuid", "")
        authoritative = created.resources[0]
        assert authoritative.unilabos_uuid
        assert (
            created.result.data.nodes[0].material.template_name
            == "custom-beaker"
        )
        template = material_service.list_templates()[0]
        assert template.name == "custom-beaker"
        assert (
            created.result.data.nodes[0].material.template_uuid
            == template.template_uuid
        )

        authoritative.tracker.set_liquids([("water", 20.0, "ul")])
        updated = asyncio.run(
            resource_service.update_resources(
                "liquid-handler-1",
                "device-uuid",
                authoritative,
            )
        )
        assert updated.all_nodes_uuid == [authoritative.unilabos_uuid]
        downloaded = asyncio.run(
            resource_service.get_resources(
                "liquid-handler-1",
                [authoritative.unilabos_uuid],
                with_children=True,
            )
        )
        assert downloaded.all_nodes_uuid == [authoritative.unilabos_uuid]
        downloaded_by_id = asyncio.run(
            resource_service.get_resource_by_id(
                "liquid-handler-1",
                "custom-beaker-1",
                with_children=True,
            )
        )
        assert downloaded_by_id.all_nodes_uuid == [authoritative.unilabos_uuid]
        assert [
            (item.name, item.quantity, item.quantity_unit)
            for item in material_service.get_material(
                authoritative.unilabos_uuid
            ).data.substances
        ] == [("water", 20.0, "ul")]
        deleted = asyncio.run(
            resource_service.delete_resources(
                "liquid-handler-1",
                "device-uuid",
                [authoritative.unilabos_uuid],
            )
        )
        assert deleted == [authoritative.unilabos_uuid]
        assert material_service.list_materials() == []
    finally:
        material_service.close()


def test_startup_device_ids_requires_business_device_identity() -> None:
    config = _TreeSet()
    config.trees[0].root.children.append(
        _Node("u-balance", "balance_1", resource_type="device")
    )
    assert startup_device_ids(config) == ["host_node", "balance_1"]

    empty = type("EmptyDeviceConfig", (), {"all_nodes": []})()
    assert startup_device_ids(empty) == []
    with pytest.raises(ValueError, match="至少包含一个 type=device"):
        require_slave_startup_device_ids(empty)


def test_normal_slave_waits_until_delayed_host_is_ready() -> None:
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        delayed_port = reservation.getsockname()[1]

    HostLinkConfig.host = "127.0.0.1"
    HostLinkConfig.port = delayed_port
    result: dict[str, object] = {}

    thread = threading.Thread(
        target=lambda: result.setdefault(
            "value",
            setup_slave_network_client(),
        ),
        daemon=True,
    )
    thread.start()
    time.sleep(0.2)
    assert thread.is_alive()

    service = setup_host_network_service()
    assert service is not None
    thread.join(timeout=4)
    assert not thread.is_alive()
    client, domain_id = result["value"]
    assert isinstance(client, HostLinkClient)
    assert client.online
    assert domain_id == 73


def test_slave_no_host_starts_offline_and_keeps_reconnecting() -> None:
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
    assert not client.online
    assert client._manager_thread is not None
    assert client._manager_thread.is_alive()
    assert "ROS_DOMAIN_ID" not in os.environ


def test_required_host_wait_can_be_stopped_cleanly() -> None:
    with socket.socket() as unavailable:
        unavailable.bind(("127.0.0.1", 0))
        HostLinkConfig.host = "127.0.0.1"
        HostLinkConfig.port = unavailable.getsockname()[1]
        result: dict[str, object] = {}
        thread = threading.Thread(
            target=lambda: result.setdefault(
                "value",
                setup_slave_network_client(),
            ),
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


def test_microbackend_shutdown_releases_hostlink_port() -> None:
    service = setup_host_network_service()
    assert service is not None
    port = service.server.port

    shutdown_network_services()
    assert get_host_network_service() is None
    assert get_hostlink_server() is None
    assert get_hostlink_client() is None

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", port))


def test_managed_discovery_lifecycle_belongs_to_microbackend(monkeypatch) -> None:
    from unilabos.hostlink.ros_assist import FastDDSDiscoveryServer

    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        link_port = reservation.getsockname()[1]

    starts: list[tuple[str, int]] = []
    stops: list[int] = []
    monkeypatch.setattr(
        FastDDSDiscoveryServer,
        "start",
        lambda self: (starts.append((self.bind, self.port)), self)[1],
    )
    monkeypatch.setattr(
        FastDDSDiscoveryServer,
        "stop",
        lambda self: stops.append(self.port),
    )
    monkeypatch.setattr(HostLinkConfig, "port", link_port)
    monkeypatch.setattr(HostLinkConfig, "ros_discovery_server", "")
    monkeypatch.setattr(HostLinkConfig, "ros_discovery_port", 0)

    service = setup_host_network_service()
    assert service is not None
    endpoint = f"127.0.0.1:{link_port}"
    assert starts == [("127.0.0.1", link_port)]
    assert service.ros_info.discovery_server == endpoint
    assert os.environ["ROS_DISCOVERY_SERVER"] == endpoint
    assert os.environ["ROS_SUPER_CLIENT"] == "TRUE"

    shutdown_network_services()
    assert stops == [link_port]
