"""物料结果引用的 SQLite/PostgreSQL 同一存储合同。"""

from tests.app.center_client.test_material_outcomes import (
    test_evidence_binding_and_atomic_outbox_failure as exercise_atomic,
)
from tests.app.center_client.test_material_outcomes import (
    test_invalid_material_output_preserves_physical_completion as exercise_invalid,
)
from tests.app.center_client.test_material_outcomes import (
    test_material_outcome_waits_for_evidence_without_reexecuting_physical_job as exercise_completion,
)
from tests.app.center_client.test_material_outcomes import (
    test_pending_cancel_has_no_invented_start_and_old_outcome_is_never_rewritten as exercise_legacy,
)


def test_completion(database_mode, tmp_path):
    exercise_completion(tmp_path)


def test_atomic_evidence(database_mode, tmp_path, monkeypatch):
    exercise_atomic(tmp_path, monkeypatch)


def test_legacy(database_mode, tmp_path):
    exercise_legacy(tmp_path)


def test_invalid_output(database_mode, tmp_path):
    exercise_invalid(tmp_path)
