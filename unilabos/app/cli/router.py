"""顶层 CLI 子命令的注册与分发。"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any


CLIENT_COMMANDS = frozenset(
    {"login", "logout", "whoami", "config", "lab", "material", "workflow"}
)


def register_cli_commands(
    parser: argparse.ArgumentParser,
    subparsers: Any,
) -> None:
    """注册不参与设备 runtime bootstrap 的命令。"""

    package_parser = subparsers.add_parser(
        "package",
        aliases=["pkg"],
        help="Community device package tools: inspect / install",
    )
    package_actions = package_parser.add_subparsers(
        title="package actions", dest="package_action"
    )
    for action in ("inspect", "upload"):
        action_parser = package_actions.add_parser(
            action,
            help=(
                "Scan package dir and generate package_info/archive (local only)"
                if action == "inspect"
                else "Upload package through the old Backend HTTP API (--legacy)"
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
            help="Class namespace, e.g. community.acme; defaults from pyproject name",
        )
        action_parser.add_argument(
            "--out",
            type=str,
            default=None,
            help="Output dir for archive/package_info.json",
        )
        if action == "upload":
            action_parser.add_argument(
                "--download-url",
                dest="download_url",
                type=str,
                default="",
                help="Explicit archive URL; otherwise upload through legacy OSS API",
            )

    install_parser = package_actions.add_parser(
        "install",
        help="Install a pip spec / git URL locally, then scan @device IDs",
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
    lab_list.add_argument("--page_size", type=int, default=20, help="Page size")

    material_parser = subparsers.add_parser("material", help="Material management")
    material_actions = material_parser.add_subparsers(
        title="material subcommands", dest="material_command"
    )
    material_list = material_actions.add_parser(
        "list", help="List materials in a lab"
    )
    material_list.add_argument("--lab_uuid", type=str, required=True, help="Lab UUID")
    material_list.add_argument(
        "--with_children",
        action="store_true",
        default=False,
        help="Include child resources",
    )

    workflow_parser = subparsers.add_parser("workflow", help="Workflow management")
    workflow_actions = workflow_parser.add_subparsers(
        title="workflow subcommands", dest="workflow_command"
    )
    workflow_upload = workflow_actions.add_parser(
        "upload", help="Upload workflow file"
    )
    workflow_upload.add_argument(
        "-f", "--workflow_file", type=str, required=True, help="Workflow file (JSON)"
    )
    workflow_upload.add_argument(
        "-n", "--workflow_name", type=str, default=None, help="Workflow name"
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


def run_client_command(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
) -> bool:
    """执行轻量 HTTP/会话命令；非此类命令返回 ``False``。"""

    values = vars(args)
    command = values.get("command")
    if command not in CLIENT_COMMANDS:
        return False
    if command in {"lab", "material", "workflow"} and not values.get("legacy"):
        parser.error(f"{command} uses the old Backend HTTP API; add --legacy")

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

    if values.get("json", False):
        set_output_format(OutputFormat.JSON)

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
    session_manager = SessionManager(working_dir=working_dir)

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


def run_package_command(args: dict[str, Any]) -> bool:
    """执行本地 package 命令；非 package 命令返回 ``False``。"""

    if args.get("command") not in {"package", "pkg"}:
        return False
    from unilabos.app.package_cli import PackageCLIError, cmd_package
    from unilabos.utils.banner_print import print_status

    try:
        http_client = None
        if args.get("package_action") == "upload":
            if not args.get("legacy"):
                raise PackageCLIError(
                    "package upload uses the old Backend HTTP API; add --legacy"
                )
            from unilabos.legacy_support.http import get_legacy_http_client

            http_client = get_legacy_http_client()
        cmd_package(args, http_client=http_client)
    except PackageCLIError as exc:
        print_status(str(exc), "error")
        raise SystemExit(1) from exc
    return True


__all__ = [
    "CLIENT_COMMANDS",
    "register_cli_commands",
    "run_client_command",
    "run_package_command",
]
