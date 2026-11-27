"""OS 与桌面外壳共享的逐行 JSON 启动协议。"""
from __future__ import annotations

import json
import threading
from typing import TextIO

PROTOCOL_VERSION = 2
ENTRY_MODULE = 'unilabos.workspace_host.launcher'


class Protocol:
    def __init__(self, stream: TextIO) -> None:
        self.stream = stream
        self.lock = threading.Lock()
        self._progress_message = "正在启动服务…"

    def emit(self, event: str, **payload: object) -> None:
        with self.lock:
            # 进程快照扩展既有 progress 事件，保留 v2 事件名称及 message 合同。
            if event == 'progress':
                payload.setdefault('message', self._progress_message)
                if 'processes' not in payload:
                    self._progress_message = str(payload['message'])
            elif event == 'ready':
                self._progress_message = '服务已启动，可在浏览器中使用'
            message = {'protocolVersion': PROTOCOL_VERSION, 'type': event, **payload}
            encoded = json.dumps(message, ensure_ascii=False, allow_nan=False)
            print(encoded, file=self.stream, flush=True)
