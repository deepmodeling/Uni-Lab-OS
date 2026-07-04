from __future__ import annotations

import importlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from unilabos.registry.ast_registry_scanner import scan_directory

S07_PACKAGE = "unilabos.devices.workstation.szlab_poly_studio.s07_solid_addition"
s07_module = importlib.import_module(f"{S07_PACKAGE}.s07")
sensors = importlib.import_module(f"{S07_PACKAGE}.sensors")
SZLabS07SolidAdditionDevice = s07_module.SZLabS07SolidAdditionDevice


class FakeS07Plc:
    def __init__(self) -> None:
        self.values: dict[str, Any] = {
            sensors.NODE_HOME: True,
            sensors.NODE_ALLOW_PROCESS: True,
            sensors.NODE_PROCESS_COMPLETE: 0,
        }
        self.writes: list[tuple[str, Any]] = []

    def read(self, node_name: str) -> Any:
        if node_name == sensors.NODE_PROCESS_COMPLETE:
            return self.values.get(sensors.NODE_PROCESS_SELECT, 0)
        if node_name.startswith("S07位置") and "二维码" in node_name:
            return 0
        return self.values[node_name]

    def write(self, node_name: str, value: Any) -> None:
        self.values[node_name] = value
        self.writes.append((node_name, value))


def make_s07_device(plc: FakeS07Plc | None = None) -> SZLabS07SolidAdditionDevice:
    plc = plc or FakeS07Plc()
    device = SZLabS07SolidAdditionDevice(process_timeout=0.05, poll_interval=0.001)
    device._read_plc_variable = plc.read
    device._write_plc_variable = plc.write
    return device


def test_s07_solid_addition_device_is_ast_scannable_from_own_package():
    root = Path("unilabos/devices/workstation/szlab_poly_studio/s07_solid_addition")
    with ThreadPoolExecutor(max_workers=2) as executor:
        result = scan_directory(root, python_path=Path(".").resolve(), executor=executor)

    assert set(result["devices"]) == {"szlab_s07_solid_addition"}
    actions = result["devices"]["szlab_s07_solid_addition"]["actions"]
    assert list(actions) == [
        "scan_powder_cartridges",
        "rotate_powder_cartridge_to_feed",
        "dose_powder",
    ]
    assert all(action["action_args"]["auto_prefix"] for action in actions.values())


def test_s07_debug_config_references_existing_local_files():
    config_path = Path("unilabos/devices/workstation/szlab_poly_studio/s07_solid_addition/s07_debug.json")
    config = json.loads(config_path.read_text(encoding="utf-8"))

    assert Path(config["virtual_opcua"]["csv"]).exists()
    assert Path(config["virtual_opcua"]["flow"]).exists()
    assert config["virtual_opcua"]["endpoint"] == config["opcua"]["virtual_url"]
    assert config["action"]["name"] == "scan_powder_cartridges"


def test_s07_debug_helpers_match_pump_style_layout():
    device_dir = Path("unilabos/devices/workstation/szlab_poly_studio/s07_solid_addition")

    assert not (device_dir / "opcua_client.py").exists()
    assert not (device_dir / "probe_real_opcua.py").exists()


def test_s07_scan_powder_cartridges_writes_process_and_reads_qr_codes():
    plc = FakeS07Plc()
    device = make_s07_device(plc)

    result = device.scan_powder_cartridges(timeout=0.05)

    assert result["success"] is True
    assert result["process_type"] == sensors.PROCESS_SCAN_CARTRIDGES
    assert set(result["qr_codes"]) == set(sensors.POSITION_RANGE)
    assert plc.writes.index((sensors.NODE_PROCESS_SELECT, 0)) < plc.writes.index(
        (sensors.NODE_PROCESS_SELECT, sensors.PROCESS_SCAN_CARTRIDGES)
    )
    assert (sensors.NODE_PROCESS_SELECT, sensors.PROCESS_SCAN_CARTRIDGES) in plc.writes
    assert (sensors.NODE_PARAMS_WRITTEN, True) in plc.writes
    assert (sensors.NODE_PARAMS_WRITTEN, False) in plc.writes


def test_s07_rotate_powder_cartridge_to_feed_writes_load_position():
    plc = FakeS07Plc()
    device = make_s07_device(plc)

    result = device.rotate_powder_cartridge_to_feed(position=4, timeout=0.05)

    assert result["success"] is True
    assert result["position"] == 4
    assert (sensors.NODE_LOAD_POSITION, 4) in plc.writes
    assert (sensors.NODE_PROCESS_SELECT, sensors.PROCESS_ROTATE_TO_FEED) in plc.writes


def test_s07_dose_powder_writes_positions_weight_and_powder_params():
    plc = FakeS07Plc()
    device = make_s07_device(plc)

    result = device.dose_powder(
        coarse_position=2,
        fine_position=5,
        target_weight=12.5,
        coarse_params={"opening": [1, 2, 3, 4, 5], "shake_max_speed": 80},
        fine_params={"feed_speed": [0.1, 0.2, 0.3, 0.4, 0.5]},
        timeout=0.05,
    )

    assert result["success"] is True
    assert result["target_weight"] == 12.5
    assert (sensors.NODE_COARSE_POSITION, 2) in plc.writes
    assert (sensors.NODE_FINE_POSITION, 5) in plc.writes
    assert (sensors.NODE_TARGET_WEIGHT, 12.5) in plc.writes
    assert (sensors.s07_powder_param_var("粗注粉", "开口量", 0), 1) in plc.writes
    assert (sensors.NODE_COARSE_SHAKE_MAX_SPEED, 80) in plc.writes
    assert (sensors.s07_powder_param_var("精注粉", "落粉匀速", 1), 0.2) in plc.writes
    assert (sensors.NODE_PROCESS_SELECT, sensors.PROCESS_DOSE_POWDER) in plc.writes


def test_s07_resets_all_unilab_written_params_before_dose():
    plc = FakeS07Plc()
    device = make_s07_device(plc)

    device.dose_powder(coarse_position=2, fine_position=5, target_weight=12.5, timeout=0.05)

    first_process_write = plc.writes.index((sensors.NODE_PROCESS_SELECT, sensors.PROCESS_DOSE_POWDER))
    initial_writes = plc.writes[:first_process_write]
    assert (sensors.NODE_LOAD_POSITION, 0) in initial_writes
    assert (sensors.NODE_COARSE_POSITION, 0) in initial_writes
    assert (sensors.NODE_FINE_POSITION, 0) in initial_writes
    assert (sensors.NODE_TARGET_WEIGHT, 0.0) in initial_writes
    assert (sensors.s07_powder_param_var("粗注粉", "开口量", 0), 0) in initial_writes
    assert (sensors.s07_powder_param_var("精注粉", "落粉匀速", 0), 0.0) in initial_writes
    assert (sensors.NODE_COARSE_SHAKE_MAX_SPEED, 0) in initial_writes
    assert (sensors.NODE_FINE_SHAKE_MAX_SPEED, 0) in initial_writes


def test_s07_dose_powder_loads_recipe_params_and_allows_overrides(tmp_path):
    params_path = tmp_path / "powder_params.json"
    params_path.write_text(
        json.dumps(
            {
                "test_recipe": {
                    "coarse_params": {"opening": [1, 2, 3, 4, 5], "shake_max_speed": 90},
                    "fine_params": {"feed_speed": [0.1, 0.2, 0.3, 0.4, 0.5], "shake_max_speed": 30},
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    plc = FakeS07Plc()
    device = make_s07_device(plc)

    result = device.dose_powder(
        coarse_position=2,
        fine_position=5,
        target_weight=12.5,
        coarse_params={"opening": [5, 4, 3, 2, 1]},
        params_json=str(params_path),
        recipe_name="test_recipe",
        timeout=0.05,
    )

    assert result["success"] is True
    assert result["recipe_name"] == "test_recipe"
    assert (sensors.s07_powder_param_var("粗注粉", "开口量", 0), 5) in plc.writes
    assert (sensors.s07_powder_param_var("精注粉", "落粉匀速", 4), 0.5) in plc.writes
    assert (sensors.NODE_COARSE_SHAKE_MAX_SPEED, 90) in plc.writes
    assert (sensors.NODE_FINE_SHAKE_MAX_SPEED, 30) in plc.writes
