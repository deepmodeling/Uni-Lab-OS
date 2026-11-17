"""包与 runtime 命令的连接参数；仅连接已运行的 dev 管理端。"""

from __future__ import annotations

import argparse
import math
from typing import Any


def positive_timeout(value: str) -> float:
    timeout = float(value)
    if not math.isfinite(timeout) or timeout <= 0:
        raise argparse.ArgumentTypeError("超时必须为有限正数")
    return timeout


def add_management_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--address", "--addr", default=argparse.SUPPRESS, help="已运行的 dev 管理端地址")
    parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("--ak", default=argparse.SUPPRESS)
    parser.add_argument("--sk", default=argparse.SUPPRESS)


def management_connection(args: Any, session_manager: Any) -> dict[str, Any]:
    from unilabos.app.cli.auth_resolver import resolve_effective_auth
    from unilabos.client.session import AuthInfo
    from unilabos.config.config import BasicConfig

    with session_manager:
        effective = resolve_effective_auth(args, session_manager)
    port = getattr(args, "port_management", None) or BasicConfig.port
    auth = AuthInfo(ak=effective["ak"], sk=effective["sk"])
    return {
        "base_url": effective["base_url"] or f"http://127.0.0.1:{port}",
        "get_auth_secret": auth.auth_secret,
    }
