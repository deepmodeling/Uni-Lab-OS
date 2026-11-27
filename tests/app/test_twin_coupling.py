"""使用正式 PLC-Sim 屏障测试 OS 编排故障；不代替 Isaac 验证。"""

from dataclasses import asdict

import pytest

from unilabos.simulation.coupling import CoupledWorld, IsaacSocketPort

cosimulation = pytest.importorskip("plc_sim.cosimulation", reason="需安装可选 twin-simulation 依赖")
Channel, CouplingError = cosimulation.Channel, cosimulation.CouplingError
PhysicsFrame, Sample = cosimulation.PhysicsFrame, cosimulation.Sample


class Physics:
    def __init__(self):
        self.ticks = []

    def reset(self, token, channels):
        return PhysicsFrame(token, 0, {key: Sample(0, 0, 0) for key in channels})

    def step(self, token, commands, channels):
        self.ticks.append(token.tick)
        return PhysicsFrame(token, token.dt_ns, {key: Sample(token.tick, token.tick, token.tick) for key in channels})


def test_record_failure_blocks_next_physical_step():
    port = Physics()

    def fail(frame):
        raise OSError("记录不可写")

    world = CoupledWorld(port, channels={"q": Channel()}, writers={}, record=fail)
    world.start()
    with pytest.raises(OSError):
        world.scan()
    with pytest.raises(CouplingError):
        world.scan()
    assert port.ticks == [1] and world.session.now() == 0.05


def test_unknown_commands_are_not_silently_discarded():
    port = IsaacSocketPort(lambda token: None, lambda path: None, lambda values: values)
    with pytest.raises(ValueError, match="physics_control_model_missing"):
        port.step(None, {"motor": 1}, ("q",))


def test_wrong_physics_ack_faults_without_committing_time(monkeypatch):
    port = IsaacSocketPort(lambda token: "/unused", lambda path: None, lambda values: values)
    world = CoupledWorld(port, channels={"q": Channel()}, writers={}, record=lambda frame: None)
    monkeypatch.setattr(
        port, "_call", lambda request: {**asdict(world.session.token), "epoch": 1, "observation": {"q": 0}}
    )
    world.start()
    monkeypatch.setattr(
        port,
        "_call",
        lambda request: {"token": {**request["token"], "epoch": 0}, "substeps": 12, "observation": {"q": 1}},
    )
    with pytest.raises(ValueError, match="physics_step_identity_mismatch"):
        world.scan()
    assert world.session.tick == 0 and world.session.state == "faulted"
