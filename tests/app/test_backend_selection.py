from __future__ import annotations

import subprocess
import sys
import threading
import time
from types import SimpleNamespace

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
from unilabos.app.cli.parser import build_parser
from unilabos.hostlink.local_runtime import HostLinkLocalRuntime
from unilabos.config.config import BasicConfig, HostLinkConfig
from unilabos.hostlink import main_hostlink_run
from unilabos.server.startup import resolve_database_paths


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


def test_backend_selection_has_no_application_bridge_configuration() -> None:
    assert resolve_backend_selection("ros2").name == "ros2"
    assert resolve_backend_selection("hostlink", is_slave=True).name == "hostlink"
    assert not hasattr(resolve_backend_selection("ros2"), "app_bridges")


def test_backend_capability_validation() -> None:
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
    parser = build_parser()
    assert parser.parse_args(["--backend", "hostlink"]).backend == "hostlink"
    assert parser.parse_args(["--backend", "ros2"]).backend == "ros2"
    for value in ("basic", "simple", "dora", "ros"):
        with pytest.raises(SystemExit):
            parser.parse_args(["--backend", value])
    help_text = parser.format_help()
    assert "{hostlink,ros2}" in help_text
    assert "automancer" not in help_text


def test_workflow_upload_uses_grouped_cli_only() -> None:
    parser = build_parser()
    parsed = parser.parse_args(
        [
            "workflow",
            "upload",
            "-f",
            "workflow.json",
            "-n",
            "demo",
            "--tags",
            "chemistry",
        ]
    )
    assert parsed.command == "workflow"
    assert parsed.workflow_command == "upload"
    assert parsed.workflow_file == "workflow.json"
    assert parsed.workflow_name == "demo"
    assert parsed.tags == ["chemistry"]

    with pytest.raises(SystemExit):
        parser.parse_args(["workflow_upload", "-f", "workflow.json"])


def test_local_scheduler_cli_is_removed() -> None:
    parser = build_parser()
    parsed = parser.parse_args([])
    assert not hasattr(parsed, "edge_scheduler")
    assert not hasattr(parsed, "scheduler_authority_profile")

    for arguments in (
        ["--edge-scheduler"],
        ["--scheduler-authority-profile", "local_scheduler"],
        ["--edge-inventory-db", "inventory.db"],
    ):
        with pytest.raises(SystemExit):
            parser.parse_args(arguments)


def test_server_database_cli_resolves_only_the_four_new_files(tmp_path) -> None:
    parsed = build_parser().parse_args(
        [
            "--server-database-root",
            str(tmp_path),
            "--runtime-db",
            "control.db",
        ]
    )

    paths = resolve_database_paths(vars(parsed), working_dir=tmp_path)

    assert paths.runtime_db == (tmp_path / "control.db").resolve()
    assert {path.name for path in paths.as_mapping().values()} == {
        "control.db",
        "materials.db",
        "telemetry.db",
        "history.db",
    }
    assert list(tmp_path.glob("*.db")) == []


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


def test_hostlink_builds_its_local_driver_runtime() -> None:
    runtime = main_hostlink_run.build_runtime(None)
    assert isinstance(runtime, HostLinkLocalRuntime)
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
