"""不同任务及同仓独立库位不得被父物料误扩大保管范围。"""

from dataclasses import replace
from uuid import uuid4

import pytest

from tests.app.test_handoff_placement import (
    placement,
    prepare_planned,
    source_rack_is_device,
)
from tests.app.test_inventory_dispatch_admission import (
    SOURCE_SITE,
    _admit_vessel_source,
    _material_only_request,
    station_inventory,
)
from tests.app.test_station_handoff import package, setup
from unilabos.app.scheduler.inventory.backend_contract import (
    MATERIAL_ACTIVE_CLAIM_CONFLICT,
    BackendContractError,
    BackendResourceService,
)
from unilabos.app.scheduler.inventory.dispatch_admission import DispatchResource
from unilabos.app.scheduler.resource_lock import device_lock_key, site_lock_key


def test_distinct_task_bottles_share_device_without_mutual_custody_block(
    station_inventory,
):
    store, service, first = station_inventory
    second = dict(first)
    second["vessel"] = BackendResourceService(store).create_material(
        {
            "resource_template_uuid": first["vessel_template"],
            "parent_uuid": first["source_device"],
            "name": "另一任务的瓶子",
            "barcode": "BOTTLE-B",
        }
    )["uuid"]
    tasks = [str(uuid4()), str(uuid4())]
    requests = []
    for identity, task in zip((first, second), tasks):
        _admit_vessel_source(
            service, identity, task_uuid=task, custody_policy="task_exclusive"
        )
        request = _material_only_request(
            identity, task_uuid=task, job_uuid=str(uuid4())
        )
        device = DispatchResource(
            lock_key=device_lock_key(first["source_device"]),
            scope="device",
            material_uuid=first["source_device"],
        )
        requests.append(replace(request, resources=(device,) + request.resources))
    first_permit = service.station_resources.acquire_dispatch_permit(requests[0])
    assert first_permit.acquired
    waiting = service.station_resources.acquire_dispatch_permit(requests[1])
    assert not waiting.acquired and waiting.wait_code == "resource_claimed"
    service.station_resources.transition_dispatch_permit(
        first_permit.claim_uuid, target_state="released"
    )
    assert service.station_resources.acquire_dispatch_permit(requests[1]).acquired


def test_site_custody_checks_occupant_without_sibling_bottles(station_inventory):
    store, service, identities = station_inventory
    backend = BackendResourceService(store)
    sibling = backend.create_material(
        {
            "resource_template_uuid": identities["vessel_template"],
            "parent_uuid": identities["source_device"],
            "name": "旁边的瓶子",
        }
    )
    _admit_vessel_source(
        service,
        {**identities, "vessel": sibling["uuid"]},
        task_uuid=str(uuid4()),
        custody_policy="task_exclusive",
    )
    request = _material_only_request(
        identities, task_uuid=str(uuid4()), job_uuid=str(uuid4())
    )
    site = DispatchResource(
        lock_key=site_lock_key(identities["source_device"], SOURCE_SITE),
        scope="material_site",
        material_uuid=identities["source_device"],
        site_uuid=SOURCE_SITE,
    )
    assert service.station_resources.acquire_dispatch_permit(
        replace(request, resources=(site,))
    ).acquired


@pytest.mark.parametrize("existing", [False, True])
def test_handoff_does_not_block_loading_an_unrelated_empty_site(setup, existing):
    s = setup
    source_rack_is_device(s)
    store = s["stores"]["source"]
    backend = BackendResourceService(store)
    template = s["templates"]["source"]["test.carrier"]
    spare = backend.create_material(
        {"resource_template_uuid": template, "name": "空闲瓶"}
    )
    alternate = str(uuid4())
    with store.transaction() as connection:
        row = dict(
            connection.execute(
                "SELECT * FROM site WHERE uuid=?", (s["sites"]["source"],)
            ).fetchone()
        )
        row.update(uuid=alternate, name="另一个空库位", occupied_material_uuid=None)
        connection.execute(
            "INSERT INTO site ("
            + ",".join(row)
            + ") VALUES ("
            + ",".join("?" for _ in row)
            + ")",
            tuple(row.values()),
        )
    placement(s)
    prepare_planned(s)
    s["auth"]["source"].freeze_source(
        package(s["payload"]), physical_ready=True, evidence_refs=["operator:ready"]
    )
    place = {"action": "place", "site_uuid": alternate}
    if existing:
        result = backend.update_material(spare["uuid"], {"site_placement": place})
    else:
        result = backend.create_material(
            {
                "resource_template_uuid": template,
                "name": "新瓶",
                "site_placement": place,
            }
        )
    assert (
        store.query_one(
            "SELECT occupied_material_uuid FROM site WHERE uuid=?", (alternate,)
        )["occupied_material_uuid"]
        == result["uuid"]
    )
    with pytest.raises(BackendContractError) as blocked:
        backend.update_material(
            s["carrier"]["uuid"], {"site_placement": {"action": "remove"}}
        )
    assert blocked.value.code == MATERIAL_ACTIVE_CLAIM_CONFLICT
    assert (
        store.query_one(
            "SELECT occupied_material_uuid FROM site WHERE uuid=?",
            (s["sites"]["source"],),
        )["occupied_material_uuid"]
        == s["carrier"]["uuid"]
    )
