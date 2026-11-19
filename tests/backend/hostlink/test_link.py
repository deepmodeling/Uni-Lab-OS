import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from unilabos.backend.hostlink.client import HostLinkClient
from unilabos.backend.hostlink.protocol import ActionType, RemoteError
from unilabos.backend.hostlink.ros_assist import RosNetworkInfo
from unilabos.backend.hostlink.server import HostLinkPortInUseError, HostLinkServer


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
        assert peer["node_id"] == "slave-a"
        assert peer["device_ids"] == ["pump-1", "pump-2"]
        assert peer["online"] is True
    finally:
        client.close()
        server.stop()


def test_same_device_set_keeps_logical_identity_after_reconnect() -> None:
    server = HostLinkServer("127.0.0.1", 0).start()
    first = HostLinkClient("127.0.0.1", server.port, machine_name="robot-worker", device_ids=["robot-1"])
    second = HostLinkClient("127.0.0.1", server.port, machine_name="robot-worker", device_ids=["robot-1"])
    try:
        assert first.connect_blocking(timeout=2)
        first.close()
        assert _wait_until(lambda: not server.peers()[0]["connected"])
        assert second.connect_blocking(timeout=2)
        assert _wait_until(lambda: len(server.peers()) == 1)
        assert server.peers()[0]["node_id"] == "robot-worker"
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
        machine_name="worker",
    )
    changed = HostLinkClient(
        "127.0.0.1",
        server.port,
        device_ids=["heater-1", "sensor-1"],
        machine_name="worker",
    )
    try:
        assert first.connect_blocking(timeout=2)
        original_node_id = server.peers()[0]["node_id"]
        first.close()
        assert _wait_until(lambda: not server.peers()[0]["connected"])
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


def test_second_server_on_same_port_fails_loudly() -> None:
    """同一端口不能同时被两个 Host 监听。

    Windows 上 SO_REUSEADDR 允许与仍在 LISTEN 的进程共绑同一端口，新连接落到哪个进程
    不确定——上次没退干净的 Host 会把 Slave 全部接走，新 Host 什么都看不到、也不报错。
    现在第二个 start() 必须抛出带可操作提示的 HostLinkPortInUseError。
    """

    first = HostLinkServer("127.0.0.1", 0).start()
    second = HostLinkServer("127.0.0.1", first.port)
    try:
        with pytest.raises(HostLinkPortInUseError) as excinfo:
            second.start()
        assert str(first.port) in excinfo.value.strerror
        assert "--hostlink_port" in excinfo.value.strerror
        # 首个服务不受影响，Slave 仍接到它
        client = HostLinkClient("127.0.0.1", first.port, device_ids=["pump-1"])
        try:
            assert client.connect_blocking(timeout=2)
            assert _wait_until(lambda: first.has_device("pump-1"))
        finally:
            client.close()
    finally:
        second.stop()
        first.stop()


def test_large_messages_are_chunked_and_oversized_ones_fail_fast(monkeypatch) -> None:
    """超过单帧上限的请求/应答走分片照常成功；连分片都装不下的立刻报明确错误。

    回归：Host 把整套物料模板定义（近 10MB > 8MB 单帧）回给 Slave 时 encode_frame 抛
    LinkError，只在执行线程里被吞掉，Slave 的 material.template.list 等满 10s 才以
    request timeout 失败，两侧日志都没有原因。Host → Slave 与 Slave → Host 两个方向同样处理。
    """

    from unilabos.backend.hostlink import protocol

    monkeypatch.setattr(protocol, "MAX_FRAME_BYTES", 4096)
    monkeypatch.setattr(protocol, "MAX_MESSAGE_BYTES", 64 * 1024)
    server = HostLinkServer("127.0.0.1", 0, request_timeout=5)
    big = "中文 \"x\" \\ 😀 " * 2000  # 远超单帧，但在整条消息上限之内
    huge = "x" * (128 * 1024)
    server.register_handler("test.echo", lambda data, _peer: {"blob": data["blob"]})
    server.register_handler("test.huge", lambda _data, _peer: {"blob": huge})
    server.start()
    client = HostLinkClient(
        "127.0.0.1", server.port, device_ids=["dev-1"], heartbeat_interval=10, request_timeout=5
    )
    client.register_handler("test.echo", lambda data: {"blob": data["blob"]})
    client.register_handler("test.huge", lambda _data: {"blob": huge})
    client.register_handler("test.small", lambda _data: {"ok": 1})
    try:
        assert client.connect_blocking(timeout=2)
        assert _wait_until(lambda: server.has_device("dev-1"))
        # 双向大请求 + 大应答：分片透明
        assert client.request("test.echo", {"blob": big}, timeout=5) == {"blob": big}
        assert server.request_device("dev-1", "test.echo", {"blob": big}, timeout=5) == {"blob": big}
        # Slave → Host：Host 的应答超过整条消息上限
        started = time.monotonic()
        with pytest.raises(RemoteError, match="message limit"):
            client.request("test.huge", {}, timeout=5)
        assert time.monotonic() - started < 3, "应立刻收到错误而不是等到超时"
        # Host → Slave：Slave 的应答超限
        with pytest.raises(RemoteError, match="message limit"):
            server.request_device("dev-1", "test.huge", {}, timeout=5)
        # 连接仍然健康，后续小应答照常
        assert server.request_device("dev-1", "test.small", {}, timeout=5) == {"ok": 1}
    finally:
        client.close()
        server.stop()


def test_negative_request_timeout_allows_scheduler_managed_long_action() -> None:
    server = HostLinkServer(
        "127.0.0.1",
        0,
        heartbeat_timeout=1,
        request_timeout=0.02,
    ).start()
    client = HostLinkClient(
        "127.0.0.1",
        server.port,
        device_ids=["long-action-device"],
        heartbeat_interval=10,
        request_timeout=0.02,
    )

    def long_action(_data):
        time.sleep(0.06)
        return {"done": True}

    client.register_handler("test.long_action", long_action)
    try:
        assert client.connect_blocking(timeout=2)
        assert server.request_device(
            "long-action-device",
            "test.long_action",
            {},
            timeout=-1,
        ) == {"done": True}
    finally:
        client.close()
        server.stop()
