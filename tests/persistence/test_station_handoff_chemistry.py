"""化学目录冻结保护复用 SQLite/PG 的独立连接合同。"""

import pytest

from tests.app import test_station_handoff_chemistry as contract
from tests.persistence.test_station_handoff import handoff_stations as handoff_stations


@pytest.mark.parametrize("side", ["source", "target"])
@pytest.mark.parametrize("edit", [{"cas": "64-17-5"}, {"density_g_per_ml": 0.789}, {"physical_state": "solid"}])
def test_protected_catalog_edit(handoff_stations, side, edit):
    contract.test_protected_reagent_identity_cannot_be_reinterpreted(handoff_stations, side, edit)


@pytest.mark.parametrize("side", ["source", "target"])
def test_protected_catalog_delete(handoff_stations, side):
    contract.test_protected_reagent_identity_cannot_be_deleted(handoff_stations, side)


@pytest.mark.parametrize("side", ["source", "target"])
def test_catalog_edit_serializes_with_handoff(handoff_stations, monkeypatch, side):
    contract.test_catalog_edit_waiting_on_freeze_rechecks_protection(handoff_stations, monkeypatch, side)
