import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from unilabos.app.scheduler.api import create_scheduler_router
from unilabos.app.scheduler.dispatch import RecordingDispatcher
from unilabos.app.scheduler.history import WorkflowHistoryStore
from unilabos.app.scheduler.models import WorkflowNode, WorkflowSpec
from unilabos.app.scheduler.service import EdgeScheduler


def _spec(
    workflow_id: str,
    run_id: str = "",
    device_id: str = "bench-1",
) -> WorkflowSpec:
    return WorkflowSpec(
        workflow_id=workflow_id,
        run_id=run_id,
        nodes=[
            WorkflowNode(
                id="stir",
                device_id=device_id,
                action_name="stir",
                action_type="goal",
            )
        ],
    )


def test_actions_and_jobs_keep_explicit_run_id():
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher)

    result = scheduler.submit_workflow(_spec("workflow-1", "run-1"))
    job_id = result["dispatched"][0]["job_id"]

    assert result["run_id"] == "run-1"
    assert dispatcher.dispatched[0]["run_id"] == "run-1"
    snapshot = scheduler.workflow_snapshot("workflow-1")
    assert snapshot["run_id"] == "run-1"
    assert snapshot["nodes"]["stir"]["run_id"] == "run-1"
    assert scheduler.snapshot()["inflight_jobs"][job_id]["run_id"] == "run-1"


def test_finish_rejects_cross_run_callback_without_detaching_job():
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher)
    result = scheduler.submit_workflow(_spec("workflow-2", "run-2"))
    job_id = result["dispatched"][0]["job_id"]

    with pytest.raises(ValueError, match="belongs to run run-2"):
        scheduler.on_job_finished(job_id, True, run_id="run-other")

    assert job_id in scheduler.snapshot()["inflight_jobs"]
    scheduler.on_job_finished(job_id, True, run_id="run-2")
    assert job_id not in scheduler.snapshot()["inflight_jobs"]


def test_history_persists_run_id_for_workflow_and_job():
    history = WorkflowHistoryStore()
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher, history=history)
    result = scheduler.submit_workflow(_spec("workflow-3", "run-3"))
    scheduler.on_job_finished(result["dispatched"][0]["job_id"], True, run_id="run-3")

    assert history.get_run("workflow-3")["run_id"] == "run-3"
    assert history.list_jobs(workflow_id="workflow-3")[0]["run_id"] == "run-3"


def test_same_template_runs_are_isolated_and_can_dispatch_in_parallel():
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher)

    for index in range(3):
        scheduler.submit_workflow(
            _spec(
                f"workflow-{index}",
                f"run-{index}",
                device_id=f"bench-{index}",
            )
        )

    assert [item["run_id"] for item in dispatcher.dispatched] == [
        "run-0",
        "run-1",
        "run-2",
    ]
    assert len({item["job_id"] for item in dispatcher.dispatched}) == 3


def test_group_submission_uses_one_task_id_but_keeps_run_scoped_jobs():
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher)
    specs = [
        _spec("workflow-group-0", "run-group-0", device_id="bench-0"),
        _spec("workflow-group-1", "run-group-1", device_id="bench-1"),
    ]

    result = scheduler.submit_workflow_runs(specs, task_id="task-group")

    assert result["task_id"] == "task-group"
    assert [run["run_id"] for run in result["runs"]] == [
        "run-group-0",
        "run-group-1",
    ]
    assert {item["task_id"] for item in dispatcher.dispatched} == {"task-group"}
    assert {item["run_id"] for item in dispatcher.dispatched} == {
        "run-group-0",
        "run-group-1",
    }


def test_group_submission_api_accepts_per_run_parameters():
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher)
    app = FastAPI()
    app.include_router(create_scheduler_router(lambda: scheduler))

    response = TestClient(app).post(
        "/api/v1/workflow-runs",
        json={
            "task_id": "task-api-group",
            "runs": [
                {
                    "workflow_id": "workflow-api-0",
                    "run_id": "run-api-0",
                    "nodes": [
                        {
                            "id": "mix",
                            "device_id": "bench-api-0",
                            "action_name": "mix",
                            "param": {"volume_ml": 20},
                        }
                    ],
                },
                {
                    "workflow_id": "workflow-api-1",
                    "run_id": "run-api-1",
                    "nodes": [
                        {
                            "id": "mix",
                            "device_id": "bench-api-1",
                            "action_name": "mix",
                            "param": {"volume_ml": 50},
                        }
                    ],
                },
            ],
        },
    )

    assert response.status_code == 200
    assert response.json()["task_id"] == "task-api-group"
    assert {item["run_id"] for item in response.json()["dispatched"]} == {
        "run-api-0",
        "run-api-1",
    }


def test_inventory_keys_group_reservations_by_run_id():
    class Inventory:
        def __init__(self):
            self.reserved = []
            self.consumed = []

        def reserve_workflow(self, workflow_id, requirements):
            if workflow_id not in self.reserved:
                self.reserved.append(workflow_id)

        def consume_reservation(self, workflow_id, node_id):
            self.consumed.append((workflow_id, node_id))

    inventory = Inventory()
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher, inventory=inventory)
    specs = [
        _spec("workflow-inventory-0", "run-inventory-0", device_id="bench-0"),
        _spec("workflow-inventory-1", "run-inventory-1", device_id="bench-1"),
    ]
    # No material requirements means no reservation calls; add a lightweight
    # requirement only to exercise the identity path without touching a real DB.
    from unilabos.app.scheduler.inventory.domain import MaterialRequirement

    for spec in specs:
        spec.nodes[0].material_requirements = [
            MaterialRequirement(template_id="ethanol", quantity=1, unit="ml")
        ]
    scheduler.submit_workflow_runs(specs, task_id="task-inventory")
    assert inventory.reserved == ["run-inventory-0", "run-inventory-1"]
    assert inventory.consumed == [
        ("run-inventory-0", "stir"),
        ("run-inventory-1", "stir"),
    ]
