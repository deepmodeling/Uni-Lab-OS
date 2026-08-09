"""``hostlink`` backend startup entrypoints."""

from __future__ import annotations

from typing import Any, Optional

from unilabos.basic.main_basic_run import build_runtime
from unilabos.hostlink.backend import HostLinkBackendRuntime


_runtime: Optional[HostLinkBackendRuntime] = None


def validate_environment() -> None:
    """HostLink backend only depends on the Python driver runtime."""


def get_runtime() -> Optional[HostLinkBackendRuntime]:
    return _runtime


def _run(devices_config: Any, *, is_slave: bool) -> None:
    global _runtime
    _runtime = HostLinkBackendRuntime(
        build_runtime(devices_config),
        is_slave=is_slave,
    )
    _runtime.start()
    try:
        while not _runtime.local.wait(timeout=1.0):
            pass
    finally:
        _runtime.stop()


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
        resources_config,
        resources_edge_config,
        graph,
        controllers_config,
        bridges,
        visual,
        resources_mesh_config,
        args,
        kwargs,
    )
    _run(devices_config, is_slave=False)


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
        resources_config,
        resources_edge_config,
        graph,
        controllers_config,
        bridges,
        visual,
        resources_mesh_config,
        args,
        kwargs,
    )
    _run(devices_config, is_slave=True)


__all__ = ["get_runtime", "main", "slave", "validate_environment"]
