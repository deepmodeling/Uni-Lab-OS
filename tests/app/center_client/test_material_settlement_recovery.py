"""取消结算在库存/工作流提交及中心确认之间中断，仍保留精确责任。"""
from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest

from tests.app.center_client import test_material_outcomes as outcomes
from tests.app.center_client.test_cancellation import cancellation
from tests.app.center_client.test_center_client import INSTANCE, STATION
from tests.app.center_client.test_material_cancel_settlement import SettlementCenter
from tests.app.center_client.test_material_exports import bind, complete, inventory  # noqa: F401
from tests.app.center_client.test_runtime import Center
from unilabos.app.center_client.material import MaterialBridge
from unilabos.app.center_client.material_settlement import settle_material_cancellation
from unilabos.app.center_client.transport import CenterTransportError
from unilabos.app.scheduler.inventory.material_exports import InventoryMaterialAuthority

ARGS = {'reason': '现场核实取消', 'evidence_refs': ['scan:source']}


@contextmanager
def settlement_case(tmp_path, inventory, *, client=None):
    inv, backend, template = inventory
    local = backend.create_material({'resource_template_uuid': template, 'name': 'sample', 'barcode': 'SAMPLE-1'})['uuid']
    with outcomes.material_workflow(tmp_path) as (store, _, durable, package, task, scheduler, identity):
        complete(store, task, local, inventory_store=inv)
        authority = InventoryMaterialAuthority(inv, station_uuid=STATION, instance_uuid=INSTANCE,
            event_epoch_uuid=durable.event_epoch_uuid)
        client = client or SettlementCenter()
        handoffs = bind(authority, client, local)
        bridge = MaterialBridge(client, store, authority, handoff_authority=handoffs,
            origins=lambda _: (_ for _ in ()).throw(ValueError('原冻结谱系冲突')))
        bridge.pump()
        message = cancellation(Center(), package)
        durable.receive(message)
        durable.finish_command(message['command']['command_uuid'])
        runtime = SimpleNamespace(store=durable, materials=bridge, handoffs=SimpleNamespace(authority=handoffs), client=client)
        yield SimpleNamespace(runtime=runtime, workflow=store, inventory=inv, task=task, local=local,
            invocation=identity, client=client, scheduler=scheduler, template=template)


def settle(case, **overrides):
    return settle_material_cancellation(case.runtime, case.invocation, **{**ARGS, **overrides})


def release_events(case):
    return case.inventory.query_all("SELECT * FROM inventory_ledger WHERE aggregate_type='material_cancel_settlement'")


def test_inventory_commit_before_workflow_save_reuses_exact_original_proof(tmp_path, inventory, monkeypatch):
    import unilabos.app.center_client.material_settlement as module
    with settlement_case(tmp_path, inventory) as case:
        jobs = case.workflow.list_jobs(case.task['uuid'])
        original = module._save
        def crash_before_save(workflow, invocation, state):
            if state['resources_released']:
                raise RuntimeError('inventory committed before workflow state')
            return original(workflow, invocation, state)
        with monkeypatch.context() as patch:
            patch.setattr(module, '_save', crash_before_save)
            with pytest.raises(RuntimeError, match='inventory committed'):
                settle(case)
        events = release_events(case)
        assert len(events) == 1
        assert not case.workflow.center_material_settlement(case.invocation)['resources_released']
        later = admit_later_task(case)
        result = settle(case)
        assert result['release_receipt'] is not None
        assert release_events(case) == events
        assert case.inventory.query_all('SELECT * FROM inventory_material_source_binding WHERE workflow_id=?', (later[0]['workflow_id'],)) == later
        assert case.workflow.list_jobs(case.task['uuid']) == jobs
        assert case.workflow.get_task(case.task['uuid'])['status'] == 'succeeded'


def admit_later_task(case):
    from unilabos.app.scheduler.inventory.domain import MaterialRequirement, MaterialSourceAdmissionRequest
    from unilabos.app.scheduler.inventory.service import InventoryService
    task = str(uuid4())
    InventoryService(case.inventory).admit_material_sources(task, [MaterialSourceAdmissionRequest(
        node_id=str(uuid4()),resource_template_uuid=case.template,custody_policy='task_exclusive',
        requirement=MaterialRequirement(template_id=case.template,instance_uuid=case.local))])
    return case.inventory.query_all('SELECT * FROM inventory_material_source_binding WHERE workflow_id=?', (task,))


def test_final_center_ack_lost_replays_same_request_without_releasing_new_work(tmp_path, inventory):
    class LostFinalAck(SettlementCenter):
        lost = False
        def settle_canceled_material(self, invocation, body, epoch):
            receipt = super().settle_canceled_material(invocation, body, epoch)
            if body['resources_released'] and not self.lost:
                self.lost = True
                raise CenterTransportError('final response lost', retryable=True)
            return receipt
    with settlement_case(tmp_path, inventory, client=LostFinalAck()) as case:
        with pytest.raises(CenterTransportError):
            settle(case)
        events = release_events(case)
        assert case.workflow.center_material_settlement(case.invocation)['resources_released']
        later = admit_later_task(case)
        result = settle(case)
        assert result['release_receipt']
        assert case.client.settlements[-1] == case.client.settlements[-2]
        assert release_events(case) == events
        assert case.inventory.query_all('SELECT * FROM inventory_material_source_binding WHERE workflow_id=?', (later[0]['workflow_id'],)) == later
        with pytest.raises(ValueError, match='复用'):
            settle(case, reason='changed reason')


def test_execution_hold_rejects_before_sending_or_releasing(tmp_path, inventory):
    with settlement_case(tmp_path, inventory) as case:
        with case.workflow.transaction() as c:
            c.execute('CREATE TABLE IF NOT EXISTS center_recovery_holds (subject_kind TEXT,subject_uuid TEXT,revision BIGINT,PRIMARY KEY(subject_kind,subject_uuid))')
            c.execute("INSERT INTO center_recovery_holds VALUES ('task',?,1)", (case.task['uuid'],))
        with pytest.raises(ValueError, match='保护'):
            settle(case)
        assert case.client.settlements == [] and release_events(case) == []
        assert case.inventory.query_one("SELECT 1 FROM inventory_reservation WHERE workflow_id=? AND status='active'", (case.invocation,))


def test_unsafe_output_keeps_all_original_source_bindings_atomic(tmp_path, inventory):
    from unilabos.app.scheduler.inventory.domain import MaterialRequirement, MaterialSourceAdmissionRequest
    from unilabos.app.scheduler.inventory.service import InventoryService
    with settlement_case(tmp_path, inventory) as case:
        # A separate original Task input is safe, but its output remains physically unknown.
        from unilabos.app.scheduler.inventory.backend_contract import BackendResourceService
        extra = BackendResourceService(case.inventory).create_material({'resource_template_uuid':case.template, 'name':'original input'})['uuid']
        InventoryService(case.inventory).admit_material_sources(case.task['uuid'], [MaterialSourceAdmissionRequest(
            node_id=str(uuid4()),resource_template_uuid=case.template,custody_policy='task_exclusive',
            requirement=MaterialRequirement(template_id=case.template,instance_uuid=extra))])
        before = case.inventory.query_all('SELECT * FROM inventory_material_source_binding WHERE workflow_id=?', (case.task['uuid'],))
        with case.inventory.transaction() as c:
            c.execute("UPDATE material SET inventory_status='in_use' WHERE uuid=?", (case.local,))
        with pytest.raises(ValueError):
            settle(case)
        assert case.inventory.query_all('SELECT * FROM inventory_material_source_binding WHERE workflow_id=?', (case.task['uuid'],)) == before
        assert release_events(case) == []


def test_actual_dispatch_action_keeps_original_source_and_output_protected(tmp_path, inventory):
    from tests.app.test_station_material_inputs import permit
    from unilabos.app.scheduler.inventory.backend_contract import BackendResourceService
    from unilabos.app.scheduler.inventory.domain import MaterialRequirement, MaterialSourceAdmissionRequest
    from unilabos.app.scheduler.inventory.service import InventoryService
    with settlement_case(tmp_path, inventory) as case:
        extra = BackendResourceService(case.inventory).create_material({'resource_template_uuid':case.template, 'name':'original source'})['uuid']
        InventoryService(case.inventory).admit_material_sources(case.task['uuid'], [MaterialSourceAdmissionRequest(
            node_id=str(uuid4()),resource_template_uuid=case.template,custody_policy='task_exclusive',
            requirement=MaterialRequirement(template_id=case.template,instance_uuid=extra))])
        with case.inventory.transaction() as c:
            assert permit(c, case.task['uuid'], extra).acquired
        bindings = case.inventory.query_all('SELECT * FROM inventory_material_source_binding WHERE workflow_id=?', (case.task['uuid'],))
        with pytest.raises(ValueError, match='动作'):
            settle(case)
        assert case.inventory.query_all('SELECT * FROM inventory_material_source_binding WHERE workflow_id=?', (case.task['uuid'],)) == bindings
        assert release_events(case) == []


def test_settlement_api_replays_original_body_rejects_forged_release_and_exposes_progress(tmp_path, inventory):
    import threading

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from unilabos.app.center_client.material_settlement_api import create_router
    with settlement_case(tmp_path, inventory) as case:
        case.runtime._poll_lock = threading.RLock()
        case.runtime.wake = lambda: None
        app = FastAPI()
        app.include_router(create_router(lambda: case.runtime))
        client = TestClient(app)
        path = f'/api/v1/center-material-settlements/{case.invocation}'
        assert client.get(path).json()['data'] is None
        assert client.post(path, json={**ARGS, 'resources_released':True}).status_code == 422
        first = client.post(path, json=ARGS)
        assert first.status_code == 200, first.text
        assert first.json()['data']['release_receipt']
        assert client.post(path, json=ARGS).json() == first.json()
        assert client.get(path).json() == first.json()
        assert client.post(path, json={**ARGS, 'reason':'different'}).status_code == 409
        assert len(case.client.settlements) == 2


from tests.app.test_station_material_inputs import received  # noqa: E402,F401


@pytest.mark.parametrize('quarantined', [False, True])
def test_original_material_input_receipt_finishes_in_both_stores(tmp_path, received, monkeypatch, quarantined, input_finish_crash=False, input_body_conflict=False):
    from tests.app.center_client.test_material_inputs import material_runtime
    from unilabos.app.center_client.client import TaskPackage
    with material_runtime(tmp_path, received, monkeypatch, material_output=True) as (runtime, center, service, store, durable):
        package = TaskPackage.from_response(received['body'])
        runtime.poll_once()
        identity = package.station_invocation_uuid
        input_hold = durable.material_input(identity)
        assert input_hold['state'] == 'claimed'
        assert store.get_task(input_hold['receipt']['local_task_uuid'])['status'] == 'succeeded'
        authority = InventoryMaterialAuthority(received['stores']['target'], station_uuid=received['ids']['target'],
            instance_uuid=received['ids']['target_instance'], event_epoch_uuid=durable.event_epoch_uuid)
        class TargetCenter(SettlementCenter):
            def settle_canceled_material(self, invocation, body, epoch):
                return {**super().settle_canceled_material(invocation, body, epoch),
                    'owner_instance_uuid':received['ids']['target_instance']}
        client = TargetCenter()
        runtime.materials = MaterialBridge(client, store, authority, handoff_authority=received['auth']['target'])
        runtime.client = client
        message = cancellation(center, package)
        durable.receive(message)
        durable.finish_command(message['command']['command_uuid'])
        if input_body_conflict:
            import json
            target = received['stores']['target']
            with target.transaction() as c:
                row = c.execute('SELECT context_json FROM station_material_input WHERE invocation_uuid=?', (identity,)).fetchone()
                context = json.loads(row['context_json'])
                context['package']['payload']['input'] = {'value':'different original source'}
                c.execute('UPDATE station_material_input SET context_json=? WHERE invocation_uuid=?', (json.dumps(context), identity))
            with pytest.raises(ValueError, match='占用'):
                settle_material_cancellation(runtime, identity, **ARGS)
            assert durable.material_input(identity)['state'] == 'claimed'
            assert target.query_one('SELECT state FROM station_material_input WHERE invocation_uuid=?', (identity,))['state'] == 'active'
            assert target.query_one("SELECT count(*) n FROM inventory_ledger WHERE aggregate_type='material_cancel_settlement'")['n'] == 0
            return
        if quarantined:
            from tests.app.test_station_handoff import package as handoff_package
            changed = {**deepcopy(received['payload']), 'revision':received['payload']['revision']+1,
                'attention':True,'attention_reasons':['late conflicting handoff proof']}
            received['auth']['target'].quarantine_center_snapshot(handoff_package(changed))
            before = received['stores']['target'].query_one('SELECT * FROM station_material_input WHERE invocation_uuid=?', (identity,))
            with pytest.raises(ValueError):
                settle_material_cancellation(runtime, identity, **ARGS)
            assert received['stores']['target'].query_one('SELECT * FROM station_material_input WHERE invocation_uuid=?', (identity,)) == before
            assert durable.material_input(identity)['state'] == 'claimed'
            return
        if input_finish_crash:
            def crash(receipt):
                raise RuntimeError('input inventory committed before workflow receipt')
            with monkeypatch.context() as patch:
                patch.setattr(durable, 'finish_material_input_release', crash)
                with pytest.raises(RuntimeError, match='input inventory committed'):
                    settle_material_cancellation(runtime, identity, **ARGS)
            assert durable.material_input(identity)['state'] == 'claimed'
            assert received['stores']['target'].query_one('SELECT state FROM station_material_input WHERE invocation_uuid=?', (identity,))['state'] == 'released'
        state = settle_material_cancellation(runtime, identity, **ARGS)
        assert state['release_receipt']
        assert durable.material_input(identity)['state'] == 'released'
        local = received['stores']['target'].query_one('SELECT * FROM station_material_input WHERE invocation_uuid=?', (identity,))
        assert local['state'] == 'released'
        assert state['inventory_release_proof']['input_receipt'] == input_hold['receipt']
        assert store.get_task(input_hold['receipt']['local_task_uuid'])['status'] == 'succeeded'


def test_input_inventory_commit_crash_recovers_original_workflow_receipt(tmp_path, received, monkeypatch):
    test_original_material_input_receipt_finishes_in_both_stores(tmp_path, received, monkeypatch, False, True)


@pytest.mark.parametrize('protection', ['evidence_only', 'binding_conflict'])
def test_api_preserves_query_and_returns_conflict_for_recovery_protection(tmp_path, inventory, protection):
    import threading

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from unilabos.app.center_client.material_settlement_api import create_router
    with settlement_case(tmp_path, inventory) as case:
        case.runtime._poll_lock = threading.RLock()
        case.runtime.wake = lambda: None
        with case.workflow.transaction() as c:
            if protection == 'evidence_only':
                from unilabos.persistence.recovery_mode import enter
                enter(c, 'restored original database requires review')
            else:
                c.execute("UPDATE workflow_task SET request_fingerprint='different-original-call' WHERE uuid=?", (case.task['uuid'],))
        app = FastAPI()
        app.include_router(create_router(lambda: case.runtime))
        client = TestClient(app, raise_server_exceptions=False)
        path = f'/api/v1/center-material-settlements/{case.invocation}'
        assert client.get(path).status_code == 200
        assert client.post(path, json=ARGS).status_code == 409
        assert case.client.settlements == [] and release_events(case) == []


def test_original_input_body_proof_cannot_be_bypassed_by_source_cleanup(tmp_path, received, monkeypatch):
    test_original_material_input_receipt_finishes_in_both_stores(tmp_path, received, monkeypatch, False, input_body_conflict=True)


def test_completed_settlement_leaves_no_executable_recovery_or_export_work(tmp_path, inventory):
    with settlement_case(tmp_path, inventory) as case:
        assert any(p.station_invocation_uuid == case.invocation for p in case.runtime.store.packages())
        unfinished_before = case.runtime.store.capacity_backlog()['unfinished_claims']
        settle(case)
        assert all(p.station_invocation_uuid != case.invocation for p in case.runtime.store.packages())
        assert case.runtime.store.capacity_backlog()['unfinished_claims'] == unfinished_before - 1
        assert all(s['invocation_uuid'] != case.invocation for s in case.workflow.list_center_material_exports())


def test_cancel_releases_own_shared_source_without_blocking_other_shared_users(tmp_path, inventory):
    from unilabos.app.scheduler.inventory.domain import MaterialRequirement, MaterialSourceAdmissionRequest
    from unilabos.app.scheduler.inventory.service import InventoryService
    with settlement_case(tmp_path, inventory) as case:
        backend=inventory[1]
        bottle=backend.create_material({'resource_template_uuid':case.template,'name':'shared reagent','barcode':'SHARED-1'})['uuid']
        service=InventoryService(case.inventory)
        other=str(uuid4())
        for owner in (case.task['uuid'],other):
            service.admit_material_sources(owner,[MaterialSourceAdmissionRequest(node_id=str(uuid4()),
                resource_template_uuid=case.template,custody_policy='shared_source',
                requirement=MaterialRequirement(template_id=case.template,instance_uuid=bottle))])
        state=settle(case)
        assert state['release_receipt'] is not None
        assert case.inventory.query_one("SELECT status FROM inventory_material_source_binding WHERE workflow_id=? AND material_uuid=?",(other,bottle))['status']=='active'
        assert case.inventory.query_one("SELECT status FROM inventory_material_source_binding WHERE workflow_id=? AND material_uuid=?",(case.task['uuid'],bottle))['status']=='released'
