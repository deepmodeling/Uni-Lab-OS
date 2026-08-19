"""后端线协议实现。

``control`` 是新微后端的 WS 轻通知 + HTTP 拉取协议；``old`` 仅用于连接
仍收发完整 WebSocket payload 的旧后端。
"""

CONTROL_PROTOCOL = "control"
OLD_PROTOCOL = "old"

__all__ = ["CONTROL_PROTOCOL", "OLD_PROTOCOL"]
