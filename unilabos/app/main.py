import argparse
import faulthandler
import os
import platform
import shutil
import sys
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
)
from unilabos.app.workspace_package_bootstrap import (
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


def should_attach_legacy_http_bridge(args_dict: Dict[str, Any]) -> bool:
    """判断是否挂接旧云端 HTTP 桥。

    参数：``args_dict`` 是规范化启动参数。返回：始终为 ``False``；旧云端桥已随
    Backend 控制面一起移除。异常：无。状态不变量：OS 的 HTTP 服务只作为前端
    入站接口，不向 Backend 发起物料或工作流请求。
    """

    return False


def should_request_remote_startup(
    *,
    startup_json: Optional[Dict[str, Any]],
    graph_file_path: Optional[str],
    use_remote_resource: bool = False,
) -> bool:
    """判断是否请求远端启动图。

    参数：保留历史调用签名以便上层逐步删除旧参数。返回：始终为 ``False``；
    OS 现在必须从本地图或领域包加载资源图，不再向 Backend 请求启动数据。
    异常：无。状态不变量：本地控制面是唯一资源图来源。
    """

    return False


def should_prepare_workspace_product_runtime(args_dict: dict[str, Any]) -> bool:
    """判断当前命令是否属于需要工作区产品运行时的常驻启动。

    参数：``args_dict`` 是公共命令行（CLI）参数。
    返回：软件包管理命令返回 ``False``，普通常驻启动返回 ``True``；软件包查询、
    上传和依赖增改删不得预读物理图（Graph）或现有依赖锁并启动监视线程。
    异常：无。
    """

    return args_dict.get("command") not in {"package", "pkg", "workspace"}


def dispatch_workspace_host_command(args_dict: dict[str, Any]) -> bool:
    """Dispatch the AI-native Workspace Host CLI before product composition.

    The command is a client adapter only: it never imports a laboratory package,
    creates a scheduler, or owns a component process. The persistent Workspace
    Host is the single lifecycle authority shared with Workbench.
    """

    if args_dict.get("command") != "workspace":
        return False
    from unilabos.workspace_host.cli import dispatch_workspace_command

    return dispatch_workspace_command(args_dict)


def dispatch_workflow_domain_command(args_dict: dict[str, Any]) -> bool:
    """Dispatch Agent-safe Workflow Domain commands before OS composition."""

    if args_dict.get("command") != "workflow":
        return False
    from unilabos.app.cli.workflow import dispatch_workflow_domain_command as dispatch

    return dispatch(args_dict)


def dispatch_material_renderer_command(args_dict: dict[str, Any]) -> bool:
    """Dispatch attached Material renderer commands before OS composition."""

    if args_dict.get("command") != "material":
        return False
    from unilabos.app.cli.material import dispatch_material_scene_command

    return dispatch_material_scene_command(args_dict)


def dispatch_skills_command(args_dict: dict[str, Any]) -> bool:
    """在产品启动前处理项目级 Skill 管理命令。"""
    if args_dict.get("command") != "skills":
        return False
    from unilabos.app.cli.skills import run_skills_command

    run_skills_command(args_dict)
    return True


def _add_legacy_startup_mode_compatibility(parser: argparse.ArgumentParser) -> None:
    """兼容仍在运行的旧 Workspace Host 启动参数，但不恢复旧模式语义。

    旧 Host 进程可能在切换代码版本后继续向新 Backend/Edge 传入
    ``--run_mode develop/product``。这个隐藏参数只让滚动升级期间的旧进程
    正常启动；解析结果不会参与能力策略、工作流可见性或任务运行判断。
    """

    parser.add_argument(
        "--run_mode",
        dest="_legacy_startup_mode",
        choices=["develop", "product"],
        default=None,
        help=argparse.SUPPRESS,
    )


def dispatch_local_package_command(args_dict: dict[str, Any]) -> bool:
    """在产品启动前分派不依赖远端配置的包命令。

    参数：``args_dict`` 是公共命令行（CLI）解析出的完整参数字典。
    返回：inspect、build、add、update、remove、assets 由包管理深模块处理时返回 ``True``；
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
        "assets",
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
    定义合同，并保留全局启动参数与工作区命令的统一解析入口。
    异常：无；参数错误只会在调用者后续执行 ``parse_args`` 时产生 ``SystemExit``。
    """
    parser = argparse.ArgumentParser(description="Start Uni-Lab Edge server.")
    parser.add_argument("--database_type", "--database-type", default=None, choices=("sqlite", "postgresql"), help="数据库类型；未配置时使用 SQLite")
    parser.add_argument("--database_url", "--database-url", default=None, help="PostgreSQL 连接地址")
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
        "--control_plane",
        choices=["local"],
        default="local",
        help=(
            "控制面固定为 local；Backend 上游模式已移除。"
        ),
    )
    _add_legacy_startup_mode_compatibility(parser)
    parser.add_argument(
        "--process_role",
        choices=["workspace_backend", "edge_runtime"],
        default="workspace_backend",
        help=(
            "进程职责：workspace_backend 常驻提供 "
            "工作流创作、库存和工站调度；edge_runtime 只承载动作执行运行时。"
            "完整工站请使用 unilab workspace start 启动。"
        ),
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
        choices=["edge_control", "fastapi"],
        default=["fastapi"],
        help=(
            "连接桥：edge_control 是动作进程与本地工站调度器的协议，"
            "fastapi 提供入站 HTTP 服务。"
        ),
    )
    parser.add_argument(
        "--ros_domain_id",
        type=int,
        default=None,
        help="ROS_DOMAIN_ID for this device runtime process.",
    )
    parser.add_argument(
        "--ros_discovery_server",
        type=str,
        default=None,
        help="Use an external Fast DDS discovery server (ip:port), or 'off'.",
    )
    parser.add_argument(
        "--use_remote_resource",
        action="store_true",
        help=argparse.SUPPRESS,
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
    instance_sync_parser.add_argument(
        "--devices_only",
        dest="instance_devices_only",
        action="store_true",
        help="Initialize only device nodes used by production Edge registration",
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

    # ROS 发布、订阅与假设备诊断。
    doctor_parser = subparsers.add_parser(
        "doctor",
        help="ROS diagnostics: talker/listener pair, fake device",
    )
    doctor_sub = doctor_parser.add_subparsers(
        title="doctor subcommands", dest="doctor_command"
    )

    def _add_doctor_common(sub_parser):
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

    # AIW-02: the CLI and Workbench share one per-workspace lifecycle authority.
    from unilabos.workspace_host.cli import register_workspace_subcommands

    register_workspace_subcommands(subparsers)

    from unilabos.app.cli.skills import register_skills_subcommands

    register_skills_subcommands(subparsers)

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

    from unilabos.app.cli.material import register_material_scene_subcommands

    register_material_scene_subcommands(material_grp_subparsers)

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

    from unilabos.app.cli.workflow import register_workflow_domain_subcommands

    register_workflow_domain_subcommands(workflow_grp_subparsers)

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

    if args_dict.get("backend") == "ros":
        # rcutils 可能在注册表导入设备类型时提前初始化，须在此之前指定 fd2。
        os.environ["RCUTILS_LOGGING_USE_STDOUT"] = "0"

    # 控制面已收敛为本地模式。即使调用方绕过命令包装器直接传入参数，
    # 这里也再次校验，避免未来新增入口时意外恢复 Backend 出站连接。
    if args_dict.get("control_plane") != "local":
        parser.error("当前 OS 仅支持 local 控制面，不再支持 backend 模式")
    if args_dict.get("use_remote_resource"):
        parser.error("当前 OS 只支持本地资源，不再支持远程 Backend 资源")
    args_dict["control_plane"] = "local"

    # 先分派可独立执行的子命令，再校验运行拓扑和装配设备或软件包。
    if dispatch_workspace_host_command(args_dict):
        return
    if dispatch_workflow_domain_command(args_dict):
        return
    if dispatch_material_renderer_command(args_dict):
        return
    if dispatch_skills_command(args_dict):
        return

    from unilabos.app.runtime_topology import resolve_runtime_process_plan

    try:
        runtime_process_plan = resolve_runtime_process_plan(args_dict)
    except ValueError as error:
        parser.error(str(error))
    control_plane_mode = runtime_process_plan.control_plane

    # doctor 子命令：组网诊断，不加载完整环境（net 甚至不 import rclpy），提前处理并退出
    if args_dict.get("command") == "doctor":
        from unilabos.networking.ros_doctor import run_doctor

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
        from unilabos.app.cli.auth import cmd_login, cmd_logout, cmd_whoami
        from unilabos.app.cli.config import cmd_config_show
        from unilabos.app.cli.lab import cmd_lab_list
        from unilabos.app.cli.material import cmd_material_list
        from unilabos.app.cli.workflow import cmd_workflow_upload
        from unilabos.client import (
            OutputFormat,
            SessionManager,
            print_error,
            resolve_addr,
            set_output_format,
        )

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
        AuthoringWorkerError,
        PackageCompileError,
        PackageDependencyError,
        WorkspaceGenerationChangedError,
        prepare_stable_workspace_product_generation_in_worker,
    )

    try:
        prepared_workspace_generation = (
            prepare_stable_workspace_product_generation_in_worker(args_dict)
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
        AuthoringWorkerError,
        TypeError,
        ValueError,
        WorkspaceGenerationChangedError,
    ) as error:
        parser.error(str(error))
    if workspace_registry_runtime is not None:
        print_status(
            "已编译工作区（Workspace）注册表运行时: "
            f"{workspace_registry_runtime.catalog.import_package} "
            f"(Authoring Worker PID "
            f"{prepared_workspace_generation.authoring_worker_pid})",
            "info",
        )

    # 环境检查 - 检查并自动安装必需的包 (可选)
    # ``ensure_dependencies`` 只来自已验证工作区计划；产品不再保留重复命令行入口。
    ensure_dependencies = args_dict.get("_ensure_dependencies", True)
    check_mode = args_dict.get("check_mode", False)

    if ensure_dependencies:
        from unilabos.utils.environment_check import (
            check_device_package_requirements,
            check_environment,
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
    from unilabos.persistence.config import (
        resolve_database_settings,
        verify_database_connection,
    )

    database_settings = resolve_database_settings(args_dict)
    if not check_mode:
        verify_database_connection(database_settings)
    # 根据配置重新设置日志级别
    from unilabos.utils.log import configure_comm_logger, configure_logger, logger
    from unilabos.utils.log_storage import LogPolicy

    log_policy = LogPolicy.from_config(BasicConfig)
    file_path = configure_logger(
        loglevel=BasicConfig.log_level, working_dir=working_dir, policy=log_policy,
        file_log_level=BasicConfig.file_log_level, log_detailed=BasicConfig.log_detailed,
    )
    if file_path is not None:
        logger.info(f"[LOG_FILE] {file_path}")

    # 为服务端通信(WebSocket)配置独立日志，避免与主日志混在一起，便于排查通信机制
    comm_log_path = configure_comm_logger(
        loglevel=BasicConfig.log_level, working_dir=working_dir, policy=log_policy,
        file_log_level=BasicConfig.file_log_level, log_detailed=BasicConfig.log_detailed,
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
                workspace_runtime=workspace_registry_runtime,
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

    BasicConfig.port = args_dict["port"] if args_dict["port"] else BasicConfig.port
    BasicConfig.control_plane = control_plane_mode.value
    BasicConfig.process_role = runtime_process_plan.role.value
    print_status(
        "Workspace Backend 拥有本地工站调度" if runtime_process_plan.role.value == "workspace_backend"
        else "Edge Runtime 加载设备并连接工作区后端",
        "info",
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
    BasicConfig.action_mode = args_dict.get("action_mode", "real")
    if BasicConfig.action_mode == "simulate":
        print_status(
            "启用模拟动作模式：动作返回模拟成功结果，不调用真实硬件",
            "warning",
        )
    BasicConfig.extra_resource = args_dict.get("extra_resource", False)
    if BasicConfig.extra_resource:
        print_status("启用额外资源加载：将加载lab_开头的labware资源定义", "info")
    # 本地控制面不创建遗留云端 WebSocket；Edge Runtime 只通过显式
    # ``edge_control`` 桥连接同一工作区的 Scheduler。
    BasicConfig.communication_protocol = "local"
    ros_discovery_server = args_dict.get("ros_discovery_server")
    if ros_discovery_server is not None:
        endpoint = str(ros_discovery_server).strip()
        if endpoint.lower() in {"off", "none", "disabled"}:
            os.environ.pop("ROS_DISCOVERY_SERVER", None)
        else:
            os.environ["ROS_DISCOVERY_SERVER"] = endpoint
    ros_domain_id = args_dict.get("ros_domain_id")
    if ros_domain_id is not None:
        os.environ["ROS_DOMAIN_ID"] = str(ros_domain_id)
        print_status(f"ROS_DOMAIN_ID = {ros_domain_id}", "info")
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
    # Workspace Backend 不启动嵌入式 Inventory，但仍需以同一编译代发布前端 3D
    # 场景依赖的只读模型资产；这里只保存不可变目录，不引入第二套资源权威。
    BasicConfig.workspace_material_shapes = tuple(workspace_material_shapes)
    BasicConfig.workspace_material_model_catalog = workspace_material_models

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
    from unilabos.app.backend import start_backend
    from unilabos.app.web import http_client, start_server
    from unilabos.resources.graphio import (
        modify_to_backend_format,
        read_graphml,
        read_node_link_json,
    )
    from unilabos.resources.resource_tracker import ResourceDict, ResourceTreeSet

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

    if args_dict["controllers"] is not None:
        args_dict["controllers_config"] = yaml.safe_load(
            open(args_dict["controllers"], encoding="utf-8")
        )
    else:
        args_dict["controllers_config"] = None

    args_dict["bridges"] = []

    if should_attach_legacy_http_bridge(args_dict):
        args_dict["bridges"].append(http_client)
    if runtime_process_plan.role.value == "edge_runtime":
        from unilabos.app.control_plane import ControlPlaneRuntimeContext
        from unilabos.app.edge_control.runtime import start_backend_control_runtime

        edge_control_runtime = start_backend_control_runtime(
            ControlPlaneRuntimeContext(
                arguments=args_dict,
                working_dir=working_dir,
                resource_tree_set=resource_tree_set,
                registry=lab_registry,
                graph_source_id=str(file_path or "remote-startup.json"),
                material_shapes=workspace_material_shapes,
                material_model_catalog=workspace_material_models,
            )
        )
        args_dict["bridges"].extend(edge_control_runtime.bridges)
        install_host_shutdown_handlers(
            edge_control_runtime.communication_clients,
            runtime_shutdown=edge_control_runtime.shutdown_services,
        )
        print_status(
            "Edge Runtime 使用生产形态 HTTP/WebSocket 协议连接控制面",
            "info",
        )
    else:
        # 工站后端独立持有库存和调度权威，不随设备运行时退出。
        from unilabos.app.control_plane import (
            ControlPlaneRuntimeContext,
            start_control_plane_runtime,
        )

        control_plane_runtime = start_control_plane_runtime(
            ControlPlaneRuntimeContext(
                arguments=args_dict,
                working_dir=working_dir,
                resource_tree_set=resource_tree_set,
                registry=lab_registry,
                graph_source_id=str(file_path or "remote-startup.json"),
                material_shapes=workspace_material_shapes,
                material_model_catalog=workspace_material_models,
            ),
        )
        args_dict["bridges"].extend(control_plane_runtime.bridges)
        install_host_shutdown_handlers(
            control_plane_runtime.communication_clients,
            runtime_shutdown=control_plane_runtime.shutdown_services,
        )
    args_dict["resources_mesh_config"] = {}
    args_dict["resources_edge_config"] = resource_edge_info
    if not runtime_process_plan.starts_web_server:
        print_status(
            "Edge Runtime 已与 Workspace Backend 分离；当前进程不启动 HTTP/Authoring 服务",
            "info",
        )
        edge_backend_thread = start_backend(**args_dict)
        edge_backend_thread.join()
        from unilabos.app.runtime_thread_lifecycle import (
            fail_runtime_thread,
            runtime_shutdown_requested,
        )

        if runtime_shutdown_requested():
            return
        fail_runtime_thread(edge_backend_thread.name)

    if runtime_process_plan.role.value == "workspace_backend":
        print_status(
            "Workspace Backend 常驻提供 Authoring/Inventory/Scheduler；不启动 ROS 或设备驱动",
            "info",
        )
        restart_requested = start_server(
            open_browser=not BasicConfig.disable_browser,
            port=BasicConfig.port,
        )
        if restart_requested:
            print_status("[Main] Restart requested, cleaning up...", "info")
            cleanup_for_restart()
            os._exit(RESTART_EXIT_CODE)
        return


if __name__ == "__main__":
    main()
