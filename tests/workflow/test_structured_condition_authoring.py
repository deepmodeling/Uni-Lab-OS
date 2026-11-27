"""结构化条件区域的 Python 创作公共合同测试。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from unilabos.app.scheduler.dispatch import RecordingDispatcher
from unilabos.app.scheduler.models import (
    NodeState,
    WorkflowEdge,
    WorkflowNode,
    WorkflowSpec,
)
from unilabos.app.scheduler.service import EdgeScheduler
from unilabos.workflow.authoring_engine import WorkflowAuthoringEngine
from unilabos.workflow.authoring_kernel import AuthoringCatalogSnapshot
from unilabos.workflow.execution_plan import (
    ExecutionPlanBuilder,
    ExecutionPlanBuildError,
)
from unilabos.workflow.workflow_spec_compiler import WorkflowSpecCompiler

from .test_authoring_engine import (
    ANALYZE_NODE_UUID,
    WORKFLOW_UUID,
    _compile,
    _engine,
    _handle,
    _template,
)

CONDITION_NODE_UUID = "20000000-0000-4000-8000-000000000021"
ELSE_NODE_UUID = "20000000-0000-4000-8000-000000000022"
CONDITION_TEMPLATE_UUID = "30000000-0000-4000-8000-000000000021"
RECORD_TEMPLATE_UUID = "30000000-0000-4000-8000-000000000022"
RECORD_LABEL_TARGET = "40000000-0000-4000-8000-000000000021"
RECORD_READY_TARGET = "40000000-0000-4000-8000-000000000022"
INSPECT_TEMPLATE_UUID = "30000000-0000-4000-8000-000000000023"
INSPECT_LABEL_TARGET = "40000000-0000-4000-8000-000000000023"
INSPECT_QUALIFIED_SOURCE = "40000000-0000-4000-8000-000000000024"
INSPECT_READY_SOURCE = "40000000-0000-4000-8000-000000000025"
MEASUREMENT_NODE_UUID = "20000000-0000-4000-8000-000000000023"


def _condition_template() -> dict[str, Any]:
    """返回隔离测试目录中的框架条件区域模板。"""

    return {
        "uuid": CONDITION_TEMPLATE_UUID,
        "resource_template_uuid": "31000000-0000-4000-8000-000000000001",
        "name": "condition",
        "display_name": "条件",
        "class": "unilabos.workflow.authoring:condition",
        "description": "由调度器求值并选择唯一分支的结构化控制区域。",
        "meta_data": {"unilab": {"framework_owner_only": True}},
        "goal": {},
        "goal_default": {},
        "feedback": {},
        "result": {},
        "schema": None,
        "type": "condition",
        "node_type": "condition",
        "icon": None,
        "header": None,
        "footer": None,
    }


def _condition_engine() -> WorkflowAuthoringEngine:
    """在既有动作目录上加入唯一条件区域模板。"""

    base_catalog = _engine()._catalog
    node_templates = [action.detached_template() for action in base_catalog.actions]
    handle_templates = [
        handle
        for action in base_catalog.actions
        for handle in action.detached_handles()
    ]
    record, record_handles = _template(
        RECORD_TEMPLATE_UUID,
        name="record",
        handles=[
            _handle(
                RECORD_LABEL_TARGET,
                node_template_uuid=RECORD_TEMPLATE_UUID,
                key="label",
                io_type="target",
                value_type="string",
                required=True,
            ),
            _handle(
                RECORD_READY_TARGET,
                node_template_uuid=RECORD_TEMPLATE_UUID,
                key="ready",
                io_type="target",
                value_type="any",
                data_source="dependency",
            ),
        ],
    )
    record["node_type"] = "ILab"
    record["schema"] = {
        "type": "object",
        "properties": {"label": {"type": "string"}},
        "required": ["label"],
    }
    record["meta_data"]["unilab"] = {
        "action_contract_schema": {
            "type": "object",
            "properties": {"goal": record["schema"]},
            "required": ["goal"],
        }
    }
    inspect, inspect_handles = _template(
        INSPECT_TEMPLATE_UUID,
        name="inspect",
        handles=[
            _handle(
                INSPECT_LABEL_TARGET,
                node_template_uuid=INSPECT_TEMPLATE_UUID,
                key="label",
                io_type="target",
                value_type="string",
                required=True,
            ),
            _handle(
                INSPECT_QUALIFIED_SOURCE,
                node_template_uuid=INSPECT_TEMPLATE_UUID,
                key="qualified",
                io_type="source",
                value_type="boolean",
            ),
            _handle(
                INSPECT_READY_SOURCE,
                node_template_uuid=INSPECT_TEMPLATE_UUID,
                key="ready",
                io_type="source",
                value_type="any",
                data_source="dependency",
            ),
        ],
    )
    inspect["node_type"] = "ILab"
    inspect["schema"] = {
        "type": "object",
        "properties": {"label": {"type": "string"}},
        "required": ["label"],
    }
    inspect["meta_data"]["unilab"] = {
        "action_contract_schema": {
            "type": "object",
            "properties": {"goal": inspect["schema"]},
            "required": ["goal"],
        }
    }
    return WorkflowAuthoringEngine(
        catalog=AuthoringCatalogSnapshot.from_entities(
            [*node_templates, record, inspect, _condition_template()],
            [*handle_templates, *record_handles, *inspect_handles],
        )
    )


def _condition_source() -> str:
    """返回以严格布尔工作流输入选择两个动作分支的作者源码。"""

    return f'''from lab.devices import Reactor
from unilabos.registry.placeholder_type import ResourceSlot
from unilabos.workflow.authoring import device, workflow, workflow_output


reactor: Reactor = device("60000000-0000-4000-8000-000000000001")


@workflow(workflow_uuid="{WORKFLOW_UUID}", displayname="Conditional analysis")
def conditional_analysis(*, sample: ResourceSlot, should_analyze: bool):
    # unilab:node_uuid={CONDITION_NODE_UUID}
    if should_analyze:
        # unilab:node_uuid={ANALYZE_NODE_UUID}
        selected = reactor.record(label="selected")
    else:
        # unilab:node_uuid={ELSE_NODE_UUID}
        fallback = reactor.record(label="fallback")
    return workflow_output()
'''


def test_python_if_compiles_to_structured_condition_region() -> None:
    """原生 if/else 应稳定编译为无可执行字符串的条件控制区域。"""

    engine = _condition_engine()

    compiled = _compile(engine, _condition_source())

    assert compiled.valid, compiled.diagnostics
    assert compiled.graph is not None
    nodes = {node["uuid"]: node for node in compiled.graph["nodes"]}
    region = nodes[CONDITION_NODE_UUID]
    assert region["type"] == "condition"
    assert region["param"] == {
        "bindings": {
            "should_analyze": {
                "kind": "workflow_input",
                "parameter": "should_analyze",
            }
        },
        "predecessor_node_uuids": [],
        "branches": [
            {
                "label": "if",
                "condition": {"var": "should_analyze"},
                "node_uuids": [ANALYZE_NODE_UUID],
                "entry_node_uuids": [ANALYZE_NODE_UUID],
                "exit_node_uuids": [ANALYZE_NODE_UUID],
            },
            {
                "label": "else",
                "condition": None,
                "node_uuids": [ELSE_NODE_UUID],
                "entry_node_uuids": [ELSE_NODE_UUID],
                "exit_node_uuids": [ELSE_NODE_UUID],
            },
        ],
    }
    assert nodes[ANALYZE_NODE_UUID]["parent_uuid"] == CONDITION_NODE_UUID
    assert nodes[ELSE_NODE_UUID]["parent_uuid"] == CONDITION_NODE_UUID
    assert compiled.normalized_python_source is not None
    assert "if should_analyze:" in compiled.normalized_python_source
    assert "eval(" not in compiled.normalized_python_source

    repeated = _compile(
        engine,
        compiled.normalized_python_source,
        graph=compiled.graph,
    )
    assert repeated.valid, repeated.diagnostics
    assert repeated.graph == compiled.graph


def test_condition_graph_builds_versioned_control_execution_plan() -> None:
    """冻结图应生成显式能力版本和调度器本地条件依赖。"""

    compiled = _compile(_condition_engine(), _condition_source())
    assert compiled.valid and compiled.graph is not None, compiled.diagnostics

    plan, jobs = ExecutionPlanBuilder().build(
        compiled.graph,
        run_mode="normal",
        target_node_uuid=None,
    )

    assert plan["version"] == 2
    assert plan["capabilities"] == [
        "condition_expression_v1",
        "control_regions_v1",
        "resource_intervals_v1",
        "static_resource_dag_v1",
    ]
    planned_nodes = {node["uuid"]: node for node in plan["nodes"]}
    assert planned_nodes[CONDITION_NODE_UUID]["kind"] == "condition"
    assert planned_nodes[CONDITION_NODE_UUID]["control_region"] == (
        planned_nodes[CONDITION_NODE_UUID]["param"]
    )
    control_edges = {
        (edge["source_node_uuid"], edge["target_node_uuid"])
        for edge in plan["edges"]
        if edge.get("dependency_only") is True
    }
    assert control_edges == {
        (CONDITION_NODE_UUID, ANALYZE_NODE_UUID),
        (CONDITION_NODE_UUID, ELSE_NODE_UUID),
    }
    assert {job["executor_kind"] for job in jobs} == {
        "condition",
        "device_action",
    }


def test_execution_plan_rejects_condition_members_outside_the_region() -> None:
    """画布数据不能借未选分支跳过区域外节点。"""

    compiled = _compile(_condition_engine(), _condition_source())
    assert compiled.valid and compiled.graph is not None, compiled.diagnostics
    graph = deepcopy(compiled.graph)
    condition = next(
        node for node in graph["nodes"] if node["uuid"] == CONDITION_NODE_UUID
    )
    condition["param"]["branches"][0]["node_uuids"].append(CONDITION_NODE_UUID)

    with pytest.raises(ExecutionPlanBuildError, match="不属于对应控制区域"):
        ExecutionPlanBuilder().build(
            graph,
            run_mode="normal",
            target_node_uuid=None,
        )


def test_execution_plan_rejects_disabled_condition_with_enabled_branches() -> None:
    """禁用控制器时不能把其启用分支降级为无保护的普通 DAG。"""

    compiled = _compile(_condition_engine(), _condition_source())
    assert compiled.valid and compiled.graph is not None, compiled.diagnostics
    graph = deepcopy(compiled.graph)
    condition = next(
        node for node in graph["nodes"] if node["uuid"] == CONDITION_NODE_UUID
    )
    condition["disabled"] = True

    with pytest.raises(ExecutionPlanBuildError, match="仍包含启用的分支"):
        ExecutionPlanBuilder().build(
            graph,
            run_mode="normal",
            target_node_uuid=None,
        )


def test_scheduler_selects_condition_branch_without_edge_dispatch() -> None:
    """调度器应本地完成条件作业，只向命中分支发送动作。"""

    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher)
    control_events: list[dict[str, object]] = []
    scheduler.add_local_control_listener(control_events.append)
    condition = WorkflowNode(
        id="condition",
        job_id="condition-job",
        executor_kind="condition",
        param={
            "variables": {"should_analyze": True},
            "branches": [
                {
                    "label": "if",
                    "condition": {"var": "should_analyze"},
                    "node_uuids": ["selected"],
                    "entry_node_uuids": ["selected"],
                    "exit_node_uuids": ["selected"],
                },
                {
                    "label": "else",
                    "condition": None,
                    "node_uuids": ["fallback"],
                    "entry_node_uuids": ["fallback"],
                    "exit_node_uuids": ["fallback"],
                },
            ],
        },
    )
    selected = WorkflowNode(
        id="selected",
        job_id="selected-job",
        device_id="analyzer-a",
        action_name="record",
        action_type="goal",
    )
    fallback = WorkflowNode(
        id="fallback",
        job_id="fallback-job",
        device_id="analyzer-b",
        action_name="record",
        action_type="goal",
    )
    spec = WorkflowSpec(
        workflow_id="conditional-workflow",
        nodes=[condition, selected, fallback],
        edges=[
            WorkflowEdge(
                uuid="condition-selected",
                source_node_id="condition",
                target_node_id="selected",
            ),
            WorkflowEdge(
                uuid="condition-fallback",
                source_node_id="condition",
                target_node_id="fallback",
            ),
        ],
    )

    submitted = scheduler.submit_workflow(spec)

    assert [item["node_id"] for item in submitted["dispatched"]] == ["selected"]
    assert [item["node_id"] for item in dispatcher.dispatched] == ["selected"]
    assert control_events == [
        {
            "workflow_id": "conditional-workflow",
            "job_id": "condition-job",
            "node_id": "condition",
            "selected_branch": "if",
            "skipped_node_ids": ["fallback"],
            "skipped_jobs": [{"node_id": "fallback", "job_id": "fallback-job"}],
        }
    ]
    snapshot = scheduler.workflow_snapshot("conditional-workflow")
    assert snapshot["nodes"]["condition"]["state"] == NodeState.SUCCESS.value
    assert snapshot["nodes"]["fallback"]["state"] == NodeState.SKIPPED.value

    finished = scheduler.on_job_finished("selected-job", True, {"ok": True})

    assert finished["workflow_state"] == "success"


def test_non_boolean_condition_fails_without_becoming_success() -> None:
    """严格布尔失败必须保持工作流 failed，并跳过所有未派发节点。"""

    scheduler = EdgeScheduler(dispatcher=RecordingDispatcher())
    condition = WorkflowNode(
        id="condition",
        executor_kind="condition",
        param={
            "variables": {"value": 1},
            "branches": [
                {
                    "label": "if",
                    "condition": {"var": "value"},
                    "node_uuids": ["action"],
                    "entry_node_uuids": ["action"],
                    "exit_node_uuids": ["action"],
                }
            ],
        },
    )
    action = WorkflowNode(
        id="action",
        device_id="reactor-a",
        action_name="record",
        action_type="goal",
    )

    submitted = scheduler.submit_workflow(
        WorkflowSpec(
            workflow_id="invalid-condition",
            nodes=[condition, action],
            edges=[
                WorkflowEdge(
                    uuid="condition-action",
                    source_node_id="condition",
                    target_node_id="action",
                )
            ],
        )
    )

    assert submitted["state"] == "failed"
    snapshot = scheduler.workflow_snapshot("invalid-condition")
    assert snapshot["nodes"]["condition"]["state"] == "failed"
    assert snapshot["nodes"]["action"]["state"] == "skipped"


def test_failed_local_projection_can_discard_and_retry_same_workflow() -> None:
    """本地投影异常不得把同一任务身份永久卡在调度器注册表中。"""

    scheduler = EdgeScheduler(dispatcher=RecordingDispatcher())
    condition = WorkflowNode(
        id="condition",
        executor_kind="condition",
        param={
            "variables": {"value": True},
            "branches": [
                {
                    "label": "if",
                    "condition": {"var": "value"},
                    "node_uuids": ["action"],
                    "entry_node_uuids": ["action"],
                    "exit_node_uuids": ["action"],
                }
            ],
        },
    )
    action = WorkflowNode(
        id="action",
        device_id="reactor-a",
        action_name="record",
        action_type="goal",
    )
    spec = WorkflowSpec(
        workflow_id="retry-condition",
        nodes=[condition, action],
        edges=[
            WorkflowEdge(
                uuid="condition-action",
                source_node_id="condition",
                target_node_id="action",
            )
        ],
    )

    def fail_projection(_event: dict[str, object]) -> None:
        raise RuntimeError("projection unavailable")

    scheduler.add_local_control_listener(fail_projection)
    with pytest.raises(RuntimeError, match="projection unavailable"):
        scheduler.submit_workflow(spec)
    scheduler.remove_local_control_listener(fail_projection)

    assert scheduler.discard_workflow("retry-condition") is True
    retried = scheduler.submit_workflow(spec)

    assert [item["node_id"] for item in retried["dispatched"]] == ["action"]


def test_condition_failure_remains_failed_after_parallel_job_finishes() -> None:
    """条件失败是不可逆终态，后到的并行成功不得覆盖它。"""

    scheduler = EdgeScheduler(dispatcher=RecordingDispatcher())
    producer = WorkflowNode(
        id="producer",
        job_id="producer-job",
        device_id="reactor-a",
        action_name="inspect",
        action_type="goal",
    )
    parallel = WorkflowNode(
        id="parallel",
        job_id="parallel-job",
        device_id="reactor-b",
        action_name="record",
        action_type="goal",
    )
    condition = WorkflowNode(
        id="condition",
        executor_kind="condition",
        param={
            "variables": {"invalid": 1},
            "branches": [
                {
                    "label": "if",
                    "condition": {"var": "invalid"},
                    "node_uuids": ["branch"],
                    "entry_node_uuids": ["branch"],
                    "exit_node_uuids": ["branch"],
                }
            ],
        },
    )
    branch = WorkflowNode(
        id="branch",
        device_id="reactor-c",
        action_name="record",
        action_type="goal",
    )
    scheduler.submit_workflow(
        WorkflowSpec(
            workflow_id="parallel-condition-failure",
            nodes=[producer, parallel, condition, branch],
            edges=[
                WorkflowEdge(
                    uuid="producer-condition",
                    source_node_id="producer",
                    target_node_id="condition",
                ),
                WorkflowEdge(
                    uuid="condition-branch",
                    source_node_id="condition",
                    target_node_id="branch",
                ),
            ],
        )
    )

    failed = scheduler.on_job_finished("producer-job", True, {"ok": True})
    settled = scheduler.on_job_finished("parallel-job", True, {"ok": True})

    assert failed["workflow_state"] == "failed"
    assert settled["workflow_state"] == "failed"


def test_compiled_condition_plan_runs_from_frozen_workflow_input() -> None:
    """Python 条件应经计划编译后读取冻结输入并只派发命中动作。"""

    authored = _compile(_condition_engine(), _condition_source())
    assert authored.valid and authored.graph is not None, authored.diagnostics
    plan, jobs = ExecutionPlanBuilder().build(
        authored.graph,
        run_mode="normal",
        target_node_uuid=None,
    )
    task_snapshot = {
        "uuid": "50000000-0000-4000-8000-000000000001",
        "workflow_snapshot": authored.graph,
        "execution_plan": plan,
        "input": {"sample": {"uuid": "sample-1"}, "should_analyze": False},
    }

    spec = WorkflowSpecCompiler().compile(task_snapshot, jobs)
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher)

    submitted = scheduler.submit_workflow(spec)

    assert [item["node_id"] for item in submitted["dispatched"]] == [ELSE_NODE_UUID]
    assert all(item["node_id"] != CONDITION_NODE_UUID for item in dispatcher.dispatched)


def test_condition_waits_for_and_reads_previous_node_result() -> None:
    """动作结果条件必须等生产者完成，再从返回值字段选择分支。"""

    source = _condition_source().replace(
        f"    # unilab:node_uuid={CONDITION_NODE_UUID}\n    if should_analyze:",
        f"""    # unilab:node_uuid={MEASUREMENT_NODE_UUID}
    measurement = reactor.inspect(label="sample")
    # unilab:node_uuid={CONDITION_NODE_UUID}
    if measurement.qualified:""",
    )
    authored = _compile(_condition_engine(), source)
    assert authored.valid and authored.graph is not None, authored.diagnostics
    plan, jobs = ExecutionPlanBuilder().build(
        authored.graph,
        run_mode="normal",
        target_node_uuid=None,
    )
    task_snapshot = {
        "uuid": "50000000-0000-4000-8000-000000000002",
        "workflow_snapshot": authored.graph,
        "execution_plan": plan,
        "input": {
            "sample": {"uuid": "sample-1"},
            "measurement": {"qualified": False},
            "should_analyze": False,
        },
    }
    spec = WorkflowSpecCompiler().compile(task_snapshot, jobs)
    condition_node = next(node for node in spec.nodes if node.id == CONDITION_NODE_UUID)
    condition_node.param["variables"]["measurement"] = {"qualified": False}
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher)

    submitted = scheduler.submit_workflow(spec)

    assert [item["node_id"] for item in submitted["dispatched"]] == [
        MEASUREMENT_NODE_UUID
    ]
    measurement_job = next(
        job["uuid"]
        for job in jobs
        if job["workflow_node_uuid"] == MEASUREMENT_NODE_UUID
    )

    advanced = scheduler.on_job_finished(
        measurement_job,
        True,
        {"qualified": True},
    )

    assert [item["node_id"] for item in advanced["dispatched"]] == [ANALYZE_NODE_UUID]
    snapshot = scheduler.workflow_snapshot(task_snapshot["uuid"])
    assert snapshot["nodes"][CONDITION_NODE_UUID]["selected_branch"] == "if"
    assert snapshot["nodes"][ELSE_NODE_UUID]["state"] == "skipped"
