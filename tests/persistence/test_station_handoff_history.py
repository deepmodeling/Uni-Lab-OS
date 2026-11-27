"""复杂历史库存合同复用双后端夹具；PG 按当前任务约定延期执行。"""
import pytest

from tests.app import test_station_handoff_history as contract
from tests.persistence.test_station_handoff import handoff_stations


@pytest.mark.parametrize("change", ["unknown_unit", "unknown_catalog", "wrong_chemistry", "bad_origin", "duplicate", "over_capacity", "unknown_concentration", "missing_concentration_value", "missing_concentration_unit", "nonstring_concentration_unit"])
def test_historical_substance_invalid_mapping(handoff_stations, change):
    contract.test_invalid_historical_substance_rolls_back_everything(handoff_stations, change)


def test_historical_substance_guards(handoff_stations):
    contract.test_substance_freeze_and_catalog_changes_are_not_reinterpreted(handoff_stations)


@pytest.mark.parametrize("kind", ["substance", "historical_sample", "empty_reagent"])
def test_historical_restart(handoff_stations, kind):
    contract.test_historical_prepare_restart_replays_original_inventory(handoff_stations, kind)


def test_zero_reagent_creation_guard(handoff_stations):
    contract.test_zero_reagent_does_not_relax_ordinary_creation(handoff_stations)


@pytest.mark.parametrize("legacy_missing_identity", [False, True])
def test_first_export_chemical_evidence(handoff_stations, legacy_missing_identity):
    contract.test_first_export_never_guesses_historical_chemical_identity(handoff_stations, legacy_missing_identity)
