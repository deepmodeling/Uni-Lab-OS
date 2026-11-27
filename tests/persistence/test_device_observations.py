"""真实 SQLite/PostgreSQL 上验证状态共享、完整样本与原生结算。"""

import pytest

from tests.app.test_inventory_dispatch_admission import station_inventory
from tests.app.test_observation_execution_routing import (
    test_inventory_receipt_survives_ack_loss_and_rejects_changed_evidence as verify_settlement,
)
from tests.app.test_observation_services import world
from tests.app.test_twin_native_jobs import (
    test_gate_checks_frozen_executor_parameters_and_running_permit as verify_authority,
)
from unilabos.app.scheduler.device_observations import DeviceObservationService
from unilabos.app.scheduler.inventory.runtime_view import ObservationInventoryView
from unilabos.app.scheduler.observation_context import ObservationContext
from unilabos.app.scheduler.observation_sessions import ObservationSessions
from unilabos.runtime.observations import StateError


def test_observations_share_os_state_store_and_preserve_full_samples(
    database_mode, world
):
    legacy, _, create, clock, node, _, sample, _ = world
    store = legacy.store
    context = ObservationContext(
        ObservationInventoryView(legacy.inventory), store, clock=lambda: clock[0]
    )
    observations = DeviceObservationService(context)
    sources = ObservationSessions(context, observations)
    assert context.db is store.db
    store.set("axis", "temperature", 22)
    assert store.latest_all()["axis"]["temperature"]["value"] == 22
    with pytest.raises(StateError, match="runtime_superseded"):
        legacy.observations.snapshot()
    sources.recover("group", quiescent=lambda: True)
    lease = sources.switch(
        "group",
        operation_id="os-source",
        mode="simulation",
        provider="physics",
        source_session="new-source",
        prepare=lambda: None,
        quiescent=lambda: True,
    )
    sources.ready("group", node["uuid"], lease["epoch"], "new-source")
    value = sample(
        runtime_session=context.session,
        source_session="new-source",
        epoch=lease["epoch"],
    )
    observations.publish(value)
    observations.publish(value.model_copy(update={"seq": 1, "sampled_at": 0.1}))
    assert len(observations.records()["records"]) == 2
    latest = observations.snapshot()["nodes"][0]["properties"]["position.observed"]
    assert (
        latest["sample"]["clock"] == "simulation"
        and latest["sample"]["sampled_at"] == 0.1
    )
    before = observations.snapshot()["graph_version"]
    created = create("temporary", "Temporary")
    from unilabos.app.scheduler.inventory.backend_contract import BackendResourceService

    BackendResourceService(legacy.inventory).delete_material(created["uuid"])
    assert observations.snapshot()["graph_version"] != before


def test_canonical_effect_ack_replay_on_both_backends(database_mode, station_inventory):
    verify_settlement(station_inventory)


def test_running_authority_on_both_backends(
    database_mode, station_inventory, monkeypatch
):
    verify_authority(station_inventory, monkeypatch)


def test_source_switch_obeys_native_active_and_uncertain_claims(database_mode, station_inventory):
    from tests.app.test_inventory_dispatch_admission import _request
    from unilabos.app.scheduler.device_state import DeviceStateStore
    from unilabos.runtime.observations import Descriptor, Selector
    store, service, identities = station_inventory
    state = DeviceStateStore()
    context = ObservationContext(ObservationInventoryView(store), state)
    observations = DeviceObservationService(context)
    sources = ObservationSessions(context, observations)
    try:
        selector = Selector(by='name', value='ROBOT')
        observations.describe(selector, Descriptor(version='1', properties={
            'position': {'semantic':'observed', 'unit':'m', 'frame':'world'}}))
        sources.configure_group('robot', [selector])
        permit = service.station_resources.acquire_dispatch_permit(_request(identities)).permit
        for status in ('reserved', 'uncertain'):
            service.station_resources.transition_dispatch_permit(permit.claim_uuid, target_state=status)
            with pytest.raises(StateError, match='group_busy'):
                sources.switch('robot', operation_id='switch', mode='replay', provider='replay',
                    source_session='new', prepare=lambda: pytest.fail('占用时不能准备切换'), quiescent=lambda: True)
            with pytest.raises(StateError, match='reconciliation_required'):
                sources.recover('robot', quiescent=lambda: True)
    finally:
        state.close()
