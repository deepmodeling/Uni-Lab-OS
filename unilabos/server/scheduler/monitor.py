"""实时监控事件总线：物料 / 设备 / 动作 / 调度 / 状态五通道。

进程内 pub/sub（零三方依赖）：

- 生产端：EdgeScheduler（scheduler/action/device 通道）与 InventoryService
  （material 通道）在关键节点 ``emit()``；emit 非阻塞（deque 追加 +
  put_nowait），慢消费者丢新事件，不反压调度。
- 消费端：``GET /api/v1/monitor/events`` SSE 长连接实时推送（前端
  EventSource 接收），``GET /api/v1/monitor/snapshot`` 一次性快照兜底。
- 环形历史缓冲支持断线重连时 backlog 回放。

通道约定（event.channel）：

- ``material``：仓储领域事件（lot.inbound / reservation.consumed /
  instance.deployed / ...，与 sync_outbox 同一词汇）
- ``device``：设备占用/空闲及状态 incident
- ``action``：动作执行（job_dispatched / job_finished / job_canceled）
- ``scheduler``：平台调度（workflow_submitted / reschedule /
  workflow_state）
- ``status``：状态联锁（status_incident_required / resolved / cleared）
"""

from __future__ import annotations

import json
import queue
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Set, Tuple

from unilabos.utils.tracing import current_trace_ids

CHANNELS = ("material", "device", "action", "scheduler", "status")


def format_sse_event(event: Dict[str, Any]) -> str:
    """按微后端监控契约编码一条 SSE 事件。"""

    payload = json.dumps(event, ensure_ascii=False, default=str)
    return (
        f"id: {event['seq']}\n"
        f"event: {event['channel']}\n"
        f"data: {payload}\n\n"
    )


class MonitorBus:
    """线程安全事件总线 + 环形历史缓冲。"""

    def __init__(self, history: int = 400, subscriber_buffer: int = 500):
        self._lock = threading.Lock()
        self._history: Deque[Dict[str, Any]] = deque(maxlen=history)
        self._subs: Dict[int, Tuple["queue.Queue[Dict[str, Any]]", Optional[Set[str]]]] = {}
        self._seq = 0
        self._next_sub_id = 0
        self._subscriber_buffer = subscriber_buffer

    # ── 生产端 ────────────────────────────────────────────────

    def emit(self, channel: str, event_type: str, data: Optional[Dict[str, Any]] = None) -> None:
        """发布事件；绝不阻塞、绝不抛出（监控故障不影响业务）。"""
        try:
            trace_id, span_id = current_trace_ids()
            with self._lock:
                self._seq += 1
                event = {
                    "seq": self._seq,
                    "ts": time.time(),
                    "channel": channel,
                    "type": event_type,
                    "data": data or {},
                    "trace_id": trace_id,
                    "span_id": span_id,
                }
                self._history.append(event)
                for q, channels in self._subs.values():
                    if channels is not None and channel not in channels:
                        continue
                    try:
                        q.put_nowait(event)
                    except queue.Full:
                        pass  # 慢消费者丢事件，靠 seq 空洞 + snapshot 自愈
        except Exception:  # noqa: BLE001
            pass

    # ── 消费端 ────────────────────────────────────────────────

    def subscribe(
        self, channels: Optional[Set[str]] = None, backlog: int = 0
    ) -> Tuple[int, "queue.Queue[Dict[str, Any]]", List[Dict[str, Any]]]:
        """注册订阅者；返回 (id, 事件队列, backlog 回放列表)。"""
        with self._lock:
            self._next_sub_id += 1
            sub_id = self._next_sub_id
            q: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=self._subscriber_buffer)
            self._subs[sub_id] = (q, channels)
            replay: List[Dict[str, Any]] = []
            if backlog > 0:
                for event in self._history:
                    if channels is not None and event["channel"] not in channels:
                        continue
                    replay.append(event)
                replay = replay[-backlog:]
            return sub_id, q, replay

    def unsubscribe(self, sub_id: int) -> None:
        with self._lock:
            self._subs.pop(sub_id, None)

    def recent(self, channel: str, limit: int = 40) -> List[Dict[str, Any]]:
        """某通道最近 N 条（snapshot 端点用）。"""
        with self._lock:
            out = [e for e in self._history if e["channel"] == channel]
            return out[-limit:]

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)


# 进程级单例：composition root 注入给 EdgeScheduler / InventoryService，
# API 层直接引用（同进程同实例）。
monitor_bus = MonitorBus()

__all__ = ["CHANNELS", "MonitorBus", "format_sse_event", "monitor_bus"]
