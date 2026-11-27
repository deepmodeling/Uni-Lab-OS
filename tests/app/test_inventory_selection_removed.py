"""页面库存选择关闭后，工站仍允许上游产物提前选择收货位。"""
from dataclasses import asdict

import pytest

from tests.app.placement_test_support import inventory_rows
from tests.app.test_handoff_placement_api import request, session
from tests.app.test_station_handoff import setup  # noqa: F401
from unilabos.app.scheduler.inventory.placement import validate_inventory_placement


@pytest.mark.parametrize("role", ["source", "target"])
def test_inventory_selection_is_rejected_without_reading_or_changing_stock(setup, role):
    s = setup
    before = inventory_rows(s["stores"][role])
    with pytest.raises(ValueError, match="库存直选已移除"):
        validate_inventory_placement(s["auth"][role], request(s, role), session=asdict(session(s, role)))
    assert inventory_rows(s["stores"][role]) == before
    assert not s["auth"][role].pending_intents()


def test_upstream_destination_preflight_remains_read_only(setup):
    s = setup
    before = inventory_rows(s["stores"]["target"])
    proof = validate_inventory_placement(s["auth"]["target"], request(s, destinations=True), session=asdict(session(s, "target")))
    assert proof["can_run"] is True
    assert proof["items"][0]["target_site_uuid"] == s["sites"]["target"]
    assert inventory_rows(s["stores"]["target"]) == before
