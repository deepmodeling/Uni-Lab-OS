"""组合展开允许只在公共边界透传的必填物料输入。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from unilabos.server.services.runtime.workflow.authoring_kernel import AuthoringCatalogSnapshot
from unilabos.server.services.runtime.workflow.catalog import PublishedSourceCatalog
from unilabos.server.services.runtime.workflow.composite import (
    CompositeAuthoring,
    project_published_workflow_contract,
)

PARENT_UUID = "76000000-0000-4000-8000-000000000001"
CHILD_UUID = "76000000-0000-4000-8000-000000000002"
INVOCATION_UUID = "76000000-0000-4000-8000-000000000003"
HOST_UUID = "76000000-0000-4000-8000-000000000004"
MATERIAL_UUID = "76000000-0000-4000-8000-000000000005"
SOURCE_HASH = "sha256:" + "a" * 64
CONTENT_HASH = "sha256:" + "b" * 64


@dataclass
class _Provider:
    snapshot: dict[str, Any]

    def get_published_workflow_snapshot(self, workflow_uuid: str) -> dict[str, Any]:
        if workflow_uuid != CHILD_UUID:
            raise LookupError(workflow_uuid)
        return self.snapshot


def _snapshot() -> dict[str, Any]:
    slot = {"$slot": "ResourceSlot"}
    return {
        "workflow": {
            "uuid": CHILD_UUID,
            "revision": 1,
            "name": "Passthrough",
            "meta_data": {
                "unilab": {
                    "input_contract": {
                        "version": 1,
                        "parameters": [
                            {
                                "name": "material",
                                "schema": slot,
                                "required": True,
                            }
                        ],
                    },
                    "output_contract": {
                        "version": 1,
                        "outputs": [
                            {
                                "name": "material",
                                "schema": slot,
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
        "applied_source": {
            "workflow_revision": 1,
            "source_hash": SOURCE_HASH,
        },
        "nodes": [],
        "edges": [],
        "node_templates": [],
        "handle_templates": [],
    }


def test_required_passthrough_input_does_not_require_internal_target() -> None:
    snapshot = _snapshot()
    sources = PublishedSourceCatalog.from_records(
        [
            {
                "workflow_uuid": CHILD_UUID,
                "definition_fqid": "fixture.workflows.passthrough",
                "module": "fixture.workflows",
                "symbol": "passthrough",
                "source_uri": "package://fixture/workflows/passthrough.py",
                "definition_content_hash": CONTENT_HASH,
            }
        ]
    )
    source = sources.resolve("fixture.workflows", "passthrough")
    contract = project_published_workflow_contract(
        source=source,
        applied_snapshot=snapshot,
        host_node_resource_template={
            "uuid": HOST_UUID,
            "name": "host_node",
            "display_name": "Host Node",
        },
    )
    assert contract is not None
    authoring = CompositeAuthoring(
        snapshot_provider=_Provider(snapshot),
        catalog=AuthoringCatalogSnapshot.from_entities(
            [contract.template],
            list(contract.handles),
        ),
        resolver=sources,
    )

    expansion = authoring.compile_invocation(
        parent_workflow_uuid=PARENT_UUID,
        invocation_uuid=INVOCATION_UUID,
        module="fixture.workflows",
        symbol="passthrough",
        keyword_arguments={"material": {"uuid": MATERIAL_UUID}},
    )

    assert expansion.diagnostics == ()
    assert expansion.nodes == ()
    assert all(not targets for targets in expansion.target_mappings.values())
    assert [mapping["kind"] for mapping in expansion.source_mappings.values()] == [
        "workflow_input"
    ]
