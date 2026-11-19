"""HostLink 启动参数应用与校验，以及 Host 进程尽早打开 HostLink 端口。

Host 的 HostLink 端口从进程启动之初就可连：配置一就绪就预绑定（:func:`prebind_host_server`），
设备 runtime 启动时由 ``HostLinkBackend`` 接管同一个 server。依赖 Host 状态的请求（物料权威、
本机设备与 host 服务）在对应阶段就绪前回「Host 启动中」，由 Slave 按退避重试。
"""

from __future__ import annotations

import os
import threading
from typing import Any, Mapping, Optional

from unilabos.config.config import HostLinkConfig
from unilabos.backend.hostlink.ros_assist import parse_host_target, validate_domain_id
from unilabos.backend.hostlink.server import (
    HOST_STARTUP_GATES,
    HostLinkServer,
    get_hostlink_server,
    set_hostlink_server,
)
from unilabos.utils import logger
from unilabos.utils.banner_print import print_status


def apply_hostlink_cli(args: Mapping[str, Any], *, is_slave: bool) -> None:
    """在配置文件和环境变量之后应用 HostLink 命令行覆盖。"""

    host_node_ip = str(args.get("host_node_ip") or "").strip()
    if host_node_ip:
        host, hostlink_port = parse_host_target(host_node_ip, HostLinkConfig.port)
        HostLinkConfig.port = hostlink_port
        if is_slave:
            HostLinkConfig.host = host
        else:
            HostLinkConfig.advertise_ip = host

    explicit_port = args.get("hostlink_port")
    if explicit_port is not None:
        if not 1 <= int(explicit_port) <= 65535:
            raise ValueError("--hostlink-port must be between 1 and 65535")
        HostLinkConfig.port = int(explicit_port)

    hostlink_bind = args.get("hostlink_bind")
    if hostlink_bind is not None:
        HostLinkConfig.bind = str(hostlink_bind).strip()
        if not HostLinkConfig.bind:
            raise ValueError("--hostlink-bind cannot be empty")

    advertise_ip = args.get("hostlink_advertise_ip")
    if advertise_ip is not None:
        HostLinkConfig.advertise_ip = str(advertise_ip).strip()
        if not HostLinkConfig.advertise_ip:
            raise ValueError("--hostlink-advertise-ip cannot be empty")

    if args.get("disable_hostlink", False):
        HostLinkConfig.enable = False

    if is_slave and HostLinkConfig.host:
        print_status(
            f"Slave HostNode: {HostLinkConfig.host}:{HostLinkConfig.port}",
            "info",
        )

    timeout_fields = {
        "hostlink_heartbeat_interval": "heartbeat_interval",
        "hostlink_heartbeat_timeout": "heartbeat_timeout",
        "hostlink_connect_timeout": "connect_timeout",
        "hostlink_host_wait_timeout": "host_wait_timeout",
        "hostlink_request_timeout": "request_timeout",
    }
    for argument, field in timeout_fields.items():
        value = args.get(argument)
        if value is None:
            continue
        if float(value) <= 0:
            raise ValueError(
                f"--{argument.replace('_', '-')} must be greater than 0"
            )
        setattr(HostLinkConfig, field, float(value))

    ros_domain_id = validate_domain_id(args.get("ros_domain_id"))
    if ros_domain_id is not None:
        HostLinkConfig.ros_domain_id = str(ros_domain_id)
        os.environ["ROS_DOMAIN_ID"] = str(ros_domain_id)
        print_status(f"ROS_DOMAIN_ID = {ros_domain_id}", "info")
    if args.get("ros_discovery_range") is not None:
        HostLinkConfig.ros_discovery_range = args["ros_discovery_range"]
    if args.get("ros_static_peers") is not None:
        HostLinkConfig.ros_static_peers = str(args["ros_static_peers"]).strip()
    if args.get("ros_discovery_server") is not None:
        HostLinkConfig.ros_discovery_server = str(
            args["ros_discovery_server"]
        ).strip()
    ros_discovery_port = args.get("ros_discovery_port")
    if ros_discovery_port is not None:
        if not 0 <= int(ros_discovery_port) <= 65535:
            raise ValueError("--ros-discovery-port must be between 0 and 65535")
        HostLinkConfig.ros_discovery_port = int(ros_discovery_port)
    if args.get("no_ros_assist", False):
        HostLinkConfig.ros_assist_apply = False


def validate_hostlink_backend(args: Mapping[str, Any], *, is_slave: bool) -> None:
    """校验 HostLink 作为直接 backend 时的必需条件。"""

    if args.get("backend") != "hostlink":
        return
    if not HostLinkConfig.enable:
        raise ValueError("--backend hostlink 不能与 --disable-hostlink 同时使用")
    if is_slave and not str(HostLinkConfig.host or "").strip():
        raise ValueError(
            "--backend hostlink --is-slave 必须通过 --host-node-ip 指定 Host"
        )


def create_host_server() -> HostLinkServer:
    """按 ``HostLinkConfig`` 构造尚未监听的 Host server，并进入启动期门控。

    门控见 :data:`~unilabos.backend.hostlink.server.HOST_STARTUP_GATES`：HELLO / 心跳 /
    状态上报随时可用；物料请求等 ``materials``、设备与 service 调用等 ``devices`` 阶段。
    """

    server = HostLinkServer(
        bind=HostLinkConfig.bind,
        port=HostLinkConfig.port,
        heartbeat_timeout=HostLinkConfig.heartbeat_timeout,
        request_timeout=HostLinkConfig.request_timeout,
    )
    server.hello_payload = {"backend": "hostlink", "role": "host", "devices": []}
    server.hold_until_ready(HOST_STARTUP_GATES)
    return server


def should_prebind_host_server(
    args: Mapping[str, Any],
    *,
    is_slave: bool,
    check_mode: bool,
    runs_local_authority: bool,
) -> bool:
    """本进程是否是会监听 HostLink 的 hostlink Host（设备 runtime 在本进程）。

    默认拓扑里调度权威进程不监听 HostLink（它的 Host 子进程监听）；``--role backend``、
    Slave、``--check_mode`` 和 ROS2 backend（端口由 ``setup_host_network_service`` 打开）
    都不预绑定。
    """

    return (
        not check_mode
        and not is_slave
        and not runs_local_authority
        and args.get("role") != "backend"
        and str(args.get("backend") or "") == "hostlink"
        and bool(HostLinkConfig.enable)
    )


_prebound_lock = threading.Lock()
_prebound_server: Optional[HostLinkServer] = None


def prebind_host_server() -> Optional[HostLinkServer]:
    """Host 进程配置就绪后立刻打开 HostLink 端口，不等注册表、设备图与设备启动。

    Slave 随即可以握手、登记设备并维持心跳；需要 Host 状态的请求在就绪前回「Host 启动中」。
    绑定失败（端口仍被上一个正在退出的 Host 占着等）不在这里终止进程：放弃预绑定，由设备
    runtime 启动时按原有路径再绑一次并沿用原来的报错与退出码。
    """

    global _prebound_server
    with _prebound_lock:
        if _prebound_server is not None:
            return _prebound_server
        server = create_host_server()
        try:
            server.start()
        except OSError as exc:
            server.stop()
            logger.warning(
                "[HostLink] 提前打开 HostLink 端口失败：%s；设备 runtime 启动时再绑定一次", exc
            )
            return None
        _prebound_server = server
    set_hostlink_server(server)
    logger.info(
        "[HostLink] Host 端口已在启动之初打开：%s:%d；Slave 可立即连接，"
        "物料与设备请求在 Host 就绪前回「Host 启动中」并由 Slave 自动重试",
        HostLinkConfig.bind,
        server.port,
    )
    return server


def adopt_prebound_host_server() -> Optional[HostLinkServer]:
    """设备 runtime 接管预绑定的 server（只交出一次）；没有预绑定时返回 None。"""

    global _prebound_server
    with _prebound_lock:
        server, _prebound_server = _prebound_server, None
    return server


def release_prebound_host_server() -> None:
    """本进程不会启动设备 runtime 时（如驱动包操作待人工确认）关闭预绑定的端口。"""

    server = adopt_prebound_host_server()
    if server is None:
        return
    server.stop()
    if get_hostlink_server() is server:
        set_hostlink_server(None)
    logger.info("[HostLink] 设备 runtime 不启动，已关闭提前打开的 HostLink 端口")


__all__ = [
    "adopt_prebound_host_server",
    "apply_hostlink_cli",
    "create_host_server",
    "prebind_host_server",
    "release_prebound_host_server",
    "should_prebind_host_server",
    "validate_hostlink_backend",
]
