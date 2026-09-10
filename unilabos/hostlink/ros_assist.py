"""ROS2 组网协助：HostLink 下发定向发现端点，Action 数据仍走 ROS2。

ROS2（Iron+ / Fast DDS）提供三档「可降级」的发现机制，全部由环境变量控制，
正好可以经 TCP 通路统一分发：

- ``ROS_DOMAIN_ID``                   域号（host/slave 必须一致才能互见）
- ``ROS_AUTOMATIC_DISCOVERY_RANGE``   自动发现范围：
      ``SUBNET``（默认，组播全网段）→ ``LOCALHOST``（仅本机）→ ``OFF``（完全关闭
      组播自动发现）。降级到 OFF 后仅靠静态对端/发现服务器单播组网，
      适合禁组播的实验室网络，也是逐步去 ROS 的过渡形态。
- ``ROS_STATIC_PEERS``                静态对端列表（分号分隔的 ip），OFF/受限
      范围下与列表中的地址仍可单播互发现。
- ``ROS_DISCOVERY_SERVER``            Fast DDS Discovery Server 地址（ip:port）。
      Host 微后端可在 HostLink 数字端口上同时监听 TCP（HostLink）和 UDP
      （Fast DDS），Slave 用它定向发现 HostNode，不依赖组播。

host 侧 ``build_host_ros_info()`` 汇总当前进程的组网配置；slave 侧
``apply_ros_network_env()`` 必须在 ``rclpy.init`` 之前调用（DDS 只在初始化时
读取这些变量）。
"""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, MutableMapping, Optional, Sequence, Tuple

_VALID_RANGES = ("SYSTEM_DEFAULT", "SUBNET", "LOCALHOST", "OFF")
_PLATFORM = sys.platform


@dataclass
class RosNetworkInfo:
    """经 HostLink 下发的 ROS 组网信息（hello 响应的 ``ros`` 字段）。"""

    domain_id: Optional[int] = None
    automatic_discovery_range: str = ""      # 空 = 不干预
    static_peers: List[str] = field(default_factory=list)
    discovery_server: str = ""  # "ip:port"；空 = 不使用
    # True 表示该 Server 由 Host 微后端托管。Slave 应使用其实际连接的
    # HostLink host 替换地址部分，只保留 Host 下发的 UDP 端口，避免多网卡误选。
    discovery_server_managed: bool = False
    # True 表示 Host 明确要求禁用 Discovery Server，而不是“未提供覆盖”。
    # 该位用于清理 Slave 进程可能从 shell 继承的旧 ROS_DISCOVERY_SERVER。
    discovery_server_disabled: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "RosNetworkInfo":
        data = data or {}
        domain = data.get("domain_id")
        return cls(
            domain_id=int(domain) if domain is not None else None,
            automatic_discovery_range=str(data.get("automatic_discovery_range") or ""),
            static_peers=[str(p) for p in (data.get("static_peers") or []) if p],
            discovery_server=str(data.get("discovery_server") or ""),
            discovery_server_managed=bool(data.get("discovery_server_managed", False)),
            discovery_server_disabled=bool(
                data.get("discovery_server_disabled", False)
            ),
        )


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
    """host 侧汇总组网信息；未显式配置的项回退到 host 自身环境变量。

    host_ip 非空且未提供 static_peers 时，自动把 host 自身加入静态对端——
    slave 至少能与 host 单播互发现（降级组网的最小可用形态）。
    """
    env = environ if environ is not None else os.environ
    if domain_id is None:
        raw = env.get("ROS_DOMAIN_ID", "").strip()
        domain_id = int(raw) if raw.isdigit() else None
    if not discovery_range:
        discovery_range = env.get("ROS_AUTOMATIC_DISCOVERY_RANGE", "").strip().upper()
    if static_peers is None:
        raw_peers = env.get("ROS_STATIC_PEERS", "")
        static_peers = [p.strip() for p in raw_peers.split(";") if p.strip()]
    if not static_peers and host_ip:
        static_peers = [host_ip]
    if not discovery_server and not discovery_server_disabled:
        discovery_server = env.get("ROS_DISCOVERY_SERVER", "").strip()
    if discovery_range and discovery_range not in _VALID_RANGES:
        raise ValueError(
            f"invalid discovery range {discovery_range!r}, expected one of {_VALID_RANGES}"
        )
    return RosNetworkInfo(
        domain_id=domain_id,
        automatic_discovery_range=discovery_range,
        static_peers=static_peers,
        discovery_server=discovery_server,
        discovery_server_managed=discovery_server_managed,
        discovery_server_disabled=discovery_server_disabled,
    )


def apply_ros_network_env(
    info: RosNetworkInfo,
    environ: Optional[MutableMapping[str, str]] = None,
) -> Dict[str, str]:
    """把组网信息写入环境变量（必须在 rclpy.init 之前调用）。

    通常只写有值的项；Host 明确下发 ``discovery_server_disabled`` 时会清除
    继承的 ``ROS_DISCOVERY_SERVER``。其余未下发项保持原样。返回实际写入的
    键值对（被清除的键不包含在返回值中）。
    """
    env = environ if environ is not None else os.environ
    applied: Dict[str, str] = {}
    if info.domain_id is not None:
        applied["ROS_DOMAIN_ID"] = str(info.domain_id)
    if info.automatic_discovery_range:
        if info.automatic_discovery_range not in _VALID_RANGES:
            raise ValueError(f"invalid discovery range {info.automatic_discovery_range!r}")
        applied["ROS_AUTOMATIC_DISCOVERY_RANGE"] = info.automatic_discovery_range
    if info.static_peers:
        applied["ROS_STATIC_PEERS"] = ";".join(info.static_peers)
    if info.discovery_server_disabled:
        env.pop("ROS_DISCOVERY_SERVER", None)
    elif info.discovery_server:
        applied["ROS_DISCOVERY_SERVER"] = info.discovery_server
    env.update(applied)
    return applied


def parse_host_port(endpoint: str) -> Tuple[str, int]:
    """Parse ``host:port`` (including ``[IPv6]:port``) with strict validation."""

    value = str(endpoint or "").strip()
    if value.startswith("["):
        close = value.find("]")
        if close <= 1 or close + 1 >= len(value) or value[close + 1] != ":":
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


def format_host_port(host: str, port: int) -> str:
    """Format an IPv4/hostname/IPv6 endpoint for ROS discovery variables."""

    clean_host = str(host or "").strip()
    if not clean_host:
        raise ValueError("discovery server host cannot be empty")
    if not 1 <= int(port) <= 65535:
        raise ValueError(f"discovery server port out of range: {port}")
    if ":" in clean_host and not clean_host.startswith("["):
        clean_host = f"[{clean_host}]"
    return f"{clean_host}:{int(port)}"


def use_connected_host(endpoint: str, connected_host: str) -> str:
    """Keep a Host-managed discovery port but use the proven HostLink address."""

    if not endpoint or not connected_host:
        return endpoint
    _advertised_host, port = parse_host_port(endpoint)
    return format_host_port(connected_host, port)


def available_udp_port(bind: str = "0.0.0.0") -> int:
    """Reserve an ephemeral UDP port long enough to obtain its number."""

    family = socket.AF_INET6 if ":" in bind else socket.AF_INET
    with socket.socket(family, socket.SOCK_DGRAM) as probe:
        probe.bind((bind, 0))
        return int(probe.getsockname()[1])


def _discovery_command() -> Optional[Sequence[str]]:
    """Find the Fast DDS discovery executable, including the active venv bin."""

    direct = shutil.which("fast-discovery-server")
    if direct:
        return [direct]
    executable_suffix = ".exe" if _PLATFORM.startswith("win") else ""
    sibling = Path(sys.executable).resolve().with_name(
        f"fast-discovery-server{executable_suffix}"
    )
    if sibling.is_file():
        return [str(sibling)]
    fastdds = shutil.which("fastdds")
    if fastdds:
        return [fastdds, "discovery"]
    sibling_fastdds = Path(sys.executable).resolve().with_name(
        f"fastdds{executable_suffix}"
    )
    if sibling_fastdds.is_file():
        return [str(sibling_fastdds), "discovery"]
    return None


class FastDDSDiscoveryServer:
    """Small lifecycle wrapper for the Host-managed Fast DDS discovery process."""

    def __init__(self, bind: str, port: int, server_id: int = 0) -> None:
        self.bind = str(bind or "0.0.0.0")
        self.port = int(port)
        self.server_id = int(server_id)
        self.process: Optional[subprocess.Popen] = None

    def start(self) -> "FastDDSDiscoveryServer":
        if self.process is not None and self.process.poll() is None:
            return self
        command = _discovery_command()
        if command is None:
            raise RuntimeError(
                "Fast DDS discovery executable not found (fast-discovery-server/fastdds)"
            )
        args = [*command, "-i", str(self.server_id)]
        if self.bind not in ("", "0.0.0.0", "::"):
            args.extend(["-l", self.bind])
        args.extend(["-p", str(self.port)])
        process_group_options: Dict[str, Any]
        if _PLATFORM.startswith("win"):
            process_group_options = {
                "creationflags": getattr(
                    subprocess,
                    "CREATE_NEW_PROCESS_GROUP",
                    0,
                )
            }
        else:
            process_group_options = {"start_new_session": True}
        self.process = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **process_group_options,
        )
        # The CLI reports bind/config errors immediately.  A short admission
        # window prevents advertising a dead endpoint to every Slave.
        time.sleep(0.15)
        return_code = self.process.poll()
        if return_code is not None:
            self.process = None
            raise RuntimeError(
                f"Fast DDS discovery server exited during startup (code={return_code})"
            )
        return self

    def stop(self) -> None:
        process, self.process = self.process, None
        if process is None or process.poll() is not None:
            return
        try:
            if _PLATFORM.startswith("win"):
                process.terminate()
            else:
                os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            try:
                if _PLATFORM.startswith("win"):
                    process.kill()
                else:
                    os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass


def detect_local_ip(probe_addr: str = "8.8.8.8") -> str:
    """探测本机对外 IP（UDP connect 技巧，不产生真实流量）；失败返回空串。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect((probe_addr, 80))
            return sock.getsockname()[0]
    except OSError:
        return ""


__all__ = [
    "RosNetworkInfo",
    "FastDDSDiscoveryServer",
    "apply_ros_network_env",
    "available_udp_port",
    "build_host_ros_info",
    "detect_local_ip",
    "format_host_port",
    "parse_host_port",
    "use_connected_host",
]
