"""多车预占、普通派发互斥及释放围栏的库存事务测试。"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from uuid import uuid4

import pytest

from unilabos.app.scheduler.inventory.backend_contract import BackendResourceService
from unilabos.app.scheduler.inventory.dispatch_admission import (
    DispatchAdmissionRequest,
    DispatchResource,
    InventoryMutationConflict,
    acquire_dispatch_permit,
    assert_resource_keys_unclaimed,
)
from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.app.scheduler.inventory.transport_allocation import (
    AllocationConflict,
    TransportAllocations,
    VehicleCandidate,
)
from unilabos.workflow.resource_lock_key import device_lock_key


def request():
    return {
        "request_uuid": str(uuid4()),
        "generation": 1,
        "station_invocation_uuid": str(uuid4()),
        "contract_digest": "original",
    }


@pytest.fixture
def fleet(tmp_path):
    store = InventoryStore(str(tmp_path / "inventory.sqlite"))
    backend = BackendResourceService(store)
    template = backend.sync_resource_templates(
        [
            {
                "id": "test.transport-vehicle",
                "display_name": "测试车辆",
                "registry_type": "resource",
                "class": {},
            }
        ]
    )["templates"][0]["uuid"]
    vehicles = []
    for name in ("agv-1", "agv-2"):
        item = backend.create_material({"resource_template_uuid": template, "name": name, "barcode": name})
        with store.transaction() as c:
            c.execute("UPDATE material SET type='device' WHERE uuid=?", (item["uuid"],))
        vehicles.append(VehicleCandidate(item["uuid"], name))
    yield store, TransportAllocations(store), vehicles
    store.close()


def test_concurrent_requests_choose_distinct_cars_and_replay_original(fleet):
    store, allocations, vehicles = fleet
    requests = [request(), request()]
    with ThreadPoolExecutor(2) as pool:
        bindings = list(pool.map(lambda r: allocations.reserve(r, vehicles), requests))
    assert len({b["vehicle_resource_uuid"] for b in bindings}) == 2
    assert allocations.reserve(request(), vehicles)["state"] == "waiting"
    assert allocations.reserve(requests[0], list(reversed(vehicles))) == bindings[0]
    with store.transaction() as c, pytest.raises(InventoryMutationConflict):
        assert_resource_keys_unclaimed(c, lock_keys=(device_lock_key(bindings[0]["vehicle_resource_uuid"]),))


def test_regular_job_blocked_until_same_task_adopts_and_hold_still_blocks(fleet):
    store, allocations, vehicles = fleet
    binding = allocations.reserve(request(), vehicles[:1])
    task = str(uuid4())
    resources = (
        DispatchResource(
            device_lock_key(binding["vehicle_resource_uuid"]), "device", material_uuid=binding["vehicle_resource_uuid"]
        ),
    )
    job = DispatchAdmissionRequest(str(uuid4()), task, str(uuid4()), 1, "parameters", {}, resources)
    with store.transaction() as c:
        assert acquire_dispatch_permit(c, job).wait_code == "transport_vehicle_reserved"
    allocations.commit(binding)
    allocations.adopt(binding, task_uuid=task)
    with store.transaction() as c:
        assert (
            acquire_dispatch_permit(c, replace(job, task_uuid=str(uuid4()))).wait_code == "transport_vehicle_reserved"
        )
        assert acquire_dispatch_permit(c, job).acquired
    allocations.hold(task)
    with store.transaction() as c:
        assert acquire_dispatch_permit(c, job).wait_code == "transport_vehicle_reserved"


def test_cancel_before_request_is_durable_and_new_generation_requires_close(fleet):
    _, allocations, vehicles = fleet
    r = request()
    allocations.cancel(request_uuid=r["request_uuid"], generation=1, invocation_uuid=r["station_invocation_uuid"])
    assert allocations.reserve(r, vehicles)["state"] == "closed"
    b = allocations.reserve({**r, "generation": 2}, vehicles)
    with pytest.raises(AllocationConflict):
        allocations.reserve({**r, "generation": 3}, vehicles)
    with pytest.raises(AllocationConflict):
        allocations.reserve({**r, "generation": 2, "contract_digest": "changed"}, vehicles)
    assert b["generation"] == 2


def test_release_ack_replay_cannot_release_later_owner(fleet):
    store, allocations, vehicles = fleet
    b = allocations.reserve(request(), vehicles[:1])
    task = str(uuid4())
    allocations.commit(b)
    allocations.adopt(b, task_uuid=task)
    d = {**b, "decision_uuid": str(uuid4()), "close_reason": "normal_settled", "evidence_digest": "settled"}
    with pytest.raises(AllocationConflict):
        allocations.release(d)
    allocations.hold(task)
    ack = allocations.release(d)
    newer = allocations.reserve(request(), vehicles[:1])
    assert allocations.release(d) == ack
    with store.transaction() as c, pytest.raises(InventoryMutationConflict):
        assert_resource_keys_unclaimed(c, lock_keys=(device_lock_key(newer["vehicle_resource_uuid"]),))
    with pytest.raises(AllocationConflict):
        allocations.release({**d, "decision_uuid": str(uuid4())})


def test_committed_permit_cannot_use_before_permit_cancel(fleet):
    _, allocations, vehicles = fleet
    r = request()
    b = allocations.reserve(r, vehicles)
    allocations.commit(b)
    with pytest.raises(AllocationConflict):
        allocations.cancel(request_uuid=r["request_uuid"], generation=1, invocation_uuid=r["station_invocation_uuid"])


def test_uncertain_hold_requires_original_local_physical_proof(fleet):
    _, allocations, vehicles = fleet
    binding = allocations.reserve(request(), vehicles[:1])
    task = str(uuid4())
    allocations.commit(binding)
    allocations.adopt(binding, task_uuid=task)
    allocations.hold(task, uncertain=True)
    allocations.hold(task)
    decision = {
        "decision_uuid": str(uuid4()),
        "allocation_uuid": binding["allocation_uuid"],
        "generation": binding["generation"],
        "allocation_fence": binding["allocation_fence"],
        "close_reason": "normal_settled",
        "evidence_digest": "unverified",
    }
    with pytest.raises(AllocationConflict):
        allocations.release(decision)
    decision["close_reason"] = "reconciled_safe"
    with pytest.raises(AllocationConflict):
        allocations.release(decision)
    decision["evidence_digest"] = allocations.record_reconciliation(
        binding["allocation_uuid"],
        physical_safe=True,
        evidence_refs=["inspection:vehicle-empty-and-stopped"],
    )
    assert allocations.release(decision)["state"] == "released"


def test_task_cannot_change_frozen_vehicle_binding(fleet):
    _, allocations, vehicles = fleet
    binding = allocations.reserve(request(), vehicles[:1])
    with pytest.raises(AllocationConflict):
        allocations.commit({**binding, "vehicle_id": "other-car"})
    allocations.commit(binding)
    with pytest.raises(AllocationConflict):
        allocations.adopt({**binding, "resource_keys": []}, task_uuid=str(uuid4()))


def test_route_installation_change_stops_next_action_and_keeps_original_car(fleet, tmp_path):
    import json
    from types import SimpleNamespace

    from unilabos.app.center_client.transport_preparation import TransportPreparationBridge
    from unilabos.app.center_client.transport_routes import TransportRoutes

    store, allocations, vehicles = fleet
    profile = {
        "schema_version": 1,
        "profile_uuid": str(uuid4()),
        "sites": [
            {"station_uuid": str(uuid4()), "site_uuid": str(uuid4()), "nav_point_ref": side, "slot_ref": "slot"}
            for side in ["source", "target"]
        ],
    }
    path = tmp_path / "routes.json"
    path.write_text(json.dumps(profile))
    routes = TransportRoutes(str(path))
    r = request()
    for side, site in zip(["source", "target"], profile["sites"]):
        r[side + "_endpoint"] = {
            "station_uuid": site["station_uuid"],
            "items": [{"container_uuid": "11111111-1111-4111-8111-111111111111", "site_uuid": site["site_uuid"]}],
        }
    binding = allocations.reserve(r, vehicles[:1], route_binding=routes.resolve(r))
    allocations.commit(binding)
    allocations.adopt(binding, task_uuid=str(uuid4()))
    manager = TransportPreparationBridge(
        SimpleNamespace(service=SimpleNamespace(_task_scheduler_bridge=SimpleNamespace())), store, routes=routes
    )
    assert manager.dispatch_guard(binding)
    profile["sites"][0]["nav_point_ref"] = "changed"
    path.write_text(json.dumps(profile))
    assert not manager.dispatch_guard(binding)
    assert store.query_one("SELECT state FROM transport_allocation")["state"] == "needs_review"
    with store.transaction() as c, pytest.raises(InventoryMutationConflict):
        assert_resource_keys_unclaimed(c, lock_keys=(device_lock_key(binding["vehicle_resource_uuid"]),))


def test_restart_between_reserve_and_event_retains_a_closable_preparation(fleet):
    store, allocations, vehicles = fleet
    first, second = request(), request()
    reserved = allocations.reserve(first, vehicles[:1])
    committed = allocations.reserve(second, vehicles[1:])
    allocations.commit(committed)
    allocations.recover_execution_restart()
    assert (
        store.query_one(
            "SELECT state FROM transport_allocation WHERE allocation_uuid=?", (reserved["allocation_uuid"],)
        )["state"]
        == "reserved"
    )
    assert allocations.reserve(first, vehicles) == reserved
    allocations.cancel(
        request_uuid=first["request_uuid"], generation=1, invocation_uuid=first["station_invocation_uuid"]
    )
    assert (
        store.query_one(
            "SELECT state FROM transport_allocation WHERE allocation_uuid=?", (reserved["allocation_uuid"],)
        )["state"]
        == "released"
    )
    assert (
        store.query_one(
            "SELECT state FROM transport_allocation WHERE allocation_uuid=?", (committed["allocation_uuid"],)
        )["state"]
        == "needs_review"
    )


@pytest.mark.parametrize("same_task_binding", [True, False])
def test_reconciliation_repairs_task_created_before_adopt_without_resuming_execution(fleet, same_task_binding):
    from types import SimpleNamespace

    from unilabos.app.center_client.transport_preparation import TransportPreparationBridge

    store, allocations, vehicles = fleet
    r = {**request(), "handoff_uuid": str(uuid4()), "global_task_uuid": str(uuid4())}
    binding = allocations.reserve(r, vehicles[:1])
    allocations.commit(binding)
    allocations.recover_execution_restart()
    task_id = str(uuid4())
    actual = {
        "uuid": task_id,
        "status": "failed",
        "workflow_snapshot": {
            "workflow": {"meta_data": {"center_transport_binding": binding if same_task_binding else {}}}
        },
    }
    package = SimpleNamespace(
        raw={
            "transport_binding": binding,
            "invocation_key": r["station_invocation_uuid"],
            "workflow_uuid": str(uuid4()),
            "revision_fingerprint": "original",
        },
        global_task_uuid=r["global_task_uuid"],
        input={},
    )
    scheduler = SimpleNamespace(active_or_uncertain_job_ids=lambda: set())
    service = SimpleNamespace(
        _task_scheduler_bridge=scheduler,
        find_station_workflow_submission=lambda **kwargs: actual,
        get_workflow_task=lambda identity: actual,
        list_workflow_node_jobs=lambda identity: [],
    )
    durable = SimpleNamespace(
        event_epoch_uuid=str(uuid4()), package=lambda identity: package, recorded_task_uuid=lambda identity: task_id
    )
    runtime = SimpleNamespace(
        service=service,
        store=durable,
        client=SimpleNamespace(handoff_package=lambda *args: {"payload": {"attention": False, "status": "canceled"}}),
    )
    manager = TransportPreparationBridge(runtime, store)
    if same_task_binding:
        proof = manager.reconcile(
            r["handoff_uuid"],
            allocation_uuid=binding["allocation_uuid"],
            physical_safe=True,
            evidence_refs=["inspection:physical-safe"],
        )
        assert proof["evidence_digest"]
        row = store.query_one("SELECT state,task_uuid FROM transport_allocation")
        assert (row["state"], row["task_uuid"]) == ("needs_review", task_id)
        assert not manager.dispatch_guard(binding)
    else:
        with pytest.raises(ValueError):
            manager.reconcile(
                r["handoff_uuid"],
                allocation_uuid=binding["allocation_uuid"],
                physical_safe=True,
                evidence_refs=["inspection:physical-safe"],
            )
        assert store.query_one("SELECT task_uuid FROM transport_allocation")["task_uuid"] is None


def test_adopt_wakes_real_scheduler_first_job_without_unrelated_events(fleet, tmp_path):
    import json
    from types import SimpleNamespace

    from tests.workflow.test_f05_task_scheduler_bridge import JOB_UUID, TASK_UUID, _seed_task
    from unilabos.app.center_client.transport_preparation import TransportPreparationBridge
    from unilabos.app.scheduler.dispatch import RecordingDispatcher
    from unilabos.app.scheduler.inventory.service import InventoryService
    from unilabos.app.scheduler.inventory.station_resource import SqliteStationResourceInventory
    from unilabos.app.scheduler.service import EdgeScheduler
    from unilabos.workflow.store import WorkflowStore
    from unilabos.workflow.task_scheduler_bridge import TaskSchedulerBridge

    inventory, allocations, vehicles = fleet
    binding = allocations.reserve(request(), vehicles[:1])
    allocations.commit(binding)
    workflow = WorkflowStore(tmp_path / "actual-scheduler.db")
    _seed_task(workflow, with_material=False)
    task = workflow.get_task(TASK_UUID)
    plan = task["execution_plan"]
    plan["nodes"][0].update(device_id=binding["vehicle_resource_uuid"], material_uuid=binding["vehicle_resource_uuid"])
    with workflow.transaction() as c:
        c.execute("UPDATE workflow_task SET execution_plan=? WHERE uuid=?", (json.dumps(plan), TASK_UUID))
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(
        dispatcher=dispatcher,
        inventory=InventoryService(inventory),
        station_resources=SqliteStationResourceInventory(inventory, settle_material_transfer=lambda command: {}),
    )
    bridge = TaskSchedulerBridge(workflow, scheduler=scheduler)
    try:
        bridge.submit(workflow.get_task(TASK_UUID))
        assert dispatcher.dispatched == []
        manager = TransportPreparationBridge(
            SimpleNamespace(service=SimpleNamespace(_task_scheduler_bridge=bridge)), inventory
        )
        manager.adopt(SimpleNamespace(raw={"transport_binding": binding}), TASK_UUID)
        assert [job["job_id"] for job in dispatcher.dispatched] == [JOB_UUID]
    finally:
        bridge.close()
        workflow.close()


@pytest.mark.parametrize("kind", ["cancel", "release"])
def test_cleanup_fact_persists_even_if_unrelated_dispatch_wakeup_fails(fleet, tmp_path, kind):
    from types import SimpleNamespace

    from tests.app.center_client.test_event_envelope import store_at
    from unilabos.app.center_client.transport_preparation import TransportPreparationBridge
    from unilabos.center_protocol.digest import digest

    inventory, allocations, vehicles = fleet
    r = {**request(), "condition_digest": digest({"ready": True})}
    binding = allocations.reserve(r, vehicles[:1])
    if kind == "release":
        allocations.commit(binding)
        allocations.adopt(binding, task_uuid=str(uuid4()))
        allocations.hold(inventory.query_one("SELECT task_uuid FROM transport_allocation")["task_uuid"])
    durable = store_at(tmp_path / "durable-cleanup.db")

    def broken_dispatch():
        raise RuntimeError("另一个独立作业重排失败")

    runtime = SimpleNamespace(
        store=durable,
        wake=lambda: None,
        service=SimpleNamespace(_task_scheduler_bridge=SimpleNamespace(reschedule=broken_dispatch)),
    )
    bridge = TransportPreparationBridge(runtime, inventory)
    payload = (
        {
            "request_uuid": r["request_uuid"],
            "generation": 1,
            "station_invocation_uuid": r["station_invocation_uuid"],
            "condition_digest": r["condition_digest"],
        }
        if kind == "cancel"
        else {
            "decision_uuid": str(uuid4()),
            "allocation_uuid": binding["allocation_uuid"],
            "generation": 1,
            "allocation_fence": binding["allocation_fence"],
            "authorization_uuid": str(uuid4()),
            "close_reason": "normal_settled",
            "evidence_digest": digest({"settled": True}),
        }
    )
    try:
        with pytest.raises(RuntimeError):
            bridge.handle(
                {
                    "type": "transport.preparation.cancel" if kind == "cancel" else "transport.release",
                    "payload": payload,
                }
            )
        assert inventory.query_one("SELECT state FROM transport_allocation")["state"] == "released"
        assert durable.preparation_events()[0]["fact"] == ("closed" if kind == "cancel" else "released")
    finally:
        durable.close()
