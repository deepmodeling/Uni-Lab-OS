"""物料账本的共享轮询：一个物料权威一条专用线程，所有 SSE 连接共用它的结果。

``/api/v1/events`` 的 ``material.changed``（由权威进程的前端通知写入器订阅，合并后写进 ``frontend_event``）
与 ``/api/v1/materials/events``（``materials.changed``，按连接订阅）都要知道账本前进了什么。以前每个连接每秒各读一次账本（还占着默认线程池，与 runtime.v1 控制 socket 的出站泵、
edge 代理抢同一个池），标签页一多、碰上长写入，发往 Host 的消息就被拖慢。现在：

- 只有一条专用线程读账本：有订阅者时每秒读一次末尾，有新条目就读出这一段（每批最多
  :data:`PAGE` 条，多了下一轮接着读），算好变了哪些物料，分发给全部订阅者；
- 订阅时给出自己的起点：起点落后于共享进度的，这条线程先给它补读那一段；起点超前（库被重置）的
  直接收到一个重置批次；
- 连接建立前要固定的基线（账本末尾、纪元）也在这条线程上读（:meth:`LedgerWatch.call`），不进默认线程池；
- 读账本出错只写日志，下一轮再读，不会断开订阅；分发出错的订阅者被移除；
- 没有订阅者、也没有待办的读请求时线程退出，下一个请求再启动。
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import weakref
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

#: 一批最多带出的账本条目；也是 ``material.changed`` 列物料的窗口
PAGE = 500


@dataclass(frozen=True)
class LedgerBatch:
    """``(after, upto]`` 这段账本：条目、变了的物料（None 表示列不准、整体重读），或库被重置。"""

    after: int
    upto: int
    rows: list[Any] = field(default_factory=list)
    changed: Optional[list[str]] = None
    reset: bool = False


Deliver = Callable[[LedgerBatch], None]


class LedgerWatch:
    """一个物料权威的账本共享轮询（见模块说明）。``deliver`` 在轮询线程上调用，必须很快返回。"""

    def __init__(self, service: Any, *, interval: float = 1.0) -> None:
        self._service = service
        self._interval = interval
        self._lock = threading.Lock()
        self._requests: "queue.Queue[tuple[Callable[[], Any], Future]]" = queue.Queue()
        self._subscribers: dict[int, Deliver] = {}
        self._next_token = 0
        self._latest: Optional[int] = None
        self._thread: Optional[threading.Thread] = None

    # ── 读账本（在轮询线程上） ───────────────────────────────────────

    def latest(self) -> int:
        return int(self._service.latest_ledger_sequence())

    def changed_materials(self, rows: list[Any]) -> Optional[list[str]]:
        """这些账本条目里变了的物料 uuid（Site 归到所属物料，拓扑边归到两端）；模板变了返回 None。"""

        changed: set[str] = set()
        sites: set[str] = set()
        for row in rows:
            if row.aggregate_type == "material":
                changed.add(row.aggregate_uuid)
            elif row.aggregate_type == "site":
                sites.add(row.aggregate_uuid)
            elif row.aggregate_type == "link":
                delta = getattr(row, "delta", None) or {}
                changed.update(
                    str(delta[key]) for key in ("source_material_uuid", "target_material_uuid") if delta.get(key)
                )
            elif row.aggregate_type == "resource_template":
                # 模板变化可能改变很多物料的展示，不逐个列
                return None
        # 同一个 Site 在一段账本里常出现多次（占用、释放），每个只查一次所属物料
        for site_uuid in sorted(sites):
            site = self._service.get_site(site_uuid, include_deleted=True)
            if site is not None:
                changed.add(site.owner_material_uuid)
        return sorted(changed)

    def read(self, after: int, upto: Optional[int] = None) -> LedgerBatch:
        rows = list(self._service.changes(after_sequence=after, limit=PAGE))
        if upto is not None:
            rows = [row for row in rows if row.sequence <= upto]
        end = rows[-1].sequence if rows else (upto if upto is not None else after)
        return LedgerBatch(after=after, upto=end, rows=rows, changed=self.changed_materials(rows))

    def _catch_up(self, after: int, deliver: Deliver) -> None:
        """新订阅者的起点到共享进度之间：补读给它；起点超前（库被重置）时给它一个重置批次。"""

        assert self._latest is not None
        if after > self._latest:
            deliver(LedgerBatch(after=after, upto=self._latest, reset=True))
            return
        while after < self._latest:
            batch = self.read(after, self._latest)
            if not batch.rows:
                break
            deliver(batch)
            after = batch.upto

    def _tick(self) -> bool:
        """读一轮；返回 True 表示这一批没读完，应当马上再读。"""

        latest = self.latest()
        previous = self._latest
        if previous is None:
            self._latest = latest
            return False
        if latest < previous:
            self._latest = latest
            self._fan_out(LedgerBatch(after=previous, upto=latest, reset=True))
            return False
        if latest == previous:
            return False
        batch = self.read(previous)
        if not batch.rows:
            self._latest = latest
            return False
        self._latest = batch.upto
        self._fan_out(batch)
        return len(batch.rows) >= PAGE

    def _fan_out(self, batch: LedgerBatch) -> None:
        with self._lock:
            subscribers = list(self._subscribers.items())
        for token, deliver in subscribers:
            try:
                deliver(batch)
            except Exception:  # noqa: BLE001 - 一个订阅者坏了不影响其他订阅者
                logger.exception("物料账本通知分发失败，移除该订阅者")
                self._drop(token)

    # ── 线程 ─────────────────────────────────────────────────────────

    def _ensure_thread(self) -> None:
        with self._lock:
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="materials-ledger-watch", daemon=True)
                self._thread.start()

    def _run(self) -> None:
        next_tick = time.monotonic()
        failures = 0
        while True:
            try:
                function, future = self._requests.get(timeout=max(0.0, next_tick - time.monotonic()))
            except queue.Empty:
                pass
            else:
                if future.set_running_or_notify_cancel():
                    try:
                        future.set_result(function())
                    except BaseException as exc:  # noqa: BLE001 - 交给等待方
                        future.set_exception(exc)
                continue
            with self._lock:
                if not self._subscribers and self._requests.empty():
                    # 没人订阅时进度不再前进：忘掉它，下一个请求重新读账本末尾
                    self._thread = None
                    self._latest = None
                    return
            again = False
            try:
                again = self._tick()
                failures = 0
            except Exception as exc:  # noqa: BLE001 - 读不到这一轮就不发，下一轮再读
                failures += 1
                if failures == 1 or failures % 60 == 0:
                    logger.warning("读物料账本失败（第 %s 次），下一轮再读：%s", failures, exc)
            next_tick = time.monotonic() + (0 if again else self._interval)

    def call(self, function: Callable[..., Any], *args: Any, **kwargs: Any) -> Future:
        """在轮询线程上执行一次读（连接前的基线、纪元），返回 ``concurrent.futures.Future``。"""

        future: Future = Future()
        self._requests.put((lambda: function(*args, **kwargs), future))
        self._ensure_thread()
        return future

    def baseline(self) -> Future:
        """共享进度（没在轮询时读一次账本末尾）：新连接据此固定基线。

        有订阅者时共享进度最多落后账本一轮；从它开始订阅不会漏掉变化，最多多收到一次失效。
        """

        def read() -> int:
            if self._latest is None:
                self._latest = self.latest()
            return self._latest

        return self.call(read)

    def subscribe(self, after: Optional[int], deliver: Deliver) -> Callable[[], None]:
        """从 ``after`` 之后开始收到每一批账本变化；``after`` 为 None 时从共享进度开始。返回取消订阅的函数。"""

        with self._lock:
            self._next_token += 1
            token = self._next_token

        def register() -> None:
            if self._latest is None:
                self._latest = self.latest()
            self._catch_up(self._latest if after is None else after, deliver)
            with self._lock:
                self._subscribers[token] = deliver

        self.call(register)
        return lambda: self._drop(token)

    def _drop(self, token: int) -> None:
        with self._lock:
            self._subscribers.pop(token, None)


_watches: "weakref.WeakKeyDictionary[Any, LedgerWatch]" = weakref.WeakKeyDictionary()
_watches_lock = threading.Lock()


def ledger_watch(service: Any) -> LedgerWatch:
    """这个物料权威的共享轮询（进程内唯一）。"""

    with _watches_lock:
        watch = _watches.get(service)
        if watch is None:
            watch = _watches[service] = LedgerWatch(service)
        return watch


__all__ = ["LedgerBatch", "LedgerWatch", "PAGE", "ledger_watch"]
