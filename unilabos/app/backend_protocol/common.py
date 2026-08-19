"""新旧后端协议共用的 WebSocket 传输辅助函数。"""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlparse

from unilabos.config.config import HTTPConfig


def build_schedule_websocket_url() -> Optional[str]:
    """从显式 schedule 地址或 Backend HTTP 地址构建 WS URL。"""

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


__all__ = ["build_schedule_websocket_url"]
