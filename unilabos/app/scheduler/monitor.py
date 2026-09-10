"""实时监控事件总线：物料 / 设备 / 动作 / 调度四通道。

进程内 pub/sub（零三方依赖）：

- 生产端：EdgeScheduler（scheduler/action/device 通道）与 InventoryService
  （material 通道）在关键节点 ``emit()``；emit 非阻塞（deque 追加 +
  put_nowait），慢消费者丢新事件，不反压调度。
- 消费端：``GET /api/v1/monitor/events`` SSE 长连接实时推送（前端
  EventSource 接收），``GET /api/v1/monitor/snapshot`` 一次性快照兜底。
- 环形缓冲只供同进程实时唤醒与诊断；跨断线/重启恢复必须读取持久全局事件游标
  （Cursor），不得把 backlog 当成历史权威。

通道约定（event.channel）：

- ``material``：仓储领域事件（lot.inbound / reservation.consumed /
  instance.deployed / ...，与 sync_outbox 同一词汇）
- ``device``：设备占用/空闲（device_busy / device_idle）
- ``action``：动作执行（job_dispatched / job_finished / job_canceled）
- ``scheduler``：平台调度（workflow_submitted / reschedule /
  workflow_state）
"""

from __future__ import annotations

import queue
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Set, Tuple

from unilabos.utils.tracing import current_trace_ids

CHANNELS = ("material", "device", "action", "scheduler")


class MonitorBus:
    """线程安全的进程内实时通知总线；不承担持久历史权威。"""

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
        """注册进程内实时订阅者。

        参数：``channels`` 是可选通道过滤，``backlog`` 只请求当前进程的诊断
        缓冲。返回：订阅身份、实时队列和尽力而为的诊断记录。异常：无；该记录
        不可用于跨断线恢复或重建调度事实。
        """
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
        """读取当前进程某通道的近期诊断记录。

        参数：``channel`` 是通道名，``limit`` 是尾部数量。返回：内存记录副本。
        异常：无；结果不承诺跨重启连续，也不是持久事件游标（Cursor）。
        """
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

__all__ = ["CHANNELS", "MonitorBus", "monitor_bus"]
