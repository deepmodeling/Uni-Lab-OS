"""业务控制面严格保持 WS notice / HTTP document 分层。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from unilabos.server.clients.backend_control import BackendControlHTTPClient
from unilabos.server.protocol.common import canonical_hash
from unilabos.server.protocol.control import BackendCommandNotice


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


def test_ws_notice_forbids_execution_or_result_body() -> None:
    with pytest.raises(ValidationError, match="action_args"):
        BackendCommandNotice.model_validate(
            {**_notice_data(), "action_args": {"volume": 5}}
        )


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
    client = BackendControlHTTPClient(
        "https://backend.example/api/v1", session=session
    )

    document = client.fetch_command("command/1")

    assert document.payload == payload
    assert session.calls[0][0].endswith("/edge/commands/command%2F1")
