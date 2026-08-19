"""旧导入路径；实现已迁入 unilabos.legacy_support.websocket。"""

from unilabos.legacy_support.websocket import LegacyWebSocketClient

OldBackendProtocolClient = LegacyWebSocketClient

__all__ = ["OldBackendProtocolClient"]
