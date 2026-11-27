"""工站合流只改变传输连接数，不合并独立持久序列。"""

from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.datastructures import Headers

from unilabos.app.station_events import (
    SCOPES,
    StationEventSource,
    decode_cursor,
    encode_cursor,
    station_event_stream,
)
from unilabos.workflow.event_reader import DurableEventReader


class MemoryEvents:
    def __init__(self):
        self.events = []

    def latest(self):
        return self.events[-1]["id"] if self.events else 0

    def read_event_window(self, *, after_sequence, limit):
        return (
            self.events[0]["id"] if self.events else 0, self.latest(),
            [event for event in self.events if event["id"] > after_sequence][:limit],
        )

    def append(self, event, sequence=None):
        self.events.append({"id": sequence or self.latest() + 1, "event": event, "data": {"marker": event}})


def request(cursor=None):
    async def connected():
        return False

    return SimpleNamespace(
        headers=Headers({} if cursor is None else {"Last-Event-ID": cursor}),
        is_disconnected=connected,
    )


def frame_cursor(frame):
    return next(line[4:] for line in frame.splitlines() if line.startswith("id: "))


def sources(stores):
    return {
        scope: StationEventSource(store.latest, DurableEventReader(store).read_stream)
        for scope, store in stores.items()
    }


def test_reconnect_after_one_frame_does_not_skip_unemitted_events():
    async def scenario():
        stores = {scope: MemoryEvents() for scope in SCOPES}
        response = await station_event_stream(request(), "local", sources(stores))
        stream = response.body_iterator
        baseline = await anext(stream)
        assert decode_cursor(frame_cursor(baseline), "local") == dict.fromkeys(SCOPES, 0)
        stores["runtime"].append("workflow.task.changed")
        stores["runtime"].append("workflow.node.changed")
        stores["inventory"].append("material.changed")
        stores["workflow"].append("resource.usage.changed")
        first = await anext(stream)
        cursor = frame_cursor(first)
        assert decode_cursor(cursor, "local") == {"runtime": 1, "inventory": 0, "workflow": 0}
        await stream.aclose()

        response = await station_event_stream(request(cursor), "local", sources(stores))
        stream = response.body_iterator
        await anext(stream)
        frames = [await anext(stream) for _ in range(3)]
        assert [next(line for line in f.splitlines() if line.startswith("event:")) for f in frames] == [
            "event: workflow.node.changed", "event: material.changed", "event: resource.usage.changed",
        ]
        assert decode_cursor(frame_cursor(frames[-1]), "local") == {"runtime": 2, "inventory": 1, "workflow": 1}
        await stream.aclose()

    asyncio.run(asyncio.wait_for(scenario(), 2))


@pytest.mark.parametrize("scope", SCOPES)
@pytest.mark.parametrize("ahead", [False, True])
def test_reset_only_advances_affected_source(scope, ahead):
    async def scenario():
        stores = {name: MemoryEvents() for name in SCOPES}
        stores[scope].append("changed", sequence=100)
        cursors = dict.fromkeys(SCOPES, 0)
        cursors[scope] = 200 if ahead else 2
        stream = (await station_event_stream(request(encode_cursor("local", cursors)), "local", sources(stores))).body_iterator
        await anext(stream)
        frame = await anext(stream)
        assert "event: stream.reset" in frame
        payload = json.loads(next(line[6:] for line in frame.splitlines() if line.startswith("data: ")))
        assert payload == {"scope": scope, "reason": "cursor_ahead" if ahead else "cursor_expired"}
        cursors[scope] = 100
        assert decode_cursor(frame_cursor(frame), "local") == cursors
        await stream.aclose()

    asyncio.run(asyncio.wait_for(scenario(), 2))


@pytest.mark.parametrize("cursor", [
    "0", "v1.!!!", encode_cursor("other", dict.fromkeys(SCOPES, 0)),
    encode_cursor("local", {"runtime": 0, "inventory": 0}),
    encode_cursor("local", {"runtime": True, "inventory": 0, "workflow": 0}),
    encode_cursor("local", {"runtime": 2**63, "inventory": 0, "workflow": 0}),
    "v1." + base64.urlsafe_b64encode(b'{"v":1,"v":1,"station":"local","cursors":{}}').decode(),
])
def test_invalid_or_conflicting_composite_cursor_is_rejected(cursor):
    with pytest.raises(HTTPException) as error:
        decode_cursor(cursor, "local")
    assert error.value.status_code == 400


def test_duplicate_last_event_id_headers_are_rejected():
    async def scenario():
        req = request()
        req.headers = Headers(raw=[(b"last-event-id", b"a"), (b"last-event-id", b"b")])
        with pytest.raises(HTTPException) as error:
            await station_event_stream(req, "local", {})
        assert error.value.status_code == 400

    asyncio.run(scenario())
