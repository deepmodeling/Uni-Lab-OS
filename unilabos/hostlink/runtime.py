"""ROS2/HostLink compatibility facade backed by the Edge microbackend.

The ROS backend used to own a second HostLink server/client implementation in
this module.  That split made the declared microbackend networking service
dead code and allowed the two lifecycle owners to drift.  Keep the historical
function names for embedders, but delegate every operation to
``unilabos.server.scheduler.host_network``.

The direct ``hostlink`` backend does not use this facade; its
``HostLinkBackendRuntime`` owns the transport that executes Python drivers.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional, Tuple

from unilabos.hostlink.client import HostLinkClient
from unilabos.hostlink.server import HostLinkServer


def setup_hostlink_server() -> Optional[HostLinkServer]:
    """Start/reuse the microbackend-owned ROS2 networking listener."""

    from unilabos.server.scheduler.host_network import setup_host_network_service

    service = setup_host_network_service()
    return service.server if service is not None else None


def setup_hostlink_client(
    device_ids: Optional[Iterable[str]] = None,
    *,
    wait_for_host: Optional[bool] = None,
) -> Tuple[Optional[HostLinkClient], Optional[int]]:
    """Connect/reuse the microbackend-owned Slave networking client."""

    from unilabos.server.scheduler.host_network import setup_slave_network_client

    return setup_slave_network_client(
        device_ids=device_ids,
        wait_for_host=wait_for_host,
    )


def startup_device_ids(devices_config: Any) -> list[str]:
    """Compatibility wrapper for startup graph identity extraction."""

    from unilabos.server.scheduler.host_network import startup_device_ids as extract

    return extract(devices_config)


def shutdown_hostlink() -> None:
    """Stop the microbackend-owned ROS2 HostLink services."""

    from unilabos.server.scheduler.host_network import shutdown_network_services

    shutdown_network_services()


__all__ = [
    "setup_hostlink_client",
    "setup_hostlink_server",
    "shutdown_hostlink",
    "startup_device_ids",
]
