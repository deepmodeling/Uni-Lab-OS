"""工作流命令模块

提供 workflow 子命令：
- workflow upload: 上传工作流文件（迁移自 workflow_upload）

通过 resolve_effective_auth 注入凭据到 BasicConfig / HTTPConfig，
然后委托给现有的 handle_workflow_upload_command 实现。
"""

import json
import logging
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Dict

from unilabos.client import (
    SessionManager,
    print_error,
    print_success,
)
from unilabos.client.domain import DomainBackendClient, DomainClientError


def register_workflow_domain_subcommands(subparsers: Any) -> None:
    """Register Agent-safe Domain commands under ``unilab workflow``."""

    def common(parser: Any, *, jsonl: bool = False) -> None:
        parser.add_argument("--workspace", dest="workflow_workspace", default=None)
        parser.add_argument("--ak", dest="workflow_ak", default="")
        parser.add_argument("--sk", dest="workflow_sk", default="")
        output = parser.add_mutually_exclusive_group()
        output.add_argument("--json", dest="workflow_json", action="store_true")
        if jsonl:
            output.add_argument(
                "--jsonl", dest="workflow_jsonl", action="store_true"
            )

    list_parser = subparsers.add_parser("list", help="List workflow definitions")
    list_parser.add_argument("--page", type=int, default=1)
    list_parser.add_argument("--page-size", type=int, default=100)
    list_parser.add_argument("--name", default="")
    common(list_parser, jsonl=True)

    inspect_parser = subparsers.add_parser(
        "inspect", help="Inspect a workflow, Task, or node Job"
    )
    inspect_parser.add_argument("identity")
    inspect_parser.add_argument(
        "--kind", choices=["workflow", "task", "job", "debug"], default="task"
    )
    inspect_parser.add_argument("--event-limit", type=int, default=100)
    common(inspect_parser)

    run_parser = subparsers.add_parser("run", help="Create a workflow Task")
    run_parser.add_argument("workflow_uuid")
    run_parser.add_argument(
        "--mode", choices=["normal", "step", "single_node"], default="normal"
    )
    run_parser.add_argument("--target-node", default=None)
    run_parser.add_argument("--inputs", default="{}")
    run_parser.add_argument("--operation-id", default=None)
    run_parser.add_argument("--follow", action="store_true")
    run_parser.add_argument("--after", type=int, default=0)
    run_parser.add_argument("--timeout", type=float, default=300.0)
    run_parser.add_argument("--max-events", type=int, default=500)
    common(run_parser, jsonl=True)

    debug_parser = subparsers.add_parser(
        "debug", help="Launch with one start node and optional breakpoints"
    )
    debug_parser.add_argument("workflow_uuid")
    debug_parser.add_argument("--start-node", required=True)
    debug_parser.add_argument("--breakpoint", action="append", default=[])
    debug_parser.add_argument("--inputs", default="{}")
    debug_parser.add_argument("--operation-id", default=None)
    debug_parser.add_argument("--follow", action="store_true")
    debug_parser.add_argument("--after", type=int, default=0)
    debug_parser.add_argument("--timeout", type=float, default=300.0)
    debug_parser.add_argument("--max-events", type=int, default=500)
    common(debug_parser, jsonl=True)

    watch_parser = subparsers.add_parser(
        "watch", help="Watch a Task from an exclusive durable cursor"
    )
    watch_parser.add_argument("task_uuid")
    watch_parser.add_argument("--after", type=int, default=0)
    watch_parser.add_argument("--limit", type=int, default=100)
    watch_parser.add_argument("--timeout", type=float, default=300.0)
    watch_parser.add_argument("--max-events", type=int, default=500)
    common(watch_parser, jsonl=True)

    command_parser = subparsers.add_parser(
        "command", help="Submit pause/resume/continue/step/cancel idempotently"
    )
    command_parser.add_argument("task_uuid")
    command_parser.add_argument(
        "type", choices=["pause", "resume", "continue", "step", "cancel"]
    )
    command_parser.add_argument("--target-node", default=None)
    command_parser.add_argument("--hold", default=None)
    command_parser.add_argument("--idempotency-key", default=None)
    common(command_parser)

    authoring_parser = subparsers.add_parser(
        "authoring", help="Wait for Local Authoring revision or diagnostics"
    )
    authoring_parser.add_argument("workflow_uuid")
    authoring_parser.add_argument("--after-revision", type=int, required=True)
    authoring_parser.add_argument("--timeout", type=float, default=30.0)
    common(authoring_parser)


def dispatch_workflow_domain_command(args: Dict[str, Any]) -> bool:
    """Dispatch Domain commands without composing or controlling OS processes."""

    if args.get("command") != "workflow" or args.get("workflow_command") not in {
        "list",
        "inspect",
        "run",
        "debug",
        "watch",
        "command",
        "authoring",
    }:
        return False
    workspace = args.get("workflow_workspace") or args.get("workspace") or "."
    output_json = bool(args.get("workflow_json"))
    output_jsonl = bool(args.get("workflow_jsonl"))
    if output_json or output_jsonl:
        # Machine output must stay parseable even when the product root logger
        # is configured at DEBUG by an embedding application.
        for logger_name in ("httpx", "httpcore"):
            logging.getLogger(logger_name).setLevel(logging.WARNING)
    try:
        with DomainBackendClient.discover(
            workspace,
            ak=str(args.get("workflow_ak") or args.get("ak") or ""),
            sk=str(args.get("workflow_sk") or args.get("sk") or ""),
        ) as client:
            action = str(args["workflow_command"])
            if action == "list":
                data = client.list_workflows(
                    page=int(args["page"]),
                    page_size=int(args["page_size"]),
                    name=str(args.get("name") or ""),
                )
                _emit(client.result(data), json_output=output_json, jsonl=output_jsonl)
            elif action == "inspect":
                kind = str(args["kind"])
                identity = str(args["identity"])
                if kind == "workflow":
                    result = client.inspect_workflow(identity)
                elif kind == "job":
                    result = client.inspect_job(identity)
                elif kind == "debug":
                    result = client.get_debug_task(identity)
                else:
                    result = client.inspect_task(
                        identity,
                        event_limit=int(args["event_limit"]),
                    )
                _emit(result, json_output=output_json)
            elif action in {"run", "debug"}:
                input_value = _read_json_object(str(args.get("inputs") or "{}"))
                if action == "debug":
                    result = client.create_debug_task(
                        str(args["workflow_uuid"]),
                        start_node_uuid=str(args["start_node"]),
                        breakpoint_node_uuids=tuple(args.get("breakpoint") or ()),
                        input_value=input_value,
                        operation_id=args.get("operation_id"),
                    )
                else:
                    result = client.create_task(
                        str(args["workflow_uuid"]),
                        run_mode=str(args["mode"]),
                        target_node_uuid=args.get("target_node"),
                        input_value=input_value,
                        operation_id=args.get("operation_id"),
                    )
                _emit(result, json_output=output_json, jsonl=output_jsonl)
                if bool(args.get("follow")):
                    _emit_watch(
                        client,
                        str(result["taskUuid"]),
                        args,
                        json_output=output_json,
                        jsonl=output_jsonl,
                    )
            elif action == "watch":
                _emit_watch(
                    client,
                    str(args["task_uuid"]),
                    args,
                    json_output=output_json,
                    jsonl=output_jsonl,
                )
            elif action == "command":
                if args.get("hold"):
                    result = client.command_debug_task(
                        str(args["task_uuid"]),
                        str(args["type"]),
                        hold_uuid=str(args["hold"]),
                        idempotency_key=args.get("idempotency_key"),
                    )
                else:
                    result = client.command_task(
                        str(args["task_uuid"]),
                        str(args["type"]),
                        target_node_uuid=args.get("target_node"),
                        idempotency_key=args.get("idempotency_key"),
                    )
                _emit(result, json_output=output_json)
            else:
                result = client.wait_authoring(
                    str(args["workflow_uuid"]),
                    after_revision=int(args["after_revision"]),
                    timeout=float(args["timeout"]),
                )
                _emit(result, json_output=output_json)
    except (DomainClientError, OSError, ValueError) as error:
        failure = (
            error.as_dict()
            if isinstance(error, DomainClientError)
            else {"code": "invalid_input", "message": str(error)}
        )
        print(
            json.dumps({"ok": False, "error": failure}, ensure_ascii=False),
            file=sys.stderr,
        )
        raise SystemExit(1) from error
    return True


def _emit_watch(
    client: DomainBackendClient,
    task_uuid: str,
    args: Mapping[str, Any],
    *,
    json_output: bool,
    jsonl: bool,
) -> None:
    events = client.watch_task(
        task_uuid,
        after=int(args.get("after") or 0),
        limit=int(args.get("limit") or 100),
        timeout=float(args.get("timeout") or 300.0),
        max_events=int(args.get("max_events") or 500),
    )
    if jsonl:
        for event in events:
            print(json.dumps(event, ensure_ascii=False, separators=(",", ":")))
        return
    collected = list(events)
    _emit(collected, json_output=json_output)


def _read_json_object(specification: str) -> Dict[str, Any]:
    if specification.startswith("@"):
        raw = Path(specification[1:]).read_text(encoding="utf-8")
    else:
        raw = specification
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("--inputs 必须是 JSON 对象或 @JSON文件")
    return value


def _emit(payload: object, *, json_output: bool = False, jsonl: bool = False) -> None:
    if jsonl:
        if isinstance(payload, Mapping):
            data = payload.get("data")
            if isinstance(data, Mapping) and isinstance(data.get("items"), list):
                source = payload.get("sourceIdentity")
                for item in data["items"]:
                    print(
                        json.dumps(
                            {
                                "schemaVersion": payload.get("schemaVersion"),
                                "sourceIdentity": source,
                                "data": item,
                            },
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                    )
                return
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        return
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return
    if isinstance(payload, Mapping):
        source = payload.get("sourceIdentity")
        authority = source.get("authority") if isinstance(source, Mapping) else "?"
        print(f"Authority: {authority}")
        if payload.get("taskUuid"):
            print(f"Task: {payload['taskUuid']}")
        if payload.get("operationId"):
            print(f"Operation: {payload['operationId']}")
        print(json.dumps(payload.get("data"), ensure_ascii=False, indent=2))
        return
    print(payload)


def _inject_credentials(args: Any, session_manager: SessionManager) -> bool:
    """将解析后的 ak/sk + base_url 注入到 BasicConfig / HTTPConfig

    Returns:
        是否成功注入（凭据完整时返回 True）
    """
    from unilabos.app.cli.auth_resolver import resolve_effective_auth
    from unilabos.config.config import BasicConfig, HTTPConfig

    effective = resolve_effective_auth(args, session_manager)

    if not effective["ak"] or not effective["sk"]:
        print_error(
            "未找到 ak/sk。请通过以下方式之一配置：\n"
            "  1. unilab login --ak <ak> --sk <sk>\n"
            "  2. 命令行传入 --ak <ak> --sk <sk>\n"
            "  3. 在 local_config.py 中设置 BasicConfig.ak/sk"
        )
        return False

    BasicConfig.ak = effective["ak"]
    BasicConfig.sk = effective["sk"]
    BasicConfig.working_dir = str(session_manager.working_dir)
    HTTPConfig.remote_addr = effective["base_url"]
    return True


def cmd_workflow_upload(args, session_manager: SessionManager):
    """workflow upload 命令处理"""
    try:
        with session_manager:
            if not _inject_credentials(args, session_manager):
                sys.exit(1)

        # 注意：handle_workflow_upload_command 期待 args_dict 形式
        from unilabos.workflow.wf_utils import handle_workflow_upload_command

        args_dict: Dict[str, Any] = {
            "workflow_file": args.workflow_file,
            "workflow_name": args.workflow_name,
            "tags": args.tags or [],
            "published": args.published,
            "description": args.description or "",
        }
        handle_workflow_upload_command(args_dict)
        print_success("工作流上传完成")
    except SystemExit:
        raise
    except Exception as e:
        print_error(f"上传失败: {e}")
        sys.exit(1)
