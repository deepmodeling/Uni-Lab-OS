"""Pydantic DTO contract tests."""

from task_orchestration.models import (
    OpcSnapshotRequest,
    TaskInstance,
    Template,
    Trigger,
    VersionedWorkspaceResponse,
    Workspace,
    WorkflowPathQuery,
)
from pydantic import ValidationError
import pytest


def test_models_serialize_task_workspace_contract():
    trigger = Trigger(kind="manual", config={})
    template = Template(
        id="template-1",
        name="Prepare sample",
        workflow_path="demo.json",
        node_ids=["node-1"],
        trigger=trigger,
    )
    task = TaskInstance(
        id="task-1",
        template_id=template.id,
        status="pending",
    )
    response = VersionedWorkspaceResponse(
        version=3,
        workspace=Workspace(
            workflow_path="demo.json",
            templates=[template],
            task_instances=[task],
        ),
    )

    assert response.model_dump()["workspace"]["templates"][0]["trigger"]["kind"] == "manual"
    assert response.model_dump()["workspace"]["task_instances"][0]["status"] == "pending"


def test_query_and_opc_snapshot_dtos_reserve_expected_fields():
    query = WorkflowPathQuery(workflow_path="workflows/demo.json")
    request = OpcSnapshotRequest(
        workflow_path=query.workflow_path,
        provider_id="opc-1",
        variables=["temperature"],
    )

    assert request.workflow_path == "workflows/demo.json"
    assert request.variables == ["temperature"]


def test_task_instance_rejects_unknown_status():
    with pytest.raises(ValidationError):
        TaskInstance(
            id="task-1",
            template_id="template-1",
            status="queued",
        )


@pytest.mark.parametrize(
    "status,started_at,finished_at",
    [
        ("waiting", 1, None),
        ("pending", None, 1),
        ("running", None, None),
        ("running", 1, 2),
        ("completed", None, 2),
        ("completed", 2, None),
        ("completed", 2, 1),
        ("cancelled", None, 1),
    ],
)
def test_task_instance_rejects_invalid_lifecycle_timestamps(
    status, started_at, finished_at
):
    with pytest.raises(ValidationError):
        TaskInstance(
            id="task-1",
            template_id="template-1",
            status=status,
            started_at=started_at,
            finished_at=finished_at,
        )


@pytest.mark.parametrize(
    "status,started_at,finished_at",
    [
        ("waiting", None, None),
        ("pending", None, None),
        ("running", 1, None),
        ("completed", 1, 2),
        ("cancelled", None, None),
        ("cancelled", 1, 2),
    ],
)
def test_task_instance_accepts_valid_lifecycle_timestamps(
    status, started_at, finished_at
):
    instance = TaskInstance(
        id="task-1",
        template_id="template-1",
        status=status,
        started_at=started_at,
        finished_at=finished_at,
    )
    assert instance.status == status


@pytest.mark.parametrize(
    ("templates", "task_instances"),
    [
        (
            [
                Template(
                    id="template-1",
                    name="first",
                    workflow_path="demo.json",
                ),
                Template(
                    id="template-1",
                    name="second",
                    workflow_path="demo.json",
                ),
            ],
            [],
        ),
        (
            [],
            [
                TaskInstance(
                    id="task-1",
                    template_id="template-1",
                    status="pending",
                ),
                TaskInstance(
                    id="task-1",
                    template_id="template-1",
                    status="pending",
                ),
            ],
        ),
    ],
)
def test_workspace_rejects_duplicate_template_or_instance_ids(
    templates,
    task_instances,
):
    with pytest.raises(ValidationError):
        Workspace(
            workflow_path="demo.json",
            templates=templates,
            task_instances=task_instances,
        )
