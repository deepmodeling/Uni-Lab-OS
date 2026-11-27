"""库存权威导出在 SQLite/PostgreSQL 上使用相同合同。"""
import pytest

from tests.app.center_client import test_material_export_retry as retry_contract
from tests.app.center_client import test_material_exports as contract


@pytest.fixture
def inventory(database_mode, tmp_path, monkeypatch):
    yield from contract.inventory.__wrapped__(tmp_path, monkeypatch)


def test_mapping_retry(database_mode, tmp_path, inventory):
    contract.test_inventory_version_mapping_survives_lost_response_and_restart(tmp_path, inventory)


def test_workflow_retry(database_mode, tmp_path, inventory, monkeypatch):
    contract.test_workflow_commit_failure_reuses_cached_mapping(tmp_path, inventory, monkeypatch)


def test_inventory_drift(database_mode, tmp_path, inventory):
    contract.test_inventory_drift_keeps_original_mapping_pending(tmp_path, inventory)


def test_container_configuration(database_mode, tmp_path, inventory):
    contract.test_unbound_container_keeps_mapping_and_waits_for_configuration(tmp_path, inventory)


@pytest.mark.parametrize("shape", ["many", "two_ports"])
@pytest.mark.parametrize("existing_intent", [False, True])
def test_duplicate_output_isolation(database_mode, tmp_path, inventory, shape, existing_intent):
    contract.test_duplicate_output_is_persistently_isolated_from_independent_exports(tmp_path, inventory, shape, existing_intent)


# 原表诊断字段和阶段预算必须在两种数据库上行为一致。


def test_prepare_failure_durability(database_mode, tmp_path, inventory, caplog):
    retry_contract.test_prepare_failure_survives_restart_and_stops_busy_retry(tmp_path, inventory, caplog)


def test_export_retry_deadline(database_mode, tmp_path, inventory):
    retry_contract.test_transient_failure_backoff_survives_restart_and_keeps_frozen_request(tmp_path, inventory)


@pytest.mark.parametrize("retryable,status_code", [(False, 409), (True, 503)])
def test_export_retry_budget(database_mode, tmp_path, inventory, retryable, status_code):
    retry_contract.test_blocked_or_exhausted_calls_require_explicit_resume(tmp_path, inventory, retryable, status_code)


def test_boundary_retry_budget(database_mode, tmp_path, inventory):
    retry_contract.test_boundary_failure_has_own_budget_and_never_rewrites_outcome(tmp_path, inventory)


@pytest.mark.parametrize("phase", ["evidence", "boundary"])
def test_success_diagnostic_atomicity(database_mode, tmp_path, inventory, monkeypatch, phase):
    retry_contract.test_success_acknowledgment_clears_diagnostic_in_same_transaction(tmp_path, inventory, monkeypatch, phase)


@pytest.mark.parametrize("retry_after,status,next_attempt", [
    (60, "retrying", 70_000), (9_999_999_999_999_999, "blocked", None),
])
def test_export_retry_after_bounds(database_mode, tmp_path, inventory, retry_after, status, next_attempt):
    retry_contract.test_retry_after_is_respected_without_blocking_other_pump_work(tmp_path, inventory, retry_after, status, next_attempt)


def test_independent_export_progress(database_mode, tmp_path, inventory):
    retry_contract.test_blocked_prepare_does_not_starve_an_independent_success(tmp_path, inventory)


def test_unprepared_export_is_not_sendable(database_mode, tmp_path, inventory):
    retry_contract.test_unprepared_attempt_cannot_accept_receipt_or_acknowledgment(tmp_path, inventory)


def test_unprepared_export_restart(database_mode, tmp_path, inventory):
    retry_contract.test_restart_after_attempt_creation_finishes_same_export(tmp_path, inventory)


def test_legacy_export_column_addition(database_mode, tmp_path, inventory):
    retry_contract.test_legacy_export_table_adds_only_retry_column_and_keeps_request(tmp_path, inventory)


def test_origin_failure_resume(database_mode, tmp_path, inventory):
    retry_contract.test_origin_resolution_failure_is_saved_before_prepare_and_can_resume(tmp_path, inventory)


def test_original_output_admission_required(database_mode, tmp_path, inventory):
    contract.test_raw_uuid_output_without_original_admission_cannot_allocate_or_export(tmp_path, inventory)


def test_local_history_protection_transfer(database_mode, tmp_path, inventory):
    contract.test_completed_outbound_can_transfer_to_verified_local_history_input_atomically(tmp_path, inventory)
