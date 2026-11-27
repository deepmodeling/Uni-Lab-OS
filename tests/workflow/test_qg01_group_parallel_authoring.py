"""QG01 分组（Group）与并行结构（Parallel Structure）创作回归合同。"""

from __future__ import annotations

from typing import Any

from unilabos.workflow.authoring_engine import WorkflowAuthoringEngine
from unilabos.workflow.authoring_kernel import AuthoringCatalogSnapshot

from .test_authoring_engine import (
    ANALYZE_NODE_UUID,
    ANALYZE_READY_TARGET,
    PREPARE_NODE_UUID,
    PREPARE_READY_SOURCE,
    WORKFLOW_UUID,
    _compile,
    _engine,
)

GROUP_A_NODE_UUID = "20000000-0000-4000-8000-000000000011"
GROUP_B_NODE_UUID = "20000000-0000-4000-8000-000000000012"
BRANCH_B_NODE_UUID = "20000000-0000-4000-8000-000000000013"
GROUP_TEMPLATE_UUID = "30000000-0000-4000-8000-000000000011"


def _group_template() -> dict[str, Any]:
    """构造无执行连接点（Handle）的展示分组模板（Presentation Group Template）。

    参数：无。返回：可加入不可变创作目录快照（Catalog Snapshot）的后端形状
    节点模板。异常：无；稳定 UUID 仅属于隔离测试目录。
    """

    return {
        "uuid": GROUP_TEMPLATE_UUID,
        "resource_template_uuid": "31000000-0000-4000-8000-000000000001",
        "name": "group",
        "display_name": "分组",
        "class": "unilabos.workflow.authoring:group",
        "description": "只组织工作流节点（WorkflowNode）的展示层级。",
        "meta_data": {"unilab": {"framework_owner_only": True}},
        "goal": {},
        "goal_default": {},
        "feedback": {},
        "result": {},
        "schema": None,
        "type": "group",
        "node_type": "group",
        "icon": None,
        "header": None,
        "footer": None,
    }


def _group_engine() -> WorkflowAuthoringEngine:
    """在既有动作目录上加入唯一展示分组模板。

    参数：无。返回：含 ``prepare``、``analyze`` 与 ``group`` 的隔离创作编译器
    （Authoring Compiler）。异常：基础测试目录失效时原样传播，防止测试用另一套
    手写动作语义掩盖产品回归。
    """

    # ``base_catalog`` 是产品既有测试动作模板的同代不可变目录。
    base_catalog = _engine()._catalog
    # ``node_templates`` 与 ``handle_templates`` 从目录公开的分离副本重建新快照。
    node_templates = [
        action.detached_template() for action in base_catalog.actions
    ]
    handle_templates = [
        handle
        for action in base_catalog.actions
        for handle in action.detached_handles()
    ]
    return WorkflowAuthoringEngine(
        catalog=AuthoringCatalogSnapshot.from_entities(
            [*node_templates, _group_template()],
            handle_templates,
        )
    )


def _group_source() -> str:
    """返回包含一个真实展示分组节点的最小可信作者源码。

    参数：无。返回：先处理物料、再在同一分组内分析的工作流源码（Workflow
    Source）。异常：无；源码节点 UUID 固定，便于精确断言父子关系。
    """

    return f'''from lab.devices import Reactor
from unilabos.registry.placeholder_type import ResourceSlot
from unilabos.workflow.authoring import device, group, workflow, workflow_output


reactor: Reactor = device()


@workflow(workflow_uuid="{WORKFLOW_UUID}", displayname="Grouped preparation")
def grouped(*, sample: ResourceSlot):
    # unilab:node_uuid={GROUP_A_NODE_UUID}
    with group(name="Preparation"):
        # unilab:node_uuid={PREPARE_NODE_UUID}
        prepared = reactor.prepare(sample=sample, cycles=1)
        # unilab:node_uuid={ANALYZE_NODE_UUID}
        analyzed = reactor.analyze(prepared=prepared.prepared, label="grouped")
    return workflow_output(sample=prepared.prepared, report=analyzed.report)
'''


def _parallel_source() -> str:
    """返回两个分组分支汇入最终动作的最小并行作者源码。

    参数：无。返回：分支 A 的数据输出和分支 B 的完成依赖共同汇入最终分析动作
    的工作流源码（Workflow Source）。异常：无；并行结构本身没有持久执行节点。
    """

    return f'''from lab.devices import Reactor
from unilabos.registry.placeholder_type import ResourceSlot
from unilabos.workflow.authoring import device, group, parallel, workflow, workflow_output


reactor: Reactor = device()


@workflow(workflow_uuid="{WORKFLOW_UUID}", displayname="Parallel preparation")
def parallel_preparation(*, sample_a: ResourceSlot, sample_b: ResourceSlot):
    with parallel():
        # unilab:node_uuid={GROUP_A_NODE_UUID}
        with group(name="Branch A"):
            # unilab:node_uuid={PREPARE_NODE_UUID}
            branch_a = reactor.prepare(sample=sample_a, cycles=1)
        # unilab:node_uuid={GROUP_B_NODE_UUID}
        with group(name="Branch B"):
            # unilab:node_uuid={BRANCH_B_NODE_UUID}
            branch_b = reactor.prepare(sample=sample_b, cycles=2)
    # unilab:node_uuid={ANALYZE_NODE_UUID}
    final = reactor.analyze(prepared=branch_a.prepared, label="complete")
    return workflow_output(
        sample_a=branch_a.prepared,
        sample_b=branch_b.prepared,
        report=final.report,
    )
'''


def _node(graph: dict[str, Any], node_uuid: str) -> dict[str, Any]:
    """按稳定 UUID 取得唯一候选工作流节点（WorkflowNode）。

    参数：``graph`` 是后端五集合候选图，``node_uuid`` 是预期节点身份。返回：
    唯一节点字典。异常/断言：缺失或重复时由 ``next``/测试断言失败，不做模糊匹配。
    """

    matches = [node for node in graph["nodes"] if node["uuid"] == node_uuid]
    assert len(matches) == 1
    return matches[0]


def test_group_is_a_presentation_node_without_execution_edges() -> None:
    """展示分组节点应保留父子关系但不成为执行屏障。

    参数：无。返回：无。断言：编译结果包含一个 ``group`` 节点、两个子动作，
    分组不出现在执行边端点；生成源码仍含 ``with group``，再次编译达到同一语义
    固定点。失败诊断必须保留在候选结果中，不允许空图假绿。
    """

    engine = _group_engine()
    result = _compile(engine, _group_source())

    assert result.valid, result.diagnostics
    assert result.graph is not None
    graph = result.graph
    assert {node["uuid"] for node in graph["nodes"]} == {
        GROUP_A_NODE_UUID,
        PREPARE_NODE_UUID,
        ANALYZE_NODE_UUID,
    }
    assert _node(graph, GROUP_A_NODE_UUID)["type"] == "group"
    assert _node(graph, PREPARE_NODE_UUID)["parent_uuid"] == GROUP_A_NODE_UUID
    assert _node(graph, ANALYZE_NODE_UUID)["parent_uuid"] == GROUP_A_NODE_UUID
    assert all(
        GROUP_A_NODE_UUID
        not in {edge["source_node_uuid"], edge["target_node_uuid"]}
        for edge in graph["edges"]
    )
    assert {item["workflow_node_uuid"] for item in result.source_map} == {
        GROUP_A_NODE_UUID,
        PREPARE_NODE_UUID,
        ANALYZE_NODE_UUID,
    }
    assert result.normalized_python_source is not None
    assert "with group(name='Preparation'):" in result.normalized_python_source

    repeated = _compile(
        engine,
        result.normalized_python_source,
        graph=graph,
    )
    assert repeated.valid, repeated.diagnostics
    assert repeated.graph == graph


def test_parallel_groups_use_real_ready_order_without_fork_join_nodes() -> None:
    """并行分组应只形成真实数据边和就绪控制边（Ready Control Edge）。

    参数：无。返回：无。断言：候选图含两个展示分组和三个动作，不含合成
    fork/join；最终动作同时接收分支 A 数据边与分支 B 的 ``ready`` 控制边，且
    源码生成保留 ``parallel``。这覆盖 S07 两条物料链汇合所需的最小语义。
    """

    result = _compile(_group_engine(), _parallel_source())

    assert result.valid, result.diagnostics
    assert result.graph is not None
    graph = result.graph
    assert {node["uuid"] for node in graph["nodes"]} == {
        GROUP_A_NODE_UUID,
        GROUP_B_NODE_UUID,
        PREPARE_NODE_UUID,
        BRANCH_B_NODE_UUID,
        ANALYZE_NODE_UUID,
    }
    assert all(node["type"] not in {"fork", "join"} for node in graph["nodes"])
    assert _node(graph, PREPARE_NODE_UUID)["parent_uuid"] == GROUP_A_NODE_UUID
    assert _node(graph, BRANCH_B_NODE_UUID)["parent_uuid"] == GROUP_B_NODE_UUID
    assert all(
        group_uuid not in {edge["source_node_uuid"], edge["target_node_uuid"]}
        for group_uuid in (GROUP_A_NODE_UUID, GROUP_B_NODE_UUID)
        for edge in graph["edges"]
    )
    # ``incoming`` 是两个并行分支对最终动作的完整、无合成节点依赖集合。
    incoming = [
        edge
        for edge in graph["edges"]
        if edge["target_node_uuid"] == ANALYZE_NODE_UUID
    ]
    assert {edge["source_node_uuid"] for edge in incoming} == {
        PREPARE_NODE_UUID,
        BRANCH_B_NODE_UUID,
    }
    ready_edge = next(
        edge for edge in incoming if edge["source_node_uuid"] == BRANCH_B_NODE_UUID
    )
    assert ready_edge["source_handle_uuid"] == PREPARE_READY_SOURCE
    assert ready_edge["target_handle_uuid"] == ANALYZE_READY_TARGET
    assert result.normalized_python_source is not None
    assert "with parallel():" in result.normalized_python_source


def test_parallel_branch_cannot_read_a_sibling_result() -> None:
    """并行分支不能读取同级分支的未汇合结果。

    参数：无。返回：无。断言：把分支 B 输入改成分支 A 输出后，静态编译器必须
    返回结构化失败而不是用源码顺序串行化两个分支；这保持并行结构（Parallel
    Structure）的失败关闭语义。
    """

    source = _parallel_source().replace(
        "sample=sample_b, cycles=2",
        "sample=branch_a.prepared, cycles=2",
    )
    result = _compile(_group_engine(), source)

    assert not result.valid
    assert result.graph is None
    assert [item["code"] for item in result.diagnostics] == [
        "unsupported_authoring_syntax"
    ]
