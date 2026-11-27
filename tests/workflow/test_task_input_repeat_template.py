"""RepeatUntil 动态作业模板的工作流任务输入绑定回归。"""

from __future__ import annotations

from typing import Any

from unilabos.workflow.task_input import prepare_task_input


def test_plan_removes_virtual_parent_before_validating_repeat_input():
    from unilabos.workflow.execution_plan import ExecutionPlanBuilder
    graph = _graph()
    group_id = "72000000-0000-4000-8000-000000000199"
    template_id = "74000000-0000-4000-8000-000000000199"
    graph["node_templates"].append({"uuid": template_id, "node_type": "group", "type": "group", "name": "group"})
    graph["nodes"].append({"uuid": group_id, "workflow_node_template_uuid": template_id, "name": "Group", "type": "group", "param": {}, "meta_data": {}})
    graph["nodes"][0]["parent_uuid"] = group_id
    graph["nodes"][1]["material_uuid"] = "60000000-0000-4000-8000-000000000001"
    graph["nodes"][1]["meta_data"]["unilab"]["executor_binding"] = {"mode": "fixed", "device_id": "test-axis"}
    graph["node_templates"][1]["meta_data"] = {"unilab": {"action_contract_schema": {"type": "object", "properties": {"goal": {"type": "object", "properties": {"position": {"type": "integer"}}, "required": ["position"]}}, "required": ["goal"]}}}
    plan, jobs = ExecutionPlanBuilder().build(graph, run_mode="normal", target_node_uuid=None)
    nodes = {n["uuid"]: n for n in plan["nodes"]}
    assert group_id not in nodes and nodes[REPEAT_UUID]["parent_uuid"] is None
    assert nodes[CHILD_UUID]["parent_uuid"] == REPEAT_UUID
    prepare_task_input(graph=graph, execution_plan=plan, jobs=jobs, raw_input={"position": 3})


WORKFLOW_UUID = "71000000-0000-4000-8000-000000000101"
REPEAT_UUID = "72000000-0000-4000-8000-000000000101"
CHILD_UUID = "73000000-0000-4000-8000-000000000101"
CONTROL_TEMPLATE_UUID = "74000000-0000-4000-8000-000000000101"
CHILD_TEMPLATE_UUID = "75000000-0000-4000-8000-000000000101"
TARGET_TEMPLATE_HANDLE_UUID = "76000000-0000-4000-8000-000000000101"
TARGET_RUNTIME_HANDLE_UUID = "77000000-0000-4000-8000-000000000101"
CONDITION_UUID = "79000000-0000-4000-8000-000000000101"


def _graph() -> dict[str, Any]:
    """构造带工作流输入和循环体动作的最小冻结图。"""

    return {
        "workflow": {
            "uuid": WORKFLOW_UUID,
            "revision": 1,
            "name": "repeat input",
            "tags": [],
            "meta_data": {
                "unilab": {
                    "input_contract": {
                        "version": 1,
                        "parameters": [
                            {
                                "name": "position",
                                "schema": {"type": "integer"},
                                "required": True,
                            }
                        ],
                    },
                    "output_contract": {"version": 1, "outputs": []},
                    "output_bindings": {},
                }
            },
        },
        "nodes": [
            {
                "uuid": REPEAT_UUID,
                "workflow_node_template_uuid": CONTROL_TEMPLATE_UUID,
                "name": "重复直到",
                "type": "repeat_until",
                "pose": {},
                "param": {
                    "predecessor_node_uuids": [],
                    "successor_node_uuids": [],
                    "loop_variable": "round",
                    "max_iterations": 2,
                    "initial_carry": {"done": {"kind": "literal", "value": False}},
                    "next_carry": {"done": {"kind": "literal", "value": True}},
                    "until": {"lit": True},
                    "bindings": {},
                    "node_uuids": [CHILD_UUID],
                    "entry_node_uuids": [CHILD_UUID],
                    "exit_node_uuids": [CHILD_UUID],
                },
                "execution_policy": {},
                "disabled": False,
                "minimized": False,
                "meta_data": {"unilab": {"authoring_source_order": 0}},
            },
            {
                "uuid": CHILD_UUID,
                "workflow_node_template_uuid": CHILD_TEMPLATE_UUID,
                "parent_uuid": REPEAT_UUID,
                "name": "循环动作",
                "type": "device_action",
                "pose": {},
                "param": {},
                "execution_policy": {},
                "disabled": False,
                "minimized": False,
                "meta_data": {
                    "unilab": {
                        "input_bindings": {
                            TARGET_TEMPLATE_HANDLE_UUID: {"parameter": "position"}
                        }
                    }
                },
            },
        ],
        "edges": [],
        "node_templates": [
            {
                "uuid": CONTROL_TEMPLATE_UUID,
                "type": "repeat_until",
                "node_type": "repeat_until",
                "name": "repeat_until",
                "class": "unilabos.workflow.authoring:repeat_until",
            },
            {
                "uuid": CHILD_TEMPLATE_UUID,
                "type": "device_action",
                "node_type": "device_action",
                "name": "move",
                "class": "example.devices:Robot",
            },
        ],
        "handle_templates": [
            {
                "uuid": TARGET_TEMPLATE_HANDLE_UUID,
                "workflow_node_template_uuid": CHILD_TEMPLATE_UUID,
                "handle_key": "position",
                "io_type": "target",
                "display_name": "位置",
                "description": "动作位置",
                "type": "integer",
                "required": True,
                "data_source": "executor",
                "data_key": "position",
                "meta_data": {"unilab": {"value_schema": {"type": "integer"}}},
            }
        ],
    }


def _plan(*, child_parent_uuid: str) -> dict[str, Any]:
    """构造直接或隔着条件控制节点的循环体输入计划。"""

    nodes = [
        {
            "uuid": REPEAT_UUID,
            "kind": "repeat_until",
            "param": {},
            "execution_policy": {},
        }
    ]
    if child_parent_uuid == CONDITION_UUID:
        nodes.append(
            {
                "uuid": CONDITION_UUID,
                "parent_uuid": REPEAT_UUID,
                "kind": "condition",
                "param": {},
                "execution_policy": {},
            }
        )
    nodes.append(
        {
            "uuid": CHILD_UUID,
            "parent_uuid": child_parent_uuid,
            "kind": "device_action",
            "param": {},
            "execution_policy": {},
            "inputs": [
                {
                    "handle_uuid": TARGET_RUNTIME_HANDLE_UUID,
                    "data_key": "position",
                    "required": True,
                }
            ],
        }
    )
    return {
        "version": 2,
        "nodes": nodes,
        "handles": [
            {
                "uuid": TARGET_RUNTIME_HANDLE_UUID,
                "node_uuid": CHILD_UUID,
                "template_handle_uuid": TARGET_TEMPLATE_HANDLE_UUID,
                "handle_key": "position",
                "data_key": "position",
                "io_type": "target",
            }
        ],
        "edges": [],
    }


def _jobs() -> list[dict[str, Any]]:
    """仅创建循环控制 Job，循环体 Job 留待逐轮物化。"""

    return [
        {
            "uuid": "78000000-0000-4000-8000-000000000101",
            "workflow_node_uuid": REPEAT_UUID,
            "executor_kind": "repeat_until",
            "param": {},
        }
    ]


def test_repeat_body_input_is_frozen_without_a_first_round_job() -> None:
    """循环体输入应写入计划模板，而不是要求创建阶段已有动态 Job。"""

    prepared = prepare_task_input(
        graph=_graph(),
        raw_input={"position": 4},
        execution_plan=_plan(child_parent_uuid=REPEAT_UUID),
        jobs=_jobs(),
    )

    child = next(
        node for node in prepared.execution_plan["nodes"] if node["uuid"] == CHILD_UUID
    )
    assert child["param"] == {"position": 4}
    assert all(job["workflow_node_uuid"] != CHILD_UUID for job in prepared.jobs)


def test_repeat_condition_branch_input_is_frozen_without_a_first_round_job() -> None:
    """循环内条件分支动作即使隔着控制节点，也仍是动态作业模板。"""

    graph = _graph()
    graph["nodes"].insert(
        1,
        {
            "uuid": CONDITION_UUID,
            "parent_uuid": REPEAT_UUID,
            "name": "循环内条件",
            "type": "condition",
            "pose": {},
            "param": {},
            "execution_policy": {},
            "disabled": False,
            "minimized": False,
            "meta_data": {},
        },
    )
    graph["nodes"][2]["parent_uuid"] = CONDITION_UUID
    prepared = prepare_task_input(
        graph=graph,
        raw_input={"position": 4},
        execution_plan=_plan(child_parent_uuid=CONDITION_UUID),
        jobs=_jobs(),
    )

    child = next(
        node for node in prepared.execution_plan["nodes"] if node["uuid"] == CHILD_UUID
    )
    assert child["param"] == {"position": 4}
    assert all(job["workflow_node_uuid"] != CHILD_UUID for job in prepared.jobs)
