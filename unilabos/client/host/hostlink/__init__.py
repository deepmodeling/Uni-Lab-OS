"""Host/Slave control channel and optional no-ROS distributed backend."""

from __future__ import annotations

from typing import Any


def __getattr__(name: str) -> Any:
    """避免只导入轻量 adapter registry 时加载完整驱动和服务栈。"""

    if name == "HostLinkBackend":
        from unilabos.backend.hostlink.backend import HostLinkBackend

        return HostLinkBackend
    if name in {"HostLinkClient", "get_hostlink_client"}:
        from unilabos.backend.hostlink.client import HostLinkClient, get_hostlink_client

        return {
            "HostLinkClient": HostLinkClient,
            "get_hostlink_client": get_hostlink_client,
        }[name]
    if name in {"HostLinkServer", "get_hostlink_server"}:
        from unilabos.backend.hostlink.server import HostLinkServer, get_hostlink_server

        return {
            "HostLinkServer": HostLinkServer,
            "get_hostlink_server": get_hostlink_server,
        }[name]
    raise AttributeError(name)

__all__ = [
    "HostLinkClient",
    "HostLinkBackend",
    "HostLinkServer",
    "get_hostlink_client",
    "get_hostlink_server",
]
