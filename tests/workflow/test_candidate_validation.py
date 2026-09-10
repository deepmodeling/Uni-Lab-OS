"""工作流创作候选 bundle 公共校验边界测试。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from unilabos.workflow.candidate_validation import (
    CandidateBundleError,
    validate_candidate_bundle,
)

from .test_authoring_engine import (
    WORKFLOW_UUID,
    _applied_graph,
    _compile,
    _engine,
)


def _valid_bundle() -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    """构造一个由真实编译器产生的有效候选 bundle。

    返回值：依次为候选图、基线图、源码映射和变更集，所有容器均可由测试独立
    修改，不污染其他用例。
    """

    base_graph = _applied_graph()
    compilation = _compile(_engine(), graph=base_graph)
    assert compilation.valid and compilation.graph is not None
    assert isinstance(compilation.source_map, list)
    assert isinstance(compilation.changeset, dict)
    return (
        deepcopy(compilation.graph),
        deepcopy(base_graph),
        deepcopy(compilation.source_map),
        deepcopy(compilation.changeset),
    )


def test_valid_compiler_bundle_crosses_the_public_boundary() -> None:
    """真实编译结果应通过候选图、源码映射和变更集共同校验。"""

    graph, base_graph, source_map, changeset = _valid_bundle()

    validated = validate_candidate_bundle(
        graph=graph,
        base_graph=base_graph,
        workflow_uuid=WORKFLOW_UUID,
        revision=7,
        source_map=source_map,
        changeset=changeset,
    )

    assert validated == graph


def test_forged_changeset_fails_closed() -> None:
    """伪造的变更集不能借用真实候选图越过服务边界。"""

    graph, base_graph, source_map, changeset = _valid_bundle()
    changeset["created_node_uuids"] = []

    with pytest.raises(CandidateBundleError):
        validate_candidate_bundle(
            graph=graph,
            base_graph=base_graph,
            workflow_uuid=WORKFLOW_UUID,
            revision=7,
            source_map=source_map,
            changeset=changeset,
        )


def test_source_map_cannot_reference_foreign_node() -> None:
    """源码映射不得指向候选图之外的工作流节点。"""

    graph, base_graph, source_map, changeset = _valid_bundle()
    source_map[0]["workflow_node_uuid"] = (
        "20000000-0000-4000-8000-000000000099"
    )

    with pytest.raises(CandidateBundleError):
        validate_candidate_bundle(
            graph=graph,
            base_graph=base_graph,
            workflow_uuid=WORKFLOW_UUID,
            revision=7,
            source_map=source_map,
            changeset=changeset,
        )
