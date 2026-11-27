"""失效通知窗口、清理和持久续传合同。"""

from pathlib import Path

from unilabos.workflow.error_control import StationExecutionControl
from unilabos.workflow.event_reader import DurableEventReader
from unilabos.workflow.store import WorkflowStore


def append_events(store: WorkflowStore, times: list[str]) -> None:
    store.append_forwarded_events([
        {"event": "workflow.runtime.changed", "data": {}, "create_time": time}
        for time in times
    ])


def test_retention_is_bounded_preserves_watermark_and_detects_expiry(
    tmp_path: Path,
) -> None:
    store = WorkflowStore(tmp_path / "retention.db")
    try:
        append_events(store, ["2026-10-14T00:00:00Z"] * 8)
        assert store.prune_frontend_events(before="2020", max_count=3, batch_size=2) == 2
        assert [e["id"] for e in store.list_events()] == [3, 4, 5, 6, 7, 8]
        assert store.prune_frontend_events(before="2020", max_count=3, batch_size=2) == 2
        assert store.prune_frontend_events(before="2020", max_count=3, batch_size=2) == 1
        reader = DurableEventReader(store)
        assert reader.read_stream(after_sequence=2)["reset_reason"] == "cursor_expired"
        assert [e["id"] for e in reader.read_stream(after_sequence=5)["items"]] == [6, 7, 8]
        assert [e["id"] for e in reader.read_stream(after_sequence=0, replay_retained=True)["items"]] == [6, 7, 8]
        assert reader.read_stream(after_sequence=9)["reset_reason"] == "cursor_ahead"
        assert store.prune_frontend_events(before="2099", max_count=3, batch_size=10) == 2
        assert store.latest_event_sequence() == 8
        assert store.prune_frontend_events(before="2099", max_count=3, batch_size=10) == 0
        append_events(store, ["2026-10-14T00:00:01Z"])
        assert store.latest_event_sequence() == 9
    finally:
        store.close()


def test_age_retention_never_punches_holes_in_the_retained_sequence(
    tmp_path: Path,
) -> None:
    store = WorkflowStore(tmp_path / "age.db")
    try:
        append_events(store, ["2020", "2026", "2020", "2026"])
        assert store.prune_frontend_events(before="2025", max_count=10, batch_size=10) == 1
        assert [e["id"] for e in store.list_events()] == [2, 3, 4]
        assert store.prune_frontend_events(before="2025", max_count=10, batch_size=10) == 0
        assert store.prune_frontend_events(before="2025", max_count=1, batch_size=10) == 2
        assert store.latest_event_sequence() == 4
    finally:
        store.close()


def test_empty_event_window(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path / "empty.db")
    try:
        assert store.latest_event_sequence() == 0
        assert store.read_event_window(after_sequence=0, limit=100) == (0, 0, [])
        assert DurableEventReader(store).read_stream(after_sequence=0)["items"] == []
        assert store.prune_frontend_events(before="2099", max_count=1, batch_size=1) == 0
    finally:
        store.close()


def test_station_event_reader_signals_expired_and_ahead_cursors(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path / "station.db")
    try:
        store.append_forwarded_events([
            {"event": name, "data": {}, "create_time": "2026"}
            for name in ["station.error_handling.changed", "workflow.runtime.changed"] * 3
        ])
        store.prune_frontend_events(before="2020", max_count=3, batch_size=10)
        control = StationExecutionControl(store)
        for cursor in (1, 7):
            assert control.events(cursor) == {
                "items": [], "next_cursor": 6, "reset_required": True,
            }
        for cursor in (0, 3, 4):
            page = control.events(cursor)
            assert not page["reset_required"]
            assert [e["id"] for e in page["items"]] == [5]
            assert page["next_cursor"] == 5
    finally:
        store.close()
