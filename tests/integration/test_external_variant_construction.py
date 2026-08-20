"""Plan 09 Task 6 (integration): class.init is resolved and fed to the real device
construction machinery, building the shared Python class with a factory backend.

- Creator-level test exercises resolve_init_kwargs -> DeviceClassCreator ->
  create_instance_from_config -> cls(**kwargs) (the exact surface T6 touches).
- ROS-level test goes through _instantiate_device_node under an rclpy context and
  asserts the wrapped node's driver_instance got the factory-built backend.
"""

import pytest

ENTRY = {
    "class": {
        "module": "tests.registry.fixtures.initializer_drivers:SharedDevice",
        "type": "python",
        "init": {
            "kwargs": {
                "backend": {
                    "factory": "tests.registry.fixtures.initializer_drivers:MockBackend",
                    "kwargs": {"host": "${config.host}", "port": "${config.port}"},
                },
                "deck": {
                    "factory": "tests.registry.fixtures.initializer_drivers:MockDeck",
                    "kwargs": {"name": "runtime-deck"},
                },
                "name": "${node.id}",
                "channels": 384,
            }
        },
        "status_types": {},
        "action_value_mappings": {},
    }
}
NODE = {"id": "lh-runtime", "name": "Runtime LH"}
CONFIG = {"host": "10.0.0.2", "port": 1234}


@pytest.mark.integration
def test_class_init_built_via_real_creator():
    """resolve_init_kwargs output flows through the real DeviceClassCreator."""
    from unilabos.registry.initializer import resolve_init_kwargs
    from unilabos.resources.resource_tracker import DeviceNodeResourceTracker
    from unilabos.device_runtime.driver_creator import DeviceClassCreator
    from tests.registry.fixtures.initializer_drivers import SharedDevice

    resolved = resolve_init_kwargs(ENTRY, node=NODE, config=CONFIG)
    creator = DeviceClassCreator(SharedDevice, children=[], resource_tracker=DeviceNodeResourceTracker())
    device = creator.create_instance(resolved["kwargs"])

    assert isinstance(device, SharedDevice)
    assert device.backend.host == "10.0.0.2"
    assert device.backend.port == 1234
    assert device.deck.name == "runtime-deck"
    assert device.name == "lh-runtime"
    assert device.channels == 384


@pytest.mark.integration
def test_class_init_via_instantiate_device_node(ros_context):
    """Full edge path: registry entry with class.init -> _instantiate_device_node ->
    ROS2DeviceNode whose driver_instance is the factory-constructed SharedDevice."""
    from unilabos.registry.registry import lab_registry
    from unilabos.resources.resource_tracker import ResourceDictInstance
    from unilabos.ros.initialize_device import _instantiate_device_node

    lab_registry.device_type_registry["vendor.lh.model_a"] = dict(ENTRY)
    try:
        device_config = ResourceDictInstance.get_resource_instance_from_dict({
            "name": "lh_runtime",  # ROS2 node name: no hyphens
            "type": "device",
            "class": "vendor.lh.model_a",
            "config": CONFIG,
        })
        node = _instantiate_device_node("lh_runtime", device_config, "vendor.lh.model_a")
        assert node is not None
        driver = getattr(node, "driver_instance", None)
        assert driver is not None
        assert driver.backend.host == "10.0.0.2"
        assert driver.backend.port == 1234
        assert driver.deck.name == "runtime-deck"
        assert driver.channels == 384
        assert driver.name == "lh_runtime"  # ${node.id} injected
    finally:
        lab_registry.device_type_registry.pop("vendor.lh.model_a", None)


# --- Plan 09 T6: real pylabrobot LiquidHandler via class.init (F) -----------------

PLR_ENTRY = {
    "class": {
        "module": "pylabrobot.liquid_handling.liquid_handler:LiquidHandler",
        "type": "python",
        "init": {
            "kwargs": {
                "backend": {
                    "factory": "pylabrobot.liquid_handling.backends.chatterbox:LiquidHandlerChatterboxBackend",
                    "kwargs": {"num_channels": 8},
                },
                "deck": {
                    "factory": "pylabrobot.resources:Deck",
                    "kwargs": {"size_x": 100.0, "size_y": 100.0, "size_z": 10.0},
                },
                "name": "${node.id}",
            }
        },
        "status_types": {},
        "action_value_mappings": {},
    }
}


@pytest.mark.integration
def test_pylabrobot_liquidhandler_built_via_class_init():
    """Two registry entries can share pylabrobot LiquidHandler but pick different
    backends via class.init — proven by constructing a real LiquidHandler."""
    pytest.importorskip("pylabrobot")
    from unilabos.registry.initializer import build_instance_from_registry_entry

    lh = build_instance_from_registry_entry(PLR_ENTRY, node={"id": "lh_plr", "name": "LH"}, config={})

    from pylabrobot.liquid_handling.liquid_handler import LiquidHandler

    assert isinstance(lh, LiquidHandler)
    assert lh.backend.num_channels == 8
    assert lh.name == "lh_plr"


@pytest.mark.integration
def test_pylabrobot_via_instantiate_device_node(ros_context):
    """Full edge path for a pylabrobot driver (goes through PyLabRobotCreator)."""
    pytest.importorskip("pylabrobot")
    from unilabos.registry.registry import lab_registry
    from unilabos.resources.resource_tracker import ResourceDictInstance
    from unilabos.ros.initialize_device import _instantiate_device_node

    lab_registry.device_type_registry["pylabrobot.lh.chatterbox"] = dict(PLR_ENTRY)
    try:
        device_config = ResourceDictInstance.get_resource_instance_from_dict({
            "name": "lh_plr_node",
            "type": "device",
            "class": "pylabrobot.lh.chatterbox",
            "config": {},
        })
        node = _instantiate_device_node("lh_plr_node", device_config, "pylabrobot.lh.chatterbox")
        assert node is not None
        driver = getattr(node, "driver_instance", None)
        assert driver is not None
        assert driver.backend.num_channels == 8
    finally:
        lab_registry.device_type_registry.pop("pylabrobot.lh.chatterbox", None)
