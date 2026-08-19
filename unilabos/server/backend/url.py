"""Backend 连接地址构建函数。"""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlparse

from unilabos.config.config import HTTPConfig


def build_backend_websocket_url() -> Optional[str]:
    """从显式 schedule 地址或 Backend HTTP 地址构建 WS URL。

    ``/ws/schedule`` 是当前 Backend 已发布的线协议路径；模块命名不再把
    连接能力限定为 scheduler，后续其他数据域也复用该 Backend 会话。
    """

    if HTTPConfig.schedule_addr:
        parsed = urlparse(HTTPConfig.schedule_addr)
        scheme = "wss" if parsed.scheme in {"https", "wss"} else "ws"
        return f"{scheme}://{parsed.netloc}/api/v1/ws/schedule"

    if not HTTPConfig.remote_addr:
        return None

    parsed = urlparse(HTTPConfig.remote_addr)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    if ":" in parsed.netloc and parsed.port is not None:
        return (
            f"{scheme}://{parsed.hostname}:{parsed.port + 1}"
            "/api/v1/ws/schedule"
        )
    return f"{scheme}://{parsed.netloc}/api/v1/ws/schedule"


__all__ = ["build_backend_websocket_url"]
