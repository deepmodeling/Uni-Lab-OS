"""原生 ROS Goal 的零值不能覆盖 Registry 明确声明的可执行默认值。"""

from unilabos.registry.registry import build_registry


def test_explicit_native_action_goal_defaults_survive_type_resolution() -> None:
    registry = build_registry(upload_registry=False)

    heater = registry.device_type_registry["virtual_heatchill"]
    heat_default = heater["class"]["action_value_mappings"]["heat_chill"][
        "goal_default"
    ]
    pump = registry.device_type_registry["virtual_transfer_pump"]
    position_default = pump["class"]["action_value_mappings"]["set_position"][
        "goal_default"
    ]
    transfer_goal = pump["class"]["action_value_mappings"]["transfer"]["goal"]

    assert heat_default["temp"] == 25.0
    assert heat_default["time"] == "1"
    assert heat_default["stir_speed"] == 300.0
    assert position_default["max_velocity"] == 5.0
    assert "volume" in transfer_goal
    assert "aspirate_velocity" not in transfer_goal
    assert "dispense_velocity" not in transfer_goal
