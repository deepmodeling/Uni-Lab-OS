from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml

from unilabos.basic.runtime import (
    BasicDeviceNode,
    BasicDriverSpec,
    BasicRuntime,
    instantiate_driver,
)
from unilabos.device_runtime import BackendCapabilityError
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


def test_driver_instantiation_supports_config_and_flat_styles() -> None:
    config_driver = instantiate_driver(ConfigDriver, "config-1", {"port": "A"})
    assert config_driver.device_id == "config-1"
    assert config_driver.config == {"port": "A"}

    flat_driver = instantiate_driver(FlatDriver, "flat-1", {"port": "B"})
    assert flat_driver.device_id == "flat-1"
    assert flat_driver.port == "B"


def test_liquid_handlers_declare_python_backends_and_action_metadata() -> None:
    registry = yaml.safe_load(
        Path("unilabos/registry/devices/liquid_handler.yaml").read_text(
            encoding="utf-8"
        )
    )
    for name in ("liquid_handler", "liquid_handler.prcxi"):
        class_config = registry[name]["class"]
        assert class_config["supported_backends"] == ["basic", "hostlink", "ros2"]
        first_action = next(iter(class_config["action_value_mappings"].values()))
        assert first_action["type"]
        assert first_action["schema"]["type"] == "object"


def test_basic_runtime_constructs_and_sets_up_pylabrobot_style_driver(
    monkeypatch,
) -> None:
    monkeypatch.setattr("unilabos.basic.runtime.register", lambda: None)
    tracker = DeviceNodeResourceTracker()
    driver = instantiate_driver(
        LiquidHandlerAbstract,
        "liquid-handler",
        {"backend": "simulator"},
        device_config=DeviceConfig(),
        resource_tracker=tracker,
    )
    node = BasicDeviceNode(
        driver,
        "liquid-handler",
        backend_name="hostlink",
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


def test_basic_device_lifecycle_and_direct_action() -> None:
    driver = AsyncDriver("dev-1", {})
    node = BasicDeviceNode(driver, "dev-1")
    node.start()
    try:
        assert driver.node is node
        assert driver.initialized is True
        assert node.call_action("add", left=2, right=3) == 5
    finally:
        node.stop()
    assert driver.cleaned is True


def test_basic_runtime_owns_and_routes_devices() -> None:
    runtime = BasicRuntime()
    runtime.add_driver(BasicDriverSpec("dev-1", AsyncDriver, {"answer": 42}))
    runtime.start()
    try:
        assert runtime.call_action("dev-1", "add", left=10, right=5) == 15
    finally:
        runtime.stop()
    assert runtime.wait(timeout=0) is True


def test_basic_runtime_routes_cross_device_actions() -> None:
    runtime = BasicRuntime()
    runtime.add_driver(BasicDriverSpec("caller", AsyncDriver, {}))
    runtime.add_driver(BasicDriverSpec("target", AsyncDriver, {}))
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


def test_basic_runtime_awaits_cross_device_actions_natively() -> None:
    runtime = BasicRuntime()
    runtime.add_driver(BasicDriverSpec("caller", AsyncDriver, {}))
    runtime.add_driver(BasicDriverSpec("target", AsyncDriver, {}))
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


def test_basic_runtime_exposes_registered_actions_and_status() -> None:
    runtime = BasicRuntime()
    runtime.add_driver(
        BasicDriverSpec(
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


def test_basic_runtime_supports_ros_shaped_timer_clock_and_parameters() -> None:
    driver = AsyncDriver("dev-1", {})
    node = BasicDeviceNode(driver, "dev-1")
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


def test_basic_runtime_routes_ros_shaped_services_between_devices() -> None:
    runtime = BasicRuntime()
    runtime.add_driver(BasicDriverSpec("provider", ServiceDriver, {"provider": True}))
    runtime.add_driver(
        BasicDriverSpec(
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


def test_basic_runtime_exposes_action_metadata() -> None:
    runtime = BasicRuntime()
    runtime.add_driver(
        BasicDriverSpec(
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


def test_basic_runtime_reports_direct_ros_node_calls_clearly() -> None:
    runtime = BasicRuntime("hostlink")
    runtime.add_driver(
        BasicDriverSpec(
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
