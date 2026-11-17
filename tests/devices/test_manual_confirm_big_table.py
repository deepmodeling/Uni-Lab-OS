# -*- coding: utf-8 -*-
"""新威 manual_confirm 大表要带上配液允差、混匀判定和扣电同轴度。"""
import csv
from pathlib import Path

from unilabos.devices.neware_battery_test_system.neware_battery_test_system import (
    NewareBatteryTestSystem,
)
from unilabos.devices.workstation.coin_cell_assembly.coin_cell_assembly import (
    CoinCellAssemblyWorkstation,
)
from unilabos.devices.workstation.mixing_judgment_query import JUDGMENT_COLUMN


def test_big_table_includes_tolerance_judgment_and_coaxiality(tmp_path: Path):
    device = NewareBatteryTestSystem.__new__(NewareBatteryTestSystem)
    device._ros_node = None
    out = device._export_manual_confirm_csv(
        csv_export_dir=str(tmp_path),
        mount_resource=[],
        formulations=[
            {
                "orderName": "DDCC1001",
                "prep_bottle_barcode": "P1",
                "vial_bottle_barcodes": "V1",
                "target_mass_ratio": {"A": 1},
                "real_mass_ratio": {"A": 1.01},
                "total_mass_tolerance": 0.012345,
                JUDGMENT_COLUMN: "1",
            }
        ],
        assembly_rows={
            "Time": ["20261117_113000"],
            "open_circuit_voltage": [3.7],
            "pole_weight": [97.5],
            "assembly_time": [120],
            "assembly_pressure": [5.1],
            "target_assembly_pressure": [5.0],
            "electrolyte_volume": [80],
            "data_coin_type": [2],
            "electrolyte_code": ["EL-1"],
            "coin_cell_code": ["CC-1"],
            "coaxiality": ["0.286mm"],
        },
        collector_mass=[12.0],
        active_material=["0.97"],
        capacity=[180.0],
        battery_system=["811_LI_002"],
    )
    with open(out, newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1
    assert rows[0]["coaxiality"] == "0.286mm"
    assert rows[0]["total_mass_tolerance"] == "0.012345"
    assert rows[0][JUDGMENT_COLUMN] == "1"
    assert rows[0]["orderName"] == "DDCC1001"


def test_missing_new_fields_stay_blank(tmp_path: Path):
    device = NewareBatteryTestSystem.__new__(NewareBatteryTestSystem)
    device._ros_node = None
    out = device._export_manual_confirm_csv(
        csv_export_dir=str(tmp_path),
        mount_resource=[],
        formulations=[{"orderName": "DDCC1002"}],
        assembly_rows={
            "Time": ["20261117_113100"],
            "open_circuit_voltage": [3.7],
            "pole_weight": [97.5],
            "assembly_time": [120],
            "assembly_pressure": [5.1],
            "target_assembly_pressure": [5.0],
            "electrolyte_volume": [80],
            "data_coin_type": [2],
            "electrolyte_code": ["EL-2"],
            "coin_cell_code": ["CC-2"],
        },
        collector_mass=[1.0],
        active_material=["0.9"],
        capacity=[100.0],
        battery_system=["x"],
    )
    with open(out, newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0]["coaxiality"] == ""
    assert rows[0]["total_mass_tolerance"] == ""
    assert rows[0][JUDGMENT_COLUMN] == ""


def test_coaxiality_is_copied_onto_assembly_data(tmp_path: Path):
    csv_path = tmp_path / "coin_cell_assembly.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Time", "coin_cell_code", "coaxiality"])
        writer.writerow(["20261117_113000", "CC-1", "0.286mm"])
        writer.writerow(["20261117_113100", "CC-1", "0.400mm"])
        writer.writerow(["20261117_113200", "CC-2", ""])
    station = CoinCellAssemblyWorkstation.__new__(CoinCellAssemblyWorkstation)
    station._alignment_csv_for_batch = str(csv_path)
    batteries = [
        {"Time": "20261117_113100", "coin_cell_code": "CC-1"},
        {"Time": "20261117_113200", "coin_cell_code": "CC-2"},
        {"Time": "20261117_113300", "coin_cell_code": "CC-3"},
    ]
    station._attach_coaxiality(batteries)
    assert batteries[0]["coaxiality"] == "0.400mm"
    assert batteries[1]["coaxiality"] == ""
    assert batteries[2]["coaxiality"] == ""
