from __future__ import annotations

import asyncio
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from unilabos.client.materials import LocalMaterialsClient
from unilabos.server.database.repositories.history import HistoryRepository
from unilabos.server.database.repositories.materials import MaterialsRepository
from unilabos.server.protocol.common import InventoryMutation
from unilabos.server.protocol.history import HistoryEventQuery
from unilabos.server.protocol.materials import (
    MaterialIdentityWrite,
    MaterialMove,
    MaterialNodeCreate,
    MaterialTreeCreate,
    ResourceTemplateWrite,
)
from unilabos.server.scheduler.authority import SchedulerAuthorityProfile
from unilabos.server.scheduler.workflow_execution import WorkflowTaskExecutor
from unilabos.server.services.materials import MaterialsService
from unilabos.server.services.history import HistoryService
from unilabos.server.workflow.api import install_workflow_api
from unilabos.server.workflow.builtin_catalog import (
    BUILTIN_CATALOG_AUTHORITY,
    HEAT_READY_SOURCE_UUID,
    HEAT_READY_TARGET_UUID,
    HEAT_SITE_TEMPLATE_UUID,
    MATERIAL_TRANSFER_TEMPLATE_UUID,
    TRANSFER_READY_SOURCE_UUID,
    TRANSFER_READY_TARGET_UUID,
    WORKBENCH_DATA_HANDLE_UUIDS,
    WORKBENCH_READY_HANDLE_UUIDS,
    WORKBENCH_TEMPLATE_UUIDS,
    builtin_workflow_catalog,
)
from unilabos.server.workflow.service import WorkflowService
from unilabos.server.workflow.store import WorkflowStore


def _sync_catalog(service: WorkflowService) -> None:
    nodes, handles = builtin_workflow_catalog()
    service.sync_template_catalog(
        authority_id=BUILTIN_CATALOG_AUTHORITY,
        node_templates=nodes,
        handle_templates=handles,
    )


def _node(
    node_uuid: str,
    *,
    material_uuid: str,
    site_id: int,
) -> dict:
    return {
        "uuid": node_uuid,
        "workflow_node_template_uuid": HEAT_SITE_TEMPLATE_UUID,
        "material_uuid": material_uuid,
        "name": f"heat-{site_id}",
        "type": "device_action",
        "pose": {},
        "param": {
            "site_id": site_id,
            "target_temperature_c": 70 + site_id,
            "duration_seconds": 0.1,
        },
        "action_name": "heat_site",
        "action_type": "UniLabJsonCommand",
        "execution_policy": {},
        "meta_data": {"target_device_id": "virtual-heater"},
    }


def test_catalog_api_publishes_real_ready_handles_and_validates_graph() -> None:
    service = WorkflowService(
        WorkflowStore(":memory:"),
        authority_profile=SchedulerAuthorityProfile.LOCAL_SCHEDULER,
    )
    try:
        _sync_catalog(service)
        app = FastAPI()
        install_workflow_api(app, service)
        response = TestClient(app).get("/api/v1/workflow-template-catalog")
        assert response.status_code == 200
        catalog = response.json()["data"]
        assert {item["uuid"] for item in catalog["node_templates"]} == {
            HEAT_SITE_TEMPLATE_UUID,
            MATERIAL_TRANSFER_TEMPLATE_UUID,
            *WORKBENCH_TEMPLATE_UUIDS.values(),
        }
        assert {item["uuid"] for item in catalog["handle_templates"]} == {
            HEAT_READY_SOURCE_UUID,
            HEAT_READY_TARGET_UUID,
            TRANSFER_READY_SOURCE_UUID,
            TRANSFER_READY_TARGET_UUID,
            *WORKBENCH_READY_HANDLE_UUIDS.values(),
            *WORKBENCH_DATA_HANDLE_UUIDS.values(),
        }

        workflow = service.create_workflow(
            name="sequential",
            tags=["demo"],
            description=None,
            meta_data={},
        )
        first_uuid = str(uuid4())
        second_uuid = str(uuid4())
        graph = service.save_graph(
            workflow["uuid"],
            revision=workflow["revision"],
            nodes=[
                _node(first_uuid, material_uuid=str(uuid4()), site_id=1),
                _node(second_uuid, material_uuid=str(uuid4()), site_id=1),
            ],
            edges=[
                {
                    "uuid": str(uuid4()),
                    "source_node_uuid": first_uuid,
                    "target_node_uuid": second_uuid,
                    "source_handle_uuid": HEAT_READY_SOURCE_UUID,
                    "target_handle_uuid": HEAT_READY_TARGET_UUID,
                }
            ],
        )
        assert len(graph["nodes"]) == 2
        assert len(graph["edges"]) == 1
    finally:
        service.close()


def _mutation(operation: str) -> InventoryMutation:
    command_uuid = str(uuid4())
    return InventoryMutation(
        command_uuid=command_uuid,
        effect_key=f"{operation}:{command_uuid}",
        operation=operation,
        actor_type="test",
    )


def _material(
    materials: MaterialsService,
    *,
    resource_id: str,
    template_name: str,
) -> str:
    if not any(item.name == template_name for item in materials.list_templates()):
        materials.put_template(
            _mutation("put_template"),
            ResourceTemplateWrite(
                name=template_name,
                display_name=template_name,
                class_name="Resource",
            ),
        )
    created = materials.create_tree(
        _mutation("create_material_tree"),
        MaterialTreeCreate(
            nodes=[
                MaterialNodeCreate(
                    client_ref=resource_id,
                    identity=MaterialIdentityWrite(
                        resource_id=resource_id,
                        name=resource_id,
                        template_name=template_name,
                    ),
                )
            ]
        ),
    )
    return created.data.root_material_uuid


class _Backend:
    def __init__(self) -> None:
        self.listeners = []
        self.dispatched = []

    def add_job_finished_listener(self, listener) -> None:
        self.listeners.append(listener)

    def dispatch(self, payload) -> None:
        self.dispatched.append(payload)

    def cancel_task(self, _task_uuid: str) -> None:
        return None


def test_material_transfer_tool_call_runs_through_materials_authority(tmp_path) -> None:
    materials = MaterialsService(MaterialsRepository(tmp_path / "materials.db"))
    history = HistoryService(HistoryRepository(tmp_path / "history.db"))
    source_uuid = _material(
        materials,
        resource_id="source-mount",
        template_name="mount",
    )
    target_uuid = _material(
        materials,
        resource_id="target-mount",
        template_name="mount",
    )
    sample_uuid = _material(
        materials,
        resource_id="sample",
        template_name="sample",
    )
    materials.move_material(
        _mutation("move_material"),
        MaterialMove(
            material_uuid=sample_uuid,
            parent_material_uuid=source_uuid,
        ),
    )
    sync_calls = []

    def sync(command):
        assert (
            materials.get_material(sample_uuid).material.parent_material_uuid
            == target_uuid
        )
        sync_calls.append(command)
        return {"success": True}

    materials.set_resource_sync_dispatcher(sync)
    workflow = WorkflowService(
        WorkflowStore(tmp_path / "workflow.db"),
        authority_profile=SchedulerAuthorityProfile.LOCAL_SCHEDULER,
    )
    backend = _Backend()
    executor = WorkflowTaskExecutor(
        workflow,
        backend,
        materials_gateway=LocalMaterialsClient(materials),
        history=history,
        endpoint_uuid="hostlink:test-host",
    )
    try:
        _sync_catalog(workflow)
        definition = workflow.create_workflow(
            name="transfer",
            tags=["demo"],
            description=None,
            meta_data={},
        )
        node_uuid = str(uuid4())
        workflow.save_graph(
            definition["uuid"],
            revision=definition["revision"],
            nodes=[
                {
                    "uuid": node_uuid,
                    "workflow_node_template_uuid": MATERIAL_TRANSFER_TEMPLATE_UUID,
                    "name": "transfer",
                    "type": "tool_call",
                    "pose": {},
                    "param": {
                        "source_device_id": "source-device",
                        "target_device_id": "target-device",
                        "items": [
                            {
                                "material_uuid": sample_uuid,
                                "target_material_uuid": target_uuid,
                            }
                        ],
                    },
                    "action_name": "materials.transfer",
                    "execution_policy": {},
                    "meta_data": {},
                }
            ],
            edges=[],
        )
        task = workflow.create_workflow_task(
            workflow_uuid=definition["uuid"],
            run_mode="normal",
            target_node_uuid=None,
            input_value={},
            description=None,
            meta_data={},
        )
        result = asyncio.run(executor.run_task(task["uuid"]))

        assert len(result) == 1
        jobs = workflow.list_workflow_node_jobs(task["uuid"])
        assert len(jobs) == 1
        assert jobs[0]["executor_kind"] == "tool_call"
        assert jobs[0]["status"] == "succeeded"
        assert jobs[0]["return_info"]["return_value"]["data"][
            "material_uuids"
        ] == [sample_uuid]
        assert backend.dispatched == []
        assert [item.action for item in sync_calls] == ["unload", "load"]
        assert (
            materials.get_material(sample_uuid).material.parent_material_uuid
            == target_uuid
        )
        events = history.query_events(HistoryEventQuery(limit=20))
        assert [event.summary["status"] for event in events] == [
            "running",
            "running",
            "succeeded",
            "succeeded",
        ]
        assert [event.summary["entity_type"] for event in events] == [
            "workflow_task",
            "workflow_node_job",
            "workflow_node_job",
            "workflow_task",
        ]
        assert all(event.endpoint_uuid == "hostlink:test-host" for event in events)
        assert all(event.event_type == "job_transition" for event in events)
    finally:
        workflow.close()
        materials.repository.close()
        history.repository.close()
