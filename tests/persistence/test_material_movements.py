"""出入站查询在两种数据库上复用真实交接回归。"""
import pytest

from tests.app import test_material_movements as contract
from tests.persistence.test_station_handoff import handoff_stations  # noqa: F401


@pytest.mark.parametrize('case', [
    'test_received_material_remains_current_and_has_durable_inbound_record',
    'test_departure_requires_pickup_or_receipt_and_replays_are_deduplicated',
    'test_receipt_confirms_departure_without_inventing_pickup_time',
    'test_cancel_is_not_presented_as_successful_receipt',
    'test_http_filters_pagination_and_repeated_carrier_history',
])
def test_movement_history(handoff_stations, case):
    getattr(contract, case)(handoff_stations)
