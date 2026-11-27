"""同一真实事务契约分别通过 SQLite 与独立 PostgreSQL 数据库验收。"""

import pytest

from tests.app.center_client import test_atomic_workflow_events as atomic


@pytest.mark.parametrize("pure,terminal", [(False, False), (True, False), (True, True)])
def test_submission_rollback(database_mode, tmp_path, monkeypatch, pure, terminal):
    atomic.test_submission_outbox_failure_rolls_back_task_jobs_binding_and_sequence(tmp_path, monkeypatch, pure, terminal)


def test_immediate_boundary(database_mode, tmp_path):
    atomic.test_pure_boundary_commits_accepted_result_and_outcome_together(tmp_path)


def test_completion_rollback(database_mode, tmp_path, monkeypatch):
    atomic.test_job_completion_outbox_failure_rolls_back_results_and_all_logs(tmp_path, monkeypatch)


@pytest.mark.parametrize("terminal_path", ["failed", "timeout", "direct_cancel", "device_canceled", "restart_failed"])
def test_terminal_outcome(database_mode, tmp_path, terminal_path):
    atomic.test_terminal_paths_persist_one_outcome_from_actual_task(tmp_path, terminal_path)


def test_cancel_uncertainty(database_mode, tmp_path):
    atomic.test_cancel_awaiting_device_does_not_invent_terminal_outcome(tmp_path)


def test_partial_completion(database_mode, tmp_path):
    atomic.test_nonfinal_job_does_not_emit_outcome(tmp_path)


def test_restart_without_resubmission(database_mode, tmp_path):
    atomic.test_network_loss_and_restart_never_resubmit_committed_task(tmp_path)


def test_wrong_database(database_mode, tmp_path):
    atomic.test_center_store_in_another_database_cannot_create_workflow_task(tmp_path)


def test_soft_deleted_identity(database_mode, tmp_path):
    atomic.test_soft_deleted_task_keeps_permanent_center_identity(tmp_path)


@pytest.mark.parametrize("pure", [False, True])
def test_local_without_center(database_mode, tmp_path, pure):
    atomic.test_local_workflow_works_without_center_schema(tmp_path, pure)


@pytest.mark.parametrize("value", [1.0, 1e20])
def test_binary64_package_binding(database_mode, tmp_path, value):
    atomic.test_binary64_input_survives_binding_and_recovery(tmp_path, value)


@pytest.mark.parametrize("first_value,replay_value", [(1.0, 1), (1, 1.0)])
def test_equivalent_input_replay(database_mode, tmp_path, first_value, replay_value):
    atomic.test_equivalent_json_notification_keeps_first_frozen_input(tmp_path, first_value, replay_value)


@pytest.mark.parametrize("pure", [False, True])
def test_legacy_recovery(database_mode, tmp_path, monkeypatch, pure):
    atomic.test_recover_legacy_task_events_is_atomic_and_never_schedules(tmp_path, monkeypatch, pure)


def test_unconfirmed_claim(database_mode, tmp_path):
    atomic.test_unconfirmed_claim_cannot_bind_or_create_task(tmp_path)


def test_conflicting_request(database_mode, tmp_path):
    atomic.test_conflicting_permanent_request_cannot_be_adopted(tmp_path)


def test_conflicting_outcome(database_mode, tmp_path):
    atomic.test_recovery_cannot_rewrite_already_recorded_outcome(tmp_path)


@pytest.mark.parametrize("pure", [False, True])
def test_empty_output_boundary(database_mode, tmp_path, pure):
    atomic.test_empty_output_contract_has_real_atomic_boundary(tmp_path, pure)


@pytest.mark.parametrize("pure", [False, True])
def test_empty_output_rollback(database_mode, tmp_path, monkeypatch, pure):
    atomic.test_empty_output_contract_rolls_back_when_outcome_write_fails(tmp_path, monkeypatch, pure)


def test_composition_upgrades_legacy_ledger(database_mode, tmp_path, monkeypatch):
    atomic.test_composition_upgrades_accepted_legacy_task_before_recovery_with_center_disabled(tmp_path, monkeypatch)
