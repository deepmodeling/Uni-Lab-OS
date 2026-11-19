"""判定显式 legacy 适配目标（仅供 Backend 侧兼容层使用）。

旧云端与 runtime.v1 的 HTTP 数据面完全不同：runtime.v1 提供
``/edge/commands/{uuid}``，旧 Backend 提供 ``/edge/lab/info``。这个探测器
保留给 Backend 侧的兼容入口；Edge 的 ``BackendSessionFactory`` 不再调用
它，也不会根据探测结果切换线协议。
"""

from __future__ import annotations

import threading
from typing import Literal, Optional

import requests

from unilabos.config.config import BasicConfig, HTTPConfig
from unilabos.utils import logger

BackendProtocol = Literal["runtime.v1", "legacy"]

PROTOCOL_RUNTIME_V1: BackendProtocol = "runtime.v1"
PROTOCOL_LEGACY: BackendProtocol = "legacy"

_PROBE_TIMEOUT = 8.0
_lock = threading.Lock()
_cached: Optional[BackendProtocol] = None
_cached_for: str = ""


def _api_base(address: str) -> str:
    base = str(address or "").strip().rstrip("/")
    if base.endswith("/api/v1"):
        return base
    return f"{base}/api/v1"


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Lab {BasicConfig.auth_secret()}"}


def _looks_like_runtime_v1(base: str, session: requests.Session) -> Optional[bool]:
    """runtime.v1 的命令路由对未知 uuid 返回 JSON 4xx；旧后端返回纯文本 404。"""

    try:
        response = session.get(
            f"{base}/edge/commands/00000000-0000-0000-0000-000000000000",
            headers=_auth_headers(),
            timeout=_PROBE_TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.warning("[BackendProbe] runtime.v1 探测失败: %s", exc)
        return None
    content_type = str(response.headers.get("content-type") or "")
    return response.status_code != 404 or "json" in content_type


def _looks_like_legacy(base: str, session: requests.Session) -> Optional[bool]:
    try:
        response = session.get(
            f"{base}/edge/lab/info", headers=_auth_headers(), timeout=_PROBE_TIMEOUT
        )
    except requests.RequestException as exc:
        logger.warning("[BackendProbe] 旧后端探测失败: %s", exc)
        return None
    if response.status_code == 404:
        return False
    try:
        body = response.json()
    except ValueError:
        return False
    return isinstance(body, dict) and "code" in body


def detect_backend_protocol(
    address: Optional[str] = None,
    *,
    session: Optional[requests.Session] = None,
    force: bool = False,
) -> BackendProtocol:
    """返回目标后端使用的协议；探测不通时回落 runtime.v1（保持既有行为）。"""

    global _cached, _cached_for
    target = str(address or HTTPConfig.remote_addr or "").strip()
    configured = str(getattr(HTTPConfig, "backend_protocol", "") or "").strip()
    if configured in (PROTOCOL_RUNTIME_V1, PROTOCOL_LEGACY):
        return configured  # type: ignore[return-value]
    if not target:
        return PROTOCOL_RUNTIME_V1
    with _lock:
        if not force and _cached is not None and _cached_for == target:
            return _cached
        base = _api_base(target)
        http = session or requests.Session()
        result: BackendProtocol = PROTOCOL_RUNTIME_V1
        runtime_v1 = _looks_like_runtime_v1(base, http)
        if runtime_v1 is False:
            legacy = _looks_like_legacy(base, http)
            if legacy:
                result = PROTOCOL_LEGACY
        _cached = result
        _cached_for = target
        logger.info("[BackendProbe] %s 使用 %s 协议", target, result)
        return result


def reset_probe_cache() -> None:
    global _cached, _cached_for
    with _lock:
        _cached = None
        _cached_for = ""


__all__ = [
    "BackendProtocol",
    "PROTOCOL_LEGACY",
    "PROTOCOL_RUNTIME_V1",
    "detect_backend_protocol",
    "reset_probe_cache",
]
