"""HostLink：host-slave 专用 TCP/IP 请求通路（去 ROS 化的第一步）。

背景：云端物料注册表下线（Edge 权威仓储），Slave 不再各自向云端要物料，
统一向 Host 的 Edge 微后端取；Host-Slave 的 ROS2 架构将逐步整体替换为 TCP/IP 组网，
本包是这条通路的第一块：

- ``protocol``  帧与信封（NDJSON over TCP；消息形状对齐通信准则）
- ``server``    host 侧 TCP 服务（peer 注册 / 心跳在线监控 / action 处理器）
- ``client``    slave 侧客户端（组网配置 host:port、自动重连、在线状态、请求通道）
- ``resolver``  host 本地资源树解析（物料查询的本地事实源，替代云端）
- ``ros_assist`` ROS2 组网协助（域号 / 发现范围降级 / 静态对端 / Discovery Server）
"""

from unilabos.hostlink.client import (
    HostLinkClient,
    get_hostlink_client,
    set_hostlink_client,
)
from unilabos.hostlink.protocol import ActionType, LinkError
from unilabos.hostlink.resolver import LocalResourceResolver
from unilabos.hostlink.ros_assist import (
    RosNetworkInfo,
    apply_ros_network_env,
    build_host_ros_info,
)
from unilabos.hostlink.server import HostLinkServer

__all__ = [
    "ActionType",
    "HostLinkClient",
    "HostLinkServer",
    "LinkError",
    "LocalResourceResolver",
    "RosNetworkInfo",
    "apply_ros_network_env",
    "build_host_ros_info",
    "get_hostlink_client",
    "set_hostlink_client",
]
