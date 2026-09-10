"""F05.2 资源模板兼容（ResourceTemplate Compatibility）公共合同。"""

from __future__ import annotations

from pathlib import Path

import pytest

from unilabos.workflow.service import WorkflowError

from .f05_material_graph_fixtures import (
    INCOMPATIBLE_TEMPLATE_UUID,
    PASSTHROUGH_SOURCE_UUID,
    PASSTHROUGH_TEMPLATE_UUID,
    candidate_with_prepare_allowlist,
    compile_material_source_graph,
    material_graph_engine,
    opened_material_graph_store,
    passthrough_chain_source,
    single_chain_source,
)
from .test_authoring_engine import WORKFLOW_UUID
from .test_f05_material_source_authoring import PLATE_TEMPLATE_UUID


def test_consumer_without_allowlist_accepts_material_source_guarantee() -> None:
    """省略允许集合的消费者应接受物料来源（MaterialSource）的精确保证。

    参数：无。返回：无；断言编译成功。异常：公共编译接缝不得泄漏内部异常。
    """

    result = compile_material_source_graph(
        material_graph_engine(),
        single_chain_source(),
    )

    assert result.valid, result.diagnostics


def test_consumer_allowlist_containing_source_template_is_compatible() -> None:
    """包含生产者资源模板的消费者允许集合应通过编译。

    参数：无。返回：无；断言物料来源精确模板保留且候选有效。异常：公共编译
    接缝不得泄漏内部异常。
    """

    result = compile_material_source_graph(
        material_graph_engine(prepare_allowlist=(PLATE_TEMPLATE_UUID,)),
        single_chain_source(),
    )

    assert result.valid and result.graph is not None, result.diagnostics
    material_source = next(
        node for node in result.graph["nodes"] if node["type"] == "material_source"
    )
    assert material_source["param"]["resource_template_uuid"] == PLATE_TEMPLATE_UUID


def test_compile_rejects_consumer_excluding_source_template() -> None:
    """源码编译必须以稳定诊断拒绝排除物料来源模板的消费者。

    参数：无。返回：无；断言 ``material_template_mismatch`` 且没有候选图或
    规范源码。异常：公共编译接缝不得泄漏内部异常。
    """

    result = compile_material_source_graph(
        material_graph_engine(
            prepare_allowlist=(INCOMPATIBLE_TEMPLATE_UUID,),
        ),
        single_chain_source(),
    )

    assert not result.valid
    assert result.graph is None
    assert result.normalized_python_source is None
    assert [item["code"] for item in result.diagnostics] == [
        "material_template_mismatch"
    ]


@pytest.mark.parametrize("public_seam", ("generate_python", "validate"))
def test_graph_public_seams_reject_incompatible_material_template(
    public_seam: str,
) -> None:
    """图公共接缝必须返回同一资源模板不兼容诊断。

    参数说明：``public_seam`` 选择源码生成或图/源码共同验证。返回：无；两个
    接缝都不得泄漏候选图或规范源码。
    """

    engine = material_graph_engine()
    compiled = compile_material_source_graph(engine, single_chain_source())
    assert compiled.valid and compiled.graph is not None
    graph = candidate_with_prepare_allowlist(
        compiled.graph,
        (INCOMPATIBLE_TEMPLATE_UUID,),
    )
    if public_seam == "generate_python":
        result = engine.generate_python(
            workflow_uuid=WORKFLOW_UUID,
            workflow_revision=7,
            graph=graph,
            source_uri="package://lab/workflows/f05_material_template.py",
        )
    else:
        result = engine.validate(
            workflow_uuid=WORKFLOW_UUID,
            workflow_revision=7,
            graph=graph,
            python_source=single_chain_source(),
            source_uri="package://lab/workflows/f05_material_template.py",
        )

    assert not result.valid
    assert result.graph is None
    assert result.normalized_python_source is None
    assert [item["code"] for item in result.diagnostics] == [
        "material_template_mismatch"
    ]


def test_direct_save_rejects_template_mismatch_without_revision_write(
    tmp_path: Path,
) -> None:
    """直接保存资源模板不兼容图必须回滚图和修订。

    参数说明：``tmp_path`` 提供隔离 SQLite 文件。局部 ``context`` 是带不兼容
    消费目录的真实写模型，``before`` 是保存前权威图。返回：无。
    """

    compiled = compile_material_source_graph(
        material_graph_engine(),
        single_chain_source(),
    )
    assert compiled.valid and compiled.graph is not None
    with opened_material_graph_store(
        tmp_path / "workflow.db",
        prepare_allowlist=(INCOMPATIBLE_TEMPLATE_UUID,),
    ) as context:
        before = context.service.get_graph(WORKFLOW_UUID)
        with pytest.raises(WorkflowError) as caught:
            context.service.save_graph(
                WORKFLOW_UUID,
                revision=1,
                nodes=compiled.graph["nodes"],
                edges=compiled.graph["edges"],
            )

        assert caught.value.code == "material_template_mismatch"
        assert context.service.get_graph(WORKFLOW_UUID) == before
        assert before == context.applied_graph
        assert before["workflow"]["revision"] == 1


def test_explicit_action_output_schema_not_device_template_proves_compatibility() -> (
    None
):
    """普通动作输出保证必须来自输出连接点而不是设备资源模板。

    参数：无。返回：无；断言设备资源模板不同仍可由输出 Schema 证明兼容。
    异常：公共编译接缝不得泄漏内部异常。
    """

    engine = material_graph_engine(
        include_passthrough=True,
        prepare_allowlist=(PLATE_TEMPLATE_UUID,),
        passthrough_input_allowlist=(PLATE_TEMPLATE_UUID,),
        passthrough_output_allowlist=(PLATE_TEMPLATE_UUID,),
        passthrough_implicit=False,
    )
    result = compile_material_source_graph(engine, passthrough_chain_source())

    assert result.valid and result.graph is not None, result.diagnostics
    action_template = next(
        template
        for template in result.graph["node_templates"]
        if template["uuid"] == PASSTHROUGH_TEMPLATE_UUID
    )
    assert action_template["resource_template_uuid"] != PLATE_TEMPLATE_UUID


def test_implicit_same_name_output_inherits_input_schema_guarantee() -> None:
    """隐式同名输出应沿用输入物料占位符（ResourceSlot）的模板保证。

    参数：无。返回：无；断言输出无需复制允许集合仍可沿用输入保证。异常：公共
    编译接缝不得泄漏内部异常。
    """

    engine = material_graph_engine(
        include_passthrough=True,
        prepare_allowlist=(PLATE_TEMPLATE_UUID,),
        passthrough_input_allowlist=(PLATE_TEMPLATE_UUID,),
        passthrough_output_allowlist=None,
        passthrough_implicit=True,
    )
    result = compile_material_source_graph(engine, passthrough_chain_source())

    assert result.valid and result.graph is not None, result.diagnostics
    output = next(
        handle
        for handle in result.graph["handle_templates"]
        if handle["uuid"] == PASSTHROUGH_SOURCE_UUID
    )
    assert output["meta_data"]["unilab"].get("allowed_resource_template_uuids") is None
