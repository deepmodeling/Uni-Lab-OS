"""组合工作流目录与兼容性投影的规范化合同。"""

from __future__ import annotations

import pytest

from unilabos.server.workflow.authoring_kernel import (
    AuthoringCatalogError,
    AuthoringCatalogSnapshot,
)
from unilabos.server.workflow.composite_compatibility import (
    classify_published_workflow_compatibility_projections,
)

WORKFLOW_UUID = "74000000-0000-4000-8000-000000000001"


def _projection(schema: dict, *, mode: object = False) -> dict:
    return {
        "workflow_uuid": WORKFLOW_UUID,
        "mode": mode,
        "digest": "sha256:" + "a" * 64,
        "inputs": [
            {
                "name": "payload",
                "schema": schema,
                "required": False,
                "has_default": True,
            }
        ],
        "outputs": [],
    }


def test_catalog_rejects_noncanonical_uuid_without_leaking_keyerror() -> None:
    with pytest.raises(AuthoringCatalogError):
        AuthoringCatalogSnapshot.from_entities(
            [
                {
                    "uuid": "74000000-0000-4000-8000-ABCDEFABCDEF",
                    "class": "fixture.devices:Device",
                    "name": "consume",
                }
            ],
            [],
        )


def test_ordinary_property_named_allowlist_is_not_ignored() -> None:
    previous = _projection(
        {
            "type": "object",
            "properties": {
                "allowed_resource_template_uuids": {"type": "string"}
            },
        }
    )
    current = _projection(
        {
            "type": "object",
            "properties": {
                "allowed_resource_template_uuids": {"type": "integer"}
            },
        }
    )

    assert (
        classify_published_workflow_compatibility_projections(previous, current)
        == "breaking"
    )


def test_malformed_compatibility_mode_is_breaking() -> None:
    previous = _projection({"type": "string"}, mode="false")
    current = _projection({"type": "string"}, mode=True)

    assert (
        classify_published_workflow_compatibility_projections(previous, current)
        == "breaking"
    )
