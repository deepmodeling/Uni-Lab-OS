"""从零导入的资产行为类生成只有仿真行为的设备声明。"""
import asyncio
import importlib.util
import textwrap

import pytest

from unilabos.package_manager.package_distribution.asset_behavior import generate_simulated_device
from unilabos.registry.ast_registry_scanner import _parse_file
from unilabos.registry.decorators import get_action_meta
from unilabos.simulation.mechanism_binding import bind_actions

BEHAVIOR = '''
class Door:
    ACTION_CONTRACTS = {"open_door": {"estimate_duration_fixed": 4.0}}

    def open_door(self, joints):
        """开门"""
        return {"move": {"door_joint": joints.upper("door_joint")}, "duration": 4.0}

    def swing(self, joints, angle_deg: float, speed: float = 1.0):
        return {"move": {"door_joint": angle_deg}, "duration": 1.0}

    def _helper(self, joints):
        return None
'''
DECLARATION = {"id": "so503_lower_door", "category": ["enclosure"], "description": "下门"}


def _generate(tmp_path, behavior=BEHAVIOR, **kwargs):
    source = tmp_path / "behavior.py"
    source.write_text(textwrap.dedent(behavior), encoding="utf8")
    target = tmp_path / "generated_door.py"
    target.write_text(generate_simulated_device(source, "Door", declaration=DECLARATION, **kwargs), encoding="utf8")
    return source, target


def test_generated_declaration_is_scanned_by_os_and_binds_every_action_by_name(tmp_path):
    source, target = _generate(tmp_path, device_class="LowerDoor")
    devices, _ = _parse_file(target, tmp_path)
    assert [d["device_id"] for d in devices] == ["so503_lower_door"]
    assert sorted(devices[0]["actions"]) == ["open_door", "swing"]
    bound = bind_actions(target, "LowerDoor", source, "Door")
    assert bound["problems"] == [] and bound["unbound"] == [] and bound["unused"] == []
    assert bound["actions"] == {"open_door": {"mechanism": "open_door", "args": {}},
                                "swing": {"mechanism": "swing", "args": {}}}


def test_generated_actions_keep_parameters_and_refuse_to_run_without_a_driver(tmp_path):
    _, target = _generate(tmp_path)
    spec = importlib.util.spec_from_file_location("generated_door", target)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    device = module.Door()
    meta = get_action_meta(module.Door.open_door)
    assert meta["description"] == "开门" and meta["estimate_duration_fixed"] == 4.0
    assert get_action_meta(module.Door.swing)["description"] == "swing"
    with pytest.raises(RuntimeError, match="simulation_only_device"):
        asyncio.run(device.swing(angle_deg=30.0))
    with pytest.raises(TypeError):
        asyncio.run(device.swing())  # angle_deg 仍是必填；关节视图参数已去掉


@pytest.mark.parametrize("behavior", [
    "class Door:\n    def _only_private(self, joints): ...\n",
    "class Door:\n    def open_door(self): ...\n",
    "class Door:\n    def open_door(self, joints, *args): ...\n",
    "class Door:\n    ACTION_CONTRACTS = {'missing': {}}\n    def open_door(self, joints): ...\n",
    "class Door:\n    ACTION_CONTRACTS = {'open_door': {'goal': {}}}\n    def open_door(self, joints): ...\n",
])
def test_ambiguous_behavior_classes_are_rejected(tmp_path, behavior):
    with pytest.raises(ValueError):
        _generate(tmp_path, behavior=behavior)
