"""社区设备 JSON 初始化参数进入真实驱动 Creator 的集成合同。"""

from unilabos.registry.init_enforce import merge_init_param_enforce
from unilabos.resources.resource_tracker import DeviceNodeResourceTracker
from unilabos.ros.utils.driver_creator import DeviceClassCreator
from tests.registry.fixtures.initializer_drivers import SharedDevice


def test_init_param_enforce_flows_through_real_device_creator():
    runtime_config = {"host": "10.0.0.2", "port": 1234, "channels": 1}
    enforced_config = {"deck_name": "runtime-deck", "channels": 384}
    driver_params = merge_init_param_enforce(runtime_config, enforced_config)
    driver_params["device_id"] = "lh-runtime"

    creator = DeviceClassCreator(
        SharedDevice,
        children=[],
        resource_tracker=DeviceNodeResourceTracker(),
    )
    device = creator.create_instance(driver_params)

    assert isinstance(device, SharedDevice)
    assert device.backend.host == "10.0.0.2"
    assert device.backend.port == 1234
    assert device.deck.name == "runtime-deck"
    assert device.name == "lh-runtime"
    assert device.channels == 384


def test_two_registry_variants_share_driver_but_keep_distinct_enforced_config():
    runtime_config = {"host": "127.0.0.1", "port": 7001}
    model_a = merge_init_param_enforce(
        runtime_config,
        {"deck_name": "model-a-deck", "channels": 8},
    )
    model_b = merge_init_param_enforce(
        runtime_config,
        {"deck_name": "model-b-deck", "channels": 96},
    )

    assert model_a == {
        "host": "127.0.0.1",
        "port": 7001,
        "deck_name": "model-a-deck",
        "channels": 8,
    }
    assert model_b == {
        "host": "127.0.0.1",
        "port": 7001,
        "deck_name": "model-b-deck",
        "channels": 96,
    }
