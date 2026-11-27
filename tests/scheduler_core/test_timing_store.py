"""SQLite 耗时样本账的事务、幂等和恢复测试。"""

from __future__ import annotations

import sqlite3

import pytest

from unilabos.app.scheduler.estimation import DurationEstimator
from unilabos.app.scheduler.timing import ActionTimingSpec
from unilabos.app.scheduler.timing_store import TimingObservation, TimingObservationStore


def _sample(
    observation_id: str = "obs-1",
    *,
    actual_s: float = 2.0,
    outcome: str = "success",
    spec_version: str = "1",
    timing_boundary: str = "dispatch_to_observed_completion",
    calibration: bool = False,
) -> TimingObservation:
    return TimingObservation(
        observation_id=observation_id,
        device_action_key="/device/run",
        actual_s=actual_s,
        params={"size": 1},
        device_id="device-1",
        action_version="action-v1",
        spec_version=spec_version,
        timing_boundary=timing_boundary,
        outcome=outcome,
        calibration=calibration,
    )


def _store() -> tuple[sqlite3.Connection, TimingObservationStore]:
    connection = sqlite3.connect(":memory:")
    store = TimingObservationStore(connection)
    store.ensure_schema()
    connection.commit()
    return connection, store


@pytest.mark.parametrize(
    "sample",
    [
        TimingObservation("", "/device/run", 1.0, {}),
        TimingObservation("obs", "", 1.0, {}),
        TimingObservation("obs", "/device/run", 0.0, {}),
        TimingObservation("obs", "/device/run", float("nan"), {}),
        TimingObservation("obs", "/device/run", 0.0, {}, outcome="failed"),
        TimingObservation("obs", "/device/run", 1.0, {}, spec_version=""),
        TimingObservation("obs", "/device/run", 1.0, {}, timing_boundary=""),
        TimingObservation("obs", "/device/run", 1.0, {}, calibration=1),  # type: ignore[arg-type]
    ],
)
def test_payload_validates_identity_timing_and_metadata(sample: TimingObservation) -> None:
    if sample.outcome == "failed" and sample.observation_id == "obs":
        # A failed observation may carry a zero duration; this case is valid.
        assert sample.payload()
    else:
        with pytest.raises(ValueError):
            sample.payload()


def test_append_requires_caller_transaction_and_is_idempotent() -> None:
    connection, store = _store()
    sample = _sample()

    with pytest.raises(RuntimeError, match="写事务"):
        store.append(sample)

    connection.execute("BEGIN")
    assert store.append(sample) is True
    assert store.append(sample) is False
    connection.commit()

    connection.execute("BEGIN")
    with pytest.raises(ValueError, match="不同内容"):
        store.append(_sample(actual_s=3.0))
    connection.rollback()


def test_get_round_trips_and_detects_missing_or_corrupt_rows() -> None:
    connection, store = _store()
    connection.execute("BEGIN")
    store.append(_sample())
    connection.commit()

    loaded = store.get("obs-1")
    assert loaded == _sample()
    assert store.get("missing") is None

    connection.execute(
        "UPDATE scheduler_timing_observations SET payload_hash = ? WHERE observation_id = ?",
        ("bad", "obs-1"),
    )
    connection.commit()
    with pytest.raises(ValueError, match="指纹"):
        store.get("obs-1")


def test_replay_requires_committed_facts_and_filters_incompatible_samples() -> None:
    connection, store = _store()
    samples = (
        _sample("accepted"),
        _sample("failed", actual_s=0.0, outcome="failed"),
        _sample("old-version", spec_version="old"),
        _sample("old-boundary", timing_boundary="dispatch_to_ack"),
    )
    connection.execute("BEGIN")
    for sample in samples:
        assert store.append(sample)
    connection.commit()

    estimator = DurationEstimator()
    estimator.register_timing_spec(
        "/device/run", ActionTimingSpec(feature_paths=("size",), version="1")
    )
    connection.execute("BEGIN")
    with pytest.raises(RuntimeError, match="提交执行事实"):
        store.replay_into(estimator)
    connection.rollback()

    result = store.replay_into(estimator)
    assert result == {"accepted": 1, "skipped": 3}
    estimate = estimator.estimate_timing(
        "/device/run", {"size": 1}, device_id="device-1", action_version="action-v1"
    )
    assert estimate.samples == 1
    assert estimate.point_s == 2.0


def test_replay_detects_corrupt_payload_hash() -> None:
    connection, store = _store()
    connection.execute("BEGIN")
    store.append(_sample())
    connection.commit()
    connection.execute(
        "UPDATE scheduler_timing_observations SET payload_json = ? WHERE observation_id = ?",
        ('{"broken": true}', "obs-1"),
    )
    connection.commit()

    with pytest.raises(ValueError, match="指纹"):
        store.replay_into(DurationEstimator())
