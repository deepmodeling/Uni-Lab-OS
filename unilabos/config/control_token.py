"""调度权威与 Host 之间控制面的共享 token。

控制 WebSocket（``/api/v1/ws/schedule``）、命令正文（``GET /edge/commands/{uuid}``）和
``backend_http`` 结果回送（``POST /api/v1/edge/http-responses/{uuid}``）只属于 Host：
谁连上控制 WebSocket，谁就能收到下发的命令、伪造执行结果，还会把真正的 Host 挤下线
（后来者赢）。所以这三处都要求携带 token。

- **权威**：``BasicConfig.control_token`` 非空就用它；否则读 ``<working_dir>/control_token``，
  没有就生成一个（权限 0600）并保存，之后每次启动沿用。
- **Host 子进程**：由权威经环境变量 ``UNILABOS_BASICCONFIG_CONTROL_TOKEN`` 传入，无需配置。
- **远端 Host**（``--address`` 指向另一台机器上的权威）：在配置文件填
  ``BasicConfig.control_token``（或设上面的环境变量）为权威那份 token；同一台机器、同一工作
  目录时直接读 ``<working_dir>/control_token``。

请求头用 ``X-UniLab-Token``（``Authorization`` 已被旧云端的 ``Lab <ak:sk>`` 占用）；
也接受 ``Authorization: Bearer <token>``。本模块不依赖 FastAPI，SDK 与 Host 都可以用。
"""

from __future__ import annotations

import hmac
import os
import secrets
from pathlib import Path
from typing import Mapping, Optional

from unilabos.config.config import BasicConfig

TOKEN_HEADER = "X-UniLab-Token"
TOKEN_ENV = "UNILABOS_BASICCONFIG_CONTROL_TOKEN"
TOKEN_FILENAME = "control_token"


def token_path(working_dir: str | os.PathLike[str]) -> Path:
    return Path(working_dir).expanduser() / TOKEN_FILENAME


def _read_token(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, NotADirectoryError):
        return ""


def ensure_authority_token(working_dir: str | os.PathLike[str]) -> tuple[str, Optional[Path]]:
    """权威启动时确定控制面 token，写回 ``BasicConfig.control_token``。

    返回 ``(token, 保存它的文件)``；token 来自配置 / 环境变量时文件为 ``None``。
    """

    configured = str(BasicConfig.control_token or "").strip()
    if configured:
        BasicConfig.control_token = configured
        return configured, None
    path = token_path(working_dir)
    token = _read_token(path)
    if not token:
        path.parent.mkdir(parents=True, exist_ok=True)
        candidate = secrets.token_urlsafe(32)
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            # 并发启动的另一个权威先写了：用它的
            token = _read_token(path)
        else:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(candidate + "\n")
            token = candidate
        if not token:
            raise RuntimeError(f"控制面 token 文件 {path} 为空，请删除后重启")
    BasicConfig.control_token = token
    return token, path


def client_token() -> str:
    """Host / SDK 发请求时带的 token：配置、环境变量、工作目录里的 token 文件，依次取第一个非空的。

    环境变量单独再读一次：CLI 子命令不加载配置文件，也不应用 ``UNILABOS_*`` 覆盖。
    """

    configured = str(BasicConfig.control_token or "").strip() or os.environ.get(TOKEN_ENV, "").strip()
    if configured:
        return configured
    working_dir = str(BasicConfig.working_dir or "").strip()
    return _read_token(token_path(working_dir)) if working_dir else ""


def authority_token() -> str:
    """本进程作为权威时使用的 token（未启用时为空）。"""

    return str(BasicConfig.control_token or "").strip()


def client_headers() -> dict[str, str]:
    token = client_token()
    return {TOKEN_HEADER: token} if token else {}


def presented_token(headers: Mapping[str, str]) -> str:
    """从请求头取出对方出示的 token（``X-UniLab-Token`` 或 ``Authorization: Bearer``）。"""

    value = headers.get(TOKEN_HEADER) or headers.get(TOKEN_HEADER.lower())
    if value:
        return str(value).strip()
    authorization = str(headers.get("authorization") or headers.get("Authorization") or "")
    scheme, _, credential = authorization.partition(" ")
    if scheme.lower() == "bearer":
        return credential.strip()
    return ""


def token_accepted(headers: Mapping[str, str]) -> bool:
    """出示的 token 与权威的一致（常量时间比较）。

    权威没有 token（未经 :func:`ensure_authority_token`，只发生在单元测试或嵌入式装配里）时不校验。
    """

    expected = str(BasicConfig.control_token or "").strip()
    if not expected:
        return True
    presented = presented_token(headers)
    return bool(presented) and hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))


__all__ = [
    "authority_token",
    "TOKEN_ENV",
    "TOKEN_FILENAME",
    "TOKEN_HEADER",
    "client_headers",
    "client_token",
    "ensure_authority_token",
    "presented_token",
    "token_accepted",
    "token_path",
]
