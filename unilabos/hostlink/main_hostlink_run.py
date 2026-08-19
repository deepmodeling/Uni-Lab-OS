"""``hostlink`` backend startup entrypoints."""

from __future__ import annotations

from typing import Any, Optional

from unilabos.basic.main_basic_run import build_runtime
from unilabos.app.execution_adapter import (
    clear_execution_adapter,
    set_execution_adapter,
)
from unilabos.hostlink.backend import HostLinkBackendRuntime
from unilabos.hostlink.execution_adapter import HostLinkExecutionAdapter


_runtime: Optional[HostLinkBackendRuntime] = None
_execution_adapter: Optional[HostLinkExecutionAdapter] = None


def validate_environment() -> None:
    """HostLink backend only depends on the Python driver runtime."""


def get_runtime() -> Optional[HostLinkBackendRuntime]:
    return _runtime


def get_execution_adapter() -> Optional[HostLinkExecutionAdapter]:
    return _execution_adapter


def _run(
    devices_config: Any,
    resources_config: Any,
    *,
    is_slave: bool,
    bridges: Optional[list[Any]] = None,
) -> None:
    global _execution_adapter, _runtime
    runtime = HostLinkBackendRuntime(
        build_runtime(devices_config, backend_name="hostlink"),
        is_slave=is_slave,
    )
    _runtime = runtime
    try:
        runtime.start()
        if not is_slave:
            _execution_adapter = HostLinkExecutionAdapter(
                runtime,
                devices_config,
                resources_config,
                bridges=bridges,
            )
            _execution_adapter.start()
            set_execution_adapter(_execution_adapter)
            _execution_adapter.notify_ready()
        while not runtime.local.wait(timeout=1.0):
            pass
    finally:
        adapter, _execution_adapter = _execution_adapter, None
        if adapter is not None:
            clear_execution_adapter(adapter)
            adapter.stop()
        runtime.stop()
        if _runtime is runtime:
            _runtime = None


def main(
    devices_config: Any,
    resources_config: Any,
    resources_edge_config: Optional[list[dict[str, Any]]] = None,
    graph: Any = None,
    controllers_config: Optional[dict[str, Any]] = None,
    bridges: Optional[list[Any]] = None,
    visual: str = "disable",
    resources_mesh_config: Optional[dict[str, Any]] = None,
    *args: Any,
    **kwargs: Any,
) -> None:
    del (
        resources_edge_config,
        graph,
        controllers_config,
        visual,
        resources_mesh_config,
        args,
        kwargs,
    )
    _run(
        devices_config,
        resources_config,
        is_slave=False,
        bridges=bridges,
    )


def slave(
    devices_config: Any,
    resources_config: Any,
    resources_edge_config: Optional[list[dict[str, Any]]] = None,
    graph: Any = None,
    controllers_config: Optional[dict[str, Any]] = None,
    bridges: Optional[list[Any]] = None,
    visual: str = "disable",
    resources_mesh_config: Optional[dict[str, Any]] = None,
    *args: Any,
    **kwargs: Any,
) -> None:
    del (
        resources_edge_config,
        graph,
        controllers_config,
        visual,
        resources_mesh_config,
        args,
        kwargs,
    )
    _run(devices_config, resources_config, is_slave=True, bridges=None)


__all__ = [
    "get_execution_adapter",
    "get_runtime",
    "main",
    "slave",
    "validate_environment",
]
