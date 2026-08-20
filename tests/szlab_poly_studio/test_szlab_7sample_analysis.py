from __future__ import annotations

import pytest

from scripts.szlab_7sample_analysis import (
    NODE_S07,
    NODE_S09_DENSITY,
    NODE_S09_LIQUID,
    extract_density,
    extract_liquid,
    extract_s07,
    raw_to_ml,
)


def row(node_id: str, params: str, result: str, sample: str = "Sample A") -> dict:
    return {
        "node_id": node_id,
        "status": "completed",
        "sample_id": sample,
        "params_json": params,
        "result_json": result,
    }


def test_extracts_powder_liquid_and_recalculates_density() -> None:
    rows = [
        row(
            NODE_S07,
            "{}",
            '[{"result":{"powder_results":[{"target_weight":1.0,"balance_reading":1.1}]}}]',
        ),
        row(
            NODE_S09_LIQUID,
            '{"volume_unit":"raw","liquid_additions":[{"volume":5000}]}',
            '[{"result":{"steps":[{"data":{"process":8,"balance_reading":0.51}}]}}]',
        ),
        row(
            NODE_S09_DENSITY,
            "{}",
            '[{"result":{"data":{"density_volume_ml":0.5,"aspirate_balance_readings":[-0.5],"dispense_balance_readings":[0.52]}}}]',
        ),
    ]

    powder = extract_s07(rows)  # type: ignore[arg-type]
    density, means = extract_density(rows)  # type: ignore[arg-type]
    liquid = extract_liquid(rows, means)  # type: ignore[arg-type]

    assert powder[0].target == 1.0
    assert powder[0].actual == 1.1
    assert [item.density_g_ml for item in density] == [1.0, 1.04]
    assert means["Sample A"] == pytest.approx(1.02)
    assert liquid[0].target == 0.5
    assert liquid[0].actual == pytest.approx(0.5)
    assert "0.51" in liquid[0].source


def test_raw_volume_conversion() -> None:
    assert raw_to_ml(5000, "raw") == 0.5
    assert raw_to_ml(500, "uL") == 0.5
    assert raw_to_ml(0.5, "mL") == 0.5
