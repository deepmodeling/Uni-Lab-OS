"""互斥条件分支中的物料消费者运行时至多执行一个，不算物料流分叉。"""

from unilabos.workflow.authoring_engine import WorkflowAuthoringEngine
from unilabos.workflow.authoring_kernel import AuthoringCatalogSnapshot

from .f05_material_graph_fixtures import (
    SECOND_CONSUMER_NODE_UUID,
    compile_material_source_graph,
    material_graph_catalog_entities,
)
from .test_f05_material_source_authoring import PLATE_SOURCE_SYMBOL, PLATE_TEMPLATE_UUID, PREPARE_NODE_UUID, _source
from .test_structured_condition_authoring import _condition_template


def material_graph_engine() -> WorkflowAuthoringEngine:
    templates, handles = material_graph_catalog_entities()
    return WorkflowAuthoringEngine(catalog=AuthoringCatalogSnapshot.from_entities(
        [*templates, _condition_template()], handles,
        resource_template_symbols={PLATE_SOURCE_SYMBOL: PLATE_TEMPLATE_UUID},
    ))

CONDITION_NODE_UUID = "20000000-0000-4000-8000-000000000031"
THIRD_CONSUMER_NODE_UUID = "20000000-0000-4000-8000-000000000032"


def _branch_source(*, same_branch: bool) -> str:
    source = _source().replace("def material_assay():", "def material_assay(*, left: bool = True):")
    consume = "    prepared = reactor.prepare(sample=assay_plate)"
    first = f"        # unilab:node_uuid={PREPARE_NODE_UUID}\n        prepared = reactor.prepare(sample=assay_plate)\n"
    second = f"        # unilab:node_uuid={SECOND_CONSUMER_NODE_UUID}\n        other = reactor.prepare(sample=assay_plate)\n"
    third = f"        # unilab:node_uuid={THIRD_CONSUMER_NODE_UUID}\n        idle = reactor.prepare(sample=assay_plate)\n"
    if same_branch:
        body = f"    # unilab:node_uuid={CONDITION_NODE_UUID}\n    if left:\n{first}{second}    else:\n{third}"
    else:
        body = f"    # unilab:node_uuid={CONDITION_NODE_UUID}\n    if left:\n{first}    else:\n{second}"
    return source.replace(f"    # unilab:node_uuid={PREPARE_NODE_UUID}\n{consume}\n", body)


def _codes(result):
    return [(d.get("code"), d.get("message")) for d in result.diagnostics]


def test_consumers_in_different_branches_of_one_condition_compile() -> None:
    result = compile_material_source_graph(material_graph_engine(), _branch_source(same_branch=False))
    assert result.valid, _codes(result)


def test_ordered_consumers_inside_one_branch_still_compile() -> None:
    # 同一分支内两个动作按源码顺序严格有序，另一分支的第三个动作与它们互斥。
    result = compile_material_source_graph(material_graph_engine(), _branch_source(same_branch=True))
    assert result.valid, _codes(result)
