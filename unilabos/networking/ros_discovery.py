"""ROS 诊断的显式发现配置；仅作用于当前进程。"""

from __future__ import annotations

import os
import sys
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, MutableMapping, Optional

_VALID_RANGES = ("SYSTEM_DEFAULT", "SUBNET", "LOCALHOST", "OFF")
_PLATFORM = sys.platform


@dataclass
class RosNetworkInfo:
    """ROS 诊断进程的发现配置。"""

    domain_id: Optional[int] = None
    automatic_discovery_range: str = ""      # 空 = 不干预
    static_peers: List[str] = field(default_factory=list)
    discovery_server: str = ""  # "ip:port"；空 = 不使用
    # 显式禁用时清理继承的 ROS_DISCOVERY_SERVER。
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
            discovery_server_disabled=bool(
                data.get("discovery_server_disabled", False)
            ),
        )




def apply_ros_network_env(
    info: RosNetworkInfo,
    environ: Optional[MutableMapping[str, str]] = None,
) -> Dict[str, str]:
    """把组网信息写入环境变量（必须在 rclpy.init 之前调用）。

    通常只写有值的项；显式设置 ``discovery_server_disabled`` 时会清除
    继承的 ``ROS_DISCOVERY_SERVER``。其余未设置项保持原样。返回实际写入的
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
















__all__ = ["RosNetworkInfo", "apply_ros_network_env"]
