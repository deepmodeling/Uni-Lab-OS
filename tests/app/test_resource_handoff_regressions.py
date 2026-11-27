"""部分交接和重新准入必须保持尚未结束的资源所有权。"""

from dataclasses import replace

import pytest

from tests.app.test_inventory_dispatch_admission import (
    _request,
)
from tests.app.test_inventory_dispatch_admission import (
    station_inventory as _station_inventory,
)
from unilabos.app.scheduler.inventory.dispatch_admission import (
    DispatchAdmissionConflict,
    DispatchAdmissionRequest,
    DispatchResource,
)
from unilabos.app.scheduler.inventory.service import InventoryService
from unilabos.app.scheduler.inventory.store import InventoryStore


@pytest.fixture()
def station_inventory(tmp_path):
    """复用准入测试的库存夹具，并把它显式注册在当前测试模块。"""

    yield from _station_inventory.__wrapped__(tmp_path)


def _device_request(
    identities: dict[str, str], suffix: int, owners: list[str]
) -> DispatchAdmissionRequest:
    return replace(
        _request(identities, job_uuid=f"40000000-0000-4000-8000-{suffix:012d}"),
        transfer=None,
        expected_change_set={"kind": "no_inventory_change"},
        resources=tuple(
            DispatchResource(f"/devices/{identities[owner]}", "device", identities[owner])
            for owner in owners
        ),
    )


def test_partial_handoff_keeps_other_continuing_interval(
    station_inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    store, service, ids = station_inventory
    first = _device_request(ids, 601, ["source_device", "target_device"])
    old = service.station_resources.acquire_dispatch_permit(first).permit
    shared = first.resources[0].lock_key
    remaining = first.resources[1].lock_key
    second = replace(
        _device_request(ids, 602, ["source_device"]),
        preheld_lock_keys=(shared,),
        preheld_job_uuids=(first.job_uuid,),
    )
    assert service.station_resources.acquire_dispatch_permit(second).acquired
    service.station_resources.release_preheld_dispatch_claims(
        task_uuid=first.task_uuid, job_uuids=(first.job_uuid,), lock_keys=(shared,)
    )
    active = store.query_all(
        "SELECT lock_key FROM station_execution_lock_lease WHERE claim_uuid=? AND state!='released'",
        (old.claim_uuid,),
    )
    assert active == [{"lock_key": remaining}]
    assert not service.station_resources.acquire_dispatch_permit(
        _device_request(ids, 603, ["target_device"])
    ).acquired


def test_shared_scope_reprepare_preserves_running_peer_fence(
    station_inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    store, service, ids = station_inventory
    first = _device_request(ids, 611, ["source_device"])
    peer = service.station_resources.acquire_dispatch_permit(first).permit
    service.station_resources.transition_dispatch_permit(peer.claim_uuid, target_state="reserved")
    service.station_resources.transition_dispatch_permit(peer.claim_uuid, target_state="running")
    key = first.resources[0].lock_key
    retry = replace(
        _device_request(ids, 612, ["source_device", "target_device"]),
        preheld_lock_keys=(key,),
        preheld_job_uuids=(first.job_uuid,),
        shared_scope_lock_keys=(key,),
    )
    initial = service.station_resources.acquire_dispatch_permit(retry).permit
    assert initial is not None
    service.station_resources.transition_dispatch_permit(
        initial.claim_uuid, target_state="released"
    )
    retried = service.station_resources.acquire_dispatch_permit(retry)
    assert retried.acquired
    assert (
        dict((f.lock_key, f.fencing_token) for f in retried.permit.fences)[key]
        == peer.fences[0].fencing_token
    )
    assert (
        store.query_one(
            "SELECT last_fencing_token FROM station_execution_fence_counter WHERE lock_key=?",
            (key,),
        )["last_fencing_token"]
        == peer.fences[0].fencing_token
    )


def test_uncertain_peer_cannot_be_bypassed_as_preheld_scope(
    station_inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    _, service, ids = station_inventory
    first = _device_request(ids, 621, ["source_device"])
    peer = service.station_resources.acquire_dispatch_permit(first).permit
    service.station_resources.transition_dispatch_permit(peer.claim_uuid, target_state="uncertain")
    key = first.resources[0].lock_key
    successor = replace(
        _device_request(ids, 622, ["source_device"]),
        preheld_lock_keys=(key,),
        preheld_job_uuids=(first.job_uuid,),
        shared_scope_lock_keys=(key,),
    )
    with pytest.raises(DispatchAdmissionConflict, match="预持有资源缺少活动前驱租约"):
        service.station_resources.acquire_dispatch_permit(successor)


def test_reprepare_rechecks_operate_in_place_fact(
    station_inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    from tests.app.test_inventory_dispatch_admission import SOURCE_SITE
    from unilabos.app.scheduler.inventory.dispatch_admission import OperateInPlaceCondition
    from unilabos.app.scheduler.inventory.station_resource import StationResourceError

    store, service, ids = station_inventory
    request = replace(
        _request(ids),
        transfer=None,
        expected_change_set={"kind": "no_inventory_change"},
        operate_in_place=OperateInPlaceCondition(
            material_uuid=ids["vessel"],
            device_material_uuid=ids["source_device"],
            site_owner_material_uuid=ids["source_device"],
            site_uuid=SOURCE_SITE,
        ),
    )
    permit = service.station_resources.acquire_dispatch_permit(request).permit
    service.station_resources.transition_dispatch_permit(permit.claim_uuid, target_state="released")
    with store.transaction() as connection:
        connection.execute(
            "UPDATE site SET occupied_material_uuid=NULL WHERE uuid=?", (SOURCE_SITE,)
        )
    with pytest.raises(StationResourceError):
        service.station_resources.acquire_dispatch_permit(request)


def test_shared_scope_dispatch_permits_are_accepted_by_edge(station_inventory, tmp_path):
    """真实库存签发不同 Claim 的共同区间凭据，Edge 必须接受而不降低版本。"""
    from uuid import uuid4

    from unilabos.app.edge_control.store import EdgeControlStore
    _store, service, ids = station_inventory
    first = _device_request(ids, 631, ['source_device'])
    permit = service.station_resources.acquire_dispatch_permit(first).permit
    service.station_resources.transition_dispatch_permit(permit.claim_uuid, target_state='reserved')
    key = first.resources[0].lock_key
    second = replace(_device_request(ids, 632, ['source_device']),
                     preheld_lock_keys=(key,), preheld_job_uuids=(first.job_uuid,),
                     shared_scope_lock_keys=(key,))
    peer = service.station_resources.acquire_dispatch_permit(second).permit
    assert peer.claim_uuid != permit.claim_uuid
    assert peer.fences == permit.fences
    edge = EdgeControlStore(str(tmp_path / 'edge.db'))
    for item in (permit, peer):
        assert edge.save_job_start({
            **item.as_mapping(), 'job_uuid': item.job_uuid,
            'task_uuid': item.task_uuid, 'node_uuid': str(uuid4()),
            'attempt': item.attempt, 'job_access_token': 'test-only',
        }, str(uuid4()))
    edge.close()
