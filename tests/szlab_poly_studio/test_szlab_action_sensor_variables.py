"""szlab Action 传感器变量解析测试。"""

from __future__ import annotations

from scripts.szlab_action_sensor_variables import (
    ROBOT_HANDSHAKE_OPC_VARIABLES,
    resolve_action_sensor_variables,
    resolve_robot_action_opc_variables,
)
from unilabos.devices.workstation.szlab_poly_studio.s06_pump.sensors import (
    ADDITION_BEAKER_SENSOR,
)
from unilabos.devices.workstation.szlab_poly_studio.s09_pipetting_station.sensors import (
    S09_STATION_SENSORS,
)
from unilabos.devices.workstation.szlab_poly_studio.s12_robot.robot_tasks import (
    S05_MATERIAL_SENSOR,
    product_slot_sensor,
    s04_sensor,
)


def test_robot_submit_pick_from_s03_resolves_product_slot_sensor():
    variables = resolve_action_sensor_variables(
        "szlab_mixer_robot",
        "submit_pick_from_s03",
        {"product_type": 1, "position": "1-1"},
    )
    assert variables == [
        product_slot_sensor(1, "1-1", used=False),
    ]


def test_robot_submit_place_to_s04_resolves_material_sensor():
    variables = resolve_action_sensor_variables(
        "szlab_mixer_robot",
        "submit_place_to_s04",
        {"position": 2},
    )
    assert variables == [s04_sensor(2)]


def test_stirrer_alias_device_id_resolves_material_sensor():
    variables = resolve_action_sensor_variables(
        "szlab_s04_magnetic_stirring",
        "run_stirring",
        {"position": 1},
    )
    assert len(variables) == 1
    assert variables[0].startswith("传感器状态_上位机")


def test_pump_alias_device_id_resolves_beaker_sensor():
    variables = resolve_action_sensor_variables(
        "szlab_s06_pump",
        "run_solvent_addition",
        {"process": 3},
    )
    assert variables == [ADDITION_BEAKER_SENSOR]


def test_photoshotting_resolves_material_sensor():
    variables = resolve_action_sensor_variables(
        "szlab_s05_photoshotting",
        "take_photo",
        {},
    )
    assert variables == [S05_MATERIAL_SENSOR]


def test_reusable_pipetting_resolves_selected_liquid_station_sensor():
    variables = resolve_action_sensor_variables(
        "szlab_mixer_pipetting_station",
        "add_liquid_with_reusable_tip",
        {"liquid_station_index": 4},
    )

    assert variables == [S09_STATION_SENSORS[4]]


def test_unknown_device_returns_empty_list():
    assert resolve_action_sensor_variables("unknown_device", "run", {}) == []


def test_robot_submit_pick_from_s072_has_no_sensor():
    assert resolve_action_sensor_variables(
        "szlab_mixer_robot",
        "submit_pick_from_s072",
        {"product_type": 1, "position": 1},
    ) == []


def test_robot_submit_pick_from_s03_resolves_handshake_and_task_variables():
    variables = resolve_robot_action_opc_variables(
        "szlab_mixer_robot",
        "submit_pick_from_s03",
    )
    assert "Robot_Home" in variables
    assert "Robot_任务允许写入" in variables
    assert "Robot_任务写入完成" in variables
    assert "Robot_任务完成" in variables
    assert "任务号" in variables
    assert "S03取放料产品" in variables
    assert "S03取放料编号" in variables
    assert variables[: len(ROBOT_HANDSHAKE_OPC_VARIABLES)] == list(
        ROBOT_HANDSHAKE_OPC_VARIABLES
    )


def test_robot_submit_place_to_s05_only_includes_handshake():
    variables = resolve_robot_action_opc_variables(
        "szlab_mixer_robot",
        "submit_place_to_s05",
    )
    assert variables == list(ROBOT_HANDSHAKE_OPC_VARIABLES)


def test_non_robot_device_returns_empty_robot_variables():
    assert resolve_robot_action_opc_variables("szlab_s06_pump", "run_solvent_addition") == []
