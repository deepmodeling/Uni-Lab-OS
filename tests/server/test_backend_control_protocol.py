"""业务控制面严格保持 WS notice / HTTP document 分层。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from unilabos.server.backend.http import BackendHTTPClient
from unilabos.server.backend.sync import InstanceSynchronizer, TemplateSynchronizer
from unilabos.server.backend.websocket import BackendWebSocketClient
from unilabos.server.protocol.common import canonical_hash
from unilabos.server.protocol.control import BackendCommandNotice, EdgeChangeNotice


class _Response:
    status_code = 200

    def __init__(self, body: dict) -> None:
        self.body = body

    def json(self) -> dict:
        return self.body


class _Session:
    def __init__(self, body: dict) -> None:
        self.headers: dict[str, str] = {}
        self.body = body
        self.calls: list[tuple[str, dict]] = []

    def get(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
        return _Response(self.body)


def _notice_data() -> dict:
    return {
        "notice_uuid": "notice-1",
        "command_uuid": "command-1",
        "command_type": "execute_job",
        "session_uuid": "session-1",
        "backend_sequence": 1,
        "edge_uuid": "edge-1",
        "authority_epoch": "authority-1",
        "connection_epoch": "connection-1",
        "content_sha256": "sha",
    }


@pytest.mark.parametrize("forbidden", ["action_args", "payload", "url"])
def test_backend_ws_notice_forbids_body_or_arbitrary_url(forbidden: str) -> None:
    with pytest.raises(ValidationError, match=forbidden):
        BackendCommandNotice.model_validate(
            {**_notice_data(), forbidden: {"volume": 5}}
        )


@pytest.mark.parametrize("forbidden", ["payload", "data", "url"])
def test_edge_ws_notice_forbids_body_or_arbitrary_url(forbidden: str) -> None:
    notice = {
        "session_uuid": "session-1",
        "event_uuid": "event-1",
        "event_sequence": 1,
        "event_type": "job.succeeded",
        "aggregate_type": "execution_job",
        "aggregate_uuid": "job-1",
        "aggregate_version": 2,
        forbidden: {"result": "secret"},
    }
    with pytest.raises(ValidationError, match=forbidden):
        EdgeChangeNotice.model_validate(notice)


def test_http_client_fetches_full_document_from_uuid_derived_path() -> None:
    payload = {"job_uuid": "job-1"}
    payload_hash = canonical_hash(payload)
    body = {
        "code": 0,
        "data": {
            "protocol_version": "control.v1",
            "command": {
                "command_uuid": "command/1",
                "session_uuid": "session-1",
                "backend_sequence": 1,
                "command_type": "execute_job",
                "job_uuid": "job-1",
                "payload_uuid": "payload-1",
                "payload_sha256": payload_hash,
            },
            "payload": payload,
        },
    }
    session = _Session(body)
    client = BackendHTTPClient(
        "https://backend.example/api/v1", session=session
    )

    document = client.fetch_command("command/1")

    assert document.payload == payload
    assert session.calls[0][0].endswith("/edge/commands/command%2F1")


def test_backend_package_owns_transport_and_data_sync() -> None:
    assert BackendWebSocketClient.__module__ == "unilabos.server.backend.websocket"
    assert BackendHTTPClient.__module__ == "unilabos.server.backend.http"
    assert InstanceSynchronizer.__module__ == "unilabos.server.backend.sync.instances"
    assert TemplateSynchronizer.__module__ == "unilabos.server.backend.sync.templates"
