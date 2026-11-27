"""源码组合展开把子实验操作的数量需求带进父图：直接调用、父输入透传与两层嵌套。"""

from unilabos.workflow.authoring_identity import expanded_node_uuid

from .test_c1_r2_static_expansion_contract import (
    CHILD_NODE_UUID,
    CHILD_WORKFLOW_UUID,
    EXPANDED_CHILD_NODE_UUID,
    INVOCATION_UUID,
    LEAF_NODE_UUID,
    LEAF_WORKFLOW_UUID,
    PARENT_WORKFLOW_UUID,
    _nested_world,
    _world,
)

REQUIREMENT_UUID = "66666666-6666-4666-8666-666666666666"


def _requirement(consume_node_uuid: str) -> dict:
    return {
        "uuid": REQUIREMENT_UUID,
        "consume_node_uuid": consume_node_uuid,
        "requirement_key": "solvent",
        "target_type": "current_substance",
        "reagent_info_uuid": None,
        "required_quantity": 1.0,
        "quantity_unit": "mL",
        "allow_split": False,
        "meta_data": {"unilab": {
            "quantity_target": "container_content",
            "quantity_binding": {"kind": "workflow_input", "parameter": "value"},
            "quantity_scale": 2.0,
        }},
    }


def _compile(authoring, value):
    return authoring.compile_invocation(
        parent_workflow_uuid=PARENT_WORKFLOW_UUID,
        invocation_uuid=INVOCATION_UUID,
        module="c1_published_lab.workflows.child",
        symbol="prepare_sample",
        keyword_arguments={"value": value},
    )


def test_child_requirement_is_carried_with_expanded_consumer_and_literal_quantity() -> None:
    authoring, provider = _world()
    provider.snapshots[CHILD_WORKFLOW_UUID]["inventory_requirements"] = [_requirement(CHILD_NODE_UUID)]
    expansion = _compile(authoring, 7.5)
    assert expansion.diagnostics == ()
    (carried,) = expansion.inventory_requirements
    assert carried["consume_node_uuid"] == EXPANDED_CHILD_NODE_UUID
    assert carried["required_quantity"] == 15.0
    assert carried["meta_data"]["unilab"]["quantity_binding"] == {"kind": "literal", "value": 7.5}
    assert carried["requirement_key"] == f"{INVOCATION_UUID}/solvent"


def test_parent_input_argument_keeps_quantity_bound_to_parent_input() -> None:
    authoring, provider = _world()
    provider.snapshots[CHILD_WORKFLOW_UUID]["inventory_requirements"] = [_requirement(CHILD_NODE_UUID)]
    expansion = _compile(authoring, {"kind": "workflow_input", "parameter": "outer_value"})
    assert expansion.diagnostics == ()
    (carried,) = expansion.inventory_requirements
    assert carried["meta_data"]["unilab"]["quantity_binding"] == {"kind": "workflow_input", "parameter": "outer_value"}


def test_runtime_quantity_argument_is_rejected_instead_of_dropped() -> None:
    authoring, provider = _world()
    provider.snapshots[CHILD_WORKFLOW_UUID]["inventory_requirements"] = [_requirement(CHILD_NODE_UUID)]
    expansion = _compile(authoring, {"kind": "node_output", "workflow_node_uuid": CHILD_NODE_UUID, "source_handle_uuid": "x"})
    assert expansion.inventory_requirements == ()
    assert expansion.diagnostics


def test_nested_leaf_requirement_reaches_the_outermost_parent() -> None:
    authoring, provider = _nested_world()
    provider.snapshots[LEAF_WORKFLOW_UUID]["inventory_requirements"] = [_requirement(LEAF_NODE_UUID)]
    expansion = _compile(authoring, 3)
    assert expansion.diagnostics == ()
    nested_invocation = expanded_node_uuid(INVOCATION_UUID, CHILD_NODE_UUID)
    (carried,) = expansion.inventory_requirements
    assert carried["consume_node_uuid"] == expanded_node_uuid(nested_invocation, LEAF_NODE_UUID)
    assert carried["required_quantity"] == 6.0
    assert carried["requirement_key"] == f"{INVOCATION_UUID}/{nested_invocation}/solvent"
