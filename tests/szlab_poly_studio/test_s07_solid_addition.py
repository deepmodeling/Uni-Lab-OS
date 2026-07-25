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
            sensors.NODE_BALANCE_READING: 12.34,
        }
        self.writes: list[tuple[str, Any]] = []
        self.events: list[tuple[Any, ...]] = []
        self.waits: list[tuple[str, Any, float, float]] = []
        self.force_process_timeout = False
        self.dose_completion_reads_remaining = 0

    def read_variable(self, node_name: str, use_cache: bool = False) -> Any:
        self.events.append(("read", node_name))
        if node_name == sensors.NODE_PROCESS_COMPLETE:
            if (
                self.values.get(sensors.NODE_PROCESS_SELECT) == sensors.PROCESS_DOSE_POWDER
                and self.dose_completion_reads_remaining > 0
            ):
                self.dose_completion_reads_remaining -= 1
                return 0
            return self.values.get(sensors.NODE_PROCESS_SELECT, 0)
        if node_name.startswith("S07位置") and "二维码" in node_name:
            return 0
        return self.values[node_name]

    def write_variable(self, node_name: str, value: Any) -> None:
        self.events.append(("write", node_name, value))
        self.values[node_name] = value
        self.writes.append((node_name, value))

    def wait_variable_equal(self, node_name: str, expected: Any, timeout: float = 300.0, interval: float = 0.2) -> bool:
        self.events.append(("wait", node_name, expected))
        self.waits.append((node_name, expected, timeout, interval))
        if node_name == sensors.NODE_PROCESS_COMPLETE:
            if self.force_process_timeout:
                return False
            return self.values.get(sensors.NODE_PROCESS_SELECT, 0) == expected
        return self.values.get(node_name) == expected


def make_s07_device(plc: FakeS07Plc | None = None) -> SZLabS07SolidAdditionDevice:
    plc = plc or FakeS07Plc()
    device = SZLabS07SolidAdditionDevice(process_timeout=0.05, poll_interval=0.001)
    device.set_plc_gateway(plc)
    return device


def test_s07_solid_addition_device_is_ast_scannable_from_own_package():
    root = Path("unilabos/devices/workstation/szlab_poly_studio/s07_solid_addition")
    with ThreadPoolExecutor(max_workers=2) as executor:
        result = scan_directory(root, python_path=Path(".").resolve(), executor=executor)

    assert set(result["devices"]) == {"szlab_s07_solid_addition"}
    actions = result["devices"]["szlab_s07_solid_addition"]["actions"]
    assert list(actions) == [
        "scan_powder_cartridges",
        "read_s07_balance",
        "rotate_powder_cartridge_to_feed",
        "dose_powder",
    ]
    assert all(action["action_args"]["auto_prefix"] for action in actions.values())
    dose_params = {param["name"] for param in actions["dose_powder"]["params"]}
    assert "coarse_params" not in dose_params
    assert "fine_params" not in dose_params
    assert {"coarse_position", "fine_position", "target_weight", "timeout", "params_json", "recipe_name"} <= dose_params


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
    assert all(len(qr_code) == sensors.QR_CODE_LENGTH for qr_code in result["qr_codes"].values())
    assert plc.waits[:2] == [
        (sensors.NODE_HOME, True, 0.05, 0.001),
        (sensors.NODE_ALLOW_PROCESS, True, 0.05, 0.001),
    ]
    assert (sensors.NODE_PROCESS_SELECT, sensors.PROCESS_SCAN_CARTRIDGES) in plc.writes
    assert (sensors.NODE_PARAMS_WRITTEN, True) in plc.writes
    assert (sensors.NODE_PARAMS_WRITTEN, False) in plc.writes


def test_s07_read_balance_returns_realtime_value():
    plc = FakeS07Plc()
    device = make_s07_device(plc)

    result = device.read_s07_balance()

    assert result == {
        "success": True,
        "value": 12.34,
        "variable": sensors.NODE_BALANCE_READING,
    }


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
        timeout=0.05,
    )

    assert result["success"] is True
    assert result["target_weight"] == 12.5
    assert (sensors.NODE_COARSE_POSITION, 2) in plc.writes
    assert (sensors.NODE_FINE_POSITION, 5) in plc.writes
    assert (sensors.NODE_TARGET_WEIGHT, 12.5) in plc.writes
    assert (sensors.s07_powder_param_var("粗注粉", "开口量", 0), 1000) in plc.writes
    assert (sensors.NODE_COARSE_SHAKE_MAX_SPEED, 900) in plc.writes
    default_params = json.loads(s07_module.DEFAULT_POWDER_PARAMS_PATH.read_text(encoding="utf-8"))
    expected_feed_speed = default_params["default"]["fine_params"]["feed_speed"][1]
    assert (sensors.s07_powder_param_var("精注粉", "落粉匀速", 1), expected_feed_speed) in plc.writes
    assert (sensors.NODE_PROCESS_SELECT, sensors.PROCESS_DOSE_POWDER) in plc.writes
    assert result["balance_sample_count"] == 1
    assert result["final_balance"] == 12.34
    assert result["balance_samples"][0]["value"] == 12.34


def test_s07_dose_powder_samples_balance_until_process_completes():
    plc = FakeS07Plc()
    plc.dose_completion_reads_remaining = 2
    device = make_s07_device(plc)

    result = device.dose_powder(coarse_position=2, fine_position=5, target_weight=12.5, timeout=0.05)

    assert result["success"] is True
    assert result["balance_sample_count"] == 3
    assert [sample["value"] for sample in result["balance_samples"]] == [12.34, 12.34, 12.34]
    first_balance_read = plc.events.index(("read", sensors.NODE_BALANCE_READING))
    process_complete_read = plc.events.index(("read", sensors.NODE_PROCESS_COMPLETE))
    assert first_balance_read < process_complete_read


def test_s07_resets_all_unilab_written_params_after_dose_complete():
    plc = FakeS07Plc()
    device = make_s07_device(plc)

    device.dose_powder(coarse_position=2, fine_position=5, target_weight=12.5, timeout=0.05)

    first_process_write = plc.writes.index((sensors.NODE_PROCESS_SELECT, sensors.PROCESS_DOSE_POWDER))
    process_complete_read = plc.events.index(("read", sensors.NODE_PROCESS_COMPLETE))
    reset_params_written_event = plc.events.index(("write", sensors.NODE_PARAMS_WRITTEN, False))
    reset_params_written = plc.writes.index((sensors.NODE_PARAMS_WRITTEN, False))
    initial_writes = plc.writes[:first_process_write]

    assert (sensors.NODE_LOAD_POSITION, 0) not in initial_writes
    assert (sensors.NODE_COARSE_POSITION, 0) not in initial_writes
    assert (sensors.NODE_FINE_POSITION, 0) not in initial_writes
    assert (sensors.NODE_TARGET_WEIGHT, 0.0) not in initial_writes
    assert (sensors.s07_powder_param_var("粗注粉", "开口量", 0), 0) not in initial_writes
    assert (sensors.s07_powder_param_var("精注粉", "落粉匀速", 0), 0.0) not in initial_writes
    assert (sensors.NODE_COARSE_SHAKE_MAX_SPEED, 0) not in initial_writes
    assert (sensors.NODE_FINE_SHAKE_MAX_SPEED, 0) not in initial_writes
    assert reset_params_written_event > process_complete_read
    assert (sensors.NODE_LOAD_POSITION, 0) in plc.writes[reset_params_written:]
    assert (sensors.NODE_COARSE_POSITION, 0) in plc.writes[reset_params_written:]
    assert (sensors.NODE_FINE_POSITION, 0) in plc.writes[reset_params_written:]
    assert (sensors.NODE_TARGET_WEIGHT, 0.0) in plc.writes[reset_params_written:]


def test_s07_resets_params_after_process_complete_timeout():
    plc = FakeS07Plc()
    plc.force_process_timeout = True
    device = make_s07_device(plc)

    result = device.rotate_powder_cartridge_to_feed(position=4, timeout=0.05)

    assert result["success"] is False
    assert ("wait", sensors.NODE_PROCESS_COMPLETE, sensors.PROCESS_ROTATE_TO_FEED) in plc.events
    assert (sensors.NODE_LOAD_POSITION, 4) in plc.writes
    assert (sensors.NODE_PARAMS_WRITTEN, False) in plc.writes
    assert (sensors.NODE_PROCESS_SELECT, 0) in plc.writes
    assert (sensors.NODE_LOAD_POSITION, 0) in plc.writes


def test_s07_dose_powder_loads_recipe_params_from_json_without_ui_overrides(tmp_path):
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
        params_json=str(params_path),
        recipe_name="test_recipe",
        timeout=0.05,
    )

    assert result["success"] is True
    assert result["recipe_name"] == "test_recipe"
    assert (sensors.s07_powder_param_var("粗注粉", "开口量", 0), 1) in plc.writes
    assert (sensors.s07_powder_param_var("精注粉", "落粉匀速", 4), 0.5) in plc.writes
    assert (sensors.NODE_COARSE_SHAKE_MAX_SPEED, 90) in plc.writes
    assert (sensors.NODE_FINE_SHAKE_MAX_SPEED, 30) in plc.writes
