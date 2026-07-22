import json

import scripts.workflow_timing as workflow_timing
from scripts.workflow_timing import WorkflowTimingRecorder


def test_timing_report_writes_parallel_timeline_and_station_dwells(monkeypatch, tmp_path):
    ticks = iter([100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0, 107.0, 108.0])
    monkeypatch.setattr(workflow_timing.time, "monotonic", lambda: next(ticks))

    recorder = WorkflowTimingRecorder("run-1", "schedule-demo", tmp_path)
    recorder.mark_execution_started()
    recorder.start_step(
        index=1,
        total=3,
        node_id="place",
        device_name="szlab_mixer_robot",
        method="submit_place_to_s06",
        params={},
    )
    recorder.finish_step(result={"success": True})
    recorder.start_step(
        index=2,
        total=3,
        node_id="process",
        device_name="szlab_s06_pump",
        method="run_solvent_addition",
        params={"volume_pump_1": 10},
    )
    recorder.finish_step(result={"success": True})
    recorder.start_step(
        index=3,
        total=3,
        node_id="pick",
        device_name="szlab_mixer_robot",
        method="submit_pick_from_s06",
        params={},
    )
    recorder.finish_step(result={"success": True})

    report_path = recorder.finish(status="completed")
    report = json.loads(report_path.read_text(encoding="utf-8"))

    assert report["schema"] == "unilabos.workflow_timing.v2"
    assert report["steps"][0]["start_offset_s"] == 1.0
    assert report["station_dwells"] == [
        {
            "station": "S06",
            "position": None,
            "product_type": None,
            "sample_id": None,
            "place_step_index": 1,
            "pick_step_index": 3,
            "started_at": report["steps"][0]["finished_at"],
            "finished_at": report["steps"][2]["started_at"],
            "start_offset_s": 2.0,
            "end_offset_s": 5.0,
            "duration_s": 3.0,
            "status": "completed",
        }
    ]
    assert report["station_dwell_summary"][0]["station"] == "S06"
    html = (tmp_path / "run-1.html").read_text(encoding="utf-8")
    assert "资源并行时间轴" in html
    assert "物料在工站停留时间" in html
    assert "submit_place_to_s06" in html


def test_station_dwell_keeps_unmatched_place_as_open(tmp_path):
    recorder = WorkflowTimingRecorder("run-2", "open-dwell", tmp_path)
    recorder.steps = [
        {
            "index": 1,
            "method": "submit_place_to_s04",
            "params": {"position": 1, "sample_id": "sample-1"},
            "status": "success",
            "finished_at": "2026-07-22T10:00:05+08:00",
            "end_offset_s": 5.0,
        }
    ]

    assert recorder._calculate_station_dwells()[0] == {
        "station": "S04",
        "position": 1,
        "product_type": None,
        "sample_id": "sample-1",
        "place_step_index": 1,
        "pick_step_index": None,
        "started_at": "2026-07-22T10:00:05+08:00",
        "finished_at": None,
        "start_offset_s": 5.0,
        "end_offset_s": None,
        "duration_s": None,
        "status": "open",
    }
