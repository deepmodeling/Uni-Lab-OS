"""Backend 连接地址构建函数。"""

from __future__ import annotations

from typing import Optional
from unilabos.config.config import HTTPConfig
from unilabos.utils.address import derive_websocket_address


def build_backend_websocket_url() -> Optional[str]:
    """从显式 schedule 地址或 Backend HTTP 地址构建 WS URL。

    ``/ws/schedule`` 是 Backend 的线协议路径；同一 Backend 会话也可供
    其他数据域复用。
    """

    if not HTTPConfig.remote_addr:
        return None
    return derive_websocket_address(
        HTTPConfig.remote_addr,
        websocket_address=HTTPConfig.schedule_addr,
    )


def build_legacy_backend_websocket_url() -> Optional[str]:
    """构造旧 Backend 的 WebSocket 地址。

    旧云端部署历史上把 ``/ws/schedule`` 放在 HTTP 端口 ``+1``。这个
    例外只属于显式 legacy 适配器；runtime.v1 请使用
    :func:`build_backend_websocket_url` 的同端口地址。
    """

    if not HTTPConfig.remote_addr:
        return None
    return derive_websocket_address(
        HTTPConfig.remote_addr,
        websocket_address=HTTPConfig.schedule_addr,
        port_offset=1,
    )


__all__ = [
    "build_backend_websocket_url",
    "build_legacy_backend_websocket_url",
]
