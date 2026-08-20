from __future__ import annotations

import asyncio
from pathlib import Path
import time

import pytest
import yaml

from unilabos.hostlink.local_runtime import (
    HostLinkDeviceNode,
    HostLinkDriverSpec,
    HostLinkLocalRuntime,
    instantiate_driver,
)
from unilabos.device_runtime import BackendCapabilityError
from unilabos.device_runtime.action import ActionContext
from unilabos.registry.decorators import topic_config
from unilabos.registry.placeholder_type import ResourceSlot
from unilabos.resources.resource_tracker import DeviceNodeResourceTracker


class ConfigDriver:
    def __init__(self, device_id=None, config=None):
        self.device_id = device_id
        self.config = config


class FlatDriver:
    def __init__(self, port, device_id=None):
        self.port = port
        self.device_id = device_id


class LiquidHandlerAbstract:
    def __init__(self, backend):
        self.backend = backend
        self.setup_called = False

    def post_init(self, node) -> None:
        self.node = node

    async def setup(self) -> None:
        await self.node.sleep(0)
        self.setup_called = True


class DeviceConfig:
    children = []


class AsyncDriver:
    def __init__(self, device_id=None, config=None):
        self.device_id = device_id
        self.config = config
        self.node = None
        self.initialized = False
        self.cleaned = False
        self.ready = "idle"

    def post_init(self, node) -> None:
        self.node = node

    async def initialize(self) -> bool:
        await self.node.sleep(0)
        self.initialized = True
        return True

    async def add(self, left: int, right: int) -> int:
        return left + right

    async def call_peer(
        self,
        target_device: str,
        left: int,
        right: int,
    ) -> int:
        return self.node.call_device_action(
            target_device,
            "add",
            {"left": left, "right": right},
        )

    async def call_peer_async(
        self,
        target_device: str,
        left: int,
        right: int,
    ) -> int:
        return await self.node.call_device_action_async(
            target_device,
            "add",
            {"left": left, "right": right},
        )

    async def cleanup(self) -> bool:
        self.cleaned = True
        return True


class RosGuardConditionDriver:
    def __init__(self, device_id=None, config=None):
        self.device_id = device_id
        self.node = None

    def post_init(self, node) -> None:
        self.node = node

    def create_guard(self) -> None:
        self.node.create_guard_condition(lambda: None)


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
        self.provider = bool((config or {}).get("provider"))

    def post_init(self, node) -> None:
        self.node = node
        if self.provider:
            node.create_service(AddService, "add", self.add)

    @staticmethod
    def add(request, response):
        response.total = request.left + request.right
        return response

    async def call_add(self, left, right):
        client = self.node.create_client(AddService, "/devices/provider/add")
        response = await client.call_async(AddService.Request(left, right))
        return response.total


class DecoratedRuntimeDriver:
    def __init__(self, device_id=None, config=None):
        self.device_id = device_id
        self.node = None
        self.progress = 0
        self.status_reads = 0

    def post_init(self, node) -> None:
        self.node = node

    @property
    @topic_config(period=0.02, name="renamed_status")
    def status(self) -> int:
        self.status_reads += 1
        return self.status_reads

    def mapped_action(self, driver_value: int) -> dict[str, int]:
        return {"value": driver_value * 2}

    def nested_action(self, driver_values: list[int]) -> list[int]:
        return [value * 2 for value in driver_values]

    def feedback_action(self, duration: float) -> int:
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            self.progress += 1
            time.sleep(0.005)
        return self.progress

    def resource_action(self, resource: ResourceSlot) -> str:
        return resource.name

    async def concurrent_action(self, duration: float) -> float:
        await self.node.sleep(duration)
        return duration


def test_driver_instantiation_supports_config_and_flat_styles() -> None:
    config_driver = instantiate_driver(ConfigDriver, "config-1", {"port": "A"})
    assert config_driver.device_id == "config-1"
    assert config_driver.config == {"port": "A"}

    flat_driver = instantiate_driver(FlatDriver, "flat-1", {"port": "B"})
    assert flat_driver.device_id == "flat-1"
    assert flat_driver.port == "B"


def test_liquid_handlers_declare_public_backends_and_action_metadata() -> None:
    registry = yaml.safe_load(
        Path("unilabos/registry/devices/liquid_handler.yaml").read_text(
            encoding="utf-8"
        )
    )
    for name in ("liquid_handler", "liquid_handler.prcxi"):
        class_config = registry[name]["class"]
        assert class_config["supported_backends"] == ["hostlink", "ros2"]
        first_action = next(iter(class_config["action_value_mappings"].values()))
        assert first_action["type"]
        assert first_action["schema"]["type"] == "object"


def test_hostlink_runtime_constructs_and_sets_up_pylabrobot_style_driver() -> None:
    tracker = DeviceNodeResourceTracker()
    driver = instantiate_driver(
        LiquidHandlerAbstract,
        "liquid-handler",
        {"backend": "simulator"},
        device_config=DeviceConfig(),
        resource_tracker=tracker,
    )
    node = HostLinkDeviceNode(
        driver,
        "liquid-handler",
        resource_tracker=tracker,
    )
    node.start()
    try:
        assert driver.backend == "simulator"
        assert driver.node is node
        assert driver.setup_called is True
        assert node.resource_tracker is tracker
    finally:
        node.stop()


def test_hostlink_device_lifecycle_and_direct_action() -> None:
    driver = AsyncDriver("dev-1", {})
    node = HostLinkDeviceNode(driver, "dev-1")
    node.start()
    try:
        assert driver.node is node
        assert driver.initialized is True
        assert node.call_action("add", left=2, right=3) == 5
    finally:
        node.stop()
    assert driver.cleaned is True


def test_hostlink_runtime_owns_and_routes_devices() -> None:
    runtime = HostLinkLocalRuntime()
    runtime.add_driver(HostLinkDriverSpec("dev-1", AsyncDriver, {"answer": 42}))
    runtime.start()
    try:
        assert runtime.call_action("dev-1", "add", left=10, right=5) == 15
    finally:
        runtime.stop()
    assert runtime.wait(timeout=0) is True


def test_hostlink_runtime_routes_cross_device_actions() -> None:
    runtime = HostLinkLocalRuntime()
    runtime.add_driver(HostLinkDriverSpec("caller", AsyncDriver, {}))
    runtime.add_driver(HostLinkDriverSpec("target", AsyncDriver, {}))
    runtime.start()
    try:
        assert (
            runtime.call_action(
                "caller",
                "call_peer",
                target_device="target",
                left=4,
                right=6,
            )
            == 10
        )
    finally:
        runtime.stop()


def test_hostlink_runtime_awaits_cross_device_actions_natively() -> None:
    runtime = HostLinkLocalRuntime()
    runtime.add_driver(HostLinkDriverSpec("caller", AsyncDriver, {}))
    runtime.add_driver(HostLinkDriverSpec("target", AsyncDriver, {}))
    runtime.start()
    try:
        result = asyncio.run(
            runtime.call_action_async(
                "caller",
                "call_peer_async",
                target_device="target",
                left=7,
                right=8,
            )
        )
        assert result == 15
    finally:
        runtime.stop()


def test_hostlink_runtime_exposes_registered_actions_and_status() -> None:
    runtime = HostLinkLocalRuntime()
    runtime.add_driver(
        HostLinkDriverSpec(
            "dev-1",
            AsyncDriver,
            {},
            registry_name="async_driver",
            display_name="Async Driver",
            action_names=("auto-add",),
            status_names=("ready",),
        )
    )
    runtime.start()
    try:
        assert runtime.descriptors() == [
            {
                "id": "dev-1",
                "registry_name": "async_driver",
                "display_name": "Async Driver",
                "actions": ["auto-add"],
                "status_fields": ["ready"],
            }
        ]
        assert runtime.snapshot_states() == {"dev-1": {"ready": "idle"}}
        assert runtime.call_action("dev-1", "auto-add", left=1, right=2) == 3
    finally:
        runtime.stop()


def test_hostlink_runtime_supports_ros_shaped_timer_clock_and_parameters() -> None:
    driver = AsyncDriver("dev-1", {})
    node = HostLinkDeviceNode(driver, "dev-1")
    fired = []
    node.start()
    try:
        parameter = node.declare_parameter("speed", 3)
        assert parameter.value == 3
        assert node.get_parameter("speed").get_parameter_value().integer_value == 3
        assert node.get_clock().now().nanoseconds > 0

        timer = node.create_timer(0.01, lambda: fired.append(True))
        for _ in range(100):
            if fired:
                break
            node.create_rate(1000).sleep()
        assert fired
        assert node.destroy_timer(timer) is True
    finally:
        node.stop()


def test_hostlink_runtime_routes_ros_shaped_services_between_devices() -> None:
    runtime = HostLinkLocalRuntime()
    runtime.add_driver(HostLinkDriverSpec("provider", ServiceDriver, {"provider": True}))
    runtime.add_driver(
        HostLinkDriverSpec(
            "caller",
            ServiceDriver,
            {},
            action_names=("call_add",),
        )
    )
    runtime.start()
    try:
        assert runtime.call_action("caller", "call_add", left=7, right=8) == 15
        assert runtime.descriptors()[0]["services"] == ["/devices/provider/add"]
    finally:
        runtime.stop()


def test_hostlink_runtime_exposes_action_metadata() -> None:
    runtime = HostLinkLocalRuntime()
    runtime.add_driver(
        HostLinkDriverSpec(
            "dev-1",
            AsyncDriver,
            {},
            action_names=("add",),
            action_value_mappings={
                "add": {
                    "type": AddService,
                    "goal": {"left": "left", "right": "right"},
                    "result": {"total": "total"},
                    "schema": {"type": "object"},
                }
            },
        )
    )
    descriptor = runtime.descriptors()[0]
    assert descriptor["action_value_mappings"]["add"] == {
        "type": f"{AddService.__module__}.{AddService.__qualname__}",
        "goal": {"left": "left", "right": "right"},
        "result": {"total": "total"},
        "schema": {"type": "object"},
    }


def _decorated_runtime() -> HostLinkLocalRuntime:
    runtime = HostLinkLocalRuntime()
    runtime.add_driver(
        HostLinkDriverSpec(
            "decorated",
            DecoratedRuntimeDriver,
            {},
            action_names=(
                "mapped_action",
                "nested_action",
                "feedback_action",
                "concurrent_action",
                "resource_action",
            ),
            action_value_mappings={
                "mapped_action": {
                    "type": "MappedAction",
                    "goal": {"wire_value": "driver_value"},
                    "result": {"value": "value"},
                },
                "nested_action": {
                    "type": "NestedAction",
                    "goal": {"items[].wire_value": "driver_values[]"},
                },
                "feedback_action": {
                    "type": "FeedbackAction",
                    "goal": {"duration": "duration"},
                    "feedback": {"progress": "progress"},
                    "feedback_interval": 0.01,
                },
                "concurrent_action": {
                    "type": "ConcurrentAction",
                    "always_free": True,
                },
                "resource_action": {
                    "type": "ResourceAction",
                    "goal": {"resource": "resource"},
                },
            },
            status_names=("status",),
        )
    )
    return runtime


def test_hostlink_runtime_applies_action_goal_mapping_and_feedback_config() -> None:
    runtime = _decorated_runtime()
    runtime.start()
    feedback: list[dict[str, int]] = []
    try:
        assert runtime.call_action(
            "decorated",
            "mapped_action",
            wire_value=7,
        ) == {"value": 14}
        assert runtime.call_action(
            "decorated",
            "nested_action",
            items=[{"wire_value": 2}, {"wire_value": 5}],
        ) == [4, 10]
        context = ActionContext(
            feedback_callback=lambda _action_id, data: feedback.append(data)
        )
        assert runtime.call_action(
            "decorated",
            "feedback_action",
            action_context=context,
            duration=0.05,
        ) > 0
        assert feedback
        assert feedback[-1]["progress"] > 0
    finally:
        runtime.stop()


def test_hostlink_runtime_resolves_resource_slot_before_driver_call() -> None:
    from pylabrobot.resources import Resource

    runtime = _decorated_runtime()
    resource = Resource("plate", size_x=1, size_y=1, size_z=1)
    resource.unilabos_uuid = "resource-uuid"
    runtime.devices["decorated"].resource_tracker.add_resource(resource)
    runtime.start()
    try:
        assert runtime.call_action(
            "decorated",
            "resource_action",
            resource={"uuid": "resource-uuid"},
        ) == "plate"
    finally:
        runtime.stop()


def test_hostlink_runtime_honors_topic_config_period_and_name() -> None:
    runtime = _decorated_runtime()
    received: list[int] = []
    runtime.topic_bus.subscribe(
        "/devices/decorated/renamed_status",
        received.append,
    )
    runtime.start()
    try:
        deadline = time.monotonic() + 0.5
        while len(received) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(received) >= 2
        assert runtime.descriptors()[0]["status_fields"] == ["renamed_status"]
        assert set(runtime.snapshot_states()["decorated"]) == {"renamed_status"}
    finally:
        runtime.stop()


def test_hostlink_runtime_does_not_queue_scheduler_dispatched_actions() -> None:
    runtime = _decorated_runtime()
    runtime.start()
    try:
        async def run_both() -> tuple[list[float], float]:
            started = time.monotonic()
            values = await asyncio.gather(
                runtime.call_action_async(
                    "decorated", "concurrent_action", duration=0.05
                ),
                runtime.call_action_async(
                    "decorated", "concurrent_action", duration=0.05
                ),
            )
            return values, time.monotonic() - started

        values, elapsed = asyncio.run(run_both())
        assert values == [0.05, 0.05]
        assert elapsed < 0.09
    finally:
        runtime.stop()


def test_hostlink_runtime_reports_direct_ros_node_calls_clearly() -> None:
    runtime = HostLinkLocalRuntime()
    runtime.add_driver(
        HostLinkDriverSpec(
            "ros-guard",
            RosGuardConditionDriver,
            {},
            action_names=("create_guard",),
        )
    )
    runtime.start()
    try:
        with pytest.raises(
            BackendCapabilityError,
            match="设备 'ros-guard'.*create_guard_condition.*DeviceNode",
        ):
            runtime.call_action("ros-guard", "create_guard")
    finally:
        runtime.stop()
