"""目录式社区包从发现、加载到 JSON 配置构建设备的完整合同。"""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from tests.registry.fixtures.initializer_drivers import SharedDevice
from unilabos.package_manager.package_distribution.registry_discovery import (
    discover_registry_paths_from_project,
)
from unilabos.registry.init_enforce import merge_init_param_enforce
from unilabos.registry.registry import Registry
from unilabos.resources.resource_tracker import DeviceNodeResourceTracker
from unilabos.ros.utils.driver_creator import DeviceClassCreator

PKG = Path(__file__).parent / "fixtures" / "external_variant_pkg"


def test_external_package_discover_load_and_construct_from_json_config():
    """外部包注册表（Registry）从发现到设备构造保持完整配置。

    参数：无；使用仓库固定的双变体外部包夹具。
    返回：无；断言共享驱动定义和各自初始化强制值都能到达设备实例。
    异常：发现、注册表加载或设备构造链任一环节漂移时测试失败。
    """

    paths = discover_registry_paths_from_project(PKG)
    assert paths == [(PKG / "unilabos_registry").resolve()]

    registry = Registry()
    if registry._startup_executor is None:
        registry._startup_executor = ThreadPoolExecutor(max_workers=2)
    registry.load_device_types(paths[0], complete_registry=False)

    model_a = registry.device_type_registry["vendor.lh.model_a"]
    model_b = registry.device_type_registry["vendor.lh.model_b"]
    assert model_a["class"]["module"] == model_b["class"]["module"]
    assert model_a["init_param_enforce"] == {"deck_name": "model-a-deck", "channels": 8}
    assert model_b["init_param_enforce"] == {
        "deck_name": "model-b-deck",
        "channels": 96,
    }
    assert "setup" in model_a["class"]["action_value_mappings"]
    assert "initialized" in model_b["class"]["status_types"]

    driver_params = merge_init_param_enforce(
        {"host": "10.0.0.9", "port": 7001, "channels": 1},
        model_b["init_param_enforce"],
    )
    driver_params["device_id"] = "lh_b"
    creator = DeviceClassCreator(
        SharedDevice,
        children=[],
        resource_tracker=DeviceNodeResourceTracker(),
    )
    device = creator.create_instance(driver_params)

    assert device.name == "lh_b"
    assert device.channels == 96
    assert device.backend.host == "10.0.0.9"
    assert device.backend.port == 7001
    assert device.deck.name == "model-b-deck"
