from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

import pytest

from unilabos.app.backend_protocol.control import ControlWebSocketClient
from unilabos.app.communication import (
    APP_BRIDGES,
    COMMUNICATION_PROTOCOL,
    CommunicationClientFactory,
)
from unilabos.app.cli.router import run_package_command
from unilabos.app.main import main as app_main
from unilabos.app.main import parse_args
from unilabos.app.web.client import HTTPClient
from unilabos.app.ws_client import MessageProcessor, WebSocketClient
from unilabos.config.config import BasicConfig, _update_config_from_module
from unilabos.device_runtime.resource import AuthorityResourceService
from unilabos.legacy_support import configure_legacy_support
from unilabos.legacy_support.http import (
    LegacyHTTPClient,
    get_legacy_http_client,
    reset_legacy_http_client,
)
from unilabos.legacy_support.websocket import LegacyWebSocketClient


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


class _Response:
    status_code = 200
    text = "ok"

    def __init__(self, payload: object) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self._payload


class _Session:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []

    def get(self, url: str, **kwargs) -> _Response:
        self.calls.append(("GET", url, kwargs))
        return _Response({"code": 0, "data": [{"uuid": "material-1"}]})

    def post(self, url: str, **kwargs) -> _Response:
        self.calls.append(("POST", url, kwargs))
        return _Response({"code": 0, "data": [{"uuid": "material-1"}]})


@pytest.fixture(autouse=True)
def _reset_legacy_mode():
    CommunicationClientFactory.reset_client()
    reset_legacy_http_client()
    configure_legacy_support(False)
    yield
    CommunicationClientFactory.reset_client()
    reset_legacy_http_client()
    configure_legacy_support(False)


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


def test_factory_selects_legacy_payload_only_from_global_switch() -> None:
    control = CommunicationClientFactory.create_client()
    configure_legacy_support(True)
    legacy = CommunicationClientFactory.create_client()
    assert isinstance(control, ControlWebSocketClient)
    assert isinstance(legacy, LegacyWebSocketClient)
    assert isinstance(legacy, WebSocketClient)


def test_cli_exposes_only_legacy_compatibility_switch() -> None:
    parser = parse_args()
    assert parser.parse_args([]).legacy is False
    assert parser.parse_args(["--legacy"]).legacy is True
    for removed in ("--app_bridges", "--backend_protocol", "--communication_protocol"):
        with pytest.raises(SystemExit):
            parser.parse_args([removed, "old"])


def test_old_package_upload_is_preserved_but_requires_legacy() -> None:
    parser = parse_args()
    values = vars(
        parser.parse_args(["package", "upload", "--path", "."])
    )
    assert values["package_action"] == "upload"
    with pytest.raises(SystemExit):
        run_package_command(values)


def test_old_registry_upload_requires_legacy(monkeypatch) -> None:
    parser = parse_args()
    assert parser.parse_args(["--legacy", "--upload_registry"]).upload_registry

    monkeypatch.setattr(sys, "argv", ["unilab", "--upload_registry"])
    with pytest.raises(SystemExit):
        app_main()


def test_old_http_client_is_available_only_in_legacy_mode() -> None:
    with pytest.raises(RuntimeError, match="restart with --legacy"):
        get_legacy_http_client()
    configure_legacy_support(True)
    assert isinstance(get_legacy_http_client(), LegacyHTTPClient)


def test_old_http_api_methods_live_only_on_legacy_client() -> None:
    assert not hasattr(HTTPClient, "workflow_import")
    assert not hasattr(HTTPClient, "resource_registry")
    assert not hasattr(HTTPClient, "resource_get")
    assert not hasattr(HTTPClient, "resource_tree_get")
    assert not hasattr(HTTPClient, "material_bench_discard")
    assert hasattr(LegacyHTTPClient, "workflow_import")
    assert hasattr(LegacyHTTPClient, "resource_registry")
    assert hasattr(LegacyHTTPClient, "resource_get")
    assert hasattr(LegacyHTTPClient, "resource_tree_get")
    assert hasattr(LegacyHTTPClient, "material_bench_discard")


def test_legacy_material_calls_delegate_to_new_authority(monkeypatch) -> None:
    class _TreeSet:
        @staticmethod
        def dump():
            return [[{"uuid": "material-1"}]]

    by_id_calls = []
    by_uuid_calls = []
    delete_calls = []
    monkeypatch.setattr(
        AuthorityResourceService,
        "get_resource_by_id_sync",
        lambda _self, resource_id, with_children: (
            by_id_calls.append((resource_id, with_children)) or _TreeSet()
        ),
    )
    monkeypatch.setattr(
        AuthorityResourceService,
        "get_resources_sync",
        lambda _self, uuids, with_children: (
            by_uuid_calls.append((uuids, with_children)) or _TreeSet()
        ),
    )
    monkeypatch.setattr(
        AuthorityResourceService,
        "delete_resources_sync",
        lambda _self, device_id, device_uuid, uuids: (
            delete_calls.append((device_id, device_uuid, uuids)) or list(uuids)
        ),
    )

    configure_legacy_support(True)
    client = LegacyHTTPClient(remote_addr="https://old.example/api/v1", auth="secret")
    session = _Session()
    client._session = session

    assert client.resource_get("material-1") == {
        "code": 0,
        "data": [{"uuid": "material-1"}],
    }
    assert client.resource_tree_get(["material-1"], True) == [
        {"uuid": "material-1"}
    ]
    assert client.material_bench_discard(["material-1"]) == {
        "code": 0,
        "uuids": ["material-1"],
    }
    assert by_id_calls == [("material-1", False)]
    assert by_uuid_calls == [(["material-1"], True)]
    assert delete_calls == [("legacy", "legacy", ["material-1"])]
    assert session.calls == []
    assert not hasattr(client, "request_startup_json")


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
