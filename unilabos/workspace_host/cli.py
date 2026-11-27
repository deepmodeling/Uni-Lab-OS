"""`unilab workspace` adapter over the Workspace Host client SDK."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from unilabos.package_manager import (
    WorkspaceInitError,
    initialize_workspace,
)

from .client import WorkspaceHostClient, ensure_workspace_host
from .model import COMPONENT_NAMES, WorkspaceHostError


def register_workspace_subcommands(subparsers: Any) -> None:
    """注册 ``unilab workspace`` 的公开生命周期命令。

    参数：``subparsers`` 是顶层 CLI 提供的 argparse 子命令容器。返回：无；
    原地注册 init/status/start/stop/restart/reset-local。异常：argparse 配置错误由
    标准库原样抛出；默认 ``all`` 只用于统一双进程生命周期，显式组件仍保留。
    """

    parser = subparsers.add_parser(
        "workspace",
        help="Control the per-workspace Local Backend, OS, PLC-Sim, and renderer",
    )
    actions = parser.add_subparsers(dest="workspace_action", required=True)
    init = actions.add_parser("init", help="Create a new device-package workspace")
    init.add_argument(
        "--output",
        required=True,
        help="待创建的工作区目录；目标必须不存在",
    )
    init.add_argument(
        "--name",
        default=None,
        dest="workspace_package_name",
        help="包名；支持 sample-lab 或 sample_lab，省略时从输出目录名派生",
    )
    init.add_argument("--json", action="store_true", dest="workspace_json")
    for action in ("status", "start", "stop", "restart", "reset-local"):
        leaf = actions.add_parser(action)
        leaf.add_argument("--workspace", dest="workspace_cli_path", default=None)
        leaf.add_argument("--json", action="store_true", dest="workspace_json")
        if action not in {"status", "reset-local"}:
            leaf.add_argument(
                "--component",
                choices=["all", "backend", "os", "plc"],
                default=("all" if action in {"start", "stop", "restart"} else "os"),
                help=(
                    "all 表示由一个 Host 操作编排调度进程和 Edge/驱动进程；"
                    "backend、os、plc 仅操作单个组件。"
                ),
            )
        if action != "status":
            leaf.add_argument("--operation-id", default=None)
            leaf.add_argument("--wait", type=float, default=120.0)
        if action in {"start", "restart", "reset-local"}:
            leaf.add_argument("--graph", default=None)
            leaf.add_argument(
                "--runtime-mode", choices=["normal", "dry-run"], default=None
            )
        if action in {"start", "restart"}:
            leaf.add_argument("-f", "--station-file", help="工站启动 YAML 路径")
            if action == "start":
                leaf.add_argument("--foreground-role", choices=["scheduler", "runtime"],
                                  help="容器前台运行的工站组件，配合 -f 和 --plan-file")
                leaf.add_argument("--plan-file", help="前台双容器共享的启动计划文件")
            leaf.add_argument(
                "--database-type", "--database_type", default=None,
                choices=["sqlite", "postgresql"], help="本次工作区启动使用的数据库类型",
            )
            leaf.add_argument(
                "--database-url", "--database_url", default=None,
                help="PostgreSQL 连接地址：postgresql://用户:密码@主机:端口/数据库",
            )
        if action == "reset-local":
            leaf.add_argument(
                "--yes",
                action="store_true",
                help="Confirm deletion of rebuildable Local Domain and Edge state",
            )
    logs = actions.add_parser("logs")
    logs.add_argument("--workspace", dest="workspace_cli_path", default=None)
    logs.add_argument("--component", choices=COMPONENT_NAMES, default="backend")
    logs.add_argument("--max-bytes", type=int, default=64 * 1024)
    logs.add_argument("--json", action="store_true", dest="workspace_json")
    operation = actions.add_parser("operation")
    operation.add_argument("operation_id")
    operation.add_argument("--workspace", dest="workspace_cli_path", default=None)
    operation.add_argument("--json", action="store_true", dest="workspace_json")


def dispatch_workspace_command(args: dict[str, Any]) -> bool:
    if args.get("command") != "workspace":
        return False
    workspace = args.get("workspace_cli_path") or args.get("workspace") or os.getcwd()
    action = str(args.get("workspace_action") or "")
    output_json = bool(args.get("workspace_json"))
    try:
        station_parameters = {}
        if args.get("station_file"):
            from .station_config import StationFile, run_station_role

            station = StationFile.load(args["station_file"])
            workspace = args.get("workspace_cli_path") or args.get("workspace") or str(station.relative(station.settings.workspace))
            if args.get("component") != "all":
                raise WorkspaceHostError("invalid_request", "-f 用于整站启动；分容器部署使用 --foreground-role")
            station_parameters = {"stationFile": str(station.path), "stationFileDigest": station.digest}
            if args.get("foreground_role"):
                if not args.get("plan_file"):
                    raise WorkspaceHostError("invalid_request", "前台组件需指定 --plan-file")
                if args.get("database_type") is not None or args.get("database_url") is not None:
                    raise WorkspaceHostError("invalid_request", "分容器数据库通过工作区配置或环境提供")
                if args["foreground_role"] == "runtime" and (args.get("graph") or args.get("runtime_mode")):
                    raise WorkspaceHostError("invalid_request", "runtime 采用 scheduler 固定的设备图和运行模式")
                run_station_role(args["foreground_role"], station, workspace, args["plan_file"],
                                 {"graphPath": args.get("graph"), "runtimeMode": args.get("runtime_mode")})
                return True
        if args.get("foreground_role") or args.get("plan_file"):
            raise WorkspaceHostError("invalid_request", "容器前台启动须同时指定 -f、--foreground-role、--plan-file")
        if action == "init":
            initialized = initialize_workspace(
                args.get("output"),
                package_name=args.get("workspace_package_name"),
            )
            result = (
                initialized.as_dict()
                if output_json
                else {"content": initialized.render_text()}
            )
        elif action == "status":
            result = WorkspaceHostClient.status(workspace)
        elif action == "logs":
            client = WorkspaceHostClient.discover(workspace)
            result = client.logs(
                str(args["component"]), max_bytes=int(args["max_bytes"])
            )
        elif action == "operation":
            client = WorkspaceHostClient.discover(workspace)
            result = client.operation(str(args["operation_id"]))
        elif action == "reset-local":
            if not args.get("yes"):
                raise WorkspaceHostError(
                    "confirmation_required",
                    "重建本地数据会删除可重建的 Local Domain 与 Edge 状态；请显式传入 --yes",
                )
            client = ensure_workspace_host(workspace)
            parameters = {
                key: value
                for key, value in {
                    "graphPath": args.get("graph"),
                    "runtimeMode": args.get("runtime_mode"),
                }.items()
                if value is not None
            }
            result = client.execute(
                "local.reset-state",
                parameters=parameters,
                operation_id=args.get("operation_id"),
                timeout=float(args.get("wait") or 120.0),
            )
        else:
            database_parameters = (
                _database_parameters(args, workspace, action)
                if action in {"start", "restart"} else {}
            )
            client = ensure_workspace_host(workspace)
            parameters = {
                key: value
                for key, value in {
                    "graphPath": args.get("graph"),
                    "runtimeMode": args.get("runtime_mode"),
                }.items()
                if value is not None
            }
            command = _command(action, str(args.get("component")))
            parameters.update(database_parameters)
            parameters.update(station_parameters)
            result = client.execute(
                command,
                parameters=parameters,
                operation_id=args.get("operation_id"),
                timeout=float(args.get("wait") or 120.0),
            )
    except (WorkspaceHostError, WorkspaceInitError) as error:
        _print({"ok": False, "error": error.as_dict()}, output_json=True)
        raise SystemExit(1) from error
    _print(result, output_json=output_json)
    return True


def _database_parameters(args: dict[str, Any], workspace: str, action: str) -> dict[str, str]:
    """在当前 CLI 进程解析配置，避免已运行 Host 使用旧的环境变量。"""
    from unilabos.persistence.workspace import workspace_settings

    overrides = {
        key: args[key] for key in ("database_type", "database_url")
        if args.get(key) is not None
    }
    supplied = bool(overrides) or any(
        key in os.environ for key in (
            "UNILABOS_BASICCONFIG_DATABASE_TYPE", "UNILABOS_BASICCONFIG_DATABASE_URL"
        )
    )
    if args.get("component") == "plc":
        if supplied:
            raise WorkspaceHostError("invalid_request", "PLC 模拟器不接收业务数据库参数")
        return {}
    try:
        settings = workspace_settings(
            Path(workspace).expanduser().resolve(),
            active=action == "restart" and not supplied,
            overrides=overrides,
        )
    except (ValueError, OSError) as error:
        raise WorkspaceHostError("database_configuration_invalid", str(error)) from error
    return {
        "databaseType": settings.database_type,
        "databaseUrl": settings.database_url if settings.database_type == "postgresql" else "",
    }


def _command(action: str, component: str) -> str:
    """把用户动作与组件名转换为 Workspace Host 命令。

    参数：``action`` 是 start/stop/restart，``component`` 是 all/backend/os/plc。
    返回：统一生命周期使用 ``workspace.*``，单组件使用对应命令。异常：组合
    不受支持时抛 ``WorkspaceHostError``，不猜测或降级到其他组件。
    """

    names = {"backend": "backend", "os": "os", "plc": "plc"}
    if action not in {"start", "stop", "restart"} or component not in names:
        if component == "all" and action in {"start", "stop", "restart"}:
            return f"workspace.{action}"
        raise WorkspaceHostError("invalid_request", "无效 workspace 操作")
    return f"{names[component]}.{action}"


def _print(payload: object, *, output_json: bool) -> None:
    if output_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return
    if isinstance(payload, dict) and "content" in payload:
        print(str(payload.get("content") or ""), end="")
        return
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
