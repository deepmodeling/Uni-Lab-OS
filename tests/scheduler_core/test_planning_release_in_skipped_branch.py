"""资源区间以条件分支收尾时，规划视图的释放点必须跟随实际选中的分支。

冻结计划把区间链按拓扑序的最后一个成员当作释放点。显式资源区间里包着一个条件、
各分支都使用同一设备时，这个成员只属于其中一个分支。选中另一分支后它被跳过，
规划视图若仍以它为释放点，就会把区间当作开放占用延伸到排程视界末端，区间之后
使用同一设备的动作在 CP-SAT 里无解，整条任务停派。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from unilabos.app.scheduler.dispatch import RecordingDispatcher
from unilabos.app.scheduler.models import WorkflowEdge, WorkflowNode, WorkflowSpec
from unilabos.app.scheduler.service import EdgeScheduler
from unilabos.app.scheduler.workflow_planning import build_workflow_planning_view
from unilabos.workflow.resource_lock_plan import (
    bind_station_resource_plan,
    compile_template_resource_plan,
    serialize_resource_plan,
)

WORKFLOW_ID = "scope-ends-in-branch"


def _spec(graph: Mapping[str, Any]) -> WorkflowSpec:
    plan = serialize_resource_plan(
        bind_station_resource_plan(
            compile_template_resource_plan(graph),
            {"robot": {"canonical_key": "/devices/robot", "kind": "device"}},
        )
    )
    return WorkflowSpec(
        workflow_id=WORKFLOW_ID,
        nodes=[
            WorkflowNode(
                id=n["uuid"],
                device_id="robot",
                action_name="run",
                action_type="goal",
                param={},
                executor_kind=n.get("executor_kind", "device_action"),
                resource_plan_id=plan["plan_id"],
                resource_interval_ids=[
                    i["interval_id"] for i in plan["intervals"] if n["uuid"] in i["node_uuids"]
                ],
                resource_acquire_set_id=next(
                    (a["acquire_set_id"] for a in plan["acquire_sets"] if a["node_uuid"] == n["uuid"]),
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
            for e in graph["edges"]
        ],
        priority="normal",
        resource_plan=plan,
    )


def _scenario() -> tuple[WorkflowSpec, dict[str, Any]]:
    """取件 → 条件(两分支各两步机械臂动作) → 区间外的登记动作，区间覆盖取件到分支末尾。"""

    robot = ["robot"]
    graph = {
        "workflow_uuid": WORKFLOW_ID,
        "nodes": [
            {"uuid": "pick", "resource_defaults": robot},
            {"uuid": "gate", "executor_kind": "condition"},
            {"uuid": "a1", "resource_defaults": robot},
            {"uuid": "a2", "resource_defaults": robot},
            {"uuid": "b1", "resource_defaults": robot},
            {"uuid": "b2", "resource_defaults": robot},
            {"uuid": "after", "resource_defaults": robot},
        ],
        "edges": [
            {"source_node_uuid": s, "target_node_uuid": t}
            for s, t in [
                ("pick", "gate"), ("gate", "a1"), ("a1", "a2"),
                ("gate", "b1"), ("b1", "b2"), ("a2", "after"), ("b2", "after"),
            ]
        ],
        "resource_scopes": [
            {
                "scope_id": "put",
                "kind": "with",
                "resources": robot,
                "node_uuids": ["pick", "gate", "a1", "a2", "b1", "b2"],
            }
        ],
    }
    spec = _spec(graph)
    scoped = [
        i for i in spec.resource_plan["intervals"]
        if i["explicit_boundary"] and "pick" in i["node_uuids"]
    ]
    assert len(scoped) == 1
    interval = scoped[0]
    static_release = interval["release_node_uuid"]
    assert static_release in {"a2", "b2"}
    # 选中不含静态释放点的那个分支。
    take_a = static_release == "b2"
    gate = next(node for node in spec.nodes if node.id == "gate")
    gate.param = {
        "variables": {"take_a": take_a},
        "bindings": {},
        "branches": [
            {
                "label": "if",
                "condition": {"var": "take_a"},
                "node_uuids": ["a1", "a2"],
                "entry_node_uuids": ["a1"],
                "exit_node_uuids": ["a2"],
            },
            {
                "label": "else",
                "condition": None,
                "node_uuids": ["b1", "b2"],
                "entry_node_uuids": ["b1"],
                "exit_node_uuids": ["b2"],
            },
        ],
        "predecessor_node_uuids": ["pick"],
    }
    return spec, interval


def test_release_in_untaken_branch_moves_to_the_last_remaining_member() -> None:
    spec, interval = _scenario()
    static_release = interval["release_node_uuid"]
    taken_tail = "a2" if static_release == "b2" else "b2"
    scheduler = EdgeScheduler(dispatcher=RecordingDispatcher())
    submitted = scheduler.submit_workflow(spec)
    assert [item["node_id"] for item in submitted["dispatched"]] == ["pick"]

    scheduler.on_job_finished(submitted["dispatched"][0]["job_id"], success=True)
    run = scheduler._workflows[WORKFLOW_ID]
    assert run.node_state(static_release).value == "skipped"

    view = build_workflow_planning_view(run)
    [planned] = [
        item for item in view.continuous_intervals if item.interval_id == interval["interval_id"]
    ]
    assert planned.release_known is True
    assert planned.release_node_id == taken_tail
    assert "after" in {node.id for node in view.nodes}


def test_release_stays_unknown_while_the_taken_branch_is_not_visible() -> None:
    """剩余成员还藏在未决条件之后时，不能提前宣称释放点已知。"""

    spec, interval = _scenario()
    scheduler = EdgeScheduler(dispatcher=RecordingDispatcher())
    scheduler.submit_workflow(spec)
    view = build_workflow_planning_view(scheduler._workflows[WORKFLOW_ID])
    [planned] = [
        item for item in view.continuous_intervals if item.interval_id == interval["interval_id"]
    ]
    assert planned.release_known is False


def test_frozen_resource_plan_reaches_the_planner_continuous_intervals() -> None:
    node_ids = ["n1", "n2", "n3"]
    spec = _spec({
        "workflow_uuid": WORKFLOW_ID,
        "nodes": [{"uuid": node_id, "resource_defaults": ["robot"]} for node_id in node_ids],
        "edges": [
            {"source_node_uuid": source, "target_node_uuid": target}
            for source, target in zip(node_ids, node_ids[1:])
        ],
    })
    scheduler = EdgeScheduler(dispatcher=RecordingDispatcher())
    try:
        assert scheduler.submit_workflow(spec)["state"] == "running"
        view = build_workflow_planning_view(scheduler._workflows[WORKFLOW_ID])
        [interval] = view.continuous_intervals
        assert interval.resource_keys == ("/devices/robot",)
        assert interval.member_node_ids == tuple(node_ids)
        assert interval.release_known is True
    finally:
        scheduler.close()
