"""Regression tests for compiler submission examples."""

from __future__ import annotations

import json
from pathlib import Path

from scheduler import CompilerRequest, CompilerService, SchedulerService

ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parent
SUBMISSIONS = REPO_ROOT / "uni-lab-designer" / "examples" / "compiler-submissions"


def _compile(filename: str):
    payload = json.loads((SUBMISSIONS / filename).read_text(encoding="utf-8"))
    return CompilerService().compile(CompilerRequest(**payload))


def test_all_compiler_submissions_compile_and_schedule():
    files = [
        "submit_a_full_process.json",
        "submit_b_to_characterization.json",
        "submit_c_post_purification.json",
    ]
    for filename in files:
        compiled = _compile(filename)
        response = SchedulerService().schedule(compiled.schedule_request)
        assert response.schedule
        assert response.objective.total_makespan > 0


def test_realistic_duration_models_are_materialized():
    compiled = _compile("submit_a_full_process.json")
    task = compiled.schedule_request.tasks[0]
    durations = {step.step_id: step.duration for step in task.steps}

    assert durations["s03_reaction_run"] == 240
    assert durations["s05_pre_characterization_prep_01"] == 21
    assert durations["s06_characterization_analysis_01"] == 159
    assert durations["s13_structural_characterization_01"] == 405
