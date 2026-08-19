import argparse
import asyncio
import faulthandler
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict, Any, List
import networkx as nx
import yaml

# Windows 中文系统 stdout 默认 GBK，无法编码 banner / emoji 日志中的 Unicode 字符
# 强制 stdout/stderr 用 UTF-8，避免 print 触发 UnicodeEncodeError 导致进程崩溃
if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            pass

# 原生崩溃(段错误 / 0xC0000005 访问违例，常见于 C 扩展 import)发生时打印 Python 调用栈。
# 仅在致命信号(SIGSEGV/SIGABRT/SIGFPE 等)时触发，不影响 SIGINT/SIGTERM 的正常退出流程。
try:
    faulthandler.enable()
except (RuntimeError, ValueError, OSError):
    pass

# 首先添加项目根目录到路径
current_dir = os.path.dirname(os.path.abspath(__file__))
unilabos_dir = os.path.dirname(os.path.dirname(current_dir))
if unilabos_dir not in sys.path:
    sys.path.append(unilabos_dir)

from unilabos.app.utils import cleanup_for_restart  # noqa: E402
from unilabos.utils.banner_print import print_status, print_unilab_banner  # noqa: E402
from unilabos.config.config import (  # noqa: E402
    BasicConfig,
    HTTPConfig,
    load_config,
    resolve_host_node_name,
)
from unilabos.server.database import (  # noqa: E402
    ServerDatabasePaths,
)

# Global restart flags (used by ws_client and web/server)
_restart_requested: bool = False
_restart_reason: str = ""

RESTART_EXIT_CODE = 42


def _build_child_argv():
    """Build sys.argv for child process, stripping supervisor-only arguments."""
    result = []
    skip_next = False
    for arg in sys.argv:
        if skip_next:
            skip_next = False
            continue
        if arg in ("--restart_mode", "--restart-mode"):
            continue
        if arg in ("--auto_restart_count", "--auto-restart-count"):
            skip_next = True
            continue
        if arg.startswith("--auto_restart_count=") or arg.startswith("--auto-restart-count="):
            continue
        result.append(arg)
    return result


def _run_as_supervisor(max_restarts: int):
    """
    Supervisor process that spawns and monitors child processes.

    Similar to Uvicorn's --reload: the supervisor itself does no heavy work,
    it only launches the real process as a child and restarts it when the child
    exits with RESTART_EXIT_CODE.
    """
    child_argv = [sys.executable] + _build_child_argv()
    restart_count = 0

    print_status(
        f"[Supervisor] Restart mode enabled (max restarts: {max_restarts}), "
        f"child command: {' '.join(child_argv)}",
        "info",
    )

    while True:
        print_status(
            f"[Supervisor] Launching process (restart {restart_count}/{max_restarts})...",
            "info",
        )

        try:
            process = subprocess.Popen(child_argv)
            exit_code = process.wait()
        except KeyboardInterrupt:
            print_status("[Supervisor] Interrupted, terminating child process...", "info")
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            sys.exit(1)

        if exit_code == RESTART_EXIT_CODE:
            restart_count += 1
            if restart_count > max_restarts:
                print_status(
                    f"[Supervisor] Maximum restart count ({max_restarts}) reached, exiting",
                    "warning",
                )
                sys.exit(1)
            print_status(
                f"[Supervisor] Child requested restart ({restart_count}/{max_restarts}), restarting in 2s...",
                "info",
            )
            time.sleep(2)
        else:
            if exit_code != 0:
                print_status(f"[Supervisor] Child exited with code {exit_code}", "warning")
            else:
                print_status("[Supervisor] Child exited normally", "info")
            sys.exit(exit_code)


def load_config_from_file(config_path):
    if config_path is None:
        config_path = os.environ.get("UNILABOS_BASICCONFIG_CONFIG_PATH", None)
    if config_path:
        if not os.path.exists(config_path):
            print_status(f"配置文件 {config_path} 不存在", "error")
        elif not config_path.endswith(".py"):
            print_status(f"配置文件 {config_path} 不是Python文件，必须以.py结尾", "error")
        else:
            load_config(config_path)
    else:
        print_status(f"启动 UniLab-OS时，配置文件参数未正确传入 --config '{config_path}' 尝试本地配置...", "warning")
        load_config(config_path)


def _apply_hostlink_cli(args_dict: Dict[str, Any], *, is_slave: bool) -> None:
    """在配置文件和环境变量之后应用 HostLink 命令行覆盖。"""

    from unilabos.config.config import HostLinkConfig
    from unilabos.hostlink.ros_assist import parse_host_target, validate_domain_id

    host_node_ip = str(args_dict.get("host_node_ip") or "").strip()
    if host_node_ip:
        host, hostlink_port = parse_host_target(host_node_ip, HostLinkConfig.port)
        HostLinkConfig.port = hostlink_port
        if is_slave:
            HostLinkConfig.host = host
        else:
            HostLinkConfig.advertise_ip = host

    explicit_port = args_dict.get("hostlink_port")
    if explicit_port is not None:
        if not 1 <= int(explicit_port) <= 65535:
            raise ValueError("--hostlink-port must be between 1 and 65535")
        HostLinkConfig.port = int(explicit_port)

    hostlink_bind = args_dict.get("hostlink_bind")
    if hostlink_bind is not None:
        HostLinkConfig.bind = str(hostlink_bind).strip()
        if not HostLinkConfig.bind:
            raise ValueError("--hostlink-bind cannot be empty")

    advertise_ip = args_dict.get("hostlink_advertise_ip")
    if advertise_ip is not None:
        HostLinkConfig.advertise_ip = str(advertise_ip).strip()
        if not HostLinkConfig.advertise_ip:
            raise ValueError("--hostlink-advertise-ip cannot be empty")

    if args_dict.get("disable_hostlink", False):
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
        value = args_dict.get(argument)
        if value is None:
            continue
        if float(value) <= 0:
            raise ValueError(f"--{argument.replace('_', '-')} must be greater than 0")
        setattr(HostLinkConfig, field, float(value))

    ros_domain_id = validate_domain_id(args_dict.get("ros_domain_id"))
    if ros_domain_id is not None:
        HostLinkConfig.ros_domain_id = str(ros_domain_id)
        os.environ["ROS_DOMAIN_ID"] = str(ros_domain_id)
        print_status(f"ROS_DOMAIN_ID = {ros_domain_id}", "info")
    if args_dict.get("ros_discovery_range") is not None:
        HostLinkConfig.ros_discovery_range = args_dict["ros_discovery_range"]
    if args_dict.get("ros_static_peers") is not None:
        HostLinkConfig.ros_static_peers = str(args_dict["ros_static_peers"]).strip()
    if args_dict.get("ros_discovery_server") is not None:
        HostLinkConfig.ros_discovery_server = str(
            args_dict["ros_discovery_server"]
        ).strip()
    ros_discovery_port = args_dict.get("ros_discovery_port")
    if ros_discovery_port is not None:
        if not 0 <= int(ros_discovery_port) <= 65535:
            raise ValueError("--ros-discovery-port must be between 0 and 65535")
        HostLinkConfig.ros_discovery_port = int(ros_discovery_port)
    if args_dict.get("no_ros_assist", False):
        HostLinkConfig.ros_assist_apply = False


def convert_argv_dashes_to_underscores(args: argparse.ArgumentParser):
    # easier for user input, easier for dev search code
    option_strings = list(args._option_string_actions.keys())
    for i, arg in enumerate(sys.argv):
        for option_string in option_strings:
            if arg.startswith(option_string):
                new_arg = arg[:2] + arg[2 : len(option_string)].replace("-", "_") + arg[len(option_string) :]
                sys.argv[i] = new_arg
                break


def configure_material_startup(args_dict: Dict[str, Any]) -> str:
    """应用物料来源配置并解析嵌入式/独立 Provider 模式。"""

    source = str(
        args_dict.get("material_source")
        or HTTPConfig.material_source
        or "microbackend"
    ).strip().lower()
    source = {
        "edge": "microbackend",
        "local": "microbackend",
        "cloud": "backend",
        "remote": "backend",
    }.get(source, source)
    if source not in {"microbackend", "backend", "auto"}:
        raise ValueError("material source must be microbackend, backend, or auto")
    HTTPConfig.material_source = source

    address_arg = args_dict.get("material_microbackend_addr")
    if address_arg is not None:
        HTTPConfig.material_microbackend_addr = str(address_arg).strip()
    address = str(HTTPConfig.material_microbackend_addr or "").strip()
    mode = str(
        args_dict.get("material_service_mode")
        or ("external" if address else "embedded")
    )
    if mode == "external":
        HTTPConfig.material_microbackend_addr = (
            address or "http://127.0.0.1:8092/api/v1"
        )
    else:
        HTTPConfig.material_microbackend_addr = ""
    args_dict["_material_service_mode"] = mode
    return mode


def configure_server_databases(
    args_dict: Dict[str, Any], *, working_dir: str | os.PathLike[str]
) -> ServerDatabasePaths:
    """一次解析微后端四库；仅 Host 组合根会实际打开数据库。"""

    root = str(
        args_dict.get("server_database_root")
        or (Path(working_dir).expanduser() / ".unilabos")
    )
    overrides = {
        key: value
        for key, value in {
            "runtime": args_dict.get("runtime_db"),
            "materials": args_dict.get("materials_db"),
            "telemetry": args_dict.get("telemetry_db"),
            "history": args_dict.get("history_db"),
        }.items()
        if value is not None and str(value).strip()
    }
    paths = ServerDatabasePaths.resolve(root, overrides)
    BasicConfig.server_database_paths = paths
    return paths


def should_start_embedded_material_service(
    args_dict: Dict[str, Any], *, is_host_mode: bool
) -> bool:
    return (
        is_host_mode
        and HTTPConfig.material_source in {"microbackend", "auto"}
        and args_dict.get("_material_service_mode") == "embedded"
    )


def parse_args():
    """解析命令行参数"""
    from unilabos.app.backend import BACKEND_NAMES, backend_cli_value

    parser = argparse.ArgumentParser(description="Start Uni-Lab Edge server.")
    subparsers = parser.add_subparsers(title="Valid subcommands", dest="command")

    parser.add_argument("-g", "--graph", help="Physical setup graph file path.")
    parser.add_argument("-c", "--controllers", default=None, help="Controllers config file path.")
    parser.add_argument(
        "--registry_path",
        type=str,
        default=None,
        action="append",
        help="Path to the registry directory",
    )
    parser.add_argument(
        "--devices",
        type=str,
        default=None,
        action="append",
        help="Path to Python code directory for AST-based device/resource scanning",
    )
    parser.add_argument(
        "--working_dir",
        type=str,
        default=None,
        help="Path to the working directory",
    )
    parser.add_argument(
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
    parser.add_argument(
        "--app_bridges",
        nargs="*",
        default=None,
        help=(
            "Application bridges. Host modes enable websocket and fastapi by "
            "default; HostLink slaves enable none. Pass the flag with "
            "no values to disable all bridges explicitly."
        ),
    )
    parser.add_argument(
        "--backend_protocol",
        "--communication_protocol",
        dest="backend_protocol",
        choices=["control", "old"],
        default=None,
        help=(
            "Backend wire protocol: control uses WebSocket notices plus HTTP "
            "pull; old connects to the legacy full-payload WebSocket backend."
        ),
    )
    parser.add_argument(
        "--material_source",
        choices=["microbackend", "backend", "auto"],
        default=None,
        help="Host material source; default is the embedded microbackend.",
    )
    parser.add_argument(
        "--material_service_mode",
        choices=["embedded", "external"],
        default=None,
        help="Run Inventory/Resource Provider in this process or use :8092.",
    )
    parser.add_argument(
        "--material_microbackend_addr",
        type=str,
        default=None,
        help="External material Provider API base.",
    )
    parser.add_argument(
        "--server_database_root",
        "--server-database-root",
        default="~/.unilabos",
        help="Directory containing runtime/materials/telemetry/history SQLite files.",
    )
    parser.add_argument(
        "--runtime_db",
        "--runtime-db",
        default="",
        help="Optional runtime.db path override.",
    )
    parser.add_argument(
        "--materials_db",
        "--materials-db",
        default="",
        help="Optional materials.db path override.",
    )
    parser.add_argument(
        "--telemetry_db",
        "--telemetry-db",
        default="",
        help="Optional telemetry.db path override.",
    )
    parser.add_argument(
        "--history_db",
        "--history-db",
        default="",
        help="Optional history.db path override.",
    )
    parser.add_argument(
        "--is_slave",
        "--is-slave",
        dest="is_slave",
        action="store_true",
        help="Run the backend as slave node (without host privileges).",
    )
    parser.add_argument(
        "--host_node_name",
        "--host-node-name",
        "--host_node_id",
        "--host-node-id",
        dest="host_node_name",
        default=None,
        help="Rename the HostNode runtime instance; registry type remains host_node.",
    )
    parser.add_argument(
        "--host_node_ip",
        "--host-node-ip",
        dest="host_node_ip",
        default="",
        help=(
            "Slave 连接的 HostNode IP/主机名，可兼容写成 ip:port；"
            "建议端口单独使用 --hostlink-port。"
        ),
    )
    parser.add_argument(
        "--hostlink_port",
        "--hostlink-port",
        dest="hostlink_port",
        type=int,
        default=None,
        help="HostLink TCP 端口；Host 监听、Slave 连接，默认 7302。",
    )
    parser.add_argument(
        "--hostlink_bind",
        "--hostlink-bind",
        dest="hostlink_bind",
        default=None,
        help="HostLink 在 Host 上的监听地址，默认 0.0.0.0；Slave 忽略。",
    )
    parser.add_argument(
        "--hostlink_advertise_ip",
        "--hostlink-advertise-ip",
        dest="hostlink_advertise_ip",
        default=None,
        help="Host 向 Slave 发布的可达 IP；多网卡环境建议显式指定。",
    )
    parser.add_argument(
        "--disable_hostlink",
        "--disable-hostlink",
        dest="disable_hostlink",
        action="store_true",
        help=(
            "关闭 HostLink；ROS2 使用原有发现和注册流程。"
            "不能与 --backend hostlink 同时使用。"
        ),
    )
    parser.add_argument(
        "--hostlink_heartbeat_interval",
        "--hostlink-heartbeat-interval",
        dest="hostlink_heartbeat_interval",
        type=float,
        default=None,
        help="Slave 心跳发送间隔（秒），默认 5。",
    )
    parser.add_argument(
        "--hostlink_heartbeat_timeout",
        "--hostlink-heartbeat-timeout",
        dest="hostlink_heartbeat_timeout",
        type=float,
        default=None,
        help="Host 判定 Slave 离线的心跳超时（秒），默认 15。",
    )
    parser.add_argument(
        "--hostlink_connect_timeout",
        "--hostlink-connect-timeout",
        dest="hostlink_connect_timeout",
        type=float,
        default=None,
        help="单次 HostLink TCP 连接/握手超时（秒），默认 5。",
    )
    parser.add_argument(
        "--hostlink_request_timeout",
        "--hostlink-request-timeout",
        dest="hostlink_request_timeout",
        type=float,
        default=None,
        help="HostLink 控制请求超时（秒），默认 10。",
    )
    parser.add_argument(
        "--ros_domain_id",
        "--ros-domain-id",
        dest="ros_domain_id",
        type=int,
        default=None,
        help=(
            "ROS2 domain id（0-232）；Host 下发给 Slave，Slave 本地值仅作连接前兜底。"
        ),
    )
    parser.add_argument(
        "--ros_discovery_range",
        "--ros-discovery-range",
        dest="ros_discovery_range",
        choices=["SYSTEM_DEFAULT", "SUBNET", "LOCALHOST", "OFF"],
        default=None,
        help="Host 下发的 ROS_AUTOMATIC_DISCOVERY_RANGE。",
    )
    parser.add_argument(
        "--ros_static_peers",
        "--ros-static-peers",
        dest="ros_static_peers",
        default=None,
        help="Host 发布的 ROS_STATIC_PEERS，多个地址用分号分隔。",
    )
    parser.add_argument(
        "--ros_discovery_server",
        "--ros-discovery-server",
        dest="ros_discovery_server",
        default=None,
        help=(
            "Fast DDS Discovery Server 的 host:port；off 表示禁用；"
            "空值由 ROS2 组网微后端托管。"
        ),
    )
    parser.add_argument(
        "--ros_discovery_port",
        "--ros-discovery-port",
        dest="ros_discovery_port",
        type=int,
        default=None,
        help=(
            "微后端托管 Fast DDS Discovery Server 的 UDP 端口；"
            "0 表示复用 HostLink 数字端口。"
        ),
    )
    parser.add_argument(
        "--no_ros_assist",
        "--no-ros-assist",
        dest="no_ros_assist",
        action="store_true",
        help=(
            "ROS2 backend：保留 HostLink 设备发现和心跳，"
            "但不应用 Host 下发的 ROS2 环境。"
        ),
    )
    parser.add_argument(
        "--slave_no_host",
        "--slave-no-host",
        dest="slave_no_host",
        action="store_true",
        help=(
            "允许 Slave 在 HostLink/Host ROS 服务离线时启动；"
            "控制通道仍会在后台重连。"
        ),
    )
    parser.add_argument(
        "--upload_registry",
        action="store_true",
        help="Upload registry information when starting unilab",
    )
    parser.add_argument(
        "--use_remote_resource",
        action="store_true",
        help="Use remote resources when starting unilab",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Configuration file path, supports .py format Python config files",
    )
    parser.add_argument(
        "--port_management",
        "--port-management",
        "--port",
        dest="port_management",
        type=int,
        default=None,
        help=(
            "管理端 HTTP/Web API 端口，状态页和主微前端使用，默认 8002；"
            "--port 是兼容缩写，不影响 HostLink TCP 端口。"
        ),
    )
    parser.add_argument(
        "--disable_browser",
        "--disable-browser",
        dest="disable_browser",
        action="store_true",
        help=(
            "仅禁止启动时自动打开浏览器；管理端 HTTP/Web 服务仍会在 "
            "--port-management 指定的端口启动。"
        ),
    )
    parser.add_argument(
        "--2d_vis",
        action="store_true",
        help="Enable 2D visualization when starting pylabrobot instance",
    )
    parser.add_argument(
        "--visual",
        choices=["rviz", "web", "disable"],
        default="disable",
        help="Choose visualization tool: rviz, web, or disable",
    )
    parser.add_argument(
        "--ak",
        type=str,
        default="",
        help="Access key for laboratory requests",
    )
    parser.add_argument(
        "--sk",
        type=str,
        default="",
        help="Secret key for laboratory requests",
    )
    parser.add_argument(
        "--addr",
        type=str,
        default="https://leap-lab.bohrium.com/api/v1",
        help="Laboratory backend address (API)",
    )
    parser.add_argument(
        "--schedule_addr",
        type=str,
        default="",
        help=(
            "Schedule WebSocket address. If empty, derived from --addr: "
            "port +1 when --addr has a port, otherwise the same host is used."
        ),
    )
    parser.add_argument(
        "--skip_env_check",
        action="store_true",
        help="Skip environment dependency check on startup",
    )
    parser.add_argument(
        "--check_mode",
        action="store_true",
        default=False,
        help="Run in check mode for CI: validates registry imports and ensures no file changes",
    )
    parser.add_argument(
        "--complete_registry",
        action="store_true",
        default=False,
        help="Complete and rewrite YAML registry files using AST analysis results",
    )
    parser.add_argument(
        "--no_update_feedback",
        action="store_true",
        help="Disable sending update feedback to server",
    )
    parser.add_argument(
        "--test_mode",
        action="store_true",
        default=False,
        help="Test mode: all actions simulate execution and return mock results without running real hardware",
    )
    parser.add_argument(
        "--external_devices_only",
        action="store_true",
        default=False,
        help="Only load external device packages (--devices), skip built-in unilabos/devices/ scanning and YAML device registry",
    )
    parser.add_argument(
        "--extra_resource",
        action="store_true",
        default=False,
        help="Load extra lab_ prefixed labware resources (529 auto-generated definitions from lab_resources.py)",
    )
    parser.add_argument(
        "--restart_mode",
        action="store_true",
        default=False,
        help="Enable supervisor mode: automatically restart the process when triggered via WebSocket",
    )
    parser.add_argument(
        "--auto_restart_count",
        type=int,
        default=500,
        help="Maximum number of automatic restarts in restart mode (default: 500)",
    )
    # package subcommand: 社区设备包 inspect / upload
    package_parser = subparsers.add_parser(
        "package",
        aliases=["pkg"],
        help="Community device package tools: inspect / upload / install",
    )
    package_actions = package_parser.add_subparsers(
        title="package actions", dest="package_action"
    )
    for action_name in ("inspect", "upload"):
        action_parser = package_actions.add_parser(
            action_name,
            help=(
                "Scan package dir and generate package_info/archive (local only)"
                if action_name == "inspect"
                else "Inspect then upload archive + package_info to backend /lab/resource"
            ),
        )
        action_parser.add_argument(
            "--path",
            dest="package_path",
            type=str,
            required=True,
            help="Path to the community device package directory (contains pyproject.toml)",
        )
        action_parser.add_argument(
            "--namespace",
            type=str,
            default=None,
            help="Class namespace, e.g. community.acme; defaults to community.<normalized pyproject name>",
        )
        action_parser.add_argument(
            "--out",
            type=str,
            default=None,
            help="Output dir for archive/package_info.json (default: <package>/../dist)",
        )
        if action_name == "upload":
            action_parser.add_argument(
                "--download-url",
                dest="download_url",
                type=str,
                default="",
                help="Explicit reachable archive URL (skips OSS upload; handy for local static server)",
            )

    # install：开发者本地调试入口
    install_parser = package_actions.add_parser(
        "install",
        help="Install a pip spec / git URL locally (uv pip > pip), then scan @device IDs",
    )
    install_parser.add_argument(
        "install_spec",
        type=str,
        help="pip spec (name==version / name) or git URL (git+https://...)",
    )
    install_parser.add_argument(
        "--no-inspect",
        dest="no_inspect",
        action="store_true",
        help="Skip post-install @device scan / device listing",
    )

    # HTTP 客户端子命令（与现有 --ak/--sk/--addr 复用）
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output in JSON format (for AI agent consumption)",
    )

    # login: 保存 ak/sk 到会话文件
    login_parser = subparsers.add_parser("login", help="Save ak/sk to session file")
    login_parser.add_argument("--ak", type=str, required=True, help="Access key")
    login_parser.add_argument("--sk", type=str, required=True, help="Secret key")

    subparsers.add_parser("logout", help="Clear local ak/sk")
    subparsers.add_parser("whoami", help="Show current user information")

    # config show: 查看当前会话配置
    config_parser = subparsers.add_parser("config", help="Show session configuration")
    config_subparsers = config_parser.add_subparsers(title="config subcommands", dest="config_command")
    config_subparsers.add_parser("show", help="Show current session configuration")

    # lab 命令组
    lab_grp_parser = subparsers.add_parser("lab", help="Laboratory management")
    lab_grp_subparsers = lab_grp_parser.add_subparsers(title="lab subcommands", dest="lab_command")
    lab_list_parser = lab_grp_subparsers.add_parser("list", help="List laboratories")
    lab_list_parser.add_argument("--page", type=int, default=1, help="Page number")
    lab_list_parser.add_argument("--page_size", type=int, default=20, help="Page size")

    # material 命令组
    material_grp_parser = subparsers.add_parser("material", help="Material management")
    material_grp_subparsers = material_grp_parser.add_subparsers(
        title="material subcommands", dest="material_command"
    )
    material_list_parser = material_grp_subparsers.add_parser("list", help="List materials in a lab")
    material_list_parser.add_argument("--lab_uuid", type=str, required=True, help="Lab UUID")
    material_list_parser.add_argument(
        "--with_children", action="store_true", default=False, help="Include child resources"
    )

    # workflow 命令组
    workflow_grp_parser = subparsers.add_parser("workflow", help="Workflow management")
    workflow_grp_subparsers = workflow_grp_parser.add_subparsers(
        title="workflow subcommands", dest="workflow_command"
    )
    wf_upload_parser = workflow_grp_subparsers.add_parser("upload", help="Upload workflow file")
    wf_upload_parser.add_argument("-f", "--workflow_file", type=str, required=True, help="Workflow file (JSON)")
    wf_upload_parser.add_argument("-n", "--workflow_name", type=str, default=None, help="Workflow name")
    wf_upload_parser.add_argument("--tags", type=str, nargs="*", default=[], help="Tags (space-separated)")
    wf_upload_parser.add_argument("--published", action="store_true", default=False, help="Publish after upload")
    wf_upload_parser.add_argument("--description", type=str, default="", help="Workflow description")

    return parser


def _resolve_graph_file_path(file_path: str | None) -> str | None:
    if file_path is None:
        return None
    if os.path.isfile(file_path):
        return file_path
    temp_file_path = os.path.abspath(str(os.path.join(__file__, "..", "..", file_path)))
    if os.path.isfile(temp_file_path):
        print_status(f"使用相对路径{temp_file_path}", "info")
        return temp_file_path
    return file_path


def _load_graph_json_preview(file_path: str | None) -> Dict[str, Any] | None:
    if not file_path or not file_path.endswith(".json") or not os.path.isfile(file_path):
        return None
    try:
        with open(file_path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        print_status(f"预读取 graph JSON 失败，跳过 community 包解析: {exc}", "warning")
        return None


def main():
    """主函数"""
    # 解析命令行参数
    parser = parse_args()
    convert_argv_dashes_to_underscores(parser)
    args = parser.parse_args()
    args_dict = vars(args)

    from unilabos.app.backend import (
        BackendConfigurationError,
        resolve_backend_selection,
    )

    try:
        backend_selection = resolve_backend_selection(
            args_dict["backend"],
            args_dict.get("app_bridges"),
            is_slave=args_dict.get("is_slave", False),
            visual=args_dict.get("visual", "disable"),
        )
    except BackendConfigurationError as exc:
        parser.error(str(exc))
    args_dict["backend"] = backend_selection.name
    args_dict["app_bridges"] = list(backend_selection.app_bridges)
    if backend_selection.name == "ros2":
        # HostLink direct backend must not probe/import rclpy as a side effect.
        from unilabos.app.utils import patch_rclpy_dll_windows

        patch_rclpy_dll_windows()

    # 处理 HTTP 客户端子命令（login, logout, whoami, config, lab, material, workflow）
    # 这些命令不需要加载完整的 UniLab-OS 环境，提前处理并退出
    http_client_commands = ["login", "logout", "whoami", "config", "lab", "material", "workflow"]
    if args_dict.get("command") in http_client_commands:
        from unilabos.client import (
            SessionManager,
            set_output_format,
            OutputFormat,
            print_error,
            resolve_addr,
        )
        from unilabos.app.cli.auth import cmd_login, cmd_logout, cmd_whoami
        from unilabos.app.cli.config import cmd_config_show
        from unilabos.app.cli.lab import cmd_lab_list
        from unilabos.app.cli.material import cmd_material_list
        from unilabos.app.cli.workflow import cmd_workflow_upload

        # 设置输出格式
        if args_dict.get("json", False):
            set_output_format(OutputFormat.JSON)

        # 解析 working_dir：与设备控制模式逻辑一致（cwd 或 cwd/unilabos_data）
        raw_working_dir = args_dict.get("working_dir")
        if raw_working_dir:
            wd = os.path.abspath(raw_working_dir)
        else:
            wd = os.path.abspath(os.getcwd())
        if os.path.basename(wd) != "unilabos_data":
            sub = os.path.join(wd, "unilabos_data")
            if os.path.isdir(sub):
                wd = sub

        # 解析 --addr（支持 test/uat/local/prod 别名）
        addr_arg = args_dict.get("addr")
        if addr_arg and addr_arg != parser.get_default("addr"):
            args.addr_resolved = resolve_addr(addr_arg)
        else:
            args.addr_resolved = None

        # 创建会话管理器
        session_manager = SessionManager(working_dir=wd)

        # 路由到对应的命令处理函数
        command = args_dict.get("command")
        if command == "login":
            cmd_login(args, session_manager)
        elif command == "logout":
            cmd_logout(args, session_manager)
        elif command == "whoami":
            cmd_whoami(args, session_manager)
        elif command == "config":
            config_command = args_dict.get("config_command")
            if config_command == "show":
                cmd_config_show(args, session_manager)
            else:
                print_error("config 子命令需要指定: show")
                sys.exit(1)
        elif command == "lab":
            lab_command = args_dict.get("lab_command")
            if lab_command == "list":
                cmd_lab_list(args, session_manager)
            else:
                print_error("lab 子命令需要指定: list")
                sys.exit(1)
        elif command == "material":
            material_command = args_dict.get("material_command")
            if material_command == "list":
                cmd_material_list(args, session_manager)
            else:
                print_error("material 子命令需要指定: list")
                sys.exit(1)
        elif command == "workflow":
            workflow_command = args_dict.get("workflow_command")
            if workflow_command == "upload":
                cmd_workflow_upload(args, session_manager)
            else:
                print_error("workflow 子命令需要指定: upload")
                sys.exit(1)
        else:
            print_error(f"{command} 命令暂未实现")
            sys.exit(1)

        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)

    # Supervisor mode: spawn child processes and monitor for restart
    if args_dict.get("restart_mode", False):
        _run_as_supervisor(args_dict.get("auto_restart_count", 5))
        return

    # 环境检查 - 检查并自动安装必需的包 (可选)
    skip_env_check = args_dict.get("skip_env_check", False)
    check_mode = args_dict.get("check_mode", False)

    if not skip_env_check:
        from unilabos.utils.environment_check import check_environment, check_device_package_requirements

        if not check_environment(auto_install=True):
            print_status("环境检查失败，程序退出", "error")
            os._exit(1)

        # 第一次设备包依赖检查：build_registry 之前，确保 import map 可用
        devices_dirs_for_req = args_dict.get("devices", None)
        if devices_dirs_for_req:
            if not check_device_package_requirements(devices_dirs_for_req):
                print_status("设备包依赖检查失败，程序退出", "error")
                os._exit(1)
    else:
        print_status("跳过环境依赖检查", "warning")

    # 加载配置文件，优先加载config，然后从env读取
    config_path = args_dict.get("config")

    # === 解析 working_dir ===
    # 规则1: working_dir 传入 → 检测 unilabos_data 子目录，已是则不修改
    # 规则2: 仅 config_path 传入 → 用其父目录作为 working_dir
    # 规则4: 两者都传入 → 各用各的，但 working_dir 仍做 unilabos_data 子目录检测
    raw_working_dir = args_dict.get("working_dir")
    if raw_working_dir:
        working_dir = os.path.abspath(raw_working_dir)
    elif config_path and os.path.exists(config_path):
        working_dir = os.path.dirname(os.path.abspath(config_path))
    else:
        working_dir = os.path.abspath(os.getcwd())

    # unilabos_data 子目录自动检测
    if os.path.basename(working_dir) != "unilabos_data":
        unilabos_data_sub = os.path.join(working_dir, "unilabos_data")
        if os.path.isdir(unilabos_data_sub):
            working_dir = unilabos_data_sub
        elif not raw_working_dir and not (config_path and os.path.exists(config_path)):
            # 未显式指定路径，默认使用 cwd/unilabos_data
            working_dir = os.path.abspath(os.path.join(os.getcwd(), "unilabos_data"))

    # === 解析 config_path ===
    if config_path and not os.path.exists(config_path):
        # config_path 传入但不存在，尝试在 working_dir 中查找
        candidate = os.path.join(working_dir, "local_config.py")
        if os.path.exists(candidate):
            config_path = candidate
            print_status(f"在工作目录中发现配置文件: {config_path}", "info")
        else:
            print_status(
                f"配置文件 {config_path} 不存在，工作目录 {working_dir} 中也未找到 local_config.py，"
                f"请通过 --config 传入 local_config.py 文件路径",
                "error",
            )
            os._exit(1)
    elif not config_path:
        # 规则3: 未传入 config_path，尝试 working_dir/local_config.py
        candidate = os.path.join(working_dir, "local_config.py")
        if os.path.exists(candidate):
            config_path = candidate
            print_status(f"发现本地配置文件: {config_path}", "info")
        else:
            print_status("未指定config路径，可通过 --config 传入 local_config.py 文件路径", "info")
            print_status(f"您是否为第一次使用？并将当前路径 {working_dir} 作为工作目录？ (Y/n)", "info")
            if check_mode or input() != "n":
                os.makedirs(working_dir, exist_ok=True)
                config_path = os.path.join(working_dir, "local_config.py")
                shutil.copy(
                    os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "example_config.py"),
                    config_path,
                )
                print_status(f"已创建 local_config.py 路径： {config_path}", "info")
            else:
                os._exit(1)

    # 加载配置文件 (check_mode 跳过)
    print_status(f"当前工作目录为 {working_dir}", "info")
    if not check_mode:
        load_config_from_file(config_path)

    # 根据配置重新设置日志级别
    from unilabos.utils.log import configure_logger, configure_comm_logger, logger

    if hasattr(BasicConfig, "log_level"):
        logger.info(f"Log level set to '{BasicConfig.log_level}' from config file.")
    file_path = configure_logger(loglevel=BasicConfig.log_level, working_dir=working_dir)
    if file_path is not None:
        logger.info(f"[LOG_FILE] {file_path}")

    # 为服务端通信(WebSocket)配置独立日志，避免与主日志混在一起，便于排查通信机制
    comm_log_path = configure_comm_logger(loglevel=BasicConfig.log_level, working_dir=working_dir)
    if comm_log_path is not None:
        logger.info(f"[COMM_LOG_FILE] {comm_log_path}")

    if args.addr != parser.get_default("addr"):
        if args.addr == "test":
            print_status("使用测试环境地址", "info")
            HTTPConfig.remote_addr = "https://leap-lab.test.bohrium.com/api/v1"
        elif args.addr == "uat":
            print_status("使用uat环境地址", "info")
            HTTPConfig.remote_addr = "https://leap-lab.uat.bohrium.com/api/v1"
        elif args.addr == "local":
            print_status("使用本地环境地址", "info")
            HTTPConfig.remote_addr = "http://127.0.0.1:48197/api/v1"
        else:
            HTTPConfig.remote_addr = args.addr

    # schedule 通道地址：显式指定则直接使用，否则在连接时从 remote_addr 派生
    if args_dict.get("schedule_addr", ""):
        HTTPConfig.schedule_addr = args_dict["schedule_addr"]
        print_status(f"使用独立 schedule 地址: {HTTPConfig.schedule_addr}", "info")

    # 设置BasicConfig参数
    if args_dict.get("ak", ""):
        BasicConfig.ak = args_dict.get("ak", "")
        print_status("传入了ak参数，优先采用传入参数！", "info")
    if args_dict.get("sk", ""):
        BasicConfig.sk = args_dict.get("sk", "")
        print_status("传入了sk参数，优先采用传入参数！", "info")
    BasicConfig.working_dir = working_dir

    # package 子命令：在配置/鉴权就绪后尽早处理，不进入设备 bootstrap
    if args_dict.get("command") in ("package", "pkg"):
        from unilabos.app.package_cli import PackageCLIError, cmd_package

        package_http_client = None
        if args_dict.get("package_action") == "upload":
            if not (BasicConfig.ak and BasicConfig.sk):
                print_status("package upload 需要 --ak/--sk 鉴权信息", "error")
                os._exit(1)
            from unilabos.app.web import http_client as _http_client_for_package

            package_http_client = _http_client_for_package
        try:
            cmd_package(args_dict, http_client=package_http_client)
        except PackageCLIError as exc:
            print_status(str(exc), "error")
            os._exit(1)
        return

    # ROS2 backend 用 HostLink 辅助发现；hostlink backend 则在同一 TCP 长连接上
    # 直接同步设备描述/状态和执行设备动作，不导入 ROS。
    is_slave = bool(args_dict.get("is_slave", False))
    _apply_hostlink_cli(args_dict, is_slave=is_slave)
    if args_dict["backend"] == "hostlink":
        from unilabos.config.config import HostLinkConfig

        if not HostLinkConfig.enable:
            parser.error("--backend hostlink 不能与 --disable-hostlink 同时使用")
        if is_slave and not str(HostLinkConfig.host or "").strip():
            parser.error(
                "--backend hostlink --is-slave 必须通过 --host-node-ip 指定 Host"
            )

    # 使用远程资源启动
    if args_dict["use_remote_resource"]:
        print_status("使用远程资源启动", "info")
        from unilabos.app.web import http_client

        res = http_client.resource_get("host_node", False)
        if str(res.get("code", 0)) == "0" and len(res.get("data", [])) > 0:
            print_status("远程资源已存在，使用云端物料！", "info")
            args_dict["graph"] = None
        else:
            print_status("远程资源不存在，本地将进行首次上报！", "info")

    BasicConfig.port = (
        args_dict["port_management"]
        if args_dict["port_management"] is not None
        else BasicConfig.port
    )
    BasicConfig.disable_browser = args_dict["disable_browser"] or BasicConfig.disable_browser
    BasicConfig.is_host_mode = not is_slave
    BasicConfig.slave_no_host = args_dict.get("slave_no_host", False)
    BasicConfig.upload_registry = args_dict.get("upload_registry", False)
    BasicConfig.no_update_feedback = args_dict.get("no_update_feedback", False)
    BasicConfig.test_mode = args_dict.get("test_mode", False)
    if BasicConfig.test_mode:
        print_status("启用测试模式：所有动作将模拟执行，不调用真实硬件", "warning")
    BasicConfig.extra_resource = args_dict.get("extra_resource", False)
    if BasicConfig.extra_resource:
        print_status("启用额外资源加载：将加载lab_开头的labware资源定义", "info")
    BasicConfig.backend = args_dict["backend"]
    BasicConfig.app_bridges = tuple(args_dict["app_bridges"])
    if "websocket" in BasicConfig.app_bridges:
        if args_dict.get("backend_protocol"):
            BasicConfig.communication_protocol = args_dict["backend_protocol"]
    else:
        BasicConfig.communication_protocol = ""
    machine_name = platform.node()
    machine_name = "".join([c if c.isalnum() or c == "_" else "_" for c in machine_name])
    BasicConfig.machine_name = machine_name
    BasicConfig.vis_2d_enable = args_dict["2d_vis"]
    BasicConfig.check_mode = check_mode
    BasicConfig.host_node_name = resolve_host_node_name(
        args_dict.get("host_node_name") or BasicConfig.host_node_name
    )

    configure_material_startup(args_dict)
    server_database_paths = configure_server_databases(
        args_dict, working_dir=working_dir
    )

    from unilabos.registry.registry import build_registry

    # 显示启动横幅
    print_unilab_banner(args_dict)

    # Step -1: 预读取 graph 中的 community.* class，并在 build_registry 前挂载社区设备包
    if not check_mode:
        startup_json_preview = None
        graph_file_path = _resolve_graph_file_path(args_dict.get("graph") or BasicConfig.startup_json_path)
        args_dict["_graph_file_path"] = graph_file_path
        graph_preview = _load_graph_json_preview(graph_file_path)

        http_client_for_community = None
        if BasicConfig.ak and BasicConfig.sk:
            from unilabos.app.web import http_client as _http_client_for_community

            http_client_for_community = _http_client_for_community
            if graph_preview is None and graph_file_path is None:
                startup_json_preview = http_client_for_community.request_startup_json()
                args_dict["_startup_json"] = startup_json_preview
                graph_preview = startup_json_preview

        if graph_preview:
            from unilabos.app.community_packages import (
                CommunityPackageError,
                prepare_community_packages,
            )

            try:
                community_result = prepare_community_packages(
                    graph_preview,
                    working_dir=BasicConfig.working_dir,
                    http_client=http_client_for_community,
                )
            except CommunityPackageError as exc:
                print_status(str(exc), "error")
                os._exit(1)

            if community_result.devices_dirs:
                existing_devices_dirs = args_dict.get("devices") or []
                args_dict["devices"] = existing_devices_dirs + community_result.devices_dirs
                if not skip_env_check:
                    from unilabos.utils.environment_check import (
                        check_device_package_requirements,
                        install_requirements_list,
                    )

                    # 社区包依赖：pyproject [project].dependencies 为标准来源，只装依赖不装包体
                    # （保持源码挂载，便于 track/卸载）；requirements.txt 作为补充兜底
                    if community_result.dependencies and not install_requirements_list(
                        community_result.dependencies, label="community"
                    ):
                        print_status("community 设备包 pyproject 依赖安装失败，程序退出", "error")
                        os._exit(1)
                    if not check_device_package_requirements(args_dict["devices"]):
                        print_status("community 设备包依赖检查失败，程序退出", "error")
                        os._exit(1)
            # 社区包设备直接以 community.<ns>.<id> 注册（扫描期命名空间化），不做 alias 桥接
            args_dict["_community_namespaces"] = community_result.namespaces

    # Step 0: AST 分析优先 + YAML 注册表加载
    # check_mode 和 upload_registry 都会执行实际 import 验证
    devices_dirs = args_dict.get("devices", None)
    complete_registry = args_dict.get("complete_registry", False) or check_mode
    external_only = args_dict.get("external_devices_only", False)
    lab_registry = build_registry(
        registry_paths=args_dict["registry_path"],
        devices_dirs=devices_dirs,
        community_namespaces=args_dict.get("_community_namespaces"),
        upload_registry=(
            BasicConfig.upload_registry
            or (
                BasicConfig.is_host_mode
                and HTTPConfig.material_source in {"microbackend", "auto"}
            )
        ),
        check_mode=check_mode,
        complete_registry=complete_registry,
        external_only=external_only,
    )

    # Check mode: 注册表验证完成后直接退出
    if check_mode:
        device_count = len(lab_registry.device_type_registry)
        resource_count = len(lab_registry.resource_type_registry)
        print_status(f"Check mode: 注册表验证完成 ({device_count} 设备, {resource_count} 资源)，退出", "info")
        os._exit(0)

    # 以下导入依赖 ROS2 环境，check_mode 已退出不需要
    from unilabos.resources.graphio import (
        read_node_link_json,
        read_graphml,
        dict_from_graph,
        modify_to_backend_format,
    )
    from unilabos.app.communication import get_communication_client
    from unilabos.app.backend import start_backend
    from unilabos.app.web import http_client
    from unilabos.app.register import register_devices_and_resources
    from unilabos.resources.resource_tracker import ResourceTreeSet, ResourceDict

    # Step 1: 上传全部注册表到服务端，同步保存到 unilabos_data
    if BasicConfig.upload_registry:
        if BasicConfig.ak and BasicConfig.sk:
            # print_status("开始注册设备到服务端...", "info")
            try:
                register_devices_and_resources(lab_registry)
                # print_status("设备注册完成", "info")
            except Exception as e:
                print_status(f"设备注册失败: {e}", "error")
        else:
            print_status("未提供 ak 和 sk，跳过设备注册", "info")
    else:
        print_status("本次启动注册表不报送云端，如果您需要联网调试，请在启动命令增加--upload_registry", "warning")

    # 使用远程资源启动
    if args_dict["use_remote_resource"]:
        print_status("后续运行必须拥有一个实验室，请前往 https://leap-lab.bohrium.com 注册实验室！", "warning")
        os._exit(1)
    graph: nx.Graph
    resource_tree_set: ResourceTreeSet
    resource_links: List[Dict[str, Any]]
    request_startup_json = args_dict.get("_startup_json")
    if request_startup_json is None:
        request_startup_json = http_client.request_startup_json()

    file_path = args_dict.get("_graph_file_path")
    if file_path is None:
        file_path = _resolve_graph_file_path(args_dict.get("graph") or BasicConfig.startup_json_path)
    if file_path is None:
        if not request_startup_json:
            print_status(
                "未指定设备加载文件路径，尝试从HTTP获取失败，请检查网络或者使用-g参数指定设备加载文件路径", "error"
            )
            os._exit(1)
        else:
            print_status("联网获取设备加载文件成功", "info")
        graph, resource_tree_set, resource_links = read_node_link_json(request_startup_json)
    else:
        if file_path.endswith(".json"):
            graph, resource_tree_set, resource_links = read_node_link_json(file_path)
        else:
            graph, resource_tree_set, resource_links = read_graphml(file_path)
    import unilabos.resources.graphio as graph_res

    graph_res.physical_setup_graph = graph
    resource_edge_info = modify_to_backend_format(resource_links)
    materials = lab_registry.obtain_registry_resource_info()
    materials.extend(lab_registry.obtain_registry_device_info())
    materials = {k["id"]: k for k in materials}
    # 从 ResourceTreeSet 中获取节点信息
    nodes = {node.res_content.id: node.res_content for node in resource_tree_set.all_nodes}
    edge_info = len(resource_edge_info)
    for ind, i in enumerate(resource_edge_info[::-1]):
        source_node: ResourceDict = nodes[i["source"]]
        target_node: ResourceDict = nodes[i["target"]]
        if "sourceHandle" not in source_node:
            continue
        if "targetHandle" not in target_node:
            continue
        source_handle = i["sourceHandle"]
        target_handle = i["targetHandle"]
        source_handler_keys = [
            h["handler_key"] for h in materials[source_node.klass]["handles"] if h["io_type"] == "source"
        ]
        target_handler_keys = [
            h["handler_key"] for h in materials[target_node.klass]["handles"] if h["io_type"] == "target"
        ]
        if source_handle not in source_handler_keys:
            print_status(
                f"节点 {source_node.id} 的source端点 {source_handle} 不存在，请检查，支持的端点 {source_handler_keys}",
                "error",
            )
            resource_edge_info.pop(edge_info - ind - 1)
            continue
        if target_handle not in target_handler_keys:
            print_status(
                f"节点 {target_node.id} 的target端点 {target_handle} 不存在，请检查，支持的端点 {target_handler_keys}",
                "error",
            )
            resource_edge_info.pop(edge_info - ind - 1)
            continue

    # 如果从远端获取了物料信息，则与本地物料进行同步
    if file_path is not None and request_startup_json and "nodes" in request_startup_json:
        print_status("开始同步远端物料到本地...", "info")
        remote_tree_set = ResourceTreeSet.from_raw_dict_list(request_startup_json["nodes"])
        resource_tree_set.merge_remote_resources(remote_tree_set)
        print_status("远端物料同步完成", "info")

    # 第二次设备包依赖检查：云端物料同步后，community 包可能引入新的 requirements
    # TODO: 当 community device package 功能上线后，在这里调用
    #   install_requirements_txt(community_pkg_path / "requirements.txt", label="community.xxx")

    # 使用 ResourceTreeSet 代替 list
    args_dict["resources_config"] = resource_tree_set
    args_dict["devices_config"] = resource_tree_set
    args_dict["graph"] = graph_res.physical_setup_graph

    if args_dict["controllers"] is not None:
        args_dict["controllers_config"] = yaml.safe_load(open(args_dict["controllers"], encoding="utf-8"))
    else:
        args_dict["controllers_config"] = None

    args_dict["bridges"] = []

    # 旧 HTTP bridge 只在正式 Backend 拥有物料写权威时挂载；启用本地
    # FastAPI 不再隐式把 ResourceTree 写到远端。
    if (
        "fastapi" in args_dict["app_bridges"]
        and HTTPConfig.material_source == "backend"
    ):
        args_dict["bridges"].append(http_client)
    # 根据线协议创建后端通信客户端（传输层均为 WebSocket）
    if BasicConfig.is_host_mode:
        comm_client = None
        materials_gateway = None
        if "websocket" in args_dict["app_bridges"]:
            comm_client = get_communication_client()
            args_dict["bridges"].append(comm_client)

            def _exit(signum, frame):
                comm_client.stop()
                sys.exit(0)

            signal.signal(signal.SIGINT, _exit)
            signal.signal(signal.SIGTERM, _exit)

        if should_start_embedded_material_service(
            args_dict, is_host_mode=BasicConfig.is_host_mode
        ):
            from unilabos.server.scheduler.integration import setup_materials_service

            materials_service = setup_materials_service(
                database_paths=server_database_paths,
                ws_client=comm_client,
            )
            from unilabos.server.adapters.registry_materials import (
                sync_registry_resources,
            )
            from unilabos.server.clients.materials import LocalMaterialsClient

            materials_gateway = LocalMaterialsClient(materials_service)
            template_report = sync_registry_resources(
                lab_registry, materials_gateway
            )
            print_status(
                f"Materials Provider 已启用: {server_database_paths.materials_db} "
                f"({template_report.resource_count} 个资源模板)",
                "info",
            )
        elif (
            BasicConfig.is_host_mode
            and HTTPConfig.material_source in {"microbackend", "auto"}
            and args_dict.get("_material_service_mode") == "external"
        ):
            from unilabos.server.adapters.registry_materials import (
                sync_registry_resources,
            )
            from unilabos.server.clients.materials import HTTPMaterialsClient

            materials_gateway = HTTPMaterialsClient(
                HTTPConfig.material_microbackend_addr
            )
            template_report = sync_registry_resources(
                lab_registry, materials_gateway
            )
            print_status(
                "外部 Materials Provider 模板同步完成 "
                f"({template_report.resource_count} 个资源模板)",
                "info",
            )

        if materials_gateway is not None:
            from unilabos.server.scheduler.integration import (
                set_materials_gateway,
            )

            set_materials_gateway(materials_gateway)

        from unilabos.server.scheduler.integration import setup_job_execution_backend

        edge_execution_backend = setup_job_execution_backend(
            ws_client=comm_client,
            database_paths=server_database_paths,
        )
        args_dict["bridges"].append(edge_execution_backend)
        print_status(
            "Job 微后端已启用（仅消费后端调度命令）",
            "info",
        )

        if args_dict["backend"] == "ros2":
            # ROS2 的 HostLink 仅是组网控制面。由微后端在 ROS backend
            # 启动/rclpy.init 之前持有 listener；HostNode 只在创建后挂接
            # 实时资源树，绝不再成为第二个网络生命周期所有者。
            from unilabos.server.scheduler.host_network import (
                setup_host_network_service,
            )
            from unilabos.config.config import HostLinkConfig

            host_network = setup_host_network_service(
                material_gateway=materials_gateway
            )
            if host_network is not None:
                print_status(
                    "ROS2 HostLink 组网微后端已启用: "
                    f"{HostLinkConfig.bind}:{host_network.server.port}",
                    "info",
                )

        # 微后端必须先于控制链路接收命令，避免首个 job_start 绕过生命周期权威。
        if comm_client is not None:
            comm_client.start()
    else:
        print_status("SlaveMode跳过Websocket连接")
        if args_dict["backend"] == "ros2":
            # 正常 Slave 必须在 rclpy.init 前拿到 Host 的 ROS policy；
            # --slave_no_host 才允许离线启动并后台重连。
            from unilabos.server.scheduler.host_network import (
                require_slave_startup_device_ids,
                setup_slave_network_client,
            )

            setup_slave_network_client(
                device_ids=require_slave_startup_device_ids(
                    args_dict["devices_config"]
                )
            )

    args_dict["resources_mesh_config"] = {}
    args_dict["resources_edge_config"] = resource_edge_info
    # web visiualize 2D
    if args_dict["visual"] != "disable":
        enable_rviz = args_dict["visual"] == "rviz"
        devices_and_resources = dict_from_graph(graph_res.physical_setup_graph)
        if devices_and_resources is not None:
            from unilabos.device_mesh.resource_visalization import (
                ResourceVisualization,
            )  # 此处开启后，logger会变更为INFO，有需要请调整

            resource_visualization = ResourceVisualization(
                devices_and_resources,
                [n.res_content for n in args_dict["resources_config"].all_nodes],  # type: ignore  # FIXME
                enable_rviz=enable_rviz,
            )
            args_dict["resources_mesh_config"] = resource_visualization.resource_model
            start_backend(**args_dict)
            if "fastapi" in args_dict["app_bridges"]:
                from unilabos.app.web import start_server

                server_thread = threading.Thread(
                    target=start_server,
                    kwargs=dict(
                        open_browser=not BasicConfig.disable_browser,
                        port=BasicConfig.port,
                    ),
                )
                server_thread.start()
            asyncio.set_event_loop(asyncio.new_event_loop())
            try:
                resource_visualization.start()
            except OSError as e:
                if "AMENT_PREFIX_PATH" in str(e):
                    print_status(f"ROS 2环境未正确设置，跳过3D可视化启动。错误详情: {e}", "warning")
                    print_status(
                        "建议解决方案：\n"
                        "1. 激活Conda环境: conda activate unilab\n"
                        "2. 或使用 --backend hostlink 参数（无需 ROS2 可视化）\n"
                        "3. 或使用 --visual disable 参数禁用可视化",
                        "info",
                    )
                else:
                    raise
            while True:
                time.sleep(1)
        else:
            backend_thread = start_backend(**args_dict)
            if "fastapi" in args_dict["app_bridges"]:
                from unilabos.app.web import start_server

                restart_requested = start_server(
                    open_browser=not BasicConfig.disable_browser,
                    port=BasicConfig.port,
                )
            else:
                backend_thread.join()
                restart_requested = False
            if restart_requested:
                print_status("[Main] Restart requested, cleaning up...", "info")
                cleanup_for_restart()
                return
    else:
        backend_thread = start_backend(**args_dict)

        # 只有声明支持 FastAPI bridge 的 backend 才加载 ROS2 Web 状态面。
        if "fastapi" in args_dict["app_bridges"]:
            from unilabos.app.web import start_server

            restart_requested = start_server(
                open_browser=not BasicConfig.disable_browser,
                port=BasicConfig.port,
            )
        else:
            backend_thread.join()
            restart_requested = False
        if restart_requested:
            print_status("[Main] Restart requested, cleaning up...", "info")
            cleanup_for_restart()
            os._exit(RESTART_EXIT_CODE)


if __name__ == "__main__":
    main()
