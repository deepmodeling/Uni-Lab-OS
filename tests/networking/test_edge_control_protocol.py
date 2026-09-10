"""生产 Edge 协议客户端的持久化和任务闭环测试。"""

from __future__ import annotations

import asyncio
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Dict, List

from unilabos.app.edge_control.client import (
    EdgeControlClient,
    EdgeControlSettings,
    _stored_event_envelope,
)
from unilabos.app.edge_control.http import EdgeDataPlane
from unilabos.app.edge_control.store import EdgeControlStore, StoredJob


class FakeDataPlane:
    def __init__(self) -> None:
        self.fetched_jobs: List[StoredJob] = []
        self.outcomes: List[Dict[str, Any]] = []

    def fetch_job(self, job: StoredJob) -> Dict[str, Any]:
        self.fetched_jobs.append(job)
        return {
            "job_uuid": job.job_uuid,
            "task_uuid": job.task_uuid,
            "node_uuid": job.node_uuid,
            "command_uuid": job.command_uuid,
            "local_device_id": "heater-01",
            "action_name": "heat",
            "action_type": "UniLabJsonCommand",
            "param": {
                "unilabos_device_id": "heater-01",
				"timeout_seconds": 7200,
				"assignee_user_ids": ["operator-1"],
                "temperature": 37,
            },
        }

    def commit_outcome(
        self,
        job: StoredJob,
        outcome: str,
        return_info: Dict[str, Any],
        error_info: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        self.outcomes.append(
            {
                "job": job,
                "outcome": outcome,
                "return_info": return_info,
                "error_info": error_info,
            }
        )
        return {"uuid": str(uuid.uuid4())}


class FakeHostNode:
    def __init__(self) -> None:
        self.started: List[Dict[str, Any]] = []

    def send_goal(
        self,
        item: Any,
        action_type: str,
        action_kwargs: Dict[str, Any],
        sample_material: Dict[str, str],
        server_info: Any,
    ) -> None:
        self.started.append(
            {
                "item": item,
                "action_type": action_type,
                "action_kwargs": action_kwargs,
                "sample_material": sample_material,
                "server_info": server_info,
            }
        )


class FakeResponse:
    status_code = 200

    def __init__(self, payload: Dict[str, Any]) -> None:
        self._payload = payload

    def json(self) -> Dict[str, Any]:
        return self._payload


class RecordingWebSocket:
    def __init__(self) -> None:
        self.messages: List[Dict[str, Any]] = []

    async def send(self, encoded: str) -> None:
        self.messages.append(__import__("json").loads(encoded))


class RecordingSession:
    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.headers: Dict[str, str] = {}

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        if method == "GET":
            job_uuid = url.rsplit("/", 1)[-1]
            return FakeResponse(
                {
                    "code": 0,
                    "data": {
                        "job_uuid": job_uuid,
                        "task_uuid": kwargs["params"]["task_uuid"],
                        "node_uuid": kwargs["params"]["node_uuid"],
                        "command_uuid": kwargs["headers"]["X-Command-UUID"],
                    },
                }
            )
        return FakeResponse({"code": 0, "data": {"uuid": str(uuid.uuid4())}})


def _settings(path: Path) -> EdgeControlSettings:
    return EdgeControlSettings(
        scheduler_address="http://scheduler:8081",
        backend_address="http://backend:8080",
        api_key="edge-secret",
        edge_key="edge-test",
        capability_revision="test-v1",
        instance_uuid="",
        state_db=str(path),
        reconnect_interval=0.01,
        request_timeout=1,
        event_retry_interval=1,
    )


def test_store_persists_command_job_and_event_ack(tmp_path: Path) -> None:
    path = tmp_path / "edge-control.db"
    command_uuid = str(uuid.uuid4())
    job_uuid = str(uuid.uuid4())
    task_uuid = str(uuid.uuid4())
    node_uuid = str(uuid.uuid4())
    traceparent = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    tracestate = "vendor=value"
    store = EdgeControlStore(str(path))

    assert store.record_command(
        {
            "message_uuid": command_uuid,
            "sequence": 7,
            "type": "job.start",
            "payload": {"job_uuid": job_uuid},
            "traceparent": traceparent,
            "tracestate": tracestate,
        }
    )
    assert not store.record_command(
        {
            "message_uuid": command_uuid,
            "sequence": 7,
            "type": "job.start",
            "payload": {"job_uuid": job_uuid},
        }
    )
    assert store.save_job_start(
        {
            "job_uuid": job_uuid,
            "task_uuid": task_uuid,
            "node_uuid": node_uuid,
            "job_access_token": "short-token",
        },
        command_uuid,
    )
    store.mark_command_completed(command_uuid)
    event_uuid = store.enqueue_event(
        "command.ack",
        {"command_uuid": command_uuid},
        {"traceparent": traceparent, "tracestate": tracestate},
    )

    assert store.last_ack_command_sequence() == 7
    job = store.get_job(job_uuid)
    assert job is not None
    assert job.traceparent == traceparent
    assert job.tracestate == tracestate
    assert store.save_pending_outcome(
        job_uuid,
        "succeeded",
        {"suc": True},
        [],
    )
    assert store.get_pending_outcome(job_uuid) is not None
    pending_events = store.pending_events(0)
    assert [event.event_uuid for event in pending_events] == [event_uuid]
    assert pending_events[0].traceparent == traceparent
    assert pending_events[0].tracestate == tracestate
    envelope = _stored_event_envelope(pending_events[0])
    assert envelope["traceparent"] == traceparent
    assert envelope["tracestate"] == tracestate
    store.acknowledge_event(event_uuid)
    assert store.pending_events(float("inf")) == []
    instance_uuid = store.get_or_create_instance_uuid()
    store.close()

    reopened = EdgeControlStore(str(path))
    assert reopened.get_or_create_instance_uuid() == instance_uuid
    reopened_job = reopened.get_job(job_uuid)
    assert reopened_job is not None
    assert reopened_job.traceparent == traceparent
    assert reopened_job.tracestate == tracestate
    pending_outcome = reopened.get_pending_outcome(job_uuid)
    assert pending_outcome is not None
    assert pending_outcome.return_info == {"suc": True}
    reopened.close()


def test_store_migrates_existing_runtime_and_outbox_schema(tmp_path: Path) -> None:
    path = tmp_path / "old-edge-control.db"
    job_uuid = str(uuid.uuid4())
    event_uuid = str(uuid.uuid4())
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE edge_event_outbox (
            event_uuid TEXT PRIMARY KEY,
            type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_sent_at REAL,
            acked_at REAL
        );
        CREATE TABLE edge_job_runtime (
            job_uuid TEXT PRIMARY KEY,
            task_uuid TEXT NOT NULL,
            node_uuid TEXT NOT NULL,
            command_uuid TEXT NOT NULL,
            job_access_token TEXT NOT NULL,
            status TEXT NOT NULL,
            feedback_sequence INTEGER NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL
        );
        """
    )
    connection.execute(
        """
        INSERT INTO edge_event_outbox(
            event_uuid, type, payload_json, created_at
        ) VALUES (?, 'command.ack', '{}', '2026-08-02T00:00:00Z')
        """,
        (event_uuid,),
    )
    connection.execute(
        """
        INSERT INTO edge_job_runtime(
            job_uuid, task_uuid, node_uuid, command_uuid,
            job_access_token, status, updated_at
        ) VALUES (?, ?, ?, ?, 'token', 'received', 1)
        """,
        (job_uuid, str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())),
    )
    connection.commit()
    connection.close()

    store = EdgeControlStore(str(path))
    job = store.get_job(job_uuid)
    events = store.pending_events(float("inf"))
    assert job is not None
    assert job.traceparent == ""
    assert [event.event_uuid for event in events] == [event_uuid]
    assert events[0].traceparent == ""
    store.close()


def test_store_discards_legacy_pong_events_when_reopened(tmp_path: Path) -> None:
    path = tmp_path / "edge-control.db"
    store = EdgeControlStore(str(path))
    store.enqueue_event("pong", {"ping_uuid": str(uuid.uuid4())})
    store.close()

    reopened = EdgeControlStore(str(path))

    assert reopened.pending_events(float("inf")) == []
    reopened.close()


def test_ping_pong_is_sent_on_current_connection_without_outbox(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        path = tmp_path / "runtime.db"
        store = EdgeControlStore(str(path))
        client = EdgeControlClient(_settings(path), store=store)
        websocket = RecordingWebSocket()
        client._websocket = websocket
        ping_uuid = str(uuid.uuid4())

        await client._handle_envelope(
            {
                "protocol_version": 1,
                "message_uuid": str(uuid.uuid4()),
                "sequence": 0,
                "type": "ping",
                "sent_at": "2026-08-02T00:00:00.000000Z",
                "payload": {"ping_uuid": ping_uuid},
            }
        )

        assert len(websocket.messages) == 1
        assert websocket.messages[0]["type"] == "pong"
        assert websocket.messages[0]["payload"] == {"ping_uuid": ping_uuid}
        assert store.pending_events(float("inf")) == []
        store.close()

    asyncio.run(scenario())


def test_http_data_plane_uses_three_uuid_identity() -> None:
    job = StoredJob(
        job_uuid=str(uuid.uuid4()),
        task_uuid=str(uuid.uuid4()),
        node_uuid=str(uuid.uuid4()),
        command_uuid=str(uuid.uuid4()),
        job_access_token="short-token",
        status="received",
        feedback_sequence=0,
    )
    plane = EdgeDataPlane(
        "http://backend:8080/api/v1",
        "http://scheduler:8081",
        "edge-secret",
    )
    session = RecordingSession()
    plane._session = session  # type: ignore[assignment]

    plane.fetch_job(job)
    plane.commit_outcome(job, "succeeded", {"suc": True}, [])

    fetch = session.calls[0]
    assert fetch["params"] == {
        "task_uuid": job.task_uuid,
        "node_uuid": job.node_uuid,
    }
    assert fetch["headers"] == {
        "X-Command-UUID": job.command_uuid,
        "X-Job-Token": job.job_access_token,
    }
    outcome = session.calls[1]
    assert outcome["json"]["task_uuid"] == job.task_uuid
    assert outcome["json"]["node_uuid"] == job.node_uuid
    assert outcome["headers"]["Idempotency-Key"] == f"{job.job_uuid}:outcome:v1"


def test_http_data_plane_injects_w3c_trace_headers(monkeypatch) -> None:
    traceparent = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"

    def inject(carrier: Dict[str, Any]) -> Dict[str, Any]:
        carrier["traceparent"] = traceparent
        carrier["tracestate"] = "vendor=value"
        return carrier

    monkeypatch.setattr(
        "unilabos.app.edge_control.http.inject_trace_context", inject
    )
    job = StoredJob(
        job_uuid=str(uuid.uuid4()),
        task_uuid=str(uuid.uuid4()),
        node_uuid=str(uuid.uuid4()),
        command_uuid=str(uuid.uuid4()),
        job_access_token="short-token",
        status="received",
        feedback_sequence=0,
    )
    plane = EdgeDataPlane(
        "http://backend:8080",
        "http://scheduler:8081",
        "edge-secret",
    )
    session = RecordingSession()
    plane._session = session  # type: ignore[assignment]

    plane.fetch_job(job)

    assert session.calls[0]["headers"]["traceparent"] == traceparent
    assert session.calls[0]["headers"]["tracestate"] == "vendor=value"


def test_job_start_fetches_http_payload_and_outcome_precedes_notification(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        data_plane = FakeDataPlane()
        host_node = FakeHostNode()
        store = EdgeControlStore(str(tmp_path / "runtime.db"))
        client = EdgeControlClient(
            _settings(tmp_path / "runtime.db"),
            store=store,
            data_plane=data_plane,  # type: ignore[arg-type]
            host_node_provider=lambda: host_node,
        )
        client._connected.set()
        job_uuid = str(uuid.uuid4())
        task_uuid = str(uuid.uuid4())
        node_uuid = str(uuid.uuid4())
        command_uuid = str(uuid.uuid4())
        traceparent = (
            "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
        )
        await client._handle_envelope(
            {
                "protocol_version": 1,
                "message_uuid": command_uuid,
                "sequence": 11,
                "type": "job.start",
                "sent_at": "2026-08-02T00:00:00.000000Z",
                "traceparent": traceparent,
                "tracestate": "vendor=value",
                "payload": {
                    "job_uuid": job_uuid,
                    "task_uuid": task_uuid,
                    "node_uuid": node_uuid,
                    "executor_kind": "device_action",
                    "job_access_token": "short-token",
                },
            }
        )
        if client._tasks:
            await asyncio.gather(*list(client._tasks))

        assert len(data_plane.fetched_jobs) == 1
        assert len(host_node.started) == 1
        context = host_node.started[0]["item"]
        assert context.task_id == task_uuid
        assert context.node_id == node_uuid
        assert host_node.started[0]["action_kwargs"] == {"temperature": 37}
        assert context.trace_context["traceparent"] == traceparent

        client.publish_job_started(context)
        await client._commit_terminal_status(
            job_uuid,
            "success",
            {"actual_temperature": 37},
            {"suc": True},
        )

        assert len(data_plane.outcomes) == 1
        assert data_plane.outcomes[0]["job"].task_uuid == task_uuid
        assert data_plane.outcomes[0]["outcome"] == "succeeded"
        events = store.pending_events(float("inf"))
        assert [event.event_type for event in events] == [
            "command.ack",
            "job.started",
            "job.outcome_committed",
        ]
        assert all(event.traceparent == traceparent for event in events)
        assert all(event.tracestate == "vendor=value" for event in events)
        assert store.get_job(job_uuid).status == "outcome_committed"  # type: ignore[union-attr]
        assert store.get_pending_outcome(job_uuid) is None
        store.close()

    asyncio.run(scenario())
