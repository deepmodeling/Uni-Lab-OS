"""顶层 CLI 子命令的注册与分发。"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from .package import (
    register_package_commands,
    run_package_command,
)

CLIENT_COMMANDS = frozenset(
    {"login", "logout", "whoami", "config", "material", "workflow", "graph"}
)
PACKAGE_COMMANDS = frozenset({"package", "pkg"})
RUNTIME_COMMANDS = frozenset({"runtime"})
#: 现场组网诊断：不读会话、不建工作目录，也不启动 runtime（net 层连 rclpy 都不导入）。
DOCTOR_COMMANDS = frozenset({"doctor"})


def _add_client_connection_options(
    command_parser: argparse.ArgumentParser,
    *,
    include_auth: bool = False,
    jsonl: bool = False,
) -> None:
    """允许统一连接参数位于子命令之后。"""

    command_parser.add_argument(
        "--address",
        "--addr",
        dest="address",
        default=argparse.SUPPRESS,
        help="Backend or microbackend address; --addr is an alias.",
    )
    if include_auth:
        command_parser.add_argument("--ak", default=argparse.SUPPRESS)
        command_parser.add_argument("--sk", default=argparse.SUPPRESS)
    command_parser.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Output in JSON format.",
    )
    if jsonl:
        command_parser.add_argument(
            "--jsonl",
            action="store_true",
            default=False,
            help="Emit one compact JSON object per item or task change.",
        )


def _register_workflow_authority_commands(workflow_actions: Any) -> None:
    workflow_list = workflow_actions.add_parser(
        "list",
        help="List workflow definitions",
    )
    workflow_list.add_argument("--page", type=int, default=1)
    workflow_list.add_argument(
        "--page_size",
        "--page-size",
        dest="page_size",
        type=int,
        default=100,
    )
    workflow_list.add_argument("--name", default="")
    _add_client_connection_options(
        workflow_list,
        include_auth=True,
        jsonl=True,
    )

    workflow_inspect = workflow_actions.add_parser(
        "inspect",
        help="Inspect a workflow, Task, or node Job",
    )
    workflow_inspect.add_argument("identity")
    workflow_inspect.add_argument(
        "--kind",
        choices=["workflow", "task", "job"],
        default="task",
    )
    _add_client_connection_options(workflow_inspect, include_auth=True)

    workflow_run = workflow_actions.add_parser(
        "run",
        help="Create a workflow Task",
    )
    workflow_run.add_argument("workflow_uuid")
    workflow_run.add_argument(
        "--mode",
        choices=["normal", "step", "single_node"],
        default="normal",
    )
    workflow_run.add_argument(
        "--target_node",
        "--target-node",
        dest="target_node",
        default=None,
    )
    workflow_run.add_argument(
        "--inputs",
        default="{}",
        help="Workflow entry input: a JSON object, or @path/to/file.json",
    )
    workflow_run.add_argument("--operation_id", "--operation-id", default=None)
    workflow_run.add_argument("--follow", action="store_true")
    workflow_run.add_argument("--after", type=int, default=0)
    workflow_run.add_argument("--timeout", type=float, default=300.0)
    workflow_run.add_argument(
        "--max_events",
        "--max-events",
        dest="max_events",
        type=int,
        default=500,
    )
    _add_client_connection_options(
        workflow_run,
        include_auth=True,
        jsonl=True,
    )

    workflow_watch = workflow_actions.add_parser(
        "watch",
        help="Watch a Task via WS invalidations and HTTP snapshots",
    )
    workflow_watch.add_argument("task_uuid")
    workflow_watch.add_argument("--after", type=int, default=0)
    workflow_watch.add_argument("--timeout", type=float, default=300.0)
    workflow_watch.add_argument(
        "--max_events",
        "--max-events",
        dest="max_events",
        type=int,
        default=500,
    )
    _add_client_connection_options(
        workflow_watch,
        include_auth=True,
        jsonl=True,
    )

    workflow_command = workflow_actions.add_parser(
        "command",
        help="Submit a Task control command (pause / resume / step / cancel) idempotently",
    )
    workflow_command.add_argument("task_uuid")
    workflow_command.add_argument(
        "type",
        # continue 是 V1 CLI 的 resume 别名
        choices=["pause", "resume", "continue", "step", "cancel"],
        help=(
            "pause closes the dispatch gate (running actions finish); resume reopens it or leaves step mode; "
            "step releases one node; cancel cancels the Task (continue = resume)"
        ),
    )
    workflow_command.add_argument(
        "--target_node",
        "--target-node",
        dest="target_node",
        default=None,
        help="Node to release (step only)",
    )
    workflow_command.add_argument(
        "--idempotency_key",
        "--idempotency-key",
        dest="idempotency_key",
        default=None,
        help="Reuse the key when retrying the same command (default: a new key)",
    )
    _add_client_connection_options(workflow_command, include_auth=True)

    workflow_authoring = workflow_actions.add_parser(
        "authoring",
        help="Wait for an Authoring revision or diagnostics",
    )
    workflow_authoring.add_argument("workflow_uuid")
    workflow_authoring.add_argument(
        "--after_revision",
        "--after-revision",
        dest="after_revision",
        type=int,
        required=True,
    )
    workflow_authoring.add_argument("--timeout", type=float, default=30.0)
    _add_client_connection_options(workflow_authoring, include_auth=True)


def _add_graph_backend_options(command_parser: argparse.ArgumentParser) -> None:
    """graph 子命令的后端选择：默认本机微后端，``--remote`` 切云端。"""

    command_parser.add_argument(
        "--remote",
        action="store_true",
        default=False,
        help="Target the cloud Graph Authority instead of the local microbackend.",
    )
    command_parser.add_argument(
        "--port_management",
        "--port-management",
        dest="port_management",
        type=int,
        default=None,
        help="Local microbackend management port (default: BasicConfig.port).",
    )
    _add_client_connection_options(command_parser, include_auth=True)


def _register_graph_commands(subparsers: Any) -> None:
    """``unilab graph``：设备图的 create/upload/list/get/download/delete。"""

    graph_parser = subparsers.add_parser("graph", help="Lab graph management")
    graph_actions = graph_parser.add_subparsers(
        title="graph subcommands", dest="graph_command"
    )

    graph_create = graph_actions.add_parser(
        "create",
        help="Scan @device packages and generate a node-link graph skeleton",
    )
    graph_create.add_argument(
        "--devices",
        nargs="+",
        default=None,
        help="Device package directories to scan.",
    )
    graph_create.add_argument(
        "--include",
        nargs="*",
        default=[],
        help="Only include these registry device keys.",
    )
    graph_create.add_argument(
        "-o",
        "--output",
        dest="output",
        default="graph.json",
        help="Output graph file path.",
    )

    graph_upload = graph_actions.add_parser(
        "upload", help="Upload (upsert) a graph file to the Graph Authority"
    )
    graph_upload.add_argument(
        "-f",
        "--graph_file",
        "--graph-file",
        dest="graph_file",
        required=True,
        help="Graph file (node-link JSON).",
    )
    graph_upload.add_argument(
        "-n",
        "--graph_name",
        "--graph-name",
        dest="graph_name",
        default=None,
        help="Graph name (default: file stem).",
    )
    graph_upload.add_argument(
        "--tags", nargs="*", default=[], help="Tags (space-separated)."
    )
    graph_upload.add_argument(
        "--description", default="", help="Graph description."
    )
    _add_graph_backend_options(graph_upload)

    graph_list = graph_actions.add_parser("list", help="List graphs")
    graph_list.add_argument("--page", type=int, default=1)
    graph_list.add_argument(
        "--page_size",
        "--page-size",
        dest="page_size",
        type=int,
        default=100,
    )
    graph_list.add_argument("--name", default="", help="Filter by name substring.")
    _add_graph_backend_options(graph_list)

    graph_get = graph_actions.add_parser(
        "get", help="Show one graph record (uuid or name)"
    )
    graph_get.add_argument("identity")
    _add_graph_backend_options(graph_get)

    graph_download = graph_actions.add_parser(
        "download", help="Download a graph payload to a file"
    )
    graph_download.add_argument("identity")
    graph_download.add_argument(
        "-o",
        "--output",
        dest="output",
        default=None,
        help="Output file (default: <identity>.json).",
    )
    _add_graph_backend_options(graph_download)

    graph_delete = graph_actions.add_parser(
        "delete", help="Soft-delete a graph (uuid or name)"
    )
    graph_delete.add_argument("identity")
    _add_graph_backend_options(graph_delete)


def _register_doctor_commands(subparsers: Any) -> None:
    """``unilab doctor``：Host / Slave 组网分层诊断（TCP 通路 → ROS 收发）。"""

    doctor_parser = subparsers.add_parser(
        "doctor",
        help="Network diagnostics: TCP probe, ROS talker/listener pair, fake device",
        description=(
            "逐层定位「设备不上线」卡在哪一层：net 只测 HostLink TCP 通路与握手；"
            "talker / listener 成对测 ROS 收发；fake-device 以设备身份发探测并检查 Host 服务可见性。"
            "不启动设备运行时。"
        ),
    )
    doctor_actions = doctor_parser.add_subparsers(
        title="doctor subcommands", dest="doctor_command", required=True
    )

    def add_common(command_parser: argparse.ArgumentParser) -> None:
        command_parser.add_argument(
            "--hostlink_addr",
            "--hostlink-addr",
            dest="hostlink_addr",
            default="",
            help=(
                "Host ip[:port] (default port 7302): TCP probe target; talker/listener/fake-device "
                "also fetch the ROS network settings through the HostLink handshake"
            ),
        )
        command_parser.add_argument(
            "--peer",
            default="",
            help=(
                "Peer ip list (comma separated) for unicast-only diagnosis "
                "(sets ROS_STATIC_PEERS; discovery defaults to OFF)"
            ),
        )
        # 顶层已有同名参数：这里不给默认值，免得覆盖 ``unilab --ros_domain_id N doctor ...``
        command_parser.add_argument(
            "--ros_domain_id",
            "--ros-domain-id",
            dest="ros_domain_id",
            type=int,
            default=argparse.SUPPRESS,
            help="ROS domain id override",
        )
        command_parser.add_argument(
            "--discovery",
            default="",
            choices=["", "OFF", "LOCALHOST", "SUBNET"],
            help="ROS_AUTOMATIC_DISCOVERY_RANGE (default: OFF when --peer is given)",
        )
        command_parser.add_argument("--topic", default="/unilab_doctor", help="Probe topic")
        command_parser.add_argument("--rate", type=float, default=1.0, help="Publish rate in Hz")
        command_parser.add_argument(
            "--duration",
            type=float,
            default=0.0,
            help="Seconds to run, 0 = until Ctrl-C",
        )

    doctor_net = doctor_actions.add_parser(
        "net", help="Layer 1: HostLink TCP channel probe (connect, hello, ping RTT; no ROS needed)"
    )
    add_common(doctor_net)
    doctor_net.add_argument("--count", type=int, default=5, help="Ping count")
    doctor_talker = doctor_actions.add_parser("talker", help="Layer 2: publish ROS probe messages")
    add_common(doctor_talker)
    doctor_listener = doctor_actions.add_parser(
        "listener", help="Layer 2: receive ROS probes, report loss/latency"
    )
    add_common(doctor_listener)
    doctor_listener.add_argument(
        "--quiet", action="store_true", help="Only print the final summary"
    )
    doctor_fake = doctor_actions.add_parser(
        "fake-device",
        help="Fake device: probe as a device and check host service visibility",
    )
    add_common(doctor_fake)
    doctor_fake.add_argument(
        "--device_id",
        "--device-id",
        dest="device_id",
        default="",
        help="Fake device id (default: random)",
    )
    doctor_fake.add_argument(
        "--no_service_check",
        "--no-service-check",
        dest="no_service_check",
        action="store_true",
        help="Skip the host registration service visibility check",
    )


def register_cli_commands(
    parser: argparse.ArgumentParser,
    subparsers: Any,
) -> None:
    """注册不参与设备 runtime bootstrap 的命令。"""

    register_package_commands(subparsers)
    from .runtime import register_runtime_commands

    register_runtime_commands(subparsers)
    _register_doctor_commands(subparsers)

    parser.add_argument(
        "--json",
        action="store_true",
        help="Output in JSON format (for AI agent consumption)",
    )

    login_parser = subparsers.add_parser("login", help="Save ak/sk to session file")
    login_parser.add_argument("--ak", type=str, required=True, help="Access key")
    login_parser.add_argument("--sk", type=str, required=True, help="Secret key")
    _add_client_connection_options(login_parser)
    logout_parser = subparsers.add_parser("logout", help="Clear local ak/sk")
    _add_client_connection_options(logout_parser)
    whoami_parser = subparsers.add_parser(
        "whoami",
        help="Show current user information",
    )
    _add_client_connection_options(whoami_parser)

    config_parser = subparsers.add_parser("config", help="Show session configuration")
    config_actions = config_parser.add_subparsers(
        title="config subcommands", dest="config_command"
    )
    config_show = config_actions.add_parser(
        "show",
        help="Show current session configuration",
    )
    _add_client_connection_options(config_show)

    material_parser = subparsers.add_parser("material", help="Material management")
    material_actions = material_parser.add_subparsers(
        title="material subcommands", dest="material_command"
    )
    material_list = material_actions.add_parser(
        "list", help="List material instances from the materials authority"
    )
    material_list.add_argument(
        "--roots_only",
        "--roots-only",
        action="store_true",
        default=False,
        help="Only return root material instances",
    )
    _add_client_connection_options(material_list)

    workflow_parser = subparsers.add_parser("workflow", help="Workflow management")
    workflow_actions = workflow_parser.add_subparsers(
        title="workflow subcommands", dest="workflow_command"
    )
    workflow_upload = workflow_actions.add_parser("upload", help="Upload workflow file")
    workflow_upload.add_argument(
        "-f",
        "--workflow_file",
        "--workflow-file",
        type=str,
        required=True,
        help="Workflow file (JSON)",
    )
    workflow_upload.add_argument(
        "-n",
        "--workflow_name",
        "--workflow-name",
        type=str,
        default=None,
        help="Workflow name",
    )
    workflow_upload.add_argument(
        "--tags", type=str, nargs="*", default=[], help="Tags (space-separated)"
    )
    workflow_upload.add_argument(
        "--published", action="store_true", default=False, help="Publish after upload"
    )
    workflow_upload.add_argument(
        "--description", type=str, default="", help="Workflow description"
    )
    _add_client_connection_options(workflow_upload, include_auth=True)
    _register_workflow_authority_commands(workflow_actions)
    _register_graph_commands(subparsers)


def _prepare_command_session(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
):
    from unilabos.client import SessionManager, resolve_address

    values = vars(args)
    from unilabos.config.working_dir import resolve_working_dir

    working_dir = resolve_working_dir(values.get("working_dir"), values.get("config"))
    from unilabos.config.config import BasicConfig

    # 与调度权威用同一个工作目录；同一解释器连续执行多个管理命令时不沿用上一次的目录。
    BasicConfig.working_dir = str(working_dir)

    address = values.get("address")
    args.address_resolved = resolve_address(address) if address else None
    # 同步第三方 CLI 扩展使用的属性名；内部代码读取 address_resolved。
    args.addr_resolved = args.address_resolved
    return SessionManager(working_dir=working_dir)


def run_client_command(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
    *,
    session_manager: Any = None,
) -> bool:
    """执行轻量 HTTP/会话命令；非此类命令返回 ``False``。"""

    values = vars(args)
    command = values.get("command")
    if command not in CLIENT_COMMANDS:
        return False
    from unilabos.app.cli.auth import cmd_login, cmd_logout, cmd_whoami
    from unilabos.app.cli.config import cmd_config_show
    from unilabos.app.cli.material import cmd_material_list
    from unilabos.app.cli.workflow import cmd_workflow_command, cmd_workflow_upload
    from unilabos.client import OutputFormat, print_error, set_output_format

    if values.get("json", False):
        set_output_format(OutputFormat.JSON)

    session_manager = session_manager or _prepare_command_session(args, parser)

    if command == "login":
        cmd_login(args, session_manager)
    elif command == "logout":
        cmd_logout(args, session_manager)
    elif command == "whoami":
        cmd_whoami(args, session_manager)
    elif command == "config" and values.get("config_command") == "show":
        cmd_config_show(args, session_manager)
    elif command == "material" and values.get("material_command") == "list":
        cmd_material_list(args, session_manager)
    elif command == "workflow":
        if values.get("workflow_command") == "upload":
            cmd_workflow_upload(args, session_manager)
        elif values.get("workflow_command") in {
            "list",
            "inspect",
            "run",
            "watch",
            "command",
            "authoring",
        }:
            cmd_workflow_command(args, session_manager)
        else:
            print_error("workflow 子命令不完整")
            raise SystemExit(1)
    elif command == "graph":
        if values.get("graph_command") in {
            "create",
            "upload",
            "list",
            "get",
            "download",
            "delete",
        }:
            from unilabos.app.cli.graph import cmd_graph_command

            cmd_graph_command(args, session_manager)
        else:
            print_error("graph 子命令不完整")
            raise SystemExit(1)
    else:
        print_error(f"{command} 子命令不完整")
        raise SystemExit(1)

    sys.stdout.flush()
    sys.stderr.flush()
    return True


def _run_doctor_command(args: argparse.Namespace) -> bool:
    """执行组网诊断；诊断失败按 ``run_doctor`` 的退出码退出（1 参数错误，2 / 3 诊断不通过）。"""

    from unilabos.client.host.hostlink.doctor import run_doctor

    code = run_doctor(vars(args))
    sys.stdout.flush()
    sys.stderr.flush()
    if code:
        raise SystemExit(code)
    return True


def run_cli_command(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
) -> bool:
    """统一分发所有无需启动设备 runtime 的 CLI 子命令。"""

    command = getattr(args, "command", None)
    if command in DOCTOR_COMMANDS:
        return _run_doctor_command(args)
    if command not in CLIENT_COMMANDS | PACKAGE_COMMANDS | RUNTIME_COMMANDS:
        return False
    session_manager = _prepare_command_session(args, parser)
    if command in RUNTIME_COMMANDS:
        from .runtime import run_runtime_command

        run_runtime_command(args, session_manager)
        return True
    if command in PACKAGE_COMMANDS:
        return run_package_command(
            vars(args),
            args_namespace=args,
            session_manager=session_manager,
        )
    return run_client_command(
        args,
        parser,
        session_manager=session_manager,
    )


__all__ = [
    "CLIENT_COMMANDS",
    "DOCTOR_COMMANDS",
    "PACKAGE_COMMANDS",
    "RUNTIME_COMMANDS",
    "register_cli_commands",
    "run_cli_command",
    "run_client_command",
    "run_package_command",
]
