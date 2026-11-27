"""取消结算崩溃恢复在 SQLite 与真实 PostgreSQL 上使用相同合同。"""
import pytest

from tests.app.center_client import test_material_cancel_settlement as original
from tests.app.center_client import test_material_exports as exports
from tests.app.center_client import test_material_settlement_recovery as recovery
from tests.persistence.test_station_handoff import handoff_stations  # noqa: F401


@pytest.fixture(autouse=True)
def isolate_recovery_guard(database_mode):
    # database_mode 会截断测试库。单例安全门空表按设计拒绝执行，不能遗留给下一用例。
    if database_mode == 'postgresql':
        import psycopg

        from unilabos.config.config import BasicConfig
        with psycopg.connect(BasicConfig.database_url, autocommit=True) as c:
            c.execute('DROP TABLE IF EXISTS center_recovery_mode')
    yield
    if database_mode == 'postgresql':
        with psycopg.connect(BasicConfig.database_url, autocommit=True) as c:
            c.execute('DROP TABLE IF EXISTS center_recovery_mode')


@pytest.fixture
def inventory(database_mode, tmp_path, monkeypatch):
    yield from exports.inventory.__wrapped__(tmp_path, monkeypatch)


def test_original_success_fact(database_mode, tmp_path, inventory):
    original.test_succeeded_material_cancel_settlement_keeps_original_result_and_releases_only_original_hold(tmp_path, inventory)


def test_inventory_commit_crash(database_mode, tmp_path, inventory, monkeypatch):
    recovery.test_inventory_commit_before_workflow_save_reuses_exact_original_proof(tmp_path, inventory, monkeypatch)


def test_lost_final_ack(database_mode, tmp_path, inventory):
    recovery.test_final_center_ack_lost_replays_same_request_without_releasing_new_work(tmp_path, inventory)


def test_task_recovery_hold(database_mode, tmp_path, inventory):
    recovery.test_execution_hold_rejects_before_sending_or_releasing(tmp_path, inventory)


def test_release_is_atomic(database_mode, tmp_path, inventory):
    recovery.test_unsafe_output_keeps_all_original_source_bindings_atomic(tmp_path, inventory)


def test_actual_dispatch_hold(database_mode, tmp_path, inventory):
    recovery.test_actual_dispatch_action_keeps_original_source_and_output_protected(tmp_path, inventory)


def test_operator_api(database_mode, tmp_path, inventory):
    recovery.test_settlement_api_replays_original_body_rejects_forged_release_and_exposes_progress(tmp_path, inventory)


@pytest.fixture
def received(database_mode, handoff_stations, tmp_path, monkeypatch):
    from tests.app import test_station_material_inputs as inputs
    monkeypatch.setattr(inputs, 'handoff_inventory_fixture', lambda _: iter([handoff_stations]))
    yield from inputs.received.__wrapped__(tmp_path)


@pytest.mark.parametrize('quarantined', [False, True])
def test_original_input_receipt(database_mode, tmp_path, received, monkeypatch, quarantined):
    recovery.test_original_material_input_receipt_finishes_in_both_stores(tmp_path, received, monkeypatch, quarantined)


def test_input_inventory_commit_crash(database_mode, tmp_path, received, monkeypatch):
    recovery.test_input_inventory_commit_crash_recovers_original_workflow_receipt(tmp_path, received, monkeypatch)


@pytest.mark.parametrize('protection', ['evidence_only', 'binding_conflict'])
def test_operator_recovery_protection(database_mode, tmp_path, inventory, protection):
    recovery.test_api_preserves_query_and_returns_conflict_for_recovery_protection(tmp_path, inventory, protection)


def test_original_input_body_proof(database_mode, tmp_path, received, monkeypatch):
    recovery.test_original_input_body_proof_cannot_be_bypassed_by_source_cleanup(tmp_path, received, monkeypatch)


def test_completed_settlement_is_not_executable_backlog(database_mode, tmp_path, inventory):
    recovery.test_completed_settlement_leaves_no_executable_recovery_or_export_work(tmp_path, inventory)


def test_cancel_preserves_other_shared_source_users(database_mode, tmp_path, inventory):
    recovery.test_cancel_releases_own_shared_source_without_blocking_other_shared_users(tmp_path, inventory)
