"""中心事实与工作流事实遵循同一 SQLite/PostgreSQL 选择及重置。"""

import pytest

from tests.app.center_client.test_center_client import INSTANCE, STATION
from tests.app.center_client.test_event_envelope import (
    test_binary64_result_restarts_and_receives_exact_ack as exercise_binary64,
)
from tests.app.center_client.test_event_envelope import (
    test_conflict_ack_is_quarantined_durably_without_claiming_success as exercise_conflict,
)
from tests.app.center_client.test_event_envelope import (
    test_event_transaction_rolls_back_sequence_and_body as exercise_atomic_sequence,
)
from tests.app.center_client.test_event_envelope import (
    test_legacy_jcs_float_output_migration_preserves_digest_and_identity as exercise_legacy_float,
)
from tests.app.center_client.test_event_envelope import (
    test_legacy_jcs_output_rejects_inexact_integer_without_rewriting as exercise_legacy_inexact,
)
from tests.app.center_client.test_event_envelope import (
    test_legacy_outbox_migration_keeps_evidence_and_acknowledgement as exercise_migration,
)
from tests.app.center_client.test_event_envelope import (
    test_migration_epoch_mismatch_rolls_back_without_rewriting_history as exercise_bad_epoch,
)
from tests.app.center_client.test_event_envelope import (
    test_two_connections_serialize_event_numbers as exercise_concurrent_sequence,
)
from tests.app.center_client.test_runtime import (
    test_actual_public_service_frozen_contract_and_result as exercise_public_service,
)
from tests.app.center_client.test_runtime import (
    test_result_replay_keeps_exact_event_after_restart_and_fencing as exercise_replay,
)
from unilabos.app.center_client.durable import TABLES, DurableCenterStore
from unilabos.persistence.config import configured_settings
from unilabos.persistence.workspace import reset_postgresql_domain


def test_center_public_workflow_round_trip(database_mode, tmp_path):
    exercise_public_service(tmp_path)
    if database_mode == "postgresql":
        assert not list(tmp_path.glob("*.db*"))


def test_center_outbox_restart_and_fencing(database_mode, tmp_path):
    exercise_replay(tmp_path)


def test_event_envelope_migration(database_mode, tmp_path):
    exercise_migration(tmp_path)


def test_event_envelope_migration_rolls_back_invalid_epoch(database_mode, tmp_path):
    exercise_bad_epoch(tmp_path)


def test_event_sequence_and_outbox_are_atomic(database_mode, tmp_path, monkeypatch):
    exercise_atomic_sequence(tmp_path, monkeypatch)


def test_event_sequence_is_serialized_between_connections(database_mode, tmp_path):
    exercise_concurrent_sequence(tmp_path)


def test_conflict_ack_is_preserved_and_not_retried(database_mode, tmp_path):
    exercise_conflict(tmp_path)


@pytest.mark.parametrize("value", [1e16, 1e20])
def test_binary64_outbox_restart_and_exact_ack(database_mode, tmp_path, value):
    exercise_binary64(tmp_path, value)


@pytest.mark.parametrize("value", [1e16, 1e20])
def test_legacy_jcs_float_output_upgrade(database_mode, tmp_path, value):
    exercise_legacy_float(tmp_path, value)


@pytest.mark.parametrize("value", [2**53 + 1, 10**20 + 1, 10**400])
def test_legacy_jcs_float_output_rejects_rounding(database_mode, tmp_path, value):
    exercise_legacy_inexact(tmp_path, value)


def test_center_epoch_reset_with_workflow_database(database_mode, tmp_path):
    path = tmp_path / "workflow_history.db"
    store = DurableCenterStore(path, station_uuid=STATION, instance_uuid=INSTANCE)
    original = store.event_epoch_uuid
    store.close()
    if database_mode == "postgresql":
        truncated = reset_postgresql_domain(configured_settings())
        assert set(TABLES).issubset(truncated)
    else:
        path.unlink()
    restarted = DurableCenterStore(path, station_uuid=STATION, instance_uuid=INSTANCE)
    assert restarted.event_epoch_uuid != original
    restarted.close()


def test_mixed_handoff_and_workflow_sequence_after_restart(database_mode, tmp_path):
    from tests.app.center_client.test_handoff_events import (
        test_mixed_events_preserve_one_contiguous_sequence_after_restart as exercise,
    )
    exercise(tmp_path)


def test_mixed_handoff_and_workflow_writers(database_mode, tmp_path):
    from tests.app.center_client.test_handoff_events import test_mixed_writers_share_transaction_sequence as exercise
    exercise(tmp_path)


def test_handoff_event_write_is_atomic(database_mode, tmp_path, monkeypatch):
    from tests.app.center_client.test_handoff_events import (
        test_handoff_write_failure_rolls_back_body_and_shared_sequence as exercise,
    )
    exercise(tmp_path, monkeypatch)
