"""组合调用展开携带子工作流数量需求：改写节点身份、按调用实参重绑用量、关闭式拒绝运行时值。"""

import pytest

from unilabos.workflow.composite_inventory_requirements import (
    CompositeInventoryRequirementError,
    expand_child_inventory_requirements,
)

INVOCATION = "11111111-1111-4111-8111-111111111111"
OUTER = "22222222-2222-4222-8222-222222222222"
CHILD = "33333333-3333-4333-8333-333333333333"
NODE_MAP = {"fill": "fill-expanded", "bottle": "bottle-expanded"}


def requirement(binding, *, quantity=20.0, scale=3.0, key="solvent_1"):
    return {
        "uuid": "44444444-4444-4444-8444-444444444444",
        "consume_node_uuid": "fill",
        "requirement_key": key,
        "target_type": "current_substance",
        "reagent_info_uuid": None,
        "required_quantity": quantity,
        "quantity_unit": "mL",
        "allow_split": False,
        "meta_data": {"unilab": {
            "material_source_node_uuid": "bottle",
            "quantity_target": "container_content",
            "quantity_binding": binding,
            "quantity_scale": scale,
        }},
    }


def expand(requirements, arguments, **kwargs):
    return expand_child_inventory_requirements(
        requirements, invocation_uuid=INVOCATION, child_workflow_uuid=CHILD,
        node_uuid_map=NODE_MAP, arguments=arguments, **kwargs,
    )


def test_literal_argument_rebinds_quantity_and_remaps_nodes():
    (item,) = expand([requirement({"kind": "workflow_input", "parameter": "volume_ml"})], {"volume_ml": 5})
    unilab = item["meta_data"]["unilab"]
    assert item["consume_node_uuid"] == "fill-expanded"
    assert unilab["material_source_node_uuid"] == "bottle-expanded"
    assert unilab["quantity_binding"] == {"kind": "literal", "value": 5}
    assert item["required_quantity"] == 15.0
    assert item["requirement_key"] == f"{INVOCATION}/solvent_1"
    assert item["uuid"] != requirement({})["uuid"]
    assert unilab["composite_origin"][0]["child_workflow_uuid"] == CHILD


def test_parent_input_argument_stays_a_workflow_input_of_the_parent():
    (item,) = expand([requirement({"kind": "workflow_input", "parameter": "volume_ml"})],
                     {"volume_ml": {"kind": "workflow_input", "parameter": "develop_volume"}})
    assert item["meta_data"]["unilab"]["quantity_binding"] == {"kind": "workflow_input", "parameter": "develop_volume"}
    assert item["required_quantity"] == 20.0


def test_literal_requirement_passes_through_unchanged_quantity():
    (item,) = expand([requirement({"kind": "literal", "value": 2})], {})
    assert item["meta_data"]["unilab"]["quantity_binding"] == {"kind": "literal", "value": 2}


def test_zero_quantity_call_needs_no_inventory():
    assert expand([requirement({"kind": "workflow_input", "parameter": "volume_ml"})], {"volume_ml": 0}) == ()


@pytest.mark.parametrize("argument", [
    {"kind": "node_output", "workflow_node_uuid": "x", "source_handle_uuid": "y"},
    {"uuid": "55555555-5555-4555-8555-555555555555"},
    "twenty",
])
def test_runtime_or_non_numeric_quantity_fails_closed(argument):
    with pytest.raises(CompositeInventoryRequirementError) as caught:
        expand([requirement({"kind": "workflow_input", "parameter": "volume_ml"})], {"volume_ml": argument})
    assert caught.value.code == "composite_quantity_binding_unsupported"


def test_unmapped_consumer_fails_closed():
    broken = requirement({"kind": "literal", "value": 1})
    broken["consume_node_uuid"] = "missing"
    with pytest.raises(CompositeInventoryRequirementError) as caught:
        expand([broken], {})
    assert caught.value.code == "composite_quantity_requirement_unmapped"


def test_nested_expansion_composes_namespaces_and_rebinds_again():
    (inner,) = expand([requirement({"kind": "workflow_input", "parameter": "volume_ml"})],
                      {"volume_ml": {"kind": "workflow_input", "parameter": "middle_volume"}})
    (outer,) = expand_child_inventory_requirements(
        [], nested_requirements=[inner], invocation_uuid=OUTER, child_workflow_uuid="middle",
        node_uuid_map={}, arguments={"middle_volume": 7},
    )
    assert outer["requirement_key"] == f"{OUTER}/{INVOCATION}/solvent_1"
    assert outer["consume_node_uuid"] == "fill-expanded"
    assert outer["required_quantity"] == 21.0
    assert [item["invocation_uuid"] for item in outer["meta_data"]["unilab"]["composite_origin"]] == [OUTER, INVOCATION]
