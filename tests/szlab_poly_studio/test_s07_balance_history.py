from __future__ import annotations

import csv
from pathlib import Path

from unilabos.devices.workstation.szlab_poly_studio.s07_solid_addition.balance_history import (
    BalanceSample,
    S07BalanceHistoryRecorder,
    estimate_coarse_transition,
)


def test_balance_history_appends_runs_and_refreshes_shared_curve_in_realtime(tmp_path: Path):
    first = S07BalanceHistoryRecorder(
        output_dir=tmp_path,
        target_weight=10.0,
        recipe_name="first",
        coarse_position=1,
        fine_position=2,
    )
    first.record(3.0)
    chart_path = Path(first.artifact_info()["balance_chart_path"])
    assert chart_path.exists()
    assert "测试次数：1" in chart_path.read_text(encoding="utf-8")
    first.record(10.1)
    first_info = first.finish(status="success", final_weight=10.1)

    second = S07BalanceHistoryRecorder(
        output_dir=tmp_path,
        target_weight=20.0,
        recipe_name="second",
        coarse_position=3,
        fine_position=4,
    )
    second.record(19.8)
    second_info = second.finish(status="success", final_weight=19.8)

    with (tmp_path / "samples.csv").open(newline="", encoding="utf-8") as csv_file:
        sample_rows = list(csv.DictReader(csv_file))
    svg = chart_path.read_text(encoding="utf-8")

    assert len(sample_rows) == 3
    assert {row["run_id"] for row in sample_rows} == {first.run_id, second.run_id}
    assert [row["elapsed_s"] for row in sample_rows if row["run_id"] == first.run_id][0] == "0.0"
    assert [row["elapsed_s"] for row in sample_rows if row["run_id"] == second.run_id][0] == "0.0"
    assert first_info["balance_chart_path"] == second_info["balance_chart_path"]
    assert "测试次数：2" in svg
    assert "目标 10.0000 g" in svg
    assert "目标 20.0000 g" in svg
    assert "0 g 基线" in svg
    assert 'stroke="#dc2626"' in svg
    assert not (tmp_path / "runs.csv").exists()
    assert not (tmp_path / "overview.svg").exists()
    assert not (tmp_path / "charts").exists()


def test_balance_history_can_finish_without_samples_or_summary_csv(tmp_path: Path):
    recorder = S07BalanceHistoryRecorder(
        output_dir=tmp_path,
        target_weight=10.0,
        recipe_name="failed",
        coarse_position=1,
        fine_position=2,
    )

    info = recorder.finish(status="failed")

    assert Path(info["balance_chart_path"]).exists()
    assert "等待 S07 天平采样" in Path(info["balance_chart_path"]).read_text(encoding="utf-8")
    assert not (tmp_path / "runs.csv").exists()


def test_coarse_transition_is_only_an_estimated_plot_marker():
    values = [0.0, 2.0, 4.0, 6.0, 8.0, 9.0, 9.2, 9.35, 9.5, 9.65]
    samples = [
        BalanceSample(timestamp=f"sample-{index}", elapsed_s=float(index), value_g=value)
        for index, value in enumerate(values)
    ]

    transition = estimate_coarse_transition(samples, target_weight=10.0)

    assert transition is not None
    assert transition.value_g == 9.0
