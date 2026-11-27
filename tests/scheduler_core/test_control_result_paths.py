"""组合子操作的控制输入须读取指定结果字段，且覆盖同名预填变量。"""

from types import SimpleNamespace

import pytest

from unilabos.app.scheduler.dag_state import WorkflowRun
from unilabos.app.scheduler.models import WorkflowNode, WorkflowSpec


@pytest.mark.parametrize("mounted, expected", [(1, "if"), (2, "else")])
def test_condition_resolves_bound_result_field(mounted, expected):
    condition = WorkflowNode(id="choose", executor_kind="condition", param={
        "variables": {"tool": 99},
        "bindings": {"tool": {"kind": "node_result", "node_uuid": "query", "result_path": ["mounted_tool"]}},
        "branches": [
            {"label": "if", "condition": {"binop": "==", "left": {"var": "tool"}, "right": {"lit": 1}},
             "node_uuids": ["yes"]},
            {"label": "else", "condition": None, "node_uuids": ["no"]},
        ],
    })
    run = WorkflowRun(WorkflowSpec(workflow_id="control-path", nodes=[
        WorkflowNode(id="query"), condition, WorkflowNode(id="yes"), WorkflowNode(id="no")
    ]))
    run.mark_finished("query", {"mounted_tool": mounted})
    evaluation = run.prepare_local_control()
    assert evaluation["selected_branch"] == expected, evaluation
    assert evaluation["skipped_node_ids"] == (["no"] if expected == "if" else ["yes"])


@pytest.mark.parametrize("valid", [True, False])
def test_repeat_until_reads_field_from_current_iteration(valid):
    repeat = WorkflowNode(id="repeat", executor_kind="repeat_until", param={
        "bindings": {"valid": {"kind": "node_result", "node_uuid": "capture", "result_path": ["valid"]}},
        "until": {"binop": "==", "left": {"var": "valid"}, "right": {"lit": True}},
        "next_carry": {}, "max_iterations": 3,
    })
    run = WorkflowRun(WorkflowSpec(workflow_id="repeat-path", nodes=[repeat]))
    current = WorkflowRun(WorkflowSpec(workflow_id="round", nodes=[WorkflowNode(id="capture-2")]))
    current.mark_finished("capture-2", {"valid": valid, "dx_mm": 0.25})
    runtime = SimpleNamespace(
        round_run=current, runtime_to_template={"capture-2": "capture"},
        iteration_index=1, carry={},
    )
    evaluation = run._prepare_repeat_evaluation("repeat", runtime)
    assert evaluation["condition_result"] is valid
    assert evaluation["iteration_index"] == 1
