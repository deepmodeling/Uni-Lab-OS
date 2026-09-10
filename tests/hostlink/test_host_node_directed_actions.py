"""HostLink reports identity; HostNode creates and verifies ROS Action clients."""

from types import SimpleNamespace

from unilabos.ros.nodes.presets import host_node as host_node_module
from unilabos.ros.nodes.presets.host_node import HostNode
from unilabos_msgs.action import SetPumpPosition, StrSingleInput


class _Logger:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def info(self, message: str) -> None:
        self.messages.append(message)

    def warning(self, message: str) -> None:
        self.messages.append(message)


class _ActionClient:
    def __init__(self, _node, action_type, action_id, callback_group=None) -> None:
        self.action_type = action_type
        self.action_id = action_id
        self.callback_group = callback_group

    def server_is_ready(self) -> bool:
        return self.action_id.endswith("/set_position")


def _fake_host(*, ready_check=None):
    logger = _Logger()
    reported_locks: list[list[tuple[str, str]]] = []
    fake = SimpleNamespace(
        devices_names={},
        _action_value_mappings={},
        _action_clients={},
        _online_devices=set(),
        callback_group=object(),
        lab_logger=lambda: logger,
        _report_action_locks_free=lambda pairs: reported_locks.append(pairs),
        _resolve_reported_action_type=HostNode._resolve_reported_action_type,
        _routable_reported_action_pairs=(
            lambda device_id: HostNode._routable_reported_action_pairs(fake, device_id)
        ),
        _has_ready_action_client=(ready_check or HostNode._has_ready_action_client),
    )
    return fake, logger, reported_locks


def test_reported_device_creates_matching_ros_clients(monkeypatch):
    monkeypatch.setattr(host_node_module, "ActionClient", _ActionClient)
    fake, _logger, reported_locks = _fake_host()

    HostNode._register_reported_remote_device(
        fake,
        "pump-a",
        {
            "set_position": {"type": SetPumpPosition},
            "read_status": {"type": "UniLabJsonCommand"},
            "wait_until_idle": {"type": "UniLabJsonCommandAsync"},
        },
    )

    assert fake.devices_names == {"pump-a": "/devices/pump-a"}
    assert set(fake._action_clients) == {
        "/devices/pump-a/set_position",
        "/devices/pump-a/_execute_driver_command",
        "/devices/pump-a/_execute_driver_command_async",
    }
    assert (
        fake._action_clients["/devices/pump-a/set_position"].action_type
        is SetPumpPosition
    )
    assert (
        fake._action_clients["/devices/pump-a/_execute_driver_command"].action_type
        is StrSingleInput
    )
    assert "/devices/pump-a/pump-a" in fake._online_devices
    assert reported_locks == [
        [
            ("pump-a", "set_position"),
            ("pump-a", "read_status"),
            ("pump-a", "wait_until_idle"),
        ]
    ]


def test_reported_identity_is_not_online_until_ros_endpoint_matches(monkeypatch):
    monkeypatch.setattr(host_node_module, "ActionClient", _ActionClient)
    fake, logger, reported_locks = _fake_host(
        ready_check=lambda _clients, wait_timeout=0.0: False
    )

    HostNode._register_reported_remote_device(
        fake,
        "pump-a",
        {"set_position": {"type": SetPumpPosition}},
    )

    assert fake._online_devices == set()
    assert reported_locks == []
    assert any(
        "waiting for ROS Action endpoint" in message for message in logger.messages
    )


def test_action_type_resolver_accepts_wire_and_dotted_names():
    assert (
        HostNode._resolve_reported_action_type("unilabos_msgs/action/SetPumpPosition")
        is SetPumpPosition
    )
    assert (
        HostNode._resolve_reported_action_type(
            "unilabos_msgs.action._set_pump_position.SetPumpPosition"
        )
        is SetPumpPosition
    )


def test_ready_probe_ignores_a_client_shutting_down():
    class Broken:
        def server_is_ready(self):
            raise RuntimeError("context already shut down")

    class Ready:
        def server_is_ready(self):
            return True

    assert HostNode._has_ready_action_client([Broken(), Ready()]) is True
