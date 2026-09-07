"""Pydantic DTO contract tests."""

import json
from pathlib import Path

from task_orchestration import models
from task_orchestration.models import (
    OpcSnapshotRequest,
    TaskDependency,
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
    trigger = Trigger(
        kind="opc",
        config={
            "plc_device_id": "szlab_poly_plc",
            "variable": "s09",
            "value": True,
        },
    )
    template = Template(
        id="template-1",
        name="Prepare sample",
        workflow_path="demo.json",
        node_ids=["node-1"],
        input_triggers=[trigger],
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

    assert response.model_dump()["workspace"]["templates"][0]["input_triggers"][0]["kind"] == "opc"
    assert response.model_dump()["workspace"]["task_instances"][0]["status"] == "pending"


def test_template_accepts_empty_triggers_and_preserves_legacy_resources():
    template = Template(
        id="template-empty",
        name="No PLC gates",
        resources=["legacy-robot"],
        input_triggers=[],
        output_triggers=[],
    )

    assert template.input_triggers == []
    assert template.output_triggers == []
    assert template.resources == ["legacy-robot"]


def test_template_distinguishes_legacy_and_explicit_dependencies():
    legacy = Template(id="legacy", name="Legacy")
    root = Template(id="root", name="Root", dependencies=[])
    dependent = Template(
        id="dependent",
        name="Dependent",
        dependencies=[
            TaskDependency(template_id="root"),
            TaskDependency(template_id="pour", node_id="pour-done"),
        ],
    )

    assert legacy.dependencies is None
    assert root.dependencies == []
    assert dependent.model_dump()["dependencies"] == [
        {"template_id": "root", "node_id": None},
        {"template_id": "pour", "node_id": "pour-done"},
    ]


@pytest.mark.parametrize(
    "dependencies",
    [
        [{"template_id": " "}],
        [{"template_id": "previous", "node_id": " "}],
        [{"template_id": "current"}],
        [{"template_id": "previous"}, {"template_id": "previous"}],
    ],
)
def test_template_rejects_invalid_dependencies(dependencies):
    with pytest.raises(ValidationError):
        Template(
            id="current",
            name="Current",
            dependencies=dependencies,
        )


def test_szlab_workspace_defines_seventeen_task_dependency_graph():
    project_root = Path(__file__).resolve().parents[2]
    sidecar = json.loads(
        (
            project_root
            / "task-orchestration"
            / "szlab_robot_action_workflow.json.task-workspace.json"
        ).read_text(encoding="utf-8")
    )
    response = models.VersionedWorkspaceResponse.model_validate(sidecar)
    templates = response.workspace.templates
    template_ids = [template.id for template in templates]

    assert len(templates) == 17
    assert response.workspace.scheduled_template_ids == template_ids
    s04_to_s05 = next(
        template
        for template in templates
        if template.node_ids == [
            "w06_pick_beaker_s04",
            "w06_place_beaker_s05",
        ]
    )
    assert [trigger.model_dump(mode="json") for trigger in s04_to_s05.input_triggers] == [
        {
            "kind": "opc",
            "config": {
                "plc_device_id": "szlab_poly_plc",
                "variable": "传感器状态_上位机[3].NO[0]",
                "value": False,
            },
        }
    ]
    assert [
        [(dependency.template_id, dependency.node_id) for dependency in template.dependencies]
        for template in templates
    ] == [
        [],
        [(template_ids[0], None)],
        [(template_ids[1], None)],
        [(template_ids[2], None)],
        [(template_ids[3], None)],
        [(template_ids[4], None)],
        [(template_ids[5], None)],
        [(template_ids[6], None)],
        [(template_ids[7], None)],
        [(template_ids[8], None)],
        [(template_ids[9], None)],
        [(template_ids[10], None)],
        [(template_ids[10], None)],
        [(template_ids[12], None)],
        [(template_ids[13], None), (template_ids[11], None)],
        [(template_ids[14], "w07_pour_beaker_s08")],
        [(template_ids[14], None), (template_ids[15], None)],
    ]
    assert templates[14].node_ids == [
        "w06_pick_beaker_s09_after_density",
        "w07_pour_beaker_s08",
        "w07_place_beaker_s11",
    ]

    workflow = json.loads(
        (project_root / "szlab_robot_action_workflow.json").read_text(
            encoding="utf-8"
        )
    )
    workflow_node_ids = [
        item["action"]["workflow_node_id"]
        for item in workflow["rules"][0]["actions"]
    ]
    assert [node_id for template in templates for node_id in template.node_ids] == (
        workflow_node_ids
    )


def test_action_resource_compatibility_fields_accept_ignored_contents():
    common = {
        "workflow_path": "demo.json",
        "expected_version": 1,
        "instance_id": "instance",
        "node_id": "node",
        "execution_id": "execution",
    }

    claim = models.ActionClaimRequest(
        **common,
        resources=["", "robot", "robot"],
    )
    succeed = models.ActionSucceedRequest(
        **common,
        release_resources=["", "unknown", "unknown"],
    )

    assert claim.resources == ["", "robot", "robot"]
    assert succeed.release_resources == ["", "unknown", "unknown"]


def test_models_reject_legacy_fields_outside_store_load_migration():
    with pytest.raises(ValidationError, match="trigger"):
        Template.model_validate({
            "id": "legacy-template",
            "name": "Legacy",
            "trigger": {"kind": "legacy", "value": True},
        })

    with pytest.raises(
        ValidationError,
        match="completed instance execution must be finished",
    ):
        Workspace.model_validate({
            "workflow_path": "demo.json",
            "templates": [{
                "id": "legacy-template",
                "name": "Legacy",
                "node_ids": ["node-1"],
            }],
            "task_instances": [{
                "id": "legacy-completed",
                "template_id": "legacy-template",
                "status": "completed",
                "started_at": 10,
                "finished_at": 20,
            }],
        })


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "resource", "config": {"resource": "s09"}},
        {"kind": "workstation", "config": {"workstation": "s09"}},
        {"kind": "internal", "config": {"key": "next", "value": True}},
        {"kind": "opc", "config": {"provider_id": "default", "variable": "s09", "value": True}},
        {"kind": "opc", "config": {"plc_device_id": "szlab_poly_plc", "variable": "s09"}},
    ],
)
def test_trigger_rejects_legacy_or_incomplete_condition(payload):
    with pytest.raises(ValidationError):
        Trigger.model_validate(payload)


def test_query_and_opc_snapshot_dtos_reserve_expected_fields():
    query = WorkflowPathQuery(workflow_path="workflows/demo.json")
    request = OpcSnapshotRequest(
        workflow_path=query.workflow_path,
        plc_device_id="szlab_poly_plc",
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
    ("status", "started_at", "finished_at"),
    [
        ("waiting", None, None),
        ("pending", None, None),
        ("completed", 1, 2),
        ("failed", 1, 2),
        ("cancelled", 1, 2),
    ],
)
def test_non_running_task_instance_rejects_active_execution(
    status, started_at, finished_at
):
    with pytest.raises(
        ValidationError,
        match="only running task instances may have active execution",
    ):
        TaskInstance(
            id="task-1",
            template_id="template-1",
            status=status,
            started_at=started_at,
            finished_at=finished_at,
            execution_state={
                "records": [
                    {
                        "node_id": "node-1",
                        "attempt": 1,
                        "execution_id": "exec-1",
                        "status": "running",
                        "started_at": 1,
                    }
                ],
                "active_node_id": "node-1",
                "active_execution_id": "exec-1",
            },
        )


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


@pytest.mark.parametrize(
    "payload",
    [
        {
            "node_id": "node-1",
            "attempt": 1,
            "execution_id": "exec-1",
            "status": "pending",
            "started_at": 1,
        },
        {
            "node_id": "node-1",
            "attempt": 1,
            "execution_id": "exec-1",
            "status": "running",
        },
        {
            "node_id": "node-1",
            "attempt": 1,
            "execution_id": "exec-1",
            "status": "running",
            "started_at": 1,
            "finished_at": 2,
        },
        {
            "node_id": "node-1",
            "attempt": 1,
            "execution_id": "exec-1",
            "status": "succeeded",
            "started_at": 1,
        },
        {
            "node_id": "node-1",
            "attempt": 1,
            "execution_id": "exec-1",
            "status": "failed",
            "started_at": 2,
            "finished_at": 1,
        },
    ],
)
def test_node_execution_record_rejects_inconsistent_timestamps(payload):
    with pytest.raises(ValidationError):
        models.NodeExecutionRecord.model_validate(payload)


@pytest.mark.parametrize("status", ["succeeded", "failed"])
def test_node_execution_record_accepts_terminal_status_with_timestamps(status):
    record = models.NodeExecutionRecord(
        node_id="node-1",
        attempt=2,
        execution_id="exec-2",
        status=status,
        started_at=10,
        finished_at=20,
        result={"value": 1} if status == "succeeded" else None,
        error="device error" if status == "failed" else None,
    )

    assert record.status == status
    assert record.finished_at == 20


@pytest.mark.parametrize(
    ("model_type", "payload"),
    [
        (
            models.NodeExecutionRecord,
            {
                "node_id": "node-1",
                "attempt": 1,
                "execution_id": "exec-1",
            },
        ),
        (
            models.ResourceLease,
            {
                "resource": "robot",
                "instance_id": "task-1",
                "node_id": "node-1",
                "execution_id": "exec-1",
                "acquired_at": 10,
            },
        ),
        (models.TaskExecutionState, {}),
        (
            models.WorkspacePauseReason,
            {
                "code": "robot_unavailable",
                "message": "Robot is unavailable",
                "timestamp": 10,
            },
        ),
    ],
)
def test_runtime_models_reject_unknown_fields(model_type, payload):
    with pytest.raises(ValidationError):
        model_type.model_validate({**payload, "unknown": True})


@pytest.mark.parametrize("field", ["node_id", "execution_id"])
def test_node_execution_record_rejects_blank_critical_ids(field):
    payload = {
        "node_id": "node-1",
        "attempt": 1,
        "execution_id": "exec-1",
    }
    payload[field] = " "

    with pytest.raises(ValidationError):
        models.NodeExecutionRecord.model_validate(payload)


@pytest.mark.parametrize(
    "field",
    ["resource", "instance_id", "node_id", "execution_id"],
)
def test_resource_lease_rejects_blank_critical_ids(field):
    payload = {
        "resource": "robot",
        "instance_id": "task-1",
        "node_id": "node-1",
        "execution_id": "exec-1",
        "acquired_at": 10,
    }
    payload[field] = " "

    with pytest.raises(ValidationError):
        models.ResourceLease.model_validate(payload)


@pytest.mark.parametrize("field", ["active_node_id", "active_execution_id"])
def test_task_execution_state_rejects_blank_active_ids(field):
    with pytest.raises(ValidationError):
        models.TaskExecutionState.model_validate({field: " "})


@pytest.mark.parametrize("field", ["instance_id", "node_id", "execution_id"])
def test_workspace_pause_reason_rejects_blank_context_ids(field):
    payload = {
        "code": "robot_unavailable",
        "message": "Robot is unavailable",
        "timestamp": 10,
        field: " ",
    }

    with pytest.raises(ValidationError):
        models.WorkspacePauseReason.model_validate(payload)


def test_task_execution_state_rejects_negative_cursor():
    with pytest.raises(ValidationError):
        models.TaskExecutionState(cursor=-1)


def test_task_execution_state_rejects_duplicate_execution_ids():
    with pytest.raises(ValidationError):
        models.TaskExecutionState(
            records=[
                models.NodeExecutionRecord(
                    node_id="node-1",
                    attempt=1,
                    execution_id="exec-1",
                ),
                models.NodeExecutionRecord(
                    node_id="node-2",
                    attempt=1,
                    execution_id="exec-1",
                ),
            ]
        )


def test_task_execution_state_rejects_multiple_running_records():
    with pytest.raises(ValidationError):
        models.TaskExecutionState(
            records=[
                models.NodeExecutionRecord(
                    node_id="node-1",
                    attempt=1,
                    execution_id="exec-1",
                    status="running",
                    started_at=10,
                ),
                models.NodeExecutionRecord(
                    node_id="node-2",
                    attempt=1,
                    execution_id="exec-2",
                    status="running",
                    started_at=11,
                ),
            ],
            active_node_id="node-1",
            active_execution_id="exec-1",
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"active_node_id": "node-1"},
        {"active_execution_id": "exec-1"},
        {
            "records": [
                {
                    "node_id": "node-1",
                    "attempt": 1,
                    "execution_id": "exec-1",
                    "status": "running",
                    "started_at": 10,
                }
            ],
        },
        {
            "active_node_id": "node-1",
            "active_execution_id": "exec-1",
            "records": [],
        },
        {
            "active_node_id": "node-2",
            "active_execution_id": "exec-1",
            "records": [
                {
                    "node_id": "node-1",
                    "attempt": 1,
                    "execution_id": "exec-1",
                    "status": "running",
                    "started_at": 10,
                }
            ],
        },
    ],
)
def test_task_execution_state_rejects_inconsistent_active_execution(payload):
    with pytest.raises(ValidationError):
        models.TaskExecutionState.model_validate(payload)


def test_task_instance_and_workspace_persist_explicit_runtime_state():
    record = models.NodeExecutionRecord(
        node_id="node-1",
        attempt=1,
        execution_id="exec-1",
        status="running",
        started_at=10,
        resources=["robot"],
    )
    task = TaskInstance(
        id="task-1",
        template_id="template-1",
        status="running",
        started_at=10,
        execution_state=models.TaskExecutionState(
            cursor=0,
            records=[record],
            active_node_id="node-1",
            active_execution_id="exec-1",
        ),
    )
    workspace = Workspace(
        workflow_path="demo.json",
        templates=[
            Template(
                id="template-1",
                name="Prepare sample",
                workflow_path="demo.json",
                node_ids=["node-1"],
            )
        ],
        task_instances=[task],
        dynamic_resource_leases=[
            models.ResourceLease(
                resource="robot",
                instance_id="task-1",
                node_id="node-1",
                execution_id="exec-1",
                acquired_at=10,
            )
        ],
        pause_reason=models.WorkspacePauseReason(
            code="robot_unavailable",
            message="Robot is unavailable",
            instance_id="task-1",
            node_id="node-1",
            execution_id="exec-1",
            timestamp=11,
            detail={"resource": "robot"},
        ),
    )

    dumped = workspace.model_dump()
    assert dumped["task_instances"][0]["execution_state"]["active_node_id"] == "node-1"
    assert dumped["dynamic_resource_leases"][0]["resource"] == "robot"
    assert dumped["pause_reason"]["code"] == "robot_unavailable"


def test_legacy_sidecar_defaults_new_runtime_state_fields():
    workspace = Workspace.model_validate(
        {
            "workflow_path": "demo.json",
            "scheduler_paused": True,
            "templates": [
                {
                    "id": "template-1",
                    "name": "Prepare sample",
                    "workflow_path": "demo.json",
                }
            ],
            "task_instances": [
                {
                    "id": "task-1",
                    "template_id": "template-1",
                    "status": "pending",
                }
            ],
        }
    )

    assert workspace.scheduler_paused is True
    assert workspace.task_instances[0].execution_state.cursor == 0
    assert workspace.task_instances[0].execution_state.records == []
    assert workspace.dynamic_resource_leases == []
    assert workspace.pause_reason is None


@pytest.mark.parametrize(
    ("model_type", "payload"),
    [
        (
            models.NodeExecutionRecord,
            {
                "node_id": "node-1",
                "attempt": 1,
                "execution_id": "exec-1",
                "result": object(),
            },
        ),
        (
            models.NodeExecutionRecord,
            {
                "node_id": "node-1",
                "attempt": 1,
                "execution_id": "exec-1",
                "result": {"nested": {1}},
            },
        ),
        (
            models.WorkspacePauseReason,
            {
                "code": "blocked",
                "message": "Execution blocked",
                "timestamp": 1,
                "detail": {"invalid": object()},
            },
        ),
        (
            models.WorkspacePauseReason,
            {
                "code": "blocked",
                "message": "Execution blocked",
                "timestamp": 1,
                "detail": {"nested": {1}},
            },
        ),
    ],
)
def test_runtime_metadata_rejects_non_json_values(model_type, payload):
    with pytest.raises(ValidationError):
        model_type.model_validate(payload)


def test_validated_copy_revalidates_updates():
    record = models.NodeExecutionRecord(
        node_id="node-1",
        attempt=1,
        execution_id="exec-1",
        status="running",
        started_at=10,
    )

    with pytest.raises(ValidationError, match="terminal records require"):
        record.validated_copy(update={"status": "succeeded"})


def _runtime_workspace_payload():
    return {
        "workflow_path": "demo.json",
        "templates": [
            {
                "id": "template-1",
                "name": "Prepare sample",
                "workflow_path": "demo.json",
                "node_ids": ["node-1", "node-2"],
            }
        ],
        "task_instances": [
            {
                "id": "task-1",
                "template_id": "template-1",
                "status": "running",
                "started_at": 10,
                "execution_state": {
                    "cursor": 0,
                    "records": [
                        {
                            "node_id": "node-1",
                            "attempt": 1,
                            "execution_id": "exec-1",
                            "status": "running",
                            "started_at": 10,
                            "resources": ["robot"],
                        }
                    ],
                    "active_node_id": "node-1",
                    "active_execution_id": "exec-1",
                },
            }
        ],
        "dynamic_resource_leases": [
            {
                "resource": "robot",
                "instance_id": "task-1",
                "node_id": "node-1",
                "execution_id": "exec-1",
                "acquired_at": 10,
            }
        ],
    }


def test_workspace_rejects_lease_for_unknown_instance():
    payload = _runtime_workspace_payload()
    payload["dynamic_resource_leases"][0]["instance_id"] = "missing-task"

    with pytest.raises(
        ValidationError, match="dynamic resource lease instance must exist"
    ):
        Workspace.model_validate(payload)


def test_workspace_rejects_lease_node_outside_instance_template():
    payload = _runtime_workspace_payload()
    payload["dynamic_resource_leases"][0]["node_id"] = "unknown-node"

    with pytest.raises(
        ValidationError, match="dynamic resource lease node must belong to template"
    ):
        Workspace.model_validate(payload)


def test_workspace_rejects_lease_without_matching_execution_record():
    payload = _runtime_workspace_payload()
    payload["dynamic_resource_leases"][0]["execution_id"] = "missing-execution"

    with pytest.raises(
        ValidationError, match="dynamic resource lease must match an execution record"
    ):
        Workspace.model_validate(payload)


def test_workspace_rejects_multiple_active_leases_for_resource():
    payload = _runtime_workspace_payload()
    payload["dynamic_resource_leases"].append(
        {
            **payload["dynamic_resource_leases"][0],
            "acquired_at": 11,
        }
    )

    with pytest.raises(
        ValidationError, match="dynamic resources may have only one active lease"
    ):
        Workspace.model_validate(payload)


def test_workspace_rejects_cursor_past_template_nodes():
    payload = _runtime_workspace_payload()
    payload["task_instances"][0]["execution_state"]["cursor"] = 3

    with pytest.raises(
        ValidationError, match="execution cursor must not exceed template node count"
    ):
        Workspace.model_validate(payload)


def test_workspace_rejects_completed_instance_before_execution_cursor_is_exhausted():
    payload = _runtime_workspace_payload()
    instance = payload["task_instances"][0]
    instance.update({
        "status": "completed",
        "finished_at": 20,
        "execution_state": {
            "cursor": 0,
            "records": [],
            "active_node_id": None,
            "active_execution_id": None,
        },
    })
    payload["dynamic_resource_leases"] = []

    with pytest.raises(
        ValidationError, match="completed instance execution must be finished"
    ):
        Workspace.model_validate(payload)


@pytest.mark.parametrize(
    ("cursor", "active_node_id", "records"),
    [
        (
            0,
            "node-2",
            [{
                "node_id": "node-2",
                "attempt": 1,
                "execution_id": "exec-active",
                "status": "running",
                "started_at": 10,
            }],
        ),
        (
            2,
            "node-2",
            [
                {
                    "node_id": "node-1",
                    "attempt": 1,
                    "execution_id": "exec-node-1",
                    "status": "succeeded",
                    "started_at": 1,
                    "finished_at": 2,
                },
                {
                    "node_id": "node-2",
                    "attempt": 1,
                    "execution_id": "exec-node-2",
                    "status": "succeeded",
                    "started_at": 3,
                    "finished_at": 4,
                },
                {
                    "node_id": "node-2",
                    "attempt": 2,
                    "execution_id": "exec-active",
                    "status": "running",
                    "started_at": 5,
                },
            ],
        ),
    ],
)
def test_workspace_rejects_active_execution_not_at_cursor(
    cursor, active_node_id, records
):
    payload = _runtime_workspace_payload()
    payload["task_instances"][0]["execution_state"] = {
        "cursor": cursor,
        "records": records,
        "active_node_id": active_node_id,
        "active_execution_id": "exec-active",
    }
    payload["dynamic_resource_leases"] = []

    with pytest.raises(
        ValidationError, match="active execution must point to cursor node"
    ):
        Workspace.model_validate(payload)


def test_workspace_rejects_execution_record_node_outside_template():
    payload = _runtime_workspace_payload()
    state = payload["task_instances"][0]["execution_state"]
    state["records"][0]["node_id"] = "unknown-node"
    state["active_node_id"] = "unknown-node"

    with pytest.raises(
        ValidationError, match="execution record node must belong to template"
    ):
        Workspace.model_validate(payload)


def test_workspace_rejects_cursor_with_missing_succeeded_predecessor():
    payload = _runtime_workspace_payload()
    state = payload["task_instances"][0]["execution_state"]
    state.update({
        "cursor": 1,
        "records": [],
        "active_node_id": None,
        "active_execution_id": None,
    })
    payload["dynamic_resource_leases"] = []

    with pytest.raises(
        ValidationError, match="cursor predecessors must each have one succeeded record"
    ):
        Workspace.model_validate(payload)


def test_workspace_rejects_duplicate_succeeded_record_before_cursor():
    payload = _runtime_workspace_payload()
    state = payload["task_instances"][0]["execution_state"]
    succeeded = {
        "node_id": "node-1",
        "attempt": 1,
        "execution_id": "exec-success-1",
        "status": "succeeded",
        "started_at": 1,
        "finished_at": 2,
    }
    state.update({
        "cursor": 1,
        "records": [
            succeeded,
            {**succeeded, "attempt": 2, "execution_id": "exec-success-2"},
        ],
        "active_node_id": None,
        "active_execution_id": None,
    })
    payload["dynamic_resource_leases"] = []

    with pytest.raises(
        ValidationError, match="cursor predecessors must each have one succeeded record"
    ):
        Workspace.model_validate(payload)


def test_workspace_rejects_succeeded_record_after_cursor():
    payload = _runtime_workspace_payload()
    state = payload["task_instances"][0]["execution_state"]
    state.update({
        "cursor": 0,
        "records": [{
            "node_id": "node-1",
            "attempt": 1,
            "execution_id": "exec-success",
            "status": "succeeded",
            "started_at": 1,
            "finished_at": 2,
        }],
        "active_node_id": None,
        "active_execution_id": None,
    })
    payload["dynamic_resource_leases"] = []

    with pytest.raises(
        ValidationError, match="cursor and succeeded records are inconsistent"
    ):
        Workspace.model_validate(payload)


def test_workspace_rejects_out_of_order_succeeded_records():
    payload = _runtime_workspace_payload()
    state = payload["task_instances"][0]["execution_state"]
    state.update({
        "cursor": 2,
        "records": [
            {
                "node_id": "node-2",
                "attempt": 1,
                "execution_id": "exec-node-2",
                "status": "succeeded",
                "started_at": 1,
                "finished_at": 2,
            },
            {
                "node_id": "node-1",
                "attempt": 1,
                "execution_id": "exec-node-1",
                "status": "succeeded",
                "started_at": 3,
                "finished_at": 4,
            },
        ],
        "active_node_id": None,
        "active_execution_id": None,
    })
    payload["dynamic_resource_leases"] = []

    with pytest.raises(
        ValidationError, match="succeeded records must follow template node order"
    ):
        Workspace.model_validate(payload)


def test_workspace_rejects_lease_resource_not_declared_by_execution():
    payload = _runtime_workspace_payload()
    payload["task_instances"][0]["execution_state"]["records"][0]["resources"] = [
        "s07"
    ]

    with pytest.raises(
        ValidationError, match="dynamic resource lease must be declared by execution"
    ):
        Workspace.model_validate(payload)


@pytest.mark.parametrize(
    ("pause_reason", "error"),
    [
        (
            {
                "code": "blocked",
                "message": "Execution blocked",
                "instance_id": "missing-task",
                "timestamp": 11,
            },
            "pause reason instance must exist",
        ),
        (
            {
                "code": "blocked",
                "message": "Execution blocked",
                "instance_id": "task-1",
                "node_id": "unknown-node",
                "timestamp": 11,
            },
            "pause reason node must belong to template",
        ),
        (
            {
                "code": "blocked",
                "message": "Execution blocked",
                "instance_id": "task-1",
                "node_id": "node-1",
                "execution_id": "missing-execution",
                "timestamp": 11,
            },
            "pause reason must match an execution record",
        ),
        (
            {
                "code": "blocked",
                "message": "Execution blocked",
                "node_id": "node-1",
                "timestamp": 11,
            },
            "pause reason node requires instance_id",
        ),
    ],
)
def test_workspace_rejects_invalid_pause_reason_references(pause_reason, error):
    payload = _runtime_workspace_payload()
    payload["pause_reason"] = pause_reason

    with pytest.raises(ValidationError, match=error):
        Workspace.model_validate(payload)
