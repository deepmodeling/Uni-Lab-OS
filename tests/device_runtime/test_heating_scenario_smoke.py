from __future__ import annotations

from tests.device_runtime.heating_scenario_smoke import run_heating_scenario_smoke


def test_hostlink_runs_all_heating_scenarios_from_shared_graph() -> None:
    proof = run_heating_scenario_smoke("hostlink", timeout=25.0)
    assert proof["success"] is True
