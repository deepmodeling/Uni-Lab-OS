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
from typing import Dict, Any, List

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

from unilabos.app.utils import cleanup_for_restart
from unilabos.utils.banner_print import print_status, print_unilab_banner
from unilabos.config.config import load_config, BasicConfig, HTTPConfig, SimGatewayConfig

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


def convert_argv_dashes_to_underscores(args: argparse.ArgumentParser):
    # easier for user input, easier for dev search code
    option_strings = list(args._option_string_actions.keys())
    for i, arg in enumerate(sys.argv):
        for option_string in option_strings:
            if arg.startswith(option_string):
                new_arg = arg[:2] + arg[2 : len(option_string)].replace("-", "_") + arg[len(option_string) :]
                sys.argv[i] = new_arg
                break


def build_argparser():
    """解析命令行参数"""
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
        choices=["ros", "simple", "automancer"],
        default="ros",
        help="Choose the backend to run with: 'ros', 'simple', or 'automancer'.",
    )
    parser.add_argument(
        "--app_bridges",
        nargs="+",
        default=["websocket", "fastapi"],
        help="Bridges to connect to. Now support 'websocket' and 'fastapi'.",
    )
    parser.add_argument(
        "--is_slave",
        action="store_true",
        help="Run the backend as slave node (without host privileges).",
    )
    parser.add_argument(
        "--slave_no_host",
        action="store_true",
        help="Skip waiting for host service in slave mode",
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
    parser.add_argument(
        "--mode",
        choices=["real", "sim", "twin"],
        default="real",
        help="Runtime mode: real hardware, full simulation, or one-way digital twin.",
    )
    parser.add_argument(
        "--sim_engine",
        type=str,
        default="none",
        help="Unified simulation engine used to pick the virtual driver in sim/twin mode "
        "(none / isaac / gazebo / genesis / matterix / custom). Plan 08 v2.",
    )
    parser.add_argument(
        "--sim_rate",
        type=float,
        default=1.0,
        help="Simulation acceleration ratio. Only sim mode can run faster than real time.",
    )
    parser.add_argument(
        "--sim_paused",
        action="store_true",
        default=False,
        help="Start the simulation clock paused.",
    )
    parser.add_argument(
        "--disable_sim_services",
        action="store_true",
        default=False,
        help="Do not auto-start /clock publisher and sim clock control ROS services.",
    )
    parser.add_argument(
        "--physics",
        choices=["none", "fake", "isaac"],
        default="none",
        help="Physics backend for sim mode: none, fake in-process backend, or Isaac HTTP bridge.",
    )
    parser.add_argument(
        "--physics_endpoint",
        type=str,
        default=None,
        help="Physics backend endpoint, required for --physics isaac.",
    )
    parser.add_argument(
        "--physics_scene",
        type=str,
        default=None,
        help="Scene path to load into the selected physics backend during startup.",
    )
    parser.add_argument(
        "--scene",
        type=str,
        default=None,
        help="Path to a lab architecture scene JSON ({nodes, rootNodeIds}); overrides the scene "
        "carried in the startup download. Walls/slabs are merged into full_dev for MoveIt collision.",
    )
    parser.add_argument(
        "--physics_timeout",
        type=float,
        default=120.0,
        help="Physics backend RPC timeout in seconds.",
    )
    parser.add_argument(
        "--disable_query_api",
        action="store_true",
        default=False,
        help="Do not auto-start the Robo-UniLabOS query API (ROS2 /unilabos/query + gRPC).",
    )
    parser.add_argument(
        "--query_grpc_port",
        type=int,
        default=50051,
        help="gRPC port for the query API (0 disables gRPC; ROS2 service still starts).",
    )
    parser.add_argument(
        "--query_labutopia_assets",
        type=str,
        default=None,
        help="Directory of LabUtopia asset cards (*.json) to serve as a query scene source.",
    )
    parser.add_argument(
        "--query_labutopia_config",
        type=str,
        default=None,
        help="Directory of LabUtopia task config (*.yaml) to serve action schemas / affordances.",
    )
    parser.add_argument(
        "--query_labutopia_usd",
        type=str,
        default=None,
        help="Path to a LabUtopia USD stage for precise per-prim poses (requires pxr/usd-core).",
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


def parse_args():
    return build_argparser()


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


def _can_start_without_cloud_auth(args_dict: Dict[str, Any], graph_file_path: str | None) -> bool:
    if graph_file_path is None:
        return False
    if args_dict.get("use_remote_resource", False):
        return False
    return "websocket" not in (args_dict.get("app_bridges") or [])


def main():
    """主函数"""
    # 解析命令行参数
    parser = parse_args()
    convert_argv_dashes_to_underscores(parser)
    args = parser.parse_args()
    args_dict = vars(args)

    # 处理 HTTP 客户端子命令（login, logout, whoami, config, lab, material, workflow）
    # 这些命令不需要加载完整的 UniLab-OS 环境，提前处理并退出
    http_client_commands = ["login", "logout", "whoami", "config", "lab", "material", "workflow"]
    if args_dict.get("command") in http_client_commands:
        from unilabos.client import (
            SessionManager,
            set_output_format,
            OutputFormat,
            print_error,
            print_output,
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
            print_status(f"未指定config路径，可通过 --config 传入 local_config.py 文件路径", "info")
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

    workflow_upload = args_dict.get("command") in ("workflow_upload", "wf")

    # 使用远程资源启动
    if not workflow_upload and args_dict["use_remote_resource"]:
        print_status("使用远程资源启动", "info")
        from unilabos.app.web import http_client

        res = http_client.resource_get("host_node", False)
        if str(res.get("code", 0)) == "0" and len(res.get("data", [])) > 0:
            print_status("远程资源已存在，使用云端物料！", "info")
            args_dict["graph"] = None
        else:
            print_status("远程资源不存在，本地将进行首次上报！", "info")

    BasicConfig.port = args_dict["port"] if args_dict["port"] else BasicConfig.port
    BasicConfig.disable_browser = args_dict["disable_browser"] or BasicConfig.disable_browser
    BasicConfig.is_host_mode = not args_dict.get("is_slave", False)
    BasicConfig.slave_no_host = args_dict.get("slave_no_host", False)
    BasicConfig.upload_registry = args_dict.get("upload_registry", False)
    BasicConfig.no_update_feedback = args_dict.get("no_update_feedback", False)
    BasicConfig.test_mode = args_dict.get("test_mode", False)
    if BasicConfig.test_mode:
        print_status("启用测试模式：所有动作将模拟执行，不调用真实硬件", "warning")
    BasicConfig.extra_resource = args_dict.get("extra_resource", False)
    if BasicConfig.extra_resource:
        print_status("启用额外资源加载：将加载lab_开头的labware资源定义", "info")
    BasicConfig.communication_protocol = "websocket"
    machine_name = platform.node()
    machine_name = "".join([c if c.isalnum() or c == "_" else "_" for c in machine_name])
    BasicConfig.machine_name = machine_name
    BasicConfig.vis_2d_enable = args_dict["2d_vis"]
    BasicConfig.check_mode = check_mode

    from unilabos.registry.registry import build_registry

    # 显示启动横幅
    print_unilab_banner(args_dict)

    # Step -1: 预读取 graph 中的 community.* class，并在 build_registry 前挂载社区设备包
    if not check_mode and not workflow_upload:
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

    # Plan 09 Task 5: 发现外源包的 unilabos_registry/ 目录,并入 build_registry。
    # 来源:device dirs(社区包 + 显式 --devices)各自的包根 + `unilabos.registry` entry points。
    try:
        from pathlib import Path as _Path

        from unilabos.registry.external_registry_discovery import (
            discover_registry_paths_from_entry_points,
            discover_registry_paths_from_project,
        )

        _ext: list = []
        for _d in args_dict.get("devices") or []:
            _dp = _Path(_d)
            _ext.extend(discover_registry_paths_from_project(_dp))
            _ext.extend(discover_registry_paths_from_project(_dp.parent))
        _ext.extend(discover_registry_paths_from_entry_points())
        _ext_str = list(dict.fromkeys(str(p) for p in _ext))
        if _ext_str:
            args_dict["_external_registry_paths"] = _ext_str
            print_status(f"发现 {len(_ext_str)} 个外源 registry 目录", "info")
    except Exception as _ext_exc:  # noqa: BLE001
        logger.warning(f"[ext-registry] 外源 registry 发现跳过: {_ext_exc}")

    # Step 0: AST 分析优先 + YAML 注册表加载
    # check_mode 和 upload_registry 都会执行实际 import 验证
    devices_dirs = args_dict.get("devices", None)
    complete_registry = args_dict.get("complete_registry", False) or check_mode
    external_only = args_dict.get("external_devices_only", False)
    lab_registry = build_registry(
        registry_paths=args_dict["registry_path"],
        devices_dirs=devices_dirs,
        community_namespaces=args_dict.get("_community_namespaces"),
        upload_registry=BasicConfig.upload_registry,
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
    from unilabos.app.web import start_server
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

    workflow_upload = args_dict.get("command") in ("workflow_upload", "wf")

    if workflow_upload:
        handle_workflow_upload_command(args_dict)
        print_status("工作流上传完成，程序退出", "info")
        os._exit(0)

    # 使用远程资源启动
    if not workflow_upload and args_dict.get("use_remote_resource"):
        print_status("后续运行必须拥有一个实验室，请前往 https://leap-lab.bohrium.com 注册实验室！", "warning")
        os._exit(1)

    import networkx as nx
    import yaml

    graph: nx.Graph
    resource_tree_set: ResourceTreeSet
    resource_links: List[Dict[str, Any]]
    file_path = args_dict.get("_graph_file_path")
    if file_path is None:
        file_path = _resolve_graph_file_path(args_dict.get("graph") or BasicConfig.startup_json_path)
    can_start_without_auth = _can_start_without_cloud_auth(args_dict, file_path)
    if not BasicConfig.ak or not BasicConfig.sk:
        if not can_start_without_auth:
            print_status("后续运行必须拥有一个实验室，请前往 https://leap-lab.bohrium.com 注册实验室！", "warning")
            os._exit(1)
        print_status("未提供 ak/sk，使用本地 graph 和非 websocket bridge 进入离线启动模式", "warning")

    request_startup_json = args_dict.get("_startup_json")
    if request_startup_json is None and BasicConfig.ak and BasicConfig.sk:
        request_startup_json = http_client.request_startup_json()

    # 实验室建筑场景：--scene 本地文件优先，否则取启动下载里携带的 scene 字段
    # （方案 A，后端未提供时为 None，优雅降级，不影响设备装配）
    scene_json = None
    scene_file = args_dict.get("scene")
    if scene_file:
        if os.path.exists(scene_file):
            try:
                with open(scene_file, encoding="utf-8") as f:
                    scene_json = json.load(f)
                print_status(f"已从本地文件加载实验室建筑场景: {scene_file}", "info")
            except (OSError, json.JSONDecodeError) as e:
                print_status(f"加载实验室建筑场景文件失败 {scene_file}: {e}", "warning")
        else:
            print_status(f"实验室建筑场景文件不存在: {scene_file}", "warning")
    elif isinstance(request_startup_json, dict):
        scene_json = request_startup_json.get("scene")
    args_dict["scene_json"] = scene_json

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

    # Phase 1B (08): sim/twin 模式下,从设备广场 resolve 仿真配对 bundle,
    # 生成运行时 device_pair.yaml 并指向 PairRegistry。Guarded:任何失败不阻断启动。
    if args_dict.get("mode") in ("sim", "twin"):
        try:
            from unilabos.app.web import http_client as _sim_http_client
            from unilabos.sim.pairs.download import make_downloader
            from unilabos.sim.pairs.edge_setup import setup_simulation_pairs

            setup_simulation_pairs(
                graph=graph,
                mode=args_dict.get("mode"),
                http_client=_sim_http_client,
                cache_dir=BasicConfig.working_dir,
                engine=args_dict.get("sim_engine", "none"),
                downloader=make_downloader(_sim_http_client, working_dir=BasicConfig.working_dir),
                device_registry=getattr(lab_registry, "device_type_registry", None),
            )
        except Exception as _sim_pair_exc:  # noqa: BLE001
            logger.warning(f"[sim-pair] 仿真配对 resolve 跳过(用默认 device_pair.yaml): {_sim_pair_exc}")

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

    if "fastapi" in args_dict["app_bridges"]:
        args_dict["bridges"].append(http_client)
    # 获取通信客户端（仅支持WebSocket）
    isaac_gateway = None
    if BasicConfig.is_host_mode:
        comm_client = get_communication_client()
        if "websocket" in args_dict["app_bridges"]:
            args_dict["bridges"].append(comm_client)

            def _exit(signum, frame):
                comm_client.stop()
                if isaac_gateway is not None:
                    isaac_gateway.stop()
                sys.exit(0)

            signal.signal(signal.SIGINT, _exit)
            signal.signal(signal.SIGTERM, _exit)
            comm_client.start()
    else:
        print_status("SlaveMode跳过Websocket连接")

    if SimGatewayConfig.enabled:
        try:
            from unilabos.sim.isaac_gateway import IsaacSimGateway

            isaac_gateway = IsaacSimGateway.from_config()
            isaac_gateway.start()

            def _default_collision_handler(payload):
                # 默认安全处理：结构化告警日志；可后续替换为停机/审计逻辑
                print_status(f"[IsaacSim] 碰撞事件: {payload.get('pairs') or payload}", "warning")

            isaac_gateway.add_collision_handler(_default_collision_handler)
            print_status(f"IsaacSimGateway 已启动: {SimGatewayConfig.endpoint}", "info")
            # visual != disable 时改用 ResourceVisualization 的整场景 URDF（见下方 RV 构建后），
            # 避免给设备发占位 URI；仅在无可视化（无 RV）时走逐资源 sync 兜底。
            if args_dict["visual"] == "disable":
                try:
                    synced = isaac_gateway.sync_from_resource_tree_set(args_dict["resources_config"])
                    print_status(f"IsaacSimGateway 资源同步完成，已发送 {synced} 条 asset.upsert", "info")
                except Exception as sync_err:
                    print_status(f"IsaacSimGateway 资源同步失败: {sync_err}", "warning")
        except Exception as e:
            print_status(f"IsaacSimGateway 启动失败: {e}", "warning")

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
                scene_json=args_dict.get("scene_json"),
            )
            args_dict["resources_mesh_config"] = resource_visualization.resource_model
            # 把整场景展开后的 URDF 作为单个 full_dev 设备发给 Isaac Sim
            if isaac_gateway is not None:
                try:
                    isaac_gateway.upsert_scene_urdf(resource_visualization.urdf_str)
                    print_status("IsaacSimGateway 已发送整场景 scene.urdf", "info")
                except Exception as scene_err:
                    print_status(f"IsaacSimGateway 场景URDF发送失败: {scene_err}", "warning")
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
                    print_status(f"ROS 2环境未正确设置，跳过3D可视化启动。错误详情: {e}", "warning")
                    print_status(
                        "建议解决方案：\n"
                        "1. 激活Conda环境: conda activate unilab\n"
                        "2. 或使用 --backend simple 参数\n"
                        "3. 或使用 --visual disable 参数禁用可视化",
                        "info",
                    )
                else:
                    raise
            while True:
                time.sleep(1)
        else:
            start_backend(**args_dict)
            restart_requested = start_server(
                open_browser=not args_dict["disable_browser"],
                port=BasicConfig.port,
            )
            if restart_requested:
                print_status("[Main] Restart requested, cleaning up...", "info")
                if isaac_gateway is not None:
                    isaac_gateway.stop()
                cleanup_for_restart()
                return
    else:
        start_backend(**args_dict)

        # 启动服务器（默认支持WebSocket触发重启）
        restart_requested = start_server(
            open_browser=not args_dict["disable_browser"],
            port=BasicConfig.port,
        )
        if restart_requested:
            print_status("[Main] Restart requested, cleaning up...", "info")
            if isaac_gateway is not None:
                isaac_gateway.stop()
            cleanup_for_restart()
            os._exit(RESTART_EXIT_CODE)


if __name__ == "__main__":
    main()
