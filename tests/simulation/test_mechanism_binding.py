"""驱动动作与资产机构动作的静态对应规则。"""
import textwrap

import pytest

from unilabos.registry.decorators import action, get_action_meta
from unilabos.simulation.mechanism_binding import bind_actions, scan_actions

DRIVER = '''
from unilabos.registry.decorators import action

class Turntable:
    @action(action_name="BY_OpenDoor", description="开门")
    async def open_door(self, timeout_tcp: int | None = None): ...

    @action(action_name="BY_Rotate", motion={"action": "rotate", "args": {"angle_deg": "angleEnum"}})
    async def rotate(self, angleEnum: int, timeout_tcp: int | None = None): ...

    @action(motion="clamp_off")
    async def release(self): ...

    @action(description="工艺动作，没有机构运动")
    async def run_protocol(self, name: str): ...

    def helper(self): ...
'''
BEHAVIOR = '''
class Asset:
    def open_door(self, joints): ...

    def rotate(self, joints, sites, angle_deg, materials=None): ...

    def clamp_off(self, joints): ...

    def close_door(self, joints): ...

    def _private(self, joints): ...
'''


def _write(tmp_path, driver=DRIVER, behavior=BEHAVIOR):
    (tmp_path / "driver.py").write_text(textwrap.dedent(driver), encoding="utf8")
    (tmp_path / "behavior.py").write_text(textwrap.dedent(behavior), encoding="utf8")
    return tmp_path / "driver.py", tmp_path / "behavior.py"


def test_same_method_name_binds_and_motion_overrides_name_and_arguments(tmp_path):
    driver, behavior = _write(tmp_path)
    result = bind_actions(driver, "Turntable", behavior, "Asset")
    assert result["problems"] == []
    assert result["actions"] == {
        "BY_OpenDoor": {"mechanism": "open_door", "args": {}},          # 同名；对外动作名取 action_name
        "BY_Rotate": {"mechanism": "rotate", "args": {"angle_deg": "angleEnum"}},
        "release": {"mechanism": "clamp_off", "args": {}},
    }
    assert result["unbound"] == ["run_protocol"] and result["unused"] == ["close_door"]
    assert scan_actions(driver, "Turntable")["open_door"]["parameters"] == ["timeout_tcp"]


def test_same_name_with_unmatched_required_parameter_is_a_problem_not_a_guess(tmp_path):
    driver, behavior = _write(tmp_path, driver=DRIVER.replace(
        'motion={"action": "rotate", "args": {"angle_deg": "angleEnum"}}', 'description="转"'))
    result = bind_actions(driver, "Turntable", behavior, "Asset")
    assert "BY_Rotate" not in result["actions"]
    assert any("angle_deg" in problem for problem in result["problems"])


def test_motion_pointing_to_missing_mechanism_action_or_parameter_is_a_problem(tmp_path):
    driver, behavior = _write(tmp_path, driver=DRIVER.replace('motion="clamp_off"', 'motion="does_not_exist"')
                              .replace('"angle_deg": "angleEnum"', '"angle": "angleEnum"'))
    problems = bind_actions(driver, "Turntable", behavior, "Asset")["problems"]
    assert any("does_not_exist" in problem for problem in problems)
    assert any("['angle']" in problem for problem in problems)


def test_action_decorator_accepts_and_validates_motion():
    @action(motion={"action": "rotate", "args": {"angle_deg": "angleEnum"}})
    def rotate(self, angleEnum: int): ...

    assert get_action_meta(rotate)["motion"] == {"action": "rotate", "args": {"angle_deg": "angleEnum"}}

    @action()
    def plain(self): ...

    assert "motion" not in get_action_meta(plain)
    with pytest.raises(TypeError):
        action(motion={"args": {}})(lambda self: None)
    with pytest.raises(TypeError):
        action(motion={"action": "rotate", "unknown": 1})(lambda self: None)
