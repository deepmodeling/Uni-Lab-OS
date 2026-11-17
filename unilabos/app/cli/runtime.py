"""以 dev 统一管理 API 管理运行中的进程；不需要 V1 那种独立的 Workspace Host 监督进程。"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import httpx

from unilabos.app.cli.management import add_management_options, management_connection, positive_timeout
from unilabos.client.runtime.lifecycle import HTTPLifecycleClient, SchedulerDrainTimeout
from unilabos.client.utils.envelope import EnvelopeError
from unilabos.protocol.runtime.lifecycle import ResetRequest, RestartRequest


def register_runtime_commands(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "runtime", help="管理 dev backend/Host：状态、排空、恢复、重启和重置",
        description="启动使用 unilab；本命令只连接管理 API，不启动额外的监督进程。",
    )
    actions = parser.add_subparsers(dest="runtime_action", required=True)
    for action in ("status", "drain", "resume", "restart", "reset"):
        command = actions.add_parser(action)
        add_management_options(command)
        if action == "drain":
            command.add_argument("--wait", action="store_true", help="等待在途设备动作收敛")
            command.add_argument("--timeout", type=positive_timeout, default=300.0)
        elif action == "restart":
            command.add_argument("--cancel", action="store_true", help="取消尚未执行的重启")
        elif action == "reset":
            # 无确认参数时只预览；真正重置复用服务端两项确认条件。
            command.add_argument("--confirmation-token", "--confirmation_token", default="")
            command.add_argument("--confirmation", default="")


def run_runtime_command(args: argparse.Namespace, session_manager: Any) -> None:
    action = args.runtime_action
    if action == "reset" and bool(args.confirmation_token) != bool(args.confirmation):
        print("重置需同时提供 --confirmation-token 和 --confirmation，或全部省略以预览。", file=sys.stderr)
        raise SystemExit(1)
    try:
        with HTTPLifecycleClient(**management_connection(args, session_manager)) as client:
            if action == "status":
                payload = {
                    "health": client.health().model_dump(),
                    "readiness": client.readiness().model_dump(),
                    "restart": client.restart_status().model_dump(),
                    "drain": client.drain_status().model_dump(),
                }
            elif action == "drain":
                status = client.drain_and_wait(timeout=args.timeout) if args.wait else client.begin_drain()
                payload = status.model_dump()
            elif action == "resume":
                payload = client.resume_from_drain().model_dump()
            elif action == "restart":
                # 权威自身由外部启动器管理；默认只在安全点重启 Host。
                status = client.cancel_restart() if args.cancel else client.request_restart(
                    RestartRequest(mode="quiescent", scope="edge")
                )
                payload = status.model_dump()
            elif action == "reset":
                status = client.request_reset(ResetRequest(
                    confirmation_token=args.confirmation_token, confirmation=args.confirmation,
                )) if args.confirmation_token else client.reset_preview()
                payload = status.model_dump()
            else:
                raise ValueError(f"未知 runtime 子命令：{action}")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    except (httpx.HTTPError, EnvelopeError, ValueError, SchedulerDrainTimeout) as exc:
        print(f"runtime {action} 失败：{exc}", file=sys.stderr)
        raise SystemExit(1) from exc
