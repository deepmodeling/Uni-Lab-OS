"""工作流（Workflow）简写装饰器与节点展示注释合同测试。"""

from __future__ import annotations

import pytest

from unilabos.workflow.authoring_engine import WorkflowAuthoringEngine

from .test_authoring_engine import (
    ANALYZE_NODE_UUID,
    PREPARE_NODE_UUID,
    _compile,
    _engine,
    _source,
)


@pytest.fixture()
def authoring_engine() -> WorkflowAuthoringEngine:
    """提供只读目录快照固定的工作流创作编译器（Authoring Compiler）。

    返回值：每个测试获得独立编译器，避免候选图或目录状态相互污染。
    """

    return _engine()


def _canonical_source() -> str:
    """把既有夹具转换成使用规范 ``@workflow`` 装饰器的作者源码。

    返回值：工作流语义不变、只替换静态导入名和装饰器名的 Python 源码。
    """

    return _source()


def _legacy_source() -> str:
    """把规范夹具转换成旧 ``@workflow_definition`` 兼容源码。

    返回值：只替换工作流装饰器导入与调用，不改变 ``workflow_output`` 或其他
    工作流（Workflow）语义。
    """

    return _source().replace(
        "device, workflow, workflow_output",
        "device, workflow_definition, workflow_output",
    ).replace("@workflow(", "@workflow_definition(")


def _annotated_source() -> str:
    """构造同时覆盖规范和兼容节点展示注释的作者源码。

    返回值：第一个节点使用带冒号规范形式，第二个节点使用用户给出的无冒号
    兼容形式，以证明两者编译为同一节点展示语义。
    """

    source = _canonical_source()
    source = source.replace(
        f"    # unilab:node_uuid={PREPARE_NODE_UUID}",
        "    # [加入预混液]: PCR 中预混液的分配\n"
        f"    # unilab:node_uuid={PREPARE_NODE_UUID}",
    )
    return source.replace(
        f"    # unilab:node_uuid={ANALYZE_NODE_UUID}",
        "    # [分析产物] PCR产物的质量分析\n"
        f"    # unilab:node_uuid={ANALYZE_NODE_UUID}",
    )


def test_workflow_is_the_canonical_authoring_decorator(
    authoring_engine: WorkflowAuthoringEngine,
) -> None:
    """``@workflow`` 应编译成功，确定性源码也只能输出该规范名称。"""

    compiled = _compile(authoring_engine, _canonical_source())

    assert compiled.valid, compiled.diagnostics
    assert compiled.normalized_python_source is not None
    assert "from unilabos.workflow.authoring import device, workflow" in (
        compiled.normalized_python_source
    )
    assert "@workflow(" in compiled.normalized_python_source
    assert "workflow_definition" not in compiled.normalized_python_source


def test_legacy_workflow_definition_normalizes_to_workflow(
    authoring_engine: WorkflowAuthoringEngine,
) -> None:
    """旧 ``@workflow_definition`` 草稿应兼容读取并规范化为 ``@workflow``。"""

    compiled = _compile(authoring_engine, _legacy_source())

    assert compiled.valid, compiled.diagnostics
    assert compiled.normalized_python_source is not None
    assert "@workflow(" in compiled.normalized_python_source
    assert "workflow_definition" not in compiled.normalized_python_source


def test_node_display_comments_project_and_reach_fixed_point(
    authoring_engine: WorkflowAuthoringEngine,
) -> None:
    """节点展示注释应进入候选图并按规范形式达到双向语义固定点。"""

    compiled = _compile(authoring_engine, _annotated_source())

    assert compiled.valid, compiled.diagnostics
    assert compiled.graph is not None
    nodes = {node["uuid"]: node for node in compiled.graph["nodes"]}
    assert nodes[PREPARE_NODE_UUID]["name"] == "加入预混液"
    assert nodes[PREPARE_NODE_UUID]["description"] == "PCR 中预混液的分配"
    assert nodes[ANALYZE_NODE_UUID]["name"] == "分析产物"
    assert nodes[ANALYZE_NODE_UUID]["description"] == "PCR产物的质量分析"
    assert compiled.normalized_python_source is not None
    assert "# [加入预混液]: PCR 中预混液的分配" in (
        compiled.normalized_python_source
    )
    assert "# [分析产物]: PCR产物的质量分析" in compiled.normalized_python_source
    normalized_lines = compiled.normalized_python_source.splitlines()
    assert normalized_lines[compiled.source_map[0]["start_line"] - 1].strip() == (
        "# [加入预混液]: PCR 中预混液的分配"
    )

    repeated = _compile(
        authoring_engine,
        compiled.normalized_python_source,
        graph=compiled.graph,
    )
    assert repeated.valid, repeated.diagnostics
    assert repeated.graph == compiled.graph


@pytest.mark.parametrize(
    ("comment", "placement"),
    [
        ("# [缺少描述]", "adjacent"),
        ("# [孤立节点]: 不属于任何动作", "orphan"),
    ],
)
def test_invalid_or_orphan_node_display_comment_fails_closed(
    authoring_engine: WorkflowAuthoringEngine,
    comment: str,
    placement: str,
) -> None:
    """畸形或孤立节点展示注释必须失败关闭且不返回候选图。

    参数说明：``comment`` 是待验证注释，``placement`` 决定它紧邻锚点还是位于
    工作流末尾；返回值为空，断言公共诊断与零候选结果。
    """

    source = _canonical_source()
    if placement == "adjacent":
        source = source.replace(
            f"    # unilab:node_uuid={PREPARE_NODE_UUID}",
            f"    {comment}\n    # unilab:node_uuid={PREPARE_NODE_UUID}",
        )
    else:
        source = source.replace(
            "    return workflow_output(",
            f"    {comment}\n    return workflow_output(",
        )

    compiled = _compile(authoring_engine, source)

    assert not compiled.valid
    assert compiled.graph is None
    assert any(
        diagnostic["code"] == "invalid_node_metadata"
        for diagnostic in compiled.diagnostics
    )


def test_inline_node_display_comment_fails_instead_of_being_ignored(
    authoring_engine: WorkflowAuthoringEngine,
) -> None:
    """动作行后的节点展示注释必须明确失败，不能静默丢失覆盖语义。"""

    source = _canonical_source().replace(
        "    prepared = reactor.prepare(sample=sample, cycles=cycles)",
        "    prepared = reactor.prepare(sample=sample, cycles=cycles)"
        "  # [加入预混液]: PCR 中预混液的分配",
    )

    compiled = _compile(authoring_engine, source)

    assert not compiled.valid
    assert compiled.graph is None
    assert any(
        diagnostic["code"] == "invalid_node_metadata"
        for diagnostic in compiled.diagnostics
    )
