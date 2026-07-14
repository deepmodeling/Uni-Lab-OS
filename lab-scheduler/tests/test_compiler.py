"""Tests for material-aware execution DAG compiler."""

from __future__ import annotations

from fastapi.testclient import TestClient

from scheduler.api.schemas import Machine, Resources
from scheduler.compiler import (
    CompilerRequest,
    CompilerService,
    CompilerStep,
    DurationModel,
    MaterialOutput,
    MaterialRef,
)
from scheduler.main import app


def _plate96_to_plate24_request() -> CompilerRequest:
    return CompilerRequest(
        lab_id="lab-compiler",
        task_id="plate-run",
        resources=Resources(
            machines=[
                Machine(type="Reactor", count=1),
                Machine(type="Liquid_Handler", count=1),
                Machine(type="Characterizer", count=1),
            ]
        ),
        steps=[
            CompilerStep(
                step_id="react_plate96",
                machine_type="Reactor",
                outputs=[
                    MaterialOutput(
                        material_id="react_product",
                        container_type="plate96",
                        amount=96,
                    )
                ],
                duration_model=DurationModel(fixed=120),
            ),
            CompilerStep(
                step_id="aliquot_to_plate24",
                machine_type="Liquid_Handler",
                inputs=[MaterialRef(material_id="react_product")],
                outputs=[
                    MaterialOutput(
                        material_id="prep_plate",
                        container_type="plate24",
                        amount=96,
                    )
                ],
                duration_model=DurationModel(
                    setup=2,
                    per_item=1,
                    items_per_cycle=8,
                    teardown=1,
                ),
            ),
            CompilerStep(
                step_id="characterize_plate24",
                machine_type="Characterizer",
                inputs=[
                    MaterialRef(
                        material_id="prep_plate",
                        container_type="plate24",
                    )
                ],
                duration_model=DurationModel(fixed=10),
            ),
        ],
    )


def test_compiler_expands_container_throughput_to_fixed_execution_dag():
    """96孔反应板转 4 块 24孔板, 再逐块表征."""
    response = CompilerService().compile(_plate96_to_plate24_request())
    task = response.schedule_request.tasks[0]

    step_ids = [step.step_id for step in task.steps]
    assert step_ids == [
        "react_plate96",
        "aliquot_to_plate24_01",
        "aliquot_to_plate24_02",
        "aliquot_to_plate24_03",
        "aliquot_to_plate24_04",
        "characterize_plate24_01",
        "characterize_plate24_02",
        "characterize_plate24_03",
        "characterize_plate24_04",
    ]

    assert ("react_plate96", "aliquot_to_plate24_01") in task.dependencies
    assert ("react_plate96", "aliquot_to_plate24_04") in task.dependencies
    assert ("aliquot_to_plate24_01", "characterize_plate24_01") in task.dependencies
    assert ("aliquot_to_plate24_04", "characterize_plate24_04") in task.dependencies

    aliquot_steps = [s for s in task.steps if s.step_id.startswith("aliquot")]
    assert [s.duration for s in aliquot_steps] == [6, 6, 6, 6]
    assert aliquot_steps[0].input_samples == ["react_product"]
    assert aliquot_steps[0].output_samples == ["prep_plate_01"]

    characterize_steps = [s for s in task.steps if s.step_id.startswith("characterize")]
    assert characterize_steps[0].input_samples == ["prep_plate_01"]
    assert characterize_steps[-1].input_samples == ["prep_plate_04"]

    prep_containers = [
        c for c in response.containers if c.material_id == "prep_plate"
    ]
    assert len(prep_containers) == 4
    assert {c.amount for c in prep_containers} == {24}


def test_compile_api_returns_schedule_request():
    client = TestClient(app)
    payload = _plate96_to_plate24_request().model_dump()

    response = client.post("/api/v1/compile", json=payload)

    assert response.status_code == 200
    body = response.json()
    task = body["schedule_request"]["tasks"][0]
    assert len(task["steps"]) == 9
    assert body["metadata"][1]["source_step_id"] == "aliquot_to_plate24"
    assert body["metadata"][1]["dispatch_count"] == 4
