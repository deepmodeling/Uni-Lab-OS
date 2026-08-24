from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from unilabos.app.cli.parser import build_parser
from unilabos.config.config import BasicConfig, _update_config_from_module
from unilabos.server.backend.session import (
    APP_BRIDGES,
    BackendSessionFactory,
    COMMUNICATION_PROTOCOL,
)
from unilabos.server.backend.websocket import BackendWebSocketClient


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


@pytest.fixture(autouse=True)
def _reset_backend_client():
    BackendSessionFactory.reset_client()
    yield
    BackendSessionFactory.reset_client()


def test_transport_configuration_is_fixed_to_websocket() -> None:
    assert APP_BRIDGES == ("websocket",)
    assert COMMUNICATION_PROTOCOL == "websocket"


def test_removed_transport_fields_are_not_loaded_from_config() -> None:
    old_basic_config = type(
        "BasicConfig",
        (),
        {"app_bridges": ("fastapi",), "communication_protocol": "old"},
    )
    _update_config_from_module(SimpleNamespace(BasicConfig=old_basic_config))
    assert not hasattr(BasicConfig, "app_bridges")
    assert not hasattr(BasicConfig, "communication_protocol")


def test_factory_always_creates_control_v1_client() -> None:
    assert isinstance(BackendSessionFactory.create_client(), BackendWebSocketClient)


def test_cli_rejects_removed_compatibility_options() -> None:
    parser = build_parser()
    removed_options = (
        "--legacy",
        "--upload_registry",
        "--restart_mode",
        "--auto_restart_count",
        "--app_bridges",
        "--backend_protocol",
        "--communication_protocol",
        "--schedule_addr",
        "--schedule-address",
    )
    assert not set(removed_options) & set(parser._option_string_actions)
    for removed in removed_options:
        with pytest.raises(SystemExit):
            parser.parse_args([removed])
    with pytest.raises(SystemExit):
        parser.parse_args(["package", "upload", "--path", "."])


def test_control_protocol_routes_only_short_control_notices() -> None:
    coordinator = _Coordinator()
    client = BackendWebSocketClient(
        "ws://backend.example/api/v1/ws/schedule",
        coordinator_getter=lambda: coordinator,
    )
    asyncio.run(client._process_message("backend_session", {"session_uuid": "s"}))
    asyncio.run(client._process_message("backend_change", {"command_uuid": "c"}))
    asyncio.run(client._process_message("edge_change_ack", {"through_sequence": 1}))
    asyncio.run(client._process_message("job_start", {"job_id": "removed"}))
    assert coordinator.sessions == [{"session_uuid": "s"}]
    assert coordinator.notices == [{"command_uuid": "c"}]
    assert coordinator.acks == [{"through_sequence": 1}]


def test_control_protocol_publishes_only_edge_change_index() -> None:
    coordinator = _Coordinator()
    client = BackendWebSocketClient(
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
