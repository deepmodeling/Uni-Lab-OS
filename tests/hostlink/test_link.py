import time

from unilabos.hostlink.client import HostLinkClient
from unilabos.hostlink.ros_assist import RosNetworkInfo
from unilabos.hostlink.server import HostLinkServer


def _wait_until(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def test_hello_discovers_slave_devices_and_returns_ros_policy() -> None:
    server = HostLinkServer("127.0.0.1", 0, heartbeat_timeout=1).start()
    server.hello_payload = {
        "host_id": "host-a",
        "ros": RosNetworkInfo(
            domain_id=42,
            automatic_discovery_range="OFF",
            static_peers=["127.0.0.1"],
        ).to_dict(),
    }
    client = HostLinkClient(
        "127.0.0.1",
        server.port,
        machine_name="slave-a",
        device_ids=["pump-2", "pump-1", "pump-1"],
        heartbeat_interval=0.05,
        request_timeout=0.5,
    )
    try:
        assert client.connect_blocking(timeout=2)
        assert client.hello_info["host_id"] == "host-a"
        assert client.hello_ros_info().domain_id == 42
        assert client.ros_info().static_peers == ["127.0.0.1"]
        assert _wait_until(lambda: server.has_device("pump-1"))
        assert server.devices()["pump-2"]["machine_name"] == "slave-a"
        peer = server.peers()[0]
        assert peer["node_id"] == "device:pump-1"
        assert peer["device_ids"] == ["pump-1", "pump-2"]
        assert peer["online"] is True
    finally:
        client.close()
        server.stop()


def test_same_device_set_keeps_logical_identity_after_reconnect() -> None:
    server = HostLinkServer("127.0.0.1", 0).start()
    first = HostLinkClient("127.0.0.1", server.port, device_ids=["robot-1"])
    second = HostLinkClient("127.0.0.1", server.port, device_ids=["robot-1"])
    try:
        assert first.connect_blocking(timeout=2)
        first.close()
        assert second.connect_blocking(timeout=2)
        assert _wait_until(lambda: len(server.peers()) == 1)
        assert server.peers()[0]["node_id"] == "device:robot-1"
        assert server.peers()[0]["online"] is True
    finally:
        first.close()
        second.close()
        server.stop()
