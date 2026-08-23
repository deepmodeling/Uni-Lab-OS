"""UniLab HTTP/WS 地址的统一解析工具。"""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlparse, urlunparse


DEFAULT_BACKEND_ADDRESS = "https://leap-lab.bohrium.com/api/v1"

ADDRESS_ALIASES = {
    "prod": DEFAULT_BACKEND_ADDRESS,
    "test": "https://leap-lab.test.bohrium.com/api/v1",
    "uat": "https://leap-lab.uat.bohrium.com/api/v1",
    "local": "http://127.0.0.1:48197/api/v1",
}


def resolve_address(
    address: Optional[str],
    *,
    default: str = DEFAULT_BACKEND_ADDRESS,
) -> str:
    """解析环境别名并返回不带尾部斜杠的规范地址。"""

    value = str(address or "").strip()
    if not value:
        value = default
    value = ADDRESS_ALIASES.get(value.lower(), value)
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
    port_offset: int = 1,
) -> str:
    """从统一 HTTP 地址派生 WebSocket 地址。

    默认沿用既有 Backend 部署约定：显式端口加一；未显式指定端口时
    沿用同一 netloc。独立部署可通过 ``websocket_address`` 覆盖。
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
    "ADDRESS_ALIASES",
    "DEFAULT_BACKEND_ADDRESS",
    "derive_websocket_address",
    "normalize_api_address",
    "resolve_address",
]
