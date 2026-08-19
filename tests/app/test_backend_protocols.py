from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from unilabos.app.backend_protocol.control import ControlWebSocketClient
from unilabos.app.backend_protocol.old import OldBackendProtocolClient
from unilabos.app.communication import (
    CommunicationClientFactory,
    normalize_communication_protocol,
)
from unilabos.app.main import parse_args
from unilabos.app.ws_client import MessageProcessor, WebSocketClient
from unilabos.config.config import BasicConfig


class _Coordinator:
    def __init__(self) -> None:
        self.sessions: list[dict] = []
        self.notices: list[dict] = []
        self.acks: list[dict] = []
        self.edge_changes = [
            SimpleNamespace(
                model_dump=lambda **_kwargs: {
                    "protocol_version": "control.v1",
                    "session_uuid": "session-1",
                    "event_uuid": "event-1",
                    "event_sequence": 1,
                    "event_type": "job.failed",
                    "aggregate_type": "execution_job",
                    "aggregate_uuid": "job-1",
                    "aggregate_version": 2,
                }
            )
        ]

    def bind_backend_session(self, value: dict) -> None:
        self.sessions.append(value)

    def handle_backend_notice(self, value: dict) -> None:
        self.notices.append(value)

    def acknowledge_edge_changes(self, value: dict) -> None:
        self.acks.append(value)

    def claim_edge_changes(self):
        return self.edge_changes


def test_protocol_names_separate_control_from_old_backend() -> None:
    assert BasicConfig.communication_protocol == "control"
    assert normalize_communication_protocol("control.v1") == "control"
    assert normalize_communication_protocol("old") == "old"
    assert normalize_communication_protocol("websocket") == "old"
    assert normalize_communication_protocol("legacy") == "old"
    assert CommunicationClientFactory.get_supported_protocols() == [
        "control",
        "old",
    ]
    with pytest.raises(ValueError, match="Unsupported backend communication"):
        normalize_communication_protocol("missing")


def test_factory_selects_explicit_wire_protocol() -> None:
    control = CommunicationClientFactory.create_client("control")
    old = CommunicationClientFactory.create_client("old")

    assert isinstance(control, ControlWebSocketClient)
    assert isinstance(old, OldBackendProtocolClient)
    assert isinstance(old, WebSocketClient)


def test_cli_exposes_old_backend_protocol_explicitly() -> None:
    parser = parse_args()

    assert parser.parse_args([]).backend_protocol is None
    assert parser.parse_args(["--backend_protocol", "control"]).backend_protocol == (
        "control"
    )
    assert parser.parse_args(["--backend_protocol", "old"]).backend_protocol == "old"


def test_old_protocol_does_not_consume_control_messages() -> None:
    assert not hasattr(MessageProcessor, "_handle_backend_change")
    assert not hasattr(MessageProcessor, "_handle_backend_session")
    assert not hasattr(MessageProcessor, "_handle_edge_change_ack")


def test_control_protocol_routes_only_short_control_notices() -> None:
    coordinator = _Coordinator()
    client = ControlWebSocketClient(
        "ws://backend.example/api/v1/ws/schedule",
        coordinator_getter=lambda: coordinator,
    )

    asyncio.run(client._process_message("backend_session", {"session_uuid": "s"}))
    asyncio.run(client._process_message("backend_change", {"command_uuid": "c"}))
    asyncio.run(client._process_message("edge_change_ack", {"through_sequence": 1}))
    asyncio.run(client._process_message("job_start", {"job_id": "legacy"}))

    assert coordinator.sessions == [{"session_uuid": "s"}]
    assert coordinator.notices == [{"command_uuid": "c"}]
    assert coordinator.acks == [{"through_sequence": 1}]


def test_control_protocol_publishes_only_edge_change_index() -> None:
    coordinator = _Coordinator()
    client = ControlWebSocketClient(
        "ws://backend.example/api/v1/ws/schedule",
        coordinator_getter=lambda: coordinator,
    )
    client._connected = True
    client._session_bound_for_connection = True

    client.publish_runtime_events()

    assert client._send_queue.get_nowait() == {
        "action": "edge_change",
        "data": {
            "protocol_version": "control.v1",
            "session_uuid": "session-1",
            "event_uuid": "event-1",
            "event_sequence": 1,
            "event_type": "job.failed",
            "aggregate_type": "execution_job",
            "aggregate_uuid": "job-1",
            "aggregate_version": 2,
        },
    }
