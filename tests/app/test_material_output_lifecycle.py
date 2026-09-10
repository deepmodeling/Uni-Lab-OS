from unilabos.app.scheduler.dispatch import RecordingDispatcher
from unilabos.app.scheduler.inventory.service import InventoryService
from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.app.scheduler.models import WorkflowEdge, WorkflowNode, WorkflowSpec
from unilabos.app.scheduler.service import EdgeScheduler


def test_successful_action_registers_output_with_content_and_stack_location():
    inventory = InventoryService(InventoryStore(":memory:"))
    inventory.register_instance(template_id="stack", edge_uuid="stack-1")
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher, inventory=inventory)
    spec = WorkflowSpec(
        workflow_id="wf-brine",
        run_id="run-brine-1",
        task_id="task-brine",
        nodes=[
            WorkflowNode(
                id="mix",
                device_id="stirrer-1",
                action_name="stir",
                action_type="goal",
                material_outputs=[
                    {
                        "output_name": "brine",
                        "template_id": "brine-solution",
                        "parent_uuid": "stack-1",
                        "slot_id": "S03",
                    }
                ],
            )
        ],
    )

    result = scheduler.submit_workflow(spec)
    job_id = result["dispatched"][0]["job_id"]
    scheduler.on_job_finished(
        job_id,
        True,
        {
            "material_outputs": [
                {
                    "output_name": "brine",
                    "content": {
                        "quantity": 20,
                        "unit": "mL",
                        "components": [
                            {"template_id": "water", "quantity": 20, "unit": "mL"},
                            {"template_id": "sodium-chloride", "quantity": 2, "unit": "g"},
                        ],
                    },
                }
            ]
        },
        run_id="run-brine-1",
    )

    output = inventory.store.get_instance("material-output:run-brine-1:mix:brine")
    assert output is not None
    assert output["template_id"] == "brine-solution"
    assert output["status"] == "stored"
    assert output["parent_uuid"] == "stack-1"
    assert inventory.store.get_relation(output["edge_uuid"])["slot_id"] == "S03"
    assert inventory.store.get_content(output["edge_uuid"])["state_json"]

    consumed = inventory.consume_material_content(
        output["edge_uuid"], 20, quantity_key="quantity", unit="mL"
    )
    assert consumed["status"] == "consumed"
    assert inventory.store.get_instance(output["edge_uuid"])["status"] == "consumed"
    assert inventory.store.get_relation(output["edge_uuid"]) is None


def test_failed_run_discards_intermediate_output():
    inventory = InventoryService(InventoryStore(":memory:"))
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher, inventory=inventory)
    spec = WorkflowSpec(
        workflow_id="wf-failed-output",
        run_id="run-failed-output",
        nodes=[
            WorkflowNode(
                id="produce",
                device_id="bench-1",
                action_name="make",
                action_type="goal",
                material_outputs=[
                    {"output_name": "product", "template_id": "product-template"}
                ],
            ),
            WorkflowNode(
                id="verify",
                device_id="bench-2",
                action_name="verify",
                action_type="goal",
            ),
        ],
        edges=[
            # 只建立执行依赖，不涉及物料句柄。
            WorkflowEdge(uuid="produce->verify", source_node_id="produce", target_node_id="verify")
        ],
    )
    job_id = scheduler.submit_workflow(spec)["dispatched"][0]["job_id"]
    scheduler.on_job_finished(
        job_id,
        True,
        {"material_outputs": [{"output_name": "product", "content": {"quantity": 1}}]},
        run_id="run-failed-output",
    )

    output_uuid = "material-output:run-failed-output:produce:product"
    assert inventory.store.get_instance(output_uuid)["status"] == "warehouse"
    verify_job = scheduler.snapshot()["inflight_jobs"]
    verify_job_id = next(iter(verify_job))
    scheduler.on_job_finished(verify_job_id, False, run_id="run-failed-output")
    assert inventory.store.get_instance(output_uuid)["status"] == "discarded"


def test_scheduler_applies_custody_effects_and_gates_following_action():
    inventory = InventoryService(InventoryStore(":memory:"))
    inventory.register_instance(template_id="beaker", edge_uuid="beaker-1")
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher, inventory=inventory)
    spec = WorkflowSpec(
        workflow_id="wf-custody",
        run_id="run-custody",
        nodes=[
            WorkflowNode(
                id="pick",
                device_id="robot-arm",
                action_name="pick",
                material_requirements=[],
                material_preconditions=[
                    {"material_uuid": "beaker-1", "location_kind": "warehouse"}
                ],
                material_effects=[
                    {
                        "material_uuid": "beaker-1",
                        "location_kind": "held",
                        "holder_kind": "device",
                        "holder_uuid": "robot-arm",
                        "expected_location_kind": "warehouse",
                        "expected_holder_kind": "",
                    }
                ],
            ),
            WorkflowNode(
                id="place",
                device_id="robot-arm",
                action_name="place_at_water",
                material_preconditions=[
                    {
                        "material_uuid": "beaker-1",
                        "location_kind": "held",
                        "holder_kind": "device",
                        "holder_uuid": "robot-arm",
                    }
                ],
                material_effects=[
                    {
                        "material_uuid": "beaker-1",
                        "location_kind": "station",
                        "location_uuid": "water-station",
                        "holder_kind": "station",
                        "holder_uuid": "water-station",
                        "expected_location_kind": "held",
                        "expected_holder_kind": "device",
                        "expected_holder_uuid": "robot-arm",
                    }
                ],
            ),
        ],
        edges=[WorkflowEdge(uuid="pick->place", source_node_id="pick", target_node_id="place")],
    )

    first = scheduler.submit_workflow(spec)
    assert [item["action"] for item in dispatcher.dispatched] == ["pick"]
    assert inventory.material_custody("beaker-1")["location_kind"] == "warehouse"

    scheduler.on_job_finished(first["dispatched"][0]["job_id"], True, {}, run_id="run-custody")
    assert [item["action"] for item in dispatcher.dispatched] == ["pick", "place_at_water"]
    assert inventory.material_custody("beaker-1")["holder_uuid"] == "robot-arm"

    scheduler.on_job_finished(
        scheduler.snapshot()["inflight_jobs"]
        and next(iter(scheduler.snapshot()["inflight_jobs"])),
        True,
        {},
        run_id="run-custody",
    )
    custody = inventory.material_custody("beaker-1")
    assert custody["location_kind"] == "station"
    assert custody["location_uuid"] == "water-station"
