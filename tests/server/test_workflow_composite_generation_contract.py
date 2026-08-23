"""已发布工作流目录代际必须只包含可投影的同修订快照。"""

from __future__ import annotations

from unilabos.server.workflow.published_workflow_runtime import (
    build_published_workflow_generation,
)

WORKFLOW_UUID = "72000000-0000-4000-8000-000000000001"


class _MissingSnapshotProvider:
    def get_published_workflow_snapshot(self, workflow_uuid: str) -> dict:
        raise LookupError(workflow_uuid)


def test_unpublished_registration_is_not_resolvable_in_closed_generation() -> None:
    generation = build_published_workflow_generation(
        registrations=[
            {
                "workflow_uuid": WORKFLOW_UUID,
                "package_id": "fixture",
                "relative_path": "fixture/workflows/child.py",
                "source_uri": "package://fixture/workflows/child.py",
                "module": "fixture.workflows.child",
                "symbol": "child",
                "definition_content_hash": "sha256:" + "a" * 64,
            }
        ],
        snapshot_provider=_MissingSnapshotProvider(),
        base_node_templates=[],
    )

    assert generation.source_catalog.sources == ()
    assert generation.node_templates == ()
    assert generation.handle_templates == ()
