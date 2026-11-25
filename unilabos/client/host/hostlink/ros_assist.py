"""ROS2 networking settings exchanged during the HostLink handshake.

HostLink is only the control channel.  Device actions and node registration
remain on ROS2.  The Slave applies the values below before ``rclpy.init`` so
both processes join the same ROS graph.
"""

from __future__ import annotations

import functools
import ipaddress
import os
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import (
    Any,
    Dict,
    List,
    Mapping,
    MutableMapping,
    Optional,
    Sequence,
    Tuple,
)

_VALID_DISCOVERY_RANGES = ("SYSTEM_DEFAULT", "SUBNET", "LOCALHOST", "OFF")

#: 用户自己指定 Fast DDS 传输或默认配置文件的环境变量：设了其中任何一个就照用户的来，不改动
FASTDDS_USER_TRANSPORT_ENV = (
    "FASTRTPS_DEFAULT_PROFILES_FILE",
    "FASTDDS_DEFAULT_PROFILES_FILE",
    "FASTDDS_BUILTIN_TRANSPORTS",
)
NETWORK_ONLY_PROFILE_FILENAME = "fastdds_network_only.xml"


#: 回环自检的数据报大小：大于以太网 MTU，要拆 IP 分片才能送达
LOOPBACK_PROBE_BYTES = 8192


def udp_loopback_delivers(size: int, timeout: float = 0.5) -> bool:
    """往 127.0.0.1 发一个 ``size`` 字节的 UDP 数据报，本机原样收到时返回 True。"""

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver, socket.socket(
            socket.AF_INET, socket.SOCK_DGRAM
        ) as sender:
            receiver.bind(("127.0.0.1", 0))
            receiver.settimeout(timeout)
            sender.sendto(b"\0" * size, receiver.getsockname())
            data, _address = receiver.recvfrom(size + 1)
            return len(data) == size
    except OSError:
        return False


@functools.lru_cache(maxsize=1)
def loopback_carries_fragmented_udp() -> bool:
    """本机回环 UDP 能否送达超过以太网 MTU（要拆 IP 分片）的数据报；每个进程只测一次。

    不用共享内存的同机 Fast DDS 参与者经 127.0.0.1 通信，超过 MTU 的消息靠 IP 分片。WSL2 的镜像网络
    把 127.0.0.1 的流量经 MTU 1500 的虚拟网卡转发并丢掉分片：只走 UDP 的参与者两个方向都收不到超过
    约 1.4 KB 的消息（例如设备多的 Slave 的 ROS 图信息）。
    """

    return udp_loopback_delivers(LOOPBACK_PROBE_BYTES)


def localhost_discovery_only(env: Mapping[str, str]) -> bool:
    """只在本机发现：rmw 会在代码里按 localhost 配置传输，盖过任何传输配置文件。"""

    return env.get("ROS_LOCALHOST_ONLY") == "1" or env.get("ROS_AUTOMATIC_DISCOVERY_RANGE", "").upper() == "LOCALHOST"


def configure_slave_transport(env: MutableMapping[str, str], *, allow_shm: bool, rmw: str) -> str:
    """在 Slave 导入 rclpy、创建 DDS 参与者之前选定传输。返回值供 Host 区分显式设置与回退。

    Windows 上 Fast DDS 只认加载前就在的 ``FASTRTPS_DEFAULT_PROFILES_FILE``，所以正常启动在 ``app.main``
    里导入 rclpy 之前调用；那时还没收到 Host 下发的发现策略，收到后由调用方用
    :func:`localhost_discovery_only` 复核上报值。
    """

    if (
        rmw not in ("rmw_fastrtps_cpp", "rmw_fastrtps_dynamic_cpp")
        or any(str(env.get(name) or "").strip() for name in FASTDDS_USER_TRANSPORT_ENV)
        or localhost_discovery_only(env)
        or env.get("RMW_FASTRTPS_USE_QOS_FROM_XML") == "1"
    ):
        return "user"
    if allow_shm:
        return "shm"
    if not loopback_carries_fragmented_udp():
        return "fallback"
    profile = Path(__file__).resolve().with_name(NETWORK_ONLY_PROFILE_FILENAME)
    if not profile.is_file():
        raise FileNotFoundError(f"缺少 Fast DDS 网络传输配置：{profile}")
    env["FASTRTPS_DEFAULT_PROFILES_FILE"] = str(profile)
    return "network"


@dataclass
class RosNetworkInfo:
    """Serializable ROS2 network policy advertised by the Host."""

    domain_id: Optional[int] = None
    automatic_discovery_range: str = ""
    static_peers: List[str] = field(default_factory=list)
    discovery_server: str = ""
    discovery_server_managed: bool = False
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
            discovery_server_managed=bool(
                payload.get("discovery_server_managed", False)
            ),
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
    discovery_server_managed: bool = False,
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
        discovery_server_managed=discovery_server_managed,
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


def use_connected_host(endpoint: str, connected_host: str) -> str:
    """Use the proven HostLink address with a Host-managed discovery port."""

    if not endpoint or not connected_host:
        return endpoint
    _advertised_host, port = parse_host_port(endpoint)
    return format_host_port(connected_host, port)


def available_udp_port(bind: str = "0.0.0.0") -> int:
    """Ask the OS for an available UDP port."""

    family = socket.AF_INET6 if ":" in bind else socket.AF_INET
    with socket.socket(family, socket.SOCK_DGRAM) as probe:
        probe.bind((bind, 0))
        return int(probe.getsockname()[1])


def _discovery_command() -> Optional[Sequence[str]]:
    """Find a Fast DDS discovery executable in PATH or the active env."""

    for executable, suffix in (
        ("fast-discovery-server", ()),
        ("fastdds", ("discovery",)),
    ):
        direct = shutil.which(executable)
        if direct:
            return [direct, *suffix]
        sibling = Path(sys.executable).resolve().with_name(executable)
        if os.name == "nt":
            sibling = sibling.with_suffix(".exe")
        if sibling.is_file():
            return [str(sibling), *suffix]
    return None


class FastDDSDiscoveryServer:
    """Lifecycle wrapper for a Host-managed Fast DDS discovery process."""

    def __init__(self, bind: str, port: int, server_id: int = 0) -> None:
        self.bind = str(bind or "0.0.0.0")
        self.port = int(port)
        self.server_id = int(server_id)
        self.process: Optional[subprocess.Popen[Any]] = None

    def start(self) -> "FastDDSDiscoveryServer":
        if self.process is not None and self.process.poll() is None:
            return self
        command = _discovery_command()
        if command is None:
            raise RuntimeError(
                "Fast DDS discovery executable not found "
                "(fast-discovery-server/fastdds)"
            )
        args = [*command, "-i", str(self.server_id)]
        if self.bind not in {"", "0.0.0.0", "::"}:
            args.extend(["-l", self.bind])
        args.extend(["-p", str(self.port)])
        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
        self.process = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=os.name != "nt",
            creationflags=creationflags,
        )
        time.sleep(0.15)
        return_code = self.process.poll()
        if return_code is not None:
            self.process = None
            raise RuntimeError(
                "Fast DDS discovery server exited during startup "
                f"(code={return_code})"
            )
        return self

    def stop(self) -> None:
        process, self.process = self.process, None
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass


def detect_local_ip(probe_addr: str = "8.8.8.8") -> str:
    """Detect the preferred outbound IPv4 address without sending traffic."""

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect((probe_addr, 80))
            return str(sock.getsockname()[0])
    except OSError:
        return ""


#: 不推荐给 Slave 的保留网段：基准测试网段常被代理 / VPN 虚拟网卡（TUN）占用，
#: 共享地址空间（CGNAT）多为运营商或覆盖网络地址。
_UNSUGGESTED_NETWORKS = (
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("100.64.0.0/10"),
)


def suggestable_host_ip(address: str) -> bool:
    """``address`` 是否适合写进给 Slave 的建议命令：普通局域网（RFC 1918）或公网 IPv4。"""

    try:
        ip = ipaddress.ip_address(str(address or "").strip())
    except ValueError:
        return False
    if ip.version != 4 or ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast:
        return False
    if any(ip in network for network in _UNSUGGESTED_NETWORKS):
        return False
    return ip.is_private or ip.is_global


__all__ = [
    "FASTDDS_USER_TRANSPORT_ENV",
    "FastDDSDiscoveryServer",
    "LOOPBACK_PROBE_BYTES",
    "NETWORK_ONLY_PROFILE_FILENAME",
    "RosNetworkInfo",
    "apply_ros_network_env",
    "available_udp_port",
    "build_host_ros_info",
    "detect_local_ip",
    "format_host_port",
    "loopback_carries_fragmented_udp",
    "configure_slave_transport",
    "parse_host_port",
    "parse_host_target",
    "suggestable_host_ip",
    "udp_loopback_delivers",
    "use_connected_host",
    "validate_domain_id",
]
