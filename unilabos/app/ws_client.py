"""旧 WebSocket 客户端的兼容模块映射。

实现已迁入 unilabos.legacy_support.websocket。模块对象本身也映射过去，
以便既有扩展对旧模块属性的 patch 仍作用于真实实现。
"""

import sys

from unilabos.legacy_support import websocket as _legacy_websocket

sys.modules[__name__] = _legacy_websocket
