"""不同时间结束的共同区间必须分别清理库存 Claim 和工作流镜像。"""

from pathlib import Path
from typing import Any

from tests.scheduler_core.conftest import build_core_runtime, stable_uuid
from unilabos.workflow.resource_lock_plan import (
    bind_station_resource_plan,
    compile_template_resource_plan,
    serialize_resource_plan,
)


def test_finished_interval_releases_waiter_while_other_interval_stays_owned(tmp_path: Path) -> None:
    runtime = build_core_runtime(
        tmp_path,
        device_ids=("executor-0", "executor-1", "executor-2", "executor-3", "scope-a", "scope-b"),
    )
    try:
        nodes: list[dict[str, Any]] = [
            {
                "uuid": stable_uuid(f"partial-node-{index}"),
                "kind": "device_action",
                "device_id": f"executor-{index}",
                "material_uuid": runtime.device_materials[f"executor-{index}"],
                "action_name": "run",
                "action_type": "UniLabJsonCommand",
                "param": {},
                "param_schema": {
                    "type": "object",
                    "properties": {"goal": {"type": "object", "properties": {}}},
                },
                "execution_policy": {},
                "resource_defaults": [f"executor-{index}"],
            }
            for index in range(4)
        ]
        edges = [
            {
                "uuid": stable_uuid(f"partial-edge-{node['uuid']}"),
                "source_node_uuid": nodes[0]["uuid"],
                "target_node_uuid": node["uuid"],
                "source_handle_uuid": "",
                "target_handle_uuid": "",
                "dependency_only": True,
                "source_data_key": "",
                "target_data_key": "",
                "source_type": "",
                "target_type": "",
            }
            for node in nodes[1:]
        ]
        plan = serialize_resource_plan(
            bind_station_resource_plan(
                compile_template_resource_plan(
                    {
                        "nodes": nodes,
                        "edges": edges,
                        "resource_scopes": [
                            {
                                "kind": "with",
                                "scope_id": "scope-a",
                                "parent_scope_id": "scope-b",
                                "resources": ["scope-a"],
                                "node_uuids": [node["uuid"] for node in nodes[:3]],
                            },
                            {
                                "kind": "root",
                                "scope_id": "scope-b",
                                "resources": ["scope-b"],
                                "node_uuids": [node["uuid"] for node in nodes],
                            },
                        ],
                    }
                ),
                {
                    name: {"canonical_key": f"/devices/{uuid}", "kind": "device"}
                    for name, uuid in runtime.device_materials.items()
                },
            )
        )
        job_ids = [stable_uuid(f"partial-job-{index}") for index in range(4)]
        for node in nodes:
            node["resource_plan_id"] = plan["plan_id"]
            node["resource_interval_ids"] = [
                interval["interval_id"]
                for interval in plan["intervals"]
                if node["uuid"] in interval["node_uuids"]
            ]
            node["resource_acquire_set_id"] = next(
                (
                    item["acquire_set_id"]
                    for item in plan["acquire_sets"]
                    if item["node_uuid"] == node["uuid"]
                ),
                "",
            )
        owner = runtime.submit_frozen(
            task_name="partial-cleanup",
            execution_plan={
                "version": 1,
                "run_mode": "normal",
                "target_node_uuid": None,
                "nodes": nodes,
                "edges": edges,
                "handles": [],
                "capabilities": plan["capabilities"],
                "resource_plan": plan,
            },
            jobs=[
                {
                    "uuid": job_id,
                    "workflow_node_uuid": node["uuid"],
                    "topological_index": index,
                    "executor_kind": "device_action",
                    "execution_policy": {},
                    "param": {},
                }
                for index, (job_id, node) in enumerate(zip(job_ids, nodes))
            ],
        )
        task_uuid = owner["task"]["uuid"]
        runtime.submit(task_name="wait-a", devices=["scope-a"], priority="high")
        runtime.submit(task_name="wait-b", devices=["scope-b"], priority="high")
        assert len(runtime.dispatcher.dispatched) == 1
        runtime.scheduler.on_job_finished(job_ids[0], True, {})
        assert {payload["job_id"] for payload in runtime.dispatcher.dispatched} == set(job_ids)
        runtime.scheduler.on_job_finished(job_ids[1], True, {})
        assert len(runtime.dispatcher.dispatched) == 4
        runtime.scheduler.on_job_finished(job_ids[2], True, {})
        assert len(runtime.dispatcher.dispatched) == 4
        key_a = f"/devices/{runtime.device_materials['scope-a']}"
        key_b = f"/devices/{runtime.device_materials['scope-b']}"
        active = runtime.inventory_store.query_all(
            "SELECT lease.lock_key FROM station_execution_lock_lease lease "
            "JOIN station_execution_claim claim USING(claim_uuid) "
            "WHERE claim.task_uuid=? AND lease.state IN ('prepared','reserved','running','uncertain')",
            (task_uuid,),
        )
        assert key_a in {row["lock_key"] for row in active}
        assert key_b in {row["lock_key"] for row in active}
        with runtime.workflow_store.read() as connection:
            mirrors = connection.execute(
                "SELECT lock_key FROM execution_lock_lease WHERE workflow_task_uuid=? "
                "AND state IN ('reserved','running','uncertain')",
                (task_uuid,),
            ).fetchall()
            assert key_a in {row["lock_key"] for row in mirrors}
            assert key_b in {row["lock_key"] for row in mirrors}
        runtime.scheduler.on_job_finished(job_ids[3], True, {})
        assert {payload["job_id"] for payload in runtime.dispatcher.dispatched[-2:]} == {
            stable_uuid("job:wait-a:0"),
            stable_uuid("job:wait-b:0"),
        }
        assert (
            runtime.inventory_store.query_all(
                "SELECT claim_uuid FROM station_execution_claim WHERE task_uuid=? "
                "AND state IN ('prepared','reserved','running','uncertain')",
                (task_uuid,),
            )
            == []
        )
        with runtime.workflow_store.read() as connection:
            assert (
                connection.execute(
                    "SELECT uuid FROM execution_lock_lease WHERE workflow_task_uuid=? "
                    "AND state IN ('reserved','running','uncertain')",
                    (task_uuid,),
                ).fetchall()
                == []
            )
    finally:
        runtime.close()
