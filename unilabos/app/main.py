import argparse
import asyncio
import faulthandler
import os
import platform
import shutil
import sys
import threading
import time
from typing import Any, Dict, List, Optional

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

from unilabos.app.process_shutdown import install_host_shutdown_handlers
from unilabos.app.process_supervisor import RESTART_EXIT_CODE, run_as_supervisor
from unilabos.app.utils import cleanup_for_restart
from unilabos.app.workspace_package_bootstrap import (
    WorkspaceCommunityBootstrapError,
    prepare_startup_community_packages,
    resolve_graph_file_path as _resolve_graph_file_path,
)
from unilabos.config.config import (
    BasicConfig,
    HTTPConfig,
    load_config,
)
from unilabos.utils.banner_print import print_status, print_unilab_banner

# Global restart flags (used by ws_client and web/server)
_restart_requested: bool = False
_restart_reason: str = ""

def _request_workspace_restart(reasons: tuple[str, ...]) -> None:
    """把安全工作区待重启原因提交给现有产品重启循环。

    参数：``reasons`` 是工作区包运行时（Workspace Package Runtime）给出的稳定
    原因集合。
    返回：无；仅设置现有进程级重启标志，不直接退出或停止设备。
    异常：无。
    """

    global _restart_reason, _restart_requested
    _restart_reason = ",".join(reasons)
    _restart_requested = True


def load_config_from_file(config_path):
    if config_path is None:
        config_path = os.environ.get("UNILABOS_BASICCONFIG_CONFIG_PATH", None)
    if config_path:
        if not os.path.exists(config_path):
            print_status(f"配置文件 {config_path} 不存在", "error")
        elif not config_path.endswith(".py"):
            print_status(
                f"配置文件 {config_path} 不是Python文件，必须以.py结尾", "error"
            )
        else:
            load_config(config_path)
    else:
        print_status(
            f"启动 UniLab-OS时，配置文件参数未正确传入 --config '{config_path}' 尝试本地配置...",
            "warning",
        )
        load_config(config_path)


def convert_argv_dashes_to_underscores(args: argparse.ArgumentParser):
    # easier for user input, easier for dev search code
    option_strings = list(args._option_string_actions.keys())
    for i, arg in enumerate(sys.argv):
        for option_string in option_strings:
            if arg.startswith(option_string):
                new_arg = (
                    arg[:2]
                    + arg[2 : len(option_string)].replace("-", "_")
                    + arg[len(option_string) :]
                )
                sys.argv[i] = new_arg
                break


def configure_workflow_editable_package_roots(
    args_dict: Dict[str, Any],
) -> tuple[str, ...]:
    """冻结当前进程工作流源码（Workflow Source）的唯一授权目录集合。

    参数：``args_dict`` 是启动参数投影；工作区（Workspace）生成的授权根存在时
    覆盖配置，否则配置必须已经是不可变 ``tuple[str, ...]``。返回：保持声明顺序
    的绝对路径 tuple，并同步写入 ``BasicConfig``。异常：非 tuple 配置、工作区
    投影不是列表、空项或非字符串项抛出 ``TypeError``。
    """

    workspace_roots = args_dict.get("workflow_editable_package_root")
    if workspace_roots is None:
        configured_roots = BasicConfig.workflow_editable_package_roots
        if not isinstance(configured_roots, tuple):
            raise TypeError("工作流源码授权目录配置必须是 tuple")
    else:
        if not isinstance(workspace_roots, list):
            raise TypeError("工作流源码工作区投影必须是目录列表")
        configured_roots = tuple(workspace_roots)
    if any(not isinstance(root, str) or not root.strip() for root in configured_roots):
        raise TypeError("工作流源码授权目录必须是非空字符串")
    # ``frozen_roots`` 只做形状与绝对路径冻结；符号链接和目录身份由发现层核验。
    frozen_roots = tuple(
        os.path.abspath(os.path.expanduser(root)) for root in configured_roots
    )
    BasicConfig.workflow_editable_package_roots = frozen_roots
    return frozen_roots


def should_bootstrap_local_resource_graph(
    *, is_host_mode: bool
) -> bool:
    """判断当前 OS 节点是否应建立本地资源图投影（Resource Graph Projection）。

    参数：``is_host_mode`` 表示当前节点是否为主机。返回：主机承担本地库存权威
    （Inventory Authority）与调度权威（Scheduler Authority）时为 ``True``，从
    节点为 ``False``。异常：无；OS 不代理正式后端（Backend）数据源。
    """

    return is_host_mode


def should_attach_legacy_http_bridge(args_dict: Dict[str, Any]) -> bool:
    """判断是否挂接显式启用的旧云端 HTTP 桥。

    参数：``args_dict`` 是规范化启动参数。返回：仅显式启用
    ``--use_remote_resource`` 时为 ``True``。异常：无；``fastapi`` 只表示 OS
    对前端提供入站 HTTP 服务，不授权任何后端（Backend）出站连接。
    """

    return bool(args_dict.get("use_remote_resource", False))


def should_request_remote_startup(
    *,
    startup_json: Optional[Dict[str, Any]],
    graph_file_path: Optional[str],
    use_remote_resource: bool = False,
) -> bool:
    """只在显式旧云端模式且没有本地图时请求遗留启动图。

    参数：``startup_json`` 与 ``graph_file_path`` 是已有启动图来源，
    ``use_remote_resource`` 是旧云端兼容开关。返回：是否允许发出远端请求。
    异常：无；普通 OS 启动始终关闭失败，不隐式连接后端（Backend）。
    """

    return (
        use_remote_resource
        and startup_json is None
        and graph_file_path is None
    )


def should_prepare_workspace_product_runtime(args_dict: dict[str, Any]) -> bool:
    """判断当前命令是否属于需要工作区产品运行时的常驻启动。

    参数：``args_dict`` 是公共命令行（CLI）参数。
    返回：软件包管理命令返回 ``False``，普通常驻启动返回 ``True``；软件包查询、
    上传和依赖增改删不得预读物理图（Graph）或现有依赖锁并启动监视线程。
    异常：无。
    """

    return args_dict.get("command") not in {"package", "pkg"}


def dispatch_local_package_command(args_dict: dict[str, Any]) -> bool:
    """在产品启动前分派不依赖远端配置的包命令。

    参数：``args_dict`` 是公共命令行（CLI）解析出的完整参数字典。
    返回：inspect、build、add、update、remove 由包管理深模块处理时返回 ``True``；
    非包命令、缺少子动作或需要显式鉴权的 upload 返回 ``False``，由后续既有路径
    处理。
    异常：软件包命令行（Package CLI）合同错误转换为退出码 1 的 ``SystemExit``；
    本接缝不得创建工作目录、读取产品配置、执行环境检查或启动 ROS/设备运行时。
    """

    command = args_dict.get("command")
    package_action = args_dict.get("package_action")
    if command not in {"package", "pkg"} or package_action not in {
        "inspect",
        "build",
        "add",
        "update",
        "remove",
    }:
        return False

    from unilabos.package_manager.cli import PackageCLIError, cmd_package

    try:
        cmd_package(args_dict)
    except PackageCLIError as error:
        print_status(str(error), "error")
        raise SystemExit(1) from error
    return True


def parse_args():
    """构建 UniLab-OS 主进程命令行解析器。

    参数：无。返回：包含产品启动和子命令参数的 ``ArgumentParser``；本函数只
    定义合同，不读取或修改进程参数。
    异常：无；参数错误只会在调用者后续执行 ``parse_args`` 时产生 ``SystemExit``。
    """
    parser = argparse.ArgumentParser(description="Start Uni-Lab Edge server.")
    subparsers = parser.add_subparsers(title="Valid subcommands", dest="command")

    parser.add_argument("-g", "--graph", help="Physical setup graph file path.")
    parser.add_argument(
        "--workspace",
        type=str,
        nargs="?",
        const=".",
        default=None,
        help="显式 Uni-Lab 工作区（Workspace）根目录；省略路径时使用当前目录。",
    )
    parser.add_argument(
        "-c", "--controllers", default=None, help="Controllers config file path."
    )
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
        help="可选的可写运行目录；提供 --workspace 时默认使用 <workspace>/.unilabos。",
    )
    parser.add_argument(
        "--preserve_runtime_databases",
        action="store_true",
        help="不创建临时会话目录，沿用工作目录内的三类 SQLite 数据库。",
    )
    parser.add_argument(
        "--backend",
        choices=["ros", "simple", "automancer"],
        default="ros",
        help="Choose the backend to run with: 'ros', 'simple', or 'automancer'.",
    )
    parser.add_argument(
        "--app_bridges",
        nargs="+",
        choices=["websocket", "fastapi"],
        default=["websocket", "fastapi"],
        help="Bridges to connect to: websocket (legacy) and fastapi.",
    )
    parser.add_argument(
        "--is_slave",
        action="store_true",
        help="Run the backend as slave node (without host privileges).",
    )
    parser.add_argument(
        "--hostlink_addr",
        type=str,
        default="",
        help="HostLink TCP channel address. Slave: Host microbackend 'ip[:port]' to join the "
        "network; Host: 'bind[:port]' to listen on (default 0.0.0.0:7302). "
        "Material queries and ROS discovery assist go through this channel.",
    )
    parser.add_argument(
        "--ros_domain_id",
        type=int,
        default=None,
        help="ROS_DOMAIN_ID for this process. Host: also advertised to slaves via "
        "HostLink handshake (network-wide domain). Slave: local fallback only — "
            "the value downloaded from host wins once connected.",
    )
    parser.add_argument(
        "--ros_discovery_port",
        type=int,
        default=None,
        help="UDP port for the Host-managed Fast DDS discovery server. Default 0 "
        "reuses the HostLink numeric port (TCP and UDP do not conflict).",
    )
    parser.add_argument(
        "--ros_discovery_server",
        type=str,
        default=None,
        help="Use an external Fast DDS discovery server (ip:port), or 'off' to "
        "disable the Host-managed directed discovery server.",
    )
    parser.add_argument(
        "--slave_no_host",
        action="store_true",
        help="Allow an intentional offline Slave start without waiting for Host. "
        "HostLink continues reconnecting in the background and local ROS config is used.",
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
        "--port",
        type=int,
        default=None,
        help="Port for web service information page",
    )
    parser.add_argument(
        "--disable_browser",
        action="store_true",
        help="Disable opening information page on startup",
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
        "--action_mode",
        choices=["real", "simulate"],
        default="real",
        help=(
            "Action execution mode: 'real' dispatches to hardware; 'simulate' "
            "returns simulated success results without dispatching hardware actions."
        ),
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
    subparsers.add_parser(
        "template-sync",
        aliases=["template_sync"],
        help="Collect the complete Edge Registry and transactionally sync templates",
    )
    instance_sync_parser = subparsers.add_parser(
        "instance-sync",
        aliases=["instance_sync"],
        help="Create missing backend material instances from an Edge device graph",
    )
    instance_sync_parser.add_argument(
        "--check_only",
        dest="instance_check_only",
        action="store_true",
        help="Only verify that every graph node has a matching backend instance",
    )
    # workflow upload subcommand
    workflow_parser = subparsers.add_parser(
        "workflow_upload",
        aliases=["wf"],
        help="Upload workflow from xdl/json/python files",
    )
    workflow_parser.add_argument(
        "-f",
        "--workflow_file",
        type=str,
        required=True,
        help="Path to the workflow file (JSON format)",
    )
    workflow_parser.add_argument(
        "-n",
        "--workflow_name",
        type=str,
        default=None,
        help="Workflow name, if not provided will use the name from file or filename",
    )
    workflow_parser.add_argument(
        "--tags",
        type=str,
        nargs="*",
        default=[],
        help="Tags for the workflow (space-separated)",
    )
    workflow_parser.add_argument(
        "--published",
        action="store_true",
        default=False,
        help="Whether to publish the workflow (default: False)",
    )
    workflow_parser.add_argument(
        "--description",
        type=str,
        default="",
        help="Workflow description, used when publishing the workflow",
    )

    # doctor subcommand: host-slave 组网分层诊断（TCP 探测 / talker / listener / 假设备）
    doctor_parser = subparsers.add_parser(
        "doctor",
        help="Network diagnostics: TCP probe, ROS talker/listener pair, fake device",
    )
    doctor_sub = doctor_parser.add_subparsers(
        title="doctor subcommands", dest="doctor_command"
    )

    def _add_doctor_common(sub_parser):
        sub_parser.add_argument(
            "--hostlink_addr",
            type=str,
            default="",
            help="Host ip[:port] — TCP probe target, also fetches ROS network info via handshake",
        )
        sub_parser.add_argument(
            "--peer",
            type=str,
            default="",
            help="Peer ip list (comma separated) for unicast-only diagnosis "
            "(sets ROS_STATIC_PEERS; defaults discovery to OFF)",
        )
        sub_parser.add_argument(
            "--ros_domain_id", type=int, default=None, help="ROS domain id override"
        )
        sub_parser.add_argument(
            "--discovery",
            type=str,
            default="",
            choices=["", "OFF", "LOCALHOST", "SUBNET"],
            help="ROS_AUTOMATIC_DISCOVERY_RANGE (default: OFF when --peer given)",
        )
        sub_parser.add_argument(
            "--topic", type=str, default="/unilab_doctor", help="Probe topic"
        )
        sub_parser.add_argument(
            "--rate", type=float, default=1.0, help="Publish rate Hz"
        )
        sub_parser.add_argument(
            "--duration",
            type=float,
            default=0.0,
            help="Seconds to run, 0 = until Ctrl-C",
        )

    doctor_net = doctor_sub.add_parser(
        "net", help="Layer 1: TCP channel probe (no ROS needed)"
    )
    _add_doctor_common(doctor_net)
    doctor_net.add_argument("--count", type=int, default=5, help="Ping count")
    doctor_talker = doctor_sub.add_parser(
        "talker", help="Layer 2: publish probe messages"
    )
    _add_doctor_common(doctor_talker)
    doctor_listener = doctor_sub.add_parser(
        "listener", help="Layer 2: receive probes, report loss/latency"
    )
    _add_doctor_common(doctor_listener)
    doctor_listener.add_argument(
        "--quiet", action="store_true", help="Only print final summary"
    )
    doctor_fake = doctor_sub.add_parser(
        "fake-device",
        help="Fake device: probe as a device + check host service visibility",
    )
    _add_doctor_common(doctor_fake)
    doctor_fake.add_argument(
        "--device_id", type=str, default="", help="Fake device id (default random)"
    )
    doctor_fake.add_argument(
        "--no_service_check",
        action="store_true",
        help="Skip host registration service visibility check",
    )

    # 软件包命令行（Package CLI）的解析合同由 package_manager 深模块唯一维护。
    from unilabos.package_manager.cli import register_package_subcommands

    register_package_subcommands(subparsers)

    # HTTP 客户端子命令（与现有 --ak/--sk/--addr 复用）。输出格式只属于真正
    # 产生客户端输出的叶子命令，不再污染常驻 OS 根启动合同。
    def _add_json_output_argument(command_parser: argparse.ArgumentParser) -> None:
        command_parser.add_argument(
            "--json",
            action="store_true",
            help="Output this command result in JSON format",
        )

    # login: 保存 ak/sk 到会话文件
    login_parser = subparsers.add_parser("login", help="Save ak/sk to session file")
    login_parser.add_argument("--ak", type=str, required=True, help="Access key")
    login_parser.add_argument("--sk", type=str, required=True, help="Secret key")
    _add_json_output_argument(login_parser)

    logout_parser = subparsers.add_parser("logout", help="Clear local ak/sk")
    _add_json_output_argument(logout_parser)
    whoami_parser = subparsers.add_parser(
        "whoami", help="Show current user information"
    )
    _add_json_output_argument(whoami_parser)

    # config show: 查看当前会话配置
    config_parser = subparsers.add_parser("config", help="Show session configuration")
    config_subparsers = config_parser.add_subparsers(
        title="config subcommands", dest="config_command"
    )
    config_show_parser = config_subparsers.add_parser(
        "show", help="Show current session configuration"
    )
    _add_json_output_argument(config_show_parser)

    # lab 命令组
    lab_grp_parser = subparsers.add_parser("lab", help="Laboratory management")
    lab_grp_subparsers = lab_grp_parser.add_subparsers(
        title="lab subcommands", dest="lab_command"
    )
    lab_list_parser = lab_grp_subparsers.add_parser("list", help="List laboratories")
    lab_list_parser.add_argument("--page", type=int, default=1, help="Page number")
    lab_list_parser.add_argument("--page_size", type=int, default=20, help="Page size")
    _add_json_output_argument(lab_list_parser)

    # material 命令组
    material_grp_parser = subparsers.add_parser("material", help="Material management")
    material_grp_subparsers = material_grp_parser.add_subparsers(
        title="material subcommands", dest="material_command"
    )
    material_list_parser = material_grp_subparsers.add_parser(
        "list", help="List materials in a lab"
    )
    material_list_parser.add_argument(
        "--lab_uuid", type=str, required=True, help="Lab UUID"
    )
    material_list_parser.add_argument(
        "--with_children",
        action="store_true",
        default=False,
        help="Include child resources",
    )
    _add_json_output_argument(material_list_parser)

    # workflow 命令组
    workflow_grp_parser = subparsers.add_parser("workflow", help="Workflow management")
    workflow_grp_subparsers = workflow_grp_parser.add_subparsers(
        title="workflow subcommands", dest="workflow_command"
    )
    wf_upload_parser = workflow_grp_subparsers.add_parser(
        "upload", help="Upload workflow file"
    )
    wf_upload_parser.add_argument(
        "-f", "--workflow_file", type=str, required=True, help="Workflow file (JSON)"
    )
    wf_upload_parser.add_argument(
        "-n", "--workflow_name", type=str, default=None, help="Workflow name"
    )
    wf_upload_parser.add_argument(
        "--tags", type=str, nargs="*", default=[], help="Tags (space-separated)"
    )
    wf_upload_parser.add_argument(
        "--published", action="store_true", default=False, help="Publish after upload"
    )
    wf_upload_parser.add_argument(
        "--description", type=str, default="", help="Workflow description"
    )
    _add_json_output_argument(wf_upload_parser)

    return parser


def main():
    """解析产品配置并启动所选 UniLab-OS 运行模式。

    参数：无。返回：普通服务退出时为 ``None``，部分一次性子命令返回整数状态；
    配置、环境或子命令失败沿用现有退出策略。工作区（Workspace）
    包目录（PackageCatalog）、注册表快照（Registry Snapshot）、
    有限激活计划和工作流源码（Workflow Source）授权在任何
    作者模块导入或 Web 组合根启动前一次冻结。
    异常：命令行错误以 ``SystemExit`` 结束；静态编译、配置和设备启动故障按各
    既有边界传播或转换为产品退出状态，不发布部分工作区候选代。
    """
    # 解析命令行参数
    parser = parse_args()
    convert_argv_dashes_to_underscores(parser)
    args = parser.parse_args()
    args_dict = vars(args)

    # doctor 子命令：组网诊断，不加载完整环境（net 甚至不 import rclpy），提前处理并退出
    if args_dict.get("command") == "doctor":
        from unilabos.hostlink.doctor import run_doctor

        sys.exit(run_doctor(args_dict))

    # 纯本地软件包命令行（Package CLI）不得落入产品启动路径；upload 仍需后续
    # 显式鉴权和远端配置，但同样不会安装工作区产品生命周期。
    if dispatch_local_package_command(args_dict):
        return

    # 处理 HTTP 客户端子命令（login, logout, whoami, config, lab, material, workflow）
    # 这些命令不需要加载完整的 UniLab-OS 环境，提前处理并退出
    http_client_commands = [
        "login",
        "logout",
        "whoami",
        "config",
        "lab",
        "material",
        "workflow",
    ]
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
        run_as_supervisor(args_dict.get("auto_restart_count", 5))
        return

    # 工作区（Workspace）只在常驻启动路径静态编译一次；运行时
    # 同时持有包目录（PackageCatalog）、注册表快照（Registry
    # Snapshot）、有限激活与工作流源码（Workflow Source）计划。
    from unilabos.package_manager import (
        PackageCompileError,
        PackageDependencyError,
        WorkspaceGenerationChangedError,
        prepare_stable_workspace_product_generation,
    )

    try:
        prepared_workspace_generation = (
            prepare_stable_workspace_product_generation(args_dict)
            if should_prepare_workspace_product_runtime(args_dict)
            else None
        )
        workspace_registry_runtime = (
            prepared_workspace_generation.candidate
            if prepared_workspace_generation is not None
            else None
        )
    except (
        PackageCompileError,
        PackageDependencyError,
        TypeError,
        ValueError,
        WorkspaceGenerationChangedError,
    ) as error:
        parser.error(str(error))
    if workspace_registry_runtime is not None:
        print_status(
            "已编译工作区（Workspace）注册表运行时: "
            f"{workspace_registry_runtime.catalog.import_package}",
            "info",
        )

    # 环境检查 - 检查并自动安装必需的包 (可选)
    # ``ensure_dependencies`` 只来自已验证工作区计划；产品不再保留重复命令行入口。
    ensure_dependencies = args_dict.get("_ensure_dependencies", True)
    check_mode = args_dict.get("check_mode", False)

    if ensure_dependencies:
        from unilabos.utils.environment_check import (
            check_environment,
            check_device_package_requirements,
        )

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
        print_status("工作区配置已关闭启动依赖保障", "warning")

    # 加载配置文件，优先加载config，然后从env读取
    config_path = args_dict.get("config")

    # === 解析 working_dir ===
    # 新启动使用隐藏的 ``.unilabos``；已存在的 ``unilabos_data`` 只作为遗留兼容，
    # 显式参数或工作区（Workspace）派生路径始终精确优先。
    raw_working_dir = args_dict.get("working_dir")
    from unilabos.app.runtime_storage import resolve_working_directory

    working_dir_resolution = resolve_working_directory(
        requested=raw_working_dir,
        config_path=config_path,
    )
    working_dir = working_dir_resolution.path
    if working_dir_resolution.used_legacy_directory:
        print_status(
            f"检测到旧运行目录并继续兼容使用: {working_dir}；新默认目录为 .unilabos",
            "warning",
        )

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
            print_status(
                "未指定config路径，可通过 --config 传入 local_config.py 文件路径",
                "info",
            )
            print_status(
                f"您是否为第一次使用？并将当前路径 {working_dir} 作为工作目录？ (Y/n)",
                "info",
            )
            if check_mode or input() != "n":
                os.makedirs(working_dir, exist_ok=True)
                config_path = os.path.join(working_dir, "local_config.py")
                shutil.copy(
                    os.path.join(
                        os.path.dirname(os.path.dirname(__file__)),
                        "config",
                        "example_config.py",
                    ),
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
    file_path = configure_logger(
        loglevel=BasicConfig.log_level, working_dir=working_dir
    )
    if file_path is not None:
        logger.info(f"[LOG_FILE] {file_path}")

    # 为服务端通信(WebSocket)配置独立日志，避免与主日志混在一起，便于排查通信机制
    comm_log_path = configure_comm_logger(
        loglevel=BasicConfig.log_level, working_dir=working_dir
    )
    if comm_log_path is not None:
        logger.info(f"[COMM_LOG_FILE] {comm_log_path}")

    # 配置完成后再初始化可选 OTel，避免默认配置在 env/file 覆盖前抢先生效。
    from unilabos.utils.tracing import initialize_tracing

    initialize_tracing()

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

    # 设置BasicConfig参数
    if args_dict.get("ak", ""):
        BasicConfig.ak = args_dict.get("ak", "")
        print_status("传入了ak参数，优先采用传入参数！", "info")
    if args_dict.get("sk", ""):
        BasicConfig.sk = args_dict.get("sk", "")
        print_status("传入了sk参数，优先采用传入参数！", "info")
    BasicConfig.working_dir = working_dir
    BasicConfig.extra_resource = bool(args_dict.get("extra_resource", False))
    configure_workflow_editable_package_roots(args_dict)
    # ``workflow_source_discovery_plan`` 与工作区注册表快照（Registry
    # Snapshot）来自同一编译代；无工作区时保持旧授权根发现。
    BasicConfig.workflow_source_discovery_plan = (
        workspace_registry_runtime.workflow_source_plan
        if workspace_registry_runtime is not None
        else None
    )
    if workspace_registry_runtime is not None:
        from unilabos.package_manager.workspace_runtime import (
            compile_workspace_package_mount_projection,
        )

        BasicConfig.workspace_package_mount_projection = (
            compile_workspace_package_mount_projection(
                workspace_registry_runtime.package_catalog_sources,
                editable_source=workspace_registry_runtime.source,
                dependency_revision=workspace_registry_runtime.dependency_revision,
            )
        )
    else:
        BasicConfig.workspace_package_mount_projection = None

    if args_dict.get("command") in ("template-sync", "template_sync"):
        from unilabos.app.template_sync import (
            TemplateSyncError,
            run_template_sync_command,
        )

        try:
            report = run_template_sync_command(
                args_dict,
                backend_address=HTTPConfig.remote_addr,
            )
        except TemplateSyncError as exc:
            print_status(f"模板同步失败: {exc}", "error")
            return 1
        print_status(
            "模板同步完成: "
            f"{report.device_count} 个设备模板，"
            f"{report.resource_count} 个器材模板",
            "info",
        )
        return 0

    if args_dict.get("command") in ("instance-sync", "instance_sync"):
        from unilabos.app.instance_sync import (
            InstanceSyncError,
            run_instance_sync_command,
        )

        try:
            report = run_instance_sync_command(
                args_dict,
                backend_address=HTTPConfig.remote_addr,
            )
        except InstanceSyncError as exc:
            print_status(f"资源实例初始化失败: {exc}", "error")
            return 1
        print_status(
            (
                "资源实例启动检查完成: "
                if args_dict.get("instance_check_only", False)
                else "资源实例初始化完成: "
            )
            + f"新建 {report.created_count} 个，复用 {report.existing_count} 个",
            "info",
        )
        return 0

    # OS 主机固定承担本地后端权威；前端在 OS 与正式后端（Backend）之间选择
    # 数据源，OS 自身不代理正式后端的物料（Material）查询。
    BasicConfig.port = args_dict["port"] if args_dict["port"] else BasicConfig.port
    BasicConfig.is_host_mode = not args_dict.get("is_slave", False)
    if BasicConfig.is_host_mode:
        print_status(
            "OS 主机使用 app/scheduler 与嵌入式库存作为本地后端权威",
            "info",
        )
    else:
        print_status(
            "Slave 不启动物料数据库，物料查询仅通过 HostLink 访问 Host", "info"
        )

    # package 子命令：在配置/鉴权就绪后尽早处理，不进入设备 bootstrap
    if args_dict.get("command") in ("package", "pkg"):
        from unilabos.package_manager.cli import PackageCLIError, cmd_package

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

    workflow_upload = args_dict.get("command") in ("workflow_upload", "wf")

    # 旧云端版本允许常驻进程显式复用已上传的远程资源图。
    if not workflow_upload and args_dict["use_remote_resource"]:
        print_status("使用远程资源启动", "info")
        from unilabos.app.web import http_client

        res = http_client.resource_get("host_node", False)
        if str(res.get("code", 0)) == "0" and len(res.get("data", [])) > 0:
            print_status("远程资源已存在，使用云端物料！", "info")
            args_dict["graph"] = None
        else:
            print_status("远程资源不存在，本地将进行首次上报！", "info")

    BasicConfig.disable_browser = (
        args_dict["disable_browser"] or BasicConfig.disable_browser
    )
    BasicConfig.slave_no_host = args_dict.get("slave_no_host", False)
    BasicConfig.action_mode = args_dict.get("action_mode", "real")
    if BasicConfig.action_mode == "simulate":
        print_status(
            "启用模拟动作模式：动作返回模拟成功结果，不调用真实硬件",
            "warning",
        )
    BasicConfig.extra_resource = args_dict.get("extra_resource", False)
    if BasicConfig.extra_resource:
        print_status("启用额外资源加载：将加载lab_开头的labware资源定义", "info")
    BasicConfig.communication_protocol = "websocket"
    # HostLink：--hostlink_addr "addr[:port]"；slave 填 host 地址，host 填监听地址
    hostlink_addr = (args_dict.get("hostlink_addr") or "").strip()
    if hostlink_addr:
        from unilabos.config.config import HostLinkConfig

        addr, _, port_text = hostlink_addr.partition(":")
        if port_text.strip().isdigit():
            HostLinkConfig.port = int(port_text)
        if BasicConfig.is_host_mode:
            HostLinkConfig.bind = addr or HostLinkConfig.bind
        else:
            HostLinkConfig.host = addr or HostLinkConfig.host
    ros_discovery_port = args_dict.get("ros_discovery_port")
    if ros_discovery_port is not None:
        if not 0 <= int(ros_discovery_port) <= 65535:
            raise ValueError("--ros_discovery_port must be between 0 and 65535")
        from unilabos.config.config import HostLinkConfig

        HostLinkConfig.ros_discovery_port = int(ros_discovery_port)
    ros_discovery_server = args_dict.get("ros_discovery_server")
    if ros_discovery_server is not None:
        from unilabos.config.config import HostLinkConfig

        HostLinkConfig.ros_discovery_server = str(ros_discovery_server).strip()
    # --ros_domain_id：写环境变量（本进程 rclpy.init 与子进程/命令行工具生效）；
    # host 同时记入 HostLinkConfig 经握手统一下发全网；slave 连上 host 后以下发值为准
    ros_domain_id = args_dict.get("ros_domain_id")
    if ros_domain_id is not None:
        from unilabos.config.config import HostLinkConfig

        os.environ["ROS_DOMAIN_ID"] = str(ros_domain_id)
        HostLinkConfig.ros_domain_id = str(ros_domain_id)
        print_status(
            f"ROS_DOMAIN_ID = {ros_domain_id}"
            + (
                "（host 将经 HostLink 下发全网）"
                if BasicConfig.is_host_mode
                else "（slave 本地兜底值）"
            ),
            "info",
        )
    machine_name = platform.node()
    machine_name = "".join(
        [c if c.isalnum() or c == "_" else "_" for c in machine_name]
    )
    BasicConfig.machine_name = machine_name
    BasicConfig.check_mode = check_mode

    from unilabos.registry.registry import build_registry

    # 显示启动横幅
    print_unilab_banner(args_dict)

    # Step -1：把完整工作区本地来源与真正缺失的远端社区包一次接入旧启动链。
    try:
        prepare_startup_community_packages(
            args_dict,
            runtime=workspace_registry_runtime,
            check_mode=check_mode,
            workflow_upload=workflow_upload,
            ensure_dependencies=ensure_dependencies,
        )
    except WorkspaceCommunityBootstrapError as error:
        print_status(str(error), "error")
        os._exit(1)

    # Step 0: AST 分析优先 + YAML 注册表加载
    # ``check_mode`` 会执行实际 import 验证；模板同步只走独立子命令。
    devices_dirs = args_dict.get("devices", None)
    complete_registry = args_dict.get("complete_registry", False) or check_mode
    external_only = args_dict.get("external_devices_only", False)
    lab_registry = build_registry(
        registry_paths=args_dict["registry_path"],
        devices_dirs=devices_dirs,
        community_namespaces=args_dict.get("_community_namespaces"),
        upload_registry=False,
        check_mode=check_mode,
        complete_registry=complete_registry,
        external_only=external_only,
    )
    workspace_material_models = None
    if workspace_registry_runtime is not None:
        # 完整注册表快照（Registry Snapshot）只在内置定义成功后原子
        # 发布；作者导入路径必须更晚激活，禁止静态编译期导入驱动。
        from unilabos.package_manager import (
            compile_workspace_material_models,
            install_workspace_product_lifecycle,
        )

        assert prepared_workspace_generation is not None
        if check_mode:
            # 静态检查只验证首代发布形状并立即退出，不安装后台文件监视生命周期。
            workspace_registry_runtime.publish(lab_registry)
            workspace_registry_runtime.activate_import_path()
        else:
            install_workspace_product_lifecycle(
                prepared_workspace_generation,
                registry=lab_registry,
                restart_mode=(
                    os.environ.get("UNILABOS_RESTART_SUPERVISED") == "1"
                ),
                request_restart=_request_workspace_restart,
            )

        # ``workspace_material_models`` 直接消费同代不可变包目录，保留声明文件的
        # 工作区逻辑证据，并只投影 OS 公开 HTTP URL，不向前端暴露本地资产路径。
        workspace_startup_plan = workspace_registry_runtime.startup_plan
        if workspace_startup_plan is None:
            raise RuntimeError("产品工作区注册表运行时缺少同代启动计划")
        workspace_material_models = compile_workspace_material_models(
            workspace_startup_plan,
            workspace_registry_runtime.catalog,
        )
        # ``workspace_material_shapes`` 直接消费完整候选代已经聚合的物料外形；
        # 主包和显式外部包均来自同一包目录（PackageCatalog）/来源配对，不再
        # 重读来源或退回注册表 AST ``file_path``。
        workspace_material_shapes = workspace_registry_runtime.material_shapes
    else:
        workspace_material_shapes = ()

    # Check mode: 注册表验证完成后直接退出
    if check_mode:
        device_count = len(lab_registry.device_type_registry)
        resource_count = len(lab_registry.resource_type_registry)
        print_status(
            f"Check mode: 注册表验证完成 ({device_count} 设备, {resource_count} 资源)，退出",
            "info",
        )
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
    from unilabos.app.web import start_server
    from unilabos.resources.resource_tracker import ResourceTreeSet, ResourceDict

    workflow_upload = args_dict.get("command") in ("workflow_upload", "wf")

    # 旧云端资源复用要求已有实验室；首次上报路径在前置检查中保留本地图。
    if not workflow_upload and args_dict["use_remote_resource"]:
        print_status(
            "后续运行必须拥有一个实验室，请前往 https://leap-lab.bohrium.com 注册实验室！",
            "warning",
        )
        os._exit(1)
    graph: nx.Graph
    resource_tree_set: ResourceTreeSet
    resource_links: List[Dict[str, Any]]
    file_path = args_dict.get("_graph_file_path")
    if file_path is None:
        file_path = _resolve_graph_file_path(
            args_dict.get("graph") or BasicConfig.startup_json_path
        )
    request_startup_json = args_dict.get("_startup_json")
    if should_request_remote_startup(
        startup_json=request_startup_json,
        graph_file_path=file_path,
        use_remote_resource=bool(args_dict.get("use_remote_resource", False)),
    ):
        request_startup_json = http_client.request_startup_json()
    if file_path is None:
        if not request_startup_json:
            print_status(
                "未指定设备加载文件路径，尝试从HTTP获取失败，请检查网络或者使用-g参数指定设备加载文件路径",
                "error",
            )
            os._exit(1)
        else:
            print_status("联网获取设备加载文件成功", "info")
        graph, resource_tree_set, resource_links = read_node_link_json(
            request_startup_json
        )
    else:
        if file_path.endswith(".json"):
            # 工作区（Workspace）的资源图解析使用同一固定物理图（Graph）观察；
            # 解析器会规范化并修改输入，所以每个消费者取得独立副本。
            graph_input = (
                workspace_registry_runtime.graph_copy()
                if workspace_registry_runtime is not None
                else file_path
            )
            graph, resource_tree_set, resource_links = read_node_link_json(graph_input)
        else:
            graph, resource_tree_set, resource_links = read_graphml(file_path)
    import unilabos.resources.graphio as graph_res

    graph_res.physical_setup_graph = graph
    resource_edge_info = modify_to_backend_format(resource_links)
    materials = lab_registry.obtain_registry_resource_info()
    materials.extend(lab_registry.obtain_registry_device_info())
    materials = {k["id"]: k for k in materials}
    # 从 ResourceTreeSet 中获取节点信息
    nodes = {
        node.res_content.id: node.res_content for node in resource_tree_set.all_nodes
    }
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
            h["handler_key"]
            for h in materials[source_node.klass]["handles"]
            if h["io_type"] == "source"
        ]
        target_handler_keys = [
            h["handler_key"]
            for h in materials[target_node.klass]["handles"]
            if h["io_type"] == "target"
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
    if (
        file_path is not None
        and request_startup_json
        and "nodes" in request_startup_json
    ):
        print_status("开始同步远端物料到本地...", "info")
        remote_tree_set = ResourceTreeSet.from_raw_dict_list(
            request_startup_json["nodes"]
        )
        resource_tree_set.merge_remote_resources(remote_tree_set)
        print_status("远端物料同步完成", "info")

    # 第二次设备包依赖检查：云端物料同步后，community 包可能引入新的 requirements
    # TODO: 当 community device package 功能上线后，在这里调用
    #   install_requirements_txt(community_pkg_path / "requirements.txt", label="community.xxx")

    # 使用 ResourceTreeSet 代替 list
    args_dict["resources_config"] = resource_tree_set
    args_dict["devices_config"] = resource_tree_set
    args_dict["graph"] = graph_res.physical_setup_graph

    slave_device_ids: List[str] = []
    if not BasicConfig.is_host_mode:
        from unilabos.app.scheduler.host_network import (
            require_slave_startup_device_ids,
        )

        try:
            slave_device_ids = require_slave_startup_device_ids(resource_tree_set)
        except ValueError as exc:
            print_status(str(exc), "error")
            os._exit(2)

    if args_dict["controllers"] is not None:
        args_dict["controllers_config"] = yaml.safe_load(
            open(args_dict["controllers"], encoding="utf-8")
        )
    else:
        args_dict["controllers_config"] = None

    args_dict["bridges"] = []

    if should_attach_legacy_http_bridge(args_dict):
        args_dict["bridges"].append(http_client)
    if BasicConfig.is_host_mode:
        # 所有输入与设备图均验证后、任何 Store 打开前，才创建本代私有数据库目录。
        from unilabos.app.runtime_storage import prepare_runtime_storage_session

        prepare_runtime_storage_session(args_dict, working_dir=working_dir)
        comm_client = None
        communication_clients = []
        if "websocket" in args_dict["app_bridges"]:
            comm_client = get_communication_client()
            args_dict["bridges"].append(comm_client)
            communication_clients.append(comm_client)
            comm_client.start()

        # Host 即使没有远端通信客户端也拥有 HostLink 和 ROS2 定向发现服务；正常
        # TERM 必须显式关闭这些独立进程资源，不能只依赖不会由默认 TERM 运行的
        # ``atexit``。
        install_host_shutdown_handlers(communication_clients)

        # 主机固定拥有同一运行目录中的库存权威（Inventory Authority）。从节点
        # 不进入此分支，只通过 HostLink 查询主机。
        from unilabos.app.scheduler.integration import setup_edge_inventory
        from unilabos.registry.template_snapshot import RegistryTemplateSnapshot

        inventory_db = str(args_dict.get("edge_inventory_db") or "").strip()
        if not inventory_db:
            raise ValueError("嵌入式物料服务缺少自动派生的 inventory.db")
        inventory_db = os.path.abspath(os.path.expanduser(inventory_db))
        bootstrap_resource_graph = should_bootstrap_local_resource_graph(
            is_host_mode=BasicConfig.is_host_mode,
        )
        setup_edge_inventory(
            inventory_db,
            ws_client=(
                comm_client if "websocket" in args_dict["app_bridges"] else None
            ),
            resource_tree_set=resource_tree_set if bootstrap_resource_graph else None,
            registry_snapshot=(
                RegistryTemplateSnapshot.from_registry(lab_registry)
                if bootstrap_resource_graph
                else None
            ),
            resource_graph_source_id=(
                str(file_path or "remote-startup.json")
                if bootstrap_resource_graph
                else ""
            ),
            material_shapes=workspace_material_shapes,
            material_model_catalog=workspace_material_models,
        )
        print_status(
            f"Host Edge 物料服务已启用 (SQLite WAL: {inventory_db})",
            "info",
        )

        # OS 作为本地后端权威时固定装配 app/scheduler，并使用本地稳定排序。
        from unilabos.app.scheduler.integration import setup_edge_scheduler

        _edge_sched, edge_exec_backend = setup_edge_scheduler(
            ws_client=(
                comm_client if "websocket" in args_dict["app_bridges"] else None
            ),
            inventory_db_path=inventory_db,
            device_state_db_path=str(args_dict.get("edge_device_state_db") or ""),
            workflow_history_db_path=str(
                args_dict.get("edge_workflow_history_db") or ""
            ),
        )
        # backend 是 bridge 形状(publish_job_status)，注册进 HostNode.bridges 收执行回报
        args_dict["bridges"].append(edge_exec_backend)
        print_status(
            "Edge 调度微后端已启用 (DAG 调度 + 设备状态 + 工作流历史)",
            "info",
        )

        # Host/Slave 连接、心跳和 ROS 组网配置由 Edge 微后端拥有。
        # HostNode 稍后创建时只向已启动的服务挂接运行时资源树。
        from unilabos.app.scheduler.host_network import setup_host_network_service
        from unilabos.config.config import HostLinkConfig

        host_network = setup_host_network_service()
        if host_network is not None:
            print_status(
                f"Edge 微后端已监听 Slave 连接: "
                f"{HostLinkConfig.bind}:{host_network.server.port}",
                "info",
            )
    else:
        print_status("SlaveMode跳过Websocket连接")
        from unilabos.app.scheduler.host_network import setup_slave_network_client

        setup_slave_network_client(device_ids=slave_device_ids)

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
                    print_status(
                        f"ROS 2环境未正确设置，跳过3D可视化启动。错误详情: {e}",
                        "warning",
                    )
                    print_status(
                        "建议解决方案：\n"
                        "1. 激活Conda环境: conda activate unilab\n"
                        "2. 或使用 --visual disable 参数禁用可视化",
                        "info",
                    )
                else:
                    raise
            while True:
                time.sleep(1)
        else:
            start_backend(**args_dict)
            restart_requested = start_server(
                open_browser=not BasicConfig.disable_browser,
                port=BasicConfig.port,
            )
            if restart_requested:
                print_status("[Main] Restart requested, cleaning up...", "info")
                cleanup_for_restart()
                return
    else:
        start_backend(**args_dict)

        # 启动服务器（默认支持WebSocket触发重启）
        restart_requested = start_server(
            open_browser=not BasicConfig.disable_browser,
            port=BasicConfig.port,
        )
        if restart_requested:
            print_status("[Main] Restart requested, cleaning up...", "info")
            cleanup_for_restart()
            os._exit(RESTART_EXIT_CODE)


if __name__ == "__main__":
    main()
