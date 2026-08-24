from __future__ import annotations

from tests.device_runtime.heating_scenario_smoke import run_heating_scenario_smoke


def test_hostlink_runs_all_heating_scenarios_from_shared_graph() -> None:
    proof = run_heating_scenario_smoke("hostlink", timeout=90.0)
    assert proof["success"] is True
    assert {
        scenario_id: value["job_count"]
        for scenario_id, value in proof["scenarios"].items()
    } == {
        "single_sequential": 2,
        "parallel_three_site": 3,
        "cross_device_transfer": 3,
    }
    assert {
        scenario_id: value["job_count"]
        for scenario_id, value in proof["workbench_scenarios"].items()
    } == {
        "single_sample": 4,
        "sequential_two_samples": 7,
        "parallel_three_samples": 10,
    }
