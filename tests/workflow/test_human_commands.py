"""Human commands traverse the real compiler, scheduler and persistent reply boundary."""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from unilabos.app.scheduler.dispatch import RecordingDispatcher
from unilabos.app.scheduler.service import EdgeScheduler
from unilabos.app.workflow_api import create_workflow_app
from unilabos.workflow.execution_plan import ExecutionPlanBuilder
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.store import WorkflowStore
from unilabos.workflow.task_input import prepare_task_input
from unilabos.workflow.task_scheduler_bridge import TaskSchedulerBridge

from .test_authoring_engine import WORKFLOW_UUID, _compile
from .test_inventory_native_command import engine

SOURCE = f'''from typing import TypedDict
from unilabos.workflow.authoring import service, workflow, workflow_output
from unilabos.workflow.native_commands import HumanCommands
human: HumanCommands = service()
class Result(TypedDict):
    choice: str
    values: list[float]
@workflow(workflow_uuid="{WORKFLOW_UUID}", displayname="Human input")
def human_input() -> Result:
    # unilab:node_uuid=20000000-0000-4000-8000-000000000010
    chosen = human.choose(prompt="Select", options=["yes", "no"], labels=["Yes", "No"], image="", timeout_seconds=30)
    # unilab:node_uuid=20000000-0000-4000-8000-000000000011
    entered = human.numbers(prompt="Offsets", labels=["X", "Y"], defaults=[0.0, 0.0], image="", timeout_seconds=30)
    return workflow_output(choice=chosen.choice, values=entered.values)
'''


@pytest.mark.parametrize("restart", [False, "bridge", "execution"])
def test_human_commands_wait_validate_persist_and_resume_without_device(
    tmp_path, restart
):
    compiler = engine()
    compiled = _compile(compiler, SOURCE)
    assert compiled.valid, compiled.diagnostics
    again = _compile(compiler, compiled.normalized_python_source, graph=compiled.graph)
    assert again.valid and again.graph == compiled.graph, again.diagnostics
    plan, jobs = ExecutionPlanBuilder().build(
        compiled.graph, run_mode="normal", target_node_uuid=None
    )
    store = WorkflowStore(tmp_path / "human.db")
    store.create_workflow(
        workflow_uuid=WORKFLOW_UUID,
        name="Human",
        tags=[],
        description=None,
        meta_data={},
    )
    task_uuid = str(uuid4())
    store.create_task_with_jobs(
        workflow_uuid=WORKFLOW_UUID,
        task_uuid=task_uuid,
        run_mode="normal",
        target_node_uuid=None,
        description=None,
        meta_data={},
        plan_builder=lambda _: prepare_task_input(
            graph=compiled.graph, execution_plan=plan, jobs=jobs, raw_input={}
        ),
    )
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher)
    bridge = TaskSchedulerBridge(store, scheduler=scheduler)
    try:
        bridge.submit(store.get_task(task_uuid))
        assert store.get_task(task_uuid)["status"] != "succeeded"
        requests = bridge.human_interactions.list(task_uuid)
        assert len(requests) == 1 and requests[0]["status"] == "pending"
        first = requests[0]
        if restart:
            bridge.close()
            scheduler = EdgeScheduler(dispatcher=dispatcher)
            bridge = TaskSchedulerBridge(store, scheduler=scheduler)
            bridge.recover_active_tasks(
                execution_process_restarted=restart == "execution"
            )
            assert bridge.human_interactions.list(task_uuid)[0]["uuid"] == first["uuid"]
        service = WorkflowService(store, task_scheduler_bridge=bridge)
        with TestClient(create_workflow_app(service)) as client:
            path = f"/api/v1/workflow-human-interactions/{first['uuid']}/reply"
            if restart == "execution":
                # The current OS deliberately terminates tasks after an execution-process restart.
                assert client.post(path, json={"choice": "yes"}).status_code == 409
                assert store.get_task(task_uuid)["status"] == "failed"
                assert dispatcher.dispatched == []
                return
            assert client.post(path, json={"choice": "unknown"}).status_code == 400
            assert (
                client.post(path, json={"choice": "yes", "payload": {}}).status_code
                == 422
            )
            assert client.post(path, json={"choice": "yes"}).status_code == 200
            scheduler._wake_reconcile()
            assert client.post(path, json={"choice": "yes"}).status_code == 200
            assert client.post(path, json={"choice": "no"}).status_code == 409
            pending = [
                r
                for r in bridge.human_interactions.list(task_uuid)
                if r["status"] == "pending"
            ]
            assert len(pending) == 1 and pending[0]["uuid"] != first["uuid"]
            path2 = f"/api/v1/workflow-human-interactions/{pending[0]['uuid']}/reply"
            assert client.post(path2, json={"values": [1]}).status_code == 400
            assert client.post(path2, json={"values": [1.25, -0.5]}).status_code == 200
            scheduler._wake_reconcile()
        assert store.get_task(task_uuid)["status"] == "succeeded", store.get_task(
            task_uuid
        )
        assert store.get_task(task_uuid)["output"] == {
            "choice": "yes",
            "values": [1.25, -0.5],
        }
        assert dispatcher.dispatched == []
    finally:
        bridge.close()
        store.close()


def test_reply_validation_rejects_nonfinite_coordinates_and_wrong_forms():
    from unilabos.workflow.human_commands import HumanInteractionStore

    args = {"labels": ["X", "Y"]}
    for result in (
        {"values": [True, 1]},
        {"values": [float("nan"), 1]},
        {"values": [1]},
    ):
        with pytest.raises(ValueError):
            HumanInteractionStore.validate("numbers", args, result)
    with pytest.raises(ValueError):
        HumanInteractionStore.validate(
            "sketch", {}, {"x": [1, 2, 3], "y": [1, 2], "corner_x": [], "corner_y": []}
        )


def test_nested_human_loops_keep_outer_identity_separate_from_inner_decisions(tmp_path):
    source = f'''from unilabos.workflow.authoring import service, workflow, workflow_output, repeat_until, until
from unilabos.workflow.native_commands import HumanCommands
human: HumanCommands = service()
@workflow(workflow_uuid="{WORKFLOW_UUID}", displayname="Nested human loops")
def nested():
    # unilab:node_uuid=20000000-0000-4000-8000-000000000020
    with repeat_until(max_iterations=3, carry={{}}) as outer:
        # unilab:node_uuid=20000000-0000-4000-8000-000000000024
        seed = human.text(prompt="Seed", default="Inner", image="", timeout_seconds=30)
        # unilab:node_uuid=20000000-0000-4000-8000-000000000021
        with repeat_until(max_iterations=3, carry={{"prompt": seed.text}}) as inner:
            # unilab:node_uuid=20000000-0000-4000-8000-000000000022
            inside = human.choose(prompt=inner.carry["prompt"], options=["again", "done"], labels=["Again", "Done"], image="", timeout_seconds=30)
            inner.next(prompt=inner.carry["prompt"])
            until(inside.choice == "done" and inner.carry["prompt"] != "")
        # unilab:node_uuid=20000000-0000-4000-8000-000000000023
        outside = human.choose(prompt="Outer", options=["again", "done"], labels=["Again", "Done"], image="", timeout_seconds=30)
        until(outside.choice == "done")
    return workflow_output()
'''
    compiled = _compile(engine(), source)
    assert compiled.valid, compiled.diagnostics
    plan, jobs = ExecutionPlanBuilder().build(
        compiled.graph, run_mode="normal", target_node_uuid=None
    )
    store = WorkflowStore(tmp_path / "nested.db")
    store.create_workflow(
        workflow_uuid=WORKFLOW_UUID,
        name="Nested",
        tags=[],
        description=None,
        meta_data={},
    )
    task_uuid = str(uuid4())
    store.create_task_with_jobs(
        workflow_uuid=WORKFLOW_UUID,
        task_uuid=task_uuid,
        run_mode="normal",
        target_node_uuid=None,
        description=None,
        meta_data={},
        plan_builder=lambda _: prepare_task_input(
            graph=compiled.graph, execution_plan=plan, jobs=jobs, raw_input={}
        ),
    )
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher)
    bridge = TaskSchedulerBridge(store, scheduler=scheduler)
    seen = set()
    try:
        bridge.submit(store.get_task(task_uuid))
        for prompt, choice in [
            ("Seed", "Inner0"),
            ("Inner0", "again"),
            ("Inner0", "done"),
            ("Outer", "again"),
            ("Seed", "Inner1"),
            ("Inner1", "done"),
            ("Outer", "done"),
        ]:
            pending = [
                r
                for r in bridge.human_interactions.list(task_uuid)
                if r["status"] == "pending"
            ]
            assert len(pending) == 1
            request = pending[0]
            assert request["arguments"]["prompt"] == prompt
            assert request["uuid"] not in seen
            seen.add(request["uuid"])
            bridge.reply_human_interaction(
                request["uuid"], {"text" if prompt == "Seed" else "choice": choice}
            )
            scheduler._wake_reconcile()
        assert store.get_task(task_uuid)["status"] == "succeeded"
        assert dispatcher.dispatched == []
        # A conflicting replay remains forbidden after the first real decision.
        from unilabos.workflow.store import StoreConflict

        inner_job = next(
            j
            for j in store.list_jobs(task_uuid)
            if j["workflow_node_uuid"].endswith("000000000021")
        )
        with pytest.raises(StoreConflict, match="同一循环轮次"):
            bridge._projection.project_repeat_evaluation(
                control_job_uuid=inner_job["uuid"],
                iteration_index=1,
                condition_result=False,
                carry={"prompt": "Inner0"},
                next_carry={"prompt": "Inner0"},
            )
    finally:
        bridge.close()
        store.close()


@pytest.mark.parametrize("status", ["pending", "succeeded", "skipped"])
def test_unmaterialized_nested_repeat_releases_resources_only_with_terminal_controller(
    status,
):
    from unilabos.workflow.resource_lock_plan import completed_resource_nodes_for_job

    plan = {
        "nodes": [
            {"uuid": "outer", "kind": "repeat_until"},
            {"uuid": "inner", "kind": "repeat_until", "parent_uuid": "outer"},
            {"uuid": "action", "kind": "device_action", "parent_uuid": "inner"},
        ],
        "resource_plan": {"metadata": {}, "resources": [], "intervals": []},
    }
    job = {"workflow_node_uuid": "outer", "status": status}
    completed = completed_resource_nodes_for_job(job, [job], plan)
    assert ("action" in completed) == (status != "pending")
    unknown = {
        "workflow_node_uuid": "action",
        "status": "succeeded",
        "uncertainty_reason": "unsettled",
    }
    assert "action" not in completed_resource_nodes_for_job(job, [job, unknown], plan)
