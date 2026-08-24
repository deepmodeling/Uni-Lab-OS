"""Backend 连接地址构建函数。"""

from __future__ import annotations

from typing import Optional
from unilabos.config.config import HTTPConfig
from unilabos.utils.address import derive_websocket_address


def build_backend_websocket_url() -> Optional[str]:
    """从显式 schedule 地址或 Backend HTTP 地址构建 WS URL。

    ``/ws/schedule`` 是当前 Backend 已发布的线协议路径；模块命名不再把
    连接能力限定为 scheduler，后续其他数据域也复用该 Backend 会话。
    """

    if not HTTPConfig.remote_addr:
        return None
    return derive_websocket_address(
        HTTPConfig.remote_addr,
        websocket_address=HTTPConfig.schedule_addr,
    )


__all__ = ["build_backend_websocket_url"]
