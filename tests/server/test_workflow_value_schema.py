from uuid import uuid4

import pytest

from unilabos.server.workflow.value_schema import (
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
