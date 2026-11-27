"""本轮逐件转运合同在两种真实数据库上执行；每站使用独立库存。"""
from __future__ import annotations

import pytest

from tests.app import test_handoff_placement as handoff
from tests.app import test_handoff_placement_api as api
from tests.app import test_inventory_selection_removed as removed
from tests.persistence.test_station_handoff import handoff_stations  # noqa: F401


@pytest.mark.parametrize("case", [
    "test_preselected_target_is_reserved_without_prompt_or_fallback",
    "test_occupied_selected_target_rolls_back_incoming_and_does_not_choose_other_empty_site",
    "test_planned_handoff_prepares_without_user_resubmitting_receiver_position",
    "test_two_items_keep_pairing_when_rows_reordered_and_restart",
    "test_last_item_failure_rolls_back_entire_batch",
    "test_pickup_cannot_replace_prepared_execution_plan",
    "test_automatic_preparation_uses_the_real_inventory_adapter",
    "test_partial_receipt_restart_and_late_cancel_keep_received_fact_and_remaining_protection",
    "test_trusted_pickup_releases_only_source_location_and_late_snapshot_does_not_relock",
    "test_concurrent_handoffs_compete_for_exact_target_without_fallback",
])
def test_handoff_placement_contract(handoff_stations, case):
    getattr(handoff, case)(handoff_stations)


@pytest.mark.parametrize("case", [
    "test_target_can_be_selected_before_source_workflow_has_produced_material",
    "test_target_rejects_incompatible_template_and_reports_the_selection_row",
    "test_validation_route_requires_operator_and_uses_its_own_session",
    "test_standard_center_inventory_router_exposes_the_same_read_only_validation",
])
def test_placement_api_contract(handoff_stations, case):
    getattr(api, case)(handoff_stations)


@pytest.mark.parametrize("scope", ["material_site", "device"])
@pytest.mark.parametrize("case", [
    "test_source_release_rechecks_site_and_owner_job_claim_after_preflight",
    "test_released_source_location_stays_protected_until_trusted_pickup",
])
def test_source_claim_contract(handoff_stations, scope, case):
    getattr(handoff, case)(handoff_stations, scope)


@pytest.mark.parametrize("role", ["source", "target"])
def test_removed_inventory_selection_contract(handoff_stations, role):
    removed.test_inventory_selection_is_rejected_without_reading_or_changing_stock(handoff_stations, role)
    api.test_operator_route_rejects_removed_inventory_selection(handoff_stations, role)
