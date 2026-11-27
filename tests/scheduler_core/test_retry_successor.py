"""当前节点重试成功后，同设备后继必须继续派发。"""

from tests.scheduler_core.conftest import stable_uuid
from tests.scheduler_core.test_control_flow import dependency, device_node, job
from unilabos.workflow.resource_lock_plan import (
    bind_station_resource_plan,
    compile_template_resource_plan,
    serialize_resource_plan,
)


def test_retry_middle_node_continues_implicit_device_interval(core_runtime):
    runtime = core_runtime
    nodes = [
        device_node(
            node_uuid=stable_uuid(f"retry-middle-node:{i}"),
            parent_uuid=None,
            device_id="reactor-a",
            material_uuid=runtime.device_materials["reactor-a"],
            action="run",
        )
        for i in range(3)
    ]
    edges = [dependency(nodes[i]["uuid"], nodes[i + 1]["uuid"], name=f"retry-middle:{i}") for i in range(2)]
    for node in nodes:
        node["resource_defaults"] = ["reactor-a"]
    plan = serialize_resource_plan(bind_station_resource_plan(
        compile_template_resource_plan({"nodes": nodes, "edges": edges}),
        {"reactor-a": {"canonical_key": f"/devices/{runtime.device_materials['reactor-a']}", "kind": "device"}},
    ))
    for node in nodes:
        node["resource_plan_id"] = plan["plan_id"]
        node["resource_interval_ids"] = [i["interval_id"] for i in plan["intervals"] if node["uuid"] in i["node_uuids"]]
        node["resource_acquire_set_id"] = next(
            (i["acquire_set_id"] for i in plan["acquire_sets"] if node["uuid"] == i["node_uuid"]), "",
        )
    jobs = [job(job_uuid=stable_uuid(f"retry-middle-job:{i}"), node_uuid=n["uuid"], index=i, kind="device_action")
            for i, n in enumerate(nodes)]
    aggregate = runtime.submit_frozen(task_name="retry-middle", execution_plan={
        "version": 1, "run_mode": "normal", "nodes": nodes, "edges": edges,
        "handles": [], "resource_plan": plan, "capabilities": plan["capabilities"],
    }, jobs=jobs)
    task_uuid = aggregate["task"]["uuid"]
    runtime.scheduler.on_job_finished(jobs[0]["uuid"], True, {})
    runtime.scheduler.on_job_finished(jobs[1]["uuid"], False, {"error": "expected failure"})
    control = runtime.bridge.error_control
    snapshot = control.snapshot()
    error = snapshot["errors"][0]
    accepted = runtime.bridge.decide_error(error["decision_id"], {
        "action": "retry_current_node", "expected_decision_version": error["decision_version"],
        "expected_station_version": snapshot["station_version"], "observed_error_epoch": snapshot["error_epoch"],
        "resume_requested": True, "operator_confirmed": True, "confirm_snapshot_id": snapshot["snapshot_id"],
    }, idempotency_key="retry-middle", operator_id="tester")
    retry = accepted["result"]["retry_job_uuid"]
    assert runtime.dispatcher.dispatched[-1]["job_id"] == retry
    runtime.scheduler.on_job_finished(retry, True, {})
    assert runtime.dispatcher.dispatched[-1]["job_id"] == jobs[2]["uuid"], runtime.scheduler.snapshot()
    runtime.scheduler.on_job_finished(jobs[2]["uuid"], True, {})
    assert runtime.workflow_store.get_task(task_uuid)["status"] == "succeeded"
    assert runtime.workflow_store.get_job(jobs[1]["uuid"])["status"] == "failed"
