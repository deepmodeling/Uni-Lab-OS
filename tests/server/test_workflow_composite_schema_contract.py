"""组合工作流值 Schema 的容器形状与对象赋值合同。"""

from __future__ import annotations

import pytest

from unilabos.server.workflow.value_schema import schema_is_assignable
from unilabos.server.workflow.value_schema import (
    normalize_value_schema,
    validate_value,
)
from unilabos.server.workflow.workflow_io import (
    WorkflowIOValidationError,
    validate_workflow_graph_io,
)

WORKFLOW_UUID = "71000000-0000-4000-8000-000000000001"
TEMPLATE_UUID = "71000000-0000-4000-8000-000000000002"


def test_resource_slot_container_shape_is_not_erased_by_assignability() -> None:
    slot = {"$slot": "ResourceSlot"}
    slot_array = {"type": "array", "items": slot}

    assert not schema_is_assignable(slot, slot_array)
    assert not schema_is_assignable(slot_array, slot)
    assert schema_is_assignable(slot_array, slot_array)


def test_enum_schema_normalization_does_not_recurse() -> None:
    assert normalize_value_schema({"type": "string", "enum": ["x"]}) == {
        "type": "string",
        "enum": ["x"],
    }


def test_resource_slot_value_is_returned_with_canonical_uuid() -> None:
    assert validate_value(
        {"$slot": "ResourceSlot"},
        {"uuid": "71000000-0000-4000-8000-ABCDEFABCDEF"},
    ) == {"uuid": "71000000-0000-4000-8000-abcdefabcdef"}


def test_object_assignability_checks_requiredness_and_optional_field_types() -> None:
    optional_string = {
        "type": "object",
        "properties": {"value": {"type": "string"}},
    }
    optional_number = {
        "type": "object",
        "properties": {"value": {"type": "number"}},
    }
    required_number = {
        "type": "object",
        "properties": {"value": {"type": "number"}},
        "required": ["value"],
    }

    assert not schema_is_assignable(optional_string, optional_number)
    assert not schema_is_assignable(optional_number, required_number)


def test_workflow_io_rejects_duplicate_node_template_uuid() -> None:
    template = {"uuid": TEMPLATE_UUID}
    graph = {
        "workflow": {"uuid": WORKFLOW_UUID, "meta_data": {}},
        "nodes": [],
        "node_templates": [template, dict(template)],
        "handle_templates": [],
    }

    with pytest.raises(WorkflowIOValidationError) as exc_info:
        validate_workflow_graph_io(graph)

    assert exc_info.value.code == "workflow_io_invalid"
    assert exc_info.value.path == "/graph"


def test_resource_slot_input_requires_compatible_same_name_output() -> None:
    graph = {
        "workflow": {
            "uuid": WORKFLOW_UUID,
            "meta_data": {
                "unilab": {
                    "input_contract": {
                        "version": 1,
                        "parameters": [
                            {
                                "name": "material",
                                "schema": {"$slot": "ResourceSlot"},
                                "required": True,
                            }
                        ],
                    },
                    "output_contract": {"version": 1, "outputs": []},
                    "output_bindings": {},
                }
            },
        },
        "nodes": [],
        "node_templates": [],
        "handle_templates": [],
    }

    with pytest.raises(WorkflowIOValidationError) as exc_info:
        validate_workflow_graph_io(graph)

    assert exc_info.value.code == "workflow_io_invalid"


def test_implicit_material_output_is_same_name_workflow_input_passthrough() -> None:
    graph = {
        "workflow": {
            "uuid": WORKFLOW_UUID,
            "meta_data": {
                "unilab": {
                    "input_contract": {
                        "version": 1,
                        "parameters": [
                            {
                                "name": "material",
                                "schema": {"$slot": "ResourceSlot"},
                                "required": True,
                            }
                        ],
                    },
                    "output_contract": {
                        "version": 1,
                        "outputs": [
                            {
                                "name": "material",
                                "schema": {"$slot": "ResourceSlot"},
                                "implicit": True,
                            }
                        ],
                    },
                    "output_bindings": {
                        "material": {
                            "kind": "workflow_input",
                            "parameter": "material",
                        }
                    },
                }
            },
        },
        "nodes": [],
        "node_templates": [],
        "handle_templates": [],
    }

    validated = validate_workflow_graph_io(graph)

    assert validated.output_bindings["material"] == {
        "kind": "workflow_input",
        "parameter": "material",
    }


def test_workflow_io_rejects_ready_handle_as_value_binding() -> None:
    node_uuid = "71000000-0000-4000-8000-000000000003"
    ready_handle_uuid = "71000000-0000-4000-8000-000000000004"
    graph = {
        "workflow": {
            "uuid": WORKFLOW_UUID,
            "meta_data": {
                "unilab": {
                    "input_contract": {
                        "version": 1,
                        "parameters": [
                            {
                                "name": "value",
                                "schema": {"type": "object"},
                                "required": True,
                            }
                        ],
                    },
                    "output_contract": {"version": 1, "outputs": []},
                    "output_bindings": {},
                }
            },
        },
        "nodes": [
            {
                "uuid": node_uuid,
                "workflow_node_template_uuid": TEMPLATE_UUID,
                "meta_data": {
                    "unilab": {
                        "input_bindings": {
                            ready_handle_uuid: {"parameter": "value"}
                        }
                    }
                },
            }
        ],
        "node_templates": [{"uuid": TEMPLATE_UUID}],
        "handle_templates": [
            {
                "uuid": ready_handle_uuid,
                "workflow_node_template_uuid": TEMPLATE_UUID,
                "handle_key": "ready",
                "io_type": "target",
                "type": "object",
                "meta_data": {
                    "unilab": {"value_schema": {"type": "object"}}
                },
            }
        ],
    }

    with pytest.raises(WorkflowIOValidationError):
        validate_workflow_graph_io(graph)
