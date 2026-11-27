"""嵌套 child 的默认参数只能被一种父来源替换。"""

from copy import deepcopy

import pytest

from tests.workflow.test_authoring_engine import _handle, _template
from unilabos.workflow.authoring_kernel import AuthoringCatalogSnapshot
from unilabos.workflow.composite_expansion import _materialize_boundary_arguments
from unilabos.workflow.composite_invocation import (
    _materialize_published_arguments,
    _published_boundary_handle_uuid,
)

NODE = "20000000-0000-4000-8000-000000000011"
TEMPLATE = "30000000-0000-4000-8000-000000000011"
HANDLE = "40000000-0000-4000-8000-000000000011"
BOUNDARY_TEMPLATE = "30000000-0000-4000-8000-000000000012"
BOUNDARY = _published_boundary_handle_uuid(BOUNDARY_TEMPLATE, "slot_id")


@pytest.mark.parametrize("path", ["authoring", "published"])
def test_parent_input_replaces_frozen_child_default_without_two_providers(path):
    handle = _handle(
        HANDLE,
        node_template_uuid=TEMPLATE,
        key="slot_id",
        io_type="target",
        value_type="integer",
        required=True,
    )
    template, handles = _template(TEMPLATE, name="move", handles=[handle])
    catalog = AuthoringCatalogSnapshot.from_entities([template], handles)
    source = {
        "uuid": NODE,
        "workflow_node_template_uuid": TEMPLATE,
        "type": "ILab",
        "param": {"slot_id": 1},
        "meta_data": {"unilab": {"input_bindings": {}}},
    }
    nodes = [deepcopy(source)]
    targets = {BOUNDARY: [{"workflow_node_uuid": NODE, "target_handle_uuid": HANDLE}]}
    argument = {"kind": "workflow_input", "parameter": "parent_slot"}
    if path == "authoring":
        _materialize_boundary_arguments(
            nodes,
            target_mappings=targets,
            boundary_handles=[
                {"uuid": BOUNDARY, "handle_key": "slot_id", "io_type": "target"}
            ],
            keyword_arguments={"slot_id": argument},
            catalog=catalog,
        )
    else:
        _materialize_published_arguments(
            expanded_nodes=nodes,
            source_nodes=[source],
            contract={
                "node_template_uuid": BOUNDARY_TEMPLATE,
                "input_contract": {"parameters": [{"name": "slot_id"}]},
                "boundary_mapping": {"target_mappings": targets},
                "graph_snapshot": {"handle_templates": handles},
            },
            normalized_param={"slot_id": argument},
        )
    assert nodes[0]["param"] == {}
    assert nodes[0]["meta_data"]["unilab"]["input_bindings"] == {
        HANDLE: {"parameter": "parent_slot"}
    }
    assert source["param"] == {"slot_id": 1}
