"""Host 微后端实时事件总线。

接口形状与 ``feat/edge-networking-and-scheduler`` 的 MonitorBus 保持一致：
生产端非阻塞写入环形历史，前端通过 SSE 接收增量，并用 REST snapshot 自愈。
"""

from __future__ import annotations

import json
import queue
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Set, Tuple


CHANNELS = ("action",)


class MonitorBus:
    """线程安全的进程内事件总线和有界历史缓冲。"""

    def __init__(self, history: int = 400, subscriber_buffer: int = 500):
        self._lock = threading.Lock()
        self._history: Deque[Dict[str, Any]] = deque(maxlen=history)
        self._subs: Dict[
            int,
            Tuple["queue.Queue[Dict[str, Any]]", Optional[Set[str]]],
        ] = {}
        self._seq = 0
        self._next_sub_id = 0
        self._subscriber_buffer = subscriber_buffer

    def emit(
        self,
        channel: str,
        event_type: str,
        data: Optional[Dict[str, Any]] = None,
    ) -> None:
        """发布事件；观测链路失败不得阻断设备与调度执行。"""

        try:
            with self._lock:
                self._seq += 1
                event = {
                    "seq": self._seq,
                    "ts": time.time(),
                    "channel": channel,
                    "type": event_type,
                    "data": data or {},
                    "trace_id": "",
                    "span_id": "",
                }
                self._history.append(event)
                for subscriber_queue, channels in self._subs.values():
                    if channels is not None and channel not in channels:
                        continue
                    try:
                        subscriber_queue.put_nowait(event)
                    except queue.Full:
                        # 前端根据 seq 空洞重新拉 snapshot，不允许慢消费者反压执行。
                        pass
        except Exception:  # noqa: BLE001 - 观测故障必须 fail-open
            pass

    def subscribe(
        self,
        channels: Optional[Set[str]] = None,
        backlog: int = 0,
    ) -> Tuple[int, "queue.Queue[Dict[str, Any]]", List[Dict[str, Any]]]:
        """注册订阅者，返回订阅 ID、增量队列和历史回放。"""

        with self._lock:
            self._next_sub_id += 1
            sub_id = self._next_sub_id
            subscriber_queue: "queue.Queue[Dict[str, Any]]" = queue.Queue(
                maxsize=self._subscriber_buffer
            )
            self._subs[sub_id] = (subscriber_queue, channels)
            replay = [
                event
                for event in self._history
                if channels is None or event["channel"] in channels
            ]
            if backlog <= 0:
                replay = []
            else:
                replay = replay[-backlog:]
            return sub_id, subscriber_queue, replay

    def unsubscribe(self, sub_id: int) -> None:
        with self._lock:
            self._subs.pop(sub_id, None)

    def recent(self, channel: str, limit: int = 40) -> List[Dict[str, Any]]:
        with self._lock:
            events = [event for event in self._history if event["channel"] == channel]
            return events[-limit:]


def format_sse_event(event: Dict[str, Any]) -> str:
    """按 Edge monitor 契约编码一条 SSE 事件。"""

    payload = json.dumps(event, ensure_ascii=False, default=str)
    return (
        f"id: {event['seq']}\n"
        f"event: {event['channel']}\n"
        f"data: {payload}\n\n"
    )


monitor_bus = MonitorBus()


__all__ = ["CHANNELS", "MonitorBus", "format_sse_event", "monitor_bus"]
