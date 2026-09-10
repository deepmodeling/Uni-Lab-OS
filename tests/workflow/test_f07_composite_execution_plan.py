"""F07 组合工作流调用（CompositeWorkflowInvocation）平面执行计划合同。"""

from __future__ import annotations

from typing import Any

from unilabos.workflow._execution_plan_graph import ExecutionPlanGraphNormalizer
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
    """构造带输入透传输出和完成边界的组合调用节点。"""

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
            "inputs": [
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
