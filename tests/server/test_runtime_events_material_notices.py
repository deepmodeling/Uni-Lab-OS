"""/api/v1/events 的物料失效：账本前进就推一条无 id 的 material.changed，不动工作流游标。

前端每个工站只维持这一条事件流（HTTP/1.1 连接数有限），物料失效与工作流、日志通知走同一条。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

from unilabos.server.api.runtime import events


class _Request:
    async def is_disconnected(self):
        return False


class _NoLogNotices:
    def subscribe(self, listener):
        return lambda: None


def _quiet(monkeypatch):
    monkeypatch.setattr(events, "log_notices", _NoLogNotices())
    monkeypatch.setattr(events, "shutting_down", lambda: False)


def test_ledger_advance_emits_one_idless_material_changed(monkeypatch):
    _quiet(monkeypatch)
    workflow = Mock()
    workflow.read_event_window.return_value = (1, 41, [])

    async def scenario():
        stream = events.runtime_event_stream(
            _Request(), workflow, cursor=41, material_sequence=lambda: 7, material_cursor=5
        )
        assert "connected" in await anext(stream)
        frame = await asyncio.wait_for(anext(stream), 2)
        assert frame.startswith("event: material.changed\n")
        assert "id:" not in frame
        assert json.loads(frame.split("data: ")[1]) == {"sequence": 7}
        await stream.aclose()

    asyncio.run(scenario())


def test_unchanged_ledger_sends_nothing_before_workflow_events(monkeypatch):
    _quiet(monkeypatch)
    workflow = Mock()
    workflow.read_event_window.return_value = (1, 42, [{"id": 42, "event": "workflow.task.changed", "data": {"uuid": "t"}}])

    async def scenario():
        stream = events.runtime_event_stream(
            _Request(), workflow, cursor=41, material_sequence=lambda: 7, material_cursor=7
        )
        assert "connected" in await anext(stream)
        frame = await asyncio.wait_for(anext(stream), 2)
        assert frame.startswith("id: 42\nevent: workflow.runtime.changed\n")
        await stream.aclose()

    asyncio.run(scenario())


def test_ledger_read_failure_does_not_break_the_workflow_stream(monkeypatch):
    _quiet(monkeypatch)
    workflow = Mock()
    workflow.read_event_window.return_value = (1, 42, [{"id": 42, "event": "workflow.task.changed", "data": {"uuid": "t"}}])

    def broken():
        raise RuntimeError("materials.db is locked")

    async def scenario():
        stream = events.runtime_event_stream(
            _Request(), workflow, cursor=41, material_sequence=broken, material_cursor=3
        )
        assert "connected" in await anext(stream)
        frame = await asyncio.wait_for(anext(stream), 2)
        assert frame.startswith("id: 42\nevent: workflow.runtime.changed\n")
        await stream.aclose()

    asyncio.run(scenario())


def test_reader_exists_only_where_the_materials_authority_lives(monkeypatch):
    from unilabos.server import execution_composition

    monkeypatch.setattr(execution_composition, "get_materials_service", lambda: None)
    assert events.material_sequence_reader() is None

    authority = SimpleNamespace(latest_ledger_sequence=lambda: 12)
    monkeypatch.setattr(execution_composition, "get_materials_service", lambda: authority)
    reader = events.material_sequence_reader()
    assert reader is not None and reader() == 12
