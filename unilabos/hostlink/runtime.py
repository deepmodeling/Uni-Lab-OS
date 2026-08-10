"""ROS-backend lifecycle wiring for HostLink.

This module intentionally has no inventory, scheduler or local-backend imports.
Its only responsibilities are Slave/device presence and ROS2 network settings.
"""

from __future__ import annotations

import atexit
import threading
from typing import Any, Iterable, Optional, Tuple

from unilabos.config.config import BasicConfig, HostLinkConfig
from unilabos.hostlink.client import (
    HostLinkClient,
    get_hostlink_client,
    set_hostlink_client,
)
from unilabos.hostlink.protocol import PROTOCOL_VERSION
from unilabos.hostlink.ros_assist import (
    RosNetworkInfo,
    apply_ros_network_env,
    build_host_ros_info,
    detect_local_ip,
    validate_domain_id,
)
from unilabos.hostlink.server import (
    HostLinkServer,
    get_hostlink_server,
    set_hostlink_server,
)
from unilabos.utils import logger

_host_lock = threading.Lock()
_slave_lock = threading.Lock()
_cleanup_lock = threading.Lock()
_cleanup_registered = False


def _register_cleanup() -> None:
    global _cleanup_registered
    with _cleanup_lock:
        if not _cleanup_registered:
            atexit.register(shutdown_hostlink)
            _cleanup_registered = True


def _configured_domain_id() -> Optional[int]:
    raw = str(HostLinkConfig.ros_domain_id or "").strip()
    return validate_domain_id(int(raw) if raw else None)


def _host_ros_info() -> RosNetworkInfo:
    advertise_ip = str(HostLinkConfig.advertise_ip or "").strip()
    if not advertise_ip and HostLinkConfig.bind not in {"", "0.0.0.0", "::"}:
        advertise_ip = str(HostLinkConfig.bind)
    advertise_ip = advertise_ip or detect_local_ip() or "127.0.0.1"
    static_peers = [
        peer.strip()
        for peer in str(HostLinkConfig.ros_static_peers or "").split(";")
        if peer.strip()
    ]
    configured_server = str(HostLinkConfig.ros_discovery_server or "").strip()
    server_disabled = configured_server.lower() in {"off", "none", "disabled"}
    return build_host_ros_info(
        host_ip=advertise_ip,
        domain_id=_configured_domain_id(),
        discovery_range=str(HostLinkConfig.ros_discovery_range or ""),
        static_peers=static_peers or None,
        discovery_server="" if server_disabled else configured_server,
        discovery_server_disabled=server_disabled,
    )


def setup_hostlink_server() -> Optional[HostLinkServer]:
    """Start the ROS Host listener and apply its own network policy."""

    if not HostLinkConfig.enable:
        return None
    with _host_lock:
        existing = get_hostlink_server()
        if existing is not None:
            return existing
        ros_info = _host_ros_info()
        applied = apply_ros_network_env(ros_info)
        if applied:
            logger.info(f"[HostLink] Host ROS2 network settings: {applied}")
        server = HostLinkServer(
            bind=str(HostLinkConfig.bind),
            port=int(HostLinkConfig.port),
            heartbeat_timeout=float(HostLinkConfig.heartbeat_timeout),
        )
        server.hello_payload = {
            "host_id": BasicConfig.machine_name,
            "host_name": BasicConfig.machine_name,
            "protocol_version": PROTOCOL_VERSION,
            "ros": ros_info.to_dict(),
        }
        try:
            server.start()
        except Exception as exc:  # noqa: BLE001 - retain legacy ROS discovery
            logger.error(f"[HostLink] listener unavailable, use legacy ROS discovery: {exc}")
            return None
        set_hostlink_server(server)
        _register_cleanup()
        return server


def setup_hostlink_client(
    device_ids: Optional[Iterable[str]] = None,
    *,
    wait_for_host: Optional[bool] = None,
) -> Tuple[Optional[HostLinkClient], Optional[int]]:
    """Connect a Slave to its configured Host before ROS initialization."""

    if not HostLinkConfig.enable or not str(HostLinkConfig.host or "").strip():
        return None, _configured_domain_id()
    if wait_for_host is None:
        wait_for_host = not BasicConfig.slave_no_host
    with _slave_lock:
        client = get_hostlink_client()
        if client is None:
            client = HostLinkClient(
                str(HostLinkConfig.host),
                int(HostLinkConfig.port),
                machine_name=BasicConfig.machine_name,
                device_ids=device_ids,
                heartbeat_interval=float(HostLinkConfig.heartbeat_interval),
                connect_timeout=float(HostLinkConfig.connect_timeout),
                request_timeout=float(HostLinkConfig.request_timeout),
            )
            set_hostlink_client(client)
            _register_cleanup()
        elif device_ids:
            client.configure_device_ids(device_ids)

    if client.online:
        connected = True
    elif wait_for_host:
        logger.info(
            f"[HostLink] waiting for HostNode {HostLinkConfig.host}:"
            f"{HostLinkConfig.port} before ROS2 initialization"
        )
        connected = client.connect_blocking(timeout=None)
    else:
        client.start()
        connected = client.online

    if not connected:
        logger.warning(
            f"[HostLink] HostNode {HostLinkConfig.host}:{HostLinkConfig.port} "
            "is offline; continuing with local ROS2 settings"
        )
        return client, _configured_domain_id()

    ros_info = client.hello_ros_info()
    if not HostLinkConfig.ros_assist_apply:
        return client, _configured_domain_id()
    # The address used by the successful TCP connection is known reachable and
    # should always be a static discovery peer, even on multi-NIC Hosts.
    host = str(HostLinkConfig.host)
    if host not in ros_info.static_peers:
        ros_info.static_peers.append(host)
    applied = apply_ros_network_env(ros_info)
    if applied:
        logger.info(f"[HostLink] applied Host ROS2 network settings: {applied}")
    return client, ros_info.domain_id


def startup_device_ids(devices_config: Any) -> list[str]:
    """Collect all device IDs from a Slave startup graph for Host discovery."""

    result: list[str] = []
    for node in getattr(devices_config, "all_nodes", []) or []:
        content = getattr(node, "res_content", None)
        if getattr(content, "type", "") != "device":
            continue
        device_id = str(getattr(content, "id", "") or "").strip()
        if device_id and device_id not in result:
            result.append(device_id)
    return result


def shutdown_hostlink() -> None:
    with _slave_lock:
        client = get_hostlink_client()
        set_hostlink_client(None)
    if client is not None:
        client.close()
    with _host_lock:
        server = get_hostlink_server()
        set_hostlink_server(None)
    if server is not None:
        server.stop()


__all__ = [
    "setup_hostlink_client",
    "setup_hostlink_server",
    "shutdown_hostlink",
    "startup_device_ids",
]
