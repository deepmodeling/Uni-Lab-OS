"""工作流源码资源模板（ResourceTemplate）注解的创作固定点。"""

from __future__ import annotations

from unilabos.workflow.authoring_engine import WorkflowAuthoringEngine
from unilabos.workflow.authoring_kernel import AuthoringCatalogSnapshot

from .test_authoring_engine import (
    PREPARE_SAMPLE_TARGET,
    PREPARE_TEMPLATE_UUID,
    WORKFLOW_UUID,
    _applied_graph,
    _handle,
    _template,
)
from .test_f05_material_source_authoring import (
    PLATE_SOURCE_SYMBOL,
    PLATE_TEMPLATE_UUID,
)

ACTION_NODE_UUID = "63000000-0000-4000-8000-000000000011"


def _engine() -> WorkflowAuthoringEngine:
    """构造输入只接受板类物料的隔离创作编译器。"""

    template, handles = _template(
        PREPARE_TEMPLATE_UUID,
        name="prepare",
        handles=[
            _handle(
                PREPARE_SAMPLE_TARGET,
                node_template_uuid=PREPARE_TEMPLATE_UUID,
                key="sample",
                io_type="target",
                value_type="ResourceSlot",
                required=True,
            )
        ],
    )
    handles[0]["meta_data"]["unilab"]["value_schema"] = {
        "$slot": "ResourceSlot",
        "allowed_resource_template_uuids": [PLATE_TEMPLATE_UUID],
    }
    catalog = AuthoringCatalogSnapshot.from_entities(
        [template],
        handles,
        resource_template_symbols={PLATE_SOURCE_SYMBOL: PLATE_TEMPLATE_UUID},
    )
    return WorkflowAuthoringEngine(catalog=catalog)


def _source() -> str:
    """返回带静态资源模板允许集合的工作流输入源码。"""

    return f'''from typing import Annotated

from lab.devices import Reactor
from lab.resources import plate_96
from unilabos.registry.annotations import AllowedResourceTemplates
from unilabos.registry.placeholder_type import ResourceSlot
from unilabos.workflow.authoring import device, workflow, workflow_output


reactor: Reactor = device()


@workflow(workflow_uuid="{WORKFLOW_UUID}", displayname="Typed material input")
def typed_material_input(
    *,
    sample: Annotated[
        ResourceSlot,
        AllowedResourceTemplates(plate_96),
    ],
):
    # unilab:node_uuid={ACTION_NODE_UUID}
    prepared = reactor.prepare(sample=sample)
    return workflow_output()
'''


def test_workflow_resource_template_annotation_is_a_canonical_fixed_point() -> None:
    """源码注解须冻结为本地模板 UUID，并可规范生成后再次编译。"""

    engine = _engine()
    compiled = engine.compile(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=7,
        python_source=_source(),
        source_uri="package://lab/workflows/typed-material-input.py",
        applied_graph=_applied_graph(),
    )

    assert compiled.valid and compiled.graph is not None, compiled.diagnostics
    parameter = compiled.graph["workflow"]["meta_data"]["unilab"][
        "input_contract"
    ]["parameters"][0]
    assert parameter["schema"] == {
        "$slot": "ResourceSlot",
        "allowed_resource_template_uuids": [PLATE_TEMPLATE_UUID],
    }
    normalized = compiled.normalized_python_source
    assert normalized is not None
    assert "AllowedResourceTemplates(plate_96)" in normalized

    repeated = engine.compile(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=7,
        python_source=normalized,
        source_uri="package://lab/workflows/typed-material-input.py",
        applied_graph=compiled.graph,
    )

    assert repeated.valid and repeated.graph == compiled.graph, repeated.diagnostics
    assert repeated.normalized_python_source == normalized
