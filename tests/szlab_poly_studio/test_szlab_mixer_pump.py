from __future__ import annotations

import inspect
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from unilabos.registry.ast_registry_scanner import scan_directory
from unilabos.devices.workstation.szlab_poly_studio.s06_pump.pump import SzlabMixerPumpDevice
from unilabos.devices.workstation.szlab_poly_studio.s06_pump.sensors import S06PipelineRoute, parse_pipeline_route_specs
from tests.szlab_poly_studio.pseudo_clients.s06_pump import PseudoSzlabMixerOpcUaClient


def make_pump_device(
    client: PseudoSzlabMixerOpcUaClient | None = None,
    *,
    pipeline_routes: dict | None = None,
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
        opcua_client=client or PseudoSzlabMixerOpcUaClient(),
    )


def test_szlab_mixer_pump_actions_use_process_parameter_name():
    root = Path("unilabos/devices/workstation/szlab_poly_studio/s06_pump")
    with ThreadPoolExecutor(max_workers=2) as executor:
        result = scan_directory(root, python_path=Path(".").resolve(), executor=executor)

    actions = result["devices"]["szlab_mixer_pump"]["actions"]
    transfer_params = {param["name"] for param in actions["transfer_liquid"]["params"]}
    solvent_params = {param["name"] for param in actions["run_solvent_addition"]["params"]}

    assert "process" in transfer_params
    assert "process" in solvent_params
    assert "pump" not in transfer_params
    assert "pump" not in solvent_params
    assert "skip_robot" not in solvent_params


def test_szlab_mixer_pump_constructor_has_no_internal_robot_position_config():
    constructor_params = set(inspect.signature(SzlabMixerPumpDevice).parameters)

    assert "robot_addition_position" not in constructor_params
    assert "robot_stirrer_position" not in constructor_params


def test_szlab_mixer_pump_can_use_shared_plc_gateway():
    gateway = PseudoSzlabMixerOpcUaClient()
    device = SzlabMixerPumpDevice(
        url="opc.tcp://127.0.0.1:0/unused",
        timeout=0.05,
        pipeline_routes={
            (1, "aspirate"): S06PipelineRoute(control_valve=11, absolute_position=21),
            (1, "dispense"): S06PipelineRoute(control_valve=12, absolute_position=22),
            (1, "air"): S06PipelineRoute(control_valve=13, absolute_position=23),
            (2, "aspirate"): S06PipelineRoute(control_valve=0, absolute_position=0),
            (2, "dispense"): S06PipelineRoute(control_valve=0, absolute_position=0),
            (2, "air"): S06PipelineRoute(control_valve=0, absolute_position=0),
        },
        use_plc_gateway=True,
    )
    device.set_plc_gateway(gateway)

    result = device.run_solvent_addition(process=1, volume=5)

    assert result["success"] is True
    assert ("S06工艺选择", 1) in gateway.writes
    assert ("S06参数写入完成", True) in gateway.writes


def test_szlab_mixer_pump_rejects_invalid_process_index():
    device = make_pump_device()
    result = device.run_solvent_addition(process=4, volume=1)
    assert result["success"] is False
    assert "1、2 或 3" in result["message"]


def test_szlab_mixer_pump_rejects_non_positive_volume():
    device = make_pump_device()
    result = device.run_solvent_addition(process=1, volume=0)
    assert result["success"] is False
    assert "体积" in result["message"]


def test_szlab_mixer_pump_rejects_when_not_allowed():
    client = PseudoSzlabMixerOpcUaClient({"S06允许加工": False})
    device = make_pump_device(client)
    result = device.run_solvent_addition(process=1, volume=5)
    assert result["success"] is False
    assert "允许加工超时" in result["message"]


def test_szlab_mixer_pump_run_solvent_addition_writes_expected_variables():
    client = PseudoSzlabMixerOpcUaClient()
    device = make_pump_device(client)

    result = device.run_solvent_addition(process=1, volume=5)

    assert result["success"] is True
    assert ("S06工艺选择", 1) in client.writes
    assert ("S06_1号溶液添加量", 5) in client.writes
    assert ("S06参数写入完成", True) in client.writes
    assert ("S06参数写入完成", False) in client.writes


def test_szlab_mixer_pump_transfer_liquid_uses_published_s06_process_variables():
    client = PseudoSzlabMixerOpcUaClient()
    device = make_pump_device(client)

    result = device.transfer_liquid(process=1, volume=5, direction="aspirate", pipeline="aspirate")

    assert result["success"] is True
    assert client.wait_equal_calls[:2] == [("S06允许加工", True), ("S06准备信号", True)]
    assert ("S06工艺选择", 1) in client.writes
    assert ("S06_1号溶液添加量", 5) in client.writes
    assert not any(name.startswith("S06注射泵") for name, _value in client.writes)
    assert ("S06参数写入完成", True) in client.writes
    assert ("S06参数写入完成", False) in client.writes


def test_szlab_mixer_pump_waits_for_new_completion_cycle_when_done_is_stale():
    client = PseudoSzlabMixerOpcUaClient({"S06加工完成": True})
    device = make_pump_device(client)

    result = device.run_solvent_addition(process=1, volume=10)

    assert result["success"] is True
    assert client.wait_equal_calls == [
        ("S06允许加工", True),
        ("S06准备信号", True),
        ("S06加工完成", False),
        ("S06加工完成", True),
    ]


def test_szlab_mixer_pump_run_solvent_addition_writes_both_solution_amounts():
    client = PseudoSzlabMixerOpcUaClient()
    device = make_pump_device(client)

    result = device.run_solvent_addition(
        process=3,
        volume=10,
        volume_pump_1=8,
        volume_pump_2=6,
    )

    assert result["success"] is True
    assert ("S06工艺选择", 3) in client.writes
    assert ("S06_1号溶液添加量", 8) in client.writes
    assert ("S06_2号溶液添加量", 6) in client.writes


def test_szlab_mixer_pump_run_solvent_addition_fails_when_not_ready():
    client = PseudoSzlabMixerOpcUaClient({"S06准备信号": False})
    device = make_pump_device(client)

    result = device.run_solvent_addition(process=1)

    assert result["success"] is False
    assert "准备信号超时" in result["message"]
    assert client.wait_equal_calls == [("S06允许加工", True), ("S06准备信号", True)]


def test_szlab_mixer_pump_resets_params_only_after_process_complete():
    client = PseudoSzlabMixerOpcUaClient()
    device = make_pump_device(client)

    result = device.transfer_liquid(process=1, volume=5, direction="aspirate", pipeline="aspirate")

    assert result["success"] is True
    done_wait = client.events.index(("wait_new_cycle_done", "S06加工完成"))
    reset_written = client.events.index(("write", "S06参数写入完成", False))
    assert reset_written > done_wait


def test_szlab_mixer_pump_does_not_reset_params_before_process_complete_timeout():
    client = PseudoSzlabMixerOpcUaClient()
    client.force_done_timeout = True
    device = make_pump_device(client)

    result = device.transfer_liquid(process=1, volume=5, direction="aspirate", pipeline="aspirate")

    assert result["success"] is False
    assert "加工完成等待超时" in result["message"]
    assert ("wait_new_cycle_done", "S06加工完成") in client.events
    assert ("write", "S06参数写入完成", False) not in client.events
    assert ("write", "S06工艺选择", 0) not in client.events


def test_szlab_mixer_pump_run_solvent_addition_checks_storage_bottle_present():
    client = PseudoSzlabMixerOpcUaClient({"传感器状态_上位机[4].NO[12]": False})
    device = make_pump_device(client)

    result = device.run_solvent_addition(process=1)

    assert result["success"] is False
    assert "储液瓶 1" in result["message"]


def test_szlab_mixer_pump_skip_level_check_bypasses_storage_bottle_sensor():
    client = PseudoSzlabMixerOpcUaClient({"传感器状态_上位机[4].NO[12]": False})
    device = make_pump_device(client)

    result = device.run_solvent_addition(process=1, volume=1, skip_level_check=True)

    assert result["success"] is True
    assert "储液瓶" not in result["message"]


def test_szlab_mixer_pump_run_solvent_addition_never_writes_legacy_robot_variables():
    client = PseudoSzlabMixerOpcUaClient()
    device = make_pump_device(client)

    result = device.run_solvent_addition(
        process=1,
        volume=1,
        skip_level_check=True,
    )

    assert result["success"] is True
    assert not any(name == "S03_1取料编号" for name, _value in client.writes)
    assert not any(name == "S03_1放料编号" for name, _value in client.writes)


def test_szlab_mixer_pump_loads_pipeline_route_specs_from_graph_config():
    specs = [
        {"pump": 1, "pipeline": "aspirate", "control_valve": 11, "absolute_position": 21},
        {"pump": 1, "pipeline": "dispense", "control_valve": 12, "absolute_position": 22},
    ]
    routes = parse_pipeline_route_specs(specs)
    device = make_pump_device(PseudoSzlabMixerOpcUaClient(), pipeline_routes=routes)

    assert routes[(1, "aspirate")].control_valve == 11
    assert routes[(1, "aspirate")].absolute_position == 21
