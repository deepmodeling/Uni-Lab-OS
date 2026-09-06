from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from unilabos.devices.workstation.szlab_poly_studio.s09_pipetting_station.pipetting_station import (
    SzlabMixerPipettingStationDevice,
)
from unilabos.devices.workstation.szlab_poly_studio.s09_pipetting_station.sensors import (
    S09_PROCESS_LABELS,
    s09_remaining_volume_var,
    validate_process,
)
from unilabos.devices.workstation.szlab_poly_studio.s12_robot.robot import SzlabMixerRobotDevice
from unilabos.registry.ast_registry_scanner import scan_directory
from scripts.run_workflow_local import (
    RuntimeConfig,
    RuntimeDeviceFactoryConfig,
    RuntimeOpcSnapshotConfig,
    WorkflowLogger,
    WorkflowNode,
    run_nodes,
)
from tests.szlab_poly_studio.pseudo_clients.s09_pipetting import PseudoSzlabS09OpcUaClient


def make_pipetting_device(
    client: PseudoSzlabS09OpcUaClient | None = None,
    **kwargs,
) -> SzlabMixerPipettingStationDevice:
    return SzlabMixerPipettingStationDevice(
        url="opc.tcp://127.0.0.1:0/unused",
        opcua_client=client or PseudoSzlabS09OpcUaClient(),
        **kwargs,
    )


def test_s09_pipetting_station_is_ast_scannable_from_own_package():
    root = Path("unilabos/devices/workstation/szlab_poly_studio/s09_pipetting_station")
    with ThreadPoolExecutor(max_workers=2) as executor:
        result = scan_directory(root, python_path=Path(".").resolve(), executor=executor)

    assert set(result["devices"]) == {"szlab_mixer_pipetting_station"}
    actions = set(result["devices"]["szlab_mixer_pipetting_station"]["actions"])
    assert {
        "check_home_position",
        "read_home_positions",
        "read_allow_process",
        "run_process",
        "add_liquid",
        "add_liquid_with_reusable_tip",
        "measure_density",
        "add_liquid_to_beaker",
        "run_liquid_workflow",
        "set_liquid_bottle_remaining_volume",
        "initialize_liquid_bottle_remaining_volumes",
        "read_balance",
        "initialize_reusable_tip_inventory",
        "get_reusable_tip_status",
        "get_pipetting_status",
    }.issubset(actions)
    assert "go_to_safe_position" not in actions


def test_s09_process_labels_cover_liquid_processes_5_to_9():
    assert set(S09_PROCESS_LABELS) == set(range(5, 10))
    assert S09_PROCESS_LABELS[5] == "取 TIP"
    assert S09_PROCESS_LABELS[7].startswith("液体瓶取液")
    assert validate_process(9) == 9

    device = make_pipetting_device()
    result = device.run_process(process=1)

    assert result["success"] is False
    assert "5-9" in result["message"]


def test_s09_run_process_writes_expected_variables_and_waits_done():
    client = PseudoSzlabS09OpcUaClient({"S09液体瓶3剩余液量": 100.0})
    device = make_pipetting_device(client)

    result = device.run_process(
        process=7,
        tip_box_index=2,
        tip_index=12,
        liquid_bottle_index=3,
        aspirate_volume=50,
    )

    assert result["success"] is True
    assert [item["message"] for item in result["logs"]] == [
        "S09 液体瓶 3 剩余液量校验通过",
        f"S09 工艺 7 参数写入开始：{S09_PROCESS_LABELS[7]}",
        "S09 工艺 7 参数写入完成",
        "S09 参数写入完成信号已置位，将保持至工艺结束",
        "等待 S09 工艺 7 完成",
        "S09 工艺 7 完成信号已确认",
        "S09 液体瓶 3 剩余液量已更新：100 -> 99.995 mL",
        "S09 工艺参数清零开始",
        "S09 工艺参数清零完成",
    ]
    assert ("S09TIP盒工位编号", 2) in client.writes
    assert ("S09TIP编号", 12) in client.writes
    assert ("S09液体瓶编号", 3) in client.writes
    assert ("S09抽液量", 50) in client.writes
    assert ("S09工艺选择", 7) in client.writes
    assert client.pulses == []
    params_written_on_index = client.events.index(("write", "S09参数写入完成", True))
    done_event_index = client.events.index(("wait_equal", "S09工艺完成", 7))
    params_written_off_index = client.events.index(("write", "S09参数写入完成", False))
    assert params_written_on_index < done_event_index < params_written_off_index
    assert client.wait_equal_calls == [("S09工艺完成", 7)]
    assert client.writes[-8:] == [
        ("S09液体瓶3剩余液量", 99.995),
        ("S09参数写入完成", False),
        ("S09工艺选择", 0),
        ("S09TIP盒工位编号", 0),
        ("S09TIP编号", 0),
        ("S09液体瓶编号", 0),
        ("S09抽液量", 0),
        ("S09放液量", 0),
    ]
    assert result["data"]["clear_process_params"]["success"] is True
    clear_event_index = client.events.index(("write", "S09工艺选择", 0))
    assert done_event_index < clear_event_index


def test_s09_run_process_waits_for_new_completion_cycle_when_done_is_stale():
    client = PseudoSzlabS09OpcUaClient({"S09工艺完成": 5})
    device = make_pipetting_device(client)

    result = device.run_process(process=5, tip_box_index=1, tip_index=1)

    assert result["success"] is True
    assert client.wait_equal_calls == [("S09工艺完成", 0), ("S09工艺完成", 5)]


def test_s09_run_process_requires_selected_liquid_bottle_sensor():
    client = PseudoSzlabS09OpcUaClient(
        {
            "S09液体瓶3剩余液量": 100.0,
            "传感器状态_上位机[4].NO[9]": False,
        }
    )
    device = make_pipetting_device(client)

    result = device.run_process(
        process=7,
        tip_box_index=1,
        tip_index=1,
        liquid_bottle_index=3,
        aspirate_volume=50,
    )

    assert result["success"] is False
    assert result["status"] == "rejected"
    assert result["sensor_precheck"]["mismatches"]["传感器状态_上位机[4].NO[9]"] == {
        "expected": True,
        "actual": False,
    }
    assert client.writes == []


def test_s09_run_process_reports_verification_failed_when_material_disappears():
    class MaterialRemovedClient(PseudoSzlabS09OpcUaClient):
        def __init__(self):
            super().__init__({"S09液体瓶1剩余液量": 100.0})
            self.sensor_wait_count = 0

        def wait_sensor_conditions(self, conditions, interval=0.2, context=None):
            self.sensor_wait_count += 1
            if self.sensor_wait_count == 2:
                self.values["传感器状态_上位机[4].NO[7]"] = False
            return super().wait_sensor_conditions(
                conditions,
                interval=interval,
                context=context,
            )

    client = MaterialRemovedClient()
    device = make_pipetting_device(client)

    result = device.run_process(
        process=7,
        tip_box_index=1,
        tip_index=1,
        liquid_bottle_index=1,
        aspirate_volume=50,
    )

    assert result["success"] is False
    assert result["status"] == "verification_failed"
    assert "在位验证失败" in result["message"]
    assert ("S09工艺选择", 0) in client.writes


def test_s09_add_liquid_uses_virtual_state_instead_of_target_station_sensor():
    client = PseudoSzlabS09OpcUaClient(
        {
            "S09液体瓶1剩余液量": 100.0,
            "传感器状态_上位机[4].NO[9]": False,
        }
    )
    device = make_pipetting_device(client)

    result = device.add_liquid(
        take_tip_box_index=1,
        release_tip_box_index=2,
        tip_index=1,
        liquid_bottle_index=1,
        station=3,
        aspirate_volume=50,
        dispense_volume=50,
    )

    assert result["success"] is True
    assert "传感器状态_上位机[4].NO[9]" not in client.reads


def test_s09_add_liquid_requires_release_tip_box_before_first_process():
    client = PseudoSzlabS09OpcUaClient(
        {
            "S09液体瓶1剩余液量": 100.0,
            "传感器状态_上位机[4].NO[6]": False,
        }
    )
    device = make_pipetting_device(client)

    result = device.add_liquid(
        take_tip_box_index=1,
        release_tip_box_index=2,
        tip_index=1,
        liquid_bottle_index=1,
        station=1,
        aspirate_volume=50,
        dispense_volume=50,
    )

    assert result["success"] is False
    assert result["sensor_precheck"]["mismatches"]["传感器状态_上位机[4].NO[6]"]["actual"] is False
    assert client.writes == []


def test_s09_add_liquid_runs_plc_process_sequence_5_7_8_6():
    client = PseudoSzlabS09OpcUaClient({"S09液体瓶4剩余液量": 100.0})
    device = make_pipetting_device(client)

    result = device.add_liquid(
        take_tip_box_index=1,
        release_tip_box_index=2,
        tip_index=2,
        liquid_bottle_index=4,
        station=3,
        aspirate_volume=20,
        dispense_volume=18,
    )

    assert result["success"] is True
    process_writes = [value for name, value in client.writes if name == "S09工艺选择"]
    assert [value for value in process_writes if value != 0] == [5, 7, 8, 6]
    assert process_writes == [5, 0, 7, 0, 8, 0, 6, 0]
    tip_box_writes = [value for name, value in client.writes if name == "S09TIP盒工位编号"]
    assert [value for value in tip_box_writes if value != 0] == [1, 1, 1, 2]
    assert [step["step"] for step in result["steps"]] == [
        "从 TIP盒1 取 TIP",
        "液体瓶取液",
        "烧杯放液",
        "向 TIP盒2 放 TIP",
    ]
    assert [step["data"]["process"] for step in result["steps"]] == [5, 7, 8, 6]
    assert client.wait_equal_calls == [
        ("S09允许加工", True),
        ("S09工艺完成", 5),
        ("S09允许加工", True),
        ("S09工艺完成", 7),
        ("S09允许加工", True),
        ("S09工艺完成", 8),
        ("S09天平读数稳定", True),
        ("S09允许加工", True),
        ("S09工艺完成", 6),
    ]


def test_s09_reusable_tip_action_uses_box_one_then_reuses_from_box_two(tmp_path):
    client = PseudoSzlabS09OpcUaClient({"S09液体瓶4剩余液量": 100.0})
    device = make_pipetting_device(
        client,
        tip_reuse_state_path=str(tmp_path / "tip_state.json"),
    )
    assert device.initialize_reusable_tip_inventory()["success"] is True

    first = device.add_liquid_with_reusable_tip(
        liquid_station_index=4,
        solvent_batch_id="batch-a",
        volume=20,
    )
    second = device.add_liquid_with_reusable_tip(
        liquid_station_index=4,
        solvent_batch_id="batch-a",
        volume=20,
    )

    assert first["success"] is True
    assert [step["data"]["tip_box_index"] for step in first["steps"]] == [1, 1, 1, 2]
    assert first["data"]["tip_reuse"]["tip_index"] == 1
    assert first["data"]["tip_reuse"]["use_count"] == 1
    assert second["success"] is True
    assert [step["data"]["tip_box_index"] for step in second["steps"]] == [2, 2, 2, 2]
    assert second["data"]["tip_reuse"]["tip_index"] == 1
    assert second["data"]["tip_reuse"]["use_count"] == 2


def test_s09_reuses_bound_tip_without_allocating_density_tips(tmp_path):
    client = PseudoSzlabS09OpcUaClient({"S09液体瓶1剩余液量": 100.0})
    device = make_pipetting_device(
        client,
        tip_reuse_state_path=str(tmp_path / "tip_state.json"),
    )
    device.initialize_reusable_tip_inventory()

    first = device.add_liquid_with_reusable_tip(
        liquid_station_index=1,
        solvent_batch_id="batch-a",
        volume=1,
    )
    second = device.add_liquid_with_reusable_tip(
        liquid_station_index=1,
        solvent_batch_id="batch-b",
        volume=1,
    )
    reused = device.add_liquid_with_reusable_tip(
        liquid_station_index=1,
        solvent_batch_id="batch-a",
        volume=1,
    )

    assert first["data"]["tip_reuse"]["tip_index"] == 1
    assert second["data"]["tip_reuse"]["tip_index"] == 2
    assert reused["data"]["tip_reuse"]["tip_index"] == 1
    assert device.get_reusable_tip_status()["data"]["last_operation"] is None


def test_s09_can_use_single_use_tip_without_changing_reusable_binding(tmp_path):
    client = PseudoSzlabS09OpcUaClient({"S09液体瓶1剩余液量": 100.0})
    device = make_pipetting_device(
        client,
        tip_reuse_state_path=str(tmp_path / "tip_state.json"),
    )
    device.initialize_reusable_tip_inventory()

    reusable = device.add_liquid_with_reusable_tip(
        liquid_station_index=1,
        solvent_batch_id="batch-a",
        volume=1,
        reuse_tip=True,
    )
    first_single_use = device.add_liquid_with_reusable_tip(
        liquid_station_index=1,
        solvent_batch_id="batch-a",
        volume=1,
        reuse_tip=False,
    )
    second_single_use = device.add_liquid_with_reusable_tip(
        liquid_station_index=1,
        solvent_batch_id="batch-a",
        volume=1,
        reuse_tip=False,
    )
    reused = device.add_liquid_with_reusable_tip(
        liquid_station_index=1,
        solvent_batch_id="batch-a",
        volume=1,
        reuse_tip=True,
    )
    status = device.get_reusable_tip_status()["data"]

    assert [
        result["data"]["tip_reuse"]["tip_index"]
        for result in (reusable, first_single_use, second_single_use, reused)
    ] == [1, 2, 3, 1]
    assert first_single_use["data"]["tip_reuse"]["reuse_tip"] is False
    assert first_single_use["data"]["tip_reuse"]["single_use"] is True
    assert status["tips"]["2"]["status"] == "exhausted"
    assert status["tips"]["3"]["status"] == "exhausted"
    assert (
        status["solvents"]["S09-STATION-1:BATCH:batch-a"]["active_tip_index"]
        == 1
    )


def test_s09_reusable_tip_action_allocates_by_batch_and_station(tmp_path):
    client = PseudoSzlabS09OpcUaClient(
        {
            "S09液体瓶1剩余液量": 100.0,
            "S09液体瓶2剩余液量": 100.0,
        }
    )
    device = make_pipetting_device(
        client,
        tip_reuse_state_path=str(tmp_path / "tip_state.json"),
    )
    assert device.initialize_reusable_tip_inventory()["success"] is True

    results = [
        device.add_liquid_with_reusable_tip(
            liquid_station_index=station,
            solvent_batch_id=batch,
            volume=20,
        )
        for batch, station in [
            ("batch-1", 1),
            ("batch-1", 2),
            ("batch-2", 1),
            ("batch-2", 2),
            ("batch-1", 1),
        ]
    ]

    assert all(result["success"] for result in results)
    assert [
        result["data"]["tip_reuse"]["tip_index"] for result in results
    ] == [1, 2, 3, 4, 1]
    assert results[-1]["data"]["tip_reuse"]["take_tip_box_index"] == 2


def test_s09_reusable_tip_action_replaces_tip_at_limit(tmp_path):
    client = PseudoSzlabS09OpcUaClient({"S09液体瓶1剩余液量": 100.0})
    device = make_pipetting_device(
        client,
        tip_reuse_state_path=str(tmp_path / "tip_state.json"),
        tip_max_use_count=2,
    )
    device.initialize_reusable_tip_inventory()

    results = [
        device.add_liquid_with_reusable_tip(
            liquid_station_index=1,
            solvent_batch_id="batch-a",
            volume=10,
        )
        for _index in range(3)
    ]
    status = device.get_reusable_tip_status()["data"]

    assert all(result["success"] for result in results)
    assert [result["data"]["tip_reuse"]["tip_index"] for result in results] == [1, 1, 2]
    assert results[2]["data"]["tip_reuse"]["take_tip_box_index"] == 1
    assert status["tips"]["1"]["status"] == "exhausted"
    assert status["tips"]["2"]["status"] == "bound"


def test_s09_reusable_tip_action_quarantines_tip_after_uncertain_take(
    tmp_path,
    monkeypatch,
):
    device = make_pipetting_device(
        tip_reuse_state_path=str(tmp_path / "tip_state.json"),
    )
    device.initialize_reusable_tip_inventory()

    def fake_run_process(*, process, **_kwargs):
        if process == 5:
            return {"success": True, "data": {"process": 5}}
        return {
            "success": False,
            "message": "吸液失败",
            "data": {"process": process},
        }

    monkeypatch.setattr(device, "run_process", fake_run_process)

    result = device.add_liquid_with_reusable_tip(
        liquid_station_index=1,
        solvent_batch_id="batch-a",
        volume=10,
    )
    status = device.get_reusable_tip_status()["data"]

    assert result["success"] is False
    assert result["tip_reuse"]["status"] == "unknown"
    assert status["tips"]["1"]["status"] == "unknown"
    assert status["tips"]["2"]["status"] == "unused"
    assert status["solvents"]["S09-STATION-1:BATCH:batch-a"]["status"] == "unknown"


def test_s09_single_use_tip_is_released_when_take_does_not_start(
    tmp_path,
    monkeypatch,
):
    device = make_pipetting_device(
        tip_reuse_state_path=str(tmp_path / "tip_state.json"),
    )
    device.initialize_reusable_tip_inventory()

    monkeypatch.setattr(
        device,
        "run_process",
        lambda **_kwargs: {"success": False, "message": "取 TIP 失败"},
    )

    result = device.add_liquid_with_reusable_tip(
        liquid_station_index=1,
        solvent_batch_id="batch-a",
        volume=10,
        reuse_tip=False,
    )
    status = device.get_reusable_tip_status()["data"]

    assert result["success"] is False
    assert result["tip_reuse"]["status"] == "unused"
    assert status["tips"]["1"]["status"] == "unused"


def test_s09_single_use_tip_is_quarantined_after_uncertain_take(
    tmp_path,
    monkeypatch,
):
    device = make_pipetting_device(
        tip_reuse_state_path=str(tmp_path / "tip_state.json"),
    )
    device.initialize_reusable_tip_inventory()

    def fake_run_process(*, process, **_kwargs):
        if process == 5:
            return {"success": True, "data": {"process": 5}}
        return {
            "success": False,
            "message": "吸液失败",
            "data": {"process": process},
        }

    monkeypatch.setattr(device, "run_process", fake_run_process)

    result = device.add_liquid_with_reusable_tip(
        liquid_station_index=1,
        solvent_batch_id="batch-a",
        volume=10,
        reuse_tip=False,
    )
    status = device.get_reusable_tip_status()["data"]

    assert result["success"] is False
    assert result["tip_reuse"]["status"] == "unknown"
    assert status["tips"]["1"]["status"] == "unknown"
    assert status["solvents"] == {}


def test_s09_reusable_tip_action_does_not_reserve_a_density_tip(tmp_path):
    client = PseudoSzlabS09OpcUaClient({"S09液体瓶1剩余液量": 100.0})
    device = make_pipetting_device(
        client,
        tip_reuse_state_path=str(tmp_path / "tip_state.json"),
    )
    device.initialize_reusable_tip_inventory(used_tip_count=95)

    result = device.add_liquid_with_reusable_tip(
        liquid_station_index=1,
        solvent_batch_id="batch-a",
        volume=1,
    )

    assert result["success"] is True
    assert result["data"]["tip_reuse"]["tip_index"] == 96
    assert [step["data"]["process"] for step in result["steps"]] == [5, 7, 8, 6]


def test_s09_reusable_tip_action_serializes_concurrent_plc_transfers(
    tmp_path,
    monkeypatch,
):
    device = make_pipetting_device(
        tip_reuse_state_path=str(tmp_path / "tip_state.json"),
    )
    device.initialize_reusable_tip_inventory()
    counter_lock = threading.Lock()
    active_count = 0
    max_active_count = 0

    def fake_run_process(*, process, **_kwargs):
        nonlocal active_count, max_active_count
        with counter_lock:
            active_count += 1
            max_active_count = max(max_active_count, active_count)
        time.sleep(0.02)
        with counter_lock:
            active_count -= 1
        data = {"process": process}
        if process == 9:
            data["balance_reading"] = -1.0
        return {"success": True, "data": data}

    monkeypatch.setattr(device, "run_process", fake_run_process)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda position: device.add_liquid_with_reusable_tip(
                    liquid_station_index=position,
                    solvent_batch_id=f"batch-{position}",
                    volume=10,
                ),
                (1, 2),
            )
        )

    assert all(result["success"] for result in results)
    assert max_active_count == 1


def test_s09_reusable_tip_action_only_runs_addition(tmp_path):
    client = PseudoSzlabS09OpcUaClient({"S09液体瓶3剩余液量": 100.0})
    device = make_pipetting_device(
        client,
        tip_reuse_state_path=str(tmp_path / "tip_state.json"),
    )
    device.initialize_reusable_tip_inventory()

    result = device.add_liquid_with_reusable_tip(
        liquid_station_index=3,
        solvent_batch_id="batch-addition",
        volume=1,
        volume_unit="mL",
    )

    assert result["success"] is True
    assert [step["data"]["process"] for step in result["steps"]] == [5, 7, 8, 6]
    assert "density" not in result["data"]
    assert result["data"]["tip_reuse"]["solvent_batch_id"] == "batch-addition"
    assert client.values["S09液体瓶3剩余液量"] == 99.0


def test_s09_measure_density_runs_without_liquid_addition(tmp_path):
    client = PseudoSzlabS09OpcUaClient({
        "S09抽液天平读数[0]": -1.58,
        "S09放液天平读数[0]": 1.58,
    })
    device = make_pipetting_device(
        client,
        tip_reuse_state_path=str(tmp_path / "tip_state.json"),
    )
    device.initialize_reusable_tip_inventory()

    result = device.measure_density(density_volume=2, volume_unit="mL")

    assert result["success"] is True
    assert [step["data"]["process"] for step in result["steps"]] == [5, 9, 6]
    assert result["data"]["density_volume_ml"] == 2.0
    assert result["data"]["density"] == 0.79
    assert result["data"]["densities"] == [0.79, 0.79]
    assert result["data"]["density_tip"]["status"] == "exhausted"
    assert result["display_message"] == "S09 密度结果：0.79 g/mL（抽液、放液各测 1 次，共 2 个结果）"


def test_s09_multiple_liquids_only_run_addition_processes(tmp_path):
    client = PseudoSzlabS09OpcUaClient({
        "S09液体瓶1剩余液量": 100.0,
        "S09液体瓶2剩余液量": 100.0,
    })
    device = make_pipetting_device(client, tip_reuse_state_path=str(tmp_path / "tip_state.json"))
    device.initialize_reusable_tip_inventory()

    result = device.add_liquid_with_reusable_tip(
        liquid_count=2,
        liquid_additions=[
            {
                "liquid_station_index": 1,
                "solvent_batch_id": "water",
                "volume": 1,
                "reuse_tip": True,
            },
            {
                "liquid_station_index": 2,
                "solvent_batch_id": "ethanol",
                "volume": 2,
                "reuse_tip": False,
            },
        ],
    )

    assert result["success"] is True
    assert result["data"]["liquid_count"] == 2
    assert result["message"] == "S09 已完成 2 次加液"
    assert (
        result["data"]["liquid_results"][0]["data"]["tip_reuse"]["reuse_tip"]
        is True
    )
    assert (
        result["data"]["liquid_results"][1]["data"]["tip_reuse"]["reuse_tip"]
        is False
    )
    assert [step["data"]["process"] for item in result["data"]["liquid_results"] for step in item["steps"]] == [
        5, 7, 8, 6,
        5, 7, 8, 6,
    ]


def test_s09_combined_action_can_initialize_tip_inventory_before_execution(tmp_path):
    client = PseudoSzlabS09OpcUaClient({
        "S09液体瓶1剩余液量": 100.0,
        "S09抽液天平读数[0]": -1.0,
        "S09放液天平读数[0]": 1.0,
    })
    device = make_pipetting_device(client, tip_reuse_state_path=str(tmp_path / "tip_state.json"))
    device.initialize_reusable_tip_inventory(known_bindings={"old-solvent": 1})

    result = device.add_liquid_with_reusable_tip(
        liquid_station_index=1,
        solvent_batch_id="water",
        volume=1,
        initialize_tip_inventory=True,
        initial_used_tip_count=2,
    )
    status = device.get_reusable_tip_status()["data"]

    assert result["success"] is True
    assert "old-solvent" not in status["solvents"]
    assert result["data"]["tip_reuse"]["tip_index"] == 3
    assert "density_tip" not in result["data"]


def test_s09_measure_density_passes_count_and_averages_both_reading_arrays(tmp_path):
    client = PseudoSzlabS09OpcUaClient({
        "S09抽液天平读数[0]": -1.0,
        "S09抽液天平读数[1]": -1.2,
        "S09放液天平读数[0]": 0.8,
        "S09放液天平读数[1]": 1.0,
    })
    device = make_pipetting_device(
        client,
        tip_reuse_state_path=str(tmp_path / "tip_state.json"),
    )
    device.initialize_reusable_tip_inventory()

    result = device.measure_density(
        density_volume=1,
        density_measurement_count=2,
        volume_unit="mL",
    )

    assert result["success"] is True
    assert result["data"]["net_masses"] == [-1.0, -1.2, 0.8, 1.0]
    assert result["data"]["densities"] == [1.0, 1.2, 0.8, 1.0]
    assert result["data"]["density"] == 1.0
    assert client.writes.count(("S09测密度次数", 2)) == 1


def test_s09_measure_density_rejects_invalid_density_count(tmp_path):
    device = make_pipetting_device(
        tip_reuse_state_path=str(tmp_path / "tip_state.json"),
    )

    result = device.measure_density(density_measurement_count=11)

    assert result["success"] is False
    assert "1-10" in result["message"]


def test_s09_measure_density_rejects_volume_over_single_transfer_limit(tmp_path):
    device = make_pipetting_device(
        tip_reuse_state_path=str(tmp_path / "tip_state.json"),
    )

    result = device.measure_density(
        density_volume=5001,
        volume_unit="uL",
    )

    assert result["success"] is False
    assert "不能超过 5000 uL" in result["message"]


def test_s09_reusable_tip_action_requires_explicit_solvent_batch(tmp_path):
    device = make_pipetting_device(
        tip_reuse_state_path=str(tmp_path / "tip_state.json"),
    )

    result = device.add_liquid_with_reusable_tip(liquid_station_index=1, volume=1)

    assert result["success"] is False
    assert "solvent_batch_id" in result["message"]


def test_s09_add_liquid_writes_frontend_remaining_volume_params_before_process():
    client = PseudoSzlabS09OpcUaClient()
    device = make_pipetting_device(client)

    result = device.add_liquid(
        take_tip_box_index=1,
        release_tip_box_index=2,
        tip_index=2,
        liquid_bottle_index=2,
        station=1,
        aspirate_volume=1,
        dispense_volume=1,
        S09液体瓶1剩余液量=10.0,
        S09液体瓶2剩余液量=20.0,
        S09液体瓶3剩余液量=30.0,
    )

    assert result["success"] is True
    assert client.writes[:3] == [
        ("S09液体瓶1剩余液量", 10.0),
        ("S09液体瓶2剩余液量", 20.0),
        ("S09液体瓶3剩余液量", 30.0),
    ]
    assert ("S09液体瓶4剩余液量", 0.0) not in client.writes
    assert ("S09液体瓶5剩余液量", 0.0) not in client.writes
    assert result["data"]["configured_remaining_volumes"] == {
        "S09液体瓶1剩余液量": 10.0,
        "S09液体瓶2剩余液量": 20.0,
        "S09液体瓶3剩余液量": 30.0,
    }
    assert client.values["S09液体瓶2剩余液量"] == 19.9999


def test_s09_add_liquid_to_beaker_exposes_business_action_for_5_7_8_6():
    client = PseudoSzlabS09OpcUaClient({"S09液体瓶2剩余液量": 100.0})
    device = make_pipetting_device(client)

    result = device.add_liquid_to_beaker(
        take_tip_box_index=1,
        release_tip_box_index=2,
        tip_index=2,
        liquid_bottle_index=2,
        station=3,
        aspirate_volume=20,
        dispense_volume=20,
    )

    assert result["success"] is True
    assert result["message"] == "S09 烧杯加液完成"
    process_writes = [value for name, value in client.writes if name == "S09工艺选择"]
    assert [value for value in process_writes if value != 0] == [5, 7, 8, 6]
    assert result["data"]["process_sequence"] == [5, 7, 8, 6]
    stable_event_index = client.events.index(("wait_equal", "S09天平读数稳定", True))
    reading_event_index = client.events.index(("read", "S09天平读数", None))
    process_8_clear_index = next(
        index
        for index, event in enumerate(client.events)
        if index > reading_event_index and event == ("write", "S09工艺选择", 0)
    )
    process_6_start_index = client.events.index(("write", "S09工艺选择", 6))
    assert stable_event_index < reading_event_index < process_8_clear_index < process_6_start_index
    assert client.reads.count("S09天平读数") == 1


def test_s09_run_process_converts_ul_to_plc_raw_volume():
    client = PseudoSzlabS09OpcUaClient({"S09液体瓶1剩余液量": 10.0})
    device = make_pipetting_device(client)

    result = device.run_process(
        process=7,
        tip_box_index=1,
        tip_index=1,
        liquid_bottle_index=1,
        aspirate_volume=5000,
        volume_unit="uL",
    )

    assert result["success"] is True
    assert ("S09抽液量", 50000) in client.writes
    assert result["data"]["aspirate_volume"] == 50000
    assert result["data"]["aspirate_volume_ul"] == 5000.0
    assert result["data"]["remaining_volume_update"]["remaining_volume"] == 5.0


def test_s09_run_process_rejects_single_transfer_over_tip_range():
    device = make_pipetting_device()

    result = device.run_process(
        process=7,
        tip_box_index=1,
        tip_index=1,
        liquid_bottle_index=1,
        aspirate_volume=5.1,
        volume_unit="mL",
    )

    assert result["success"] is False
    assert "不能超过 5000 uL" in result["message"]


def test_s09_add_liquid_splits_ml_volume_over_5ml():
    client = PseudoSzlabS09OpcUaClient({"S09液体瓶4剩余液量": 10.0})
    device = make_pipetting_device(client)

    result = device.add_liquid(
        take_tip_box_index=1,
        release_tip_box_index=2,
        tip_index=2,
        liquid_bottle_index=4,
        station=1,
        aspirate_volume=6,
        dispense_volume=6,
        volume_unit="mL",
    )

    assert result["success"] is True
    process_writes = [value for name, value in client.writes if name == "S09工艺选择"]
    assert [value for value in process_writes if value != 0] == [5, 7, 8, 7, 8, 6]
    assert [value for name, value in client.writes if name == "S09抽液量" and value != 0] == [50000, 10000]
    assert [value for name, value in client.writes if name == "S09放液量" and value != 0] == [50000, 10000]
    assert client.reads.count("S09天平读数") == 1
    assert result["data"]["split_count"] == 2
    assert result["data"]["transfer_chunks"] == [
        {
            "aspirate_volume": 50000,
            "dispense_volume": 50000,
            "aspirate_volume_ul": 5000.0,
            "dispense_volume_ul": 5000.0,
        },
        {
            "aspirate_volume": 10000,
            "dispense_volume": 10000,
            "aspirate_volume_ul": 1000.0,
            "dispense_volume_ul": 1000.0,
        },
    ]


def test_s09_run_process_deducts_remaining_volume_after_take_liquid():
    client = PseudoSzlabS09OpcUaClient({"S09液体瓶1剩余液量": 10.0})
    device = make_pipetting_device(client)

    result = device.run_process(
        process=7,
        tip_box_index=1,
        tip_index=1,
        liquid_bottle_index=1,
        aspirate_volume=2,
        volume_unit="mL",
    )

    assert result["success"] is True
    assert client.values["S09液体瓶1剩余液量"] == 8.0
    assert result["data"]["remaining_volume_update"] == {
        "bottle": 1,
        "variable": s09_remaining_volume_var(1),
        "previous_remaining_volume": 10.0,
        "deducted_volume_ml": 2.0,
        "remaining_volume": 8.0,
    }


def test_s09_run_process_rejects_take_liquid_when_remaining_volume_insufficient():
    client = PseudoSzlabS09OpcUaClient({"S09液体瓶1剩余液量": 1.0})
    device = make_pipetting_device(client)

    result = device.run_process(
        process=7,
        tip_box_index=1,
        tip_index=1,
        liquid_bottle_index=1,
        aspirate_volume=2,
        volume_unit="mL",
    )

    assert result["success"] is False
    assert "剩余液量不足" in result["message"]
    assert client.writes == []


def test_s09_density_process_uses_beaker_without_deducting_liquid_station():
    liquid_station_sensor = "传感器状态_上位机[4].NO[7]"
    client = PseudoSzlabS09OpcUaClient(
        {
            "S09天平读数": -12.34,
            "S09液体瓶1剩余液量": 10.0,
            liquid_station_sensor: False,
        }
    )
    device = make_pipetting_device(client)

    result = device.run_process(
        process=9,
        tip_box_index=1,
        tip_index=1,
        liquid_bottle_index=1,
        aspirate_volume=5,
    )

    assert result["success"] is True
    assert ("S09测密度次数", 1) in client.writes
    assert client.writes[-1] == ("S09测密度次数", 0)
    assert result["data"]["balance_reading"] == -12.34
    assert result["data"]["balance"] == {"balance_reading": -12.34, "stable": True}
    assert client.values["S09液体瓶1剩余液量"] == 10.0
    assert liquid_station_sensor not in client.reads


def test_s09_prepare_liquid_station_checks_single_station_status_only():
    client = PseudoSzlabS09OpcUaClient({"工站状态[8]": 2})
    device = make_pipetting_device(client)

    result = device.prepare_liquid_station()

    assert result["success"] is True
    assert result["data"] == {"station_status": 2, "station": 1}
    assert client.reads == ["工站状态[8]"]


def test_s09_read_allow_process_returns_allow_signal():
    client = PseudoSzlabS09OpcUaClient({"S09允许加工": False})
    device = make_pipetting_device(client)

    result = device.read_allow_process()

    assert result == {
        "success": True,
        "message": "S09 允许加工信号读取完成",
        "data": {"allowed": False, "variable": "S09允许加工"},
    }
    assert client.reads == ["S09允许加工"]


def test_s09_run_process_require_allow_blocks_after_wait_failure():
    client = PseudoSzlabS09OpcUaClient(
        {"S09允许加工": False},
        wait_results={("S09允许加工", True): False},
    )
    device = make_pipetting_device(client)

    result = device.run_process(
        process=7,
        tip_box_index=1,
        tip_index=1,
        liquid_bottle_index=1,
        aspirate_volume=1,
        require_allow=True,
    )

    assert result["success"] is False
    assert result["message"] == "等待 S09 允许加工失败"
    assert result["logs"] == [
        {
            "message": "等待 S09 允许加工信号",
            "detail": {"variable": "S09允许加工", "expected": True},
        }
    ]
    assert client.wait_equal_calls == [("S09允许加工", True)]
    assert client.writes == []


def test_s09_add_liquid_release_tip_waits_allow_before_writing_process():
    class ReleaseTipBlockedClient(PseudoSzlabS09OpcUaClient):
        def __init__(self):
            super().__init__({"S09允许加工": True, "S09液体瓶1剩余液量": 10.0})
            self.allow_wait_count = 0

        def wait_equal(self, name: str, expected, interval: float = 0.2) -> bool:
            if (name, expected) == ("S09允许加工", True):
                self.allow_wait_count += 1
                self.wait_equal_calls.append((name, expected))
                self.events.append(("wait_equal", name, expected))
                return self.allow_wait_count < 4
            return super().wait_equal(name, expected, interval=interval)

    client = ReleaseTipBlockedClient()
    device = make_pipetting_device(client)

    result = device.add_liquid(
        take_tip_box_index=1,
        release_tip_box_index=2,
        tip_index=1,
        liquid_bottle_index=1,
        station=1,
        aspirate_volume=1,
        dispense_volume=1,
        skip_level_check=True,
    )

    assert result["success"] is False
    assert result["message"] == "等待 S09 允许加工失败"
    assert [step.get("data", {}).get("process") for step in result["steps"] if step.get("success")] == [5, 7, 8]
    assert client.wait_equal_calls[-1] == ("S09允许加工", True)
    assert client.allow_wait_count == 4
    assert ("S09工艺选择", 6) not in client.writes


def test_s09_run_nodes_orchestration_emits_action_logs_to_workflow_logger():
    client = PseudoSzlabS09OpcUaClient({"S09允许加工": True, "S09液体瓶1剩余液量": 10.0})
    device = make_pipetting_device(client)
    records = []
    logger = WorkflowLogger(writer=lambda message, **kwargs: records.append((message, kwargs)))
    runtime_config = RuntimeConfig(
        path=Path("s09_test_runtime.json"),
        device_factory=RuntimeDeviceFactoryConfig(
            plc_device_id="szlab_poly_plc",
            devices={
                "szlab_mixer_pipetting_station": (
                    "unilabos.devices.workstation.szlab_poly_studio.s09_pipetting_station."
                    "pipetting_station.SzlabMixerPipettingStationDevice"
                )
            },
        ),
        opc_snapshot=RuntimeOpcSnapshotConfig(
            action_variables={
                "run_process": [
                    "S09允许加工",
                    "S09工艺选择",
                    "S09参数写入完成",
                    "S09工艺完成",
                    "S09抽液量",
                ],
            }
        ),
    )
    nodes = [
        WorkflowNode(
            uuid="s09-run-process",
            name="auto-run_process",
            device_name="szlab_mixer_pipetting_station",
            param={
                "process": 7,
                "tip_box_index": 1,
                "tip_index": 1,
                "liquid_bottle_index": 1,
                "aspirate_volume": 10,
                "require_allow": True,
            },
        )
    ]

    results = run_nodes(
        nodes,
        devices={"szlab_poly_plc": client, "szlab_mixer_pipetting_station": device},
        logger=logger,
        runtime_config=runtime_config,
    )

    assert results[0]["result"]["success"] is True
    messages = [message for message, _kwargs in records]
    assert "等待 S09 允许加工信号" in messages
    assert f"S09 工艺 7 参数写入开始：{S09_PROCESS_LABELS[7]}" in messages
    assert "S09 参数写入完成信号已置位，将保持至工艺结束" in messages
    assert "等待 S09 工艺 7 完成" in messages
    assert any(
        kwargs["detail"]["node_uuid"] == "s09-run-process"
        for _message, kwargs in records
        if isinstance(kwargs.get("detail"), dict) and "action_log" in kwargs["detail"]
    )
    assert client.wait_equal_calls[0] == ("S09允许加工", True)
    assert ("S09工艺选择", 7) in client.writes


def test_s09_bind_and_release_are_placeholders():
    device = make_pipetting_device()

    bind = device.bind_sample_to_station(sample_id="sample-1")
    release = device.release_station()

    assert bind == {
        "success": True,
        "message": "S09 样品绑定逻辑暂未启用",
        "data": {"sample_id": "sample-1", "enabled": False},
    }
    assert release == {
        "success": True,
        "message": "S09 样品解绑逻辑暂未启用",
        "data": {"enabled": False},
    }


def test_s09_read_balance_returns_stability_and_reading():
    client = PseudoSzlabS09OpcUaClient({"S09天平读数稳定": True, "S09天平读数": 56.78})
    device = make_pipetting_device(client)

    result = device.read_balance(require_stable=True)

    assert result["success"] is True
    assert result["data"] == {"balance_reading": 56.78, "stable": True}
    assert client.wait_equal_calls == [("S09天平读数稳定", True)]
    assert client.reads == ["S09天平读数"]


def test_s09_check_home_position_reads_only_requested_signal():
    client = PseudoSzlabS09OpcUaClient({"S09原点信号_3": False})
    device = make_pipetting_device(client)

    result = device.check_home_position(home_position=3)

    assert result["success"] is False
    assert result["data"]["variable"] == "S09原点信号_3"
    assert client.reads == ["S09原点信号_3"]


def test_s09_read_home_positions_returns_all_home_signals():
    client = PseudoSzlabS09OpcUaClient(
        {
            "S09原点信号_1": True,
            "S09原点信号_2": False,
            "S09原点信号_3": True,
            "S09原点信号_4": False,
        }
    )
    device = make_pipetting_device(client)

    result = device.read_home_positions()

    assert result["success"] is True
    home_positions = result["data"]["home_positions"]
    assert {index: item["value"] for index, item in home_positions.items()} == {
        1: True,
        2: False,
        3: True,
        4: False,
    }
    assert client.reads == ["S09原点信号_1", "S09原点信号_2", "S09原点信号_3", "S09原点信号_4"]


def test_s09_remaining_volume_actions_use_remaining_volume_names():
    client = PseudoSzlabS09OpcUaClient()
    device = make_pipetting_device(client)

    result = device.initialize_liquid_bottle_remaining_volumes()

    assert result["success"] is True
    assert "remaining_volume" in result["data"]
    assert all((s09_remaining_volume_var(index), 100.0) in client.writes for index in range(1, 6))
    assert not any("容量" in name for name, _value in client.writes)


def test_s09_reusable_tip_inventory_actions_persist_state(tmp_path):
    state_path = tmp_path / "s09_tip_state.json"
    device = make_pipetting_device(
        tip_reuse_state_path=str(state_path),
        tip_max_use_count=3,
    )

    before = device.get_reusable_tip_status()
    initialized = device.initialize_reusable_tip_inventory()
    repeated = device.initialize_reusable_tip_inventory()
    restored = make_pipetting_device(
        tip_reuse_state_path=str(state_path),
        tip_max_use_count=3,
    )
    status = restored.get_pipetting_status()

    assert before["data"]["initialized"] is False
    assert before["data"]["state_path"] == str(state_path)
    assert initialized["success"] is True
    assert initialized["data"] == {
        "initialized": True,
        "tip_count": 96,
        "max_use_count": 3,
        "used_tip_count": 0,
        "known_bindings": {},
        "state_path": str(state_path),
    }
    assert repeated["success"] is False
    assert "已经初始化" in repeated["message"]
    assert status["data"]["reusable_tip_state"]["initialized"] is True
    assert len(status["data"]["reusable_tip_state"]["tips"]) == 96


def test_s09_debug_csv_is_small_plc_input_with_remaining_volume_names():
    csv_path = Path(
        "unilabos/devices/workstation/szlab_poly_studio/s09_pipetting_station/pipetting_station_nodes.csv"
    )
    text = csv_path.read_text(encoding="utf-8")

    assert "S09液体瓶1剩余液量" in text
    assert "S09液体瓶1容量" not in text
    assert "S09TIP盒工位编号" in text
    assert "S09参数写入完成" in text
    assert "S09测密度次数" in text
    assert "S09抽液天平读数[9]" in text
    assert "S09放液天平读数[9]" in text


def test_s09_robot_actions_use_dev_robot_s09_task_contract(monkeypatch):
    monkeypatch.setenv("SKIP_ROBOT_HANDSHAKE_CHECK", "1")

    class FakePlcGateway:
        def __init__(self):
            self.reads = []
            self.writes = []
            self.task_submitted = False
            self.values = {
                "Robot_任务完成": 19,
                "S09工艺完成": 1,
                "S09原点信号_1": True,
            }

        def read_variable(self, name, use_cache=False):
            del use_cache
            self.reads.append(name)
            if name == "传感器状态_上位机[4].NO[6]":
                return self.values.get(name, False)
            if name == "传感器状态_上位机[4].NO[7]":
                return self.values.get(name, True)
            if name == "机器人Busy信号":
                return False
            if name in self.values:
                return self.values[name]
            raise KeyError(name)

        def write_variable(self, name, value):
            self.writes.append((name, value))
            self.values[name] = value
            if name == "任务号" and value == 19:
                self.task_submitted = True

        def wait_sensor_conditions(self, conditions, interval=0.2, context=None):
            del interval, context
            if self.task_submitted:
                self.values.update(conditions)
            values = {
                name: self.read_variable(name, use_cache=False)
                for name in conditions
            }
            return all(values[name] == expected for name, expected in conditions.items()), values

    gateway = FakePlcGateway()
    robot = SzlabMixerRobotDevice()
    robot.set_plc_gateway(gateway)

    result = robot.submit_place_to_s09(product_type=1, position=2)

    assert result["success"] is True
    assert result["target_sensor_variable"] == "传感器状态_上位机[4].NO[6]"
    assert result["sensor_precheck"]["success"] is True
    assert result["sensor_postcheck"]["success"] is True
    assert "传感器状态_上位机[4].NO[6]" in gateway.reads
    assert gateway.writes == [
        ("S09取放料产品", 1),
        ("S09取放料编号", 2),
        ("任务号", 19),
        ("Robot_任务写入完成", False),
        ("Robot_任务写入完成", True),
        ("Robot_任务写入完成", False),
        ("S09取放料产品", 0),
        ("S09取放料编号", 0),
        ("任务号", 0),
    ]
