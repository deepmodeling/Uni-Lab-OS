"""Uni-Lab-OS HTTP/WS 地址的统一解析工具。"""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlparse, urlunparse


def resolve_address(
    address: Optional[str],
    *,
    default: str = "",
) -> str:
    """返回不带尾部斜杠的规范地址；空值回退 default。

    不内置任何云端默认地址或环境别名，后端地址必须由用户显式指定
    （环境快捷选项仅保留在 UniLabOS-Launcher）。
    """

    value = str(address or "").strip()
    if not value:
        value = default
    return value.rstrip("/")


def normalize_api_address(address: str, *, api_path: str = "/api/v1") -> str:
    """把服务根地址规范化为 API 根地址。"""

    normalized = resolve_address(address, default="")
    if not normalized:
        raise ValueError("API address cannot be empty")
    suffix = "/" + api_path.strip("/")
    if normalized.endswith(suffix):
        return normalized
    return normalized + suffix


def derive_websocket_address(
    address: str,
    *,
    websocket_address: Optional[str] = None,
    endpoint: str = "/api/v1/ws/schedule",
    port_offset: int = 0,
) -> str:
    """从统一 HTTP 地址派生 WebSocket 地址。

    runtime.v1 控制面与 HTTP 数据面是同一个服务，默认使用同端口。旧
    Backend 的 ``+1`` 约定只能由 legacy 适配器显式传入 ``port_offset=1``；
    这样普通的微后端/工作流客户端不会误连到另一个端口。独立部署仍可
    通过 ``websocket_address`` 覆盖。
    """

    explicit = str(websocket_address or "").strip()
    source = explicit or resolve_address(address, default="")
    parsed = urlparse(source)
    if not parsed.scheme or not parsed.hostname:
        raise ValueError("WebSocket address is invalid")

    scheme = "wss" if parsed.scheme in {"https", "wss"} else "ws"
    endpoint_path = "/" + endpoint.strip("/")
    if explicit:
        path = parsed.path.rstrip("/")
        if not path:
            path = endpoint_path
        elif path.endswith("/api/v1"):
            path += "/ws/schedule"
        return urlunparse((scheme, parsed.netloc, path, "", parsed.query, ""))

    hostname = parsed.hostname
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    if parsed.port is not None:
        netloc = f"{hostname}:{parsed.port + port_offset}"
    else:
        netloc = parsed.netloc
    return urlunparse((scheme, netloc, endpoint_path, "", "", ""))


__all__ = [
    "derive_websocket_address",
    "normalize_api_address",
    "resolve_address",
]
