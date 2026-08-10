from __future__ import annotations

import asyncio
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from unilabos.basic.runtime import BasicDriverSpec, BasicRuntime
from unilabos.config.config import BasicConfig, HostLinkConfig
from unilabos.device_runtime import ActionCancelled, ActionContext
from unilabos.hostlink.backend import HostLinkBackendRuntime
from unilabos.hostlink.client import HostLinkClient
from unilabos.hostlink.protocol import ActionType, RemoteError
from unilabos.hostlink.server import HostLinkServer
from unilabos.resources.resource_tracker import ResourceTreeSet


def _wait_until(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


class CounterDriver:
    def __init__(self, device_id=None, config=None):
        self.device_id = device_id
        self.count = int((config or {}).get("initial", 0))
        self.node = None

    def post_init(self, node) -> None:
        self.node = node

    def increment(self, amount: int = 1) -> int:
        self.count += amount
        return self.count

    def call_peer(self, target_device: str, amount: int) -> int:
        return self.node.call_device_action(
            target_device,
            "increment",
            {"amount": amount},
        )

    def call_peer_with_action_type(self, target_device: str, amount: int) -> int:
        return self.node.call_device_action(
            target_device,
            "increment",
            {"amount": amount},
            action_type=object,
        )


class TopicDriver:
    def __init__(self, device_id=None, config=None):
        self.device_id = device_id
        self.config = dict(config or {})
        self.node = None
        self.publisher = None
        self.received = []

    def post_init(self, node) -> None:
        self.node = node
        if self.config.get("publish"):
            self.publisher = node.create_publisher(dict, "value", 10)
        subscribe_to = str(self.config.get("subscribe_to") or "")
        if subscribe_to:
            node.create_subscription(
                dict,
                f"/devices/{subscribe_to}/value",
                self.received.append,
                10,
            )

    def send(self, value: int) -> int:
        self.publisher.publish({"value": value})
        return value


class FeedbackDriver:
    def __init__(self, device_id=None, config=None):
        self.device_id = device_id
        self.node = None
        self.progress = 0

    def post_init(self, node) -> None:
        self.node = node

    async def run_steps(self, steps: int, action_context: ActionContext) -> int:
        for step in range(steps):
            action_context.raise_if_cancelled()
            self.progress = step + 1
            action_context.publish_feedback({"progress": self.progress})
            await self.node.sleep(0.02)
        action_context.raise_if_cancelled()
        return self.progress


def _counter_runtime(
    device_id: str,
    initial: int = 0,
    resource_uuid: str = "",
) -> BasicRuntime:
    runtime = BasicRuntime()
    runtime.add_driver(
        BasicDriverSpec(
            device_id=device_id,
            driver_class=CounterDriver,
            config={"initial": initial},
            registry_name="counter",
            display_name="Counter",
            action_names=(
                "increment",
                "call_peer",
                "call_peer_with_action_type",
            ),
            status_names=("count",),
            resource_uuid=resource_uuid,
        )
    )
    return runtime


def _feedback_runtime(device_id: str) -> BasicRuntime:
    runtime = BasicRuntime(backend_name="hostlink")
    runtime.add_driver(
        BasicDriverSpec(
            device_id=device_id,
            driver_class=FeedbackDriver,
            config={},
            registry_name="feedback",
            display_name="Feedback",
            action_names=("run_steps",),
            status_names=("progress",),
        )
    )
    return runtime


def test_server_calls_slave_over_the_existing_control_connection() -> None:
    server = HostLinkServer(
        "127.0.0.1",
        0,
        heartbeat_timeout=1,
        request_timeout=0.5,
    ).start()
    state = {"count": 0}
    client = HostLinkClient(
        "127.0.0.1",
        server.port,
        device_descriptors=[
            {
                "id": "counter-1",
                "registry_name": "counter",
                "display_name": "Counter",
                "actions": ["increment"],
                "status_fields": ["count"],
            }
        ],
        heartbeat_interval=0.05,
        request_timeout=0.5,
        heartbeat_payload_provider=lambda: {"states": {"counter-1": dict(state)}},
    )

    def call(data):
        state["count"] += int(data["arguments"]["amount"])
        return {"result": state["count"], "state": dict(state)}

    client.register_handler(ActionType.DEVICE_CALL, call)
    try:
        assert client.connect_blocking(timeout=2)
        response = server.call_device(
            "counter-1",
            "increment",
            {"amount": 3},
        )
        assert response["result"] == 3
        assert server.devices()["counter-1"]["device"]["actions"] == ["increment"]
        assert _wait_until(
            lambda: server.devices()["counter-1"]["state"].get("count") == 3
        )
    finally:
        client.close()
        server.stop()


def test_hostlink_backend_routes_basic_driver_actions_without_ros(
    monkeypatch,
) -> None:
    monkeypatch.setattr(HostLinkConfig, "enable", True)
    monkeypatch.setattr(HostLinkConfig, "bind", "127.0.0.1")
    monkeypatch.setattr(HostLinkConfig, "host", "127.0.0.1")
    monkeypatch.setattr(HostLinkConfig, "port", 0)
    monkeypatch.setattr(HostLinkConfig, "heartbeat_interval", 0.05)
    monkeypatch.setattr(HostLinkConfig, "heartbeat_timeout", 1.0)
    monkeypatch.setattr(HostLinkConfig, "connect_timeout", 1.0)
    monkeypatch.setattr(HostLinkConfig, "request_timeout", 0.5)
    monkeypatch.setattr(BasicConfig, "machine_name", "slave-test")
    monkeypatch.setattr(BasicConfig, "slave_no_host", False)

    host = HostLinkBackendRuntime(_counter_runtime("host-local"), is_slave=False)
    slave = HostLinkBackendRuntime(
        _counter_runtime("slave-counter", initial=1),
        is_slave=True,
    )
    host.start()
    assert host.server is not None
    HostLinkConfig.port = host.server.port
    try:
        slave.start()
        assert host.call_action("host-local", "increment", amount=2) == 2
        assert host.call_action("slave-counter", "increment", amount=4) == 5
        assert (
            host.call_action(
                "slave-counter",
                "call_peer",
                target_device="host-local",
                amount=3,
            )
            == 5
        )
        assert (
            host.call_action(
                "slave-counter",
                "call_peer_with_action_type",
                target_device="host-local",
                amount=2,
            )
            == 7
        )
        assert _wait_until(
            lambda: host.devices()["slave-counter"]["state"].get("count") == 5
        )
        assert host.devices()["slave-counter"]["location"] == "remote"
        assert host.devices()["host-local"]["location"] == "local"
        with pytest.raises(RemoteError, match="没有动作"):
            host.server.call_device("slave-counter", "missing")
    finally:
        slave.stop()
        host.stop()


def test_hostlink_routes_topics_in_both_directions(monkeypatch) -> None:
    monkeypatch.setattr(HostLinkConfig, "enable", True)
    monkeypatch.setattr(HostLinkConfig, "bind", "127.0.0.1")
    monkeypatch.setattr(HostLinkConfig, "host", "127.0.0.1")
    monkeypatch.setattr(HostLinkConfig, "port", 0)
    monkeypatch.setattr(HostLinkConfig, "heartbeat_interval", 0.05)
    monkeypatch.setattr(HostLinkConfig, "heartbeat_timeout", 1.0)
    monkeypatch.setattr(HostLinkConfig, "connect_timeout", 1.0)
    monkeypatch.setattr(HostLinkConfig, "request_timeout", 1.0)
    monkeypatch.setattr(BasicConfig, "machine_name", "topic-slave")
    monkeypatch.setattr(BasicConfig, "slave_no_host", False)

    host_local = BasicRuntime("hostlink")
    host_source = host_local.add_driver(
        BasicDriverSpec(
            device_id="host-source",
            driver_class=TopicDriver,
            config={"publish": True},
            action_names=("send",),
        )
    )
    host_sink = host_local.add_driver(
        BasicDriverSpec(
            device_id="host-sink",
            driver_class=TopicDriver,
            config={"subscribe_to": "slave-source"},
        )
    )
    slave_local = BasicRuntime("hostlink")
    slave_source = slave_local.add_driver(
        BasicDriverSpec(
            device_id="slave-source",
            driver_class=TopicDriver,
            config={"publish": True},
            action_names=("send",),
        )
    )
    slave_sink = slave_local.add_driver(
        BasicDriverSpec(
            device_id="slave-sink",
            driver_class=TopicDriver,
            config={"subscribe_to": "host-source"},
        )
    )
    host = HostLinkBackendRuntime(host_local, is_slave=False)
    slave = HostLinkBackendRuntime(slave_local, is_slave=True)
    host.start()
    assert host.server is not None
    HostLinkConfig.port = host.server.port
    try:
        slave.start()
        assert _wait_until(
            lambda: any(
                "/devices/host-source/value" in topics
                for topics in host._remote_topic_subscriptions.values()
            )
        )

        host_source.call_action("send", value=11)
        assert _wait_until(
            lambda: slave_sink.driver.received == [{"value": 11}]
        )

        slave_source.call_action("send", value=22)
        assert _wait_until(
            lambda: host_sink.driver.received == [{"value": 22}]
        )

        assert slave.client is not None
        with host._remote_topic_lock:
            host._remote_topic_subscriptions.clear()
        slave.client._teardown_socket()
        assert _wait_until(lambda: not slave.client.online)
        assert _wait_until(lambda: slave.client.online, timeout=4)
        assert _wait_until(
            lambda: any(
                "/devices/host-source/value" in topics
                for topics in host._remote_topic_subscriptions.values()
            )
        )
        host_source.call_action("send", value=33)
        assert _wait_until(
            lambda: slave_sink.driver.received
            == [{"value": 11}, {"value": 33}]
        )
    finally:
        slave.stop()
        host.stop()


def test_hostlink_backend_import_does_not_load_ros() -> None:
    code = (
        "import sys; import unilabos.hostlink.main_hostlink_run; "
        "assert 'rclpy' not in sys.modules; "
        "assert not any(name.startswith('unilabos.ros') for name in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr


def test_hostlink_action_feedback_and_cancel(monkeypatch) -> None:
    monkeypatch.setattr(HostLinkConfig, "enable", True)
    monkeypatch.setattr(HostLinkConfig, "bind", "127.0.0.1")
    monkeypatch.setattr(HostLinkConfig, "host", "127.0.0.1")
    monkeypatch.setattr(HostLinkConfig, "port", 0)
    monkeypatch.setattr(HostLinkConfig, "heartbeat_interval", 0.05)
    monkeypatch.setattr(HostLinkConfig, "heartbeat_timeout", 1.0)
    monkeypatch.setattr(HostLinkConfig, "connect_timeout", 1.0)
    monkeypatch.setattr(HostLinkConfig, "request_timeout", 2.0)
    monkeypatch.setattr(BasicConfig, "machine_name", "feedback-slave")
    monkeypatch.setattr(BasicConfig, "slave_no_host", False)

    host = HostLinkBackendRuntime(BasicRuntime("hostlink"), is_slave=False)
    slave = HostLinkBackendRuntime(
        _feedback_runtime("feedback-device"),
        is_slave=True,
    )
    host.start()
    assert host.server is not None
    HostLinkConfig.port = host.server.port
    feedback_received = threading.Event()
    feedback = []
    context = ActionContext(
        action_id="cancel-me",
        feedback_callback=lambda _action_id, data: (
            feedback.append(data),
            feedback_received.set(),
        ),
    )
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        slave.start()
        future = executor.submit(
            host.call_action,
            "feedback-device",
            "run_steps",
            action_context=context,
            steps=100,
        )
        assert feedback_received.wait(timeout=2)
        assert host.cancel_action(context.action_id) is True
        with pytest.raises(ActionCancelled, match="cancel-me"):
            future.result(timeout=2)
        assert feedback
        assert feedback[0]["progress"] >= 1
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
        slave.stop()
        host.stop()


def test_hostlink_routes_action_feedback_and_cancel_between_slaves(
    monkeypatch,
) -> None:
    monkeypatch.setattr(HostLinkConfig, "enable", True)
    monkeypatch.setattr(HostLinkConfig, "bind", "127.0.0.1")
    monkeypatch.setattr(HostLinkConfig, "host", "127.0.0.1")
    monkeypatch.setattr(HostLinkConfig, "port", 0)
    monkeypatch.setattr(HostLinkConfig, "heartbeat_interval", 0.05)
    monkeypatch.setattr(HostLinkConfig, "heartbeat_timeout", 1.0)
    monkeypatch.setattr(HostLinkConfig, "connect_timeout", 1.0)
    monkeypatch.setattr(HostLinkConfig, "request_timeout", 2.0)
    monkeypatch.setattr(BasicConfig, "slave_no_host", False)

    host = HostLinkBackendRuntime(BasicRuntime("hostlink"), is_slave=False)
    caller = HostLinkBackendRuntime(
        _counter_runtime("slave-caller"),
        is_slave=True,
    )
    target = HostLinkBackendRuntime(
        _feedback_runtime("slave-target"),
        is_slave=True,
    )
    host.start()
    assert host.server is not None
    HostLinkConfig.port = host.server.port
    feedback_received = threading.Event()
    feedback = []
    context = ActionContext(
        action_id="slave-to-slave-cancel",
        feedback_callback=lambda _action_id, data: (
            feedback.append(data),
            feedback_received.set(),
        ),
    )
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        BasicConfig.machine_name = "caller-slave"
        caller.start()
        BasicConfig.machine_name = "target-slave"
        target.start()
        assert _wait_until(lambda: "slave-target" in host.devices())

        future = executor.submit(
            caller.route_action,
            "slave-caller",
            "slave-target",
            "run_steps",
            {"steps": 100},
            action_context=context,
        )
        assert feedback_received.wait(timeout=2)
        assert caller.cancel_action(context.action_id) is True
        with pytest.raises(
            ActionCancelled,
            match="slave-to-slave-cancel",
        ):
            future.result(timeout=2)
        assert feedback
        assert feedback[0]["progress"] >= 1
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
        target.stop()
        caller.stop()
        host.stop()


def _resource(
    resource_id: str,
    resource_uuid: str,
    *,
    parent_uuid: str | None = None,
    resource_type: str = "container",
) -> dict:
    return {
        "id": resource_id,
        "uuid": resource_uuid,
        "name": resource_id,
        "parent_uuid": parent_uuid,
        "type": resource_type,
        "class": "",
        "config": {},
        "data": {},
        "extra": {},
    }


def test_hostlink_syncs_and_serves_slave_resources(monkeypatch) -> None:
    monkeypatch.setattr(HostLinkConfig, "enable", True)
    monkeypatch.setattr(HostLinkConfig, "bind", "127.0.0.1")
    monkeypatch.setattr(HostLinkConfig, "host", "127.0.0.1")
    monkeypatch.setattr(HostLinkConfig, "port", 0)
    monkeypatch.setattr(HostLinkConfig, "heartbeat_interval", 0.05)
    monkeypatch.setattr(HostLinkConfig, "heartbeat_timeout", 1.0)
    monkeypatch.setattr(HostLinkConfig, "connect_timeout", 1.0)
    monkeypatch.setattr(HostLinkConfig, "request_timeout", 2.0)
    monkeypatch.setattr(BasicConfig, "machine_name", "resource-slave")
    monkeypatch.setattr(BasicConfig, "slave_no_host", False)

    slave_resources = ResourceTreeSet.from_raw_dict_list(
        [
            _resource(
                "resource-device",
                "device-uuid",
                resource_type="device",
            ),
            _resource(
                "existing-material",
                "existing-uuid",
                parent_uuid="device-uuid",
            ),
        ]
    )
    host = HostLinkBackendRuntime(BasicRuntime("hostlink"), is_slave=False)
    slave = HostLinkBackendRuntime(
        _counter_runtime(
            "resource-device",
            resource_uuid="device-uuid",
        ),
        is_slave=True,
        resources_config=slave_resources,
    )
    host.start()
    assert host.server is not None
    HostLinkConfig.port = host.server.port
    try:
        slave.start()
        assert host.resource_store.resources.find_by_uuid("existing-uuid")

        new_material = ResourceTreeSet.from_raw_dict_list(
            [_resource("new-material", "new-uuid")]
        )
        slave_node = slave.local.devices["resource-device"]
        asyncio.run(slave_node.update_resource(new_material))
        queried = asyncio.run(slave_node.get_resource(["new-uuid"]))

        assert queried.all_nodes_uuid == ["new-uuid"]
        stored = host.resource_store.resources.find_by_uuid("new-uuid")
        assert stored is not None
        assert stored.res_content.parent_uuid == "device-uuid"
    finally:
        slave.stop()
        host.stop()
