from __future__ import annotations

import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from unilabos.app import backend as backend_module
from unilabos.app.backend import (
    BACKEND_NAMES,
    BackendConfigurationError,
    normalize_backend_name,
    resolve_driver_backends,
    resolve_backend_selection,
    start_backend,
)
from unilabos.app.main import parse_args
from unilabos.config.config import BasicConfig
from unilabos.dora import main_dora_run


def test_public_backend_names_include_hostlink() -> None:
    assert BACKEND_NAMES == ("basic", "hostlink", "ros2", "dora")
    assert BasicConfig.backend == "ros2"


@pytest.mark.parametrize(
    ("value", "canonical"),
    [
        ("basic", "basic"),
        ("hostlink", "hostlink"),
        ("simple", "basic"),
        ("ros", "ros2"),
        ("ros2", "ros2"),
        ("dora", "dora"),
    ],
)
def test_backend_names_and_legacy_aliases(value: str, canonical: str) -> None:
    assert normalize_backend_name(value) == canonical


def test_automancer_placeholder_is_not_selectable() -> None:
    with pytest.raises(BackendConfigurationError, match="从未实现"):
        normalize_backend_name("automancer")


def test_backend_specific_bridge_defaults() -> None:
    assert resolve_backend_selection("ros2").app_bridges == (
        "websocket",
        "fastapi",
    )
    assert resolve_backend_selection("basic").app_bridges == ()
    assert resolve_backend_selection("hostlink").app_bridges == ()
    assert resolve_backend_selection("dora").app_bridges == ()


def test_backend_capability_validation() -> None:
    with pytest.raises(BackendConfigurationError, match="不支持应用桥"):
        resolve_backend_selection("dora", ["websocket"])
    with pytest.raises(BackendConfigurationError, match="不支持 --is_slave"):
        resolve_backend_selection("basic", is_slave=True)
    with pytest.raises(BackendConfigurationError, match="不支持 --visual"):
        resolve_backend_selection("dora", visual="rviz")
    assert resolve_backend_selection("hostlink", is_slave=True).name == "hostlink"


def test_registry_driver_backend_defaults_and_explicit_support() -> None:
    assert resolve_driver_backends({"type": "python"}) == (
        "basic",
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


def test_cli_shows_canonical_names_and_accepts_aliases() -> None:
    parser = parse_args()
    assert parser.parse_args(["--backend", "ros"]).backend == "ros2"
    assert parser.parse_args(["--backend", "simple"]).backend == "basic"
    assert parser.parse_args(["--backend", "dora"]).backend == "dora"
    assert parser.parse_args(["--backend", "hostlink"]).backend == "hostlink"
    help_text = parser.format_help()
    assert "{basic,hostlink,ros2,dora}" in help_text
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
    thread = start_backend("basic", object(), object())
    thread.join(timeout=2)

    assert called.is_set()
    assert imported == ["unilabos.basic.main_basic_run"]
    assert thread.name == "backend-basic"
    assert received[0][2] == []


def test_dora_preflight_reports_optional_dependencies(monkeypatch) -> None:
    monkeypatch.setattr(main_dora_run.runtime, "dora_binary", lambda: None)
    monkeypatch.setattr(main_dora_run.importlib.util, "find_spec", lambda name: None)

    with pytest.raises(RuntimeError) as exc_info:
        main_dora_run.validate_environment()

    message = str(exc_info.value)
    assert "dora-cli" in message
    assert "dora-rs" in message
    assert "pyarrow" in message


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
