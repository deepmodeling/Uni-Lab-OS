from __future__ import annotations

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

    def increment(self, amount: int = 1) -> int:
        self.count += amount
        return self.count


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


def _counter_runtime(device_id: str, initial: int = 0) -> BasicRuntime:
    runtime = BasicRuntime()
    runtime.add_driver(
        BasicDriverSpec(
            device_id=device_id,
            driver_class=CounterDriver,
            config={"initial": initial},
            registry_name="counter",
            display_name="Counter",
            action_names=("increment",),
            status_names=("count",),
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
