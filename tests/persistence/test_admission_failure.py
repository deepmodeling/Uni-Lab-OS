"""受理失败复用账本的原子性与补报合同同时适用于 SQLite/PostgreSQL。"""
import pytest

from tests.app.center_client import test_admission_failure as contract
from unilabos.workflow.store import WorkflowStore


@pytest.fixture(autouse=True)
def workflow_domain(database_mode, tmp_path):
    # 生产运行时由 WorkflowStore 初始化 PostgreSQL 兼容函数及领域表。
    store = WorkflowStore(tmp_path / "workflow_history.db")
    yield
    store.close()



def test_failure_uses_existing_outbox_and_stops_creation(database_mode, tmp_path):
    contract.test_uncreated_task_failure_is_reported_once_and_catalog_continues(tmp_path)


def test_failure_replays_original_event_after_restart(database_mode, tmp_path):
    contract.test_admission_failure_replays_exact_event_after_restart(tmp_path)


def test_failure_transaction_rollback(database_mode, tmp_path, monkeypatch):
    contract.test_failure_and_command_finish_roll_back_together(tmp_path, monkeypatch)


def test_terminal_evidence_blocks_recreation_without_inbox(database_mode, tmp_path):
    contract.test_terminal_outbox_blocks_recreation_when_invocation_row_was_lost(tmp_path)


def test_three_attempt_limit_is_persistent_and_does_not_block_other_commands(database_mode, tmp_path, monkeypatch):
    from tests.app.center_client.test_local_recovery_limit import (
        test_unknown_creation_conflict_stops_after_three_attempts_without_blocking_poll,
    )
    test_unknown_creation_conflict_stops_after_three_attempts_without_blocking_poll(tmp_path, monkeypatch)


def test_attempt_limit_survives_restart_and_repeated_notifications(database_mode, tmp_path, monkeypatch):
    from tests.app.center_client.test_local_recovery_limit import (
        test_recovery_count_survives_restart_and_duplicate_notification,
    )
    test_recovery_count_survives_restart_and_duplicate_notification(tmp_path, monkeypatch)
