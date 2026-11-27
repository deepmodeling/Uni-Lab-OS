from __future__ import annotations

import pytest

from tests.app.center_client import test_material_outcomes as outcomes
from tests.app.center_client.test_material_export_retry import authority, pump_cycle
from tests.app.center_client.test_material_exports import Allocator, complete, inventory, uid
from unilabos.app.center_client.material import MaterialBridge
from unilabos.app.center_client.websocket import validate_attention_snapshot


def test_task_level_export_attention_stays_open_until_real_phase_success(tmp_path, inventory):
    inv, _, _ = inventory
    with outcomes.material_workflow(tmp_path) as (store, _, durable, _, task, scheduler, identity):
        complete(store, task, uid())
        jobs, submissions = store.list_jobs(task['uuid']), list(scheduler.submissions)
        a = authority(inv, durable, [10000])
        bridge = MaterialBridge(Allocator(), store, a)
        pump_cycle(bridge)
        items = bridge.export_attention_items()
        assert len(items) == 1
        item = items[0]
        assert item['decision_kind'] == 'material_export'
        assert item['workflow_task_uuid'] == task['uuid']
        assert 'workflow_node_job_uuid' not in item
        assert item['phase'] == 'prepare' and item['status'] == 'blocked'
        validate_attention_snapshot({'manual_confirmation_count': 0, 'exception_count': 1,
            'items': {'manual_confirmations': [], 'exceptions': items}})
        bridge.resume_failure(identity, 'prepare', expected_revision=item['revision'])
        assert bridge.export_attention_items()[0]['status'] == 'retrying'
        assert bridge.export_attention_items()[0]['meta_data']['last_error']
        pump_cycle(bridge)
        assert bridge.export_attention_items()[0]['status'] == 'blocked'
        assert store.get_task(task['uuid'])['status'] == 'succeeded'
        assert store.list_jobs(task['uuid']) == jobs and scheduler.submissions == submissions
        with pytest.raises(ValueError, match='版本'):
            bridge.resume_failure(identity, 'prepare', expected_revision=item['revision'])


def test_resume_rejects_foreign_or_missing_task_binding_and_held_execution(tmp_path, inventory):
    inv, _, _ = inventory
    with outcomes.material_workflow(tmp_path) as (store, _, durable, _, task, _, identity):
        complete(store, task, uid())
        bridge = MaterialBridge(Allocator(), store, authority(inv, durable, [10000]))
        pump_cycle(bridge)
        item = bridge.export_attention_items()[0]
        with pytest.raises(ValueError):
            bridge.resume_failure(uid(), 'prepare', expected_revision=item['revision'])
        with store.transaction() as c:
            c.execute("CREATE TABLE IF NOT EXISTS center_recovery_holds (subject_kind TEXT,subject_uuid TEXT,revision BIGINT,PRIMARY KEY(subject_kind,subject_uuid))")
            c.execute("INSERT INTO center_recovery_holds VALUES ('task',?,1)", (task['uuid'],))
        with pytest.raises(ValueError, match='恢复保护'):
            bridge.resume_failure(identity, 'prepare', expected_revision=item['revision'])
        with store.transaction() as c:
            c.execute('DELETE FROM center_invocation_inbox WHERE invocation_uuid=?', (identity,))
        assert bridge.export_attention_items() == []
        with pytest.raises(ValueError):
            bridge.resume_failure(identity, 'prepare', expected_revision=item['revision'])


def test_recovery_http_rechecks_version_and_never_closes_before_success(tmp_path, inventory):
    import threading
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from unilabos.app.center_client.material_attention_api import create_router
    inv, _, _ = inventory
    with outcomes.material_workflow(tmp_path) as (store, _, durable, _, task, _, identity):
        complete(store, task, uid())
        bridge = MaterialBridge(Allocator(), store, authority(inv, durable, [10000]))
        pump_cycle(bridge)
        runtime = SimpleNamespace(materials=bridge, store=durable, _poll_lock=threading.RLock(),
                                  wake=lambda: None)
        app = FastAPI()
        app.include_router(create_router(lambda: runtime))
        client = TestClient(app)
        path = '/api/v1/center-material-exports'
        item = client.get(path).json()['data']['items'][0]
        result = client.post(f'{path}/{identity}/prepare/resume', json={'expected_revision': item['revision']})
        assert result.status_code == 200
        assert result.json()['data']['meta_data']['last_error']
        assert client.post(f'{path}/{identity}/prepare/resume', json={'expected_revision': item['revision']}).status_code == 409
        assert client.get(f'{path}/{identity}').json()['data']['items']


def test_runtime_snapshot_composes_material_items_without_reclassifying_native_errors(tmp_path, inventory):
    from types import SimpleNamespace

    from unilabos.app.center_client.runtime import CenterRuntime
    inv, _, _ = inventory
    with outcomes.material_workflow(tmp_path) as (store, _, durable, _, task, _, identity):
        complete(store, task, uid())
        bridge = MaterialBridge(Allocator(), store, authority(inv, durable, [10000]))
        pump_cycle(bridge)
        native = {'decision_kind': 'native-test'}
        runtime = CenterRuntime.__new__(CenterRuntime)
        runtime.service = SimpleNamespace(center_attention_snapshot=lambda: {'manual_confirmation_count': 0,
            'exception_count': 1, 'items': {'manual_confirmations': [], 'exceptions': [native]}})
        runtime.materials, runtime.handoffs = bridge, None
        snapshot = runtime.center_attention_snapshot()
        assert snapshot['exception_count'] == 2
        assert snapshot['items']['exceptions'][0] == native
        assert snapshot['items']['exceptions'][1]['decision_kind'] == 'material_export'


def test_successful_original_evidence_closes_material_attention(tmp_path, inventory):
    from tests.app.center_client.test_material_exports import bind
    inv, backend, template = inventory
    local = backend.create_material({'resource_template_uuid': template, 'name': 'sample', 'barcode': 'SAMPLE-1'})['uuid']
    with outcomes.material_workflow(tmp_path) as (store, _, durable, _, task, scheduler, identity):
        complete(store, task, local, inventory_store=inv)
        jobs, submissions = store.list_jobs(task['uuid']), list(scheduler.submissions)
        a = authority(inv, durable, [10000])
        client = Allocator()
        bridge = MaterialBridge(client, store, a)
        pump_cycle(bridge)
        item = bridge.export_attention_items()[0]
        assert item['phase'] == 'evidence'
        frozen = a.pending_intents()[0]['request']
        bridge.handoffs = bind(a, client, local)
        bridge.resume_failure(identity, 'evidence', expected_revision=item['revision'])
        assert bridge.export_attention_items()
        pump_cycle(bridge)
        assert bridge.export_attention_items() == []
        assert client.requests[0] == frozen
        assert store.get_task(task['uuid'])['status'] == 'succeeded'
        assert store.list_jobs(task['uuid']) == jobs and scheduler.submissions == submissions


def test_invalid_output_has_task_attention_without_an_inventory_export_intent(tmp_path, inventory):
    inv, _, _ = inventory
    with outcomes.material_workflow(tmp_path) as (store, _, durable, _, task, _, identity):
        complete(store, task, 'invalid-material-id')
        bridge = MaterialBridge(Allocator(), store, authority(inv, durable, [10000]))
        pump_cycle(bridge)
        items = bridge.export_attention_items()
        assert len(items) == 1 and items[0]['phase'] == 'output'
        assert items[0]['options'] == []
        assert items[0]['meta_data']['last_error'] == 'material_output_invalid'
        assert bridge.authority.export_failures() == []
        assert store.get_task(task['uuid'])['status'] == 'succeeded'
        with pytest.raises(ValueError):
            bridge.resume_failure(identity, 'output', expected_revision=1)


@pytest.mark.parametrize('invalid_output', [False, True])
def test_cancel_settlement_attention_closes_only_after_final_release_receipt(tmp_path, inventory, invalid_output):
    import json
    inv, _, _ = inventory
    with outcomes.material_workflow(tmp_path) as (store, _, durable, _, task, _, identity):
        complete(store, task, 'invalid-output' if invalid_output else uid())
        bridge = MaterialBridge(Allocator(), store, authority(inv, durable, [10000]))
        pump_cycle(bridge)
        assert bridge.export_attention_items()
        state = {'resources_released': True, 'release_receipt': None}
        with store.transaction() as c:
            c.execute('UPDATE center_outcome_evidence SET cancel_settlement_json=? WHERE invocation_uuid=?', (json.dumps(state), identity))
        assert bridge.export_attention_items(), 'physical release alone does not finish Center responsibility'
        state['release_receipt'] = {'resources_released': True}
        with store.transaction() as c:
            c.execute('UPDATE center_outcome_evidence SET cancel_settlement_json=? WHERE invocation_uuid=?', (json.dumps(state), identity))
        assert bridge.export_attention_items() == []
        assert store.get_task(task['uuid'])['status'] == 'succeeded'


@pytest.mark.parametrize('guard_kind',['evidence_only','store_conflict'])
def test_export_recovery_guard_is_a_conflict_and_preserves_readable_diagnostic(tmp_path,inventory,monkeypatch,guard_kind):
    import threading
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from unilabos.app.center_client.material_attention_api import create_router
    from unilabos.persistence.recovery_mode import enter
    from unilabos.workflow.store import StoreConflict
    inv,_,_=inventory
    with outcomes.material_workflow(tmp_path) as (store,_,durable,_,task,_,identity):
        complete(store,task,uid())
        bridge=MaterialBridge(Allocator(),store,authority(inv,durable,[10000]))
        pump_cycle(bridge)
        if guard_kind=='evidence_only':
            with durable._db.transaction():
                enter(durable._db,'恢复屏障禁止重启补报预算')
        else:
            def blocked():
                raise StoreConflict('补报仍受原任务恢复保护')
            monkeypatch.setattr(durable,'require_execution',blocked)
        wakes=[]
        runtime=SimpleNamespace(materials=bridge,store=durable,_poll_lock=threading.RLock(),wake=lambda:wakes.append(True))
        app=FastAPI()
        app.include_router(create_router(lambda:runtime))
        client=TestClient(app,raise_server_exceptions=False)
        path='/api/v1/center-material-exports'
        before=client.get(path).json()['data']['items'][0]
        result=client.post(f'{path}/{identity}/prepare/resume',json={'expected_revision':before['revision']})
        assert result.status_code==409
        after=client.get(f'{path}/{identity}')
        assert after.status_code==200
        assert after.json()['data']['items'][0]==before
        assert wakes==[]
