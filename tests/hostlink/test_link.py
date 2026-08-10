import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from unilabos.hostlink.client import HostLinkClient
from unilabos.hostlink.protocol import ActionType
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


def test_overlapping_device_set_keeps_identity_when_assignment_changes() -> None:
    server = HostLinkServer("127.0.0.1", 0).start()
    first = HostLinkClient(
        "127.0.0.1",
        server.port,
        device_ids=["pump-1", "sensor-1"],
    )
    changed = HostLinkClient(
        "127.0.0.1",
        server.port,
        device_ids=["heater-1", "sensor-1"],
    )
    try:
        assert first.connect_blocking(timeout=2)
        original_node_id = server.peers()[0]["node_id"]
        first.close()
        assert changed.connect_blocking(timeout=2)
        assert _wait_until(lambda: len(server.peers()) == 1)
        peer = server.peers()[0]
        assert peer["node_id"] == original_node_id
        assert peer["device_ids"] == ["heater-1", "sensor-1"]
        assert peer["online"] is True
    finally:
        first.close()
        changed.close()
        server.stop()


def test_slow_request_does_not_block_ping_on_the_same_connection() -> None:
    server = HostLinkServer(
        "127.0.0.1",
        0,
        heartbeat_timeout=1,
        request_timeout=1,
    ).start()
    entered = threading.Event()
    release = threading.Event()

    def slow(_data, _peer):
        entered.set()
        assert release.wait(timeout=2)
        return {"done": True}

    server.register_handler("test.slow", slow)
    client = HostLinkClient(
        "127.0.0.1",
        server.port,
        heartbeat_interval=10,
        request_timeout=1,
    )
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        assert client.connect_blocking(timeout=2)
        future = executor.submit(client.request, "test.slow")
        assert entered.wait(timeout=1)
        assert client.request(ActionType.PING, timeout=0.5)["pong"] is True
        release.set()
        assert future.result(timeout=1) == {"done": True}
    finally:
        release.set()
        executor.shutdown(wait=False, cancel_futures=True)
        client.close()
        server.stop()


def test_async_requests_work_in_both_directions() -> None:
    server = HostLinkServer(
        "127.0.0.1",
        0,
        heartbeat_timeout=1,
        request_timeout=1,
    ).start()
    server.register_handler(
        "test.host_echo",
        lambda data, _peer: {"host": data["value"]},
    )
    client = HostLinkClient(
        "127.0.0.1",
        server.port,
        device_ids=["async-device"],
        heartbeat_interval=10,
        request_timeout=1,
    )
    client.register_handler(
        "test.slave_echo",
        lambda data: {"slave": data["value"]},
    )
    try:
        assert client.connect_blocking(timeout=2)

        async def scenario() -> tuple[dict, dict]:
            return await asyncio.gather(
                client.request_async(
                    "test.host_echo",
                    {"value": "to-host"},
                ),
                server.request_device_async(
                    "async-device",
                    "test.slave_echo",
                    {"value": "to-slave"},
                ),
            )

        host_result, slave_result = asyncio.run(scenario())
        assert host_result == {"host": "to-host"}
        assert slave_result == {"slave": "to-slave"}
    finally:
        client.close()
        server.stop()
