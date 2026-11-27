"""RepeatUntil 循环区域的 Python 创作与执行计划公共合同测试。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from unilabos.app.scheduler.dispatch import RecordingDispatcher
from unilabos.app.scheduler.models import (
    Handle,
    RepeatUntilRegion,
    WorkflowEdge,
    WorkflowNode,
    WorkflowSpec,
)
from unilabos.app.scheduler.service import EdgeScheduler
from unilabos.workflow.authoring_engine import WorkflowAuthoringEngine
from unilabos.workflow.authoring_kernel import AuthoringCatalogSnapshot
from unilabos.workflow.execution_plan import ExecutionPlanBuilder
from unilabos.workflow.store import StoreConflict, WorkflowStore
from unilabos.workflow.task_input import TaskInputError, prepare_task_input
from unilabos.workflow.task_scheduler_bridge import TaskSchedulerBridge
from unilabos.workflow.workflow_spec_compiler import WorkflowSpecCompiler

from .test_authoring_engine import WORKFLOW_UUID, _compile, _engine, _handle, _template
from .test_qg01_group_parallel_authoring import _group_template
from .test_structured_condition_authoring import _condition_template

LOOP_NODE_UUID = "20000000-0000-4000-8000-000000000031"
MEASURE_NODE_UUID = "20000000-0000-4000-8000-000000000032"
ADJUST_NODE_UUID = "20000000-0000-4000-8000-000000000033"
FINAL_NODE_UUID = "20000000-0000-4000-8000-000000000034"
LOOP_TEMPLATE_UUID = "30000000-0000-4000-8000-000000000031"
MEASURE_TEMPLATE_UUID = "30000000-0000-4000-8000-000000000032"
ADJUST_TEMPLATE_UUID = "30000000-0000-4000-8000-000000000033"
FINAL_TEMPLATE_UUID = "30000000-0000-4000-8000-000000000034"


def _repeat_template() -> dict[str, Any]:
    """返回隔离测试目录中的框架 RepeatUntil 模板。"""

    return {
        "uuid": LOOP_TEMPLATE_UUID,
        "resource_template_uuid": "31000000-0000-4000-8000-000000000001",
        "name": "repeat_until",
        "display_name": "重复直到",
        "class": "unilabos.workflow.authoring:repeat_until",
        "description": "由调度器逐轮执行并在严格布尔条件满足时退出。",
        "meta_data": {"unilab": {"framework_owner_only": True}},
        "goal": {},
        "goal_default": {},
        "feedback": {},
        "result": {},
        "schema": None,
        "type": "repeat_until",
        "node_type": "repeat_until",
        "icon": None,
        "header": None,
        "footer": None,
    }


def _repeat_engine() -> WorkflowAuthoringEngine:
    """创建包含循环控制器和三个测试动作的创作编译器。"""

    base_catalog = _engine()._catalog
    node_templates = [action.detached_template() for action in base_catalog.actions]
    handle_templates = [
        handle
        for action in base_catalog.actions
        for handle in action.detached_handles()
    ]
    measure, measure_handles = _template(
        MEASURE_TEMPLATE_UUID,
        name="measure",
        handles=[
            _handle(
                "40000000-0000-4000-8000-000000000031",
                node_template_uuid=MEASURE_TEMPLATE_UUID,
                key="sample",
                io_type="target",
                value_type="ResourceSlot",
                required=True,
            ),
            _handle(
                "40000000-0000-4000-8000-000000000032",
                node_template_uuid=MEASURE_TEMPLATE_UUID,
                key="qualified",
                io_type="source",
                value_type="boolean",
            ),
            _handle(
                "40000000-0000-4000-8000-000000000033",
                node_template_uuid=MEASURE_TEMPLATE_UUID,
                key="ready",
                io_type="source",
                value_type="any",
                data_source="dependency",
            ),
        ],
    )
    adjust, adjust_handles = _template(
        ADJUST_TEMPLATE_UUID,
        name="adjust",
        handles=[
            _handle(
                "40000000-0000-4000-8000-000000000034",
                node_template_uuid=ADJUST_TEMPLATE_UUID,
                key="sample",
                io_type="target",
                value_type="ResourceSlot",
                required=True,
            ),
            _handle(
                "40000000-0000-4000-8000-000000000035",
                node_template_uuid=ADJUST_TEMPLATE_UUID,
                key="dose",
                io_type="target",
                value_type="integer",
                required=True,
            ),
            _handle(
                "40000000-0000-4000-8000-000000000036",
                node_template_uuid=ADJUST_TEMPLATE_UUID,
                key="next_dose",
                io_type="source",
                value_type="integer",
            ),
            _handle(
                "40000000-0000-4000-8000-000000000037",
                node_template_uuid=ADJUST_TEMPLATE_UUID,
                key="ready",
                io_type="target",
                value_type="any",
                data_source="dependency",
            ),
            _handle(
                "40000000-0000-4000-8000-000000000038",
                node_template_uuid=ADJUST_TEMPLATE_UUID,
                key="ready",
                io_type="source",
                value_type="any",
                data_source="dependency",
            ),
        ],
    )
    final, final_handles = _template(
        FINAL_TEMPLATE_UUID,
        name="finish",
        handles=[
            _handle(
                "40000000-0000-4000-8000-000000000039",
                node_template_uuid=FINAL_TEMPLATE_UUID,
                key="label",
                io_type="target",
                value_type="string",
                required=True,
            ),
            _handle(
                "40000000-0000-4000-8000-000000000040",
                node_template_uuid=FINAL_TEMPLATE_UUID,
                key="ready",
                io_type="target",
                value_type="any",
                data_source="dependency",
            ),
        ],
    )
    for template in (measure, adjust, final):
        template["node_type"] = "ILab"
        template["schema"] = {"type": "object", "properties": {}}
        template["meta_data"]["unilab"] = {
            "action_contract_schema": {
                "type": "object",
                "properties": {"goal": template["schema"]},
                "required": ["goal"],
            }
        }
    return WorkflowAuthoringEngine(
        catalog=AuthoringCatalogSnapshot.from_entities(
            [
                *node_templates,
                _group_template(),
                _condition_template(),
                _repeat_template(),
                measure,
                adjust,
                final,
            ],
            [
                *handle_templates,
                *measure_handles,
                *adjust_handles,
                *final_handles,
            ],
        )
    )


def _repeat_source() -> str:
    """返回包含显式 carry/next 和终止条件的规范循环源码。"""

    return f'''from lab.devices import Reactor
from unilabos.registry.placeholder_type import ResourceSlot
from unilabos.workflow.authoring import device, repeat_until, until, workflow, workflow_output


reactor: Reactor = device("60000000-0000-4000-8000-000000000001")


@workflow(workflow_uuid="{WORKFLOW_UUID}", displayname="Repeat analysis")
def repeat_analysis(*, sample: ResourceSlot, initial_dose: int):
    # unilab:node_uuid={LOOP_NODE_UUID}
    with repeat_until(max_iterations=20, carry={{"dose": initial_dose}}) as loop:
        # unilab:node_uuid={MEASURE_NODE_UUID}
        measurement = reactor.measure(sample=sample)
        # unilab:node_uuid={ADJUST_NODE_UUID}
        adjusted = reactor.adjust(sample=sample, dose=loop.carry["dose"])
        loop.next(dose=adjusted.next_dose)
        until(measurement.qualified)
    # unilab:node_uuid={FINAL_NODE_UUID}
    final = reactor.finish(label="done")
    return workflow_output()
'''


def test_repeat_until_compiles_to_structured_region_and_roundtrips() -> None:
    """循环源码应冻结模板、carry/next 和无可执行字符串的退出表达式。"""

    engine = _repeat_engine()
    compiled = _compile(engine, _repeat_source())

    assert compiled.valid, compiled.diagnostics
    assert compiled.graph is not None
    nodes = {node["uuid"]: node for node in compiled.graph["nodes"]}
    region = nodes[LOOP_NODE_UUID]
    assert region["type"] == "repeat_until"
    assert region["param"] == {
        "predecessor_node_uuids": [],
        "successor_node_uuids": [FINAL_NODE_UUID],
        "loop_variable": "loop",
        "max_iterations": 20,
        "initial_carry": {
            "dose": {"kind": "workflow_input", "parameter": "initial_dose"}
        },
        "next_carry": {
            "dose": {
                "kind": "node_result",
                "node_uuid": ADJUST_NODE_UUID,
                "result_path": ["next_dose"],
            }
        },
        "until": {"field": {"var": "measurement"}, "name": "qualified"},
        "bindings": {
            "measurement": {
                "kind": "node_result",
                "node_uuid": MEASURE_NODE_UUID,
            }
        },
        "node_uuids": [MEASURE_NODE_UUID, ADJUST_NODE_UUID],
        "entry_node_uuids": [MEASURE_NODE_UUID],
        "exit_node_uuids": [ADJUST_NODE_UUID],
    }
    assert nodes[MEASURE_NODE_UUID]["parent_uuid"] == LOOP_NODE_UUID
    assert nodes[ADJUST_NODE_UUID]["parent_uuid"] == LOOP_NODE_UUID
    adjust_unilab = nodes[ADJUST_NODE_UUID]["meta_data"]["unilab"]
    assert adjust_unilab["carry_bindings"] == {
        "40000000-0000-4000-8000-000000000035": {
            "control_region_uuid": LOOP_NODE_UUID,
            "key": "dose",
        }
    }
    assert compiled.normalized_python_source is not None
    assert "with repeat_until(" in compiled.normalized_python_source
    assert 'loop.carry["dose"]' in compiled.normalized_python_source
    assert "loop.next(dose=adjusted.next_dose)" in compiled.normalized_python_source
    assert "until(measurement.qualified)" in compiled.normalized_python_source
    assert "eval(" not in compiled.normalized_python_source

    repeated = _compile(
        engine,
        compiled.normalized_python_source,
        graph=compiled.graph,
    )
    assert repeated.valid, repeated.diagnostics
    assert repeated.graph == compiled.graph


def test_nested_repeat_roundtrips_and_compiles_as_recursive_lazy_templates() -> None:
    """嵌套循环应保留在父轮次模板中，而不是提前创建内层作业。"""

    outer_uuid = "20000000-0000-4000-8000-000000000051"
    outer_measure_uuid = "20000000-0000-4000-8000-000000000052"
    inner_uuid = "20000000-0000-4000-8000-000000000053"
    inner_measure_uuid = "20000000-0000-4000-8000-000000000054"
    inner_adjust_uuid = "20000000-0000-4000-8000-000000000055"
    outer_adjust_uuid = "20000000-0000-4000-8000-000000000056"
    final_uuid = "20000000-0000-4000-8000-000000000057"
    source = f'''from lab.devices import Reactor
from unilabos.registry.placeholder_type import ResourceSlot
from unilabos.workflow.authoring import device, repeat_until, until, workflow, workflow_output


reactor: Reactor = device("60000000-0000-4000-8000-000000000001")


@workflow(workflow_uuid="{WORKFLOW_UUID}", displayname="Nested repeat")
def nested_repeat(
    *,
    outer_sample: ResourceSlot,
    inner_sample: ResourceSlot,
    adjust_sample: ResourceSlot,
    outer_adjust_sample: ResourceSlot,
    initial_dose: int,
):
    # unilab:node_uuid={outer_uuid}
    with repeat_until(max_iterations=3, carry={{"dose": initial_dose}}) as outer:
        # unilab:node_uuid={outer_measure_uuid}
        outer_measure = reactor.measure(sample=outer_sample)
        # unilab:node_uuid={inner_uuid}
        with repeat_until(max_iterations=2, carry={{"dose": outer.carry["dose"]}}) as inner:
            # unilab:node_uuid={inner_measure_uuid}
            inner_measure = reactor.measure(sample=inner_sample)
            # unilab:node_uuid={inner_adjust_uuid}
            inner_adjust = reactor.adjust(sample=adjust_sample, dose=inner.carry["dose"])
            inner.next(dose=inner_adjust.next_dose)
            until(inner_measure.qualified)
        # unilab:node_uuid={outer_adjust_uuid}
        outer_adjust = reactor.adjust(sample=outer_adjust_sample, dose=outer.carry["dose"])
        outer.next(dose=outer_adjust.next_dose)
        until(outer_measure.qualified)
    # unilab:node_uuid={final_uuid}
    final = reactor.finish(label="done")
    return workflow_output()
'''
    compiled = _compile(_repeat_engine(), source)
    assert compiled.valid and compiled.graph is not None, compiled.diagnostics
    repeated = _compile(
        _repeat_engine(),
        compiled.normalized_python_source or "",
        graph=compiled.graph,
    )
    assert repeated.valid and repeated.graph == compiled.graph, repeated.diagnostics

    plan, jobs = ExecutionPlanBuilder().build(
        compiled.graph,
        run_mode="normal",
        target_node_uuid=None,
    )
    spec = WorkflowSpecCompiler().compile(
        {
            "uuid": "10000000-0000-4000-8000-000000000095",
            "execution_plan": plan,
            "input": {
                "outer_sample": {"uuid": "70000000-0000-4000-8000-000000000001"},
                "inner_sample": {"uuid": "70000000-0000-4000-8000-000000000002"},
                "adjust_sample": {"uuid": "70000000-0000-4000-8000-000000000003"},
                "outer_adjust_sample": {"uuid": "70000000-0000-4000-8000-000000000004"},
                "initial_dose": 1,
            },
        },
        jobs,
    )
    assert set(spec.repeat_regions) == {outer_uuid}
    outer_region = spec.repeat_regions[outer_uuid]
    assert set(outer_region.repeat_regions) == {inner_uuid}
    assert inner_uuid in {node.id for node in outer_region.nodes}
    assert inner_measure_uuid not in {node.id for node in outer_region.nodes}


def test_repeat_body_can_roundtrip_parallel_dag_branches() -> None:
    """循环体允许用 parallel/group 表达可并行的轮内 DAG。"""

    source = f'''from lab.devices import Reactor
from unilabos.registry.placeholder_type import ResourceSlot
from unilabos.workflow.authoring import device, group, parallel, repeat_until, until, workflow, workflow_output

reactor: Reactor = device("60000000-0000-4000-8000-000000000001")

@workflow(workflow_uuid="{WORKFLOW_UUID}", displayname="Parallel repeat")
def parallel_repeat(*, left_sample: ResourceSlot, right_sample: ResourceSlot):
    # unilab:node_uuid=20000000-0000-4000-8000-000000000071
    with repeat_until(max_iterations=2, carry={{}}) as loop:
        with parallel():
            # unilab:node_uuid=20000000-0000-4000-8000-000000000072
            with group(name="left"):
                # unilab:node_uuid=20000000-0000-4000-8000-000000000073
                left = reactor.measure(sample=left_sample)
            # unilab:node_uuid=20000000-0000-4000-8000-000000000074
            with group(name="right"):
                # unilab:node_uuid=20000000-0000-4000-8000-000000000075
                right = reactor.measure(sample=right_sample)
        loop.next()
        until(left.qualified and right.qualified)
    return workflow_output()
'''
    compiled = _compile(_repeat_engine(), source)
    assert compiled.valid and compiled.graph is not None, compiled.diagnostics
    assert "        with parallel():" in (compiled.normalized_python_source or "")
    repeated = _compile(
        _repeat_engine(),
        compiled.normalized_python_source or "",
        graph=compiled.graph,
    )
    assert repeated.valid and repeated.graph == compiled.graph, repeated.diagnostics


def test_repeat_plan_freezes_body_but_creates_no_body_jobs_eagerly() -> None:
    """首批作业只包含控制器和区域外动作，循环体留待每轮幂等物化。"""

    compiled = _compile(_repeat_engine(), _repeat_source())
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
        "dynamic_iteration_jobs_v1",
        "resource_intervals_v1",
        "static_resource_dag_v1",
    ]
    assert {node["uuid"] for node in plan["nodes"]} == {
        LOOP_NODE_UUID,
        MEASURE_NODE_UUID,
        ADJUST_NODE_UUID,
        FINAL_NODE_UUID,
    }
    assert {job["workflow_node_uuid"] for job in jobs} == {
        LOOP_NODE_UUID,
        FINAL_NODE_UUID,
    }
    control_edges = {
        (edge["source_node_uuid"], edge["target_node_uuid"])
        for edge in plan["edges"]
        if edge.get("dependency_only") is True
    }
    assert (LOOP_NODE_UUID, MEASURE_NODE_UUID) in control_edges
    assert (LOOP_NODE_UUID, FINAL_NODE_UUID) in control_edges


def test_task_input_binds_repeat_templates_without_eager_jobs() -> None:
    """任务输入冻结到循环模板，但不得提前创建任何循环体 Job。"""

    compiled = _compile(_repeat_engine(), _repeat_source())
    assert compiled.valid and compiled.graph is not None, compiled.diagnostics
    plan, jobs = ExecutionPlanBuilder().build(
        compiled.graph,
        run_mode="normal",
        target_node_uuid=None,
    )
    material_uuid = "70000000-0000-4000-8000-000000000031"
    template_uuid = "71000000-0000-4000-8000-000000000031"

    prepared = prepare_task_input(
        graph=compiled.graph,
        raw_input={
            "sample": {"uuid": material_uuid},
            "initial_dose": 1,
        },
        execution_plan=plan,
        jobs=jobs,
        resource_resolver=lambda resolved_uuid: {
            "uuid": resolved_uuid,
            "resource_template_uuid": template_uuid,
        },
    )

    initial_job_nodes = {job["workflow_node_uuid"] for job in prepared.jobs}
    assert {LOOP_NODE_UUID, FINAL_NODE_UUID} <= initial_job_nodes
    assert {MEASURE_NODE_UUID, ADJUST_NODE_UUID}.isdisjoint(initial_job_nodes)
    nodes = {node["uuid"]: node for node in prepared.execution_plan["nodes"]}
    assert nodes[MEASURE_NODE_UUID]["param"]["sample"] == {"uuid": material_uuid}
    assert nodes[ADJUST_NODE_UUID]["param"]["sample"] == {"uuid": material_uuid}

    spec = WorkflowSpecCompiler().compile(
        {
            "uuid": "10000000-0000-4000-8000-000000000099",
            "execution_plan": prepared.execution_plan,
            "input": prepared.resolved_input,
            "run_mode": "normal",
        },
        prepared.jobs,
    )
    region = spec.repeat_regions[LOOP_NODE_UUID]
    repeat_nodes = {node.id: node for node in region.nodes}
    assert repeat_nodes[MEASURE_NODE_UUID].param["sample"] == {"uuid": material_uuid}
    assert repeat_nodes[ADJUST_NODE_UUID].param["sample"] == {"uuid": material_uuid}

    with pytest.raises(TaskInputError, match="计划连接点未归属唯一活动作业"):
        prepare_task_input(
            graph=compiled.graph,
            raw_input={
                "sample": {"uuid": material_uuid},
                "initial_dose": 1,
            },
            execution_plan=plan,
            jobs=[job for job in jobs if job["workflow_node_uuid"] != FINAL_NODE_UUID],
            resource_resolver=lambda resolved_uuid: {
                "uuid": resolved_uuid,
                "resource_template_uuid": template_uuid,
            },
        )


def test_task_input_freezes_repeat_template_site_selection() -> None:
    """循环体库位候选冻结到模板策略，并由后续轮次 Job 继承。"""

    compiled = _compile(_repeat_engine(), _repeat_source())
    assert compiled.valid and compiled.graph is not None, compiled.diagnostics
    plan, jobs = ExecutionPlanBuilder().build(
        compiled.graph,
        run_mode="normal",
        target_node_uuid=None,
    )
    material_uuid = "70000000-0000-4000-8000-000000000032"
    template_uuid = "71000000-0000-4000-8000-000000000032"
    site_uuid = "72000000-0000-4000-8000-000000000032"
    measure = next(node for node in plan["nodes"] if node["uuid"] == MEASURE_NODE_UUID)
    measure["site_selectors"] = [
        {
            "handle_uuid": "73000000-0000-4000-8000-000000000032",
            "parameter": "target_site",
            "owner_parameter": "sample",
            "group_key": "measurement_sites",
        }
    ]

    prepared = prepare_task_input(
        graph=compiled.graph,
        raw_input={
            "sample": {"uuid": material_uuid},
            "initial_dose": 1,
        },
        execution_plan=plan,
        jobs=jobs,
        resource_resolver=lambda resolved_uuid: {
            "uuid": resolved_uuid,
            "resource_template_uuid": template_uuid,
        },
        site_selection_resolver=lambda request: {
            "site_uuids": [site_uuid],
            "fingerprint": f"{request['owner_material_uuid']}:measurement_sites",
        },
    )

    prepared_measure = next(
        node for node in prepared.execution_plan["nodes"] if node["uuid"] == MEASURE_NODE_UUID
    )
    assert prepared_measure["execution_policy"]["target_site_group"] == [site_uuid]
    spec = WorkflowSpecCompiler().compile(
        {
            "uuid": "10000000-0000-4000-8000-000000000100",
            "execution_plan": prepared.execution_plan,
            "input": prepared.resolved_input,
            "run_mode": "normal",
        },
        prepared.jobs,
    )
    repeat_nodes = {node.id: node for node in spec.repeat_regions[LOOP_NODE_UUID].nodes}
    assert repeat_nodes[MEASURE_NODE_UUID].execution_policy["target_site_group"] == [site_uuid]


def test_scheduler_materializes_distinct_jobs_until_strict_condition_is_true() -> None:
    """每轮作业身份独立、carry 提交后才启动下一轮，满足条件后退出。"""

    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher)
    control_events: list[dict[str, Any]] = []
    scheduler.add_local_control_listener(control_events.append)
    repeat = WorkflowNode(
        id=LOOP_NODE_UUID,
        job_id="50000000-0000-4000-8000-000000000031",
        executor_kind="repeat_until",
        param={
            "max_iterations": 3,
            "initial_carry": {"dose": {"kind": "literal", "value": 1}},
            "next_carry": {
                "dose": {
                    "kind": "node_result",
                    "node_uuid": ADJUST_NODE_UUID,
                    "result_path": ["next_dose"],
                }
            },
            "until": {"field": {"var": "measurement"}, "name": "qualified"},
            "bindings": {
                "measurement": {
                    "kind": "node_result",
                    "node_uuid": MEASURE_NODE_UUID,
                }
            },
        },
    )
    measure = WorkflowNode(
        id=MEASURE_NODE_UUID,
        device_id="analyzer",
        action_name="measure",
        action_type="goal",
    )
    adjust = WorkflowNode(
        id=ADJUST_NODE_UUID,
        device_id="reactor",
        action_name="adjust",
        action_type="goal",
        carry_bindings={
            "40000000-0000-4000-8000-000000000035": {
                "control_region_uuid": LOOP_NODE_UUID,
                "key": "dose",
            }
        },
    )
    final = WorkflowNode(
        id=FINAL_NODE_UUID,
        job_id="50000000-0000-4000-8000-000000000034",
        device_id="output",
        action_name="finish",
        action_type="goal",
    )
    region = RepeatUntilRegion(
        control_node_id=LOOP_NODE_UUID,
        nodes=[measure, adjust],
        handles=[
            Handle(
                uuid="40000000-0000-4000-8000-000000000035",
                node_id=ADJUST_NODE_UUID,
                io_type="target",
                handle_key="dose",
                data_key="dose",
            )
        ],
    )
    spec = WorkflowSpec(
        workflow_id="10000000-0000-4000-8000-000000000099",
        nodes=[repeat, final],
        edges=[
            WorkflowEdge(
                uuid="60000000-0000-4000-8000-000000000031",
                source_node_id=LOOP_NODE_UUID,
                target_node_id=FINAL_NODE_UUID,
            )
        ],
        repeat_regions={LOOP_NODE_UUID: region},
    )

    first = scheduler.submit_workflow(spec)
    assert len(first["dispatched"]) == 2
    first_jobs = {item["action"]: item for item in dispatcher.dispatched[-2:]}
    assert set(first_jobs) == {"measure", "adjust"}
    assert first_jobs["adjust"]["action_args"]["dose"] == 1
    scheduler.on_job_finished(
        first_jobs["measure"]["job_id"], True, {"qualified": False}
    )
    second_dispatch = scheduler.on_job_finished(
        first_jobs["adjust"]["job_id"], True, {"next_dose": 2}
    )["dispatched"]
    assert len(second_dispatch) == 2
    second_jobs = {item["action"]: item for item in dispatcher.dispatched[-2:]}
    assert set(second_jobs) == {"measure", "adjust"}
    assert second_jobs["adjust"]["action_args"]["dose"] == 2
    assert second_jobs["measure"]["job_id"] != first_jobs["measure"]["job_id"]
    scheduler.on_job_finished(
        second_jobs["measure"]["job_id"], True, {"qualified": True}
    )
    completed = scheduler.on_job_finished(
        second_jobs["adjust"]["job_id"], True, {"next_dose": 3}
    )
    assert [item["node_id"] for item in completed["dispatched"]] == [FINAL_NODE_UUID]
    assert [event["phase"] for event in control_events] == [
        "materialize",
        "evaluate",
        "materialize",
        "evaluate",
    ]


def test_scheduler_executes_nested_repeat_regions_with_runtime_scoped_ids() -> None:
    """嵌套循环应随父轮次实例化，且内外控制节点分别结算。"""

    inner_control_uuid = "20000000-0000-4000-8000-000000000041"
    inner_action_uuid = "20000000-0000-4000-8000-000000000042"
    outer_action_uuid = "20000000-0000-4000-8000-000000000043"
    final_uuid = "20000000-0000-4000-8000-000000000044"
    inner = WorkflowNode(
        id=inner_control_uuid,
        executor_kind="repeat_until",
        param={
            "max_iterations": 2,
            "initial_carry": {
                "dose": {
                    "kind": "loop_carry",
                    "control_region_uuid": LOOP_NODE_UUID,
                    "key": "dose",
                }
            },
            "next_carry": {
                "dose": {"kind": "loop_carry", "key": "dose"}
            },
            "until": {"field": {"var": "inner_result"}, "name": "done"},
            "bindings": {
                "inner_result": {
                    "kind": "node_result",
                    "node_uuid": inner_action_uuid,
                }
            },
        },
    )
    inner_action = WorkflowNode(
        id=inner_action_uuid,
        device_id="inner-device",
        action_name="inner_action",
        action_type="goal",
        carry_bindings={
            "40000000-0000-4000-8000-000000000041": {
                "control_region_uuid": inner_control_uuid,
                "key": "dose",
            }
        },
    )
    outer_action = WorkflowNode(
        id=outer_action_uuid,
        device_id="outer-device",
        action_name="outer_action",
        action_type="goal",
    )
    outer = WorkflowNode(
        id=LOOP_NODE_UUID,
        job_id="50000000-0000-4000-8000-000000000041",
        executor_kind="repeat_until",
        param={
            "max_iterations": 2,
            "initial_carry": {"dose": {"kind": "literal", "value": 7}},
            "next_carry": {
                "dose": {"kind": "loop_carry", "key": "dose"}
            },
            "until": {"field": {"var": "outer_result"}, "name": "done"},
            "bindings": {
                "outer_result": {
                    "kind": "node_result",
                    "node_uuid": outer_action_uuid,
                }
            },
        },
    )
    inner_region = RepeatUntilRegion(
        control_node_id=inner_control_uuid,
        nodes=[inner_action],
        handles=[
            Handle(
                uuid="40000000-0000-4000-8000-000000000041",
                node_id=inner_action_uuid,
                io_type="target",
                handle_key="dose",
                data_key="dose",
            )
        ],
    )
    outer_region = RepeatUntilRegion(
        control_node_id=LOOP_NODE_UUID,
        nodes=[inner, outer_action],
        edges=[
            WorkflowEdge(
                uuid="60000000-0000-4000-8000-000000000041",
                source_node_id=inner_control_uuid,
                target_node_id=outer_action_uuid,
            )
        ],
        repeat_regions={inner_control_uuid: inner_region},
    )
    final = WorkflowNode(
        id=final_uuid,
        job_id="50000000-0000-4000-8000-000000000044",
        device_id="final-device",
        action_name="finish_nested",
        action_type="goal",
    )
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher)
    scheduler.submit_workflow(
        WorkflowSpec(
            workflow_id="10000000-0000-4000-8000-000000000096",
            nodes=[outer, final],
            edges=[
                WorkflowEdge(
                    uuid="60000000-0000-4000-8000-000000000042",
                    source_node_id=LOOP_NODE_UUID,
                    target_node_id=final_uuid,
                )
            ],
            repeat_regions={LOOP_NODE_UUID: outer_region},
        )
    )

    inner_job = next(
        item for item in dispatcher.dispatched if item["action"] == "inner_action"
    )
    assert inner_job["action_args"]["dose"] == 7
    scheduler.on_job_finished(inner_job["job_id"], True, {"done": True})
    outer_job = next(
        item for item in dispatcher.dispatched if item["action"] == "outer_action"
    )
    completed = scheduler.on_job_finished(
        outer_job["job_id"], True, {"done": True}
    )

    assert [item["node_id"] for item in completed["dispatched"]] == [final_uuid]
    assert inner_job["job_id"] != inner_control_uuid


def test_repeat_fails_immediately_on_limit_or_non_boolean_condition() -> None:
    """轮次上限和非布尔退出值都必须在本地控制阶段立即失败。"""

    for result, expected_code in (
        ({"done": False}, "loop_iteration_limit_exceeded"),
        ({"done": 1}, "condition_evaluation_failed"),
    ):
        action_uuid = "20000000-0000-4000-8000-000000000061"
        repeat = WorkflowNode(
            id=LOOP_NODE_UUID,
            job_id="50000000-0000-4000-8000-000000000061",
            executor_kind="repeat_until",
            param={
                "max_iterations": 1,
                "initial_carry": {},
                "next_carry": {},
                "until": {"field": {"var": "result"}, "name": "done"},
                "bindings": {
                    "result": {"kind": "node_result", "node_uuid": action_uuid}
                },
            },
        )
        action = WorkflowNode(
            id=action_uuid,
            device_id="test-device",
            action_name="check",
            action_type="goal",
        )
        dispatcher = RecordingDispatcher()
        scheduler = EdgeScheduler(dispatcher=dispatcher)
        events: list[dict[str, Any]] = []
        scheduler.add_local_control_listener(events.append)
        scheduler.submit_workflow(
            WorkflowSpec(
                workflow_id=(
                    "10000000-0000-4000-8000-000000000093"
                    if expected_code == "loop_iteration_limit_exceeded"
                    else "10000000-0000-4000-8000-000000000094"
                ),
                nodes=[repeat],
                repeat_regions={
                    LOOP_NODE_UUID: RepeatUntilRegion(
                        control_node_id=LOOP_NODE_UUID,
                        nodes=[action],
                    )
                },
            )
        )
        job = dispatcher.dispatched[-1]
        scheduler.on_job_finished(job["job_id"], True, result)

        assert events[-1]["error"] == expected_code
        assert events[-1]["phase"] == "evaluate"
        assert scheduler.workflow_snapshot(repeat.job_id) is None
        assert scheduler.workflow_snapshot(
            "10000000-0000-4000-8000-000000000093"
            if expected_code == "loop_iteration_limit_exceeded"
            else "10000000-0000-4000-8000-000000000094"
        )["state"] == "failed"


def test_materialization_projection_failure_cannot_dispatch_ghost_jobs() -> None:
    """持久投影拒绝物化时，权威失败决定必须回传给内存 DAG。"""

    action_uuid = "20000000-0000-4000-8000-000000000081"
    repeat = WorkflowNode(
        id=LOOP_NODE_UUID,
        job_id="50000000-0000-4000-8000-000000000081",
        executor_kind="repeat_until",
        param={
            "max_iterations": 1,
            "initial_carry": {},
            "next_carry": {},
            "until": {"lit": True},
            "bindings": {},
        },
    )
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher)

    def reject_materialization(event: dict[str, Any]) -> dict[str, Any] | None:
        if event.get("phase") != "materialize":
            return None
        event["phase"] = "evaluate"
        event["condition_result"] = None
        event["next_carry"] = None
        event["error"] = "repeat_iteration_materialization_failed"
        event.pop("iteration_jobs", None)
        return event

    scheduler.add_local_control_listener(reject_materialization)
    result = scheduler.submit_workflow(
        WorkflowSpec(
            workflow_id="10000000-0000-4000-8000-000000000092",
            nodes=[repeat],
            repeat_regions={
                LOOP_NODE_UUID: RepeatUntilRegion(
                    control_node_id=LOOP_NODE_UUID,
                    nodes=[
                        WorkflowNode(
                            id=action_uuid,
                            device_id="ghost-device",
                            action_name="must_not_run",
                            action_type="goal",
                        )
                    ],
                )
            },
        )
    )

    assert result["state"] == "failed"
    assert dispatcher.dispatched == []


def test_unselected_condition_branch_skips_repeat_without_materializing_body() -> None:
    """条件未选中嵌套循环时，只跳过控制节点，不访问惰性 body 模板。"""

    condition_uuid = "20000000-0000-4000-8000-000000000082"
    body_uuid = "20000000-0000-4000-8000-000000000083"
    selected_uuid = "20000000-0000-4000-8000-000000000084"
    condition = WorkflowNode(
        id=condition_uuid,
        job_id="50000000-0000-4000-8000-000000000082",
        executor_kind="condition",
        param={
            "variables": {},
            "bindings": {},
            "branches": [
                {
                    "label": "if",
                    "condition": {"lit": False},
                    "node_uuids": [LOOP_NODE_UUID, body_uuid],
                },
                {
                    "label": "else",
                    "condition": None,
                    "node_uuids": [selected_uuid],
                },
            ],
        },
    )
    repeat = WorkflowNode(
        id=LOOP_NODE_UUID,
        job_id="50000000-0000-4000-8000-000000000083",
        executor_kind="repeat_until",
        param={
            "max_iterations": 1,
            "initial_carry": {},
            "next_carry": {},
            "until": {"lit": True},
            "bindings": {},
        },
    )
    selected = WorkflowNode(
        id=selected_uuid,
        job_id="50000000-0000-4000-8000-000000000084",
        device_id="selected-device",
        action_name="selected_action",
        action_type="goal",
    )
    dispatcher = RecordingDispatcher()
    result = EdgeScheduler(dispatcher=dispatcher).submit_workflow(
        WorkflowSpec(
            workflow_id="10000000-0000-4000-8000-000000000091",
            nodes=[condition, repeat, selected],
            edges=[
                WorkflowEdge(
                    uuid="60000000-0000-4000-8000-000000000081",
                    source_node_id=condition_uuid,
                    target_node_id=LOOP_NODE_UUID,
                ),
                WorkflowEdge(
                    uuid="60000000-0000-4000-8000-000000000082",
                    source_node_id=condition_uuid,
                    target_node_id=selected_uuid,
                ),
            ],
            repeat_regions={
                LOOP_NODE_UUID: RepeatUntilRegion(
                    control_node_id=LOOP_NODE_UUID,
                    nodes=[
                        WorkflowNode(
                            id=body_uuid,
                            device_id="body-device",
                            action_name="must_not_materialize",
                            action_type="goal",
                        )
                    ],
                )
            },
        )
    )

    assert [item["node_id"] for item in result["dispatched"]] == [selected_uuid]
    assert all(item["action"] != "must_not_materialize" for item in dispatcher.dispatched)


def test_compiled_repeat_plan_separates_lazy_body_templates() -> None:
    """计划编译器应只激活首批作业，并把循环体保留为轮次模板。"""

    authored = _compile(_repeat_engine(), _repeat_source())
    assert authored.valid and authored.graph is not None, authored.diagnostics
    plan, jobs = ExecutionPlanBuilder().build(
        authored.graph,
        run_mode="normal",
        target_node_uuid=None,
    )

    spec = WorkflowSpecCompiler().compile(
        {
            "uuid": "10000000-0000-4000-8000-000000000098",
            "execution_plan": plan,
            "input": {
                "sample": {"uuid": "70000000-0000-4000-8000-000000000001"},
                "initial_dose": 1,
            },
            "run_mode": "normal",
        },
        jobs,
    )

    assert {node.id for node in spec.nodes} == {LOOP_NODE_UUID, FINAL_NODE_UUID}
    assert set(spec.repeat_regions) == {LOOP_NODE_UUID}
    region = spec.repeat_regions[LOOP_NODE_UUID]
    assert [node.id for node in region.nodes] == [
        MEASURE_NODE_UUID,
        ADJUST_NODE_UUID,
    ]
    assert all(not node.job_id for node in region.nodes)
    repeat_node = next(node for node in spec.nodes if node.id == LOOP_NODE_UUID)
    assert repeat_node.param["variables"]["initial_dose"] == 1


def test_scheduler_bridge_persists_each_iteration_before_dispatch(
    tmp_path: Path,
) -> None:
    """持久桥必须先幂等建轮次作业，再允许这些作业进入统一派发门禁。"""

    authored = _compile(_repeat_engine(), _repeat_source())
    assert authored.valid and authored.graph is not None, authored.diagnostics
    plan, jobs = ExecutionPlanBuilder().build(
        authored.graph,
        run_mode="normal",
        target_node_uuid=None,
    )
    task_uuid = "10000000-0000-4000-8000-000000000097"
    store = WorkflowStore(tmp_path / "repeat-runtime.db")
    store.create_workflow(
        workflow_uuid=WORKFLOW_UUID,
        name="Repeat runtime",
        tags=[],
        description=None,
        meta_data={},
    )
    with store.transaction() as connection:
        connection.execute(
            """
            INSERT INTO workflow_task(
                uuid, create_time, update_time, deleted_at, description,
                meta_data, workflow_uuid, status, workflow_snapshot,
                execution_plan, run_mode, target_node_uuid, control_status,
                cleanup_status, trace_context, input, output, error_info
            ) VALUES (?, '2026-09-02T00:00:00Z', '2026-09-02T00:00:00Z',
                      NULL, NULL, '{}', ?, 'pending', '{}', ?, 'normal', NULL,
                      'active', 'none', '{}', ?, '{}', '[]')
            """,
            (
                task_uuid,
                WORKFLOW_UUID,
                json.dumps(plan),
                json.dumps(
                    {
                        "sample": {
                            "uuid": "70000000-0000-4000-8000-000000000001"
                        },
                        "initial_dose": 1,
                    }
                ),
            ),
        )
        for job in jobs:
            connection.execute(
                """
                INSERT INTO workflow_node_job(
                    uuid, create_time, update_time, deleted_at, description,
                    meta_data, workflow_task_uuid, workflow_node_uuid,
                    feedback_sequence, topological_index, executor_kind,
                    execution_policy, execution_timeout_seconds, status,
                    attempt, param, feedback_data, return_info, control_data,
                    error_info
                ) VALUES (?, '2026-09-02T00:00:00Z', '2026-09-02T00:00:00Z',
                          NULL, NULL, '{}', ?, ?, 0, ?, ?, ?, 0, 'pending', 1,
                          ?, '{}', '{}', '{}', '[]')
                """,
                (
                    job["uuid"],
                    task_uuid,
                    job["workflow_node_uuid"],
                    job["topological_index"],
                    job["executor_kind"],
                    json.dumps(job.get("execution_policy") or {}),
                    json.dumps(job.get("param") or {}),
                ),
            )
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher)
    bridge = TaskSchedulerBridge(store, scheduler=scheduler)
    control_events: list[dict[str, Any]] = []
    scheduler.add_local_control_listener(control_events.append)
    try:
        bridge.submit(store.get_task(task_uuid))
        first_jobs = {item["action"]: item for item in dispatcher.dispatched[-2:]}
        persisted = store.list_jobs(task_uuid)
        assert len(persisted) == 4
        dynamic = [job for job in persisted if job["meta_data"].get("unilab")]
        assert {job["workflow_node_uuid"] for job in dynamic} == {
            MEASURE_NODE_UUID,
            ADJUST_NODE_UUID,
        }
        assert {job["meta_data"]["unilab"]["iteration_index"] for job in dynamic} == {
            0
        }
        replay_jobs = json.loads(json.dumps(control_events[0]["iteration_jobs"]))
        replay_jobs[0]["param"]["tampered"] = True
        control_job_uuid = next(
            job["uuid"]
            for job in jobs
            if job["workflow_node_uuid"] == LOOP_NODE_UUID
        )
        with pytest.raises(StoreConflict, match="幂等键载荷冲突"):
            bridge._projection.materialize_repeat_iteration(
                control_job_uuid=control_job_uuid,
                iteration_index=0,
                control_path=LOOP_NODE_UUID,
                jobs=replay_jobs,
            )

        scheduler.on_job_finished(
            first_jobs["measure"]["job_id"], True, {"qualified": False}
        )
        first_jobs = {item["action"]: item for item in dispatcher.dispatched[-2:]}
        scheduler.on_job_finished(
            first_jobs["adjust"]["job_id"], True, {"next_dose": 2}
        )
        persisted = store.list_jobs(task_uuid)
        assert len(persisted) == 6
        assert {
            job["meta_data"]["unilab"]["iteration_index"]
            for job in persisted
            if job["meta_data"].get("unilab")
        } == {0, 1}
    finally:
        bridge.close()
        store.close()
