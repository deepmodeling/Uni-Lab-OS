"""HTTP 完成回执先于 WebSocket started 时，库存占用不能被复活。"""
import pytest

from tests.app.test_inventory_dispatch_admission import _request, station_inventory


@pytest.mark.parametrize('settled_state', ['released', 'uncertain'])
def test_late_started_preserves_settled_inventory_claim(station_inventory, settled_state):
    store, service, identities = station_inventory
    permit = service.station_resources.acquire_dispatch_permit(_request(identities)).permit
    service.station_resources.transition_dispatch_permit(permit.claim_uuid, target_state='reserved')
    service.station_resources.transition_dispatch_permit(permit.claim_uuid, target_state=settled_state)
    before = [dict(row) for row in store.query_all('SELECT * FROM station_execution_lock_lease WHERE claim_uuid=?', (permit.claim_uuid,))]
    service.station_resources.transition_dispatch_permit(permit.claim_uuid, target_state='running')
    assert store.query_one('SELECT state FROM station_execution_claim WHERE claim_uuid=?', (permit.claim_uuid,))['state'] == settled_state
    assert [dict(row) for row in store.query_all('SELECT * FROM station_execution_lock_lease WHERE claim_uuid=?', (permit.claim_uuid,))] == before
