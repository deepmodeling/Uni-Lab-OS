"""原生 ROS Goal 的零值不能覆盖 Registry 明确声明的可执行默认值。"""

import sys
from pathlib import Path
from shutil import copyfile

import pytest

from unilabos.config.config import BasicConfig
from unilabos.registry.registry import lab_registry


def test_explicit_native_action_goal_defaults_survive_type_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 以外部注册表加载旧模板，单测保持覆盖 Goal 默认值而无需安装它的驱动。
    source = lab_registry.registry_paths[0] / "devices" / "virtual_device.yaml"
    devices = tmp_path / "devices"
    devices.mkdir()
    copyfile(source, devices / source.name)
    monkeypatch.setattr(BasicConfig, "working_dir", str(tmp_path))
    monkeypatch.setattr(sys, "path", sys.path.copy())
    registry = type(lab_registry)()
    registry.registry_paths = [tmp_path]
    registry.setup()
    registry.resolve_all_types()

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
