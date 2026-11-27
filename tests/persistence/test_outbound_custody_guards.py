"""SQLite/PostgreSQL 对真实载体保管与专用出站保护实施相同门禁。"""

import pytest

from tests.app import test_outbound_protection as contract
from tests.persistence.test_station_handoff import handoff_stations  # noqa: F401


def test_transfer_endpoint_does_not_expand_into_other_task(handoff_stations):
    contract.test_frozen_transfer_endpoint_preserves_unrelated_custody(handoff_stations)


def test_dynamic_uuid_requires_production_ledger(handoff_stations):
    contract.test_runtime_dynamic_output_admission_excludes_plain_input_uuid(handoff_stations)


def test_carrier_admission_rejects_used_descendants(handoff_stations):
    contract.test_exclusive_carrier_admission_rejects_foreign_shared_descendant(handoff_stations)


def test_custody_blocks_descendant_admission_and_dispatch(handoff_stations):
    contract.test_exclusive_carrier_blocks_later_foreign_child_admission_and_action(handoff_stations)


def test_custody_blocks_public_descendant_mutation(handoff_stations):
    contract.test_exclusive_carrier_blocks_public_descendant_mutation(handoff_stations)


@pytest.mark.parametrize("operation", ["reserve", "release", "consume", "quarantine"])
def test_generic_api_cannot_remove_outbound_protection(handoff_stations, operation):
    contract.test_generic_reservation_operations_cannot_change_outbound_namespace(handoff_stations, operation)


def test_command_cannot_remove_outbound_protection(handoff_stations):
    contract.test_command_release_cannot_remove_outbound_protection(handoff_stations)


def test_original_contents_remain_in_exclusive_custody(handoff_stations):
    contract.test_exclusive_source_retains_original_descendant_after_owner_rearrangement(handoff_stations)


@pytest.mark.parametrize('reservation_state', ['active', 'released'])
def test_cancel_settlement_retains_unsafe_original_contents(handoff_stations, reservation_state):
    contract.test_cancel_settlement_cannot_release_detached_unsafe_original_content(handoff_stations, reservation_state)
