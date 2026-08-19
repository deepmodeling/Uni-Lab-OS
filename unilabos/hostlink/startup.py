"""HostLink 启动参数应用与校验。"""

from __future__ import annotations

import os
from typing import Any, Mapping

from unilabos.config.config import HostLinkConfig
from unilabos.hostlink.ros_assist import parse_host_target, validate_domain_id
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


__all__ = ["apply_hostlink_cli", "validate_hostlink_backend"]
