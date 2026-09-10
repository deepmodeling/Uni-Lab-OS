"""F05.4-C8 已应用创作投影（Applied Authoring Projection）公共合同。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from tests.workflow.test_authoring_engine import (
    ANALYZE_NODE_UUID,
    ANALYZE_TEMPLATE_UUID,
    PREPARE_NODE_UUID,
    PREPARE_SAMPLE_TARGET,
    PREPARE_TEMPLATE_UUID,
    WORKFLOW_UUID,
    _applied_graph,
    _engine,
    _source,
    _template,
)
from tests.workflow.test_qg01_group_parallel_authoring import (
    GROUP_A_NODE_UUID,
    _group_engine,
    _group_source,
)
from unilabos.workflow.authoring_engine import WorkflowAuthoringEngine
from unilabos.workflow.authoring_kernel import AuthoringCatalogSnapshot

CREATE_TIME = "2026-08-05T12:48:35.647663Z"
UPDATE_TIME = "2026-08-05T12:48:35.650229Z"


def _compile(
    engine: WorkflowAuthoringEngine,
    *,
    graph: dict[str, Any],
    source: str | None = None,
):
    """通过公共编译接口观察已应用创作投影合并结果。

    参数：``engine`` 是冻结目录的可信创作编译器，``graph`` 是已应用工作流图，
    ``source`` 可覆盖标准工作流源码（Workflow Source）。返回候选编译结果；
    诊断和关闭式失败（Fail-closed）语义由调用测试断言。
    """

    return engine.compile(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=7,
        python_source=_source() if source is None else source,
        source_uri="package://lab/workflows/applied_projection.py",
        applied_graph=graph,
    )


def _persisted_read_graph(graph: dict[str, Any]) -> dict[str, Any]:
    """把候选写形状转换成 Backend/SQLite 同形的持久读取投影。

    参数：``graph`` 是编译器候选图。返回深拷贝图：节点和边带工作流 UUID 与
    数据库时间，模板与连接点（Handle）带时间，Backend ``omitempty`` 可空字段
    在值为空时省略。异常：图缺少固定五集合时由字典访问原样抛出。
    """

    projected = deepcopy(graph)
    projected["workflow"]["create_time"] = CREATE_TIME
    projected["workflow"]["update_time"] = UPDATE_TIME
    for collection_name in ("nodes", "edges"):
        # ``entity`` 是一个已持久工作流节点或边的只读投影事实。
        for entity in projected[collection_name]:
            entity["workflow_uuid"] = WORKFLOW_UUID
            entity["create_time"] = CREATE_TIME
            entity["update_time"] = UPDATE_TIME
    for collection_name in ("node_templates", "handle_templates"):
        # ``catalog_entity`` 是当前已应用图冻结的目录读取投影。
        for catalog_entity in projected[collection_name]:
            catalog_entity["create_time"] = CREATE_TIME
            catalog_entity["update_time"] = UPDATE_TIME

    node_nullable = {
        "parent_uuid",
        "material_uuid",
        "icon",
        "footer",
        "action_type",
        "script",
        "description",
    }
    template_nullable = {
        "description",
        "class",
        "schema",
        "icon",
        "header",
        "footer",
    }
    handle_nullable = {"description", "data_source", "data_key"}
    for node in projected["nodes"]:
        for field_name in node_nullable:
            if node.get(field_name) is None:
                node.pop(field_name, None)
    for edge in projected["edges"]:
        if edge.get("description") is None:
            edge.pop("description", None)
    for template in projected["node_templates"]:
        for field_name in template_nullable:
            if template.get(field_name) is None:
                template.pop(field_name, None)
    for handle in projected["handle_templates"]:
        for field_name in handle_nullable:
            if handle.get(field_name) is None:
                handle.pop(field_name, None)
    return projected


def _persisted_standard_graph() -> tuple[WorkflowAuthoringEngine, dict[str, Any], str]:
    """构造两节点一边的真实持久读取投影固定向量。

    参数：无。返回可信创作编译器、持久读取图和规范工作流源码三元组；首次编译
    失败时用断言暴露既有基线回归。
    """

    engine = _engine()
    compiled = _compile(engine, graph=_applied_graph())
    assert compiled.valid and compiled.graph is not None, compiled.diagnostics
    assert compiled.normalized_python_source is not None
    return (
        engine,
        _persisted_read_graph(compiled.graph),
        compiled.normalized_python_source,
    )


def _diagnostic_code(result: Any) -> str:
    """读取失败候选的唯一稳定诊断码。

    参数：``result`` 是候选编译结果。返回首个诊断 ``code``；若结果意外成功或
    诊断缺失，断言立即失败而不猜测错误类别。
    """

    assert not result.valid
    assert len(result.diagnostics) == 1
    return str(result.diagnostics[0]["code"])


def _single_prepare_source() -> str:
    """生成仅保留准备动作的已应用工作流源码。

    参数：无。返回标准源码删除分析动作后的确定文本；文本锚点不匹配时抛出
    ``AssertionError``，防止夹具随源码静默漂移。
    """

    removed = f"""    # unilab:node_uuid={ANALYZE_NODE_UUID}
    analyzed = reactor.analyze(prepared=prepared.prepared, label=mode)
    return workflow_output(sample=prepared.prepared, report=analyzed.report)
"""
    source = _source()
    assert removed in source
    return source.replace(
        removed, "    return workflow_output(sample=prepared.prepared)\n"
    )


def test_retained_nodes_and_edges_preserve_the_exact_persisted_read_shape() -> None:
    """保留节点和边必须精确保留工作流 UUID、数据库时间与可空省略形状。

    参数：无。返回：无；通过公共编译接口证明规范源码回编译后的节点和边等于
    已应用读投影，同时候选保持有效。
    """

    engine, applied_graph, normalized_source = _persisted_standard_graph()

    result = _compile(engine, graph=applied_graph, source=normalized_source)

    assert result.valid and result.graph is not None, result.diagnostics
    assert result.graph["nodes"] == applied_graph["nodes"]
    assert result.graph["edges"] == applied_graph["edges"]


def test_retained_group_preserves_omitted_nullable_action_name() -> None:
    """展示分组节点必须保留 Backend 省略的空动作名读形状。

    参数：无。返回：无；先编译真实展示分组节点（Presentation Group Node），
    再模拟 Backend ``omitempty`` 省略 ``action_name`` 后回编译，断言候选达到同一
    wire 固定点，避免浏览器校验误报 ``round_trip_mismatch``。
    """

    engine = _group_engine()
    compiled = _compile(engine, graph=_applied_graph(), source=_group_source())
    assert compiled.valid and compiled.graph is not None, compiled.diagnostics
    assert compiled.normalized_python_source is not None
    applied_graph = _persisted_read_graph(compiled.graph)
    # ``group_node`` 是 Backend 会省略空动作名的展示分组读投影。
    group_node = next(
        node for node in applied_graph["nodes"] if node["uuid"] == GROUP_A_NODE_UUID
    )
    assert group_node.pop("action_name") is None

    result = _compile(
        engine,
        graph=applied_graph,
        source=compiled.normalized_python_source,
    )

    assert result.valid and result.graph is not None, result.diagnostics
    retained_group = next(
        node for node in result.graph["nodes"] if node["uuid"] == GROUP_A_NODE_UUID
    )
    assert "action_name" not in retained_group


@pytest.mark.parametrize(
    ("field_name", "field_value"),
    [
        pytest.param("pose", {"position": {"x": 236, "y": 16}}, id="pose"),
        pytest.param(
            "execution_policy", {"priority": 7, "queue": "manual"}, id="policy"
        ),
        pytest.param("disabled", False, id="disabled"),
        pytest.param("minimized", True, id="minimized"),
    ],
)
def test_retained_node_preserves_explicit_graph_owned_field(
    field_name: str,
    field_value: Any,
) -> None:
    """同 UUID 节点必须精确保留显式图拥有字段并与候选深拷贝隔离。

    参数：``field_name`` 是图拥有字段名，``field_value`` 是显式值。返回：无；
    断言位置、执行策略、禁用和最小化形状不被源码默认值覆盖，候选修改也不污染
    已应用工作流图（Workflow Graph）。
    """

    engine, applied_graph, normalized_source = _persisted_standard_graph()
    # ``applied_node`` 是同 UUID 节点的已应用图权威形状。
    applied_node = next(
        node for node in applied_graph["nodes"] if node["uuid"] == PREPARE_NODE_UUID
    )
    applied_node[field_name] = deepcopy(field_value)
    applied_node["future_canvas_state"] = {"must_not": "survive"}

    result = _compile(engine, graph=applied_graph, source=normalized_source)

    assert result.valid and result.graph is not None, result.diagnostics
    # ``candidate_node`` 是合并后的独立候选节点，不得共享嵌套容器。
    candidate_node = next(
        node for node in result.graph["nodes"] if node["uuid"] == PREPARE_NODE_UUID
    )
    assert candidate_node[field_name] == field_value
    assert "future_canvas_state" not in candidate_node
    if isinstance(field_value, dict):
        candidate_node[field_name]["candidate_only"] = True
        assert "candidate_only" not in applied_node[field_name]


@pytest.mark.parametrize(
    "field_name", ["pose", "execution_policy", "disabled", "minimized"]
)
def test_retained_node_preserves_missing_graph_owned_field_shape(
    field_name: str,
) -> None:
    """已应用图缺失的图拥有字段不得被编译器默认值补造。

    参数：``field_name`` 选择一种图拥有字段。返回：无；断言缺失与显式空值或
    ``false`` 仍是不同 wire 形状。
    """

    engine, applied_graph, normalized_source = _persisted_standard_graph()
    # ``applied_node`` 是被刻意删除一个字段的已应用节点读投影。
    applied_node = next(
        node for node in applied_graph["nodes"] if node["uuid"] == PREPARE_NODE_UUID
    )
    applied_node.pop(field_name)

    result = _compile(engine, graph=applied_graph, source=normalized_source)

    assert result.valid and result.graph is not None, result.diagnostics
    retained_node = next(
        node for node in result.graph["nodes"] if node["uuid"] == PREPARE_NODE_UUID
    )
    assert field_name not in retained_node


def test_graph_owned_fields_do_not_cross_node_identity_or_preserve_unknowns() -> None:
    """图拥有字段只按稳定节点身份保留，未知字段不得进入候选。

    参数：无。返回：无；把准备节点改成新 UUID 后，断言旧节点的位置和未知字段
    均不转移到新节点，从而同时固定新节点不继承与跨身份不传递语义。
    """

    engine, applied_graph, normalized_source = _persisted_standard_graph()
    old_node = next(
        node for node in applied_graph["nodes"] if node["uuid"] == PREPARE_NODE_UUID
    )
    old_node["pose"] = {"position": {"x": 236, "y": 16}}
    old_node["future_canvas_state"] = {"must_not": "survive"}
    new_node_uuid = "20000000-0000-4000-8000-000000000099"
    changed_source = normalized_source.replace(PREPARE_NODE_UUID, new_node_uuid)

    result = _compile(engine, graph=applied_graph, source=changed_source)

    assert result.valid and result.graph is not None, result.diagnostics
    new_node = next(
        node for node in result.graph["nodes"] if node["uuid"] == new_node_uuid
    )
    assert new_node["pose"] == {}
    assert "future_canvas_state" not in new_node


def test_retained_catalog_uses_applied_projection_and_new_catalog_uses_current() -> (
    None
):
    """保留目录必须沿用已应用读形状，新节点目录必须来自当前目录快照。

    参数：无。返回：无；断言准备模板/连接点（Handle）精确保留，新分析模板不
    伪造已应用时间，且新节点和新边也没有数据库操作字段。
    """

    engine = _engine()
    first = _compile(engine, graph=_applied_graph(), source=_single_prepare_source())
    assert first.valid and first.graph is not None, first.diagnostics
    applied_graph = _persisted_read_graph(first.graph)

    result = _compile(engine, graph=applied_graph)

    assert result.valid and result.graph is not None, result.diagnostics
    retained_template = next(
        item
        for item in result.graph["node_templates"]
        if item["uuid"] == PREPARE_TEMPLATE_UUID
    )
    assert retained_template == applied_graph["node_templates"][0]
    assert {
        item["uuid"]: item
        for item in result.graph["handle_templates"]
        if item["workflow_node_template_uuid"] == PREPARE_TEMPLATE_UUID
    } == {item["uuid"]: item for item in applied_graph["handle_templates"]}
    new_template = next(
        item
        for item in result.graph["node_templates"]
        if item["uuid"] == ANALYZE_TEMPLATE_UUID
    )
    assert "create_time" not in new_template and "update_time" not in new_template
    new_node = next(
        item for item in result.graph["nodes"] if item["uuid"] == ANALYZE_NODE_UUID
    )
    new_edge = result.graph["edges"][0]
    for entity in (new_node, new_edge):
        assert "workflow_uuid" not in entity
        assert "create_time" not in entity
        assert "update_time" not in entity


def test_retained_template_without_handles_remains_a_complete_generation() -> None:
    """零连接点（Handle）的保留模板必须合法且不能补造当前连接点。

    参数：无。返回：无；断言无参数动作的模板和节点在持久读投影回编译后精确
    保留，连接点集合继续为空。
    """

    template, handles = _template(
        "30000000-0000-4000-8000-000000000090",
        name="noop",
        handles=[],
    )
    engine = WorkflowAuthoringEngine(
        catalog=AuthoringCatalogSnapshot.from_entities([template], handles)
    )
    source = f'''from lab.devices import Reactor
from unilabos.workflow.authoring import device, workflow, workflow_output

reactor: Reactor = device()

@workflow(workflow_uuid="{WORKFLOW_UUID}", displayname="No handles")
def no_handles():
    # unilab:node_uuid=20000000-0000-4000-8000-000000000090
    completed = reactor.noop()
    return workflow_output()
'''
    compiled = _compile(engine, graph=_applied_graph(), source=source)
    assert compiled.valid and compiled.graph is not None, compiled.diagnostics
    applied_graph = _persisted_read_graph(compiled.graph)

    result = _compile(
        engine,
        graph=applied_graph,
        source=str(compiled.normalized_python_source),
    )

    assert result.valid and result.graph is not None, result.diagnostics
    assert result.graph == applied_graph
    assert result.graph["handle_templates"] == []


def test_catalog_projection_accepts_browser_json_number_lexical_collapse() -> None:
    """浏览器把 ``300.0`` 重编码为 ``300`` 时目录语义必须保持等价。

    参数：无。返回：无；构造当前目录浮点默认值和浏览器往返后的整数读投影，
    断言公共编译仍达到固定点，同时保留已应用 wire 形状。布尔值、字段集合和
    其他目录语义仍由既有漂移用例严格关闭。
    """

    engine, applied_graph, normalized_source = _persisted_standard_graph()
    # ``catalog`` 是测试夹具冻结的当前目录，用于构造同语义的浮点默认值代际。
    catalog = engine._catalog
    node_templates = [action.detached_template() for action in catalog.actions]
    handle_templates = [
        handle
        for action in catalog.actions
        for handle in action.detached_handles()
    ]
    current_template = next(
        item for item in node_templates if item["uuid"] == PREPARE_TEMPLATE_UUID
    )
    current_template["goal_default"] = {"timeout": 300.0}
    current_template["meta_data"] = {
        "owner": "test",
        "contract": {"timeout": {"default": 300.0}},
    }
    browser_template = next(
        item
        for item in applied_graph["node_templates"]
        if item["uuid"] == PREPARE_TEMPLATE_UUID
    )
    browser_template["goal_default"] = {"timeout": 300}
    browser_template["meta_data"] = {
        "owner": "test",
        "contract": {"timeout": {"default": 300}},
    }
    browser_engine = WorkflowAuthoringEngine(
        catalog=AuthoringCatalogSnapshot.from_entities(
            node_templates,
            handle_templates,
        )
    )

    result = _compile(
        browser_engine,
        graph=applied_graph,
        source=normalized_source,
    )

    assert result.valid and result.graph is not None, result.diagnostics
    projected_template = next(
        item
        for item in result.graph["node_templates"]
        if item["uuid"] == PREPARE_TEMPLATE_UUID
    )
    assert projected_template == browser_template


@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param("template_title", id="template-title"),
        pytest.param("handle_required", id="handle-required"),
        pytest.param("handle_direction", id="handle-direction"),
        pytest.param("missing_handle", id="missing-handle"),
        pytest.param("unexpected_nullable", id="unexpected-nullable"),
    ],
)
def test_retained_catalog_semantic_drift_returns_template_catalog_mismatch(
    mutation: str,
) -> None:
    """保留模板真实语义、连接点身份或非白名单空字段漂移必须稳定拒绝。

    参数：``mutation`` 选择标题、必填、方向、成员缺失或未知空字段漂移。返回：
    无；断言公共编译诊断为 ``template_catalog_mismatch``，不返回候选图。
    """

    engine, applied_graph, normalized_source = _persisted_standard_graph()
    if mutation == "template_title":
        applied_graph["node_templates"][0]["display_name"] = "过期标题"
    elif mutation == "handle_required":
        target = next(
            item
            for item in applied_graph["handle_templates"]
            if item["uuid"] == PREPARE_SAMPLE_TARGET
        )
        target["required"] = not target["required"]
    elif mutation == "handle_direction":
        applied_graph["handle_templates"][0]["io_type"] = "source"
    elif mutation == "missing_handle":
        applied_graph["handle_templates"].pop()
    else:
        applied_graph["node_templates"][0]["future_nullable"] = None

    result = _compile(engine, graph=applied_graph, source=normalized_source)

    assert _diagnostic_code(result) == "template_catalog_mismatch"
    if mutation == "template_title":
        # ``diagnostic`` 必须定位到具体模板和漂移字段，不能只给出泛化目录错误。
        diagnostic = result.diagnostics[0]
        assert PREPARE_TEMPLATE_UUID in diagnostic["message"]
        assert "display_name" in diagnostic["message"]
    assert result.graph is None


@pytest.mark.parametrize(
    ("collection_name", "source_index"),
    [
        pytest.param("nodes", 0, id="node"),
        pytest.param("edges", 0, id="edge"),
        pytest.param("node_templates", 0, id="node-template"),
        pytest.param("handle_templates", 0, id="handle-template"),
    ],
)
def test_duplicate_applied_projection_uuid_fails_closed(
    collection_name: str,
    source_index: int,
) -> None:
    """已应用节点、边、模板或连接点（Handle）的重复 UUID 必须关闭式失败。

    参数：``collection_name`` 选择五集合成员，``source_index`` 选择复制实体。返回：
    无；断言诊断为 ``candidate_invalid``，不允许后写覆盖先写。
    """

    engine, applied_graph, normalized_source = _persisted_standard_graph()
    applied_graph[collection_name].append(
        deepcopy(applied_graph[collection_name][source_index])
    )

    result = _compile(engine, graph=applied_graph, source=normalized_source)

    assert _diagnostic_code(result) == "candidate_invalid"


@pytest.mark.parametrize("collection_name", ["nodes", "edges"])
def test_foreign_workflow_projection_fails_closed(collection_name: str) -> None:
    """已应用节点或边声明其他工作流身份时必须关闭式失败。

    参数：``collection_name`` 选择节点或边集合。返回：无；断言外部工作流 UUID
    不能进入当前候选投影，稳定返回 ``candidate_invalid``。
    """

    engine, applied_graph, normalized_source = _persisted_standard_graph()
    applied_graph[collection_name][0]["workflow_uuid"] = (
        "10000000-0000-4000-8000-000000000099"
    )

    result = _compile(engine, graph=applied_graph, source=normalized_source)

    assert _diagnostic_code(result) == "candidate_invalid"


@pytest.mark.parametrize(
    "collection_name",
    ["nodes", "edges", "node_templates", "handle_templates"],
)
def test_invalid_database_time_in_applied_projection_fails_closed(
    collection_name: str,
) -> None:
    """已应用五集合实体中的非法数据库时间必须关闭式失败。

    参数：``collection_name`` 选择节点、边、模板或连接点（Handle）。返回：无；
    断言不可解析时间不被复制进候选，稳定返回 ``candidate_invalid``。
    """

    engine, applied_graph, normalized_source = _persisted_standard_graph()
    applied_graph[collection_name][0]["update_time"] = "不是时间"

    result = _compile(engine, graph=applied_graph, source=normalized_source)

    assert _diagnostic_code(result) == "candidate_invalid"


def test_retained_template_cannot_mix_applied_identity_with_current_handles() -> None:
    """保留模板不得混用已应用模板与当前目录连接点（Handle）。

    参数：无。返回：无；把已应用准备模板的连接点清空后，断言编译器拒绝从当前
    目录补回连接点并返回 ``template_catalog_mismatch``。
    """

    engine, applied_graph, normalized_source = _persisted_standard_graph()
    applied_graph["handle_templates"] = [
        item
        for item in applied_graph["handle_templates"]
        if item["workflow_node_template_uuid"] != PREPARE_TEMPLATE_UUID
    ]

    result = _compile(engine, graph=applied_graph, source=normalized_source)

    assert _diagnostic_code(result) == "template_catalog_mismatch"
