"""真实库存事务的 ACK 丢失重放与物料效果身份校验。"""
from dataclasses import replace

import pytest

from tests.app.test_inventory_dispatch_admission import SOURCE_SITE, TARGET_SITE, _request, station_inventory
from unilabos.app.scheduler.inventory.execution_effects import CanonicalSettlement
from unilabos.app.scheduler.inventory.station_resource import MaterialTransferCommand
from unilabos.testing.observations import observation_services


def test_inventory_receipt_survives_ack_loss_and_rejects_changed_evidence(station_inventory):
    store, service, identities = station_inventory
    request = _request(identities)
    permit = service.station_resources.acquire_dispatch_permit(request).permit
    service.station_resources.transition_dispatch_permit(permit.claim_uuid, target_state="reserved")
    command = MaterialTransferCommand(material_uuid=identities["vessel"], target_owner_material_uuid=identities["target_device"],
        target_site_uuid=TARGET_SITE, target_site_name="IN", effect_uuid=permit.effect_uuid, claim_uuid=permit.claim_uuid,
        job_uuid=request.job_uuid, attempt=request.attempt, parameter_hash=request.parameter_hash,
        expected_change_set=request.expected_change_set, fences=permit.fences)
    evidence = {"identity": {"task_uuid": request.task_uuid, "node_job_uuid": request.job_uuid}, "physical_tick": 100}
    first = CanonicalSettlement(service).commit(command, evidence)
    # 创建新适配器代表调用方丢失返回值后恢复；库存台账只能保留一次移动。
    second = CanonicalSettlement(service).commit(command, evidence)
    assert first == second and first["scope"] == "canonical_inventory"
    assert store.query_one("SELECT COUNT(*) AS n FROM inventory_ledger WHERE entry_uuid=?", (first["receipt_id"],))["n"] == 1
    assert store.query_one("SELECT occupied_material_uuid FROM site WHERE uuid=?", (SOURCE_SITE,))["occupied_material_uuid"] is None
    assert store.query_one("SELECT occupied_material_uuid FROM site WHERE uuid=?", (TARGET_SITE,))["occupied_material_uuid"] == identities["vessel"]
    with pytest.raises(ValueError, match="effect_replay_conflict"):
        CanonicalSettlement(service).commit(command, {**evidence, "physical_tick": 101})
    with pytest.raises(ValueError, match="effect_replay_conflict"):
        CanonicalSettlement(service).commit(replace(command, target_site_uuid=SOURCE_SITE), evidence)
    with pytest.raises(ValueError, match="effect_task_identity_mismatch"):
        CanonicalSettlement(service).commit(command, {**evidence, "identity": {**evidence["identity"], "task_uuid": "other"}})


def test_provider_reconciliation_requires_canonical_ledger_and_is_idempotent(station_inventory):
    import json

    from unilabos.app.scheduler.dispatch import RecordingDispatcher
    from unilabos.runtime.observations import Descriptor, Selector
    from unilabos.simulation.dispatch import SourceDispatcher
    from unilabos.simulation.provider import Connect, Lease, ProviderControl, Settlement

    store, service, identities = station_inventory
    with store.transaction() as connection:
        connection.execute('UPDATE material SET meta_data=? WHERE uuid=?',
            (json.dumps({'source':'resource-tree-set','source_node_id':'robot'}),identities['robot']))
    runtime=observation_services(store)
    try:
        runtime.observations.describe(Selector(value='robot'),Descriptor(version='1',properties={'position.observed':{'semantic':'observed','unit':'m','frame':'local'}}))
        dispatcher=SourceDispatcher(RecordingDispatcher(),runtime.observations, runtime.sources)
        control=ProviderControl(runtime.observations,dispatcher,{'group':'cell','provider':'physics','versions':{'robot':'1'},'actions':{'robot':['move']}},
            simulation_dispatch=lambda payload, admission: None, sources=runtime.sources)
        operation=control.connect(Connect(operation_id='start',source_session='worker',versions={'robot':'1'}))['operation']
        lease={key:operation[key] for key in ('runtime_session','source_session','epoch')}
        control.ready(Lease(**lease))
        request=_request(identities)
        from tests.app.test_observation_services import seed_legacy_hold
        seed_legacy_hold(runtime, request.job_uuid, group='cell', task_id=request.task_uuid, identifier='robot')
        with pytest.raises(ValueError,match='canonical_inventory_receipt_missing'):
            control.settle(Settlement(**lease,job_id=request.job_uuid,receipt_id='missing',quiescent=True))
        permit=service.station_resources.acquire_dispatch_permit(request).permit
        service.station_resources.transition_dispatch_permit(permit.claim_uuid,target_state='reserved')
        command=MaterialTransferCommand(material_uuid=identities['vessel'],target_owner_material_uuid=identities['target_device'],
            target_site_uuid=TARGET_SITE,target_site_name='IN',effect_uuid=permit.effect_uuid,claim_uuid=permit.claim_uuid,
            job_uuid=request.job_uuid,attempt=request.attempt,parameter_hash=request.parameter_hash,
            expected_change_set=request.expected_change_set,fences=permit.fences)
        receipt=CanonicalSettlement(service).commit(command,{'identity':{'task_uuid':request.task_uuid,'node_job_uuid':request.job_uuid},'physical_tick':1})
        settled=Settlement(**lease,job_id=request.job_uuid,receipt_id=receipt['receipt_id'],quiescent=True)
        first=control.settle(settled)
        assert control.settle(settled)==first and first['state']=='settled'
        assert not runtime.observations.snapshot()['groups']['cell']['pending']
    finally:
        runtime.store.close()


def test_migrated_effect_replays_the_original_audit_actor(station_inventory):
    import json
    from pathlib import Path

    from unilabos.app.scheduler.inventory.service import InventoryService
    from unilabos.app.scheduler.inventory.store import InventoryStore
    store, service, identities = station_inventory
    request = _request(identities)
    permit = service.station_resources.acquire_dispatch_permit(request).permit
    service.station_resources.transition_dispatch_permit(permit.claim_uuid, target_state='reserved')
    command = MaterialTransferCommand(material_uuid=identities['vessel'],
        target_owner_material_uuid=identities['target_device'], target_site_uuid=TARGET_SITE,
        target_site_name='IN', actor='twin.physical_settlement', effect_uuid=permit.effect_uuid,
        claim_uuid=permit.claim_uuid, job_uuid=request.job_uuid, attempt=request.attempt,
        parameter_hash=request.parameter_hash, expected_change_set=request.expected_change_set, fences=permit.fences)
    proof = {'identity': {'task_uuid':request.task_uuid, 'node_job_uuid':request.job_uuid}, 'physical_tick':100}
    first = CanonicalSettlement(service).commit(command, proof)
    with store.transaction() as db:
        db.execute('ALTER TABLE execution_effect_intent RENAME TO twin_effect_intent')
    reopened = InventoryStore(store.path)
    try:
        second = CanonicalSettlement(InventoryService(reopened)).commit(command, proof)
        assert first == second
        payload = reopened.query_one('SELECT payload FROM execution_effect_intent WHERE effect_uuid=?', (permit.effect_uuid,))['payload']
        assert json.loads(payload)['command']['actor'] == 'twin.physical_settlement'
        with pytest.raises(ValueError, match='effect_replay_conflict'):
            CanonicalSettlement(InventoryService(reopened)).commit(replace(command, actor='different'), proof)
    finally:
        reopened.close()
