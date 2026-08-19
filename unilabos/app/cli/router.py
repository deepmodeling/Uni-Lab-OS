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
    {"login", "logout", "whoami", "config", "lab", "material", "workflow"}
)
PACKAGE_COMMANDS = frozenset({"package", "pkg"})


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
    subparsers.add_parser("logout", help="Clear local ak/sk")
    subparsers.add_parser("whoami", help="Show current user information")

    config_parser = subparsers.add_parser("config", help="Show session configuration")
    config_actions = config_parser.add_subparsers(
        title="config subcommands", dest="config_command"
    )
    config_actions.add_parser("show", help="Show current session configuration")

    lab_parser = subparsers.add_parser("lab", help="Laboratory management")
    lab_actions = lab_parser.add_subparsers(
        title="lab subcommands", dest="lab_command"
    )
    lab_list = lab_actions.add_parser("list", help="List laboratories")
    lab_list.add_argument("--page", type=int, default=1, help="Page number")
    lab_list.add_argument(
        "--page_size", "--page-size", type=int, default=20, help="Page size"
    )

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

    workflow_parser = subparsers.add_parser("workflow", help="Workflow management")
    workflow_actions = workflow_parser.add_subparsers(
        title="workflow subcommands", dest="workflow_command"
    )
    workflow_upload = workflow_actions.add_parser(
        "upload", help="Upload workflow file"
    )
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


def _prepare_command_session(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
):
    from unilabos.client import SessionManager, resolve_addr

    values = vars(args)
    working_dir = os.path.abspath(values.get("working_dir") or os.getcwd())
    if os.path.basename(working_dir) != "unilabos_data":
        data_dir = os.path.join(working_dir, "unilabos_data")
        if os.path.isdir(data_dir):
            working_dir = data_dir

    address = values.get("addr")
    args.addr_resolved = (
        resolve_addr(address)
        if address and address != parser.get_default("addr")
        else None
    )
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
    if command in {"lab", "workflow"} and not values.get("legacy"):
        parser.error(f"{command} uses the old Backend HTTP API; add --legacy")

    from unilabos.app.cli.auth import cmd_login, cmd_logout, cmd_whoami
    from unilabos.app.cli.config import cmd_config_show
    from unilabos.app.cli.lab import cmd_lab_list
    from unilabos.app.cli.material import cmd_material_list
    from unilabos.app.cli.workflow import cmd_workflow_upload
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
    elif command == "lab" and values.get("lab_command") == "list":
        cmd_lab_list(args, session_manager)
    elif command == "material" and values.get("material_command") == "list":
        cmd_material_list(args, session_manager)
    elif command == "workflow" and values.get("workflow_command") == "upload":
        cmd_workflow_upload(args, session_manager)
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
