"""HTTP 交接必须进入库存权威，错误条码不能生成收货事实。"""
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.app.test_station_handoff import frozen, handoff_inventory_fixture, package
from unilabos.app.center_client.inventory_api import create_router
from unilabos.workflow.material_ports import MaterialPortError, ports_from_graph


def test_receiver_uses_center_snapshot_and_rejects_wrong_scan(tmp_path):
    fixture = handoff_inventory_fixture(tmp_path)
    s = next(fixture)
    calls = []
    try:
        frozen(s)
        def snapshot(identity, revision, epoch):
            calls.append(identity)
            assert identity == s['ids']['handoff']
            assert epoch == s['ids']['target_epoch']
            return package(s['payload'])
        runtime = SimpleNamespace(store=SimpleNamespace(require_execution=lambda: None), handoffs=SimpleNamespace(
            client=SimpleNamespace(handoff_package=snapshot), store=SimpleNamespace(event_epoch_uuid=s['ids']['target_epoch']),
            authority=s['auth']['target']))
        app = FastAPI()
        app.include_router(create_router(lambda: runtime))
        client = TestClient(app)
        path = f"/api/v1/center-handoffs/{s['ids']['handoff']}/items/{s['ids']['item']}/receive"
        body = {'barcode_namespace': 'lab', 'barcode': 'wrong', 'evidence_refs': ['sim://scan']}
        assert client.post(path, json=body).status_code == 409
        body['barcode'] = 'BKR-1'
        response = client.post(path, json=body)
        assert response.status_code == 200, response.text
        assert client.post(path, json=body).json() == response.json()
        assert calls == [s['ids']['handoff']] * 3
    finally:
        fixture.close()


def test_public_material_ports_preserve_legacy_and_reject_disagreement():
    port = {'name': 'sample', 'direction': 'output', 'value_path': '$.sample', 'local_value_kind': 'material_uuid',
            'cardinality': 'one', 'resource_template_uuid': str(uuid4())}
    metadata = {'unilab': {'input_contract': {'parameters': []}, 'output_contract': {'outputs': [{'name': 'sample', 'schema': {'type': 'string'}}]}}}
    graph = {'workflow': {'meta_data': metadata}}
    assert ports_from_graph(graph) == []
    metadata['center_material_ports'] = [port]
    assert ports_from_graph(graph) == [port]
    metadata['unilab']['material_ports'] = [port]
    assert ports_from_graph(graph) == [port]
    metadata['unilab']['material_ports'] = []
    with pytest.raises(MaterialPortError, match='不一致'):
        ports_from_graph(graph)


def test_handoff_detail_reports_real_protection_and_reservation_state(tmp_path):
    fixture = handoff_inventory_fixture(tmp_path)
    s = next(fixture)
    try:
        frozen(s)
        runtime = SimpleNamespace(store=SimpleNamespace(require_execution=lambda: None),
            handoffs=SimpleNamespace(authority=s['auth']['target']))
        app = FastAPI()
        app.include_router(create_router(lambda: runtime))
        result = TestClient(app).get(f"/api/v1/center-handoffs/{s['ids']['handoff']}")
        assert result.status_code == 200
        data = result.json()['data']
        assert data['items'][0]['reservation']['state'] == 'reserved'
        assert data['items'][0]['receipt'] is None
        assert data['items'][0]['settlement'] is None
        assert data['items'][0]['protection_active'] is True
    finally:
        fixture.close()


def test_attention_lists_canceling_handoff_with_real_local_progress(tmp_path):
    from unilabos.app.center_client.websocket import validate_attention_snapshot
    from unilabos.app.scheduler.inventory.handoff_attention import attention_handoffs
    fixture = handoff_inventory_fixture(tmp_path)
    s = next(fixture)
    try:
        frozen(s)
        assert attention_handoffs(s['auth']['target']) == []
        payload = dict(s['payload'], revision=s['payload']['revision'] + 1, status='canceling')
        s['auth']['target'].apply_center_snapshot(package(payload))
        items = attention_handoffs(s['auth']['target'])
        assert len(items) == 1
        assert 'workflow_node_job_uuid' not in items[0]
        assert items[0]['meta_data']['progress']['items'][0]['reservation']['site_active'] == 0
        validate_attention_snapshot({'manual_confirmation_count': 0, 'exception_count': 1,
            'items': {'manual_confirmations': [], 'exceptions': items}})
    finally:
        fixture.close()


def test_handoff_queries_remain_available_under_execution_recovery_protection(tmp_path):
    fixture = handoff_inventory_fixture(tmp_path)
    s = next(fixture)
    try:
        frozen(s)
        def held():
            raise ValueError('execution recovery held')
        runtime = SimpleNamespace(store=SimpleNamespace(require_execution=held),
            handoffs=SimpleNamespace(authority=s['auth']['target']))
        app = FastAPI()
        app.include_router(create_router(lambda: runtime))
        client = TestClient(app, raise_server_exceptions=False)
        assert client.get(f"/api/v1/center-handoffs/{s['ids']['handoff']}").status_code == 200
        assert client.get('/api/v1/center-handoffs/attention/current').status_code == 200
    finally:
        fixture.close()


@pytest.mark.parametrize('guard_kind',['evidence_only','store_conflict'])
def test_handoff_recovery_posts_map_guard_conflicts_and_leave_queries_available(tmp_path,monkeypatch,guard_kind):
    from unilabos.app.center_client.durable import DurableCenterStore
    from unilabos.persistence.recovery_mode import enter
    from unilabos.workflow.store import StoreConflict
    fixture=handoff_inventory_fixture(tmp_path)
    s=next(fixture)
    durable=DurableCenterStore(tmp_path/'handoff-recovery.db',station_uuid=s['ids']['target'],instance_uuid=s['ids']['target_instance'])
    try:
        frozen(s)
        if guard_kind=='evidence_only':
            with durable._db.transaction():
                enter(durable._db,'恢复屏障下交接操作仍须明确拒绝并可查询')
        else:
            def blocked():
                raise StoreConflict('交接恢复与原执行责任冲突')
            monkeypatch.setattr(durable,'require_execution',blocked)
        runtime=SimpleNamespace(store=durable,handoffs=SimpleNamespace(authority=s['auth']['target']))
        app=FastAPI()
        app.include_router(create_router(lambda:runtime))
        client=TestClient(app,raise_server_exceptions=False)
        base='/api/v1/center-handoffs/'+s['ids']['handoff']
        assert client.get(base).status_code==200
        assert client.get('/api/v1/center-handoffs/attention/current').status_code==200
        result=client.post(base+'/source-ready',json={'physical_ready':True,'evidence_refs':['scan:1']})
        assert result.status_code==409
    finally:
        durable.close()
        fixture.close()
