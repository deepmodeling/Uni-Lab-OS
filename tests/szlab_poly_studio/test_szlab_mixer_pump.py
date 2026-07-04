from __future__ import annotations

from unilabos.devices.workstation.szlab_poly_studio.s06_pump.pump import SzlabMixerPumpDevice
from unilabos.devices.workstation.szlab_poly_studio.s06_pump.sensors import S06PipelineRoute, parse_pipeline_route_specs
from tests.szlab_poly_studio.pseudo_clients.s06_pump import PseudoSzlabMixerOpcUaClient


def make_pump_device(
    client: PseudoSzlabMixerOpcUaClient | None = None,
    *,
    pipeline_routes: dict | None = None,
    robot_addition_position: int = 0,
    robot_stirrer_position: int = 0,
) -> SzlabMixerPumpDevice:
    routes = pipeline_routes or {
        (1, "aspirate"): S06PipelineRoute(control_valve=11, absolute_position=21),
        (1, "dispense"): S06PipelineRoute(control_valve=12, absolute_position=22),
        (1, "air"): S06PipelineRoute(control_valve=13, absolute_position=23),
        (2, "aspirate"): S06PipelineRoute(control_valve=0, absolute_position=0),
        (2, "dispense"): S06PipelineRoute(control_valve=0, absolute_position=0),
        (2, "air"): S06PipelineRoute(control_valve=0, absolute_position=0),
    }
    return SzlabMixerPumpDevice(
        url="opc.tcp://127.0.0.1:0/unused",
        timeout=0.05,
        pipeline_routes=routes,
        robot_addition_position=robot_addition_position,
        robot_stirrer_position=robot_stirrer_position,
        opcua_client=client or PseudoSzlabMixerOpcUaClient(),
    )


def test_szlab_mixer_pump_rejects_invalid_process_index():
    device = make_pump_device()
    result = device.run_solvent_addition(pump=4, volume=1)
    assert result["success"] is False
    assert "1、2 或 3" in result["message"]


def test_szlab_mixer_pump_rejects_non_positive_volume():
    device = make_pump_device()
    result = device.run_solvent_addition(pump=1, volume=0)
    assert result["success"] is False
    assert "体积" in result["message"]


def test_szlab_mixer_pump_rejects_when_not_allowed():
    client = PseudoSzlabMixerOpcUaClient({"S06允许加工": False})
    device = make_pump_device(client)
    result = device.run_solvent_addition(pump=1, volume=5)
    assert result["success"] is False
    assert "允许加工超时" in result["message"]


def test_szlab_mixer_pump_run_solvent_addition_writes_expected_variables():
    client = PseudoSzlabMixerOpcUaClient()
    device = make_pump_device(client)

    result = device.run_solvent_addition(pump=1, volume=5, skip_robot=True)

    assert result["success"] is True
    assert ("S06工艺选择", 1) in client.writes
    assert ("S06_1号溶液添加量", 5) in client.writes
    assert ("S06参数写入完成", True) in client.writes
    assert ("S06参数写入完成", False) in client.writes


def test_szlab_mixer_pump_transfer_liquid_uses_published_s06_process_variables():
    client = PseudoSzlabMixerOpcUaClient()
    device = make_pump_device(client)

    result = device.transfer_liquid(pump=1, volume=5, direction="aspirate", pipeline="aspirate")

    assert result["success"] is True
    assert ("S06工艺选择", 1) in client.writes
    assert ("S06_1号溶液添加量", 5) in client.writes
    assert not any(name.startswith("S06注射泵") for name, _value in client.writes)
    assert ("S06参数写入完成", True) in client.writes
    assert ("S06参数写入完成", False) in client.writes


def test_szlab_mixer_pump_waits_for_new_completion_cycle_when_done_is_stale():
    client = PseudoSzlabMixerOpcUaClient({"S06加工完成": True})
    device = make_pump_device(client)

    result = device.run_solvent_addition(pump=1, volume=10, skip_robot=True)

    assert result["success"] is True
    assert client.wait_equal_calls == [("S06加工完成", False), ("S06加工完成", True)]


def test_szlab_mixer_pump_run_solvent_addition_writes_both_solution_amounts():
    client = PseudoSzlabMixerOpcUaClient()
    device = make_pump_device(client)

    result = device.run_solvent_addition(
        pump=3,
        volume=10,
        volume_pump_1=8,
        volume_pump_2=6,
        skip_robot=True,
    )

    assert result["success"] is True
    assert ("S06工艺选择", 3) in client.writes
    assert ("S06_1号溶液添加量", 8) in client.writes
    assert ("S06_2号溶液添加量", 6) in client.writes


def test_szlab_mixer_pump_run_solvent_addition_fails_when_not_ready():
    client = PseudoSzlabMixerOpcUaClient({"S06准备信号": False})
    device = make_pump_device(client)

    result = device.run_solvent_addition(pump=1, skip_robot=True)

    assert result["success"] is False
    assert "未就绪" in result["message"]


def test_szlab_mixer_pump_run_solvent_addition_checks_storage_bottle_present():
    client = PseudoSzlabMixerOpcUaClient({"传感器状态_上位机[4].NO[12]": False})
    device = make_pump_device(client)

    result = device.run_solvent_addition(pump=1, skip_robot=True)

    assert result["success"] is False
    assert "储液瓶 1" in result["message"]


def test_szlab_mixer_pump_transport_beaker_writes_robot_positions():
    client = PseudoSzlabMixerOpcUaClient()
    device = make_pump_device(client, robot_addition_position=12, robot_stirrer_position=3)

    result = device.run_solvent_addition(
        pump=1,
        volume=1,
        skip_robot=False,
    )

    assert result["success"] is True
    assert ("S03_1取料编号", 12) in client.writes
    assert ("S03_1放料编号", 3) in client.writes


def test_szlab_mixer_pump_loads_pipeline_route_specs_from_graph_config():
    specs = [
        {"pump": 1, "pipeline": "aspirate", "control_valve": 11, "absolute_position": 21},
        {"pump": 1, "pipeline": "dispense", "control_valve": 12, "absolute_position": 22},
    ]
    routes = parse_pipeline_route_specs(specs)
    device = make_pump_device(PseudoSzlabMixerOpcUaClient(), pipeline_routes=routes)

    assert routes[(1, "aspirate")].control_valve == 11
    assert routes[(1, "aspirate")].absolute_position == 21
