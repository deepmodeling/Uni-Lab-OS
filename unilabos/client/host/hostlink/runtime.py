"""ROS2 networking facade backed by the Edge microbackend.

The public setup helpers delegate lifecycle ownership to
``unilabos.backend.hostlink.network``. The direct ``hostlink`` backend uses
``HostLinkBackend`` as the transport for Python drivers.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional, Tuple

from unilabos.backend.hostlink.client import HostLinkClient
from unilabos.backend.hostlink.server import HostLinkServer


def setup_hostlink_server() -> Optional[HostLinkServer]:
    """Start/reuse the microbackend-owned ROS2 networking listener."""

    from unilabos.backend.hostlink.network import setup_host_network_service

    service = setup_host_network_service()
    return service.server if service is not None else None


def setup_hostlink_client(
    device_ids: Optional[Iterable[str]] = None,
    *,
    wait_for_host: Optional[bool] = None,
) -> Tuple[Optional[HostLinkClient], Optional[int]]:
    """Connect/reuse the microbackend-owned Slave networking client."""

    from unilabos.backend.hostlink.network import setup_slave_network_client

    return setup_slave_network_client(
        device_ids=device_ids,
        wait_for_host=wait_for_host,
    )


def startup_device_ids(devices_config: Any) -> list[str]:
    """Compatibility wrapper for startup graph identity extraction."""

    from unilabos.backend.hostlink.network import startup_device_ids as extract

    return extract(devices_config)


def shutdown_hostlink() -> None:
    """Stop the microbackend-owned ROS2 HostLink services."""

    from unilabos.backend.hostlink.network import shutdown_network_services

    shutdown_network_services()


__all__ = [
    "setup_hostlink_client",
    "setup_hostlink_server",
    "shutdown_hostlink",
    "startup_device_ids",
]
