"""出入站历史来自持久交接凭据，不依赖物料当前库存状态。"""
import json
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.app.test_station_handoff import frozen, package, prepared, receive, setup, uid  # noqa: F401
from tests.app.test_station_handoff_adapter import settle
from tests.app.test_station_handoff_pickup import picked_up
from unilabos.app.scheduler.inventory.backend_api import install_backend_resource_api
from unilabos.app.scheduler.inventory.backend_contract import BackendResourceService


def test_received_material_remains_current_and_has_durable_inbound_record(setup):
    s = setup
    frozen(s)
    target = BackendResourceService(s['stores']['target'])
    assert target.list_material_movements()['total'] == 0
    receipt = receive(s)
    settle(s, receipt)
    # 重放收货、中心确认均不能产生重复记录。
    assert receive(s) == receipt
    settle(s, receipt)
    result = target.list_material_movements(direction='inbound')
    assert result['total'] == 1
    record = result['items'][0]
    assert record['material_uuid'] == receipt['target_snapshot']['carrier_material_uuid']
    assert record['occurred_at'] == receipt['target_snapshot']['received_at']
    assert record['result'] == 'succeeded'
    assert record['site_uuid'] == s['sites']['target']
    assert record['source_station_uuid'] == s['ids']['source']
    assert target.get_material(record['material_uuid'])['inventory_presence'] == 'current'
    assert target.material_graph(inventory_scope='history')['nodes'] == []
    # 改名不覆盖交接时间；软删除也必须能追溯收货。
    with s['stores']['target'].transaction() as c:
        c.execute("UPDATE material SET deleted_at='2030-01-01',update_time='2030-01-01' WHERE uuid=?", (record['material_uuid'],))
    assert target.list_material_movements()['items'][0]['occurred_at'] == record['occurred_at']


def test_departure_requires_pickup_or_receipt_and_replays_are_deduplicated(setup):
    s = setup
    frozen(s)
    source = BackendResourceService(s['stores']['source'])
    assert source.list_material_movements()['total'] == 0
    s["clock"][0] += 123
    payload = picked_up(s)
    for _ in range(2):
        s['auth']['source'].apply_center_snapshot(package(payload))
    result = source.list_material_movements(direction='outbound')
    assert result['total'] == 1
    assert result['items'][0]['time_basis'] == 'pickup'
    assert result['items'][0]['occurred_at'] == datetime.fromtimestamp(s['clock'][0] / 1000, timezone.utc).isoformat()
    assert result['items'][0]['site_uuid'] == s['sites']['source']
    assert source.list_material_movements(direction='inbound')['total'] == 0


def test_receipt_confirms_departure_without_inventing_pickup_time(setup):
    s = setup
    frozen(s)
    receipt = receive(s)
    settle(s, receipt)
    record = BackendResourceService(s['stores']['source']).list_material_movements()['items'][0]
    assert record['time_basis'] == 'receipt_confirmation'
    assert record['occurred_at'] == receipt['target_snapshot']['received_at']


def test_cancel_is_not_presented_as_successful_receipt(setup):
    s = setup
    prepared(s)
    s['payload'].update(status='canceling', revision=3)
    s['auth']['target'].apply_center_snapshot(package(s['payload']))
    record = BackendResourceService(s['stores']['target']).list_material_movements()['items'][0]
    assert record['direction'] == 'inbound'
    assert record['result'] == 'canceled'
    assert record['time_basis'] == 'cancellation'
    assert record['occurred_at'] is not None


def test_http_filters_pagination_and_repeated_carrier_history(setup):
    s = setup
    frozen(s)
    receipt = receive(s)
    settle(s, receipt)
    store = s['stores']['target']
    # 模拟同一只瓶子的第二笔已持久化交接；记录身份按交接明细，而非物料去重。
    with store.transaction() as c:
        h = dict(c.execute('SELECT * FROM station_handoff').fetchone())
        i = dict(c.execute('SELECT * FROM station_handoff_item').fetchone())
        h['handoff_uuid'], i['handoff_item_uuid'] = uid(), uid()
        i['handoff_uuid'], i['reservation_uuid'] = h['handoff_uuid'], None
        payload = json.loads(h['package_json'])
        payload['handoff_uuid'] = h['handoff_uuid']
        payload['items'][0]['handoff_item_uuid'] = i['handoff_item_uuid']
        h['package_json'] = json.dumps(payload)
        for table, values in [('station_handoff', h), ('station_handoff_item', i)]:
            c.execute(f"INSERT INTO {table} ({','.join(values)}) VALUES ({','.join('?' for _ in values)})", tuple(values.values()))
    app = FastAPI()
    install_backend_resource_api(app, BackendResourceService(store))
    with TestClient(app) as client:
        path = '/api/v1/materials/movements'
        first = client.get(path, params={'direction':'inbound','keyword':'BKR-1','page_size':1}).json()['data']
        second = client.get(path, params={'direction':'inbound','keyword':'BKR-1','page_size':1,'page':2}).json()['data']
        assert first['total'] == second['total'] == 2
        assert first['items'][0]['record_id'] != second['items'][0]['record_id']
        assert first['items'][0]['material_uuid'] == second['items'][0]['material_uuid']
        assert client.get(path, params={'keyword':'missing'}).json()['data']['total'] == 0
        assert client.get(path, params={'material_uuid':uid()}).json()['data']['total'] == 0
        assert client.get(path, params={'direction':'invalid'}).json()['code'] != 0
        assert client.get(path, params={'page_size':101}).json()['code'] != 0


def test_graph_includes_template_categories_for_reload_eligibility(setup):
    s = setup
    backend = BackendResourceService(s['stores']['source'])
    templates = backend.sync_resource_templates([{
        'id':'new.sample_vial', 'display_name':'页面新建样品瓶',
        'registry_type':'resource', 'category':['container','sample_vial'], 'class':{},
    }])['templates']
    created = backend.create_material({'resource_template_uuid':templates[0]['uuid'],'name':'新瓶'})
    node = next(n for n in backend.material_graph()['nodes'] if n['material']['uuid'] == created['uuid'])
    assert node['material']['type'] == 'resource'
    assert node['resource_template']['tags'] == ['container','sample_vial']
