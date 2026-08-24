from __future__ import annotations

import asyncio
from array import array
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from unilabos.server.database.repositories.materials import MaterialsRepository
from unilabos.hostlink.local_runtime import HostLinkDriverSpec, HostLinkLocalRuntime
from unilabos.config.config import BasicConfig, HostLinkConfig
from unilabos.device_runtime import (
    ActionCancelled,
    ActionContext,
)
from unilabos.hostlink.backend import HostLinkBackend
from unilabos.hostlink.client import HostLinkClient
from unilabos.hostlink.protocol import ActionType, RemoteError
from unilabos.hostlink.server import HostLinkServer


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

    async def increment_async(self, amount: int = 1) -> int:
        await self.node.sleep(0)
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

    async def call_peer_async(self, target_device: str, amount: int) -> int:
        return await self.node.call_device_action_async(
            target_device,
            "increment_async",
            {"amount": amount},
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


class RosJsonMessage:
    def __init__(self, name: str, samples: list[float]) -> None:
        self.name = name
        self.samples = array("f", samples)

    @staticmethod
    def get_fields_and_field_types() -> dict[str, str]:
        return {"name": "string", "samples": "sequence<float>"}


class RosJsonDriver:
    def __init__(self, device_id=None, config=None):
        self.device_id = device_id
        self.config = dict(config or {})
        self.node = None
        self.publisher = None
        self.received = []

    def post_init(self, node) -> None:
        self.node = node
        if self.config.get("publish"):
            self.publisher = node.create_publisher(
                RosJsonMessage,
                "ros_value",
                10,
            )
        subscribe_to = str(self.config.get("subscribe_to") or "")
        if subscribe_to:
            node.create_subscription(
                RosJsonMessage,
                f"/devices/{subscribe_to}/ros_value",
                self.received.append,
                10,
            )

    def send(self, name: str) -> str:
        self.publisher.publish(RosJsonMessage(name, [1.25, 2.5]))
        return name

    def echo(self, payload) -> object:
        return payload


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


class AddService:
    class Request:
        def __init__(self, left=0, right=0):
            self.left = left
            self.right = right

    class Response:
        def __init__(self):
            self.total = 0


class ServiceDriver:
    def __init__(self, device_id=None, config=None):
        self.device_id = device_id
        self.provide = bool((config or {}).get("provide"))

    def post_init(self, node) -> None:
        self.node = node
        if self.provide:
            node.create_service(AddService, "add", self.add)

    @staticmethod
    def add(request, response):
        response.total = request.left + request.right
        return response

    async def call_service(self, target_device: str, left: int, right: int) -> int:
        client = self.node.create_client(
            AddService,
            f"/devices/{target_device}/add",
        )
        response = await client.call_async(AddService.Request(left, right))
        return response.total


def _counter_runtime(
    device_id: str,
    initial: int = 0,
    resource_uuid: str = "",
) -> HostLinkLocalRuntime:
    runtime = HostLinkLocalRuntime()
    runtime.add_driver(
        HostLinkDriverSpec(
            device_id=device_id,
            driver_class=CounterDriver,
            config={"initial": initial},
            registry_name="counter",
            display_name="Counter",
            action_names=(
                "increment",
                "increment_async",
                "call_peer",
                "call_peer_async",
                "call_peer_with_action_type",
            ),
            action_value_mappings={
                "increment": {
                    "type": "unilabos_msgs/action/IntSingleInput",
                    "goal": {"amount": "value"},
                    "result": {"return_info": "return_info"},
                    "schema": {
                        "type": "object",
                        "properties": {"amount": {"type": "integer"}},
                    },
                }
            },
            status_names=("count",),
            resource_uuid=resource_uuid,
        )
    )
    return runtime


def _feedback_runtime(device_id: str) -> HostLinkLocalRuntime:
    runtime = HostLinkLocalRuntime()
    runtime.add_driver(
        HostLinkDriverSpec(
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

    host = HostLinkBackend(_counter_runtime("host-local"), is_slave=False)
    slave = HostLinkBackend(
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
        with pytest.raises(RemoteError, match="没有动作") as exc_info:
            host.server.call_device("slave-counter", "missing")
        assert exc_info.value.error_info["exception_type"] == "AttributeError"
        assert "AttributeError" in exc_info.value.error_info["exception_mro"]
    finally:
        slave.stop()
        host.stop()


def test_dynamic_subdevices_refresh_hostlink_routes(monkeypatch) -> None:
    monkeypatch.setattr(HostLinkConfig, "enable", True)
    monkeypatch.setattr(HostLinkConfig, "bind", "127.0.0.1")
    monkeypatch.setattr(HostLinkConfig, "host", "127.0.0.1")
    monkeypatch.setattr(HostLinkConfig, "port", 0)
    monkeypatch.setattr(HostLinkConfig, "heartbeat_interval", 0.05)
    monkeypatch.setattr(HostLinkConfig, "heartbeat_timeout", 1.0)
    monkeypatch.setattr(HostLinkConfig, "connect_timeout", 1.0)
    monkeypatch.setattr(HostLinkConfig, "request_timeout", 0.5)
    monkeypatch.setattr(BasicConfig, "machine_name", "dynamic-slave")
    monkeypatch.setattr(BasicConfig, "slave_no_host", False)

    host = HostLinkBackend(_counter_runtime("host-owner"), is_slave=False)
    slave = HostLinkBackend(
        _counter_runtime("slave-owner"),
        is_slave=True,
    )
    host.start()
    assert host.server is not None
    HostLinkConfig.port = host.server.port
    try:
        slave.start()
        slave.local.add_driver(
            HostLinkDriverSpec(
                "slave-child",
                CounterDriver,
                {"initial": 2},
                action_names=("increment",),
                status_names=("count",),
            )
        )
        assert _wait_until(lambda: "slave-child" in host.devices())
        assert host.call_action("slave-child", "increment", amount=3) == 5

        host.local.add_driver(
            HostLinkDriverSpec(
                "host-child",
                CounterDriver,
                {"initial": 4},
                action_names=("increment",),
                status_names=("count",),
            )
        )
        assert _wait_until(
            lambda: any(
                item.get("id") == "host-child"
                for item in (slave.client.hello_info.get("devices") or [])
            )
        )
        assert (
            slave.local.call_action(
                "slave-owner",
                "call_peer",
                target_device="host-child",
                amount=2,
            )
            == 6
        )

        assert slave.local.remove_device("slave-child") is True
        assert _wait_until(lambda: "slave-child" not in host.devices())
    finally:
        slave.stop()
        host.stop()


def test_hostlink_backend_proxies_material_create_without_template_uuid(
    tmp_path, monkeypatch
) -> None:
    from uuid import uuid4

    from unilabos.client.materials import (
        HostLinkMaterialsClient,
        LocalMaterialsClient,
    )
    from unilabos.server.protocol.common import InventoryMutation
    from unilabos.server.protocol.materials import (
        MaterialDelete,
        MaterialIdentityWrite,
        MaterialNodeCreate,
        MaterialTreeCreate,
    )
    from unilabos.server.scheduler.integration import set_materials_gateway
    from unilabos.server.services.materials import MaterialsService

    monkeypatch.setattr(HostLinkConfig, "enable", True)
    monkeypatch.setattr(HostLinkConfig, "bind", "127.0.0.1")
    monkeypatch.setattr(HostLinkConfig, "port", 0)
    monkeypatch.setattr(HostLinkConfig, "heartbeat_timeout", 1.0)
    monkeypatch.setattr(HostLinkConfig, "request_timeout", 1.0)

    material_service = MaterialsService(MaterialsRepository(tmp_path / "materials.db"))
    set_materials_gateway(LocalMaterialsClient(material_service))
    host = HostLinkBackend(HostLinkLocalRuntime(), is_slave=False)
    host.start()
    assert host.server is not None
    client = HostLinkClient(
        "127.0.0.1",
        host.server.port,
        heartbeat_interval=0.05,
        request_timeout=1.0,
    )
    try:
        assert client.connect_blocking(timeout=2)
        request = MaterialTreeCreate(
            nodes=[
                MaterialNodeCreate(
                    client_ref="root",
                    identity=MaterialIdentityWrite(
                        resource_id="custom-container-1",
                        name="custom-container-1",
                        resource_type="container",
                        class_name="Container",
                        template_name="custom-container",
                    ),
                )
            ]
        )
        gateway = HostLinkMaterialsClient(client)
        result = gateway.create_tree(
            InventoryMutation(
                command_uuid=str(uuid4()),
                effect_key="create_material_tree",
                operation="create_material_tree",
            ),
            request,
        )

        assert result.data.root_material_uuid
        assert result.data.nodes[0].material.template_uuid
        assert material_service.list_templates()[0].name == "custom-container"
        aggregate = gateway.get_material_by_resource_id("custom-container-1")
        assert aggregate.material.material_uuid == result.data.root_material_uuid
        deleted = gateway.delete_material(
            InventoryMutation(
                command_uuid=str(uuid4()),
                effect_key="delete_material_tree",
                operation="delete_material",
            ),
            MaterialDelete(
                material_uuid=result.data.root_material_uuid,
                recursive=True,
            ),
        )
        assert deleted.data.deleted_material_uuids == [
            result.data.root_material_uuid
        ]
        assert material_service.list_materials() == []
    finally:
        client.close()
        host.stop()
        set_materials_gateway(None)
        material_service.repository.close()


def test_hostlink_awaits_device_tools_without_thread_fallback(
    monkeypatch,
) -> None:
    monkeypatch.setattr(HostLinkConfig, "enable", True)
    monkeypatch.setattr(HostLinkConfig, "bind", "127.0.0.1")
    monkeypatch.setattr(HostLinkConfig, "host", "127.0.0.1")
    monkeypatch.setattr(HostLinkConfig, "port", 0)
    monkeypatch.setattr(HostLinkConfig, "heartbeat_interval", 0.05)
    monkeypatch.setattr(HostLinkConfig, "heartbeat_timeout", 1.0)
    monkeypatch.setattr(HostLinkConfig, "connect_timeout", 1.0)
    monkeypatch.setattr(HostLinkConfig, "request_timeout", 1.0)
    monkeypatch.setattr(BasicConfig, "machine_name", "async-slave")
    monkeypatch.setattr(BasicConfig, "slave_no_host", False)

    host = HostLinkBackend(_counter_runtime("host-target"), is_slave=False)
    slave = HostLinkBackend(
        _counter_runtime("slave-target", initial=1),
        is_slave=True,
    )
    host.start()
    assert host.server is not None
    HostLinkConfig.port = host.server.port
    try:
        slave.start()

        async def reject_thread_fallback(*_args, **_kwargs):
            raise AssertionError("异步设备调用不应退回 asyncio.to_thread")

        monkeypatch.setattr(asyncio, "to_thread", reject_thread_fallback)

        async def scenario() -> tuple[int, int]:
            host_to_slave = await host.call_action_async(
                "slave-target",
                "increment_async",
                amount=4,
            )
            slave_to_host = await slave.local.call_action_async(
                "slave-target",
                "call_peer_async",
                target_device="host-target",
                amount=3,
            )
            return host_to_slave, slave_to_host

        assert asyncio.run(scenario()) == (5, 3)
        assert _wait_until(
            lambda: host.devices()["slave-target"]["state"].get("count") == 5
        )
        discovered = host.devices()["slave-target"]
        assert "increment_async" in discovered["device"]["actions"]
        assert discovered["device"]["status_fields"] == ["count"]
        assert discovered["device"]["action_value_mappings"]["increment"]["goal"] == {
            "amount": "value"
        }
        assert discovered["online"] is True
    finally:
        slave.stop()
        host.stop()


def test_hostlink_routes_ros_shaped_services_in_both_directions(
    monkeypatch,
) -> None:
    monkeypatch.setattr(HostLinkConfig, "enable", True)
    monkeypatch.setattr(HostLinkConfig, "bind", "127.0.0.1")
    monkeypatch.setattr(HostLinkConfig, "host", "127.0.0.1")
    monkeypatch.setattr(HostLinkConfig, "port", 0)
    monkeypatch.setattr(HostLinkConfig, "heartbeat_interval", 0.05)
    monkeypatch.setattr(HostLinkConfig, "heartbeat_timeout", 1.0)
    monkeypatch.setattr(HostLinkConfig, "connect_timeout", 1.0)
    monkeypatch.setattr(HostLinkConfig, "request_timeout", 1.0)
    monkeypatch.setattr(BasicConfig, "machine_name", "service-slave")
    monkeypatch.setattr(BasicConfig, "slave_no_host", False)

    host_local = HostLinkLocalRuntime()
    host_local.add_driver(
        HostLinkDriverSpec("host-service", ServiceDriver, {"provide": True})
    )
    host_local.add_driver(
        HostLinkDriverSpec(
            "host-caller",
            ServiceDriver,
            {},
            action_names=("call_service",),
        )
    )
    slave_local = HostLinkLocalRuntime()
    slave_local.add_driver(
        HostLinkDriverSpec("slave-service", ServiceDriver, {"provide": True})
    )
    slave_local.add_driver(
        HostLinkDriverSpec(
            "slave-caller",
            ServiceDriver,
            {},
            action_names=("call_service",),
        )
    )
    host = HostLinkBackend(host_local, is_slave=False)
    slave = HostLinkBackend(slave_local, is_slave=True)
    host.start()
    assert host.server is not None
    HostLinkConfig.port = host.server.port
    try:
        slave.start()
        assert _wait_until(lambda: "slave-service" in host.devices())
        assert host.has_service("/devices/slave-service/add")
        assert host.devices()["slave-service"]["device"]["services"] == [
            "/devices/slave-service/add"
        ]
        assert (
            host.call_action(
                "host-caller",
                "call_service",
                target_device="slave-service",
                left=4,
                right=5,
            )
            == 9
        )
        assert (
            host.call_action(
                "slave-caller",
                "call_service",
                target_device="host-service",
                left=7,
                right=8,
            )
            == 15
        )
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

    host_local = HostLinkLocalRuntime()
    host_source = host_local.add_driver(
        HostLinkDriverSpec(
            device_id="host-source",
            driver_class=TopicDriver,
            config={"publish": True},
            action_names=("send",),
        )
    )
    host_sink = host_local.add_driver(
        HostLinkDriverSpec(
            device_id="host-sink",
            driver_class=TopicDriver,
            config={"subscribe_to": "slave-source"},
        )
    )
    slave_local = HostLinkLocalRuntime()
    slave_source = slave_local.add_driver(
        HostLinkDriverSpec(
            device_id="slave-source",
            driver_class=TopicDriver,
            config={"publish": True},
            action_names=("send",),
        )
    )
    slave_sink = slave_local.add_driver(
        HostLinkDriverSpec(
            device_id="slave-sink",
            driver_class=TopicDriver,
            config={"subscribe_to": "host-source"},
        )
    )
    host = HostLinkBackend(host_local, is_slave=False)
    slave = HostLinkBackend(slave_local, is_slave=True)
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
        assert _wait_until(lambda: slave_sink.driver.received == [{"value": 11}])

        slave_source.call_action("send", value=22)
        assert _wait_until(lambda: host_sink.driver.received == [{"value": 22}])

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
            lambda: slave_sink.driver.received == [{"value": 11}, {"value": 33}]
        )
    finally:
        slave.stop()
        host.stop()


def test_hostlink_transports_ros_messages_as_json(monkeypatch) -> None:
    monkeypatch.setattr(HostLinkConfig, "enable", True)
    monkeypatch.setattr(HostLinkConfig, "bind", "127.0.0.1")
    monkeypatch.setattr(HostLinkConfig, "host", "127.0.0.1")
    monkeypatch.setattr(HostLinkConfig, "port", 0)
    monkeypatch.setattr(HostLinkConfig, "heartbeat_interval", 0.05)
    monkeypatch.setattr(HostLinkConfig, "heartbeat_timeout", 1.0)
    monkeypatch.setattr(HostLinkConfig, "connect_timeout", 1.0)
    monkeypatch.setattr(HostLinkConfig, "request_timeout", 1.0)
    monkeypatch.setattr(BasicConfig, "machine_name", "ros-json-slave")
    monkeypatch.setattr(BasicConfig, "slave_no_host", False)

    host_local = HostLinkLocalRuntime()
    source = host_local.add_driver(
        HostLinkDriverSpec(
            device_id="ros-source",
            driver_class=RosJsonDriver,
            config={"publish": True},
            action_names=("send",),
        )
    )
    slave_local = HostLinkLocalRuntime()
    sink = slave_local.add_driver(
        HostLinkDriverSpec(
            device_id="ros-sink",
            driver_class=RosJsonDriver,
            config={"subscribe_to": "ros-source"},
            action_names=("echo",),
        )
    )
    host = HostLinkBackend(host_local, is_slave=False)
    slave = HostLinkBackend(slave_local, is_slave=True)
    host.start()
    assert host.server is not None
    HostLinkConfig.port = host.server.port
    try:
        slave.start()
        assert _wait_until(
            lambda: any(
                "/devices/ros-source/ros_value" in topics
                for topics in host._remote_topic_subscriptions.values()
            )
        )

        source.call_action("send", name="中文状态")
        assert _wait_until(
            lambda: (
                sink.driver.received == [{"name": "中文状态", "samples": [1.25, 2.5]}]
            )
        )

        result = host.call_action(
            "ros-sink",
            "echo",
            payload=RosJsonMessage("中文动作", [3.5, 4.75]),
        )
        assert result == {"name": "中文动作", "samples": [3.5, 4.75]}
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

    host = HostLinkBackend(HostLinkLocalRuntime(), is_slave=False)
    slave = HostLinkBackend(
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


def test_cancelling_async_call_forwards_to_remote_action(monkeypatch) -> None:
    monkeypatch.setattr(HostLinkConfig, "enable", True)
    monkeypatch.setattr(HostLinkConfig, "bind", "127.0.0.1")
    monkeypatch.setattr(HostLinkConfig, "host", "127.0.0.1")
    monkeypatch.setattr(HostLinkConfig, "port", 0)
    monkeypatch.setattr(HostLinkConfig, "heartbeat_interval", 0.05)
    monkeypatch.setattr(HostLinkConfig, "heartbeat_timeout", 1.0)
    monkeypatch.setattr(HostLinkConfig, "connect_timeout", 1.0)
    monkeypatch.setattr(HostLinkConfig, "request_timeout", 2.0)
    monkeypatch.setattr(BasicConfig, "machine_name", "async-cancel-slave")
    monkeypatch.setattr(BasicConfig, "slave_no_host", False)

    host = HostLinkBackend(HostLinkLocalRuntime(), is_slave=False)
    slave = HostLinkBackend(
        _feedback_runtime("feedback-device"),
        is_slave=True,
    )
    host.start()
    assert host.server is not None
    HostLinkConfig.port = host.server.port
    feedback: list[dict] = []
    context = ActionContext(
        action_id="async-cancel",
        feedback_callback=lambda _action_id, data: feedback.append(data),
    )
    try:
        slave.start()

        async def scenario() -> None:
            task = asyncio.create_task(
                host.call_action_async(
                    "feedback-device",
                    "run_steps",
                    action_context=context,
                    steps=100,
                )
            )
            deadline = asyncio.get_running_loop().time() + 2.0
            while not feedback and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.01)
            assert feedback
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

            deadline = asyncio.get_running_loop().time() + 2.0
            while slave._actions and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.01)
            assert not slave._actions

        asyncio.run(scenario())
        assert context.is_cancelled is True
    finally:
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

    host = HostLinkBackend(HostLinkLocalRuntime(), is_slave=False)
    caller = HostLinkBackend(
        _counter_runtime("slave-caller"),
        is_slave=True,
    )
    target = HostLinkBackend(
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


def test_hostlink_runtime_uses_microbackend_resource_authority(
    tmp_path, monkeypatch
) -> None:
    from unilabos.resources.presets.container import RegularContainer
    from unilabos.client.materials import LocalMaterialsClient
    from unilabos.server.scheduler.integration import set_materials_gateway
    from unilabos.server.services.materials import MaterialsService

    monkeypatch.setattr(HostLinkConfig, "enable", True)
    monkeypatch.setattr(HostLinkConfig, "bind", "127.0.0.1")
    monkeypatch.setattr(HostLinkConfig, "port", 0)
    material_service = MaterialsService(MaterialsRepository(tmp_path / "materials.db"))
    set_materials_gateway(LocalMaterialsClient(material_service))
    runtime = _counter_runtime("resource-device", resource_uuid="device-uuid")
    backend = HostLinkBackend(runtime, is_slave=False)
    node = runtime.devices["resource-device"]
    backend.start()
    beaker = RegularContainer(
        name="runtime-beaker",
        size_x=10,
        size_y=10,
        size_z=20,
        max_volume=100,
    )
    beaker.unilabos_extra = {
        "unilabos_resource_class": "runtime-beaker"
    }
    try:
        created = asyncio.run(node.create_material(beaker))
        material = created.resources[0]
        material_uuid = material.unilabos_uuid

        downloaded = asyncio.run(node.get_resource([material_uuid]))
        assert downloaded.all_nodes_uuid == [material_uuid]

        material.tracker.set_liquids([("water", 12.0, "ul")])
        asyncio.run(node.update_resource(material))
        substances = material_service.get_material(
            material_uuid
        ).data.substances
        assert [
            (item.name, item.quantity, item.quantity_unit)
            for item in substances
        ] == [("water", 12.0, "ul")]

        assert not hasattr(ActionType, "RESOURCE_UPDATE")
        assert not hasattr(ActionType, "RESOURCE_GET")
        assert not hasattr(ActionType, "MATERIAL")
    finally:
        backend.stop()
        set_materials_gateway(None)
        material_service.repository.close()
