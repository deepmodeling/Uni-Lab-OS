"""F07 组合工作流调用（CompositeWorkflowInvocation）平面执行计划合同。"""

from __future__ import annotations

from typing import Any

import pytest

from unilabos.workflow._execution_plan_graph import (
    ExecutionPlanBuildError,
    ExecutionPlanGraphNormalizer,
)
from unilabos.workflow.execution_plan import ExecutionPlanBuilder
from unilabos.workflow.task_input import prepare_task_input

WORKFLOW_UUID = "71000000-0000-4000-8000-000000000001"
PRODUCER_UUID = "71000000-0000-4000-8000-000000000002"
INVOCATION_UUID = "71000000-0000-4000-8000-000000000003"
INTERNAL_UUID = "71000000-0000-4000-8000-000000000004"
CONSUMER_UUID = "71000000-0000-4000-8000-000000000005"
SECOND_INVOCATION_UUID = "71000000-0000-4000-8000-000000000006"
SECOND_INTERNAL_UUID = "71000000-0000-4000-8000-000000000007"
PRODUCER_TEMPLATE = "72000000-0000-4000-8000-000000000001"
INVOCATION_TEMPLATE = "72000000-0000-4000-8000-000000000002"
INTERNAL_TEMPLATE = "72000000-0000-4000-8000-000000000003"
CONSUMER_TEMPLATE = "72000000-0000-4000-8000-000000000004"
PRODUCER_SOURCE = "73000000-0000-4000-8000-000000000001"
INVOCATION_TARGET = "73000000-0000-4000-8000-000000000002"
INVOCATION_SOURCE = "73000000-0000-4000-8000-000000000003"
INTERNAL_TARGET = "73000000-0000-4000-8000-000000000004"
INTERNAL_READY = "73000000-0000-4000-8000-000000000005"
CONSUMER_TARGET = "73000000-0000-4000-8000-000000000006"
REPEAT_REGION_UUID = "71000000-0000-4000-8000-000000000008"


def _node(
    uuid: str,
    template_uuid: str,
    *,
    node_type: str = "compute",
    param: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造一个最小冻结节点；参数定位身份、模板、类型和可选静态参数。"""

    return {
        "uuid": uuid,
        "workflow_node_template_uuid": template_uuid,
        "name": uuid,
        "type": node_type,
        "pose": {},
        "param": param or {},
        "execution_policy": {},
        "disabled": False,
        "minimized": False,
        "meta_data": {"unilab": {"input_bindings": {}}},
    }


def _handle(
    uuid: str,
    template_uuid: str,
    *,
    io_type: str,
    key: str,
    data_source: str,
    required: bool,
) -> dict[str, Any]:
    """构造整数连接点（Handle）；参数给出端点身份与值来源语义。"""

    return {
        "uuid": uuid,
        "workflow_node_template_uuid": template_uuid,
        "handle_key": key,
        "io_type": io_type,
        "type": "integer",
        "required": required,
        "data_source": data_source,
        "data_key": key,
        "meta_data": {"unilab": {"value_schema": {"type": "integer"}}},
    }


def _edge(
    uuid: str,
    source_node_uuid: str,
    source_handle_uuid: str,
    target_node_uuid: str,
    target_handle_uuid: str,
) -> dict[str, str]:
    """构造一条冻结工作流边；参数完整指定来源和目标端点。"""

    return {
        "uuid": uuid,
        "source_node_uuid": source_node_uuid,
        "source_handle_uuid": source_handle_uuid,
        "target_node_uuid": target_node_uuid,
        "target_handle_uuid": target_handle_uuid,
    }


def _composite_node(
    *,
    static_value: int | None = None,
    invocation_uuid: str = INVOCATION_UUID,
    internal_uuid: str = INTERNAL_UUID,
) -> dict[str, Any]:
    """构造带参数透传输出和完成边界的组合调用节点。

    参数：``static_value`` 是可选节点固定值；``invocation_uuid`` 是组合调用稳定
    身份；``internal_uuid`` 是本次展开的内部节点身份。返回：供公开执行计划编译器
    使用的完整节点。异常：无；测试调用方负责提供符合 UUID 合同的身份。
    """

    node = _node(
        invocation_uuid,
        INVOCATION_TEMPLATE,
        node_type="workflow",
        param={} if static_value is None else {"value": static_value},
    )
    node["meta_data"]["unilab"]["composite"] = {
        "target_mappings": {
            INVOCATION_TARGET: [
                {
                    "workflow_node_uuid": internal_uuid,
                    "target_handle_uuid": INTERNAL_TARGET,
                }
            ]
        },
        "source_mappings": {
            INVOCATION_SOURCE: {"kind": "workflow_input", "parameter": "value"}
        },
        "structural_mappings": {
            "entry_targets": [
                {
                    "workflow_node_uuid": internal_uuid,
                    "target_handle_uuid": INTERNAL_TARGET,
                }
            ],
            "completion_sources": [
                {
                    "workflow_node_uuid": internal_uuid,
                    "source_handle_uuid": INTERNAL_READY,
                }
            ],
        },
        "contract_compatibility": {
            "parameters": [
                {
                    "name": "value",
                    "handle_uuid": INVOCATION_TARGET,
                    "schema": {"type": "integer"},
                    "required": True,
                    "has_default": False,
                }
            ]
        },
    }
    return node


def _handles() -> list[dict[str, Any]]:
    """返回生产者、组合边界、内部动作和消费者使用的完整端点集合。"""

    return [
        _handle(
            PRODUCER_SOURCE,
            PRODUCER_TEMPLATE,
            io_type="source",
            key="value",
            data_source="result",
            required=False,
        ),
        _handle(
            INVOCATION_TARGET,
            INVOCATION_TEMPLATE,
            io_type="target",
            key="value",
            data_source="executor",
            required=True,
        ),
        _handle(
            INVOCATION_SOURCE,
            INVOCATION_TEMPLATE,
            io_type="source",
            key="value",
            data_source="result",
            required=False,
        ),
        _handle(
            INTERNAL_TARGET,
            INTERNAL_TEMPLATE,
            io_type="target",
            key="value",
            data_source="executor",
            required=True,
        ),
        _handle(
            INTERNAL_READY,
            INTERNAL_TEMPLATE,
            io_type="source",
            key="ready",
            data_source="status",
            required=False,
        ),
        _handle(
            CONSUMER_TARGET,
            CONSUMER_TEMPLATE,
            io_type="target",
            key="value",
            data_source="executor",
            required=True,
        ),
    ]


def test_result_source_remains_value_provider_in_frozen_plan() -> None:
    """动作 result 输出必须提供值，不能被降级为纯依赖边。"""

    graph = {
        "workflow": {
            "uuid": WORKFLOW_UUID,
            "revision": 1,
            "name": "result value",
            "tags": [],
            "meta_data": {
                "unilab": {
                    "input_contract": {"version": 1, "parameters": []},
                    "output_contract": {"version": 1, "outputs": []},
                    "output_bindings": {},
                }
            },
        },
        "nodes": [
            _node(PRODUCER_UUID, PRODUCER_TEMPLATE, param={"seed": 1}),
            _node(CONSUMER_UUID, CONSUMER_TEMPLATE),
        ],
        "edges": [
            _edge(
                "74000000-0000-4000-8000-000000000001",
                PRODUCER_UUID,
                PRODUCER_SOURCE,
                CONSUMER_UUID,
                CONSUMER_TARGET,
            )
        ],
        "node_templates": [
            {"uuid": PRODUCER_TEMPLATE, "node_type": "compute", "type": "compute"},
            {"uuid": CONSUMER_TEMPLATE, "node_type": "compute", "type": "compute"},
        ],
        "handle_templates": [
            _handles()[0],
            _handles()[-1],
        ],
    }

    plan, jobs = ExecutionPlanBuilder().build(
        graph, run_mode="normal", target_node_uuid=None
    )
    prepared = prepare_task_input(
        graph=graph,
        raw_input={},
        execution_plan=plan,
        jobs=jobs,
    )

    assert plan["edges"][0].get("dependency_only") is not True
    assert len(prepared.jobs) == 2


def test_composite_passthrough_flattens_value_and_completion_edges() -> None:
    """组合透传值须进入内部动作和下游，同时保留完成顺序依赖。"""

    nodes = {
        node["uuid"]: node
        for node in (
            _node(PRODUCER_UUID, PRODUCER_TEMPLATE),
            _composite_node(),
            _node(INTERNAL_UUID, INTERNAL_TEMPLATE),
            _node(CONSUMER_UUID, CONSUMER_TEMPLATE),
        )
    }
    handles = {handle["uuid"]: handle for handle in _handles()}
    edges = [
        _edge(
            "74000000-0000-4000-8000-000000000002",
            PRODUCER_UUID,
            PRODUCER_SOURCE,
            INVOCATION_UUID,
            INVOCATION_TARGET,
        ),
        _edge(
            "74000000-0000-4000-8000-000000000003",
            INVOCATION_UUID,
            INVOCATION_SOURCE,
            CONSUMER_UUID,
            CONSUMER_TARGET,
        ),
    ]

    flattened, params = ExecutionPlanGraphNormalizer().flatten_composite_edges(
        nodes=nodes,
        edges=edges,
        handles=handles,
    )

    endpoints = {
        (
            edge["source_node_uuid"],
            edge["source_handle_uuid"],
            edge["target_node_uuid"],
            edge["target_handle_uuid"],
        )
        for edge in flattened
    }
    assert endpoints == {
        (PRODUCER_UUID, PRODUCER_SOURCE, INTERNAL_UUID, INTERNAL_TARGET),
        (PRODUCER_UUID, PRODUCER_SOURCE, CONSUMER_UUID, CONSUMER_TARGET),
        (INTERNAL_UUID, INTERNAL_READY, CONSUMER_UUID, CONSUMER_TARGET),
    }
    assert params == {}


def test_chained_composite_passthrough_counts_only_value_provider() -> None:
    """连续组合调用须保留完成边，但不得把它误算成第二个值提供者。"""

    nodes = {
        node["uuid"]: node
        for node in (
            _node(PRODUCER_UUID, PRODUCER_TEMPLATE),
            _composite_node(),
            _node(INTERNAL_UUID, INTERNAL_TEMPLATE),
            _composite_node(
                invocation_uuid=SECOND_INVOCATION_UUID,
                internal_uuid=SECOND_INTERNAL_UUID,
            ),
            _node(SECOND_INTERNAL_UUID, INTERNAL_TEMPLATE),
            _node(CONSUMER_UUID, CONSUMER_TEMPLATE),
        )
    }
    handles = {handle["uuid"]: handle for handle in _handles()}
    flattened, params = ExecutionPlanGraphNormalizer().flatten_composite_edges(
        nodes=nodes,
        edges=[
            _edge(
                "74000000-0000-4000-8000-000000000005",
                PRODUCER_UUID,
                PRODUCER_SOURCE,
                INVOCATION_UUID,
                INVOCATION_TARGET,
            ),
            _edge(
                "74000000-0000-4000-8000-000000000006",
                INVOCATION_UUID,
                INVOCATION_SOURCE,
                SECOND_INVOCATION_UUID,
                INVOCATION_TARGET,
            ),
            _edge(
                "74000000-0000-4000-8000-000000000007",
                SECOND_INVOCATION_UUID,
                INVOCATION_SOURCE,
                CONSUMER_UUID,
                CONSUMER_TARGET,
            ),
        ],
        handles=handles,
    )

    endpoints = {
        (
            edge["source_node_uuid"],
            edge["source_handle_uuid"],
            edge["target_node_uuid"],
            edge["target_handle_uuid"],
        )
        for edge in flattened
    }
    assert endpoints == {
        (PRODUCER_UUID, PRODUCER_SOURCE, INTERNAL_UUID, INTERNAL_TARGET),
        (PRODUCER_UUID, PRODUCER_SOURCE, SECOND_INTERNAL_UUID, INTERNAL_TARGET),
        (INTERNAL_UUID, INTERNAL_READY, SECOND_INTERNAL_UUID, INTERNAL_TARGET),
        (PRODUCER_UUID, PRODUCER_SOURCE, CONSUMER_UUID, CONSUMER_TARGET),
        (SECOND_INTERNAL_UUID, INTERNAL_READY, CONSUMER_UUID, CONSUMER_TARGET),
    }
    assert params == {}


def test_composite_static_passthrough_projects_actual_action_params() -> None:
    """静态组合入参须同时冻结到内部动作和透传输出的下游动作。"""

    nodes = {
        node["uuid"]: node
        for node in (
            _composite_node(static_value=7),
            _node(INTERNAL_UUID, INTERNAL_TEMPLATE),
            _node(CONSUMER_UUID, CONSUMER_TEMPLATE),
        )
    }
    handles = {handle["uuid"]: handle for handle in _handles()}
    flattened, params = ExecutionPlanGraphNormalizer().flatten_composite_edges(
        nodes=nodes,
        edges=[
            _edge(
                "74000000-0000-4000-8000-000000000004",
                INVOCATION_UUID,
                INVOCATION_SOURCE,
                CONSUMER_UUID,
                CONSUMER_TARGET,
            )
        ],
        handles=handles,
    )

    assert params == {
        INTERNAL_UUID: {"value": 7},
        CONSUMER_UUID: {"value": 7},
    }
    assert [
        (edge["source_node_uuid"], edge["target_node_uuid"]) for edge in flattened
    ] == [(INTERNAL_UUID, CONSUMER_UUID)]


@pytest.mark.parametrize("region_type", ["repeat_until", "condition"])
@pytest.mark.parametrize("composite_consumer", [False, True])
def test_completion_inside_control_region_uses_region_barrier(
    region_type: str,
    composite_consumer: bool,
) -> None:
    """组合完成边提升到控制区域后仍可编译，并阻止普通或组合后续动作提前执行。"""

    invocation = _composite_node(static_value=7)
    invocation["parent_uuid"] = REPEAT_REGION_UUID
    internal = _node(INTERNAL_UUID, INTERNAL_TEMPLATE)
    internal["parent_uuid"] = REPEAT_REGION_UUID
    region = _node(REPEAT_REGION_UUID, REPEAT_REGION_UUID, node_type=region_type)
    nodes = {
        node["uuid"]: node
        for node in (
            invocation,
            internal,
            region,
            _node(CONSUMER_UUID, CONSUMER_TEMPLATE),
        )
    }
    consumer_uuid = CONSUMER_UUID
    consumer_handle = CONSUMER_TARGET
    if composite_consumer:
        nodes[SECOND_INVOCATION_UUID] = _composite_node(
            invocation_uuid=SECOND_INVOCATION_UUID,
            internal_uuid=CONSUMER_UUID,
        )
        nodes[CONSUMER_UUID] = _node(CONSUMER_UUID, INTERNAL_TEMPLATE)
        consumer_uuid = SECOND_INVOCATION_UUID
        consumer_handle = INVOCATION_TARGET
    flattened, _ = ExecutionPlanGraphNormalizer().flatten_composite_edges(
        nodes=nodes,
        edges=[
            _edge(
                "74000000-0000-4000-8000-000000000008",
                INVOCATION_UUID,
                INVOCATION_SOURCE,
                consumer_uuid,
                consumer_handle,
            )
        ],
        handles={handle["uuid"]: handle for handle in _handles()},
    )

    completion_edges = {
        (edge["source_node_uuid"], edge["source_handle_uuid"], edge["target_node_uuid"])
        for edge in flattened
    }
    expected_source = REPEAT_REGION_UUID if region_type == "repeat_until" else INTERNAL_UUID
    expected_handle = "" if region_type == "repeat_until" else INTERNAL_READY
    assert (expected_source, expected_handle, CONSUMER_UUID) in completion_edges
    active = {key: nodes[key] for key in (REPEAT_REGION_UUID, CONSUMER_UUID)}
    if region_type == "condition":
        active[INTERNAL_UUID] = internal
    handles = {handle["uuid"]: handle for handle in _handles()}
    normalizer = ExecutionPlanGraphNormalizer()
    _, runtime_ids = normalizer.runtime_handles(active=active, handles=handles)
    planned = normalizer.contract_edges(
        nodes=nodes,
        active=active,
        edges=flattened,
        handles=handles,
        runtime_handle_ids=runtime_ids,
    )
    assert len(planned) == 1
    assert planned[0]["dependency_only"] is True
    assert planned[0]["source_node_uuid"] == expected_source
    # 循环：区域节点作无值屏障；条件：仍是叶动作的 ready 连接点（运行时身份）。
    if region_type == "repeat_until":
        assert planned[0]["source_handle_uuid"] == ""
        assert planned[0]["target_handle_uuid"] == ""
    else:
        assert (
            planned[0]["source_handle_uuid"]
            == runtime_ids[(INTERNAL_UUID, INTERNAL_READY)]
        )
        target_template = INTERNAL_TARGET if composite_consumer else CONSUMER_TARGET
        assert (
            planned[0]["target_handle_uuid"]
            == runtime_ids[(CONSUMER_UUID, target_template)]
        )
    order = normalizer.topological_order(active, planned)
    assert order.index(expected_source) < order.index(CONSUMER_UUID)
    # 条件求值完成后，后续动作仍须等待分支内物理动作完成。
    from unilabos.app.scheduler.dag_state import WorkflowRun
    from unilabos.app.scheduler.models import WorkflowEdge, WorkflowNode, WorkflowSpec
    run = WorkflowRun(WorkflowSpec(workflow_id=WORKFLOW_UUID,
        nodes=[WorkflowNode(id=key, executor_kind="device_action") for key in active],
        edges=[WorkflowEdge(uuid=edge["uuid"], source_node_id=edge["source_node_uuid"],
                            target_node_id=edge["target_node_uuid"]) for edge in planned]))
    if region_type == "condition":
        run.mark_finished(REPEAT_REGION_UUID, {})
        assert CONSUMER_UUID not in {node.id for node in run.ready_nodes()}
    run.mark_finished(expected_source, {})
    assert CONSUMER_UUID in {node.id for node in run.ready_nodes()}



@pytest.mark.parametrize(
    "source_handle,marked", [("", False), ("missing", False), ("missing", True)]
)
def test_missing_source_handle_is_not_implicitly_a_completion_barrier(
    source_handle: str,
    marked: bool,
) -> None:
    """普通数据边缺失来源连接点时仍拒绝；标记不能掩盖错误的非空连接点。"""

    edge: dict[str, Any] = _edge(
        "edge", PRODUCER_UUID, source_handle, CONSUMER_UUID, CONSUMER_TARGET
    )
    edge["dependency_only"] = marked
    with pytest.raises(ExecutionPlanBuildError, match="工作流边引用快照外连接点"):
        ExecutionPlanGraphNormalizer._direct_edge(
            edge,
            handles={h["uuid"]: h for h in _handles()},
            runtime_handle_ids={(CONSUMER_UUID, CONSUMER_TARGET): "consumer-runtime"},
        )


@pytest.mark.parametrize("target_handle", ["missing", CONSUMER_TARGET])
def test_completion_barrier_still_validates_target(target_handle: str) -> None:
    """纯依赖完成边仍须验证目标连接点存在且属于目标节点。"""

    edge: dict[str, Any] = _edge(
        "edge", REPEAT_REGION_UUID, "", CONSUMER_UUID, target_handle
    )
    edge["dependency_only"] = True
    with pytest.raises(ExecutionPlanBuildError):
        ExecutionPlanGraphNormalizer._direct_edge(
            edge,
            handles={h["uuid"]: h for h in _handles()},
            runtime_handle_ids={},
        )


def test_composite_output_binding_is_frozen_against_internal_job() -> None:
    """父工作流输出引用组合调用时须改写为真实内部 Job，不能保留虚拟节点。"""

    invocation = _composite_node()
    invocation["meta_data"]["unilab"]["composite"]["source_mappings"] = {
        INVOCATION_SOURCE: {
            "kind": "node_output",
            "workflow_node_uuid": INTERNAL_UUID,
            "source_handle_uuid": INTERNAL_READY,
        }
    }
    graph = {
        "workflow": {
            "uuid": WORKFLOW_UUID,
            "revision": 1,
            "name": "composite output",
            "tags": [],
            "meta_data": {
                "unilab": {
                    "input_contract": {"version": 1, "parameters": []},
                    "output_contract": {
                        "version": 1,
                        "outputs": [{"name": "result", "schema": {"type": "integer"}}],
                    },
                    "output_bindings": {
                        "result": {
                            "kind": "node_output",
                            "workflow_node_uuid": INVOCATION_UUID,
                            "source_handle_uuid": INVOCATION_SOURCE,
                        }
                    },
                }
            },
        },
        "nodes": [invocation, _node(INTERNAL_UUID, INTERNAL_TEMPLATE)],
        "edges": [],
        "node_templates": [
            {
                "uuid": INVOCATION_TEMPLATE,
                "node_type": "workflow",
                "type": "workflow",
            },
            {"uuid": INTERNAL_TEMPLATE, "node_type": "compute", "type": "compute"},
        ],
        "handle_templates": [
            handle
            for handle in _handles()
            if handle["uuid"] in {INVOCATION_SOURCE, INTERNAL_READY}
        ],
    }

    plan, jobs = ExecutionPlanBuilder().build(
        graph, run_mode="normal", target_node_uuid=None
    )
    prepared = prepare_task_input(
        graph=graph,
        raw_input={},
        execution_plan=plan,
        jobs=jobs,
    )

    output_node = next(
        node
        for node in prepared.execution_plan["nodes"]
        if node.get("kind") == "workflow_output"
    )
    assert output_node["output_bindings"]["result"] == {
        "kind": "node_output",
        "workflow_node_uuid": INTERNAL_UUID,
        "source_handle_uuid": INTERNAL_READY,
    }


def test_child_condition_selection_does_not_release_following_action():
    """后续物理动作等待所选分支出口；未选分支跳过后不会永久阻塞。"""
    from unilabos.app.scheduler.dag_state import WorkflowRun
    from unilabos.app.scheduler.models import WorkflowEdge, WorkflowNode, WorkflowSpec

    selected = SECOND_INTERNAL_UUID
    alternative = SECOND_INVOCATION_UUID
    child = _composite_node(static_value=7)
    control = _node(
        INTERNAL_UUID,
        INTERNAL_TEMPLATE,
        node_type="condition",
        param={
            "branches": [
                {"exit_node_uuids": [selected]},
                {"exit_node_uuids": [alternative]},
            ]
        },
    )
    control["parent_uuid"] = INVOCATION_UUID
    members = [
        _node(selected, INTERNAL_TEMPLATE),
        _node(alternative, INTERNAL_TEMPLATE),
    ]
    for member in members:
        member["parent_uuid"] = INTERNAL_UUID
    nodes = {
        n["uuid"]: n
        for n in [child, control, *members, _node(CONSUMER_UUID, CONSUMER_TEMPLATE)]
    }
    edges, _ = ExecutionPlanGraphNormalizer().flatten_composite_edges(
        nodes=nodes,
        edges=[
            _edge(
                "74000000-0000-4000-8000-000000000020",
                INVOCATION_UUID,
                INVOCATION_SOURCE,
                CONSUMER_UUID,
                CONSUMER_TARGET,
            )
        ],
        handles={h["uuid"]: h for h in _handles()},
    )
    parents = {
        e["source_node_uuid"] for e in edges if e["target_node_uuid"] == CONSUMER_UUID
    }
    assert parents == {selected, alternative}
    run = WorkflowRun(
        WorkflowSpec(
            workflow_id=WORKFLOW_UUID,
            nodes=[
                WorkflowNode(id=i)
                for i in [INTERNAL_UUID, selected, alternative, CONSUMER_UUID]
            ],
            edges=[
                WorkflowEdge(
                    uuid=str(i),
                    source_node_id=e["source_node_uuid"],
                    target_node_id=e["target_node_uuid"],
                )
                for i, e in enumerate(edges)
            ],
        )
    )
    run.mark_finished(INTERNAL_UUID, {"selected_branch": "if"})
    run.mark_skipped(alternative, reason="branch_not_selected")
    assert CONSUMER_UUID not in {n.id for n in run.ready_nodes()}
    run.mark_finished(selected, {})
    assert CONSUMER_UUID in {n.id for n in run.ready_nodes()}


def test_nested_child_entry_preserves_preceding_action_dependency():
    outer = _composite_node(internal_uuid=SECOND_INVOCATION_UUID, static_value=7)
    inner = _composite_node(invocation_uuid=SECOND_INVOCATION_UUID, static_value=7)
    inner['parent_uuid'] = INVOCATION_UUID
    leaf = _node(INTERNAL_UUID, INTERNAL_TEMPLATE)
    leaf['parent_uuid'] = SECOND_INVOCATION_UUID
    nodes = {n['uuid']: n for n in [outer, inner, leaf, _node(PRODUCER_UUID, PRODUCER_TEMPLATE)]}
    ready = _handle("73000000-0000-4000-8000-000000000020", INVOCATION_TEMPLATE,
                    io_type="target", key="ready", data_source="parameter", required=False)
    edges, _ = ExecutionPlanGraphNormalizer().flatten_composite_edges(
        nodes=nodes,
        edges=[_edge('74000000-0000-4000-8000-000000000021', PRODUCER_UUID, PRODUCER_SOURCE,
                     INVOCATION_UUID, ready["uuid"])],
        handles={h['uuid']: h for h in [*_handles(), ready]})
    assert {(e['source_node_uuid'], e['target_node_uuid']) for e in edges} == {(PRODUCER_UUID, INTERNAL_UUID)}


@pytest.mark.parametrize("control_kind", ["condition", "repeat_until"])
def test_handleless_composite_barrier_waits_for_real_child_completion(control_kind):
    """组合调用后的纯控制边须落到真实完成动作，且不得虚构数据连接点。"""
    child = _composite_node(static_value=7)
    leaf = _node(INTERNAL_UUID, INTERNAL_TEMPLATE)
    leaf["parent_uuid"] = INVOCATION_UUID
    control = _node(CONSUMER_UUID, CONSUMER_TEMPLATE, node_type=control_kind)
    nodes = {n["uuid"]: n for n in (child, leaf, control)}
    barrier = {**_edge("74000000-0000-4000-8000-000000000025",
                       INVOCATION_UUID, "", CONSUMER_UUID, ""), "dependency_only": True}
    flattened, _ = ExecutionPlanGraphNormalizer().flatten_composite_edges(
        nodes=nodes, edges=[barrier], handles={h["uuid"]: h for h in _handles()})
    assert len(flattened) == 1
    assert flattened[0]["source_node_uuid"] == INTERNAL_UUID
    assert flattened[0]["source_handle_uuid"] == ""
    planned = ExecutionPlanGraphNormalizer._direct_edge(
        flattened[0], handles={}, runtime_handle_ids={}, target_node=control)
    assert planned["dependency_only"] is True
    assert planned["target_node_uuid"] == CONSUMER_UUID
    with pytest.raises(ExecutionPlanBuildError):
        ExecutionPlanGraphNormalizer().flatten_composite_edges(
            nodes=nodes, edges=[{**barrier, "dependency_only": False}],
            handles={h["uuid"]: h for h in _handles()})


def test_nested_child_receives_computed_parent_parameter():
    """上游真实结果穿过两层调用后，仍连到最内层动作的必填输入。"""
    outer = _composite_node(internal_uuid=SECOND_INVOCATION_UUID)
    composite = outer["meta_data"]["unilab"]["composite"]
    composite["target_mappings"][INVOCATION_TARGET][0]["target_handle_uuid"] = INVOCATION_TARGET
    inner = _composite_node(invocation_uuid=SECOND_INVOCATION_UUID)
    inner["parent_uuid"] = INVOCATION_UUID
    leaf = _node(INTERNAL_UUID, INTERNAL_TEMPLATE)
    leaf["parent_uuid"] = SECOND_INVOCATION_UUID
    nodes = {n["uuid"]: n for n in (outer, inner, leaf, _node(PRODUCER_UUID, PRODUCER_TEMPLATE))}
    edges, _ = ExecutionPlanGraphNormalizer().flatten_composite_edges(
        nodes=nodes,
        edges=[_edge("74000000-0000-4000-8000-000000000026", PRODUCER_UUID,
                     PRODUCER_SOURCE, INVOCATION_UUID, INVOCATION_TARGET)],
        handles={h["uuid"]: h for h in _handles()})
    values = [e for e in edges if not e.get("dependency_only")]
    assert len(values) == 1
    assert values[0]["source_node_uuid"] == PRODUCER_UUID
    assert values[0]["target_node_uuid"] == INTERNAL_UUID
    assert values[0]["target_handle_uuid"] == INTERNAL_TARGET
    assert not any(e["source_node_uuid"] in {INVOCATION_UUID, SECOND_INVOCATION_UUID}
                   or e["target_node_uuid"] in {INVOCATION_UUID, SECOND_INVOCATION_UUID} for e in edges)


def test_empty_structural_mappings_still_serialize_sibling_invocations() -> None:
    """结构映射为空时，相邻实验操作调用仍要等前一个调用的叶子完成。"""

    ready_source = "73000000-0000-4000-8000-000000000010"
    ready_target = "73000000-0000-4000-8000-000000000011"
    internal_ready_target = "73000000-0000-4000-8000-000000000012"
    first = _composite_node()
    second = _composite_node(
        invocation_uuid=SECOND_INVOCATION_UUID,
        internal_uuid=SECOND_INTERNAL_UUID,
    )
    for invocation in (first, second):
        invocation["meta_data"]["unilab"]["composite"]["structural_mappings"] = {
            "entry_targets": [],
            "completion_sources": [],
        }
    first_internal = _node(INTERNAL_UUID, INTERNAL_TEMPLATE)
    first_internal["parent_uuid"] = INVOCATION_UUID
    second_internal = _node(SECOND_INTERNAL_UUID, INTERNAL_TEMPLATE)
    second_internal["parent_uuid"] = SECOND_INVOCATION_UUID
    handles = {
        handle["uuid"]: handle
        for handle in (
            *_handles(),
            _handle(
                ready_source,
                INVOCATION_TEMPLATE,
                io_type="source",
                key="ready",
                data_source="status",
                required=False,
            ),
            _handle(
                ready_target,
                INVOCATION_TEMPLATE,
                io_type="target",
                key="ready",
                data_source="status",
                required=False,
            ),
            _handle(
                internal_ready_target,
                INTERNAL_TEMPLATE,
                io_type="target",
                key="ready",
                data_source="status",
                required=False,
            ),
        )
    }
    flattened, _params = ExecutionPlanGraphNormalizer().flatten_composite_edges(
        nodes={
            node["uuid"]: node
            for node in (first, first_internal, second, second_internal)
        },
        edges=[
            _edge(
                "74000000-0000-4000-8000-000000000008",
                INVOCATION_UUID,
                ready_source,
                SECOND_INVOCATION_UUID,
                ready_target,
            )
        ],
        handles=handles,
    )

    assert (
        INTERNAL_UUID,
        INTERNAL_READY,
        SECOND_INTERNAL_UUID,
        internal_ready_target,
    ) in {
        (
            edge["source_node_uuid"],
            edge["source_handle_uuid"],
            edge["target_node_uuid"],
            edge["target_handle_uuid"],
        )
        for edge in flattened
    }
