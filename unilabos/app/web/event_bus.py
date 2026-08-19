"""Host 事件入口与 Scheduler Provider 共用的进程级监控总线。

Host、Scheduler、Inventory 与 Status 必须写入同一条序列，否则 Edge UI 的
``Last-Event-ID`` 无法恢复完整增量。本模块保留旧导入路径，只做兼容转发。
"""

from __future__ import annotations

import json
from typing import Any, Dict

from unilabos.app.scheduler.monitor import CHANNELS, MonitorBus, monitor_bus


def format_sse_event(event: Dict[str, Any]) -> str:
    """按 Edge monitor 契约编码一条 SSE 事件。"""

    payload = json.dumps(event, ensure_ascii=False, default=str)
    return (
        f"id: {event['seq']}\n"
        f"event: {event['channel']}\n"
        f"data: {payload}\n\n"
    )


__all__ = ["CHANNELS", "MonitorBus", "format_sse_event", "monitor_bus"]
