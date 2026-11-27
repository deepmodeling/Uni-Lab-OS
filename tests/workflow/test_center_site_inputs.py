"""冻结库位关系的中心边界，不依赖参数命名或当前库存。"""
from uuid import uuid4

import pytest

from unilabos.workflow.center_sites import site_bindings_from_graph


def test_nullable_occupant_preserves_template_constraint_and_input_owner():
    template = str(uuid4())
    graph = {"workflow": {"meta_data": {"center_site_selectors": {"schema_version": 1}}},
        "nodes": [{"workflow_node_template_uuid": "action", "param": {}, "meta_data": {"unilab": {"input_bindings": {
            "site": {"parameter": "dispatch_position"}, "owner": {"parameter": "rack"}}}}}],
        "handle_templates": [
            {"uuid": "site", "handle_key": "place", "io_type": "target", "workflow_node_template_uuid": "action",
             "meta_data": {"unilab": {"site_selector": {"version": 1, "owner": "owner", "occupant": "sample",
                                                         "show_occupied": True, "allow_occupied": False}}}},
            {"uuid": "owner", "handle_key": "owner", "io_type": "target", "workflow_node_template_uuid": "action"},
            {"uuid": "sample", "handle_key": "sample", "io_type": "target", "workflow_node_template_uuid": "action",
             "meta_data": {"unilab": {"value_schema": {"anyOf": [
                 {"$slot": "ResourceSlot", "allowed_resource_template_uuids": [template]}, {"type": "null"}]}}}},
        ]}
    result = site_bindings_from_graph(graph)["dispatch_position"]
    assert result["owner"] == {"kind": "input", "parameter": "rack"}
    assert result["allowed_resource_template_uuids"] == [template]
    graph["nodes"][0]["meta_data"]["unilab"]["input_bindings"].pop("owner")
    with pytest.raises(ValueError, match="所属物料"):
        site_bindings_from_graph(graph)
    graph["workflow"]["meta_data"].clear()
    assert site_bindings_from_graph(graph) == {}
