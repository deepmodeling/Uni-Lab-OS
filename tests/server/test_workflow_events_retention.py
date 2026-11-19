"""frontend_event 保留清理（v1.0 ADR 0006）：连续前缀、保留水位、数量与时间、有界批次。"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from unilabos.server.services.runtime.workflow.event_retention import (
    EventRetentionPolicy,
    FrontendEventRetention,
)
from unilabos.server.services.runtime.workflow.service import WorkflowService
from unilabos.server.services.runtime.workflow.store import WorkflowStore

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
FAR_FUTURE = "9999-12-31T00:00:00.000000Z"
FAR_PAST = "0001-01-01T00:00:00.000000Z"


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _append(service: WorkflowService, *times: datetime) -> None:
    with service.transaction() as conn:
        for index, moment in enumerate(times):
            WorkflowStore._append_event(
                conn, event="workflow.task.changed", data={"workflow_task_uuid": f"t{index}"}, now=_iso(moment),
            )


def _sequences(service: WorkflowService) -> list[int]:
    return [event["id"] for event in service.read_event_window(after_id=0, limit=1000)[2]]


@pytest.fixture
def service():
    workflow = WorkflowService(":memory:")
    yield workflow
    workflow.close()


def test_age_cleanup_stops_at_first_retained_event_without_holes(service) -> None:
    old, new = NOW - timedelta(days=2), NOW
    _append(service, old, old, new, old, new)  # 第 4 条时间乱序：仍不能在中间挖洞
    deleted = service.prune_frontend_events(before=_iso(NOW - timedelta(days=1)), max_count=1000, batch_size=1000)
    assert deleted == 2
    assert _sequences(service) == [3, 4, 5]


def test_count_limit_keeps_newest_events(service) -> None:
    _append(service, *([NOW] * 5))
    assert service.prune_frontend_events(before=FAR_PAST, max_count=2, batch_size=1000) == 3
    assert _sequences(service) == [4, 5]
    assert service.read_event_window(after_id=0, limit=10)[:2] == (4, 5)


def test_latest_event_is_kept_as_watermark_even_when_expired(service) -> None:
    _append(service, *([NOW - timedelta(days=3)] * 3))
    assert service.prune_frontend_events(before=FAR_FUTURE, max_count=1000, batch_size=1000) == 2
    assert _sequences(service) == [3]
    assert service.prune_frontend_events(before=FAR_FUTURE, max_count=1, batch_size=1000) == 0
    assert service.latest_event_sequence() == 3


def test_each_cleanup_transaction_is_bounded(service) -> None:
    _append(service, *([NOW - timedelta(days=3)] * 10))
    assert service.prune_frontend_events(before=FAR_FUTURE, max_count=1000, batch_size=3) == 3
    assert _sequences(service)[0] == 4


def test_empty_window_reads_zero_bounds(service) -> None:
    assert service.read_event_window(after_id=0, limit=10) == (0, 0, [])
    assert service.prune_frontend_events(before=FAR_FUTURE, max_count=1, batch_size=10) == 0


@pytest.mark.parametrize("field", ["max_age_seconds", "max_count", "interval_seconds", "batch_size"])
def test_policy_rejects_non_positive_values(field: str) -> None:
    with pytest.raises(ValueError):
        EventRetentionPolicy(**{field: 0})


def test_run_once_applies_age_cutoff(service) -> None:
    _append(service, NOW - timedelta(hours=2), NOW - timedelta(minutes=1), NOW)
    retention = FrontendEventRetention(service, EventRetentionPolicy(max_age_seconds=3600))
    assert retention.run_once(now=NOW) == 1
    assert _sequences(service) == [2, 3]


def test_worker_cleans_without_subscribers_and_stops(service) -> None:
    # 用真实当前时间：worker 按真实时钟计算超龄，这里只验证数量上限。
    _append(service, *([datetime.now(timezone.utc)] * 6))
    retention = FrontendEventRetention(service, EventRetentionPolicy(max_count=2, interval_seconds=60, batch_size=2))
    retention.start()
    try:
        deadline = time.monotonic() + 5
        while _sequences(service) != [5, 6] and time.monotonic() < deadline:
            time.sleep(0.02)
        assert _sequences(service) == [5, 6]  # 积压时连续清理，不等 60 秒间隔
    finally:
        started = time.monotonic()
        retention.stop()
        assert time.monotonic() - started < 2
