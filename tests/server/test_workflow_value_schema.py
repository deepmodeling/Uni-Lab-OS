from uuid import uuid4

import pytest

from unilabos.server.services.runtime.workflow.value_schema import (
    WorkflowValueSchemaError,
    validate_value,
)


def test_resource_slot_value_requires_a_canonical_uuid() -> None:
    material_uuid = str(uuid4())

    assert validate_value(
        {"$slot": "ResourceSlot"},
        {"uuid": material_uuid},
    ) == {"uuid": material_uuid}

    with pytest.raises(WorkflowValueSchemaError) as exc_info:
        validate_value(
            {"$slot": "ResourceSlot"},
            {"uuid": "local-material-name"},
        )
    assert exc_info.value.code == "workflow_value_invalid"


@pytest.mark.parametrize("schema,value", [
    ({"type": "integer", "minimum": 2}, 1),
    ({"type": "number", "maximum": 2}, 3),
    ({"type": "string", "minLength": 2}, "a"),
    ({"type": "array", "items": {"type": "integer"}, "maxItems": 1}, [1, 2]),
])
def test_input_and_output_bounds_are_enforced(schema, value):
    with pytest.raises(WorkflowValueSchemaError):
        validate_value(schema, value)


def test_composite_cannot_narrow_parent_bounds_without_validation():
    from unilabos.server.services.runtime.workflow.value_schema import schema_is_assignable
    target = {"type": "number", "minimum": 1, "maximum": 10}
    assert not schema_is_assignable({"type": "integer"}, target)
    assert not schema_is_assignable({"type": "integer", "enum": [0, 2]}, target)
    assert schema_is_assignable({"type": "integer", "enum": [1, 2]}, target)
    assert schema_is_assignable({"type": "integer", "minimum": 2, "maximum": 8}, target)
