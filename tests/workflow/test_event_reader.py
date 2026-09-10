"""F09 持久全局事件游标（Cursor）的只读合同。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from unilabos.app.scheduler.monitor import MonitorBus
from unilabos.workflow.event_reader import DurableEventReader, EventProjectionError
from unilabos.workflow.store import WorkflowStore

WORKFLOW_UUID = "91000000-0000-4000-8000-000000000001"


class RecordingEventStore:
    """记录读取参数并返回固定持久事件的测试存储。"""

    def __init__(self, events: list[dict[str, Any]]) -> None:
        """保存测试事件。

        参数：``events`` 是按事件序号排列的固定记录。返回：无。异常：无；非法
        记录由被测读取器失败关闭。
        """

        self.events = events
        self.calls: list[tuple[int, int]] = []

    def list_events(
        self,
        *,
        after_sequence: int,
        limit: int,
    ) -> list[dict[str, Any]]:
        """模拟持久事件范围读取。

        参数：``after_sequence`` 是排他游标，``limit`` 是物理读取上限。返回：
        符合范围的独立事件副本。异常：无。
        """

        self.calls.append((after_sequence, limit))
        return [dict(event) for event in self.events if event["id"] > after_sequence][
            :limit
        ]


def _events() -> list[dict[str, Any]]:
    """构造三个只含失效身份的持久事件。

    参数：无。返回：严格递增的事件列表。异常：无。
    """

    return [
        {
            "id": sequence,
            "event": "workflow.authoring.changed",
            "data": {"workflow_uuid": WORKFLOW_UUID},
            "create_time": f"2026-08-05T00:00:0{sequence}Z",
        }
        for sequence in (1, 2, 3)
    ]


def test_reader_pages_by_exclusive_monotonic_sequence() -> None:
    """读取器必须给出排他游标、下一游标和是否还有后页。

    参数：无。返回：无；断言物理多读一条但只公开请求页。异常：游标、顺序或
    分页语义回归由断言暴露。
    """

    store = RecordingEventStore(_events())
    reader = DurableEventReader(store)

    page = reader.read(after_sequence=1, limit=1)

    assert store.calls == [(1, 2)]
    assert [event["id"] for event in page["items"]] == [2]
    assert page == {
        "items": [page["items"][0]],
        "after_sequence": 1,
        "next_sequence": 2,
        "has_more": True,
    }


@pytest.mark.parametrize(
    ("after_sequence", "limit"),
    [
        (True, 1),
        (-1, 1),
        (1 << 63, 1),
        (0, False),
        (0, 0),
        (0, 1001),
    ],
)
def test_reader_rejects_invalid_int64_cursor_and_limit(
    after_sequence: Any,
    limit: Any,
) -> None:
    """布尔值、越界游标和非法页长必须在存储读取前失败。

    参数：``after_sequence`` 与 ``limit`` 来自非法值矩阵。返回：无。异常：预期
    ``ValueError``；若存储被调用或错误被吞掉则断言失败。
    """

    store = RecordingEventStore(_events())
    reader = DurableEventReader(store)

    with pytest.raises(ValueError):
        reader.read(after_sequence=after_sequence, limit=limit)

    assert store.calls == []


def test_reader_rejects_non_monotonic_store_projection() -> None:
    """持久读取结果不得倒退、重复或包含请求游标本身。

    参数：无。返回：无。异常：预期 ``EventProjectionError``，证明损坏投影不会
    被误报为客户端游标错误，也不会进入 SSE。
    """

    store = RecordingEventStore([_events()[1], _events()[0]])

    with pytest.raises(EventProjectionError, match="严格递增"):
        DurableEventReader(store).read(after_sequence=0, limit=10)


def test_sqlite_sequence_survives_restart_and_monitor_loss(tmp_path: Path) -> None:
    """SQLite 事件序号跨重启递增，内存监控丢失不影响重放。

    参数：``tmp_path`` 是隔离数据库目录。返回：无；断言两个进程代际的事件可由
    第一游标续读。异常：SQLite、事件追加或读取失败由测试暴露。
    """

    database = tmp_path / "f09-events.db"
    first_store = WorkflowStore(database)
    try:
        first_store.create_workflow(
            workflow_uuid=WORKFLOW_UUID,
            name="F09 durable events",
            tags=[],
            description=None,
            meta_data={},
        )
        first_sequence = first_store.record_draft_compilation(
            workflow_uuid=WORKFLOW_UUID,
            draft_hash="sha256:" + "1" * 64,
            draft_update_time="2026-08-05T00:00:01Z",
            diagnostics=[],
            candidate_hash=None,
            candidate=None,
            event_data={"workflow_uuid": WORKFLOW_UUID, "cause": "first"},
        )
    finally:
        first_store.close()

    transient_monitor = MonitorBus()
    transient_monitor.emit("scheduler", "process_will_restart")
    assert transient_monitor.recent("scheduler", limit=10)
    del transient_monitor

    reopened_store = WorkflowStore(database)
    try:
        second_sequence = reopened_store.record_draft_compilation(
            workflow_uuid=WORKFLOW_UUID,
            draft_hash="sha256:" + "2" * 64,
            draft_update_time="2026-08-05T00:00:02Z",
            diagnostics=[],
            candidate_hash=None,
            candidate=None,
            event_data={"workflow_uuid": WORKFLOW_UUID, "cause": "second"},
        )
        page = DurableEventReader(reopened_store).read(
            after_sequence=first_sequence,
            limit=10,
        )
    finally:
        reopened_store.close()

    assert second_sequence > first_sequence
    assert [event["id"] for event in page["items"]] == [second_sequence]
    assert page["next_sequence"] == second_sequence
    assert page["has_more"] is False
