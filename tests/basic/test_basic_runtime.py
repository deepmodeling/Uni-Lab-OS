from __future__ import annotations

from unilabos.basic.runtime import (
    BasicDeviceNode,
    BasicDriverSpec,
    BasicRuntime,
    instantiate_driver,
)


class ConfigDriver:
    def __init__(self, device_id=None, config=None):
        self.device_id = device_id
        self.config = config


class FlatDriver:
    def __init__(self, port, device_id=None):
        self.port = port
        self.device_id = device_id


class AsyncDriver:
    def __init__(self, device_id=None, config=None):
        self.device_id = device_id
        self.config = config
        self.node = None
        self.initialized = False
        self.cleaned = False

    def post_init(self, node) -> None:
        self.node = node

    async def initialize(self) -> bool:
        await self.node.sleep(0)
        self.initialized = True
        return True

    async def add(self, left: int, right: int) -> int:
        return left + right

    async def cleanup(self) -> bool:
        self.cleaned = True
        return True


def test_driver_instantiation_supports_config_and_flat_styles() -> None:
    config_driver = instantiate_driver(ConfigDriver, "config-1", {"port": "A"})
    assert config_driver.device_id == "config-1"
    assert config_driver.config == {"port": "A"}

    flat_driver = instantiate_driver(FlatDriver, "flat-1", {"port": "B"})
    assert flat_driver.device_id == "flat-1"
    assert flat_driver.port == "B"


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
    runtime.add_driver(
        BasicDriverSpec("dev-1", AsyncDriver, {"answer": 42})
    )
    runtime.start()
    try:
        assert runtime.call_action("dev-1", "add", left=10, right=5) == 15
    finally:
        runtime.stop()
    assert runtime.wait(timeout=0) is True
