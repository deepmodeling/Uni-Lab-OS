"""ROS2 networking settings exchanged during the HostLink handshake.

HostLink is only the control channel.  Device actions and node registration
remain on ROS2.  The Slave applies the values below before ``rclpy.init`` so
both processes join the same ROS graph.
"""

from __future__ import annotations

import os
import socket
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, MutableMapping, Optional, Tuple

_VALID_DISCOVERY_RANGES = ("SYSTEM_DEFAULT", "SUBNET", "LOCALHOST", "OFF")


@dataclass
class RosNetworkInfo:
    """Serializable ROS2 network policy advertised by the Host."""

    domain_id: Optional[int] = None
    automatic_discovery_range: str = ""
    static_peers: List[str] = field(default_factory=list)
    discovery_server: str = ""
    discovery_server_disabled: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "RosNetworkInfo":
        payload = data or {}
        domain = payload.get("domain_id")
        return cls(
            domain_id=int(domain) if domain is not None else None,
            automatic_discovery_range=str(
                payload.get("automatic_discovery_range") or ""
            ).upper(),
            static_peers=[
                str(peer).strip()
                for peer in payload.get("static_peers") or []
                if str(peer).strip()
            ],
            discovery_server=str(payload.get("discovery_server") or "").strip(),
            discovery_server_disabled=bool(
                payload.get("discovery_server_disabled", False)
            ),
        )


def validate_domain_id(domain_id: Optional[int]) -> Optional[int]:
    """Validate the portable ROS2 domain range."""

    if domain_id is None:
        return None
    value = int(domain_id)
    if not 0 <= value <= 232:
        raise ValueError("ROS domain id must be between 0 and 232")
    return value


def build_host_ros_info(
    host_ip: str = "",
    domain_id: Optional[int] = None,
    discovery_range: str = "",
    static_peers: Optional[List[str]] = None,
    discovery_server: str = "",
    discovery_server_disabled: bool = False,
    environ: Optional[MutableMapping[str, str]] = None,
) -> RosNetworkInfo:
    """Resolve explicit Host settings, then fall back to its environment."""

    env = environ if environ is not None else os.environ
    if domain_id is None:
        raw_domain = env.get("ROS_DOMAIN_ID", "").strip()
        domain_id = int(raw_domain) if raw_domain else None
    domain_id = validate_domain_id(domain_id)

    discovery_range = (
        discovery_range
        or env.get("ROS_AUTOMATIC_DISCOVERY_RANGE", "")
    ).strip().upper()
    if discovery_range and discovery_range not in _VALID_DISCOVERY_RANGES:
        raise ValueError(
            f"invalid ROS discovery range {discovery_range!r}; "
            f"expected one of {_VALID_DISCOVERY_RANGES}"
        )

    if static_peers is None:
        static_peers = [
            peer.strip()
            for peer in env.get("ROS_STATIC_PEERS", "").split(";")
            if peer.strip()
        ]
    if host_ip and host_ip not in static_peers:
        static_peers.append(host_ip)

    if not discovery_server and not discovery_server_disabled:
        discovery_server = env.get("ROS_DISCOVERY_SERVER", "").strip()
    if discovery_server:
        host, port = parse_host_port(discovery_server)
        discovery_server = format_host_port(host, port)

    return RosNetworkInfo(
        domain_id=domain_id,
        automatic_discovery_range=discovery_range,
        static_peers=list(dict.fromkeys(static_peers)),
        discovery_server=discovery_server,
        discovery_server_disabled=discovery_server_disabled,
    )


def apply_ros_network_env(
    info: RosNetworkInfo,
    environ: Optional[MutableMapping[str, str]] = None,
) -> Dict[str, str]:
    """Apply Host networking settings before ``rclpy.init``."""

    env = environ if environ is not None else os.environ
    applied: Dict[str, str] = {}
    domain_id = validate_domain_id(info.domain_id)
    if domain_id is not None:
        applied["ROS_DOMAIN_ID"] = str(domain_id)
    if info.automatic_discovery_range:
        discovery_range = info.automatic_discovery_range.upper()
        if discovery_range not in _VALID_DISCOVERY_RANGES:
            raise ValueError(f"invalid ROS discovery range {discovery_range!r}")
        applied["ROS_AUTOMATIC_DISCOVERY_RANGE"] = discovery_range
    if info.static_peers:
        applied["ROS_STATIC_PEERS"] = ";".join(dict.fromkeys(info.static_peers))
    if info.discovery_server_disabled:
        env.pop("ROS_DISCOVERY_SERVER", None)
    elif info.discovery_server:
        host, port = parse_host_port(info.discovery_server)
        applied["ROS_DISCOVERY_SERVER"] = format_host_port(host, port)
    env.update(applied)
    return applied


def parse_host_port(endpoint: str) -> Tuple[str, int]:
    """Parse ``host:port`` and ``[IPv6]:port`` endpoints."""

    value = str(endpoint or "").strip()
    if value.startswith("["):
        close = value.find("]")
        if close <= 1 or value[close + 1 : close + 2] != ":":
            raise ValueError(f"invalid host:port endpoint: {endpoint!r}")
        host, port_text = value[1:close], value[close + 2 :]
    else:
        host, separator, port_text = value.rpartition(":")
        if not separator:
            raise ValueError(f"invalid host:port endpoint: {endpoint!r}")
    if not host or not port_text.isdigit():
        raise ValueError(f"invalid host:port endpoint: {endpoint!r}")
    port = int(port_text)
    if not 1 <= port <= 65535:
        raise ValueError(f"port out of range in endpoint: {endpoint!r}")
    return host, port


def parse_host_target(endpoint: str, default_port: int) -> Tuple[str, int]:
    """Parse a HostNode address with an optional port."""

    value = str(endpoint or "").strip()
    if not value:
        raise ValueError("HostNode address cannot be empty")
    if value.startswith("["):
        close = value.find("]")
        if close <= 1:
            raise ValueError(f"invalid HostNode address: {endpoint!r}")
        host = value[1:close]
        remainder = value[close + 1 :]
        if not remainder:
            return host, int(default_port)
        if not remainder.startswith(":"):
            raise ValueError(f"invalid HostNode address: {endpoint!r}")
        return parse_host_port(value)
    if value.count(":") == 1:
        host, port_text = value.rsplit(":", 1)
        if port_text.isdigit():
            return parse_host_port(value)
        raise ValueError(f"invalid HostNode address: {endpoint!r}")
    if value.count(":") > 1:
        # Unbracketed IPv6 without a port.
        return value, int(default_port)
    return value, int(default_port)


def format_host_port(host: str, port: int) -> str:
    clean_host = str(host or "").strip()
    if not clean_host:
        raise ValueError("host cannot be empty")
    if not 1 <= int(port) <= 65535:
        raise ValueError(f"port out of range: {port}")
    if ":" in clean_host and not clean_host.startswith("["):
        clean_host = f"[{clean_host}]"
    return f"{clean_host}:{int(port)}"


def detect_local_ip(probe_addr: str = "8.8.8.8") -> str:
    """Detect the preferred outbound IPv4 address without sending traffic."""

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect((probe_addr, 80))
            return str(sock.getsockname()[0])
    except OSError:
        return ""


__all__ = [
    "RosNetworkInfo",
    "apply_ros_network_env",
    "build_host_ros_info",
    "detect_local_ip",
    "format_host_port",
    "parse_host_port",
    "parse_host_target",
    "validate_domain_id",
]
