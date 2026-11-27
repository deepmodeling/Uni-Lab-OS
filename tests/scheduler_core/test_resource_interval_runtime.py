"""资源占用区间的运行时连续取锁回归。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from typing import Any

import pytest

from unilabos.app.scheduler.dispatch import RecordingDispatcher
from unilabos.app.scheduler.models import (
    DispatchedJob,
    RepeatUntilRegion,
    WorkflowEdge,
    WorkflowNode,
    WorkflowSpec,
    spec_from_dict,
)
from unilabos.app.scheduler.service import EdgeScheduler, ExecutionPolicyError
from unilabos.workflow.resource_lock_key import material_lock_key
from unilabos.workflow.resource_lock_plan import (
    bind_station_resource_plan,
    compile_template_resource_plan,
    serialize_resource_plan,
)


def _plan(*node_ids: str) -> dict[str, object]:
    """构造一个跨连续节点持有同一设备资源的 bound 计划。"""

    graph = {
        "workflow_uuid": "workflow-interval-runtime",
        "nodes": [
            {"uuid": node_id, "resource_defaults": ["robot"]}
            for node_id in node_ids
        ],
        "edges": [
            {
                "source_node_uuid": source,
                "target_node_uuid": target,
            }
            for source, target in zip(node_ids, node_ids[1:])
        ],
    }
    template = compile_template_resource_plan(graph)
    bound = bind_station_resource_plan(
        template,
        {"robot": {"canonical_key": "/devices/shared", "kind": "device"}},
    )
    return serialize_resource_plan(bound)


def _spec(
    workflow_id: str,
    *,
    node_ids: list[str],
    priority: str,
) -> WorkflowSpec:
    nodes = [
        WorkflowNode(
            id=node_id,
            device_id="shared",
            action_name="run",
            action_type="goal",
            param={},
        )
        for node_id in node_ids
    ]
    plan = _plan(*node_ids)
    interval_by_node = {
        node_id: [
            str(interval["interval_id"])
            for interval in plan["intervals"]
            if node_id in interval["node_uuids"]
        ]
        for node_id in node_ids
    }
    plan_id = str(plan["plan_id"])
    for node in nodes:
        node.resource_plan_id = plan_id
        node.resource_interval_ids = interval_by_node[node.id]
        node.resource_acquire_set_id = next(
            (
                str(acquire_set["acquire_set_id"])
                for acquire_set in plan["acquire_sets"]
                if acquire_set["node_uuid"] == node.id
            ),
            "",
        )
    edges = [
        WorkflowEdge(
            uuid=f"{source}->{target}",
            source_node_id=source,
            target_node_id=target,
        )
        for source, target in zip(node_ids, node_ids[1:])
    ]
    return WorkflowSpec(
        workflow_id=workflow_id,
        nodes=nodes,
        edges=edges,
        priority=priority,
        resource_plan=plan,
    )


def test_repeat_body_can_run_between_redundantly_connected_device_actions() -> None:
    """首轮点样完成后进入两轮润洗，退出后才能放板，不能被自己的锁挡住。"""
    before, control, body, after = [
        f"10000000-0000-4000-8000-{i:012d}" for i in range(1, 5)
    ]
    graph = {"nodes": [
        {"uuid": before, "resource_defaults": ["sampling"]},
        {"uuid": control, "kind": "repeat_until"},
        {"uuid": body, "resource_defaults": ["sampling"], "parent_uuid": control},
        {"uuid": after, "resource_defaults": ["sampling"]},
    ], "edges": [{"source_node_uuid": a, "target_node_uuid": b}
                 for a, b in [(before, control), (control, body), (control, after), (before, after)]]}
    plan = serialize_resource_plan(bind_station_resource_plan(
        compile_template_resource_plan(graph),
        {"sampling": {"canonical_key": "/devices/sampling", "kind": "device"}},
    ))
    def action(uid):
        return WorkflowNode(
            id=uid, device_id="sampling", action_name=uid, action_type="goal",
            resource_plan_id=plan["plan_id"],
            resource_interval_ids=[i["interval_id"] for i in plan["intervals"] if uid in i["node_uuids"]],
            resource_acquire_set_id=next((a["acquire_set_id"] for a in plan["acquire_sets"] if a["node_uuid"] == uid), ""),
        )
    loop = WorkflowNode(id=control, executor_kind="repeat_until", param={
        "max_iterations": 3, "initial_carry": {}, "next_carry": {},
        "until": {"field": {"var": "result"}, "name": "done"},
        "bindings": {"result": {"kind": "node_result", "node_uuid": body}},
    })
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher)
    scheduler.submit_workflow(WorkflowSpec(
        workflow_id="loop-resource-runtime", nodes=[action(before), loop, action(after)],
        edges=[WorkflowEdge(uuid=f"{a}-{b}", source_node_id=a, target_node_id=b)
               for a, b in [(before, control), (control, after), (before, after)]],
        repeat_regions={control: RepeatUntilRegion(control_node_id=control, nodes=[action(body)])},
        resource_plan=plan,
    ))
    assert len(dispatcher.dispatched) == 1
    scheduler.on_job_finished(dispatcher.dispatched[-1]["job_id"], True, {})
    assert [j["action"] for j in dispatcher.dispatched] == [before, body]
    scheduler.on_job_finished(dispatcher.dispatched[-1]["job_id"], True, {"done": False})
    assert [j["action"] for j in dispatcher.dispatched] == [before, body, body]
    scheduler.on_job_finished(dispatcher.dispatched[-1]["job_id"], True, {"done": True})
    assert [j["action"] for j in dispatcher.dispatched] == [before, body, body, after]
    scheduler.on_job_finished(dispatcher.dispatched[-1]["job_id"], True, {})


def test_continuous_interval_wins_over_higher_priority_waiter() -> None:
    """同一连续区间的后继 Job 必须先于其他 Task 插入。"""

    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher)
    first = scheduler.submit_workflow(
        _spec("task-a", node_ids=["a-1", "a-2"], priority="normal")
    )
    scheduler.submit_workflow(
        _spec("task-b", node_ids=["b-1"], priority="urgent")
    )

    assert [item["node_id"] for item in dispatcher.dispatched] == ["a-1"]

    continuation = scheduler.on_job_finished(
        first["dispatched"][0]["job_id"],
        success=True,
    )

    assert [item["node_id"] for item in continuation["dispatched"]] == ["a-2"]
    assert [item["node_id"] for item in dispatcher.dispatched] == ["a-1", "a-2"]


def test_parallel_continuation_branches_are_serialized_on_one_resource() -> None:
    """同一区间的并行分支不能在同一轮同时绕过设备互斥。"""

    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher)
    graph = {
        "workflow_uuid": "workflow-interval-branch",
        "nodes": [
            {"uuid": node_id, "resource_defaults": ["robot"]}
            for node_id in ("root", "left", "right")
        ],
        "edges": [
            {"source_node_uuid": "root", "target_node_uuid": "left"},
            {"source_node_uuid": "root", "target_node_uuid": "right"},
        ],
    }
    plan = serialize_resource_plan(
        bind_station_resource_plan(
            compile_template_resource_plan(graph),
            {"robot": {"canonical_key": "/devices/shared", "kind": "device"}},
        )
    )
    nodes = [
        WorkflowNode(
            id=node_id,
            device_id="shared",
            action_name="run",
            action_type="goal",
            param={},
            resource_plan_id=str(plan["plan_id"]),
            resource_interval_ids=[
                str(interval["interval_id"])
                for interval in plan["intervals"]
                if node_id in interval["node_uuids"]
            ],
            resource_acquire_set_id=next(
                (
                    str(item["acquire_set_id"])
                    for item in plan["acquire_sets"]
                    if item["node_uuid"] == node_id
                ),
                "",
            ),
        )
        for node_id in ("root", "left", "right")
    ]
    spec = WorkflowSpec(
        workflow_id="task-branch",
        nodes=nodes,
        edges=[
            WorkflowEdge(uuid="root-left", source_node_id="root", target_node_id="left"),
            WorkflowEdge(uuid="root-right", source_node_id="root", target_node_id="right"),
        ],
        resource_plan=plan,
    )
    first = scheduler.submit_workflow(spec)
    scheduler.on_job_finished(first["dispatched"][0]["job_id"], success=True)

    continuation_nodes = [
        item["node_id"] for item in dispatcher.dispatched if item["node_id"] in {"left", "right"}
    ]
    assert len(continuation_nodes) == 1


def test_restore_rebuilds_persisted_interval_holder_before_successor_dispatch() -> None:
    """进程重启后从已成功 Job 的持久元数据恢复连续所有权。"""

    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher)
    spec = _spec("task-restore", node_ids=["a-1", "a-2"], priority="normal")
    plan = spec.resource_plan or {}
    interval_ids = [str(item["interval_id"]) for item in plan["intervals"]]
    restored = scheduler.restore_workflow(
        spec,
        {"a-1": {"restored": True}},
        restored_interval_handoffs=[
            {
                "node_id": "a-1",
                "job_id": "job-a1",
                "resource_interval_ids": interval_ids,
                "resource_interval_ids_by_lock": {
                    "/devices/shared": interval_ids,
                },
            }
        ],
    )

    assert [item["node_id"] for item in restored["dispatched"]] == ["a-2"]
    assert [item["node_id"] for item in dispatcher.dispatched] == ["a-2"]


def _parallel_manual_material_restore_fixture(
) -> tuple[WorkflowSpec, list[DispatchedJob], str]:
    """构造同 Task 共享 reservation 下的两个物料 active-use Job。"""

    material_uuid = "00000000-0000-4000-8000-000000000321"
    material_key = material_lock_key(material_uuid)
    node_ids = ("manual-a", "manual-b")
    device_ids = ("device-a", "device-b")
    graph = {
        "nodes": [
            {
                "uuid": node_id,
                "resource_defaults": [device_id, "payload"],
            }
            for node_id, device_id in zip(node_ids, device_ids)
        ],
        "resource_scopes": [
            {
                "scope_id": "shared-reservation",
                "kind": "with",
                "resources": ["payload"],
                "node_uuids": list(node_ids),
            }
        ],
    }
    plan = serialize_resource_plan(
        bind_station_resource_plan(
            compile_template_resource_plan(graph),
            {
                "device-a": {
                    "canonical_key": "/devices/device-a",
                    "kind": "device",
                },
                "device-b": {
                    "canonical_key": "/devices/device-b",
                    "kind": "device",
                },
                "payload": {"canonical_key": material_key, "kind": "material"},
            },
        )
    )
    keys_by_resource_id = {
        str(resource["resource_id"]): str(resource["canonical_key"])
        for resource in plan["resources"]
    }
    nodes: list[WorkflowNode] = []
    jobs: list[DispatchedJob] = []
    for node_id, device_id in zip(node_ids, device_ids):
        interval_ids = [
            str(interval["interval_id"])
            for interval in plan["intervals"]
            if node_id in interval["node_uuids"]
        ]
        acquire_set_id = next(
            (
                str(acquire_set["acquire_set_id"])
                for acquire_set in plan["acquire_sets"]
                if acquire_set["node_uuid"] == node_id
            ),
            "",
        )
        resource_keys = {
            keys_by_resource_id[str(interval["resource_id"])]
            for interval in plan["intervals"]
            if str(interval["interval_id"]) in interval_ids
        }
        nodes.append(
            WorkflowNode(
                id=node_id,
                device_id=device_id,
                action_name="run",
                action_type="goal",
                executor_kind="manual_confirm",
                node_type="manual_confirm",
                resource_plan_id=str(plan["plan_id"]),
                resource_interval_ids=interval_ids,
                resource_acquire_set_id=acquire_set_id,
            )
        )
        jobs.append(
            DispatchedJob(
                job_id=f"job-{node_id}",
                workflow_id="manual-restore-active-use",
                node_id=node_id,
                device_action_key=f"/devices/{device_id}/run",
                device_id=device_id,
                action_name="run",
                resource_lock_keys=resource_keys,
                # 模拟旧记录里非空但缺失物料的缓存；恢复边界不得信任。
                active_resource_lock_keys={f"/devices/{device_id}"},
                resource_plan_id=str(plan["plan_id"]),
                resource_interval_ids=interval_ids,
                resource_acquire_set_id=acquire_set_id,
            )
        )
    return (
        WorkflowSpec(
            workflow_id="manual-restore-active-use",
            nodes=nodes,
            edges=[],
            resource_plan=plan,
        ),
        jobs,
        material_key,
    )


def test_restore_recomputes_manual_active_use_and_rejects_overlap_atomically() -> None:
    """恢复载荷不能用伪造 active-use 让两个 Job 同时操作同一物料。"""

    spec, jobs, _material_key = _parallel_manual_material_restore_fixture()
    scheduler = EdgeScheduler(dispatcher=RecordingDispatcher())

    with pytest.raises(ExecutionPolicyError, match="在途作业资源冲突"):
        scheduler.restore_workflow(spec, {}, restored_jobs=jobs)

    assert scheduler.workflow_snapshot(spec.workflow_id) is None
    assert scheduler.snapshot()["inflight_jobs"] == {}


def test_manual_approval_rechecks_active_use_before_physical_dispatch() -> None:
    """批准时再次以冻结计划校验 active-use，冲突时保持等待。"""

    spec, jobs, material_key = _parallel_manual_material_restore_fixture()
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher)
    scheduler.restore_workflow(spec, {}, restored_jobs=jobs[:1])
    scheduler._inflight["other-active-job"] = DispatchedJob(
        job_id="other-active-job",
        workflow_id="other-workflow",
        node_id="other-node",
        device_action_key="/devices/other-device/run",
        device_id="other-device",
        action_name="run",
        active_resource_lock_keys={material_key},
    )

    approved = scheduler.resolve_manual_confirmation(jobs[0].job_id, approved=True)

    assert approved["dispatched"] == []
    assert dispatcher.dispatched == []


@pytest.mark.parametrize(
    "invalid_shape",
    [
        "missing_active_map",
        "extra_lock_key",
        "missing_interval_id",
        "unplanned_interval",
        "wrong_interval_member",
    ],
)
def test_restore_rejects_interval_handoff_outside_frozen_plan_atomically(
    invalid_shape: str,
) -> None:
    """恢复交接必须精确匹配活跃区间，失败后不得遗留运行态或资源占用。"""

    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher)
    spec = _graph_spec(
        "task-restore-invalid-handoff",
        {
            "nodes": [
                {"uuid": "a-1", "resource_defaults": ["robot", "tool"]},
                {"uuid": "a-2", "resource_defaults": ["robot", "tool"]},
                {
                    "uuid": "camera-action",
                    "device_id": "camera",
                    "resource_defaults": ["camera"],
                },
            ],
            "edges": [
                {"source_node_uuid": "a-1", "target_node_uuid": "a-2"},
            ],
        },
    )
    plan = spec.resource_plan or {}
    resource_ids = {
        str(item["alias"]): str(item["resource_id"]) for item in plan["resources"]
    }
    interval_ids = {
        alias: str(
            next(
                item["interval_id"]
                for item in plan["intervals"]
                if item["resource_id"] == resource_id
            )
        )
        for alias, resource_id in resource_ids.items()
    }
    completed_results = {"a-1": {"restored": True}}
    invalid_handoff = {
        "node_id": "a-1",
        "job_id": "job-a1-invalid",
        "resource_interval_ids": [interval_ids["robot"], interval_ids["tool"]],
        "resource_interval_ids_by_lock": {
            "/devices/robot": [interval_ids["robot"]],
            "/devices/tool": [interval_ids["tool"]],
        },
    }
    if invalid_shape == "missing_active_map":
        invalid_handoff["resource_interval_ids_by_lock"] = {}
    elif invalid_shape == "extra_lock_key":
        invalid_handoff["resource_interval_ids_by_lock"]["/devices/extra"] = [
            interval_ids["robot"]
        ]
    elif invalid_shape == "missing_interval_id":
        invalid_handoff["resource_interval_ids"].remove(interval_ids["tool"])
    elif invalid_shape == "unplanned_interval":
        invalid_handoff["resource_interval_ids"].append("interval-not-in-plan")
    else:
        completed_results["camera-action"] = {"restored": True}
        invalid_handoff["node_id"] = "camera-action"

    with pytest.raises(ExecutionPolicyError, match="恢复的连续资源交接"):
        scheduler.restore_workflow(
            spec,
            completed_results,
            restored_interval_handoffs=[invalid_handoff],
        )
    assert dispatcher.dispatched == []

    probe = scheduler.submit_workflow(
        _graph_spec(
            "task-restore-cleanup-probe",
            {"nodes": [{"uuid": "probe", "resource_defaults": ["robot", "tool"]}]},
        )
    )
    assert [item["node_id"] for item in probe["dispatched"]] == ["probe"]
    scheduler.on_job_finished(probe["dispatched"][0]["job_id"], success=True)

    restored = scheduler.restore_workflow(
        spec,
        completed_results,
        restored_interval_handoffs=[
            {
                "node_id": "a-1",
                "job_id": "job-a1-valid",
                "resource_interval_ids": [
                    interval_ids["robot"],
                    interval_ids["tool"],
                ],
                "resource_interval_ids_by_lock": {
                    "/devices/robot": [interval_ids["robot"]],
                    "/devices/tool": [interval_ids["tool"]],
                },
            }
        ],
    )
    assert "a-2" in {item["node_id"] for item in restored["dispatched"]}


def test_restore_skipped_tail_closes_interval_without_persisted_holder() -> None:
    """恢复时已跳过末端会关闭区间，已结束区间不再要求 holder 映射。"""

    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher)
    owner = _graph_spec(
        "task-restore-skipped-tail",
        {
            "nodes": [
                {"uuid": "a-1", "resource_defaults": ["robot"]},
                {"uuid": "a-2", "resource_defaults": ["robot"]},
                {
                    "uuid": "other",
                    "device_id": "camera",
                    "resource_defaults": ["camera"],
                },
            ],
            "edges": [
                {"source_node_uuid": "a-1", "target_node_uuid": "a-2"},
            ],
        },
    )
    plan = owner.resource_plan or {}
    robot_resource_id = next(
        item["resource_id"] for item in plan["resources"] if item["alias"] == "robot"
    )
    robot_interval_id = str(
        next(
            item["interval_id"]
            for item in plan["intervals"]
            if item["resource_id"] == robot_resource_id
        )
    )

    restored = scheduler.restore_workflow(
        owner,
        {"a-1": {"restored": True}},
        skipped_nodes={"a-2": "branch_not_selected"},
        restored_interval_handoffs=[
            {
                "node_id": "a-1",
                "job_id": "job-a1",
                "resource_interval_ids": [robot_interval_id],
                "resource_interval_ids_by_lock": {},
            }
        ],
    )
    assert [item["node_id"] for item in restored["dispatched"]] == ["other"]

    waiter = scheduler.submit_workflow(
        _graph_spec(
            "task-restore-skipped-tail-waiter",
            {"nodes": [{"uuid": "wait", "resource_defaults": ["robot"]}]},
        )
    )
    assert [item["node_id"] for item in waiter["dispatched"]] == ["wait"]


def test_restore_skipped_first_physical_node_does_not_open_interval() -> None:
    """首个物理节点仅被跳过时，后继物理节点仍可首次申请区间资源。"""

    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher)
    spec = _graph_spec(
        "task-restore-skipped-first",
        {
            "nodes": [
                {"uuid": "a-1", "resource_defaults": ["robot"]},
                {"uuid": "a-2", "resource_defaults": ["robot"]},
            ],
            "edges": [
                {"source_node_uuid": "a-1", "target_node_uuid": "a-2"},
            ],
        },
    )

    restored = scheduler.restore_workflow(
        spec,
        {},
        skipped_nodes={"a-1": "branch_not_selected"},
    )

    assert restored["state"] == "running"
    assert [item["node_id"] for item in restored["dispatched"]] == ["a-2"]


def _graph_spec(
    workflow_id: str,
    graph: Mapping[str, Any],
    *,
    resource_bindings: Mapping[str, Any] | None = None,
) -> WorkflowSpec:
    """从真实编译产物创建运行规格。"""
    aliases = {
        alias for node in graph["nodes"] for alias in node.get("resource_defaults", [])
    } | set(graph.get("resources", []))
    plan = serialize_resource_plan(
        bind_station_resource_plan(
            compile_template_resource_plan(graph),
            (
                resource_bindings
                if resource_bindings is not None
                else {
                    alias: {
                        "canonical_key": f"/devices/{alias}",
                        "kind": "device",
                    }
                    for alias in aliases
                }
            ),
        )
    )
    return WorkflowSpec(
        workflow_id=workflow_id,
        nodes=[
            WorkflowNode(
                id=n["uuid"],
                device_id=n.get("device_id", "robot"),
                action_name="run",
                action_type="goal",
                param={},
                executor_kind=n.get("executor_kind", "device_action"),
                resource_plan_id=plan["plan_id"],
                resource_interval_ids=[
                    i["interval_id"] for i in plan["intervals"] if n["uuid"] in i["node_uuids"]
                ],
                resource_acquire_set_id=next(
                    (
                        a["acquire_set_id"]
                        for a in plan["acquire_sets"]
                        if a["node_uuid"] == n["uuid"]
                    ),
                    "",
                ),
            )
            for n in graph["nodes"]
        ],
        edges=[
            WorkflowEdge(
                uuid=f"{e['source_node_uuid']}->{e['target_node_uuid']}",
                source_node_id=e["source_node_uuid"],
                target_node_id=e["target_node_uuid"],
            )
            for e in graph.get("edges", [])
        ],
        resource_plan=plan,
    )


def test_join_wait_holds_robot_until_actual_prepare_completion():
    scheduler = EdgeScheduler(dispatcher=RecordingDispatcher())
    graph = {
        "nodes": [
            {"uuid": "pick", "resource_defaults": ["robot"], "branch_id": "left"},
            {
                "uuid": "prepare",
                "resource_defaults": ["camera"],
                "device_id": "camera",
                "branch_id": "right",
            },
            {"uuid": "place", "resource_defaults": ["robot"]},
        ],
        "edges": [
            {"source_node_uuid": s, "target_node_uuid": "place"} for s in ("pick", "prepare")
        ],
    }
    first = scheduler.submit_workflow(_graph_spec("owner", graph))["dispatched"]
    jobs = {j["node_id"]: j["job_id"] for j in first}
    assert set(jobs) == {"pick", "prepare"}
    waiter = _graph_spec("waiter", {"nodes": [{"uuid": "wait", "resource_defaults": ["robot"]}]})
    waiter.priority = "high"
    assert scheduler.submit_workflow(waiter)["dispatched"] == []
    assert scheduler.on_job_finished(jobs["pick"], True)["dispatched"] == []
    place = scheduler.on_job_finished(jobs["prepare"], True)["dispatched"]
    assert [j["node_id"] for j in place] == ["place"]
    assert [
        j["node_id"] for j in scheduler.on_job_finished(place[0]["job_id"], True)["dispatched"]
    ] == ["wait"]


def test_station_admission_rejects_opposite_concurrent_plan_before_dispatch():
    import pytest

    from unilabos.workflow.resource_lock_plan import ResourcePlanError

    scheduler = EdgeScheduler(dispatcher=RecordingDispatcher())

    def graph(a, b):
        return {
            "nodes": [
                {"uuid": "first", "resource_defaults": [a], "device_id": a},
                {"uuid": "second", "resource_defaults": [a, b], "device_id": a},
            ],
            "edges": [{"source_node_uuid": "first", "target_node_uuid": "second"}],
        }

    scheduler.submit_workflow(_graph_spec("ab", graph("robot", "camera")))
    with pytest.raises(ResourcePlanError):
        scheduler.submit_workflow(_graph_spec("ba", graph("camera", "robot")))


def test_nonshared_continuation_cannot_be_reused_across_reschedule_rounds() -> None:
    """后继运行期间，晚就绪兄弟不能跨轮复用已完成前驱的旧连续占用。"""

    payload_uuid = "00000000-0000-4000-8000-000000000001"
    device_materials = {
        "root": "00000000-0000-4000-8000-000000000011",
        "prepare": "00000000-0000-4000-8000-000000000012",
        "left": "00000000-0000-4000-8000-000000000013",
        "right": "00000000-0000-4000-8000-000000000014",
    }
    graph = {
        "nodes": [
            {
                "uuid": node_id,
                "resource_defaults": [
                    *([] if node_id == "prepare" else ["payload"]),
                    f"{node_id}-device",
                ],
            }
            for node_id in ("root", "prepare", "left", "right")
        ],
        "edges": [
            {"source_node_uuid": "root", "target_node_uuid": "left"},
            {"source_node_uuid": "root", "target_node_uuid": "right"},
            {"source_node_uuid": "prepare", "target_node_uuid": "right"},
        ],
    }
    plan = serialize_resource_plan(
        bind_station_resource_plan(
            compile_template_resource_plan(graph),
            {
                "payload": {
                    "instance_uuid": payload_uuid,
                    "canonical_key": f"material/{payload_uuid}/exclusive",
                    "kind": "material",
                },
                **{
                    f"{node_id}-device": {
                        "instance_uuid": material_uuid,
                        "canonical_key": f"/devices/{material_uuid}",
                        "kind": "device",
                    }
                    for node_id, material_uuid in device_materials.items()
                },
            },
        )
    )
    nodes = [
        WorkflowNode(
            id=node_id,
            device_id=f"device-{node_id}",
            device_material_uuid=device_materials[node_id],
            action_name="run",
            action_type="goal",
            param={},
            resource_plan_id=str(plan["plan_id"]),
            resource_interval_ids=[
                str(interval["interval_id"])
                for interval in plan["intervals"]
                if node_id in interval["node_uuids"]
            ],
            resource_acquire_set_id=next(
                (
                    str(acquire_set["acquire_set_id"])
                    for acquire_set in plan["acquire_sets"]
                    if acquire_set["node_uuid"] == node_id
                ),
                "",
            ),
        )
        for node_id in ("root", "prepare", "left", "right")
    ]
    spec = WorkflowSpec(
        workflow_id="task-cross-round-continuation",
        nodes=nodes,
        edges=[
            WorkflowEdge(
                uuid=f"{source}->{target}",
                source_node_id=source,
                target_node_id=target,
            )
            for source, target in (
                ("root", "left"),
                ("root", "right"),
                ("prepare", "right"),
            )
        ],
        resource_plan=plan,
    )
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher)

    initial = scheduler.submit_workflow(spec)["dispatched"]
    initial_jobs = {item["node_id"]: item["job_id"] for item in initial}
    assert set(initial_jobs) == {"root", "prepare"}

    after_root = scheduler.on_job_finished(initial_jobs["root"], True)["dispatched"]
    assert [item["node_id"] for item in after_root] == ["left"]
    left_job_id = after_root[0]["job_id"]

    after_prepare = scheduler.on_job_finished(initial_jobs["prepare"], True)["dispatched"]
    assert after_prepare == []
    assert [item["node_id"] for item in dispatcher.dispatched].count("right") == 0

    after_left = scheduler.on_job_finished(left_job_id, True)["dispatched"]
    assert [item["node_id"] for item in after_left] == ["right"]
    scheduler.on_job_finished(after_left[0]["job_id"], True)


def test_restore_without_interval_handoff_fails_closed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """已执行过物理 Job 的连续区间丢失 holder 时，恢复不得重新取锁。"""

    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher)
    spec = _graph_spec(
        "task-restore-missing-holder",
        {
            "nodes": [
                {"uuid": "a-1", "resource_defaults": ["robot"]},
                {
                    "uuid": "a-2",
                    "resource_defaults": ["robot", "camera"],
                },
            ],
            "edges": [
                {"source_node_uuid": "a-1", "target_node_uuid": "a-2"},
            ],
        },
    )
    assert spec.nodes[1].resource_acquire_set_id
    restored = scheduler.restore_workflow(
        spec,
        {"a-1": {"restored": True}},
    )

    assert restored["state"] == "failed"
    assert restored["dispatched"] == []
    assert dispatcher.dispatched == []
    assert "已打开的连续资源区间缺少可继承所有权" in caplog.text


def test_completed_control_node_does_not_open_physical_interval() -> None:
    """区间从本地控制节点开始时，第一个真实物理 Job 仍可首次申请。"""

    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher)
    spec = _graph_spec(
        "task-restore-control-entry",
        {
            "nodes": [
                {
                    "uuid": "gate",
                    "executor_kind": "condition",
                    "resource_defaults": ["robot"],
                },
                {"uuid": "action", "resource_defaults": ["robot"]},
            ],
            "edges": [
                {"source_node_uuid": "gate", "target_node_uuid": "action"},
            ],
        },
    )

    restored = scheduler.restore_workflow(spec, {"gate": True})

    assert restored["state"] == "running"
    assert [item["node_id"] for item in restored["dispatched"]] == ["action"]


def test_node_must_freeze_the_exact_planned_acquire_set(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """节点不能删除计划要求的新增资源集合身份。"""

    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher)
    spec = _spec("task-missing-acquire-set", node_ids=["action"], priority="normal")
    spec.nodes[0].resource_acquire_set_id = ""

    submitted = scheduler.submit_workflow(spec)

    assert submitted["state"] == "failed"
    assert submitted["dispatched"] == []
    assert dispatcher.dispatched == []
    assert "节点新增资源集合投影与冻结资源计划不一致" in caplog.text


def test_first_physical_job_requires_the_frozen_resource_plan_identity() -> None:
    """冻结计划存在时，首个物理 Job 不能用空 plan_id 越过派发边界。"""

    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher)
    spec = _spec(
        "task-missing-resource-plan-id",
        node_ids=["first", "successor"],
        priority="normal",
    )
    spec.nodes[0].resource_plan_id = ""

    submitted = scheduler.submit_workflow(spec)

    assert submitted["state"] == "failed"
    assert submitted["dispatched"] == []
    assert dispatcher.dispatched == []


def test_parallel_default_continuations_cannot_both_inherit_one_resource() -> None:
    """默认连续区间只能由一个在途后继接管，不能把资源变成同任务共享锁。"""

    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher)
    payload_uuid = "00000000-0000-4000-8000-000000000401"
    graph = {
        "nodes": [
            {
                "uuid": "root",
                "device_id": "root-device",
                "resource_defaults": ["root-device", "payload"],
            },
            {
                "uuid": "left",
                "device_id": "left-device",
                "resource_defaults": ["left-device", "payload"],
            },
            {
                "uuid": "right",
                "device_id": "right-device",
                "resource_defaults": ["right-device", "payload"],
            },
        ],
        "edges": [
            {"source_node_uuid": "root", "target_node_uuid": "left"},
            {"source_node_uuid": "root", "target_node_uuid": "right"},
        ],
    }
    bindings = {
        device: {"canonical_key": f"/devices/{device}", "kind": "device"}
        for device in ("root-device", "left-device", "right-device")
    }
    bindings["payload"] = {
        "canonical_key": f"material/{payload_uuid}/exclusive",
        "kind": "material",
        "instance_uuid": payload_uuid,
    }
    submitted = scheduler.submit_workflow(
        _graph_spec("default-continuation", graph, resource_bindings=bindings)
    )
    root_job = submitted["dispatched"][0]["job_id"]

    scheduler.on_job_finished(root_job, success=True)

    successors = [
        item
        for item in dispatcher.dispatched
        if item["node_id"] in {"left", "right"}
    ]
    assert len(successors) == 1

    scheduler.on_job_finished(successors[0]["job_id"], success=True)
    remaining = [
        item
        for item in dispatcher.dispatched
        if item["node_id"] in {"left", "right"}
    ]
    assert len(remaining) == 2
    assert remaining[1]["node_id"] != successors[0]["node_id"]


def test_explicit_scope_reservation_does_not_share_active_material_use() -> None:
    """同 Task 可共享连续 reservation，但同一物料不能被两个 Job 同时操作。"""

    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher)
    payload_uuid = "00000000-0000-4000-8000-000000000402"
    graph = {
        "workflow_uuid": "active-use-inside-scope",
        "resources": ["payload"],
        "nodes": [
            {
                "uuid": "left",
                "device_id": "left-device",
                "resource_defaults": ["left-device", "payload"],
            },
            {
                "uuid": "right",
                "device_id": "right-device",
                "resource_defaults": ["right-device", "payload"],
            },
        ],
    }
    bindings = {
        "left-device": {
            "canonical_key": "/devices/left-device",
            "kind": "device",
        },
        "right-device": {
            "canonical_key": "/devices/right-device",
            "kind": "device",
        },
        "payload": {
            "instance_uuid": payload_uuid,
            "canonical_key": f"material/{payload_uuid}/exclusive",
            "kind": "material",
        },
    }

    scheduler.submit_workflow(
        _graph_spec(
            "active-use-inside-scope",
            graph,
            resource_bindings=bindings,
        )
    )

    first_wave = [
        item for item in dispatcher.dispatched if item["node_id"] in {"left", "right"}
    ]
    assert len(first_wave) == 1
    scheduler.on_job_finished(first_wave[0]["job_id"], success=True)
    assert {
        item["node_id"] for item in dispatcher.dispatched if item["node_id"] in {"left", "right"}
    } == {"left", "right"}


def test_runtime_restart_notifies_bridge_when_no_job_is_inflight() -> None:
    """Runtime 级崩溃不是 Job 事件；空账本也必须让桥接层失败等待任务。"""

    scheduler = EdgeScheduler(dispatcher=RecordingDispatcher())
    notifications: list[tuple[str, ...]] = []
    scheduler.add_execution_process_restarted_listener(notifications.append)

    scheduler.on_execution_process_restarted(())

    assert notifications == [()]


def _completed_material_source_boundary_spec() -> tuple[
    WorkflowSpec,
    dict[str, dict[str, str]],
]:
    """构造省略已完成来源节点、但资源计划仍保留其身份的规格。"""

    bindings = {
        alias: {"canonical_key": f"/devices/{alias}", "kind": "device"}
        for alias in ("common", "action-a", "action-b")
    }
    owner_spec = _graph_spec(
        "coordinator-owner",
        {
            "nodes": [
                {"uuid": "source", "executor_kind": "material_source"},
                {
                    "uuid": "action-a",
                    "device_id": "action-a",
                    "resource_defaults": ["action-a"],
                },
                {
                    "uuid": "action-b",
                    "device_id": "action-b",
                    "resource_defaults": ["action-b"],
                },
            ],
            "edges": [
                {"source_node_uuid": "source", "target_node_uuid": "action-a"},
                {"source_node_uuid": "action-a", "target_node_uuid": "action-b"},
            ],
            "resource_scopes": [
                {
                    "scope_id": "source-and-first-action",
                    "kind": "with",
                    "resources": ["common"],
                    "node_uuids": ["source", "action-a"],
                }
            ],
        },
        resource_bindings=bindings,
    )
    owner_spec.nodes = [node for node in owner_spec.nodes if node.id != "source"]
    owner_spec.edges = [
        edge
        for edge in owner_spec.edges
        if "source" not in {edge.source_node_id, edge.target_node_id}
    ]
    # WorkflowSpecCompiler 只把运行节点交给 EdgeScheduler，并显式投影省略协调器。
    owner_spec.resource_coordinator_node_ids = ["source"]
    return owner_spec, bindings


def test_completed_material_source_does_not_extend_resource_interval() -> None:
    """调度规格省略已完成来源节点时，资源仍须在声明的物理末端释放。"""

    owner_spec, bindings = _completed_material_source_boundary_spec()

    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher)
    submitted = scheduler.submit_workflow(owner_spec)
    assert [item["node_id"] for item in submitted["dispatched"]] == ["action-a"]

    waiter = _graph_spec(
        "coordinator-waiter",
        {
            "nodes": [
                {
                    "uuid": "wait",
                    "device_id": "common",
                    "resource_defaults": ["common"],
                }
            ]
        },
        resource_bindings={"common": bindings["common"]},
    )
    waiter.priority = "urgent"
    assert scheduler.submit_workflow(waiter)["dispatched"] == []

    released = scheduler.on_job_finished(
        submitted["dispatched"][0]["job_id"],
        success=True,
    )

    assert {item["node_id"] for item in released["dispatched"]} == {
        "action-b",
        "wait",
    }


def test_restore_does_not_reopen_interval_completed_by_material_source() -> None:
    """恢复时已完成协调器与物理末端共同证明区间已闭合。"""

    owner_spec, bindings = _completed_material_source_boundary_spec()
    plan = owner_spec.resource_plan or {}
    action_interval_ids = [
        str(interval["interval_id"])
        for interval in plan["intervals"]
        if "action-a" in interval["node_uuids"]
    ]
    resource_keys = {
        str(resource["resource_id"]): str(resource["canonical_key"])
        for resource in plan["resources"]
    }
    action_intervals_by_lock: dict[str, list[str]] = {}
    for interval in plan["intervals"]:
        if "action-a" not in interval["node_uuids"]:
            continue
        action_intervals_by_lock.setdefault(
            resource_keys[str(interval["resource_id"])],
            [],
        ).append(str(interval["interval_id"]))
    scheduler = EdgeScheduler(dispatcher=RecordingDispatcher())

    restored = scheduler.restore_workflow(
        owner_spec,
        {"action-a": {"restored": True}},
        restored_interval_handoffs=[
            {
                "node_id": "action-a",
                "job_id": "completed-action-a-job",
                "resource_interval_ids": action_interval_ids,
                "resource_interval_ids_by_lock": action_intervals_by_lock,
            }
        ],
    )
    assert [item["node_id"] for item in restored["dispatched"]] == ["action-b"]

    waiter = _graph_spec(
        "restored-coordinator-waiter",
        {
            "nodes": [
                {
                    "uuid": "wait",
                    "device_id": "common",
                    "resource_defaults": ["common"],
                }
            ]
        },
        resource_bindings={"common": bindings["common"]},
    )

    assert [
        item["node_id"] for item in scheduler.submit_workflow(waiter)["dispatched"]
    ] == ["wait"]


def test_pending_workflow_output_does_not_hold_physical_resource() -> None:
    """纯数据输出不进入运行 DAG，也不能把已完成动作的资源延长到整任务终态。"""

    bindings = {
        alias: {"canonical_key": f"/devices/{alias}", "kind": "device"}
        for alias in ("common", "action-a", "action-b")
    }
    owner = _graph_spec(
        "output-owner",
        {
            "nodes": [
                {
                    "uuid": "action-a",
                    "device_id": "action-a",
                    "resource_defaults": ["action-a"],
                },
                {"uuid": "output", "executor_kind": "workflow_output"},
                {
                    "uuid": "action-b",
                    "device_id": "action-b",
                    "resource_defaults": ["action-b"],
                },
            ],
            "edges": [
                {"source_node_uuid": "action-a", "target_node_uuid": "output"},
            ],
            "resource_scopes": [
                {
                    "scope_id": "action-output",
                    "kind": "with",
                    "resources": ["common"],
                    "node_uuids": ["action-a", "output"],
                }
            ],
        },
        resource_bindings=bindings,
    )
    owner.nodes = [node for node in owner.nodes if node.id != "output"]
    owner.edges = [
        edge
        for edge in owner.edges
        if "output" not in {edge.source_node_id, edge.target_node_id}
    ]
    owner.resource_coordinator_node_ids = ["output"]
    scheduler = EdgeScheduler(dispatcher=RecordingDispatcher())
    first = scheduler.submit_workflow(owner)["dispatched"]
    first_jobs = {item["node_id"]: item["job_id"] for item in first}
    assert set(first_jobs) == {"action-a", "action-b"}

    waiter = _graph_spec(
        "output-waiter",
        {
            "nodes": [
                {
                    "uuid": "wait",
                    "device_id": "common",
                    "resource_defaults": ["common"],
                }
            ]
        },
        resource_bindings={"common": bindings["common"]},
    )
    assert scheduler.submit_workflow(waiter)["dispatched"] == []

    released = scheduler.on_job_finished(first_jobs["action-a"], success=True)

    assert [item["node_id"] for item in released["dispatched"]] == ["wait"]


def test_completed_local_control_closes_interval_without_skipped_branch() -> None:
    """本地控制节点完成时，即使没有跳过分支也必须重算资源释放边界。"""

    bindings = {
        alias: {"canonical_key": f"/devices/{alias}", "kind": "device"}
        for alias in ("common", "first", "parallel", "selected")
    }
    owner = _graph_spec(
        "control-boundary-owner",
        {
            "nodes": [
                {
                    "uuid": "first",
                    "device_id": "first",
                    "resource_defaults": ["first"],
                },
                {"uuid": "gate", "executor_kind": "condition"},
                {
                    "uuid": "selected",
                    "device_id": "selected",
                    "resource_defaults": ["selected"],
                },
                {
                    "uuid": "parallel",
                    "device_id": "parallel",
                    "resource_defaults": ["parallel"],
                },
            ],
            "edges": [
                {"source_node_uuid": "first", "target_node_uuid": "gate"},
                {"source_node_uuid": "gate", "target_node_uuid": "selected"},
            ],
            "resource_scopes": [
                {
                    "scope_id": "until-control",
                    "kind": "with",
                    "resources": ["common"],
                    "node_uuids": ["first", "gate"],
                }
            ],
        },
        resource_bindings=bindings,
    )
    gate = next(node for node in owner.nodes if node.id == "gate")
    gate.param = {
        "variables": {"selected": True},
        "bindings": {
            "selected": {
                "kind": "workflow_input",
                "parameter": "selected",
            }
        },
        "branches": [
            {
                "label": "if",
                "condition": {"var": "selected"},
                "node_uuids": ["selected"],
                "entry_node_uuids": ["selected"],
                "exit_node_uuids": ["selected"],
            }
        ],
        "predecessor_node_uuids": ["first"],
    }
    scheduler = EdgeScheduler(dispatcher=RecordingDispatcher())
    initial = scheduler.submit_workflow(owner)["dispatched"]
    initial_jobs = {item["node_id"]: item["job_id"] for item in initial}
    assert set(initial_jobs) == {"first", "parallel"}

    waiter = _graph_spec(
        "control-boundary-waiter",
        {
            "nodes": [
                {
                    "uuid": "wait",
                    "device_id": "common",
                    "resource_defaults": ["common"],
                }
            ]
        },
        resource_bindings={"common": bindings["common"]},
    )
    assert scheduler.submit_workflow(waiter)["dispatched"] == []

    released = scheduler.on_job_finished(initial_jobs["first"], success=True)

    assert {item["node_id"] for item in released["dispatched"]} == {
        "selected",
        "wait",
    }


def test_wire_cannot_mark_missing_physical_node_as_resource_coordinator() -> None:
    """外部规格不能伪造协调器身份来提前释放缺失物理成员的资源。"""

    trusted, bindings = _completed_material_source_boundary_spec()
    raw_spec = {
        "workflow_id": "forged-coordinator-owner",
        "nodes": [asdict(node) for node in trusted.nodes],
        "edges": [asdict(edge) for edge in trusted.edges],
        "resource_plan": trusted.resource_plan,
        "resource_coordinator_node_ids": ["source"],
    }
    forged = spec_from_dict(raw_spec)
    assert forged.resource_coordinator_node_ids == []

    scheduler = EdgeScheduler(dispatcher=RecordingDispatcher())
    submitted = scheduler.submit_workflow(forged)
    waiter = _graph_spec(
        "forged-coordinator-waiter",
        {
            "nodes": [
                {
                    "uuid": "wait",
                    "device_id": "common",
                    "resource_defaults": ["common"],
                }
            ]
        },
        resource_bindings={"common": bindings["common"]},
    )
    assert scheduler.submit_workflow(waiter)["dispatched"] == []

    after_first = scheduler.on_job_finished(
        submitted["dispatched"][0]["job_id"],
        success=True,
    )

    assert [item["node_id"] for item in after_first["dispatched"]] == ["action-b"]

@pytest.mark.parametrize('overlap,gap', [(('b', 'c'), False), (('d', 'e'), False), (('d', 'e'), True)])
def test_manual_reservation_overlap_blocks_competing_task_without_release_gap(overlap, gap):
    """自动搬运与手工范围重叠时，共同资源不在内部边界释放。"""
    graph = {
        'nodes': [{'uuid': n, 'resources': ['robot']} for n in 'abcde'],
        'edges': [{'source_node_uuid': a, 'target_node_uuid': b} for a, b in zip('abcd', 'bcde')],
        'resource_scopes': [
            {'scope_id': 'manual', 'kind': 'reservation', 'resources': ['robot'], 'node_uuids': list('abcd')},
            {'scope_id': 'auto', 'kind': 'transfer', 'resources': ['robot'], 'node_uuids': list(overlap)},
        ],
    }
    if gap:
        graph['nodes'][2]['resources'] = ['other']
    plan = serialize_resource_plan(bind_station_resource_plan(
        compile_template_resource_plan(graph),
        {'robot': {'canonical_key': '/devices/shared', 'kind': 'device'},
         'other': {'canonical_key': '/devices/other', 'kind': 'device'}},
    ))
    spec = _spec('reservation-owner', node_ids=list('abcde'), priority='normal')
    spec.resource_plan = plan
    for node in spec.nodes:
        if gap and node.id == 'c':
            node.device_id = 'other'
        node.resource_plan_id = plan['plan_id']
        node.resource_interval_ids = [i['interval_id'] for i in plan['intervals'] if node.id in i['node_uuids']]
        node.resource_acquire_set_id = next((a['acquire_set_id'] for a in plan['acquire_sets'] if a['node_uuid'] == node.id), '')
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher)
    scheduler.submit_workflow(spec)
    scheduler.submit_workflow(_spec('competitor', node_ids=['waiter'], priority='urgent'))
    protected = 'abcde' if overlap == ('d', 'e') else 'abcd'
    for n in protected:
        assert dispatcher.dispatched[-1]['node_id'] == n
        assert 'waiter' not in [p['node_id'] for p in dispatcher.dispatched]
        scheduler.on_job_finished(dispatcher.dispatched[-1]['job_id'], success=True)
    assert dispatcher.dispatched[-1]['node_id'] == 'waiter'
