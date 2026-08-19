"""UniLab 顶层命令行解析器。

这里只定义 CLI 契约，不加载配置、Registry、ROS2 或设备运行时。
"""

from __future__ import annotations

import argparse
from typing import Any

from unilabos.app.backend import BACKEND_NAMES, backend_cli_value
from unilabos.app.cli.router import register_cli_commands


def _with_dash_aliases(*option_strings: str) -> tuple[str, ...]:
    """为 ``--snake_case`` 参数显式增加 ``--kebab-case`` 别名。"""

    aliases: list[str] = []
    for option in option_strings:
        for candidate in (
            option,
            option.replace("_", "-") if option.startswith("--") else option,
        ):
            if candidate not in aliases:
                aliases.append(candidate)
    return tuple(aliases)


def _add(target: Any, *option_strings: str, **kwargs: Any) -> None:
    target.add_argument(*_with_dash_aliases(*option_strings), **kwargs)


def _register_runtime_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("runtime")
    _add(group, "-g", "--graph", help="Physical setup graph file path.")
    _add(
        group,
        "-c",
        "--controllers",
        default=None,
        help="Controllers config file path.",
    )
    _add(
        group,
        "--registry_path",
        type=str,
        default=None,
        action="append",
        help="Path to the registry directory",
    )
    _add(
        group,
        "--devices",
        type=str,
        default=None,
        action="append",
        help="Path to Python code directory for AST-based device/resource scanning",
    )
    _add(
        group,
        "--working_dir",
        type=str,
        default=None,
        help="Path to the working directory",
    )
    _add(
        group,
        "--config",
        type=str,
        default=None,
        help="Configuration file path, supports .py format Python config files",
    )
    _add(
        group,
        "--backend",
        type=backend_cli_value,
        choices=BACKEND_NAMES,
        default="ros2",
        metavar="{hostlink,ros2}",
        help=(
            "Communication backend: hostlink (distributed, no DDS) or "
            "ros2 (default)."
        ),
    )
    _add(
        group,
        "--is_slave",
        action="store_true",
        help="Run the backend as slave node (without host privileges).",
    )
    _add(
        group,
        "--host_node_name",
        "--host_node_id",
        dest="host_node_name",
        default=None,
        help="Rename the HostNode runtime instance; registry type remains host_node.",
    )
    _add(
        group,
        "--material_microbackend_addr",
        type=str,
        default=None,
        help=(
            "External materials microbackend API base. Omit it to use the "
            "process-owned materials service."
        ),
    )
    _add(
        group,
        "--port_management",
        "--port",
        dest="port_management",
        type=int,
        default=None,
        help=(
            "管理端 HTTP API 端口，默认 8002；--port 是兼容缩写，"
            "不影响 HostLink TCP 端口。"
        ),
    )
    _add(
        group,
        "--disable_browser",
        action="store_true",
        help="禁止启动时自动打开浏览器；管理端 HTTP API 仍会启动。",
    )
    _add(
        group,
        "--2d_vis",
        action="store_true",
        help="Enable 2D visualization when starting pylabrobot instance",
    )
    _add(
        group,
        "--visual",
        choices=["rviz", "web", "disable"],
        default="disable",
        help="Choose visualization tool: rviz, web, or disable",
    )


def _register_database_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("microbackend databases")
    _add(
        group,
        "--server_database_root",
        default="~/.unilabos",
        help="Directory containing runtime/materials/telemetry/history SQLite files.",
    )
    for database in ("runtime", "materials", "telemetry", "history"):
        _add(
            group,
            f"--{database}_db",
            default="",
            help=f"Optional {database}.db path override.",
        )


def _register_hostlink_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("HostLink and ROS2 networking")
    _add(
        group,
        "--host_node_ip",
        default="",
        help=(
            "Slave 连接的 HostNode IP/主机名，可兼容写成 ip:port；"
            "建议端口单独使用 --hostlink-port。"
        ),
    )
    _add(
        group,
        "--hostlink_port",
        type=int,
        default=None,
        help="HostLink TCP 端口；Host 监听、Slave 连接，默认 7302。",
    )
    _add(
        group,
        "--hostlink_bind",
        default=None,
        help="HostLink 在 Host 上的监听地址，默认 0.0.0.0；Slave 忽略。",
    )
    _add(
        group,
        "--hostlink_advertise_ip",
        default=None,
        help="Host 向 Slave 发布的可达 IP；多网卡环境建议显式指定。",
    )
    _add(
        group,
        "--disable_hostlink",
        action="store_true",
        help=(
            "关闭 HostLink；ROS2 使用原有发现和注册流程。"
            "不能与 --backend hostlink 同时使用。"
        ),
    )
    _add(
        group,
        "--hostlink_heartbeat_interval",
        type=float,
        default=None,
        help="Slave 心跳发送间隔（秒），默认 5。",
    )
    _add(
        group,
        "--hostlink_heartbeat_timeout",
        type=float,
        default=None,
        help="Host 判定 Slave 离线的心跳超时（秒），默认 15。",
    )
    _add(
        group,
        "--hostlink_connect_timeout",
        type=float,
        default=None,
        help="单次 HostLink TCP 连接/握手超时（秒），默认 5。",
    )
    _add(
        group,
        "--hostlink_request_timeout",
        type=float,
        default=None,
        help="HostLink 控制请求超时（秒），默认 10。",
    )
    _add(
        group,
        "--ros_domain_id",
        type=int,
        default=None,
        help="ROS2 domain id（0-232）；Slave 本地值仅作连接前兜底。",
    )
    _add(
        group,
        "--ros_discovery_range",
        choices=["SYSTEM_DEFAULT", "SUBNET", "LOCALHOST", "OFF"],
        default=None,
        help="Host 下发的 ROS_AUTOMATIC_DISCOVERY_RANGE。",
    )
    _add(
        group,
        "--ros_static_peers",
        default=None,
        help="Host 发布的 ROS_STATIC_PEERS，多个地址用分号分隔。",
    )
    _add(
        group,
        "--ros_discovery_server",
        default=None,
        help=(
            "Fast DDS Discovery Server 的 host:port；off 表示禁用；"
            "空值由 ROS2 组网微后端托管。"
        ),
    )
    _add(
        group,
        "--ros_discovery_port",
        type=int,
        default=None,
        help=(
            "微后端托管 Fast DDS Discovery Server 的 UDP 端口；"
            "0 表示复用 HostLink 数字端口。"
        ),
    )
    _add(
        group,
        "--no_ros_assist",
        action="store_true",
        help=(
            "ROS2 backend：保留 HostLink 设备发现和心跳，"
            "但不应用 Host 下发的 ROS2 环境。"
        ),
    )
    _add(
        group,
        "--slave_no_host",
        action="store_true",
        help=(
            "允许 Slave 在 HostLink/Host ROS 服务离线时启动；"
            "控制通道仍会在后台重连。"
        ),
    )


def _register_backend_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("upstream Backend")
    _add(
        group,
        "--ak",
        type=str,
        default="",
        help="Access key for laboratory requests",
    )
    _add(
        group,
        "--sk",
        type=str,
        default="",
        help="Secret key for laboratory requests",
    )
    _add(
        group,
        "--addr",
        type=str,
        default="https://leap-lab.bohrium.com/api/v1",
        help="Laboratory backend address (API)",
    )
    _add(
        group,
        "--schedule_addr",
        type=str,
        default="",
        help=(
            "Backend WebSocket address. If empty, derived from --addr: "
            "port +1 when --addr has a port, otherwise the same host is used."
        ),
    )
    _add(
        group,
        "--legacy",
        action="store_true",
        default=False,
        help="Connect to the old Backend WS protocol and enable old HTTP APIs.",
    )
    _add(
        group,
        "--upload_registry",
        action="store_true",
        help="Upload registry through the old Backend HTTP API (requires --legacy).",
    )


def _register_development_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("validation and development")
    _add(
        group,
        "--skip_env_check",
        action="store_true",
        help="Skip environment dependency check on startup",
    )
    _add(
        group,
        "--check_mode",
        action="store_true",
        default=False,
        help=(
            "Run in check mode for CI: validates registry imports and ensures "
            "no file changes"
        ),
    )
    _add(
        group,
        "--complete_registry",
        action="store_true",
        default=False,
        help="Complete and rewrite YAML registry files using AST analysis results",
    )
    _add(
        group,
        "--no_update_feedback",
        action="store_true",
        help="Disable sending update feedback to server",
    )
    _add(
        group,
        "--test_mode",
        action="store_true",
        default=False,
        help=(
            "Test mode: all actions simulate execution and return mock results "
            "without running real hardware"
        ),
    )
    _add(
        group,
        "--external_devices_only",
        action="store_true",
        default=False,
        help=(
            "Only load external device packages (--devices), skip built-in "
            "device scanning and YAML registry"
        ),
    )
    _add(
        group,
        "--extra_resource",
        action="store_true",
        default=False,
        help="Load extra lab_ prefixed labware resource definitions",
    )
    _add(
        group,
        "--restart_mode",
        action="store_true",
        default=False,
        help="Enable supervisor mode and restart when requested through Backend WS",
    )
    _add(
        group,
        "--auto_restart_count",
        type=int,
        default=500,
        help="Maximum number of automatic restarts (default: 500)",
    )


def build_parser() -> argparse.ArgumentParser:
    """构建不带运行时副作用的统一 CLI parser。"""

    parser = argparse.ArgumentParser(description="Start Uni-Lab Edge server.")
    subparsers = parser.add_subparsers(title="Valid subcommands", dest="command")
    _register_runtime_arguments(parser)
    _register_database_arguments(parser)
    _register_hostlink_arguments(parser)
    _register_backend_arguments(parser)
    _register_development_arguments(parser)
    register_cli_commands(parser, subparsers)
    return parser


# 兼容早期测试和集成代码中“parse_args 返回 parser”的命名。
parse_args = build_parser


__all__ = ["build_parser", "parse_args"]
