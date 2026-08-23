from __future__ import annotations

import threading
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from unilabos.client.materials import LocalMaterialsClient, bind_payload
from unilabos.server.database.repositories.materials import MaterialsRepository
from unilabos.server.api.materials import install_materials_api
from unilabos.server.protocol.common import InventoryMutation
from unilabos.server.protocol.materials import (
    InventoryLotInbound,
    InventoryRequirement,
    InventoryReservationCreate,
    InventoryReservationTransition,
    InventoryTaskReservationCreate,
    MaterialIdentityWrite,
    MaterialNodeCreate,
    MaterialTreeCreate,
    ResourceTemplateWrite,
)
from unilabos.server.scheduler.backend import JobExecutionBackend
from unilabos.server.scheduler.status_incidents import StatusIncidentManager
from unilabos.server.scheduler.workflow_execution import WorkflowTaskExecutor
from unilabos.server.services.materials import (
    InsufficientInventoryError,
    MaterialsService,
)
from unilabos.server.workflow.service import WorkflowService
from unilabos.server.workflow.store import WorkflowStore


def _mutation(operation: str, *, job_uuid: str | None = None) -> InventoryMutation:
    return InventoryMutation(
        command_uuid=str(uuid4()),
        effect_key=operation,
        operation=operation,
        actor_type="scheduler",
        job_uuid=job_uuid,
    )


def _template(service: MaterialsService, template_uuid: str, name: str) -> None:
    service.put_template(
        _mutation("put_template"),
        ResourceTemplateWrite(
            template_uuid=template_uuid,
            name=name,
            display_name=name,
            resource_type="container",
            class_name="Container",
        ),
    )


def _material(service: MaterialsService, template_name: str) -> str:
    result = service.create_tree(
        _mutation("create_material_tree"),
        MaterialTreeCreate(
            nodes=[
                MaterialNodeCreate(
                    client_ref="material",
                    identity=MaterialIdentityWrite(
                        resource_id=f"resource-{uuid4()}",
                        name=f"material-{uuid4()}",
                        resource_type="container",
                        class_name="Container",
                        template_name=template_name,
                    ),
                )
            ]
        ),
    )
    return result.data.nodes[0].material.material_uuid


def _inbound(
    service: MaterialsService,
    *,
    lot_uuid: str,
    quantity: float,
) -> None:
    service.inbound_inventory_lot(
        _mutation("inbound_inventory_lot"),
        InventoryLotInbound(
            lot_uuid=lot_uuid,
            template_uuid="reagent-template",
            batch_no=lot_uuid,
            unit="ul",
            quantity=quantity,
        ),
    )


def _reservation(
    service: MaterialsService,
    *,
    material_uuid: str,
    quantity: float = 12,
    job_uuid: str = "job-1",
):
    return service.reserve_inventory(
        _mutation("reserve_inventory", job_uuid=job_uuid),
        InventoryReservationCreate(
            task_uuid="task-1",
            node_uuid="node-1",
            job_uuid=job_uuid,
            scheduler_revision=3,
            requirements=[
                InventoryRequirement(
                    key="plate",
                    kind="material",
                    material_uuid=material_uuid,
                ),
                InventoryRequirement(
                    key="solvent",
                    kind="reagent",
                    template_uuid="reagent-template",
                    quantity=quantity,
                    unit="ul",
                ),
            ],
        ),
    ).data


def test_reserve_and_consume_material_and_reagent_atomically(tmp_path) -> None:
    service = MaterialsService(MaterialsRepository(tmp_path / "materials.db"))
    try:
        _template(service, "plate-template", "plate")
        _template(service, "reagent-template", "reagent")
        material_uuid = _material(service, "plate")
        _inbound(service, lot_uuid="lot-a", quantity=5)
        _inbound(service, lot_uuid="lot-b", quantity=10)

        reservation = _reservation(service, material_uuid=material_uuid)

        assert reservation.status == "active"
        assert service.get_material(material_uuid).material.lifecycle_status == "reserved"
        assert [
            (lot.lot_uuid, lot.quantity_available, lot.quantity_reserved)
            for lot in service.list_inventory_lots()
        ] == [("lot-a", 0, 5), ("lot-b", 3, 7)]

        consumed = service.consume_inventory_reservation(
            _mutation("consume_inventory_reservation", job_uuid="job-1"),
            InventoryReservationTransition(
                reservation_uuid=reservation.reservation_uuid,
                reason="action_start",
            ),
        ).data

        assert consumed.status == "consumed"
        assert service.get_material(material_uuid).material.lifecycle_status == "in_use"
        assert [
            (lot.lot_uuid, lot.quantity_total, lot.quantity_available, lot.quantity_reserved)
            for lot in service.list_inventory_lots()
        ] == [("lot-a", 0, 0, 0), ("lot-b", 3, 3, 0)]
    finally:
        service.repository.close()


def test_insufficient_reservation_rolls_back_the_complete_set(tmp_path) -> None:
    service = MaterialsService(MaterialsRepository(tmp_path / "materials.db"))
    try:
        _template(service, "plate-template", "plate")
        _template(service, "reagent-template", "reagent")
        material_uuid = _material(service, "plate")
        _inbound(service, lot_uuid="lot-a", quantity=5)

        with pytest.raises(InsufficientInventoryError):
            _reservation(service, material_uuid=material_uuid, quantity=6)

        assert service.get_material(material_uuid).material.lifecycle_status == "active"
        lot = service.get_inventory_lot("lot-a")
        assert (lot.quantity_total, lot.quantity_available, lot.quantity_reserved) == (
            5,
            5,
            0,
        )
        assert service.list_inventory_reservations() == []
    finally:
        service.repository.close()


def test_task_batch_reservation_is_all_or_nothing_across_jobs(tmp_path) -> None:
    service = MaterialsService(MaterialsRepository(tmp_path / "materials.db"))
    try:
        _template(service, "plate-template", "plate")
        _template(service, "reagent-template", "reagent")
        first = _material(service, "plate")
        second = _material(service, "plate")
        _inbound(service, lot_uuid="lot-a", quantity=5)

        def request(quantity: float) -> InventoryTaskReservationCreate:
            return InventoryTaskReservationCreate(
                task_uuid="task-batch",
                scheduler_revision=7,
                reservations=[
                    InventoryReservationCreate(
                        task_uuid="task-batch",
                        node_uuid=f"node-{index}",
                        job_uuid=f"job-{index}",
                        scheduler_revision=7,
                        requirements=[
                            InventoryRequirement(
                                key="plate",
                                kind="material",
                                material_uuid=material_uuid,
                            ),
                            InventoryRequirement(
                                key="solvent",
                                kind="reagent",
                                template_uuid="reagent-template",
                                quantity=quantity,
                                unit="ul",
                            ),
                        ],
                    )
                    for index, material_uuid in enumerate((first, second), start=1)
                ],
            )

        with pytest.raises(InsufficientInventoryError):
            service.reserve_task_inventory(
                _mutation("reserve_task_inventory"),
                request(3),
            )

        assert service.list_inventory_reservations(task_uuid="task-batch") == []
        assert service.get_material(first).material.lifecycle_status == "active"
        assert service.get_material(second).material.lifecycle_status == "active"
        assert service.get_inventory_lot("lot-a").quantity_available == 5

        result = service.reserve_task_inventory(
            _mutation("reserve_task_inventory"),
            request(2),
        )
        assert [item.job_uuid for item in result.data.reservations] == [
            "job-1",
            "job-2",
        ]
        assert service.get_inventory_lot("lot-a").quantity_available == 1
    finally:
        service.repository.close()


def test_inventory_http_protocol_uses_the_same_authority_transitions(tmp_path) -> None:
    service = MaterialsService(MaterialsRepository(tmp_path / "materials.db"))
    app = FastAPI()
    install_materials_api(app, service)
    try:
        _template(service, "reagent-template", "reagent")
        inbound = InventoryLotInbound(
            lot_uuid="lot-http",
            template_uuid="reagent-template",
            unit="ul",
            quantity=8,
        )
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/materials/lots/inbound",
                json=bind_payload(
                    _mutation("inbound_inventory_lot"), inbound
                ).model_dump(mode="json", exclude_none=False),
            )
            assert response.status_code == 200, response.text

            request = InventoryReservationCreate(
                task_uuid="task-http",
                node_uuid="node-http",
                job_uuid="job-http",
                scheduler_revision=1,
                requirements=[
                    InventoryRequirement(
                        key="solvent",
                        kind="reagent",
                        template_uuid="reagent-template",
                        quantity=3,
                        unit="ul",
                    )
                ],
            )
            response = client.post(
                "/api/v1/materials/reservations",
                json=bind_payload(
                    _mutation("reserve_inventory", job_uuid="job-http"),
                    request,
                ).model_dump(mode="json", exclude_none=False),
            )
            assert response.status_code == 200, response.text
            reservation_uuid = response.json()["data"]["reservation_uuid"]

            transition = InventoryReservationTransition(
                reservation_uuid=reservation_uuid,
                reason="action_start",
            )
            response = client.post(
                f"/api/v1/materials/reservations/{reservation_uuid}/consume",
                json=bind_payload(
                    _mutation(
                        "consume_inventory_reservation",
                        job_uuid="job-http",
                    ),
                    transition,
                ).model_dump(mode="json", exclude_none=False),
            )
            assert response.status_code == 200, response.text
            assert response.json()["data"]["status"] == "consumed"
            assert client.get(
                "/api/v1/materials/reservations/by-job/job-http"
            ).json()["status"] == "consumed"
    finally:
        service.repository.close()


class _InventoryAwareAdapter:
    def __init__(self, service: MaterialsService, material_uuid: str) -> None:
        self.service = service
        self.material_uuid = material_uuid
        self._action_value_mappings = {
            "device-a": {"use": {"materials_need_lock": ["material"]}}
        }
        self.goals = []
        self.goal_event = threading.Event()

    def send_goal(self, item, **_kwargs) -> None:
        reservation = self.service.get_inventory_reservation_by_job(item.job_id)
        assert reservation.status == "consumed"
        assert self.service.get_inventory_lot("lot-a").quantity_total == 6
        self.goals.append(item)
        self.goal_event.set()

    def cancel_goal(self, _job_id: str) -> None:
        return None


class _RecordingAdapter:
    def __init__(self) -> None:
        self._action_value_mappings = {}
        self.goals = []
        self.goal_event = threading.Event()

    def send_goal(self, item, **_kwargs) -> None:
        self.goals.append(item)
        self.goal_event.set()

    def cancel_goal(self, _job_id: str) -> None:
        return None


def test_execution_consumes_under_material_lock_and_releases_on_terminal(
    tmp_path,
) -> None:
    service = MaterialsService(MaterialsRepository(tmp_path / "materials.db"))
    client = LocalMaterialsClient(service)
    try:
        _template(service, "plate-template", "plate")
        _template(service, "reagent-template", "reagent")
        material_uuid = _material(service, "plate")
        _inbound(service, lot_uuid="lot-a", quantity=10)
        adapter = _InventoryAwareAdapter(service, material_uuid)
        backend = JobExecutionBackend(
            host_node_getter=lambda: adapter,
            materials_gateway=client,
        )
        backend.start()
        try:
            backend.dispatch(
                {
                    "job_id": "job-execute",
                    "task_id": "task-1",
                    "node_id": "node-1",
                    "device_id": "device-a",
                    "action": "use",
                    "action_type": "NativeAction",
                    "action_args": {"material": {"uuid": material_uuid}},
                    "materials_need_lock": ["material"],
                    "inventory_requirements": [
                        InventoryRequirement(
                            key="solvent",
                            kind="reagent",
                            template_uuid="reagent-template",
                            quantity=4,
                            unit="ul",
                        ).model_dump(mode="json", exclude_none=False)
                    ],
                    "scheduler_revision": 1,
                }
            )
            assert adapter.goal_event.wait(2)
            assert backend._material_locks.held_by("job-execute") == (material_uuid,)

            backend.publish_job_status(
                {},
                adapter.goals[0],
                "success",
                {"return_value": None},
            )
            assert backend.wait_idle()
            assert backend._material_locks.held_by("job-execute") == ()
            assert (
                service.get_inventory_reservation_by_job("job-execute").status
                == "consumed"
            )
        finally:
            backend.stop()
    finally:
        service.repository.close()


def test_failed_action_quarantines_consumed_material_without_refund(tmp_path) -> None:
    service = MaterialsService(MaterialsRepository(tmp_path / "materials.db"))
    client = LocalMaterialsClient(service)
    try:
        _template(service, "plate-template", "plate")
        _template(service, "reagent-template", "reagent")
        material_uuid = _material(service, "plate")
        _inbound(service, lot_uuid="lot-a", quantity=10)
        adapter = _InventoryAwareAdapter(service, material_uuid)
        backend = JobExecutionBackend(
            host_node_getter=lambda: adapter,
            materials_gateway=client,
        )
        backend.start()
        try:
            backend.dispatch(
                {
                    "job_id": "job-failed",
                    "task_id": "task-1",
                    "node_id": "node-1",
                    "device_id": "device-a",
                    "action": "use",
                    "action_args": {"material": {"uuid": material_uuid}},
                    "materials_need_lock": ["material"],
                    "inventory_requirements": [
                        InventoryRequirement(
                            key="plate",
                            kind="material",
                            material_uuid=material_uuid,
                        ).model_dump(mode="json", exclude_none=False),
                        InventoryRequirement(
                            key="solvent",
                            kind="reagent",
                            template_uuid="reagent-template",
                            quantity=4,
                            unit="ul",
                        ).model_dump(mode="json", exclude_none=False),
                    ],
                }
            )
            assert adapter.goal_event.wait(2)
            backend.publish_job_status(
                {},
                adapter.goals[0],
                "failed",
                {"error": "device failed"},
            )

            reservation = service.get_inventory_reservation_by_job("job-failed")
            assert reservation.status == "quarantined"
            assert service.get_material(material_uuid).material.lifecycle_status == (
                "quarantined"
            )
            assert service.get_inventory_lot("lot-a").quantity_total == 6
            assert backend._material_locks.held_by("job-failed") == ()
        finally:
            backend.stop()
    finally:
        service.repository.close()


def test_authority_allocated_material_is_automatically_action_locked(tmp_path) -> None:
    service = MaterialsService(MaterialsRepository(tmp_path / "materials.db"))
    client = LocalMaterialsClient(service)
    adapter = _RecordingAdapter()
    try:
        _template(service, "plate-template", "plate")
        material_uuid = _material(service, "plate")
        backend = JobExecutionBackend(
            host_node_getter=lambda: adapter,
            materials_gateway=client,
        )
        backend.start()
        try:
            backend.dispatch(
                {
                    "job_id": "job-auto-material",
                    "task_id": "task-auto-material",
                    "node_id": "node-auto-material",
                    "device_id": "device-a",
                    "action": "use",
                    "action_args": {},
                    "inventory_requirements": [
                        InventoryRequirement(
                            key="plate",
                            kind="material",
                            template_uuid="plate-template",
                        ).model_dump(mode="json", exclude_none=False)
                    ],
                }
            )

            assert adapter.goal_event.wait(2)
            assert backend._material_locks.held_by("job-auto-material") == (
                material_uuid,
            )
            reservation = service.get_inventory_reservation_by_job(
                "job-auto-material"
            )
            assert reservation.status == "consumed"
            assert reservation.items[0].material_uuid == material_uuid

            backend.publish_job_status(
                {},
                adapter.goals[0],
                "success",
                {"return_value": None},
            )
            assert backend.wait_idle()
            assert backend._material_locks.held_by("job-auto-material") == ()
        finally:
            backend.stop()
    finally:
        service.repository.close()


def test_cancel_task_releases_status_held_inventory_reservation(tmp_path) -> None:
    service = MaterialsService(MaterialsRepository(tmp_path / "materials.db"))
    client = LocalMaterialsClient(service)
    incidents = StatusIncidentManager()
    try:
        _template(service, "reagent-template", "reagent")
        _inbound(service, lot_uuid="lot-a", quantity=5)
        incident = incidents.observe(
            "device-held",
            "mode",
            "Error",
            {
                "normal_values": ["Idle"],
                "incidents": {"Error": {"hold": True}},
            },
        )
        assert incident is not None
        assert incidents.is_device_held("device-held")

        backend = JobExecutionBackend(
            status_incidents=incidents,
            queue_conflicts=True,
            materials_gateway=client,
        )
        backend.dispatch(
            {
                "job_id": "job-held",
                "task_id": "task-held",
                "node_id": "node-held",
                "device_id": "device-held",
                "action": "use",
                "action_args": {},
                "inventory_requirements": [
                    InventoryRequirement(
                        key="solvent",
                        kind="reagent",
                        template_uuid="reagent-template",
                        quantity=2,
                        unit="ul",
                    ).model_dump(mode="json", exclude_none=False)
                ],
            }
        )
        assert service.get_inventory_reservation_by_job("job-held").status == (
            "active"
        )
        assert service.get_inventory_lot("lot-a").quantity_reserved == 2

        assert backend.cancel_task("task-held") == ["job-held"]
        assert service.get_inventory_reservation_by_job("job-held").status == (
            "released"
        )
        lot = service.get_inventory_lot("lot-a")
        assert (lot.quantity_available, lot.quantity_reserved) == (5, 0)
    finally:
        service.repository.close()


class _ListenerOnlyBackend:
    def add_job_finished_listener(self, listener) -> None:
        self.listener = listener


def test_local_workflow_executor_reserves_complete_task_before_dispatch(tmp_path) -> None:
    service = MaterialsService(MaterialsRepository(tmp_path / "materials.db"))
    client = LocalMaterialsClient(service)
    try:
        _template(service, "reagent-template", "reagent")
        _inbound(service, lot_uuid="lot-a", quantity=5)
        executor = WorkflowTaskExecutor(
            object(),
            _ListenerOnlyBackend(),
            materials_gateway=client,
        )
        requirement = InventoryRequirement(
            key="solvent",
            kind="reagent",
            template_uuid="reagent-template",
            quantity=2,
            unit="ul",
        ).model_dump(mode="json", exclude_none=False)
        specs = {
            "job-a": {
                "workflow_node_uuid": "node-a",
                "inventory_requirements": [requirement],
            },
            "job-b": {
                "workflow_node_uuid": "node-b",
                "inventory_requirements": [requirement],
            },
        }

        executor._reserve_task_inventory({"uuid": "task-local"}, specs)  # noqa: SLF001

        assert all(spec.get("inventory_reservation_uuid") for spec in specs.values())
        assert [
            item.status
            for item in service.list_inventory_reservations(task_uuid="task-local")
        ] == ["active", "active"]
        assert service.get_inventory_lot("lot-a").quantity_available == 1

        executor._release_unconsumed_task_inventory("task-local")  # noqa: SLF001
        assert [
            item.status
            for item in service.list_inventory_reservations(task_uuid="task-local")
        ] == ["released", "released"]
        assert service.get_inventory_lot("lot-a").quantity_available == 5
    finally:
        service.repository.close()


def test_workflow_plan_freezes_inventory_requirements() -> None:
    node_uuid = "10000000-0000-4000-8000-000000000001"
    template_uuid = "20000000-0000-4000-8000-000000000001"
    requirement = {
        "key": "solvent",
        "kind": "reagent",
        "template_uuid": "reagent-template",
        "quantity": 2,
        "unit": "ul",
    }
    graph = {
        "nodes": [
            {
                "uuid": node_uuid,
                "workflow_node_template_uuid": template_uuid,
                "parent_uuid": None,
                "type": "device",
                "disabled": False,
                "param": {},
                "execution_policy": {},
                "meta_data": {"inventory_requirements": [requirement]},
            }
        ],
        "edges": [],
        "node_templates": [
            {"uuid": template_uuid, "node_type": "device", "type": "action"}
        ],
        "handle_templates": [],
    }
    workflow = WorkflowService(WorkflowStore(":memory:"))
    try:
        plan, jobs = workflow._build_execution_plan(  # noqa: SLF001
            graph,
            run_mode="normal",
            target_node_uuid=None,
        )
    finally:
        workflow.close()

    assert len(jobs) == 1
    assert plan["nodes"][0]["inventory_requirements"] == [
        InventoryRequirement.model_validate(requirement).model_dump(
            mode="json", exclude_none=False
        )
    ]
