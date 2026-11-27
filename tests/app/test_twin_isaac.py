"""步屏障单测；真实 Isaac 另在服务器运行，不能由此授予物理资格。"""

import pytest

from unilabos.simulation.isaac import ConfirmedStepper


def test_duplicate_ack_does_not_advance_twice():
    world = ConfirmedStepper("w", "s")
    calls = []
    token = dict(world_id="w", session_id="s", epoch=0, tick=1, dt_ns=50_000_000)
    result = world.step(token, lambda *args: calls.append(args), lambda: {"q": 1.0})
    assert world.step(token, lambda *args: calls.append(args), lambda: {"q": 2.0}) == result
    assert len(calls) == 12 and world.tick == 1
    with pytest.raises(ValueError, match="step_identity_mismatch"):
        world.step({**token, "tick": 3}, lambda *args: None, lambda: {})


def test_unknown_step_cannot_retry_or_advance():
    world = ConfirmedStepper("w", "s")
    token = dict(world_id="w", session_id="s", epoch=0, tick=1, dt_ns=50_000_000)

    def fail(*args):
        raise TimeoutError()

    with pytest.raises(TimeoutError):
        world.step(token, fail, lambda: {})
    assert world.pending == token and world.tick == 0
    with pytest.raises(RuntimeError, match="step_result_unknown"):
        world.step(token, lambda *args: None, lambda: {})


def test_control_duplicate_and_conflicting_payload():
    from unilabos.simulation.isaac import ModelControls
    world = ConfirmedStepper("w", "s")
    calls = []
    controls = ModelControls(world, lambda name, params: calls.append((name, params)) or {"accepted": True})
    request = dict(world_id="w", session_id="s", epoch=0, tick=0, control_id="c", name="begin", parameters={"x": 1})
    assert controls.apply(request) == controls.apply(request)
    assert calls == [("begin", {"x": 1})]
    with pytest.raises(ValueError, match="control_payload_mismatch"):
        controls.apply({**request, "parameters": {"x": 2}})
    with pytest.raises(ValueError, match="control_identity_mismatch"):
        controls.apply({**request, "session_id": "old"})


def test_control_failure_latches_physics_world():
    from unilabos.simulation.isaac import ModelControls
    world = ConfirmedStepper("w", "s")
    def fail(*args):
        raise OSError("lost acknowledgement")
    controls = ModelControls(world, fail)
    request = dict(world_id="w", session_id="s", epoch=0, tick=0, control_id="c", name="begin", parameters={})
    with pytest.raises(OSError):
        controls.apply(request)
    with pytest.raises(RuntimeError, match="step_result_unknown"):
        controls.apply(request)
    with pytest.raises(RuntimeError, match="step_result_unknown"):
        world.step(dict(world_id="w", session_id="s", epoch=0, tick=1, dt_ns=50_000_000), lambda *args: None, lambda: {})
