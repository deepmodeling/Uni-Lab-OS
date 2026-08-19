"""旧后端完整 WebSocket payload 协议的显式入口。"""

from unilabos.app.ws_client import WebSocketClient


class OldBackendProtocolClient(WebSocketClient):
    """仅用于连接旧后端；保留其完整 WS 命令与状态上报语义。"""


__all__ = ["OldBackendProtocolClient"]
