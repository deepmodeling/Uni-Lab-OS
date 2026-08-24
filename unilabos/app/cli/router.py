"""顶层 CLI 子命令的注册与分发。"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

from .package import (
    register_package_commands,
    run_package_command,
)

CLIENT_COMMANDS = frozenset(
    {"login", "logout", "whoami", "config", "material", "workflow"}
)
PACKAGE_COMMANDS = frozenset({"package", "pkg"})


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


def register_cli_commands(
    parser: argparse.ArgumentParser,
    subparsers: Any,
) -> None:
    """注册不参与设备 runtime bootstrap 的命令。"""

    register_package_commands(subparsers)

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


def _prepare_command_session(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
):
    from unilabos.client import SessionManager, resolve_address

    values = vars(args)
    working_dir = os.path.abspath(values.get("working_dir") or os.getcwd())
    if os.path.basename(working_dir) != "unilabos_data":
        data_dir = os.path.join(working_dir, "unilabos_data")
        if os.path.isdir(data_dir):
            working_dir = data_dir

    address = values.get("address")
    args.address_resolved = resolve_address(address) if address else None
    # 兼容尚未迁移的第三方 CLI 扩展；内部代码统一读取 address_resolved。
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
            "authoring",
        }:
            cmd_workflow_command(args, session_manager)
        else:
            print_error("workflow 子命令不完整")
            raise SystemExit(1)
    else:
        print_error(f"{command} 子命令不完整")
        raise SystemExit(1)

    sys.stdout.flush()
    sys.stderr.flush()
    return True


def run_cli_command(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
) -> bool:
    """统一分发所有无需启动设备 runtime 的 CLI 子命令。"""

    command = getattr(args, "command", None)
    if command not in CLIENT_COMMANDS | PACKAGE_COMMANDS:
        return False
    session_manager = _prepare_command_session(args, parser)
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
    "PACKAGE_COMMANDS",
    "register_cli_commands",
    "run_cli_command",
    "run_client_command",
    "run_package_command",
]
