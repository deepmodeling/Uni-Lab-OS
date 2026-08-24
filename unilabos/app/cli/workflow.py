"""``unilab workflow`` 工作流管理命令。"""

import json
import sys
from typing import Any

from unilabos.client import (
    EnvelopeError,
    SessionManager,
    print_error,
    print_output,
    print_success,
)
from unilabos.client.workflow import HTTPWorkflowClient, WorkflowClientError
from unilabos.config.config import BasicConfig


def _create_workflow_client(
    args: Any,
    session_manager: SessionManager,
) -> HTTPWorkflowClient:
    """按 CLI、会话和本地微后端优先级创建工作流客户端。"""

    from unilabos.app.cli.auth_resolver import resolve_effective_auth

    with session_manager:
        effective = resolve_effective_auth(args, session_manager)

    if effective["base_url_source"] == "default":
        port = getattr(args, "port_management", None) or BasicConfig.port
        base_url = f"http://127.0.0.1:{port}"
    else:
        base_url = effective["base_url"]

    return HTTPWorkflowClient(
        base_url,
        ak=effective["ak"],
        sk=effective["sk"],
    )


def _print_jsonl(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def _emit_list(data: Any, *, jsonl: bool) -> None:
    if not jsonl:
        print_output(data)
        return
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        _print_jsonl(data)
        return
    for item in items:
        _print_jsonl(item)


def _emit_watch(events: Any, *, jsonl: bool) -> None:
    for event in events:
        if jsonl:
            _print_jsonl(event)
        else:
            print_output(event)


def cmd_workflow_command(args: Any, session_manager: SessionManager) -> None:
    """执行当前 Workflow Authority 支持的轻量命令。"""

    client = None
    try:
        client = _create_workflow_client(args, session_manager)
        action = str(args.workflow_command)
        jsonl = bool(getattr(args, "jsonl", False))

        if action == "list":
            data = client.list_workflows(
                page=args.page,
                page_size=args.page_size,
                name=args.name or "",
            )
            _emit_list(data, jsonl=jsonl)
        elif action == "inspect":
            if args.kind == "workflow":
                data = client.inspect_workflow(args.identity)
            elif args.kind == "job":
                data = client.get_job(args.identity)
            else:
                data = client.inspect_task(args.identity)
            print_output(data)
        elif action == "run":
            task = client.create_task(
                args.workflow_uuid,
                run_mode=args.mode,
                target_node_uuid=args.target_node,
                operation_id=args.operation_id,
            )
            if jsonl:
                _print_jsonl(task)
            else:
                print_output(task, message="工作流任务已创建")
            if args.follow:
                _emit_watch(
                    client.watch_task(
                        str(task["uuid"]),
                        after=args.after,
                        timeout=args.timeout,
                        max_events=args.max_events,
                    ),
                    jsonl=jsonl,
                )
        elif action == "watch":
            _emit_watch(
                client.watch_task(
                    args.task_uuid,
                    after=args.after,
                    timeout=args.timeout,
                    max_events=args.max_events,
                ),
                jsonl=jsonl,
            )
        elif action == "authoring":
            print_output(
                client.wait_authoring(
                    args.workflow_uuid,
                    after_revision=args.after_revision,
                    timeout=args.timeout,
                )
            )
        else:
            raise ValueError(f"不支持的 workflow 子命令: {action}")
    except SystemExit:
        raise
    except (EnvelopeError, WorkflowClientError, OSError, ValueError) as error:
        code = getattr(error, "code", None)
        prefix = f"[{code}] " if code is not None else ""
        print_error(f"工作流命令失败: {prefix}{error}")
        raise SystemExit(1) from error
    finally:
        if client is not None:
            client.close()


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

        from unilabos.server.workflow.upload import upload_workflow

        client = _create_workflow_client(args, session_manager)
        try:
            upload_workflow(
                client,
                args.workflow_file,
                args.workflow_name,
                args.tags or [],
                args.published,
                args.description or "",
            )
        finally:
            client.close()
        print_success("工作流上传完成")
    except SystemExit:
        raise
    except Exception as e:
        print_error(f"上传失败: {e}")
        sys.exit(1)


__all__ = ["cmd_workflow_command", "cmd_workflow_upload"]
