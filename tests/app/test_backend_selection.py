from __future__ import annotations

import subprocess
import sys
import threading
import time
from types import ModuleType, SimpleNamespace

import pytest

from unilabos.app import backend as backend_module
from unilabos.app.execution_adapter import get_execution_adapter
from unilabos.app.backend import (
    BACKEND_NAMES,
    BackendConfigurationError,
    normalize_backend_name,
    resolve_driver_backends,
    resolve_backend_selection,
    start_backend,
)
from unilabos.app.main import parse_args
from unilabos.basic.runtime import BasicRuntime
from unilabos.config.config import BasicConfig, HostLinkConfig
from unilabos.hostlink import main_hostlink_run


def test_only_public_communication_backends_are_selectable() -> None:
    assert BACKEND_NAMES == ("hostlink", "ros2")
    assert BasicConfig.backend == "ros2"


@pytest.mark.parametrize(
    ("value", "canonical"),
    [
        ("hostlink", "hostlink"),
        ("ros2", "ros2"),
    ],
)
def test_public_backend_names(value: str, canonical: str) -> None:
    assert normalize_backend_name(value) == canonical


@pytest.mark.parametrize("value", ["basic", "simple", "dora", "ros"])
def test_internal_experimental_and_legacy_backend_names_are_rejected(
    value: str,
) -> None:
    with pytest.raises(BackendConfigurationError):
        normalize_backend_name(value)


def test_automancer_placeholder_is_not_selectable() -> None:
    with pytest.raises(BackendConfigurationError, match="从未实现"):
        normalize_backend_name("automancer")


def test_backend_specific_bridge_defaults() -> None:
    assert resolve_backend_selection("ros2").app_bridges == (
        "websocket",
        "fastapi",
    )
    assert resolve_backend_selection("hostlink").app_bridges == (
        "websocket",
        "fastapi",
    )
    assert resolve_backend_selection(
        "hostlink",
        is_slave=True,
    ).app_bridges == ()


def test_backend_capability_validation() -> None:
    assert resolve_backend_selection(
        "hostlink",
        ["websocket"],
    ).app_bridges == ("websocket",)
    with pytest.raises(BackendConfigurationError, match="Slave 不启动"):
        resolve_backend_selection(
            "hostlink",
            ["websocket"],
            is_slave=True,
        )
    with pytest.raises(BackendConfigurationError, match="不支持 --visual"):
        resolve_backend_selection("hostlink", visual="rviz")
    assert resolve_backend_selection("hostlink", is_slave=True).name == "hostlink"


def test_registry_driver_backend_defaults_and_explicit_support() -> None:
    assert resolve_driver_backends({"type": "python"}) == (
        "hostlink",
        "ros2",
    )
    assert resolve_driver_backends({"type": "ros2"}) == ("ros2",)
    assert resolve_driver_backends(
        {"type": "python", "supported_backends": ["hostlink", "ros2"]}
    ) == ("hostlink", "ros2")
    with pytest.raises(BackendConfigurationError, match="未知 backend"):
        resolve_driver_backends(
            {"type": "python", "supported_backends": ["missing"]}
        )
    with pytest.raises(BackendConfigurationError, match="未知 backend"):
        resolve_driver_backends(
            {"type": "python", "supported_backends": ["basic"]}
        )


def test_cli_shows_and_accepts_only_public_backend_names() -> None:
    parser = parse_args()
    assert parser.parse_args(["--backend", "hostlink"]).backend == "hostlink"
    assert parser.parse_args(["--backend", "ros2"]).backend == "ros2"
    for value in ("basic", "simple", "dora", "ros"):
        with pytest.raises(SystemExit):
            parser.parse_args(["--backend", value])
    help_text = parser.format_help()
    assert "{hostlink,ros2}" in help_text
    assert "automancer" not in help_text


def test_start_backend_imports_only_selected_profile(monkeypatch) -> None:
    called = threading.Event()
    received = []

    def main(*args) -> None:
        received.append(args)
        called.set()

    fake_module = SimpleNamespace(main=main, slave=lambda *args: None)
    imported = []

    def fake_import(name: str):
        imported.append(name)
        return fake_module

    monkeypatch.setattr(backend_module.importlib, "import_module", fake_import)
    thread = start_backend("hostlink", object(), object())
    thread.join(timeout=2)

    assert called.is_set()
    assert imported == ["unilabos.hostlink.main_hostlink_run"]
    assert thread.name == "backend-hostlink"
    assert received[0][2] == []


def test_hostlink_still_builds_its_internal_basic_runtime() -> None:
    runtime = main_hostlink_run.build_runtime(None, backend_name="hostlink")
    assert isinstance(runtime, BasicRuntime)
    assert runtime.backend_name == "hostlink"


def test_hostlink_host_registers_direct_execution_adapter(monkeypatch) -> None:
    monkeypatch.setattr(BasicConfig, "backend", "hostlink")
    monkeypatch.setattr(BasicConfig, "machine_name", "test-hostlink-host")
    monkeypatch.setattr(HostLinkConfig, "enable", True)
    monkeypatch.setattr(HostLinkConfig, "bind", "127.0.0.1")
    monkeypatch.setattr(HostLinkConfig, "port", 0)
    thread = threading.Thread(
        target=main_hostlink_run.main,
        args=(None, object()),
        daemon=True,
    )
    thread.start()
    try:
        deadline = time.monotonic() + 3
        adapter = None
        while adapter is None and time.monotonic() < deadline:
            adapter = get_execution_adapter(0)
            time.sleep(0.01)
        assert adapter is not None
        assert adapter.runtime is main_hostlink_run.get_runtime()
    finally:
        runtime = main_hostlink_run.get_runtime()
        if runtime is not None:
            runtime.request_stop()
        thread.join(timeout=3)
    assert not thread.is_alive()
    assert main_hostlink_run.get_runtime() is None
    assert get_execution_adapter(0) is None


def test_web_package_does_not_eagerly_import_ros_modules() -> None:
    code = (
        "import sys; import unilabos.app.web; "
        "assert not any(name.startswith('unilabos.ros') for name in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr


def test_hostlink_entrypoint_does_not_import_ros_runtime() -> None:
    code = (
        "import sys; "
        "from unilabos.config.config import BasicConfig; "
        "BasicConfig.backend = 'hostlink'; "
        "import unilabos.hostlink.main_hostlink_run; "
        "from unilabos.app.web.utils.ros_utils import update_ros_node_info; "
        "update_ros_node_info(); "
        "assert 'rclpy' not in sys.modules; "
        "assert not any(name.startswith('unilabos.ros') for name in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr


def test_ros_host_info_keeps_real_action_client_details(monkeypatch) -> None:
    from unilabos.app.web.utils import host_utils

    action_client = object()
    adapter = SimpleNamespace(
        device_id="host_node",
        devices_names={"device-1": "/devices"},
        _online_devices={"/devices/device-1"},
        device_machine_names={"device-1": "ros-host"},
        _subscribed_topics={"/devices/device-1/status"},
        _action_clients={"/devices/device-1/run": action_client},
        _action_value_mappings={},
        device_status={},
        device_status_timestamps={},
    )
    action_utils = ModuleType("unilabos.app.web.utils.action_utils")
    action_utils.get_action_info = lambda client, full_name: {
        "client": client,
        "action_path": full_name,
    }
    monkeypatch.setitem(
        sys.modules,
        "unilabos.app.web.utils.action_utils",
        action_utils,
    )
    monkeypatch.setattr(host_utils, "get_execution_adapter", lambda _timeout: adapter)
    monkeypatch.setattr(BasicConfig, "is_host_mode", True)

    info = host_utils.get_host_node_info()

    assert info["action_clients"]["/devices/device-1/run"] == {
        "client": action_client,
        "action_path": "/devices/device-1/run",
    }
